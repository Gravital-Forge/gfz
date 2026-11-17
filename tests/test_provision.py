"""Tests for gfz's provisioning engine: selection, rendering, and the run."""

import jinja2
import pytest

from gfz.commands import CommandError
from gfz.provision import (
    RenderedStep,
    StepSelectionError,
    closing_line,
    environment,
    provision_host,
    render_steps,
    select_steps,
    step_context,
)
from gfz.steps.registry import STEPS, Login, Step
from tests.conftest import (
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


def _rendered(step: Step, script: str = "set -euo pipefail\necho hi\n") -> RenderedStep:
    """Build a RenderedStep pairing `step` with `script`, without rendering."""
    return RenderedStep(step=step, script=script)


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


class TestSelectSteps:
    """Tests for select_steps."""

    def test_no_arguments_returns_every_step_unchanged(self):
        """With no selection, every step in STEPS comes back, in order."""
        assert select_steps() == STEPS

    def test_only_returns_just_those_steps_in_registry_order(self):
        """`only` keeps just the named steps, in registry order, any input order."""
        names = (STEPS[2].slug, STEPS[0].slug)

        selected = select_steps(only=names)

        assert selected == (STEPS[0], STEPS[2])

    def test_start_from_drops_steps_before_it_and_keeps_the_rest(self):
        """`start_from` drops every step before the named one and keeps it onward."""
        selected = select_steps(start_from=STEPS[1].slug)

        assert selected == STEPS[1:]

    def test_skip_removes_that_step_and_keeps_the_others_in_order(self):
        """`skip` removes the named step, keeping the rest in registry order."""
        selected = select_steps(skip=[STEPS[1].slug])

        assert selected == (STEPS[0], *STEPS[2:])

    def test_only_combined_with_start_from_raises(self):
        """`only` and `start_from` together raise StepSelectionError."""
        with pytest.raises(StepSelectionError):
            select_steps(only=[STEPS[0].slug], start_from=STEPS[1].slug)

    def test_unknown_slug_in_only_raises_naming_every_valid_slug(self):
        """An unknown slug in `only` raises, naming every valid step."""
        with pytest.raises(StepSelectionError) as excinfo:
            select_steps(only=["no-such-step"])

        for step in STEPS:
            assert step.slug in str(excinfo.value)

    def test_unknown_slug_in_start_from_raises_naming_every_valid_slug(self):
        """An unknown slug in `start_from` raises, naming every valid step."""
        with pytest.raises(StepSelectionError) as excinfo:
            select_steps(start_from="no-such-step")

        for step in STEPS:
            assert step.slug in str(excinfo.value)

    def test_unknown_slug_in_skip_raises_naming_every_valid_slug(self):
        """An unknown slug in `skip` raises, naming every valid step."""
        with pytest.raises(StepSelectionError) as excinfo:
            select_steps(skip=["no-such-step"])

        for step in STEPS:
            assert step.slug in str(excinfo.value)

    def test_combination_error_wins_over_unknown_slug(self):
        """When both mistakes are present, the combination error wins."""
        with pytest.raises(StepSelectionError, match="cannot be combined"):
            select_steps(only=["no-such-step"], start_from="also-no-such-step")

    def test_only_plus_skip_of_the_same_slug_returns_an_empty_tuple(self):
        """`only` naming a step that `skip` then removes returns an empty tuple."""
        slug = STEPS[0].slug

        selected = select_steps(only=[slug], skip=[slug])

        assert selected == ()


class TestRenderSteps:
    """Tests for render_steps."""

    def test_every_selected_step_renders_a_script_with_no_template_syntax_left(
        self, template_context
    ):
        """Every step's rendered script starts with the shell strict-mode header."""
        rendered = render_steps(STEPS, template_context)

        assert len(rendered) == len(STEPS)
        for item in rendered:
            assert item.script.startswith("set -euo pipefail")
            assert "{{" not in item.script
            assert "{%" not in item.script

    @pytest.mark.parametrize(
        ("value", "quoted"), [(8080, "8080"), ("it's", "'it'\"'\"'s'")]
    )
    def test_shell_quote_quotes_the_text_of_any_value(self, value, quoted):
        """shell_quote turns an integer setting into text before quoting it."""
        template = environment.from_string("{{ value | shell_quote }}")

        assert template.render(value=value) == quoted

    def test_environment_uses_strict_undefined(self):
        """The template environment raises on a variable the context lacks."""
        assert environment.undefined is jinja2.StrictUndefined

    def test_a_context_missing_a_key_a_template_reads_raises_undefined_error(
        self, template_context
    ):
        """A context missing agent_user raises UndefinedError, not a silent blank."""
        incomplete_context = dict(template_context)
        del incomplete_context["agent_user"]

        with pytest.raises(jinja2.UndefinedError):
            render_steps(STEPS, incomplete_context)


class TestProvisionHost:
    """Tests for provision_host."""

    def test_every_step_succeeding_reports_ok_and_no_abort(self, fake_runner, reporter):
        """When every step succeeds, every outcome is 'ok' and nothing aborts."""
        rendered = [_rendered(STEPS[0]), _rendered(STEPS[1]), _rendered(STEPS[2])]

        result = provision_host(rendered, fake_runner, reporter)

        assert result.aborted_at is None
        assert [outcome.status for outcome in result.outcomes] == ["ok", "ok", "ok"]
        assert fake_runner.slugs == [step.slug for step in STEPS[:3]]

    def test_a_failing_mandatory_step_stops_the_run(self, reporter):
        """A failing mandatory step stops the run there and aborts at its slug."""
        runner = FakeScriptRunner(failures={FAILING_STEP.slug: 100})
        rendered = [_rendered(STEPS[0]), _rendered(FAILING_STEP), _rendered(STEPS[1])]

        result = provision_host(rendered, runner, reporter)

        assert result.aborted_at == FAILING_STEP.slug
        assert runner.slugs == [STEPS[0].slug, FAILING_STEP.slug]
        failed_outcome = result.outcomes[-1]
        assert failed_outcome.slug == FAILING_STEP.slug
        assert failed_outcome.status == "failed"
        assert failed_outcome.exit_code == 100
        assert failed_outcome.stderr == REMOTE_FAILURE_STDERR

    def test_a_failing_optional_step_warns_and_the_run_continues(self, reporter):
        """A failing optional step warns, and every later step still runs."""
        runner = FakeScriptRunner(failures={OPTIONAL_STEP.slug: 7})
        rendered = [_rendered(STEPS[0]), _rendered(OPTIONAL_STEP), _rendered(STEPS[1])]

        result = provision_host(rendered, runner, reporter)

        assert result.aborted_at is None
        assert runner.slugs == [STEPS[0].slug, OPTIONAL_STEP.slug, STEPS[1].slug]
        statuses = [outcome.status for outcome in result.outcomes]
        assert statuses == ["ok", "warned", "ok"]

    def test_reporter_gets_two_lines_per_step_that_ran(self, reporter):
        """The reporter gets a 'running' line and a result line per step that ran."""
        runner = FakeScriptRunner(
            failures={OPTIONAL_STEP.slug: 7, FAILING_STEP.slug: 100}
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
            f"{OPTIONAL_STEP.slug}: running",
            f"{OPTIONAL_STEP.slug}: failed with exit 7 (optional)",
            f"{FAILING_STEP.slug}: running",
            f"{FAILING_STEP.slug}: failed with exit 100",
        ]

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
