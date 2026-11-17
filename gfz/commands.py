"""Command behaviour: settings and a HostClient in, data out; no provider or render."""

import shutil
import socket
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence

from gfz.hosts import ActionStatus, Host, HostClient, HostStatus, PowerAction
from gfz.settings import Settings

WAIT_SECONDS = 300.0
POLL_INTERVAL_SECONDS = 5.0
PROBE_TIMEOUT_SECONDS = 5.0
SSH_PORT = 22

STATUS_AFTER_ACTION: Mapping[PowerAction, HostStatus] = {
    "power_on": "active",
    "shutdown": "off",
    "power_off": "off",
}


class CommandError(Exception):
    """A gfz command refused to act, or could not."""


def _report_nothing(line: str) -> None:
    """Drop a progress line, for a caller that wants no progress."""


def resolve_host(client: HostClient, name: str) -> Host:
    """Return the account's single host named `name`.

    Every command resolves a droplet the same way: list the account's
    droplets and match on `name`.

    Raises:
        CommandError: No host in the account carries `name`, or more than
            one does.
    """
    matches = [host for host in client.list_hosts() if host.name == name]
    if not matches:
        raise CommandError(f"no host named {name!r}")
    if len(matches) > 1:
        ids = ", ".join(str(host.id) for host in matches)
        raise CommandError(f"more than one host named {name!r}: ids {ids}")
    return matches[0]


def list_hosts(
    client: HostClient, settings: Settings, *, all_hosts: bool, tag: str | None
) -> Sequence[Host]:
    """Return the hosts `gfz list` prints.

    `all_hosts` true returns every host in the account and overrides `tag`.
    Otherwise the selecting tag is `tag` when given, else `settings.tag`.
    """
    if all_hosts:
        selected_tag = None
    elif tag is not None:
        selected_tag = tag
    else:
        selected_tag = settings.tag
    return client.list_hosts(tag=selected_tag)


def _tcp_port_answers(address: str, port: int) -> bool:
    """Return whether a TCP connection to `address` on `port` succeeds.

    Times out after PROBE_TIMEOUT_SECONDS. A refused or timed-out connection
    means the host is not reachable yet rather than that the probe failed,
    so both cases return False instead of raising.
    """
    try:
        with socket.create_connection((address, port), timeout=PROBE_TIMEOUT_SECONDS):
            return True
    except OSError:
        return False


def _run_ssh(command: Sequence[str]) -> int:
    """Run `command` as ssh, handing it the terminal, and return its exit code.

    Nothing is piped or captured: ssh owns stdin, stdout, and stderr directly,
    since an interactive shell depends on holding the real terminal.
    """
    completed = subprocess.run(  # noqa: S603 — shutil.which resolved the binary
        command, check=False
    )
    return completed.returncode


# The seams a test replaces. Read them as module globals inside the function
# bodies — never capture them in a default argument, or monkeypatch cannot
# reach them.
probe_tcp_port: Callable[[str, int], bool] = _tcp_port_answers
now_seconds: Callable[[], float] = time.monotonic
sleep_seconds: Callable[[float], None] = time.sleep
launch_ssh: Callable[[Sequence[str]], int] = _run_ssh


def _host_is_active(host: Host) -> bool:
    """Report whether `host` reports status "active"."""
    return host.status == "active"


def _host_has_address(host: Host) -> bool:
    """Report whether `host` carries a public IPv4 address."""
    return host.ip is not None


def _host_answers_ssh(host: Host) -> bool:
    """Report whether `host` answers a TCP connection on SSH_PORT.

    Reads `probe_tcp_port` as a module global at call time, the seam a test
    monkeypatches.
    """
    return host.ip is not None and probe_tcp_port(host.ip, SSH_PORT)


def _reachability_expired(host: Host) -> CommandError:
    """Build the error create_host raises once its wait budget runs out."""
    address = host.ip if host.ip is not None else "no address yet"
    return CommandError(
        f"host {host.name!r} (id {host.id}) exists but did not become "
        f"reachable within {WAIT_SECONDS:.0f}s: address is {address}"
    )


def _poll_host(
    client: HostClient,
    host: Host,
    deadline: float,
    ready: Callable[[Host], bool],
    expired: Callable[[Host], CommandError],
) -> Host:
    """Poll `host` until `ready` reports true, and return it in that state.

    Polls every POLL_INTERVAL_SECONDS until `deadline`. A host already
    satisfying `ready` is returned without a poll. `expired` builds the
    failure from the host as it last reported.

    Raises:
        CommandError: `deadline` passes before `ready` is satisfied.
    """
    while not ready(host):
        if now_seconds() >= deadline:
            raise expired(host)
        sleep_seconds(POLL_INTERVAL_SECONDS)
        host = client.get_host(host.id)
    return host


def _wait_for_status(client: HostClient, host: Host, wanted: HostStatus) -> Host:
    """Poll `host` until it reports status `wanted`, and return it in that state.

    Waits on one budget of WAIT_SECONDS, polling every
    POLL_INTERVAL_SECONDS. A host already reporting `wanted` is returned
    without a poll.

    Raises:
        CommandError: The host did not reach `wanted` within the budget. The
            message names the last status it reported.
    """
    return _poll_host(
        client,
        host,
        now_seconds() + WAIT_SECONDS,
        lambda polled: polled.status == wanted,
        lambda polled: CommandError(
            f"host {polled.name!r} (id {polled.id}) did not reach status {wanted} "
            f"within {WAIT_SECONDS:.0f}s: status is {polled.status}"
        ),
    )


def create_host(
    client: HostClient,
    settings: Settings,
    name: str,
    *,
    report: Callable[[str], None] = _report_nothing,
) -> Host:
    """Create a host named `name` and wait for it to become reachable.

    Sends the create call with the configured region, size, image, ssh keys,
    and tag. Then waits, on one budget of WAIT_SECONDS, through three phases
    in turn: for the host to report status "active", then for it to carry a
    public IPv4 address, and only then for a TCP connection to port 22 to
    succeed. The host is never probed before it is active and addressed.
    Reports a start line and a done line for the create call and for each of
    the three waits, through `report`.

    Raises:
        CommandError: The host did not become reachable within the wait
            budget. The host is left in place — a droplet slow to boot is
            not a droplet to throw away.
    """
    report(f"creating {name}")
    host = client.create_host(
        name=name,
        region=settings.region,
        size=settings.size,
        image=settings.image,
        ssh_keys=settings.ssh_keys,
        tags=[settings.tag],
    )
    report(f"created {name} (id {host.id})")

    deadline = now_seconds() + WAIT_SECONDS

    report(f"waiting for {name} to become active")
    host = _poll_host(client, host, deadline, _host_is_active, _reachability_expired)
    report(f"{name} is active")

    report(f"waiting for {name} to get an address")
    host = _poll_host(client, host, deadline, _host_has_address, _reachability_expired)
    address = host_address(host)
    report(f"{name} has address {address}")

    report(f"waiting for ssh on {address}")
    host = _poll_host(client, host, deadline, _host_answers_ssh, _reachability_expired)
    report(f"ssh answers on {address}")

    return host


def power_host(
    client: HostClient,
    name: str,
    action: PowerAction,
    *,
    report: Callable[[str], None] = _report_nothing,
) -> tuple[Host, ActionStatus]:
    """Resolve `name`, run `action` on it, and wait for the host to settle.

    Returns the host as it stands once it reports the status the action
    leads to — `active` for `power_on`, `off` for either stop — together
    with the status the provider reported for the action. An action the
    provider reports `errored` returns at once, with the host as it stood
    before it, and reports no wait. Otherwise reports a start line and a
    done line for the wait, through `report`.

    Raises:
        CommandError: No host carries `name`, more than one does, or the
            host did not reach the action's status within the wait budget.
    """
    host = resolve_host(client, name)
    status = client.power_host(host.id, action)
    if status == "errored":
        return host, status
    wanted = STATUS_AFTER_ACTION[action]
    report(f"waiting for {host.name} to report {wanted}")
    settled = _wait_for_status(client, host, wanted)
    report(f"{host.name} reports {wanted}")
    return settled, status


def host_to_destroy(
    client: HostClient, settings: Settings, name: str, *, force: bool
) -> Host:
    """Resolve `name` and return it, refusing a host missing the configured tag.

    Raises:
        CommandError: No host carries `name`, more than one does, or the
            host does not carry `settings.tag` and `force` is False.
    """
    host = resolve_host(client, name)
    if settings.tag not in host.tags and not force:
        tags = ", ".join(host.tags) if host.tags else "none"
        raise CommandError(
            f"host {host.name!r} (id {host.id}) does not carry tag "
            f"{settings.tag!r}: tags are {tags}; pass --force to destroy it anyway"
        )
    return host


def _host_exists(client: HostClient, host_id: int) -> bool:
    """Report whether the account still holds a host with this id."""
    return any(host.id == host_id for host in client.list_hosts())


def _wait_until_gone(client: HostClient, host: Host) -> None:
    """Poll the account until it no longer lists `host`.

    Waits on one budget of WAIT_SECONDS. The account's own listing is the
    test for gone, since listing is how every command finds a host in the
    first place.

    Raises:
        CommandError: The account still lists the host when the budget
            expires.
    """
    deadline = now_seconds() + WAIT_SECONDS
    while _host_exists(client, host.id):
        if now_seconds() >= deadline:
            raise CommandError(
                f"host {host.name!r} (id {host.id}) was deleted but the account "
                f"still lists it {WAIT_SECONDS:.0f}s later"
            )
        sleep_seconds(POLL_INTERVAL_SECONDS)


def destroy_host(
    client: HostClient,
    host: Host,
    *,
    typed_name: str | None,
    report: Callable[[str], None] = _report_nothing,
) -> None:
    """Delete `host` after checking the typed confirmation, then wait for it to go.

    `typed_name` is the confirmation line the operator typed back. `None`
    means the operator already confirmed with `--yes`, so no check happens;
    any other value must equal `host.name`. Once the delete call is sent,
    waits for the account to stop listing the host, reporting a start line
    and a done line for that wait, through `report`.

    Raises:
        CommandError: `typed_name` is given and does not equal `host.name`,
            in which case nothing is deleted; or the account still lists the
            host when the wait budget expires.
    """
    if typed_name is not None and typed_name != host.name:
        raise CommandError(
            f"typed {typed_name!r} but expected {host.name!r}: nothing was destroyed"
        )
    client.delete_host(host.id)
    report(f"waiting for the account to stop listing {host.name}")
    _wait_until_gone(client, host)
    report(f"{host.name} is gone from the account")


def host_address(host: Host) -> str:
    """Return the public address anything reaching `host` connects to.

    Raises:
        CommandError: The host has no address. A droplet that is still new,
            or that never became active, has none.
    """
    if host.ip is None:
        raise CommandError(f"host {host.name!r} (id {host.id}) has no address")
    return host.ip


def ssh_command(settings: Settings, host: Host, *, as_agent: bool) -> list[str]:
    """Build the argv that opens an interactive ssh session on `host`.

    The user is `settings.agent_user` when `as_agent` is true, else
    `settings.admin_user`. `settings.ssh_identity` is stored unexpanded, so
    it is expanded here, at the point of use.

    Raises:
        CommandError: No `ssh` binary is on PATH, or `host` has no address.
    """
    binary = shutil.which("ssh")
    if binary is None:
        raise CommandError("no ssh client found on PATH")
    address = host_address(host)
    user = settings.agent_user if as_agent else settings.admin_user
    identity = settings.ssh_identity.expanduser()
    return [binary, "-i", str(identity), f"{user}@{address}"]


def open_shell(
    client: HostClient, settings: Settings, name: str, *, as_agent: bool
) -> int:
    """Resolve `name` and open an interactive ssh session on it.

    Launches the built command through the `launch_ssh` seam and returns its
    exit code.

    Raises:
        CommandError: No host carries `name`, more than one does, no `ssh`
            binary is on PATH, or the host has no address.
    """
    host = resolve_host(client, name)
    command = ssh_command(settings, host, as_agent=as_agent)
    return launch_ssh(command)
