"""Tests for gfz's CLI: argument parsing, precedence, and rendering."""

import re
import shutil
from typing import NoReturn

import pytest
import typer
from typer.testing import CliRunner

import gfz.cli as cli
import gfz.commands as commands
import gfz.provision
import gfz.remote
from gfz.cli import app
from gfz.digitalocean import DigitalOceanError
from gfz.plugin import BUILTIN_DIR, load_plugins
from gfz.provision import run_order
from gfz.steps.registry import STEPS
from tests.conftest import (
    EXAMPLE_DIR,
    NEW_HOST_ID,
    REMOTE_FAILURE_STDERR,
    FakeHostClient,
    FakeScriptRunner,
    make_host,
    make_step,
)

runner = CliRunner()

# With nothing setting `plugins`, a run is core then the claude built-in.
DEFAULT_RUN = run_order(load_plugins(["claude"]))
DEFAULT_NAMES = [step.name for step in DEFAULT_RUN]
CORE_NAMES = [step.slug for step in STEPS]
CLAUDE_NAMES = ["claude/agent", "claude/admin"]
HELLO_NAMES = ["hello/greet"]
BOTH_PLUGINS = ["--plugin", "claude", "--plugin", str(EXAMPLE_DIR)]

REQUIRED_SETTING_MANIFEST = """\
name = "needy"
summary = "A plugin with a required setting."

[settings.token_name]
type = "string"
"""


def _needy_plugin(tmp_path):
    """Write a plugin whose one setting is required and has no default."""
    directory = tmp_path / "needy"
    directory.mkdir()
    (directory / "plugin.toml").write_text(REQUIRED_SETTING_MANIFEST, encoding="utf-8")
    return directory


def _plugin_with_script(tmp_path, script):
    """Write a plugin named `broken` whose one step, `run`, is `script`."""
    directory = tmp_path / "broken"
    directory.mkdir()
    (directory / "plugin.toml").write_text(
        'name = "broken"\nsummary = "A plugin a test invented."\n\n'
        '[[steps]]\nslug = "run"\nscript = "run.sh.j2"\nsummary = "The one step."\n',
        encoding="utf-8",
    )
    (directory / "run.sh.j2").write_text(script, encoding="utf-8")
    return directory


def _plugin_with_no_steps(tmp_path):
    """Write a plugin named `empty` that declares no step."""
    directory = tmp_path / "empty"
    directory.mkdir()
    (directory / "plugin.toml").write_text(
        'name = "empty"\nsummary = "A plugin with no steps."\n', encoding="utf-8"
    )
    return directory


UNRENDERABLE_SCRIPTS = pytest.mark.parametrize(
    ("script", "reason"),
    [
        ("set -euo pipefail\necho {{ plugin.x }\n", "line 2: unexpected '}'"),
        (
            "set -euo pipefail\necho {{ plugin.undeclared }}\n",
            "has no attribute 'undeclared'",
        ),
    ],
    ids=["syntax-error", "undeclared-setting"],
)


def _unreachable_client() -> NoReturn:
    """Fail the test: a command built a client, so it could reach DigitalOcean."""
    pytest.fail("DigitalOcean reached")


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


def test_the_plugins_row_shows_the_default_built_ins_directory(required_environment):
    """With nothing supplying plugins, the row shows the claude built-in's directory."""
    result = runner.invoke(app, ["config", "show"])

    assert result.exit_code == 0
    assert _parsed_table(result.stdout)["plugins"] == (
        str(BUILTIN_DIR / "claude"),
        "default",
    )


def test_the_plugins_row_shows_each_entrys_resolved_directory(
    required_environment, monkeypatch
):
    """GFZ_PLUGINS entries show as their directories, with source 'environment'."""
    monkeypatch.setenv("GFZ_PLUGINS", f"claude,{EXAMPLE_DIR}")

    result = runner.invoke(app, ["config", "show"])

    assert result.exit_code == 0
    assert _parsed_table(result.stdout)["plugins"] == (
        f"{BUILTIN_DIR / 'claude'}, {EXAMPLE_DIR}",
        "environment",
    )


def test_a_tilde_plugin_entry_shows_as_its_expanded_directory(
    required_environment, monkeypatch, tmp_path
):
    """An entry starting with `~` shows as the absolute directory it expands to."""
    shutil.copytree(EXAMPLE_DIR, tmp_path / "home" / "hello")
    monkeypatch.setenv("GFZ_PLUGINS", "~/hello")

    result = runner.invoke(app, ["config", "show"])

    assert result.exit_code == 0
    assert _parsed_table(result.stdout)["plugins"] == (
        str(tmp_path / "home" / "hello"),
        "environment",
    )


def test_the_plugin_flag_repeats_and_reports_source_flag(required_environment):
    """`--plugin` given twice lists both plugins, with source 'flag'."""
    result = runner.invoke(app, [*BOTH_PLUGINS, "config", "show"])

    assert result.exit_code == 0
    assert _parsed_table(result.stdout)["plugins"] == (
        f"{BUILTIN_DIR / 'claude'}, {EXAMPLE_DIR}",
        "flag",
    )


HELLO_TABLE = '[plugin.hello]\ngreeting = "from the file"\n'


@pytest.mark.parametrize(
    ("variable", "file_text", "expected"),
    [
        (None, "", ("hello", "default")),
        (None, HELLO_TABLE, ("from the file", "file")),
        ("from env", HELLO_TABLE, ("from env", "environment")),
    ],
    ids=["default", "file", "environment"],
)
def test_a_plugin_setting_row_names_its_value_and_source(
    required_environment, monkeypatch, tmp_path, variable, file_text, expected
):
    """Each plugin setting shows as `<plugin>.<setting>`, with its value and source."""
    config_file = tmp_path / "config.toml"
    config_file.write_text(file_text, encoding="utf-8")
    monkeypatch.setenv("GFZ_CONFIG", str(config_file))
    if variable is not None:
        monkeypatch.setenv("GFZ_PLUGIN_HELLO_GREETING", variable)

    result = runner.invoke(app, [*BOTH_PLUGINS, "config", "show"])

    assert result.exit_code == 0
    assert _parsed_table(result.stdout)["hello.greeting"] == expected


def test_the_plugin_setting_rows_follow_the_plugins_row(required_environment):
    """Plugin setting rows come straight after the plugins row."""
    result = runner.invoke(app, [*BOTH_PLUGINS, "config", "show"])

    names = list(_parsed_table(result.stdout))
    assert names[names.index("plugins") + 1] == "hello.greeting"


def test_a_table_for_a_plugin_not_listed_is_ignored(
    required_environment, monkeypatch, tmp_path
):
    """A [plugin.<name>] table for an unlisted plugin is neither read nor shown."""
    config_file = tmp_path / "config.toml"
    config_file.write_text("[plugin.absent]\nanything = 1\n", encoding="utf-8")
    monkeypatch.setenv("GFZ_CONFIG", str(config_file))

    result = runner.invoke(app, ["config", "show"])

    assert result.exit_code == 0
    assert not any(name.startswith("absent.") for name in _parsed_table(result.stdout))


UNUSABLE_PLUGINS = pytest.mark.parametrize(
    ("make_entry", "message"),
    [
        (
            lambda tmp_path: str(tmp_path / "nowhere"),
            "is not a directory holding plugin.toml",
        ),
        (
            lambda tmp_path: str(_needy_plugin(tmp_path)),
            "GFZ_PLUGIN_NEEDY_TOKEN_NAME",
        ),
    ],
    ids=["missing-directory", "required-setting"],
)


@UNUSABLE_PLUGINS
def test_config_show_refuses_a_plugin_it_cannot_use(
    required_environment, monkeypatch, tmp_path, make_entry, message
):
    """A plugin that fails to load or resolve exits 1, naming what failed."""
    monkeypatch.setenv("GFZ_PLUGINS", make_entry(tmp_path))

    result = runner.invoke(app, ["config", "show"])

    assert result.exit_code == 1
    assert message in result.stderr
    assert result.stdout == ""
    assert "Traceback" not in result.stderr


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


def test_steps_prints_every_step_in_run_order_with_its_source():
    """`steps` lists core, claude, and hello steps, each naming its source.

    It runs with no required option supplied, as it needs none.
    """
    result = runner.invoke(app, [*BOTH_PLUGINS, "steps"])

    assert result.exit_code == 0
    rows = [re.split(r"\s{2,}", line.strip()) for line in result.stdout.splitlines()]
    assert rows[0] == ["step", "source", "login", "policy", "summary"]
    run = run_order(load_plugins(["claude", str(EXAMPLE_DIR)]))
    assert [row[0] for row in rows[1:]] == [*CORE_NAMES, *CLAUDE_NAMES, *HELLO_NAMES]
    for step, row in zip(run, rows[1:], strict=True):
        policy = "mandatory" if step.mandatory else "optional"
        source = step.plugin or "core"
        assert row == [step.name, source, step.login.value, policy, step.summary]


def test_steps_with_no_plugins_lists_core_alone(monkeypatch):
    """`plugins = []` lists only the core steps."""
    monkeypatch.setenv("GFZ_PLUGINS", "")

    result = runner.invoke(app, ["steps"])

    assert result.exit_code == 0
    assert [line.split()[0] for line in result.stdout.splitlines()[1:]] == CORE_NAMES


def test_steps_resolves_no_plugin_setting(tmp_path):
    """`steps` loads a plugin with a required setting, and asks for no value."""
    result = runner.invoke(app, ["--plugin", str(_needy_plugin(tmp_path)), "steps"])

    assert result.exit_code == 0
    assert "GFZ_PLUGIN_NEEDY" not in result.stderr


def test_steps_refuses_a_plugin_that_fails_to_load():
    """A plugin entry that does not load exits 1 and prints no table."""
    result = runner.invoke(app, ["--plugin", "nonesuch", "steps"])

    assert result.exit_code == 1
    assert "unknown built-in plugin 'nonesuch'" in result.stderr
    assert result.stdout == ""


def test_plugins_lists_each_built_in_with_its_summary():
    """`plugins` prints a name and summary row per built-in, needing no option."""
    result = runner.invoke(app, ["plugins"])

    assert result.exit_code == 0
    rows = [re.split(r"\s{2,}", line.strip()) for line in result.stdout.splitlines()]
    assert rows == [
        ["name", "summary"],
        ["claude", "Install Claude Code for the agent and the admin."],
    ]


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
    assert result.stdout.count("set -euo pipefail") == len(DEFAULT_RUN)
    for step in DEFAULT_RUN:
        assert f"# {step.name} ({step.login.value})" in result.stdout
    assert "provisioned with agent user" not in result.stdout


def test_provision_dry_run_renders_a_plugin_step_with_its_setting(
    required_environment, monkeypatch
):
    """--dry-run prints the hello step under its name, with the greeting setting."""
    fake = FakeHostClient(hosts=[make_host()])
    monkeypatch.setattr(cli, "DigitalOceanClient", lambda: fake)
    monkeypatch.setenv("GFZ_PLUGIN_HELLO_GREETING", "good day")

    result = runner.invoke(
        app,
        [*BOTH_PLUGINS, "provision", "alpha", "--dry-run", "--only", "hello"],
    )

    assert result.exit_code == 0
    assert result.stdout.startswith("# hello/greet (root)\n")
    assert "'good day'" in result.stdout


def test_provision_dry_run_against_a_host_with_no_address_exits_1(
    settings, monkeypatch
):
    """--dry-run against a host with no address exits 1 and prints no script."""
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
    assert f"{len(DEFAULT_RUN)} steps ran, all succeeded" in result.stdout
    assert fake_runner.names == DEFAULT_NAMES


@pytest.mark.parametrize(
    ("selection", "expected"),
    [
        (["--only", "claude"], CLAUDE_NAMES),
        (["--only", "claude/agent"], ["claude/agent"]),
        (["--skip", "hello"], [*CORE_NAMES, *CLAUDE_NAMES]),
        (["--from", "hello"], HELLO_NAMES),
    ],
    ids=["only-plugin", "only-plugin-step", "skip-plugin", "from-plugin"],
)
def test_provision_selects_plugin_steps_by_name_and_by_plugin(
    required_environment, monkeypatch, selection, expected
):
    """--only, --skip, and --from take a plugin step's name or a bare plugin name."""
    fake = FakeHostClient(hosts=[make_host()])
    monkeypatch.setattr(cli, "DigitalOceanClient", lambda: fake)
    fake_runner = FakeScriptRunner()
    monkeypatch.setattr(gfz.remote, "ssh_runner", lambda settings, host: fake_runner)

    result = runner.invoke(app, [*BOTH_PLUGINS, "provision", "alpha", *selection])

    assert result.exit_code == 0
    assert fake_runner.names == expected


@pytest.mark.parametrize("flag", ["--only", "--from", "--skip"])
def test_provision_selection_help_names_a_step_or_a_plugin(flag):
    """Each selection flag's help says it takes a step name or a plugin name."""
    provision = typer.main.get_command(app).commands["provision"]
    option = next(param for param in provision.params if flag in param.opts)

    assert "this step" in option.help
    assert "plugin" in option.help


@UNUSABLE_PLUGINS
def test_provision_refuses_a_plugin_before_any_digitalocean_call_or_ssh(
    required_environment, monkeypatch, tmp_path, make_entry, message
):
    """A plugin that fails to load or resolve exits 1 before reaching the droplet."""
    monkeypatch.setattr(cli, "DigitalOceanClient", _unreachable_client)
    fake_runner = FakeScriptRunner()
    monkeypatch.setattr(gfz.remote, "ssh_runner", lambda settings, host: fake_runner)

    result = runner.invoke(
        app, ["--plugin", make_entry(tmp_path), "provision", "alpha"]
    )

    assert result.exit_code == 1
    assert message in result.stderr
    assert "Traceback" not in result.stderr
    assert fake_runner.runs == []


@UNRENDERABLE_SCRIPTS
@pytest.mark.parametrize("dry_run", [[], ["--dry-run"]], ids=["run", "dry-run"])
def test_provision_refuses_a_template_that_does_not_render_before_any_call(
    required_environment, monkeypatch, tmp_path, script, reason, dry_run
):
    """A plugin template that does not render exits 1 before reaching the droplet."""
    directory = _plugin_with_script(tmp_path, script)
    monkeypatch.setattr(cli, "DigitalOceanClient", _unreachable_client)
    fake_runner = FakeScriptRunner()
    monkeypatch.setattr(gfz.remote, "ssh_runner", lambda settings, host: fake_runner)

    result = runner.invoke(
        app, ["--plugin", str(directory), "provision", "alpha", *dry_run]
    )

    assert result.exit_code == 1
    assert "broken/run" in result.stderr
    assert reason in result.stderr
    assert "Traceback" not in result.stderr
    assert result.stdout == ""
    assert fake_runner.runs == []


def test_provision_renders_only_the_selected_steps(
    required_environment, monkeypatch, tmp_path
):
    """A template outside the selection is never rendered, so it cannot fail the run."""
    directory = _plugin_with_script(tmp_path, "{% if %}\n")
    fake = FakeHostClient(hosts=[make_host()])
    monkeypatch.setattr(cli, "DigitalOceanClient", lambda: fake)
    fake_runner = FakeScriptRunner()
    monkeypatch.setattr(gfz.remote, "ssh_runner", lambda settings, host: fake_runner)

    result = runner.invoke(
        app,
        ["--plugin", str(directory), "provision", "alpha", "--skip", "broken"],
    )

    assert result.exit_code == 0
    assert fake_runner.names == CORE_NAMES


@pytest.mark.parametrize(
    ("selection", "expected"),
    [(["--only", "empty"], []), (["--skip", "empty"], CORE_NAMES)],
    ids=["only", "skip"],
)
def test_provision_takes_the_name_of_a_plugin_with_no_steps(
    required_environment, monkeypatch, tmp_path, selection, expected
):
    """A plugin with no steps is a valid selection name, and selects nothing."""
    directory = _plugin_with_no_steps(tmp_path)
    fake = FakeHostClient(hosts=[make_host()])
    monkeypatch.setattr(cli, "DigitalOceanClient", lambda: fake)
    fake_runner = FakeScriptRunner()
    monkeypatch.setattr(gfz.remote, "ssh_runner", lambda settings, host: fake_runner)

    result = runner.invoke(
        app, ["--plugin", str(directory), "provision", "alpha", *selection]
    )

    assert result.exit_code == 0
    assert fake_runner.names == expected


def test_provision_refuses_a_plugin_before_judging_the_selection(
    required_environment,
):
    """A plugin failure exits 1 even when the selection also names no step."""
    result = runner.invoke(
        app, ["--plugin", "nonesuch", "provision", "alpha", "--only", "nonesuch"]
    )

    assert result.exit_code == 1
    assert "unknown built-in plugin" in result.stderr


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
        f"{len(DEFAULT_RUN)} steps ran, all succeeded",
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
    assert fake_runner.names == [failing_slug]


def test_a_failing_plugin_step_is_named_in_the_failure_line(
    required_environment, monkeypatch
):
    """A mandatory plugin step's failure names it as `<plugin>/<slug>`."""
    fake = FakeHostClient(hosts=[make_host()])
    monkeypatch.setattr(cli, "DigitalOceanClient", lambda: fake)
    fake_runner = FakeScriptRunner(failures={"claude/agent": 3})
    monkeypatch.setattr(gfz.remote, "ssh_runner", lambda settings, host: fake_runner)

    result = runner.invoke(app, ["provision", "alpha"])

    assert result.exit_code == 1
    assert "provisioning stopped at claude/agent; remote stderr:" in result.stderr


def test_a_failing_optional_plugin_step_is_named_in_the_summary(
    required_environment, monkeypatch
):
    """An optional plugin step's failure shows in the summary under its name."""
    fake = FakeHostClient(hosts=[make_host()])
    monkeypatch.setattr(cli, "DigitalOceanClient", lambda: fake)
    fake_runner = FakeScriptRunner(failures={"claude/admin": 5})
    monkeypatch.setattr(gfz.remote, "ssh_runner", lambda settings, host: fake_runner)

    result = runner.invoke(app, ["provision", "alpha"])

    assert result.exit_code == 0
    assert "  claude/admin: exit 5" in result.stdout


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
    monkeypatch.setenv("GFZ_PLUGINS", "")
    fake = FakeHostClient(hosts=[make_host()])
    monkeypatch.setattr(cli, "DigitalOceanClient", lambda: fake)
    fake_runner = FakeScriptRunner(failures={"optional-thing": 5})
    monkeypatch.setattr(gfz.remote, "ssh_runner", lambda settings, host: fake_runner)

    result = runner.invoke(app, ["provision", "alpha"])

    assert result.exit_code == 0
    assert "optional-thing" in result.stdout
    assert "exit 5" in result.stdout
    assert fake_runner.names == ["base-packages", "optional-thing"]


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
    assert fake_runner.names == DEFAULT_NAMES


def test_provision_only_unknown_step_exits_2_and_names_every_valid_name(
    required_environment,
):
    """--only naming an unknown step exits 2, naming every step and plugin."""
    result = runner.invoke(
        app, [*BOTH_PLUGINS, "provision", "alpha", "--only", "nonesuch"]
    )

    assert result.exit_code == 2
    for name in [*CORE_NAMES, *CLAUDE_NAMES, *HELLO_NAMES]:
        assert name in result.stderr
    assert "valid plugins are claude, hello" in result.stderr


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
    assert fake_runner.names == ["base-packages"]


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
    assert fake_runner.names == DEFAULT_NAMES[1:]


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
    assert fake_runner.names == DEFAULT_NAMES


@UNUSABLE_PLUGINS
def test_create_refuses_a_plugin_before_any_digitalocean_call(
    required_environment, monkeypatch, fake_clock, tmp_path, make_entry, message
):
    """A plugin that fails to load or resolve exits 1 and creates no droplet."""
    monkeypatch.setattr(cli, "DigitalOceanClient", _unreachable_client)

    result = runner.invoke(app, ["--plugin", make_entry(tmp_path), "create", "alpha"])

    assert result.exit_code == 1
    assert message in result.stderr
    assert "Traceback" not in result.stderr
    assert result.stdout == ""


@UNRENDERABLE_SCRIPTS
def test_create_refuses_a_template_that_does_not_render_before_any_call(
    required_environment, monkeypatch, fake_clock, tmp_path, script, reason
):
    """A plugin template that does not render exits 1 and creates no droplet."""
    directory = _plugin_with_script(tmp_path, script)
    monkeypatch.setattr(cli, "DigitalOceanClient", _unreachable_client)

    result = runner.invoke(app, ["--plugin", str(directory), "create", "alpha"])

    assert result.exit_code == 1
    assert "broken/run" in result.stderr
    assert str(directory / "run.sh.j2") in result.stderr
    assert reason in result.stderr
    assert "Traceback" not in result.stderr
    assert result.stdout == ""


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
    assert f"{len(DEFAULT_RUN)} steps ran, all succeeded" in result.stdout


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
        f"{len(DEFAULT_RUN)} steps ran, all succeeded",
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
