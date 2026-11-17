"""Tests for gfz's ssh transport: argv construction and script capture."""

import shutil
import subprocess

import pytest

import gfz.remote
from gfz.commands import CommandError
from gfz.remote import CONNECT_TIMEOUT_SECONDS, RemoteResult, ssh_runner
from gfz.steps.registry import Login
from tests.conftest import make_host, make_step


def _runner(settings, monkeypatch, ip: str = "203.0.113.10"):
    """Build an SshScriptRunner for a reachable host, with ssh resolved on PATH."""
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/ssh")
    host = make_host(ip=ip)
    return ssh_runner(settings, host)


def test_command_carries_the_non_interactive_options_and_ends_with_bash_dash_s(
    settings, monkeypatch
):
    """The built argv carries the non-interactive options and ends bash -s."""
    runner = _runner(settings, monkeypatch)

    command = runner.command(make_step())

    assert command[3:9] == [
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"ConnectTimeout={CONNECT_TIMEOUT_SECONDS}",
    ]
    assert command[0] == runner.binary
    assert command[-2:] == ["bash", "-s"]


def test_command_carries_the_identity_right_after_the_binary(settings, monkeypatch):
    """`-i <identity>` follows the binary, with the identity expanded."""
    runner = _runner(settings, monkeypatch)

    command = runner.command(make_step())

    assert command[1] == "-i"
    assert command[2] == str(settings.ssh_identity.expanduser())


def test_command_omits_forward_agent_flag_when_the_step_does_not_forward(
    settings, monkeypatch
):
    """A step with forward_agent=False produces an argv with no -A."""
    runner = _runner(settings, monkeypatch)

    command = runner.command(make_step(forward_agent=False))

    assert "-A" not in command


def test_command_places_forward_agent_flag_before_the_user_at_address(
    settings, monkeypatch
):
    """A step with forward_agent=True produces an argv with -A before user@address."""
    runner = _runner(settings, monkeypatch)

    command = runner.command(make_step(forward_agent=True, login=Login.ADMIN))

    destination = f"{settings.admin_user}@{runner.address}"
    assert "-A" in command
    assert command.index("-A") < command.index(destination)


def test_command_root_login_uses_the_root_account(settings, monkeypatch):
    """Login.ROOT produces root@<address>."""
    runner = _runner(settings, monkeypatch)

    command = runner.command(make_step(login=Login.ROOT))

    assert f"root@{runner.address}" in command


def test_command_admin_login_uses_the_settings_admin_user(settings, monkeypatch):
    """Login.ADMIN produces <settings.admin_user>@<address>."""
    runner = _runner(settings, monkeypatch)

    command = runner.command(make_step(login=Login.ADMIN))

    assert f"{settings.admin_user}@{runner.address}" in command


def test_call_feeds_the_script_to_the_seam_and_returns_what_it_reported(
    settings, monkeypatch
):
    """__call__ runs the script through capture_ssh and returns its outcome."""
    runner = _runner(settings, monkeypatch)
    recorded: list[tuple[list[str], str]] = []

    def fake(command, script):
        """Record the argv and script, and report a distinctive outcome."""
        recorded.append((command, script))
        return subprocess.CompletedProcess(
            args=command,
            returncode=17,
            stdout="installed\n",
            stderr="a warning\n",
        )

    monkeypatch.setattr(gfz.remote, "capture_ssh", fake)

    result = runner(make_step(), "echo hi")

    assert result == RemoteResult(
        exit_code=17, stdout="installed\n", stderr="a warning\n"
    )
    assert recorded == [(runner.command(make_step()), "echo hi")]


def test_ssh_runner_raises_when_no_ssh_binary_on_path(settings, monkeypatch):
    """ssh_runner raises CommandError when no ssh binary is on PATH."""
    monkeypatch.setattr(shutil, "which", lambda name: None)
    host = make_host(ip="203.0.113.10")

    with pytest.raises(CommandError):
        ssh_runner(settings, host)


def test_ssh_runner_raises_naming_the_host_when_it_has_no_address(
    settings, monkeypatch
):
    """ssh_runner raises CommandError naming the host when it has no address."""
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/ssh")
    host = make_host(id=101, name="alpha", ip=None)

    with pytest.raises(CommandError) as excinfo:
        ssh_runner(settings, host)

    message = str(excinfo.value)
    assert "alpha" in message
    assert "101" in message
