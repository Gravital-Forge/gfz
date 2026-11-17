"""Tests for gfz's provisioning engine: selection, rendering, and the run."""

import jinja2
import pytest

from gfz.commands import CommandError
from gfz.plugin import PluginError, load_plugin, load_plugins
from gfz.provision import (
    RenderedStep,
    StepSelectionError,
    closing_line,
    plugin_contexts,
    provision_host,
    render_steps,
    run_order,
    select_steps,
    step_context,
    template_environment,
)
from gfz.steps.registry import STEPS, STEPS_DIR, Login, Step
from tests.conftest import (
    EXAMPLE_DIR,
    REMOTE_FAILURE_STDERR,
    FakeScriptRunner,
    make_host,
    make_step,
)

FAILING_STEP = make_step(slug="fails-hard", summary="A step scripted to fail.")
OPTIONAL_STEP = make_step(
    slug="optional-thing",
    login=Login.ADMIN,
    forward_agent=True,
    summary="A step scripted to fail, but optionally.",
    mandatory=False,
)

PLUGINS = load_plugins(["claude", str(EXAMPLE_DIR)])
RUN = run_order(PLUGINS)
CLAUDE_NAMES = ["claude/agent", "claude/admin"]
HELLO_NAMES = ["hello/greet"]
CORE_NAMES = [step.slug for step in STEPS]


def _rendered(step: Step, script: str = "set -euo pipefail\necho hi\n") -> RenderedStep:
    """Build a RenderedStep pairing `step` with `script`, without rendering."""
    return RenderedStep(step=step, script=script)


def _names(steps):
    """Return the name of every step in `steps`, in order."""
    return [step.name for step in steps]


def _write_plugin(directory, manifest, scripts):
    """Write a plugin directory holding `manifest` and each named script's text."""
    directory.mkdir(parents=True)
    (directory / "plugin.toml").write_text(manifest, encoding="utf-8")
    for name, text in scripts.items():
        (directory / name).write_text(text, encoding="utf-8")
    return directory


ONE_STEP_MANIFEST = """\
name = "probe"
summary = "A plugin a test invented."
{settings}
[[steps]]
slug = "one"
script = "one.sh.j2"
summary = "The one step."
"""

INCLUDES_APT = 'set -euo pipefail\n{% include "_apt.j2" %}\napt_get update\n'

NO_STEPS_MANIFEST = 'name = "empty"\nsummary = "A plugin with no steps."\n'


class TestStepContext:
    """Tests for step_context."""

    def test_returns_every_pinned_key_with_the_settings_values(self, settings):
        """step_context returns every pinned key, sourced from settings."""
        context = step_context(settings)

        assert context == {
            "admin_user": settings.admin_user,
            "admin_home": f"/home/{settings.admin_user}",
            "agent_user": settings.agent_user,
            "agent_home": f"/home/{settings.agent_user}",
        }

    def test_agent_home_follows_the_agent_user(self, settings):
        """agent_home is derived from the agent user, not hardcoded."""
        renamed = settings.model_copy(update={"agent_user": "coder"})

        assert step_context(renamed)["agent_home"] == "/home/coder"


class TestClosingLine:
    """Tests for closing_line."""

    @pytest.mark.parametrize("verb", ["created", "provisioned"])
    def test_names_the_droplet_its_address_and_the_agent_user(self, settings, verb):
        """The line names the droplet, its address, the verb, and the agent user."""
        host = make_host(name="alpha", ip="203.0.113.10")

        assert closing_line(verb, host, settings) == (
            f"droplet alpha (203.0.113.10) {verb} with agent user agent"
        )

    def test_a_host_with_no_address_raises_command_error(self, settings):
        """A host with ip=None raises CommandError, since it has no address."""
        with pytest.raises(CommandError):
            closing_line("created", make_host(ip=None), settings)


class TestRunOrder:
    """Tests for run_order."""

    def test_core_runs_first_then_each_plugin_in_list_order(self):
        """Every core step runs, then each plugin's steps in manifest order."""
        assert _names(RUN) == [*CORE_NAMES, *CLAUDE_NAMES, *HELLO_NAMES]

    def test_the_plugin_list_order_decides_the_plugin_order(self):
        """Listing hello before claude runs hello's steps first."""
        plugins = load_plugins([str(EXAMPLE_DIR), "claude"])

        assert _names(run_order(plugins)) == [
            *CORE_NAMES,
            *HELLO_NAMES,
            *CLAUDE_NAMES,
        ]

    def test_no_plugins_run_core_alone(self):
        """With no plugins, the run order is the core registry."""
        assert run_order(()) == STEPS


class TestSelectSteps:
    """Tests for select_steps."""

    def test_no_selection_returns_every_step_unchanged(self):
        """With no selection, every step comes back, in run order."""
        assert select_steps(PLUGINS) == RUN

    def test_only_returns_just_those_steps_in_run_order(self):
        """`only` keeps just the named steps, in run order, any input order."""
        names = ("hello/greet", STEPS[2].slug, STEPS[0].slug)

        selected = select_steps(PLUGINS, only=names)

        assert _names(selected) == [STEPS[0].slug, STEPS[2].slug, "hello/greet"]

    def test_only_a_plugin_name_selects_every_step_of_that_plugin(self):
        """A bare plugin name in `only` keeps all that plugin's steps."""
        assert _names(select_steps(PLUGINS, only=["claude"])) == CLAUDE_NAMES

    def test_only_a_plugin_step_name_selects_that_step_alone(self):
        """`<plugin>/<slug>` in `only` keeps that one step."""
        assert _names(select_steps(PLUGINS, only=["claude/agent"])) == ["claude/agent"]

    def test_start_from_drops_steps_before_it_and_keeps_the_rest(self):
        """`start_from` drops every step before the named one and keeps it onward."""
        selected = select_steps(PLUGINS, start_from=STEPS[1].slug)

        assert selected == RUN[1:]

    @pytest.mark.parametrize(
        ("plugin", "expected"),
        [("claude", [*CLAUDE_NAMES, *HELLO_NAMES]), ("hello", HELLO_NAMES)],
    )
    def test_start_from_a_plugin_name_starts_at_its_first_step(self, plugin, expected):
        """`start_from` a bare plugin name starts at that plugin's first step."""
        assert _names(select_steps(PLUGINS, start_from=plugin)) == expected

    def test_skip_removes_that_step_and_keeps_the_others_in_order(self):
        """`skip` removes the named step, keeping the rest in run order."""
        selected = select_steps(PLUGINS, skip=[STEPS[1].slug])

        assert selected == (RUN[0], *RUN[2:])

    def test_skip_a_plugin_name_removes_every_step_of_that_plugin(self):
        """A bare plugin name in `skip` removes all that plugin's steps."""
        assert _names(select_steps(PLUGINS, skip=["hello"])) == [
            *CORE_NAMES,
            *CLAUDE_NAMES,
        ]

    def test_only_combined_with_start_from_raises(self):
        """`only` and `start_from` together raise StepSelectionError."""
        with pytest.raises(StepSelectionError):
            select_steps(PLUGINS, only=[STEPS[0].slug], start_from=STEPS[1].slug)

    @pytest.mark.parametrize(
        "selection",
        [
            {"only": ["no-such-step"]},
            {"start_from": "no-such-step"},
            {"skip": ["no-such-step"]},
        ],
        ids=["only", "start_from", "skip"],
    )
    def test_an_unknown_name_raises_naming_every_step_and_plugin(self, selection):
        """An unknown name raises, naming every valid step and every plugin."""
        with pytest.raises(StepSelectionError) as excinfo:
            select_steps(PLUGINS, **selection)

        message = str(excinfo.value)
        assert "'no-such-step'" in message
        for name in [*_names(RUN), "claude", "hello"]:
            assert name in message

    def test_a_plugin_slug_without_its_plugin_is_unknown(self):
        """A plugin step's bare slug names nothing; its name carries the plugin."""
        with pytest.raises(StepSelectionError):
            select_steps(PLUGINS, only=["greet"])

    def test_combination_error_wins_over_unknown_name(self):
        """When both mistakes are present, the combination error wins."""
        with pytest.raises(StepSelectionError, match="cannot be combined"):
            select_steps(PLUGINS, only=["no-such-step"], start_from="also-no-such-step")

    def test_only_plus_skip_of_the_same_name_returns_an_empty_tuple(self):
        """`only` naming a step that `skip` then removes returns an empty tuple."""
        slug = STEPS[0].slug

        selected = select_steps(PLUGINS, only=[slug], skip=[slug])

        assert selected == ()


class TestSelectStepsWithAPluginOfNoSteps:
    """A plugin with no steps is still a name `only`, `skip`, and `start_from` take."""

    @pytest.fixture
    def plugins(self, tmp_path):
        """Load claude, a plugin with no steps, then hello, in that order."""
        empty = _write_plugin(tmp_path / "empty", NO_STEPS_MANIFEST, {})
        return load_plugins(["claude", str(empty), str(EXAMPLE_DIR)])

    def test_only_the_plugin_selects_nothing(self, plugins):
        """`only` naming the plugin selects no step."""
        assert select_steps(plugins, only=["empty"]) == ()

    def test_skip_the_plugin_removes_nothing(self, plugins):
        """`skip` naming the plugin keeps every step."""
        assert select_steps(plugins, skip=["empty"]) == run_order(plugins)

    def test_start_from_the_plugin_starts_at_its_place_in_the_run(self, plugins):
        """`start_from` the plugin keeps the steps of every plugin listed after it."""
        assert _names(select_steps(plugins, start_from="empty")) == HELLO_NAMES

    def test_start_from_the_last_plugin_selects_nothing(self, tmp_path):
        """`start_from` a plugin of no steps listed last selects no step."""
        empty = _write_plugin(tmp_path / "empty", NO_STEPS_MANIFEST, {})
        plugins = load_plugins(["claude", str(empty)])

        assert select_steps(plugins, start_from="empty") == ()

    def test_an_unknown_name_lists_the_plugin_among_the_valid_ones(self, plugins):
        """The unknown-name error names the plugin of no steps as a valid plugin."""
        with pytest.raises(StepSelectionError, match="valid plugins are claude, empty"):
            select_steps(plugins, only=["no-such-step"])


class TestPluginContexts:
    """Tests for plugin_contexts."""

    def test_core_steps_see_the_core_values_and_no_plugin(self, settings):
        """The core context is step_context, with no `plugin` value."""
        contexts = plugin_contexts(settings, PLUGINS, {}, {})

        assert contexts[None] == step_context(settings)
        assert "plugin" not in contexts[None]

    def test_a_plugin_sees_the_core_values_plus_its_own_settings(self, settings):
        """A plugin's context is the core values plus `plugin`, its settings."""
        contexts = plugin_contexts(settings, PLUGINS, {}, {})

        hello = dict(contexts["hello"])
        assert vars(hello.pop("plugin")) == {"greeting": "hello"}
        assert hello == step_context(settings)
        assert vars(contexts["claude"]["plugin"]) == {}

    def test_each_plugin_reads_its_own_table_and_the_environment(self, settings):
        """A plugin's values come from its own table, under its variables."""
        table = {"hello": {"greeting": "from the file"}}

        from_file = plugin_contexts(settings, PLUGINS, {}, table)
        from_environment = plugin_contexts(
            settings, PLUGINS, {"GFZ_PLUGIN_HELLO_GREETING": "from env"}, table
        )

        assert from_file["hello"]["plugin"].greeting == "from the file"
        assert from_environment["hello"]["plugin"].greeting == "from env"

    def test_a_table_for_a_plugin_not_listed_is_ignored(self, settings):
        """A [plugin.<name>] table for a plugin not in `plugins` is never read."""
        table = {"absent": {"anything": 1}}

        contexts = plugin_contexts(settings, PLUGINS, {}, table)

        assert set(contexts) == {None, "claude", "hello"}

    def test_a_required_setting_with_no_value_raises(self, settings, tmp_path):
        """A plugin's required setting with no value raises PluginError."""
        directory = _write_plugin(
            tmp_path / "probe",
            ONE_STEP_MANIFEST.format(settings='[settings.need]\ntype = "string"\n'),
            {"one.sh.j2": "set -euo pipefail\n"},
        )

        with pytest.raises(PluginError, match="GFZ_PLUGIN_PROBE_NEED"):
            plugin_contexts(settings, (load_plugin(directory),), {}, {})


class TestRenderSteps:
    """Tests for render_steps and template_environment."""

    def test_every_step_renders_a_script_with_no_template_syntax_left(self, settings):
        """Every core and plugin step renders, opening with the strict-mode header."""
        rendered = render_steps(RUN, plugin_contexts(settings, PLUGINS, {}, {}))

        assert _names(item.step for item in rendered) == _names(RUN)
        for item in rendered:
            assert item.script.startswith("set -euo pipefail")
            assert "{{" not in item.script
            assert "{%" not in item.script

    def test_a_plugin_step_renders_against_its_own_settings(self, settings):
        """The hello step writes its greeting setting into the agent's home."""
        contexts = plugin_contexts(
            settings, PLUGINS, {"GFZ_PLUGIN_HELLO_GREETING": "good day"}, {}
        )
        greet = next(step for step in RUN if step.name == "hello/greet")

        (rendered,) = render_steps([greet], contexts)

        assert "'good day'" in rendered.script
        assert "/home/agent/hello.txt" in rendered.script

    def test_a_plugin_template_can_include_a_core_partial(self, settings, tmp_path):
        """A plugin template's include falls back to core's step directory."""
        directory = _write_plugin(
            tmp_path / "probe",
            ONE_STEP_MANIFEST.format(settings=""),
            {"one.sh.j2": INCLUDES_APT},
        )
        plugin = load_plugin(directory)

        (rendered,) = render_steps(
            plugin.steps, plugin_contexts(settings, (plugin,), {}, {})
        )

        assert "apt-get -o DPkg::Lock::Timeout=600" in rendered.script

    def test_a_setting_named_like_a_mapping_method_reaches_the_template(
        self, settings, tmp_path
    ):
        """A setting named `items` renders its value, not a dict method."""
        directory = _write_plugin(
            tmp_path / "probe",
            ONE_STEP_MANIFEST.format(
                settings='[settings.items]\ntype = "string"\ndefault = "apples"\n'
            ),
            {"one.sh.j2": "set -euo pipefail\necho {{ plugin.items }}\n"},
        )
        plugin = load_plugin(directory)

        (rendered,) = render_steps(
            plugin.steps, plugin_contexts(settings, (plugin,), {}, {})
        )

        assert "echo apples" in rendered.script

    def test_a_plugin_template_reading_an_undeclared_setting_raises(
        self, settings, tmp_path
    ):
        """`plugin.<name>` for a setting the manifest lacks raises UndefinedError."""
        directory = _write_plugin(
            tmp_path / "probe",
            ONE_STEP_MANIFEST.format(settings=""),
            {"one.sh.j2": "set -euo pipefail\necho {{ plugin.missing }}\n"},
        )
        plugin = load_plugin(directory)

        with pytest.raises(jinja2.UndefinedError):
            render_steps(plugin.steps, plugin_contexts(settings, (plugin,), {}, {}))

    def test_one_environment_serves_each_template_directory(self, tmp_path):
        """template_environment builds one Environment per directory, then reuses it."""
        assert template_environment(STEPS_DIR) is template_environment(STEPS_DIR)
        assert template_environment(tmp_path) is not template_environment(STEPS_DIR)

    @pytest.mark.parametrize(
        ("value", "quoted"), [(8080, "8080"), ("it's", "'it'\"'\"'s'")]
    )
    def test_shell_quote_quotes_the_text_of_any_value(self, value, quoted, tmp_path):
        """shell_quote turns an integer setting into text before quoting it."""
        template = template_environment(tmp_path).from_string(
            "{{ value | shell_quote }}"
        )

        assert template.render(value=value) == quoted

    def test_environment_uses_strict_undefined(self, tmp_path):
        """Every template environment raises on a variable the context lacks."""
        assert template_environment(tmp_path).undefined is jinja2.StrictUndefined

    def test_a_context_missing_a_key_a_template_reads_raises_undefined_error(
        self, template_context
    ):
        """A context missing agent_user raises UndefinedError, not a silent blank."""
        incomplete_context = dict(template_context)
        del incomplete_context["agent_user"]

        with pytest.raises(jinja2.UndefinedError):
            render_steps(STEPS, {None: incomplete_context})


class TestProvisionHost:
    """Tests for provision_host."""

    def test_every_step_succeeding_reports_ok_and_no_abort(self, fake_runner, reporter):
        """When every step succeeds, every outcome is 'ok' and nothing aborts."""
        rendered = [_rendered(STEPS[0]), _rendered(STEPS[1]), _rendered(STEPS[2])]

        result = provision_host(rendered, fake_runner, reporter)

        assert result.aborted_at is None
        assert [outcome.status for outcome in result.outcomes] == ["ok", "ok", "ok"]
        assert fake_runner.names == [step.slug for step in STEPS[:3]]

    def test_a_failing_mandatory_step_stops_the_run(self, reporter):
        """A failing mandatory step stops the run there and aborts at its name."""
        runner = FakeScriptRunner(failures={FAILING_STEP.name: 100})
        rendered = [_rendered(STEPS[0]), _rendered(FAILING_STEP), _rendered(STEPS[1])]

        result = provision_host(rendered, runner, reporter)

        assert result.aborted_at == FAILING_STEP.name
        assert runner.names == [STEPS[0].slug, FAILING_STEP.name]
        failed_outcome = result.outcomes[-1]
        assert failed_outcome.name == FAILING_STEP.name
        assert failed_outcome.status == "failed"
        assert failed_outcome.exit_code == 100
        assert failed_outcome.stderr == REMOTE_FAILURE_STDERR

    def test_a_failing_optional_step_warns_and_the_run_continues(self, reporter):
        """A failing optional step warns, and every later step still runs."""
        runner = FakeScriptRunner(failures={OPTIONAL_STEP.name: 7})
        rendered = [_rendered(STEPS[0]), _rendered(OPTIONAL_STEP), _rendered(STEPS[1])]

        result = provision_host(rendered, runner, reporter)

        assert result.aborted_at is None
        assert runner.names == [STEPS[0].slug, OPTIONAL_STEP.name, STEPS[1].slug]
        statuses = [outcome.status for outcome in result.outcomes]
        assert statuses == ["ok", "warned", "ok"]

    def test_reporter_gets_two_lines_per_step_that_ran(self, reporter):
        """The reporter gets a 'running' line and a result line per step that ran."""
        runner = FakeScriptRunner(
            failures={OPTIONAL_STEP.name: 7, FAILING_STEP.name: 100}
        )
        rendered = [
            _rendered(STEPS[0]),
            _rendered(OPTIONAL_STEP),
            _rendered(FAILING_STEP),
            _rendered(STEPS[1]),
        ]

        provision_host(rendered, runner, reporter)

        assert reporter.lines == [
            f"{STEPS[0].slug}: running",
            f"{STEPS[0].slug}: ok",
            f"{OPTIONAL_STEP.name}: running",
            f"{OPTIONAL_STEP.name}: failed with exit 7 (optional)",
            f"{FAILING_STEP.name}: running",
            f"{FAILING_STEP.name}: failed with exit 100",
        ]

    def test_a_plugin_step_reports_and_aborts_by_its_plugin_name(self, reporter):
        """A plugin step's progress, outcome, and abort carry `<plugin>/<slug>`."""
        runner = FakeScriptRunner(failures={"hello/greet": 4})
        greet = next(step for step in RUN if step.name == "hello/greet")

        result = provision_host([_rendered(greet)], runner, reporter)

        assert reporter.lines == [
            "hello/greet: running",
            "hello/greet: failed with exit 4",
        ]
        assert result.outcomes[0].name == "hello/greet"
        assert result.aborted_at == "hello/greet"

    def test_the_runner_sees_the_right_login_per_step(self, fake_runner, reporter):
        """fake_runner.calls pairs each step's Login with the script it ran."""
        rendered = [
            _rendered(STEPS[0], script="script-zero"),
            _rendered(STEPS[1], script="script-one"),
        ]

        provision_host(rendered, fake_runner, reporter)

        assert fake_runner.calls == [
            (STEPS[0].login, "script-zero"),
            (STEPS[1].login, "script-one"),
        ]
