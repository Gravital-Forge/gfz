"""Tests for gfz's command behaviour: resolution, listing, and lifecycle waits."""

import shutil
import time
from collections.abc import Sequence
from pathlib import Path

import pytest

import gfz.commands
from gfz.commands import (
    WAIT_SECONDS,
    CommandError,
    create_host,
    destroy_host,
    host_to_destroy,
    list_hosts,
    open_shell,
    power_host,
    resolve_host,
    ssh_command,
)
from gfz.hosts import Host
from gfz.settings import Settings
from tests.conftest import NEW_HOST_ID, FakeHostClient, make_host


class _SlowDeleteClient(FakeHostClient):
    """A fake whose account keeps listing a deleted host for a few more listings.

    `listings_still_showing_it` is how many listings after the delete call
    still carry the host, which is what a destroy wait has to poll through.
    """

    def __init__(
        self, hosts: Sequence[Host], *, listings_still_showing_it: int
    ) -> None:
        """Build the fake over `hosts`, with the delete visible only after those."""
        super().__init__(hosts, deletion_takes_effect=False)
        self.listings_still_showing_it = listings_still_showing_it

    def list_hosts(self, *, tag: str | None = None) -> list[Host]:
        """List the account, dropping deleted hosts once those listings are spent."""
        if self.listings_still_showing_it > 0:
            self.listings_still_showing_it -= 1
        else:
            self.hosts = [
                host for host in self.hosts if host.id not in self.delete_calls
            ]
        return super().list_hosts(tag=tag)


@pytest.fixture
def no_sleep(monkeypatch):
    """Replace sleep_seconds with a no-op so a test never really sleeps."""
    monkeypatch.setattr(gfz.commands, "sleep_seconds", lambda seconds: None)


def test_resolve_host_returns_the_single_match():
    """resolve_host returns the one account host carrying the given name."""
    host = make_host(id=101, name="alpha")
    client = FakeHostClient(hosts=[host])

    resolved = resolve_host(client, "alpha")

    assert resolved == host


def test_resolve_host_raises_and_names_the_name_when_no_match():
    """resolve_host raises CommandError naming the name when nothing matches."""
    client = FakeHostClient(hosts=[make_host(id=101, name="alpha")])

    with pytest.raises(CommandError) as excinfo:
        resolve_host(client, "missing")

    assert "missing" in str(excinfo.value)


def test_resolve_host_raises_and_names_both_ids_on_duplicate_names():
    """Two hosts sharing a name raise CommandError naming both ids."""
    client = FakeHostClient(
        hosts=[
            make_host(id=101, name="alpha"),
            make_host(id=202, name="alpha"),
        ]
    )

    with pytest.raises(CommandError) as excinfo:
        resolve_host(client, "alpha")

    message = str(excinfo.value)
    assert "101" in message
    assert "202" in message


def test_list_hosts_default_lists_the_configured_tag(settings):
    """With no flag, list_hosts asks the client for the settings-configured tag."""
    client = FakeHostClient(hosts=[make_host()])

    list_hosts(client, settings, all_hosts=False, tag=None)

    assert client.list_calls == [settings.tag]


def test_list_hosts_all_asks_for_every_host(settings):
    """--all semantics ask the client for tag=None, regardless of the configured tag."""
    client = FakeHostClient(hosts=[make_host()])

    list_hosts(client, settings, all_hosts=True, tag=None)

    assert client.list_calls == [None]


def test_list_hosts_explicit_tag_wins_over_configured_tag(settings):
    """An explicit tag overrides the settings-configured tag."""
    client = FakeHostClient(hosts=[make_host()])

    list_hosts(client, settings, all_hosts=False, tag="other-tag")

    assert client.list_calls == ["other-tag"]


def test_list_hosts_all_hosts_beats_an_explicit_tag(settings):
    """all_hosts=True overrides an explicit tag as well as the configured one."""
    client = FakeHostClient(hosts=[make_host()])

    list_hosts(client, settings, all_hosts=True, tag="other-tag")

    assert client.list_calls == [None]


def test_list_hosts_keeps_a_host_with_no_address(settings):
    """A host with ip=None is still returned; the row is not dropped."""
    host = make_host(ip=None)
    client = FakeHostClient(hosts=[host])

    result = list_hosts(client, settings, all_hosts=True, tag=None)

    assert host in result


@pytest.mark.parametrize(
    ("action", "before", "after"),
    [
        ("power_on", "off", "active"),
        ("shutdown", "active", "off"),
        ("power_off", "active", "off"),
    ],
)
def test_power_host_waits_for_the_status_the_action_leads_to(
    no_sleep, action, before, after
):
    """Each action reaches the client by id and returns the host in its end status."""
    host = make_host(id=101, name="alpha", status=before)
    settled = make_host(id=101, name="alpha", status=after)
    client = FakeHostClient(hosts=[host], polls=[settled], power_status="in-progress")

    resolved, status = power_host(client, "alpha", action)

    assert client.power_calls == [(101, action)]
    assert client.get_calls == [101]
    assert resolved == settled
    assert status == "in-progress"


def test_power_host_polls_through_the_states_before_the_wanted_one(no_sleep):
    """The wait keeps polling while the host reports anything but the end status."""
    host = make_host(id=101, name="alpha", status="off")
    active = make_host(id=101, name="alpha", status="active")
    client = FakeHostClient(hosts=[host], polls=[host, host, active])

    resolved, _ = power_host(client, "alpha", "power_on")

    assert client.get_calls == [101, 101, 101]
    assert resolved == active


def test_power_host_returns_at_once_when_the_host_already_reports_it(no_sleep):
    """A host already in the action's end status settles without a single poll."""
    host = make_host(id=101, name="alpha", status="active")
    client = FakeHostClient(hosts=[host])

    resolved, _ = power_host(client, "alpha", "power_on")

    assert client.get_calls == []
    assert resolved == host


def test_power_host_errored_action_returns_without_waiting():
    """An errored action returns its status at once, with no wait and no poll."""
    host = make_host(id=101, name="alpha", status="off")
    client = FakeHostClient(hosts=[host], power_status="errored")

    resolved, status = power_host(client, "alpha", "power_on")

    assert status == "errored"
    assert resolved == host
    assert client.get_calls == []


def test_power_host_raises_on_expiry_naming_the_last_status(fake_clock):
    """A host that never reaches the end status raises, naming what it does report."""
    stuck = make_host(id=101, name="alpha", status="off")
    client = FakeHostClient(hosts=[stuck], polls=[stuck])

    with pytest.raises(CommandError) as excinfo:
        power_host(client, "alpha", "power_on")

    message = str(excinfo.value)
    assert "101" in message
    assert "off" in message
    assert "active" in message


def test_power_host_deletes_nothing_on_expiry(fake_clock):
    """A host that never reaches the end status is left in place, never deleted."""
    stuck = make_host(id=101, name="alpha", status="active")
    client = FakeHostClient(hosts=[stuck], polls=[stuck])

    with pytest.raises(CommandError):
        power_host(client, "alpha", "shutdown")

    assert client.delete_calls == []
    assert client.hosts == [stuck]


def test_an_expiring_wait_spends_the_budget_on_the_clock_not_the_wall(fake_clock):
    """The full wait budget elapses on the injected clock and costs no real time."""
    stuck = make_host(id=101, name="alpha", status="off")
    client = FakeHostClient(hosts=[stuck], polls=[stuck])

    started = time.monotonic()
    with pytest.raises(CommandError):
        power_host(client, "alpha", "power_on")
    real_seconds = time.monotonic() - started

    assert fake_clock.seconds >= WAIT_SECONDS
    assert real_seconds < 1.0


def test_power_host_reports_progress_through_the_wait(no_sleep, reporter):
    """power_host reports the wait for the action's status as start and done lines."""
    host = make_host(id=101, name="alpha", status="off")
    settled = make_host(id=101, name="alpha", status="active")
    client = FakeHostClient(hosts=[host], polls=[settled], power_status="in-progress")

    power_host(client, "alpha", "power_on", report=reporter)

    assert reporter.lines == [
        "waiting for alpha to report active",
        "alpha reports active",
    ]


def test_power_host_unknown_name_raises_before_any_power_call():
    """An unknown name raises CommandError before the client sees a power call."""
    client = FakeHostClient(hosts=[make_host(id=101, name="alpha")])

    with pytest.raises(CommandError):
        power_host(client, "missing", "power_on")

    assert client.power_calls == []


def test_create_host_sends_the_configured_create_arguments(monkeypatch, settings):
    """create_host sends the configured region, size, image, keys, and tag."""
    monkeypatch.setattr(gfz.commands, "sleep_seconds", lambda seconds: None)
    monkeypatch.setattr(gfz.commands, "probe_tcp_port", lambda address, port: True)
    active = make_host(id=NEW_HOST_ID, name="alpha", status="active", ip="203.0.113.10")
    client = FakeHostClient(polls=[active])

    create_host(client, settings, "alpha")

    assert client.create_calls == [
        {
            "name": "alpha",
            "region": settings.region,
            "size": settings.size,
            "image": settings.image,
            "ssh_keys": list(settings.ssh_keys),
            "tags": [settings.tag],
        }
    ]


def test_create_host_returns_once_active_addressed_and_reachable(
    monkeypatch, settings, no_sleep
):
    """create_host returns the host once active, addressed, and reachable."""
    active = make_host(id=NEW_HOST_ID, name="alpha", status="active", ip="203.0.113.10")
    client = FakeHostClient(polls=[active])
    monkeypatch.setattr(gfz.commands, "probe_tcp_port", lambda address, port: True)

    result = create_host(client, settings, "alpha")

    assert result == active


def test_create_host_does_not_probe_before_active_and_addressed(
    monkeypatch, settings, no_sleep
):
    """The probe never runs while the host is still new or has no address."""
    not_ready = make_host(id=NEW_HOST_ID, name="alpha", status="new", ip=None)
    ready = make_host(id=NEW_HOST_ID, name="alpha", status="active", ip="203.0.113.10")
    client = FakeHostClient(polls=[not_ready, ready])
    probe_calls = []

    def _recording_probe(address, port):
        """Record the probed address and port, and report the port open."""
        probe_calls.append((address, port))
        return True

    monkeypatch.setattr(gfz.commands, "probe_tcp_port", _recording_probe)

    create_host(client, settings, "alpha")

    assert probe_calls == [("203.0.113.10", gfz.commands.SSH_PORT)]


def test_create_host_polls_through_refused_connections(monkeypatch, settings, no_sleep):
    """create_host keeps polling while the port refuses, and returns once open."""
    ready = make_host(id=NEW_HOST_ID, name="alpha", status="active", ip="203.0.113.10")
    client = FakeHostClient(polls=[ready])
    answers = [False, False, True]

    def _next_answer(address, port):
        """Report the next scripted probe answer."""
        return answers.pop(0)

    monkeypatch.setattr(gfz.commands, "probe_tcp_port", _next_answer)

    result = create_host(client, settings, "alpha")

    assert result == ready
    assert answers == []


def test_create_host_raises_on_expiry_naming_the_id_and_address(
    monkeypatch, settings, fake_clock
):
    """The wait expires after the 300-second budget, naming the id and address."""
    stuck = make_host(id=NEW_HOST_ID, name="alpha", status="active", ip="203.0.113.10")
    client = FakeHostClient(polls=[stuck])
    monkeypatch.setattr(gfz.commands, "probe_tcp_port", lambda address, port: False)

    with pytest.raises(CommandError) as excinfo:
        create_host(client, settings, "alpha")

    message = str(excinfo.value)
    assert str(NEW_HOST_ID) in message
    assert "203.0.113.10" in message


def test_create_host_raises_on_expiry_with_no_address_yet(
    monkeypatch, settings, fake_clock
):
    """Expiry with no address yet says so, instead of naming an address."""
    stuck = make_host(id=NEW_HOST_ID, name="alpha", status="new", ip=None)
    client = FakeHostClient(polls=[stuck])
    monkeypatch.setattr(gfz.commands, "probe_tcp_port", lambda address, port: True)

    with pytest.raises(CommandError) as excinfo:
        create_host(client, settings, "alpha")

    assert "no address" in str(excinfo.value)


def test_create_host_never_deletes_the_host_on_expiry(
    monkeypatch, settings, fake_clock
):
    """A host that never becomes reachable is left in place, never deleted."""
    stuck = make_host(id=NEW_HOST_ID, name="alpha", status="new", ip=None)
    client = FakeHostClient(polls=[stuck])
    monkeypatch.setattr(gfz.commands, "probe_tcp_port", lambda address, port: True)

    with pytest.raises(CommandError):
        create_host(client, settings, "alpha")

    assert client.delete_calls == []


def test_create_host_reports_progress_through_each_phase(
    monkeypatch, settings, no_sleep, reporter
):
    """create_host reports the create call and each wait, as start and done lines."""
    active = make_host(id=NEW_HOST_ID, name="alpha", status="active", ip="203.0.113.10")
    client = FakeHostClient(polls=[active])
    monkeypatch.setattr(gfz.commands, "probe_tcp_port", lambda address, port: True)

    create_host(client, settings, "alpha", report=reporter)

    assert reporter.lines == [
        "creating alpha",
        f"created alpha (id {NEW_HOST_ID})",
        "waiting for alpha to become active",
        "alpha is active",
        "waiting for alpha to get an address",
        "alpha has address 203.0.113.10",
        "waiting for ssh on 203.0.113.10",
        "ssh answers on 203.0.113.10",
    ]


def test_host_to_destroy_refuses_a_host_missing_the_configured_tag(settings):
    """host_to_destroy refuses a host that does not carry the configured tag."""
    host = make_host(id=101, name="alpha", tags=("other",))
    client = FakeHostClient(hosts=[host])

    with pytest.raises(CommandError) as excinfo:
        host_to_destroy(client, settings, "alpha", force=False)

    message = str(excinfo.value)
    assert "alpha" in message
    assert "101" in message
    assert settings.tag in message
    assert "other" in message
    assert client.delete_calls == []


def test_host_to_destroy_force_overrides_the_missing_tag(settings):
    """force=True lets host_to_destroy return a host missing the configured tag."""
    host = make_host(id=101, name="alpha", tags=("other",))
    client = FakeHostClient(hosts=[host])

    resolved = host_to_destroy(client, settings, "alpha", force=True)

    assert resolved == host


def test_destroy_host_mismatched_typed_name_raises_and_deletes_nothing():
    """A typed name that does not match the host's name deletes nothing."""
    host = make_host(id=101, name="alpha")
    client = FakeHostClient(hosts=[host])

    with pytest.raises(CommandError) as excinfo:
        destroy_host(client, host, typed_name="wrong")

    message = str(excinfo.value)
    assert "wrong" in message
    assert "alpha" in message
    assert client.delete_calls == []


def test_destroy_host_matching_typed_name_deletes_only_that_host():
    """A matching typed name deletes exactly that host; another host survives."""
    host = make_host(id=101, name="alpha")
    other = make_host(id=202, name="bravo")
    client = FakeHostClient(hosts=[host, other])

    destroy_host(client, host, typed_name="alpha")

    assert client.delete_calls == [101]
    assert other in client.hosts


def test_destroy_host_none_typed_name_deletes_without_a_check():
    """typed_name=None deletes without any confirmation check."""
    host = make_host(id=101, name="alpha")
    client = FakeHostClient(hosts=[host])

    destroy_host(client, host, typed_name=None)

    assert client.delete_calls == [101]


def test_destroy_host_waits_until_the_account_stops_listing_the_host(no_sleep):
    """The wait polls the account's listing until the deleted host is gone from it."""
    host = make_host(id=101, name="alpha")
    client = _SlowDeleteClient([host], listings_still_showing_it=2)

    destroy_host(client, host, typed_name=None)

    assert client.delete_calls == [101]
    assert len(client.list_calls) == 3
    assert client.hosts == []


def test_destroy_host_reports_progress_through_the_wait(no_sleep, reporter):
    """destroy_host reports the wait for the account to stop listing the host."""
    host = make_host(id=101, name="alpha")
    client = _SlowDeleteClient([host], listings_still_showing_it=2)

    destroy_host(client, host, typed_name=None, report=reporter)

    assert reporter.lines == [
        "waiting for the account to stop listing alpha",
        "alpha is gone from the account",
    ]


def test_destroy_host_raises_on_expiry_naming_the_host_that_is_still_listed(fake_clock):
    """A host the account keeps listing raises once the budget expires."""
    host = make_host(id=101, name="alpha")
    client = FakeHostClient(hosts=[host], deletion_takes_effect=False)

    with pytest.raises(CommandError) as excinfo:
        destroy_host(client, host, typed_name=None)

    message = str(excinfo.value)
    assert "alpha" in message
    assert "101" in message


def test_destroy_host_deletes_nothing_further_while_it_waits(fake_clock):
    """The wait sends one delete call and no other, however long it polls."""
    host = make_host(id=101, name="alpha")
    bystander = make_host(id=202, name="bravo")
    client = FakeHostClient(hosts=[host, bystander], deletion_takes_effect=False)

    with pytest.raises(CommandError):
        destroy_host(client, host, typed_name=None)

    assert client.delete_calls == [101]
    assert bystander in client.hosts


def test_ssh_command_builds_the_argv_with_binary_identity_and_user_at_address(
    monkeypatch, settings
):
    """The built argv carries the resolved binary, -i identity, and user@address."""
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/ssh")
    host = make_host(ip="203.0.113.10")

    command = ssh_command(settings, host, as_agent=False)

    assert command == [
        "/usr/bin/ssh",
        "-i",
        str(settings.ssh_identity),
        f"{settings.admin_user}@203.0.113.10",
    ]


def test_ssh_command_agent_flag_uses_the_agent_user(monkeypatch, settings):
    """--agent swaps the admin user for the configured agent user."""
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/ssh")
    host = make_host(ip="203.0.113.10")

    command = ssh_command(settings, host, as_agent=True)

    assert command[-1] == f"{settings.agent_user}@203.0.113.10"


def test_ssh_command_expands_a_tilde_identity(monkeypatch, required_environment):
    """A `~` in GFZ_SSH_IDENTITY comes out expanded, not as a literal tilde."""
    monkeypatch.setenv("GFZ_SSH_IDENTITY", "~/.ssh/id_ed25519")
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/ssh")
    tilde_settings = Settings()
    host = make_host(ip="203.0.113.10")

    command = ssh_command(tilde_settings, host, as_agent=False)

    identity = command[2]
    assert "~" not in identity
    assert identity == str(Path("~/.ssh/id_ed25519").expanduser())


def test_ssh_command_raises_when_no_ssh_binary_on_path(monkeypatch, settings):
    """A missing ssh client on PATH raises CommandError."""
    monkeypatch.setattr(shutil, "which", lambda name: None)
    host = make_host(ip="203.0.113.10")

    with pytest.raises(CommandError):
        ssh_command(settings, host, as_agent=False)


def test_ssh_command_raises_when_host_has_no_address(monkeypatch, settings):
    """A host with no address raises CommandError naming the host and its id."""
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/ssh")
    host = make_host(id=101, name="alpha", ip=None)

    with pytest.raises(CommandError) as excinfo:
        ssh_command(settings, host, as_agent=False)

    message = str(excinfo.value)
    assert "alpha" in message
    assert "101" in message


def test_open_shell_passes_the_built_command_to_launch_ssh_and_returns_its_exit_code(
    monkeypatch, settings
):
    """open_shell hands the built argv to launch_ssh and returns its exit code."""
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/ssh")
    host = make_host(id=101, name="alpha", ip="203.0.113.10")
    client = FakeHostClient(hosts=[host])
    expected_command = ssh_command(settings, host, as_agent=False)
    recorded_commands = []

    def _fake_launch(command):
        """Record the launched command and report a distinctive exit code."""
        recorded_commands.append(command)
        return 42

    monkeypatch.setattr(gfz.commands, "launch_ssh", _fake_launch)

    exit_code = open_shell(client, settings, "alpha", as_agent=False)

    assert exit_code == 42
    assert recorded_commands == [expected_command]
