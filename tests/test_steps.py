"""Tests for the step registry, its templates, and the scripts they render."""

import re
import shutil
import subprocess

import pytest
from jinja2 import meta

from gfz.provision import environment
from gfz.steps.registry import STEPS, STEPS_DIR, Login
from tests.conftest import make_step

PARAMETRIZE_STEPS = pytest.mark.parametrize("step", STEPS, ids=lambda step: step.slug)

STEP_SLUGS = (
    "base-packages",
    "docker-repo",
    "docker-engine",
    "admin-user",
    "agent-user",
    "agent-linger",
    "rootless-docker",
    "docker-host-env",
    "agent-tooling",
    "agent-node",
    "admin-tools",
    "admin-node",
    "browser-deps",
    "github-cli",
)

GITHUB_CLI_STEP_TEMPLATE = "github_cli.sh.j2"

OPTIONAL_SLUGS = frozenset({"admin-tools", "admin-node", "browser-deps", "github-cli"})


def test_the_registry_holds_every_step_in_the_specified_order():
    """STEPS holds the core steps, in the specified order."""
    assert [step.slug for step in STEPS] == list(STEP_SLUGS)


def test_every_core_step_runs_as_root_and_forwards_no_agent():
    """A core step logs in as root, and none of them needs the ssh agent."""
    for step in STEPS:
        assert step.login is Login.ROOT, step.slug
        assert step.forward_agent is False, step.slug


def test_only_the_four_named_steps_are_optional():
    """The admin tools, the admin's Node, the browser packages, and gh may fail."""
    assert {step.slug for step in STEPS if not step.mandatory} == OPTIONAL_SLUGS


def test_a_core_step_is_named_by_its_slug_and_renders_from_the_steps_directory():
    """A core step has no plugin, its name is its slug, and its root is STEPS_DIR."""
    for step in STEPS:
        assert step.plugin is None, step.slug
        assert step.name == step.slug
        assert step.directory == STEPS_DIR, step.slug


def test_a_plugin_step_is_named_by_its_plugin_and_slug(tmp_path):
    """A plugin step's name is `<plugin>/<slug>`."""
    step = make_step(slug="greet", plugin="hello", directory=tmp_path)

    assert step.name == "hello/greet"


def test_the_tooling_summaries_name_only_what_the_steps_install():
    """The agent's tooling step names uv alone, and the admin's Node step no CLI."""
    by_slug = {step.slug: step for step in STEPS}

    assert by_slug["agent-tooling"].summary == "Install uv for the agent."
    assert by_slug["admin-node"].summary == (
        "Install nvm, Node 24, and pnpm for the admin."
    )


def test_the_agent_tooling_script_installs_uv_and_nothing_else(template_context):
    """The agent-tooling script installs uv, and no other tool."""
    script = environment.get_template("agent_tooling.sh.j2").render(template_context)

    assert "https://astral.sh/uv/install.sh" in script
    assert "uv tool install" not in script
    assert "claude" not in script


def test_the_admin_node_script_installs_no_claude_code(template_context):
    """The admin-node script installs nvm, Node, and pnpm, and not Claude Code."""
    script = environment.get_template("admin_node.sh.j2").render(template_context)

    assert "nvm install 24" in script
    assert "corepack enable pnpm" in script
    assert "claude" not in script


def test_the_agent_user_script_creates_no_directory_in_the_agents_home(
    template_context,
):
    """The agent-user script leaves the agent's home empty but for its ssh keys."""
    script = environment.get_template("agent_user.sh.j2").render(template_context)

    assert "mkdir" not in script


def test_the_github_cli_script_installs_gh_from_githubs_apt_repository(
    template_context,
):
    """The github-cli script adds GitHub's keyring and source, then installs gh."""
    keyring = "/etc/apt/keyrings/githubcli-archive-keyring.gpg"
    script = environment.get_template(GITHUB_CLI_STEP_TEMPLATE).render(template_context)

    for needle in (
        "install -m 0755 -d /etc/apt/keyrings",
        "curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \\\n"
        f"  -o {keyring}",
        f"chmod go+r {keyring}",
        f'echo "deb [arch=$(dpkg --print-architecture) signed-by={keyring}]'
        ' https://cli.github.com/packages stable main" \\\n'
        "  > /etc/apt/sources.list.d/github-cli.list",
        "apt_get update",
        "apt_get install -y gh",
    ):
        assert needle in script
    assert script.index("apt_get update") < script.index("apt_get install -y gh")


def test_the_admin_user_script_appends_root_keys_rather_than_overwriting_them(
    template_context,
):
    """The admin-user script appends root's keys, leaving a hand-added key in place."""
    script = environment.get_template("admin_user.sh.j2").render(template_context)

    assert "install -m 0600" not in script
    assert 'grep -qxF -- "$line" "$target"' in script
    assert "done < /root/.ssh/authorized_keys" in script
    assert 'read -r line || [ -n "$line" ]' in script


def test_the_agent_user_script_appends_root_keys_rather_than_overwriting_them(
    template_context,
):
    """The agent-user script appends root's keys, leaving a hand-added key in place."""
    script = environment.get_template("agent_user.sh.j2").render(template_context)

    assert "install -m 0600" not in script
    assert 'grep -qxF -- "$line" "$target"' in script
    assert "done < /root/.ssh/authorized_keys" in script
    assert 'read -r line || [ -n "$line" ]' in script


def test_the_admin_user_script_traps_the_sudoers_tempfile_before_visudo_runs(
    template_context,
):
    """The admin-user script traps its sudoers tempfile before visudo can fail.

    A trap set only after `visudo -cf` would leak the tempfile on a bad
    sudoers line, since the script exits under `set -e` before reaching a
    trailing `rm -f`.
    """
    script = environment.get_template("admin_user.sh.j2").render(template_context)

    mktemp_index = script.index('sudoers_tmp="$(mktemp)"')
    trap_index = script.index("trap 'rm -f \"$sudoers_tmp\"' EXIT")
    visudo_index = script.index("visudo -cf")

    assert mktemp_index < trap_index < visudo_index
    assert script.count('rm -f "$sudoers_tmp"') == 1


def test_the_base_packages_script_installs_git_alongside_the_rest(template_context):
    """The base-packages script installs git and the packages later steps build on.

    Rootless Docker's prerequisites take dbus-user-session. git, acl, and
    python3 are there for the steps a plugin adds.
    """
    script = environment.get_template("base_packages.sh.j2").render(template_context)

    assert (
        "apt_get install -y ca-certificates curl git acl dbus-user-session python3"
        in script
    )


def test_the_browser_deps_script_installs_ffmpeg(template_context):
    """The browser-deps script installs ffmpeg, which converts a recorded video."""
    script = environment.get_template("browser_deps.sh.j2").render(template_context)

    install = script[script.index("apt_get install -y") :]
    assert "ffmpeg" in install.split()


def test_every_template_that_calls_apt_waits_for_the_dpkg_lock(template_context):
    """A template that calls apt-get waits for the dpkg lock through `_apt.j2`.

    A fresh droplet's first-boot upgrader holds the dpkg lock for minutes, so
    every apt call needs the wait `_apt.j2`'s `apt_get` wrapper gives it. A
    template that included `_apt.j2` but still called bare `apt-get`, however
    indented or prefixed with `sudo`, would bypass that wait, so every
    `apt-get` occurrence in a rendered script must carry the lock timeout.
    """
    for step in STEPS:
        source = (STEPS_DIR / step.template).read_text()
        script = environment.get_template(step.template).render(template_context)
        if "apt-get" not in script:
            continue

        assert 'include "_apt.j2"' in source, step.template
        assert not re.search(r"apt-get(?! -o DPkg::Lock::Timeout=600)", script), (
            step.template
        )


def test_every_registered_step_has_a_template_file():
    """Every Step in STEPS names a template file that actually exists."""
    for step in STEPS:
        assert (STEPS_DIR / step.template).exists()


def test_every_template_file_is_registered():
    """Every template under STEPS_DIR is named by some Step, and no other."""
    template_files = {path.name for path in STEPS_DIR.glob("*.sh.j2")}
    registered_templates = {step.template for step in STEPS}
    assert template_files == registered_templates


def test_every_partial_is_underscored_and_included_by_a_registered_template():
    """A `.j2` file that is not a step template is a partial, and no orphan.

    A partial's name starts with `_`, so `test_every_template_file_is_registered`
    never has to know about it, and some registered template includes it, so
    it never sits unused.
    """
    step_templates = {step.template for step in STEPS}
    partials = {
        path.name for path in STEPS_DIR.glob("*.j2") if path.name not in step_templates
    }
    assert partials, "expected at least one shared partial under STEPS_DIR"

    for partial in partials:
        assert partial.startswith("_"), f"{partial} is not a step template or a partial"

    included = set()
    for step in STEPS:
        source = (STEPS_DIR / step.template).read_text()
        for partial in partials:
            if f'include "{partial}"' in source:
                included.add(partial)

    assert included == partials


def test_every_slug_is_unique():
    """No two steps share a slug."""
    slugs = [step.slug for step in STEPS]
    assert len(slugs) == len(set(slugs))


def test_every_slug_is_hyphenated_and_every_template_is_underscored():
    """A slug holds no underscore, and its template is that slug, underscored."""
    for step in STEPS:
        assert "_" not in step.slug
        assert "-" not in step.template
        assert step.template == step.slug.replace("-", "_") + ".sh.j2"


@PARAMETRIZE_STEPS
def test_rendered_script_opens_with_the_shell_prologue(step, template_context):
    """A rendered script starts with the shell prologue, and carries no shebang.

    The runner feeds the script to `bash -s`, so a shebang would be inert.
    """
    script = environment.get_template(step.template).render(template_context)
    assert script.startswith("set -euo pipefail")
    assert not script.startswith("#!")


@PARAMETRIZE_STEPS
def test_rendered_script_leaves_no_jinja_behind(step, template_context):
    """A rendered script carries no unresolved Jinja delimiter."""
    script = environment.get_template(step.template).render(template_context)
    assert "{{" not in script
    assert "{%" not in script


@PARAMETRIZE_STEPS
def test_every_context_value_a_template_uses_reaches_the_script(step, template_context):
    """Every context name a template reads lands, as a value, in its script.

    This is the test that keeps a later step honest: an identity must
    arrive through the context and land in the rendered script.
    """
    source = (STEPS_DIR / step.template).read_text()
    used_names = meta.find_undeclared_variables(environment.parse(source))
    script = environment.get_template(step.template).render(template_context)

    for name in used_names:
        assert str(template_context[name]) in script


@PARAMETRIZE_STEPS
def test_rendered_script_passes_shellcheck(step, template_context, tmp_path):
    """A rendered script passes shellcheck under an explicit bash shell.

    `--shell=bash` is load-bearing: a rendered script carries no shebang, so
    shellcheck left to guess assumes `sh` and rejects `pipefail`.
    """
    script = environment.get_template(step.template).render(template_context)
    script_path = tmp_path / f"{step.slug}.sh"
    script_path.write_text(script)
    binary = shutil.which("shellcheck")
    assert binary is not None

    result = subprocess.run(  # noqa: S603 — shutil.which resolved the binary
        [binary, "--shell=bash", str(script_path)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout
