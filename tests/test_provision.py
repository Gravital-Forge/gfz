"""Tests for gfz's provisioning engine: selection, rendering, and the run."""

from pathlib import Path

import jinja2
import pytest

from gfz.commands import CommandError
from gfz.provision import (
    RenderedStep,
    StepSelectionError,
    clone_context,
    environment,
    handoff_lines,
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
            "workspace": f"/home/{settings.agent_user}/Projects/dev",
            "harness_repo": settings.harness_repo,
            "repos": list(settings.repos),
            "harness_checkout": f"/home/{settings.admin_user}/harness",
            "minter_container": "github-app-minter",
            "github_app_id": settings.github_app_id,
            "github_app_installation_id": settings.github_app_installation_id,
            "git_name": settings.git_name,
            "git_email": settings.git_email,
        }

    def test_agent_home_and_workspace_follow_the_agent_user(self, settings):
        """agent_home and workspace are derived from the agent user, not hardcoded."""
        context = step_context(settings)

        assert settings.agent_user in context["agent_home"]
        assert settings.agent_user in context["workspace"]


class TestCloneContext:
    """Tests for clone_context."""

    def test_returns_step_context_with_only_repos_replaced(self, settings):
        """clone_context matches step_context in every key but repos."""
        base = step_context(settings)
        cloned = clone_context(settings, "git@example.com:org/one.git")

        assert set(cloned) == set(base)
        assert {key for key in base if cloned[key] != base[key]} == {"repos"}

    def test_repos_holds_exactly_the_one_repository_given(self, settings):
        """Repos holds a single-element list carrying the given repository."""
        context = clone_context(settings, "git@example.com:org/one.git")

        assert context["repos"] == ["git@example.com:org/one.git"]


class TestHandoffLines:
    """Tests for handoff_lines."""

    def test_returns_the_key_load_then_the_claude_line(self, settings):
        """The key load comes first, over ssh's stdin, and claude second.

        Both ssh lines carry gfz's own identity, as `gfz ssh` does.
        """
        lines = handoff_lines(settings, make_host(ip="203.0.113.10"))

        identity = "/home/admin/.ssh/id_ed25519"
        assert lines == (
            "1. Load the GitHub App's private key from this machine, then delete"
            " every copy of the key file:",
            '   export APP_KEY_FILE="<path>"   # the App\'s private key file,'
            " on this machine",
            f"   ssh -i {identity} admin@203.0.113.10 sudo docker exec -i"
            ' github-app-minter github-app-minter key set < "$APP_KEY_FILE"',
            f"2. ssh -i {identity} agent@203.0.113.10, then run: claude",
        )

    def test_an_identity_path_holding_a_space_comes_out_quoted(self, settings):
        """An identity path with a space reaches both ssh lines shell-quoted."""
        spaced = settings.model_copy(
            update={"ssh_identity": Path("/home/admin/my keys/id_ed25519")}
        )

        lines = handoff_lines(spaced, make_host(ip="203.0.113.10"))

        quoted = "'/home/admin/my keys/id_ed25519'"
        assert f"ssh -i {quoted} admin@203.0.113.10 " in lines[2]
        assert lines[3] == f"2. ssh -i {quoted} agent@203.0.113.10, then run: claude"

    def test_a_host_with_no_address_raises_command_error(self, settings):
        """A host with ip=None raises CommandError, since it has no address."""
        host = make_host(ip=None)

        with pytest.raises(CommandError):
            handoff_lines(settings, host)


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
