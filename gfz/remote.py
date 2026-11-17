"""The ssh transport: run one rendered step's script on a host and capture it."""

import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from gfz.commands import CommandError, host_address
from gfz.hosts import Host
from gfz.settings import Settings
from gfz.steps.registry import Login, Step


@dataclass(frozen=True)
class RemoteResult:
    """What one remote script run produced.

    Attributes:
        exit_code: The exit code the remote shell returned.
        stdout: Everything the script wrote to stdout.
        stderr: Everything the script wrote to stderr.
    """

    exit_code: int
    stdout: str
    stderr: str


class ScriptRunner(Protocol):
    """Runs one step's rendered script on one host.

    Two implementations exist from the day this is written: the real ssh
    runner and a test double. The step travels with the script because the
    step decides which account runs it and whether the session forwards the
    ssh agent.
    """

    def __call__(self, step: Step, script: str) -> RemoteResult:
        """Run `script` on the host the way `step` asks for, and return the result."""
        ...


CONNECT_TIMEOUT_SECONDS = 30


def _capture(command: Sequence[str], script: str) -> subprocess.CompletedProcess[str]:
    """Run `command` with `script` on its stdin, and capture what it wrote."""
    return subprocess.run(  # noqa: S603 — shutil.which resolved the binary
        command, input=script, capture_output=True, text=True, check=False
    )


# The seam a test replaces. Read it as a module global inside the function
# body — never capture it in a default argument, since monkeypatch cannot
# reach a default argument, which binds at definition time.
capture_ssh: Callable[[Sequence[str], str], subprocess.CompletedProcess[str]] = _capture


class SshScriptRunner:
    """A ScriptRunner that reaches one host over a non-interactive ssh session."""

    def __init__(
        self, *, binary: str, address: str, identity: Path, admin_user: str
    ) -> None:
        """Store the binary, address, identity, and admin user the runner uses."""
        self.binary = binary
        self.address = address
        self.identity = identity
        self.admin_user = admin_user

    def command(self, step: Step) -> list[str]:
        """Build the argv that runs `step`'s script over ssh."""
        user = self.admin_user if step.login is Login.ADMIN else Login.ROOT.value
        command = [
            self.binary,
            "-i",
            str(self.identity),
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            f"ConnectTimeout={CONNECT_TIMEOUT_SECONDS}",
        ]
        if step.forward_agent:
            command.append("-A")
        command += [f"{user}@{self.address}", "bash", "-s"]
        return command

    def __call__(self, step: Step, script: str) -> RemoteResult:
        """Run `script` on the host as `step` asks, and return what it produced."""
        completed = capture_ssh(self.command(step), script)
        return RemoteResult(
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )


def ssh_runner(settings: Settings, host: Host) -> SshScriptRunner:
    """Build the runner that provisions `host` with `settings`.

    Raises:
        CommandError: No `ssh` binary is on PATH, or `host` has no address.
    """
    binary = shutil.which("ssh")
    if binary is None:
        raise CommandError("no ssh client found on PATH")
    address = host_address(host)
    return SshScriptRunner(
        binary=binary,
        address=address,
        identity=settings.ssh_identity.expanduser(),
        admin_user=settings.admin_user,
    )
