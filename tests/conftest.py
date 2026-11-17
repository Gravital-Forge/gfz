"""Shared fixtures for the gfz test suite."""

import os
from collections.abc import Mapping, Sequence

import pytest

import gfz.commands
import gfz.remote
from gfz.hosts import ActionStatus, Host, PowerAction
from gfz.provision import step_context
from gfz.remote import RemoteResult
from gfz.settings import Settings
from gfz.steps.registry import Login, Step

REQUIRED_ENV = {
    "GFZ_ADMIN_USER": "admin",
    "GFZ_AGENT_USER": "agent",
    "GFZ_GIT_NAME": "Agent Example",
    "GFZ_GIT_EMAIL": "agent@example.com",
    "GFZ_SSH_KEYS": "key-one",
    "GFZ_SSH_IDENTITY": "/home/admin/.ssh/id_ed25519",
    "GFZ_HARNESS_REPO": "git@github.com:example/harness.git",
    "GFZ_GITHUB_APP_ID": "123456",
    "GFZ_GITHUB_APP_INSTALLATION_ID": "7654321",
}

NEW_HOST_ID = 909


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch, tmp_path):
    """Hide the machine's own GFZ_ variables, token, and config file from the tests."""
    for name in list(os.environ):
        if name.startswith("GFZ_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("DIGITALOCEAN_TOKEN", raising=False)

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))


@pytest.fixture(autouse=True)
def _no_real_ssh(monkeypatch):
    """Break the ssh transport, so no test can reach a real host by accident.

    A test that exercises the transport replaces this seam with its own
    double.
    """

    def _refuse(command, script):
        """Fail the test that tried to run ssh, naming the command it built."""
        raise AssertionError(f"the test suite must not run ssh: {command}")

    monkeypatch.setattr(gfz.remote, "capture_ssh", _refuse)


@pytest.fixture
def required_environment(monkeypatch, _isolated_environment):
    """Set every required option by environment variable; optional ones stay unset."""
    for name, value in REQUIRED_ENV.items():
        monkeypatch.setenv(name, value)


@pytest.fixture
def required_environment_missing_admin_user(monkeypatch, _isolated_environment):
    """Set every required option except GFZ_ADMIN_USER, so resolution fails on it."""
    for name, value in REQUIRED_ENV.items():
        if name != "GFZ_ADMIN_USER":
            monkeypatch.setenv(name, value)


@pytest.fixture
def settings(required_environment) -> Settings:
    """Settings resolved from the required environment variables alone."""
    return Settings()


class FakeClock:
    """A fake monotonic clock that only advances when told to sleep."""

    def __init__(self) -> None:
        """Start the clock at zero."""
        self.seconds = 0.0

    def now(self) -> float:
        """Return the current fake time."""
        return self.seconds

    def sleep(self, seconds: float) -> None:
        """Advance the fake time by `seconds`, instead of really sleeping."""
        self.seconds += seconds


@pytest.fixture
def fake_clock(monkeypatch) -> FakeClock:
    """Drive every wait in gfz.commands from a clock the test owns.

    Both seams move together: a wait's sleep advances this clock and costs
    no real time, so a test can burn the whole wait budget in an instant.
    """
    clock = FakeClock()
    monkeypatch.setattr(gfz.commands, "now_seconds", clock.now)
    monkeypatch.setattr(gfz.commands, "sleep_seconds", clock.sleep)
    return clock


HOST_DEFAULTS: dict[str, object] = {
    "id": 101,
    "name": "alpha",
    "status": "active",
    "region": "tor1",
    "size": "s-4vcpu-8gb",
    "ip": "203.0.113.10",
    "created_at": "2026-01-02T03:04:05Z",
    "tags": ("gfz",),
}


def make_host(**overrides: object) -> Host:
    """Build a Host, taking every field a test does not name from HOST_DEFAULTS."""
    return Host(**{**HOST_DEFAULTS, **overrides})


STEP_DEFAULTS: dict[str, object] = {
    "slug": "probe",
    "login": Login.ROOT,
    "forward_agent": False,
    "template": "base_packages.sh.j2",
    "summary": "A step a test invented.",
    "mandatory": True,
}


def make_step(**overrides: object) -> Step:
    """Build a Step, taking every field a test does not name from STEP_DEFAULTS.

    The default template names a real file, so a step a test invents renders
    like any registered one.
    """
    return Step(**{**STEP_DEFAULTS, **overrides})


class FakeHostClient:
    """An in-memory HostClient that records every call a command makes on it.

    `hosts` is the account's droplets. `polls` scripts what successive
    `get_host` calls return, which is how a test drives a wait through a
    droplet's states; its last entry repeats for every further call. With no
    `polls`, `get_host` returns the matching entry of `hosts`.
    `deletion_takes_effect` false leaves a deleted droplet in `hosts`, which
    is how a test drives the destroy wait to its budget.
    """

    def __init__(
        self,
        hosts: Sequence[Host] = (),
        *,
        polls: Sequence[Host] = (),
        power_status: ActionStatus = "in-progress",
        deletion_takes_effect: bool = True,
    ) -> None:
        """Build the fake over the account's hosts and the scripted poll results."""
        self.hosts = list(hosts)
        self.polls = list(polls)
        self.power_status = power_status
        self.deletion_takes_effect = deletion_takes_effect
        self.create_calls: list[dict[str, object]] = []
        self.get_calls: list[int] = []
        self.list_calls: list[str | None] = []
        self.delete_calls: list[int] = []
        self.power_calls: list[tuple[int, PowerAction]] = []

    def create_host(
        self,
        *,
        name: str,
        region: str,
        size: str,
        image: str,
        ssh_keys: Sequence[str],
        tags: Sequence[str],
    ) -> Host:
        """Record the create arguments and return the host as brand new."""
        self.create_calls.append(
            {
                "name": name,
                "region": region,
                "size": size,
                "image": image,
                "ssh_keys": list(ssh_keys),
                "tags": list(tags),
            }
        )
        host = make_host(
            id=NEW_HOST_ID,
            name=name,
            status="new",
            region=region,
            size=size,
            ip=None,
            tags=tuple(tags),
        )
        self.hosts.append(host)
        return host

    def get_host(self, host_id: int) -> Host:
        """Return the next scripted poll result, or the account host with this id."""
        self.get_calls.append(host_id)
        if self.polls:
            return self.polls.pop(0) if len(self.polls) > 1 else self.polls[0]
        return next(host for host in self.hosts if host.id == host_id)

    def list_hosts(self, *, tag: str | None = None) -> list[Host]:
        """Return every account host, or only those carrying `tag`."""
        self.list_calls.append(tag)
        if tag is None:
            return list(self.hosts)
        return [host for host in self.hosts if tag in host.tags]

    def delete_host(self, host_id: int) -> None:
        """Record the deletion, and drop the host unless deletion is scripted inert."""
        self.delete_calls.append(host_id)
        if self.deletion_takes_effect:
            self.hosts = [host for host in self.hosts if host.id != host_id]

    def power_host(self, host_id: int, action: PowerAction) -> ActionStatus:
        """Record the power action and report the configured status."""
        self.power_calls.append((host_id, action))
        return self.power_status

    def list_account_keys(self) -> list[str]:
        """Return the account's key names."""
        return ["key-one"]


REMOTE_FAILURE_STDERR = "E: Unable to locate package nonesuch"


class FakeScriptRunner:
    """A ScriptRunner that records runs instead of opening an ssh connection.

    `failures` maps a step's slug to the exit code that step's run reports;
    every step it does not name reports success. A failing run also reports
    REMOTE_FAILURE_STDERR on stderr, so a test can look for it in output.
    """

    def __init__(self, failures: Mapping[str, int] | None = None) -> None:
        """Build the fake, scripting each named step's exit code."""
        self.failures = dict(failures) if failures else {}
        self.runs: list[tuple[Step, str]] = []

    @property
    def calls(self) -> list[tuple[Login, str]]:
        """The `(login, script)` pair of every run, in run order."""
        return [(step.login, script) for step, script in self.runs]

    @property
    def slugs(self) -> list[str]:
        """The slug of every step that ran, in run order."""
        return [step.slug for step, _ in self.runs]

    def __call__(self, step: Step, script: str) -> RemoteResult:
        """Record the run and report the exit code scripted for this step."""
        self.runs.append((step, script))
        exit_code = self.failures.get(step.slug, 0)
        stderr = REMOTE_FAILURE_STDERR if exit_code != 0 else ""
        return RemoteResult(exit_code=exit_code, stdout="", stderr=stderr)


class RecordingReporter:
    """A `report` callable that keeps every line a provisioning run gives it."""

    def __init__(self) -> None:
        """Start with no lines recorded."""
        self.lines: list[str] = []

    def __call__(self, line: str) -> None:
        """Keep `line`."""
        self.lines.append(line)


@pytest.fixture
def fake_runner() -> FakeScriptRunner:
    """A ScriptRunner double that records every run and fails nothing."""
    return FakeScriptRunner()


@pytest.fixture
def reporter() -> RecordingReporter:
    """Capture the progress lines a provisioning run reports."""
    return RecordingReporter()


@pytest.fixture
def template_context(settings) -> dict[str, object]:
    """The step context with one repository configured.

    The repository makes clone-repos render its cloning branch, so every
    context value that branch reads reaches the script.
    """
    return {
        **step_context(settings),
        "repos": ["git@github.com:example/project.git"],
    }
