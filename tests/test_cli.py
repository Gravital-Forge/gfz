"""Tests for gfz's CLI: argument parsing, precedence, and rendering."""

import re
import shutil

import pytest
from typer.testing import CliRunner

import gfz.cli as cli
import gfz.commands as commands
import gfz.provision
import gfz.remote
from gfz.cli import app
from gfz.digitalocean import DigitalOceanError
from gfz.steps.registry import STEPS
from tests.conftest import (
    NEW_HOST_ID,
    REMOTE_FAILURE_STDERR,
    FakeHostClient,
    FakeScriptRunner,
    make_host,
    make_step,
)

runner = CliRunner()


def _parsed_table(rendered: str) -> dict[str, tuple[str, str]]:
    """Parse a rendered options table into a mapping of option name to (value, source).

    Columns are left-padded to their widest cell and joined by two spaces, so a
    run of two or more spaces always marks a column boundary; splitting each data
    row on that run recovers its cells. An empty value cell is pure padding, so it
    vanishes into the surrounding whitespace and the split yields two cells,
    name and source, rather than three.
    """
    table: dict[str, tuple[str, str]] = {}
    for line in rendered.splitlines()[1:]:
        cells = re.split(r"\s{2,}", line.strip())
        if len(cells) == 2:
            name, source = cells
            value = ""
        else:
            name, value, source = cells
        table[name] = (value, source)
    return table


def test_show_with_required_env_exits_zero_and_lists_every_option(required_environment):
    """With every required option in the environment, show exits 0 and lists all."""
    result = runner.invoke(app, ["config", "show"])

    assert result.exit_code == 0
    table = _parsed_table(result.stdout)
    expected_names = (
        "admin_user",
        "agent_user",
        "ssh_keys",
        "ssh_identity",
        "region",
        "plugins",
    )
    for name in (*expected_names, "DIGITALOCEAN_TOKEN"):
        assert name in table


def test_the_plugins_row_shows_the_default_entry(required_environment):
    """With nothing supplying plugins, the row shows the claude built-in by default."""
    result = runner.invoke(app, ["config", "show"])

    assert result.exit_code == 0
    assert _parsed_table(result.stdout)["plugins"] == ("claude", "default")


def test_the_plugins_row_shows_the_environments_entries(
    required_environment, monkeypatch
):
    """GFZ_PLUGINS entries show in the plugins row with source 'environment'."""
    monkeypatch.setenv("GFZ_PLUGINS", "claude,/opt/hello")

    result = runner.invoke(app, ["config", "show"])

    assert result.exit_code == 0
    assert _parsed_table(result.stdout)["plugins"] == (
        "claude, /opt/hello",
        "environment",
    )


def test_flag_beats_environment_in_the_printed_table(required_environment, monkeypatch):
    """A flag's value and source 'flag' beat the environment's value and source."""
    monkeypatch.setenv("GFZ_REGION", "ams3")

    result = runner.invoke(app, ["--region", "nyc3", "config", "show"])

    assert result.exit_code == 0
    table = _parsed_table(result.stdout)
    assert table["region"] == ("nyc3", "flag")
    assert table["agent_user"] == ("agent", "environment")


@pytest.mark.parametrize(
    "flag",
    [
        "--harness-repo",
        "--repos",
        "--github-app-id",
        "--github-app-installation-id",
        "--git-name",
        "--git-email",
    ],
)
def test_a_removed_option_is_refused_as_unknown(required_environment, flag):
    """The clone, minter, and git identity flags no longer exist."""
    result = runner.invoke(app, [flag, "value", "config", "show"])

    assert result.exit_code == 2
    assert "No such option" in result.stderr


def test_the_clone_command_no_longer_exists(required_environment):
    """`gfz clone` is refused as an unknown command."""
    result = runner.invoke(app, ["clone", "alpha", "git@example.com:org/one.git"])

    assert result.exit_code == 2
    assert "No such command" in result.stderr


def test_unsupplied_option_reports_its_default(required_environment):
    """An option nothing supplies shows its default value and source 'default'."""
    result = runner.invoke(app, ["config", "show"])

    assert result.exit_code == 0
    table = _parsed_table(result.stdout)
    assert table["region"] == ("tor1", "default")


def test_token_row_reports_set_and_never_leaks_the_value(
    required_environment, monkeypatch
):
    """The token row reads 'set' and the token's value appears nowhere in output."""
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", "sekrit-token-value")

    result = runner.invoke(app, ["config", "show"])

    assert result.exit_code == 0
    table = _parsed_table(result.stdout)
    assert table["DIGITALOCEAN_TOKEN"] == ("set", "environment")
    combined_output = result.stdout + result.stderr
    assert "sekrit-token-value" not in combined_output


def test_token_row_reports_not_set_when_absent(required_environment):
    """The token row reads 'not set' when DIGITALOCEAN_TOKEN is absent."""
    result = runner.invoke(app, ["config", "show"])

    assert result.exit_code == 0
    table = _parsed_table(result.stdout)
    assert table["DIGITALOCEAN_TOKEN"] == ("not set", "unset")


def test_missing_required_option_exits_2_with_no_stdout(
    required_environment_missing_admin_user,
):
    """A missing required option exits 2, prints nothing to stdout, and names it."""
    result = runner.invoke(app, ["config", "show"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "--admin-user" in result.stderr
    assert "GFZ_ADMIN_USER" in result.stderr
    assert "admin_user" in result.stderr
    assert "Traceback" not in result.stderr


def test_config_file_option_reports_source_file(
    required_environment, monkeypatch, tmp_path
):
    """A value that only the config file supplies reports source 'file'."""
    config_file = tmp_path / "config.toml"
    config_file.write_text('region = "sfo3"\n', encoding="utf-8")
    monkeypatch.setenv("GFZ_CONFIG", str(config_file))

    result = runner.invoke(app, ["config", "show"])

    assert result.exit_code == 0
    table = _parsed_table(result.stdout)
    assert table["region"] == ("sfo3", "file")


def test_list_renders_seven_columns_and_keeps_a_row_with_no_address(
    required_environment, monkeypatch
):
    """List prints all seven columns, and a host with no address keeps its row."""
    addressed = make_host(id=101, name="alpha", ip="203.0.113.10")
    addressless = make_host(id=202, name="bravo", ip=None)
    fake = FakeHostClient(hosts=[addressed, addressless])
    monkeypatch.setattr(cli, "DigitalOceanClient", lambda: fake)

    result = runner.invoke(app, ["list", "--all"])

    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    assert re.split(r"\s{2,}", lines[0].strip()) == [
        "name",
        "id",
        "status",
        "region",
        "size",
        "address",
        "created",
    ]
    # An empty address cell is pure padding, so splitting on the column gap
    # yields six cells for the addressless row and seven for the addressed one.
    assert re.split(r"\s{2,}", lines[1].strip()) == [
        "alpha",
        "101",
        "active",
        "tor1",
        "s-4vcpu-8gb",
        "203.0.113.10",
        "2026-01-02T03:04:05Z",
    ]
    assert re.split(r"\s{2,}", lines[2].strip()) == [
        "bravo",
        "202",
        "active",
        "tor1",
        "s-4vcpu-8gb",
        "2026-01-02T03:04:05Z",
    ]


def test_list_all_flag_lists_every_host_in_the_account(
    required_environment, monkeypatch
):
    """--all asks the client for every host, regardless of the configured tag."""
    fake = FakeHostClient(hosts=[make_host()])
    monkeypatch.setattr(cli, "DigitalOceanClient", lambda: fake)

    result = runner.invoke(app, ["list", "--all"])

    assert result.exit_code == 0
    assert fake.list_calls == [None]


def test_list_tag_flag_selects_that_tag(required_environment, monkeypatch):
    """The --tag flag on list asks the client for that tag."""
    fake = FakeHostClient(hosts=[make_host(tags=("other",))])
    monkeypatch.setattr(cli, "DigitalOceanClient", lambda: fake)

    result = runner.invoke(app, ["list", "--tag", "other"])

    assert result.exit_code == 0
    assert fake.list_calls == ["other"]


def test_list_stdout_is_only_the_rendered_table(required_environment, monkeypatch):
    """List's stdout is exactly the rendered table, and stderr stays empty."""
    alpha = make_host(id=101, name="alpha")
    bravo = make_host(id=202, name="bravo")
    fake = FakeHostClient(hosts=[alpha, bravo])
    monkeypatch.setattr(cli, "DigitalOceanClient", lambda: fake)

    result = runner.invoke(app, ["list", "--all"])

    assert result.exit_code == 0
    assert result.stderr == ""
    expected = cli._aligned_table(
        cli.LIST_HEADER, [cli._list_row(alpha), cli._list_row(bravo)]
    )
    assert result.stdout == expected + "\n"


def test_create_no_provision_prints_the_reachable_droplet(
    required_environment, monkeypatch, fake_clock
):
    """--no-provision waits for the droplet to become reachable, then stops."""
    monkeypatch.setattr(commands, "probe_tcp_port", lambda address, port: True)
    active = make_host(id=NEW_HOST_ID, name="alpha", status="active", ip="203.0.113.10")
    fake = FakeHostClient(polls=[active])
    monkeypatch.setattr(cli, "DigitalOceanClient", lambda: fake)

    result = runner.invoke(app, ["create", "alpha", "--no-provision"])

    assert result.exit_code == 0
    assert "alpha" in result.stdout
    assert "203.0.113.10" in result.stdout


def test_create_on_wait_expiry_prints_the_id_and_exits_nonzero(
    required_environment, monkeypatch, fake_clock
):
    """Create on wait expiry names the droplet's id, exits non-zero, deletes nothing."""
    monkeypatch.setattr(commands, "probe_tcp_port", lambda address, port: False)
    stuck = make_host(id=NEW_HOST_ID, name="alpha", status="active", ip="203.0.113.10")
    fake = FakeHostClient(polls=[stuck])
    monkeypatch.setattr(cli, "DigitalOceanClient", lambda: fake)

    result = runner.invoke(app, ["create", "alpha"])

    assert result.exit_code != 0
    assert str(NEW_HOST_ID) in result.stderr
    assert fake.delete_calls == []


def test_destroy_prints_details_and_refuses_a_mismatched_typed_name(
    required_environment, monkeypatch
):
    """Destroy prints name/id/region/creation time, then refuses a wrong typed name."""
    host = make_host(
        id=101, name="alpha", region="tor1", created_at="2026-01-02T03:04:05Z"
    )
    fake = FakeHostClient(hosts=[host])
    monkeypatch.setattr(cli, "DigitalOceanClient", lambda: fake)

    result = runner.invoke(app, ["destroy", "alpha"], input="wrong\n")

    assert result.exit_code != 0
    assert "alpha" in result.stdout
    assert "101" in result.stdout
    assert "tor1" in result.stdout
    assert "2026-01-02T03:04:05Z" in result.stdout
    assert fake.delete_calls == []


def test_destroy_typed_name_carries_the_prompt_through_to_the_deletion(
    required_environment, monkeypatch
):
    """The name typed back at the prompt destroys that droplet, and only that one."""
    host = make_host(id=101, name="alpha")
    bystander = make_host(id=202, name="bravo")
    fake = FakeHostClient(hosts=[host, bystander])
    monkeypatch.setattr(cli, "DigitalOceanClient", lambda: fake)

    result = runner.invoke(app, ["destroy", "alpha"], input="alpha\n")

    assert result.exit_code == 0
    assert fake.delete_calls == [101]


def test_destroy_confirms_the_droplet_is_gone(required_environment, monkeypatch):
    """Destroy waits for the droplet to leave the account, then says it is destroyed."""
    host = make_host(id=101, name="alpha")
    fake = FakeHostClient(hosts=[host])
    monkeypatch.setattr(cli, "DigitalOceanClient", lambda: fake)

    result = runner.invoke(app, ["destroy", "alpha", "--yes"])

    assert result.exit_code == 0
    assert "alpha (id 101): destroyed" in result.stdout


def test_destroy_on_wait_expiry_exits_nonzero_and_deletes_nothing_else(
    required_environment, monkeypatch, fake_clock
):
    """A droplet the account keeps listing exits non-zero after one delete call."""
    host = make_host(id=101, name="alpha")
    bystander = make_host(id=202, name="bravo")
    fake = FakeHostClient(hosts=[host, bystander], deletion_takes_effect=False)
    monkeypatch.setattr(cli, "DigitalOceanClient", lambda: fake)

    result = runner.invoke(app, ["destroy", "alpha", "--yes"])

    assert result.exit_code != 0
    assert "101" in result.stderr
    assert "destroyed" not in result.stdout
    assert fake.delete_calls == [101]
    assert bystander in fake.hosts


def test_destroy_yes_skips_the_prompt(required_environment, monkeypatch):
    """--yes destroys without reading a confirmation line."""
    host = make_host(id=101, name="alpha")
    fake = FakeHostClient(hosts=[host])
    monkeypatch.setattr(cli, "DigitalOceanClient", lambda: fake)

    result = runner.invoke(app, ["destroy", "alpha", "--yes"])

    assert result.exit_code == 0
    assert fake.delete_calls == [101]


def test_destroy_on_untagged_droplet_exits_nonzero(required_environment, monkeypatch):
    """Destroy on a droplet missing the configured tag refuses and exits non-zero."""
    host = make_host(id=101, name="alpha", tags=("other",))
    fake = FakeHostClient(hosts=[host])
    monkeypatch.setattr(cli, "DigitalOceanClient", lambda: fake)

    result = runner.invoke(app, ["destroy", "alpha"])

    assert result.exit_code != 0
    assert fake.delete_calls == []


def test_start_waits_for_active_and_prints_that_state(
    required_environment, monkeypatch, fake_clock
):
    """Start powers on, waits for active, and prints that state as the whole output."""
    host = make_host(id=101, name="alpha", status="off")
    running = make_host(id=101, name="alpha", status="active", ip="203.0.113.10")
    fake = FakeHostClient(hosts=[host], polls=[running])
    monkeypatch.setattr(cli, "DigitalOceanClient", lambda: fake)

    result = runner.invoke(app, ["start", "alpha"])

    assert result.exit_code == 0
    assert fake.power_calls == [(101, "power_on")]
    expected = "alpha (id 101): status active, address 203.0.113.10"
    assert result.stdout.strip() == expected


@pytest.mark.parametrize(
    ("flags", "action"),
    [([], "shutdown"), (["--hard"], "power_off")],
)
def test_stop_waits_for_off_and_prints_that_state(
    required_environment, monkeypatch, fake_clock, flags, action
):
    """Stop sends its action, waits for off, and prints that state alone."""
    host = make_host(id=101, name="alpha", status="active")
    stopped = make_host(id=101, name="alpha", status="off", ip="203.0.113.10")
    fake = FakeHostClient(hosts=[host], polls=[stopped])
    monkeypatch.setattr(cli, "DigitalOceanClient", lambda: fake)

    result = runner.invoke(app, ["stop", "alpha", *flags])

    assert result.exit_code == 0
    assert fake.power_calls == [(101, action)]
    assert result.stdout.strip() == "alpha (id 101): status off, address 203.0.113.10"


@pytest.mark.parametrize(
    ("command", "stuck_status"),
    [("start", "off"), ("stop", "active")],
)
def test_a_power_wait_that_expires_prints_the_last_status_and_exits_nonzero(
    required_environment, monkeypatch, fake_clock, command, stuck_status
):
    """A droplet that never settles exits non-zero, naming the status it reports."""
    stuck = make_host(id=101, name="alpha", status=stuck_status)
    fake = FakeHostClient(hosts=[stuck], polls=[stuck])
    monkeypatch.setattr(cli, "DigitalOceanClient", lambda: fake)

    result = runner.invoke(app, [command, "alpha"])

    assert result.exit_code != 0
    assert "101" in result.stderr
    assert stuck_status in result.stderr
    assert result.stdout == ""
    assert fake.delete_calls == []


def test_start_exits_nonzero_when_action_errors(required_environment, monkeypatch):
    """Start exits non-zero when the provider reports the action errored."""
    host = make_host(id=101, name="alpha", status="off")
    fake = FakeHostClient(hosts=[host], power_status="errored")
    monkeypatch.setattr(cli, "DigitalOceanClient", lambda: fake)

    result = runner.invoke(app, ["start", "alpha"])

    assert result.exit_code != 0
    assert "errored" in result.stderr
    assert fake.get_calls == []


def test_stop_exits_nonzero_when_action_errors(required_environment, monkeypatch):
    """Stop exits non-zero when the provider reports the action errored."""
    host = make_host(id=101, name="alpha", status="active")
    fake = FakeHostClient(hosts=[host], power_status="errored")
    monkeypatch.setattr(cli, "DigitalOceanClient", lambda: fake)

    result = runner.invoke(app, ["stop", "alpha"])

    assert result.exit_code != 0
    assert "errored" in result.stderr
    assert fake.get_calls == []


def test_ssh_passes_the_built_argv_to_launch_ssh_and_exits_with_its_code(
    required_environment, monkeypatch
):
    """Ssh launches the built argv through launch_ssh and exits with its code."""
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/ssh")
    host = make_host(id=101, name="alpha", ip="203.0.113.10")
    fake = FakeHostClient(hosts=[host])
    monkeypatch.setattr(cli, "DigitalOceanClient", lambda: fake)
    recorded_commands = []

    def _fake_launch(command):
        """Record the launched command and report a distinctive exit code."""
        recorded_commands.append(command)
        return 7

    monkeypatch.setattr(commands, "launch_ssh", _fake_launch)

    result = runner.invoke(app, ["ssh", "alpha"])

    assert result.exit_code == 7
    assert len(recorded_commands) == 1
    command = recorded_commands[0]
    assert command[0] == "/usr/bin/ssh"
    assert command[1] == "-i"
    assert command[-1].endswith("@203.0.113.10")


def test_digitalocean_error_prints_its_message_and_exits_nonzero_without_traceback(
    required_environment, monkeypatch
):
    """A DigitalOceanError prints to stderr, exits non-zero, and prints no traceback."""

    class _FailingClient:
        """A stand-in whose construction always raises DigitalOceanError."""

        def __init__(self):
            """Raise, as the real client does when the API token is unset."""
            raise DigitalOceanError("DIGITALOCEAN_TOKEN is not set")

    monkeypatch.setattr(cli, "DigitalOceanClient", _FailingClient)

    result = runner.invoke(app, ["list"])

    assert result.exit_code != 0
    assert "DIGITALOCEAN_TOKEN is not set" in result.stderr
    assert "Traceback" not in result.stderr
    assert result.stdout == ""


def test_steps_prints_a_header_and_one_row_per_registered_step(required_environment):
    """`steps` exits 0 and prints a header row plus one row per registered step."""
    result = runner.invoke(app, ["steps"])

    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    assert re.split(r"\s{2,}", lines[0].strip()) == [
        "step",
        "login",
        "policy",
        "summary",
    ]
    assert len(lines) == 1 + len(STEPS)
    for step, line in zip(STEPS, lines[1:], strict=True):
        assert step.slug in line
        assert step.login.value in line
        assert step.summary in line


PROVISIONED_LINE = "droplet alpha (203.0.113.10) provisioned with agent user agent"
CREATED_LINE = "droplet alpha (203.0.113.10) created with agent user agent"


def test_provision_dry_run_prints_every_step_script_and_no_closing_line(
    required_environment, monkeypatch
):
    """--dry-run prints every step's script, opens no ssh, and prints no closing line.

    A dry run provisions nothing, so no line may say the droplet was provisioned.
    """
    host = make_host()
    fake = FakeHostClient(hosts=[host])
    monkeypatch.setattr(cli, "DigitalOceanClient", lambda: fake)

    def _unreachable(settings, host):
        """Fail loudly if provision tries to open an ssh connection."""
        raise AssertionError("dry-run must not open an ssh connection")

    monkeypatch.setattr(gfz.remote, "ssh_runner", _unreachable)

    result = runner.invoke(app, ["provision", "alpha", "--dry-run"])

    assert result.exit_code == 0
    assert result.stdout.count("set -euo pipefail") == len(STEPS)
    for step in STEPS:
        assert f"# {step.slug} ({step.login.value})" in result.stdout
    assert "provisioned with agent user" not in result.stdout


def test_provision_dry_run_against_a_host_with_no_address_exits_1(
    settings, monkeypatch
):
    """--dry-run against a host with no address exits 1, refusing before any render."""
    host = make_host(ip=None)
    fake = FakeHostClient(hosts=[host])
    monkeypatch.setattr(cli, "DigitalOceanClient", lambda: fake)

    result = runner.invoke(app, ["provision", "alpha", "--dry-run"])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "no address" in result.stderr
    assert host.name in result.stderr


def test_provision_runs_every_registered_step_and_prints_the_summary(
    required_environment, monkeypatch
):
    """Provision with no failures exits 0 and reports every step succeeded."""
    fake = FakeHostClient(hosts=[make_host()])
    monkeypatch.setattr(cli, "DigitalOceanClient", lambda: fake)
    fake_runner = FakeScriptRunner()
    monkeypatch.setattr(gfz.remote, "ssh_runner", lambda settings, host: fake_runner)

    result = runner.invoke(app, ["provision", "alpha"])

    assert result.exit_code == 0
    assert f"{len(STEPS)} steps ran, all succeeded" in result.stdout
    assert fake_runner.slugs == [step.slug for step in STEPS]


def test_provision_ends_with_the_summary_then_the_closing_line(
    required_environment, monkeypatch
):
    """A clean run's stdout is the summary, then the provisioned line, and no more."""
    fake = FakeHostClient(hosts=[make_host()])
    monkeypatch.setattr(cli, "DigitalOceanClient", lambda: fake)
    fake_runner = FakeScriptRunner()
    monkeypatch.setattr(gfz.remote, "ssh_runner", lambda settings, host: fake_runner)

    result = runner.invoke(app, ["provision", "alpha"])

    assert result.exit_code == 0
    assert result.stdout.splitlines() == [
        f"{len(STEPS)} steps ran, all succeeded",
        PROVISIONED_LINE,
    ]


def test_provision_with_an_optional_step_failure_still_prints_the_closing_line(
    required_environment, monkeypatch
):
    """An optional step's failure exits 0, and the closing line ends stdout."""
    fake = FakeHostClient(hosts=[make_host()])
    monkeypatch.setattr(cli, "DigitalOceanClient", lambda: fake)
    fake_runner = FakeScriptRunner(failures={"admin-tools": 5})
    monkeypatch.setattr(gfz.remote, "ssh_runner", lambda settings, host: fake_runner)

    result = runner.invoke(app, ["provision", "alpha"])

    assert result.exit_code == 0
    assert result.stdout.splitlines()[-1] == PROVISIONED_LINE


def test_provision_stopped_by_a_mandatory_failure_prints_no_closing_line(
    required_environment, monkeypatch
):
    """A mandatory step's failure exits 1 and claims nothing was provisioned."""
    fake = FakeHostClient(hosts=[make_host()])
    monkeypatch.setattr(cli, "DigitalOceanClient", lambda: fake)
    failing_slug = STEPS[0].slug
    fake_runner = FakeScriptRunner(failures={failing_slug: 3})
    monkeypatch.setattr(gfz.remote, "ssh_runner", lambda settings, host: fake_runner)

    result = runner.invoke(app, ["provision", "alpha"])

    assert result.exit_code == 1
    combined_output = result.stdout + result.stderr
    assert "provisioned with agent user" not in combined_output


def test_provision_stops_on_a_mandatory_step_failure(required_environment, monkeypatch):
    """A mandatory step's failure exits 1, names the slug, and stops the run."""
    fake = FakeHostClient(hosts=[make_host()])
    monkeypatch.setattr(cli, "DigitalOceanClient", lambda: fake)
    failing_slug = STEPS[0].slug
    fake_runner = FakeScriptRunner(failures={failing_slug: 3})
    monkeypatch.setattr(gfz.remote, "ssh_runner", lambda settings, host: fake_runner)

    result = runner.invoke(app, ["provision", "alpha"])

    assert result.exit_code == 1
    assert failing_slug in result.stderr
    assert REMOTE_FAILURE_STDERR in result.stderr
    assert fake_runner.slugs == [failing_slug]


def test_provision_with_an_optional_step_failure_still_exits_zero(
    required_environment, monkeypatch
):
    """An optional step's failure exits 0 and the summary names it."""
    steps = (
        make_step(slug="base-packages"),
        make_step(
            slug="optional-thing",
            summary="An optional step for the test.",
            mandatory=False,
        ),
    )
    monkeypatch.setattr(gfz.provision, "STEPS", steps)
    fake = FakeHostClient(hosts=[make_host()])
    monkeypatch.setattr(cli, "DigitalOceanClient", lambda: fake)
    fake_runner = FakeScriptRunner(failures={"optional-thing": 5})
    monkeypatch.setattr(gfz.remote, "ssh_runner", lambda settings, host: fake_runner)

    result = runner.invoke(app, ["provision", "alpha"])

    assert result.exit_code == 0
    assert "optional-thing" in result.stdout
    assert "exit 5" in result.stdout
    assert fake_runner.slugs == ["base-packages", "optional-thing"]


def test_provision_with_a_registered_optional_step_failure_exits_zero(
    required_environment, monkeypatch
):
    """A registered optional step failing exits 0, and the summary names its slug."""
    fake = FakeHostClient(hosts=[make_host()])
    monkeypatch.setattr(cli, "DigitalOceanClient", lambda: fake)
    fake_runner = FakeScriptRunner(failures={"admin-tools": 5})
    monkeypatch.setattr(gfz.remote, "ssh_runner", lambda settings, host: fake_runner)

    result = runner.invoke(app, ["provision", "alpha"])

    assert result.exit_code == 0
    assert "admin-tools: exit 5" in result.stdout
    assert fake_runner.slugs == [step.slug for step in STEPS]


def test_provision_only_unknown_step_exits_2_and_names_valid_slugs(
    required_environment,
):
    """--only naming an unknown step exits 2 and the message names every valid slug."""
    result = runner.invoke(app, ["provision", "alpha", "--only", "nonesuch"])

    assert result.exit_code == 2
    for step in STEPS:
        assert step.slug in result.stderr


def test_provision_only_and_from_together_exits_2(required_environment):
    """--only and --from together exit 2."""
    result = runner.invoke(
        app,
        ["provision", "alpha", "--only", "base-packages", "--from", "docker-repo"],
    )

    assert result.exit_code == 2


def test_provision_only_combined_with_skip_runs_the_narrowed_selection(
    required_environment, monkeypatch
):
    """--skip narrows what --only selected, leaving the steps in neither flag out."""
    fake = FakeHostClient(hosts=[make_host()])
    monkeypatch.setattr(cli, "DigitalOceanClient", lambda: fake)
    fake_runner = FakeScriptRunner()
    monkeypatch.setattr(gfz.remote, "ssh_runner", lambda settings, host: fake_runner)

    result = runner.invoke(
        app,
        ["provision", "alpha", "--only", "base-packages", "--skip", "docker-repo"],
    )

    assert result.exit_code == 0
    assert fake_runner.slugs == ["base-packages"]


def test_provision_from_runs_that_step_and_every_step_after_it(
    required_environment, monkeypatch
):
    """--from starts at the named step and runs every step after it."""
    fake = FakeHostClient(hosts=[make_host()])
    monkeypatch.setattr(cli, "DigitalOceanClient", lambda: fake)
    fake_runner = FakeScriptRunner()
    monkeypatch.setattr(gfz.remote, "ssh_runner", lambda settings, host: fake_runner)

    result = runner.invoke(app, ["provision", "alpha", "--from", "docker-repo"])

    assert result.exit_code == 0
    assert fake_runner.slugs == [step.slug for step in STEPS[1:]]


def test_provision_progress_lines_go_to_stderr_not_stdout(
    required_environment, monkeypatch
):
    """A step's running line and result line land on stderr, not stdout."""
    fake = FakeHostClient(hosts=[make_host()])
    monkeypatch.setattr(cli, "DigitalOceanClient", lambda: fake)
    fake_runner = FakeScriptRunner()
    monkeypatch.setattr(gfz.remote, "ssh_runner", lambda settings, host: fake_runner)

    result = runner.invoke(app, ["provision", "alpha"])

    assert result.exit_code == 0
    first_slug = STEPS[0].slug
    assert f"{first_slug}: running" in result.stderr
    assert f"{first_slug}: ok" in result.stderr
    assert f"{first_slug}: running" not in result.stdout
    assert f"{first_slug}: ok" not in result.stdout


def test_create_provisions_after_the_wait_by_default(
    required_environment, monkeypatch, fake_clock
):
    """Create without --no-provision provisions every step after the wait."""
    monkeypatch.setattr(commands, "probe_tcp_port", lambda address, port: True)
    active = make_host(id=NEW_HOST_ID, name="alpha", status="active", ip="203.0.113.10")
    fake = FakeHostClient(polls=[active])
    monkeypatch.setattr(cli, "DigitalOceanClient", lambda: fake)
    fake_runner = FakeScriptRunner()
    monkeypatch.setattr(gfz.remote, "ssh_runner", lambda settings, host: fake_runner)

    result = runner.invoke(app, ["create", "alpha"])

    assert result.exit_code == 0
    assert fake_runner.slugs == [step.slug for step in STEPS]


def test_create_progress_lines_go_to_stderr_not_stdout(
    required_environment, monkeypatch, fake_clock
):
    """Create's wait and provisioning progress land on stderr, not stdout."""
    monkeypatch.setattr(commands, "probe_tcp_port", lambda address, port: True)
    active = make_host(id=NEW_HOST_ID, name="alpha", status="active", ip="203.0.113.10")
    fake = FakeHostClient(polls=[active])
    monkeypatch.setattr(cli, "DigitalOceanClient", lambda: fake)
    fake_runner = FakeScriptRunner()
    monkeypatch.setattr(gfz.remote, "ssh_runner", lambda settings, host: fake_runner)

    result = runner.invoke(app, ["create", "alpha"])

    assert result.exit_code == 0
    assert "waiting for alpha to become active" in result.stderr
    assert "ssh answers on 203.0.113.10" in result.stderr
    assert "waiting for alpha to become active" not in result.stdout
    assert "ssh answers on 203.0.113.10" not in result.stdout
    assert "alpha" in result.stdout
    assert "203.0.113.10" in result.stdout
    assert f"{len(STEPS)} steps ran, all succeeded" in result.stdout


def test_create_ends_with_the_summary_then_the_closing_line(
    required_environment, monkeypatch, fake_clock
):
    """Create that provisions ends stdout with the summary, then the created line."""
    monkeypatch.setattr(commands, "probe_tcp_port", lambda address, port: True)
    active = make_host(id=NEW_HOST_ID, name="alpha", status="active", ip="203.0.113.10")
    fake = FakeHostClient(polls=[active])
    monkeypatch.setattr(cli, "DigitalOceanClient", lambda: fake)
    fake_runner = FakeScriptRunner()
    monkeypatch.setattr(gfz.remote, "ssh_runner", lambda settings, host: fake_runner)

    result = runner.invoke(app, ["create", "alpha"])

    assert result.exit_code == 0
    assert result.stdout.splitlines()[-2:] == [
        f"{len(STEPS)} steps ran, all succeeded",
        CREATED_LINE,
    ]


def test_create_no_provision_prints_the_host_line_and_no_closing_line(
    required_environment, monkeypatch, fake_clock
):
    """--no-provision prints the host line alone, and no closing line."""
    monkeypatch.setattr(commands, "probe_tcp_port", lambda address, port: True)
    active = make_host(id=NEW_HOST_ID, name="alpha", status="active", ip="203.0.113.10")
    fake = FakeHostClient(polls=[active])
    monkeypatch.setattr(cli, "DigitalOceanClient", lambda: fake)

    result = runner.invoke(app, ["create", "alpha", "--no-provision"])

    assert result.exit_code == 0
    assert result.stdout.splitlines() == [
        f"alpha (id {NEW_HOST_ID}): status active, address 203.0.113.10"
    ]


def test_create_no_provision_runs_no_step(
    required_environment, monkeypatch, fake_clock
):
    """--no-provision leaves the fake runner untouched."""
    monkeypatch.setattr(commands, "probe_tcp_port", lambda address, port: True)
    active = make_host(id=NEW_HOST_ID, name="alpha", status="active", ip="203.0.113.10")
    fake = FakeHostClient(polls=[active])
    monkeypatch.setattr(cli, "DigitalOceanClient", lambda: fake)
    fake_runner = FakeScriptRunner()
    monkeypatch.setattr(gfz.remote, "ssh_runner", lambda settings, host: fake_runner)

    result = runner.invoke(app, ["create", "alpha", "--no-provision"])

    assert result.exit_code == 0
    assert fake_runner.runs == []
