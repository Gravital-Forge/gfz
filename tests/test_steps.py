"""Tests for the step registry, its templates, and the scripts they render."""

import re
import shlex
import shutil
import subprocess

import pytest
from jinja2 import meta

from gfz.provision import environment, step_context
from gfz.steps.registry import STEPS, STEPS_DIR, Login

PARAMETRIZE_STEPS = pytest.mark.parametrize("step", STEPS, ids=lambda step: step.slug)

ROOT_PHASE_SLUGS = (
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
)

CLONE_PHASE_SLUGS = ("clone-harness", "clone-repos")

MINTER_PHASE_SLUGS = (
    "minter-checkout",
    "minter",
    "minter-bearer",
    "github-cli",
    "minter-client",
)

STEP_SLUGS = (*ROOT_PHASE_SLUGS, *CLONE_PHASE_SLUGS, *MINTER_PHASE_SLUGS)

MINTER_PREREQUISITES = (
    ("minter", "minter-checkout"),
    ("minter-bearer", "minter"),
    ("minter-client", "minter-checkout"),
    ("minter-client", "agent-tooling"),
)

CHECKOUT_STEP_TEMPLATE = "minter_checkout.sh.j2"
MINTER_STEP_TEMPLATE = "minter.sh.j2"
BEARER_STEP_TEMPLATE = "minter_bearer.sh.j2"
GITHUB_CLI_STEP_TEMPLATE = "github_cli.sh.j2"
CLIENT_STEP_TEMPLATE = "minter_client.sh.j2"

OPTIONAL_SLUGS = frozenset({"admin-tools", "admin-node", "browser-deps"})

PARAMETRIZE_CLONE_STEPS = pytest.mark.parametrize(
    "step",
    [step for step in STEPS if step.slug in CLONE_PHASE_SLUGS],
    ids=lambda step: step.slug,
)

PARAMETRIZE_FORWARDING_STEPS = pytest.mark.parametrize(
    "step",
    [step for step in STEPS if step.forward_agent],
    ids=lambda step: step.slug,
)


def test_the_registry_holds_every_step_in_the_specified_order():
    """STEPS runs the root, clone, and minter phases, in the specified order."""
    assert [step.slug for step in STEPS] == list(STEP_SLUGS)


def test_every_root_phase_step_runs_as_root_and_forwards_no_agent():
    """A root-phase step logs in as root, and none of them needs the ssh agent."""
    by_slug = {step.slug: step for step in STEPS}

    for slug in ROOT_PHASE_SLUGS:
        assert by_slug[slug].login is Login.ROOT
        assert by_slug[slug].forward_agent is False


def test_only_the_three_named_steps_are_optional():
    """The admin tools, the admin's Node, and the browser packages alone may fail."""
    assert {step.slug for step in STEPS if not step.mandatory} == OPTIONAL_SLUGS


def test_every_clone_step_runs_as_the_admin_with_the_agent_forwarded():
    """A clone step logs in as the admin, with the operator's ssh agent forwarded.

    The forwarded agent lives in the operator's admin session.
    """
    by_slug = {step.slug: step for step in STEPS}

    for slug in CLONE_PHASE_SLUGS:
        assert by_slug[slug].login is Login.ADMIN
        assert by_slug[slug].forward_agent is True


def test_the_clone_repos_script_names_every_configured_repository(template_context):
    """The rendered clone-repos script names every configured repository."""
    context = {
        **template_context,
        "repos": [
            "git@example.com:org/repo-one.git",
            "git@example.com:org/repo-two.git",
        ],
    }
    script = environment.get_template("clone_repos.sh.j2").render(context)

    assert "git@example.com:org/repo-one.git" in script
    assert "git@example.com:org/repo-two.git" in script


def test_the_clone_repos_script_refuses_a_workspace_that_is_not_there(
    template_context,
):
    """The clone-repos script stops rather than create the workspace itself.

    Creating it would make clone-harness report it as already there and
    skip the harness clone for good.
    """
    context = {**template_context, "repos": ["git@example.com:org/repo-one.git"]}
    script = environment.get_template("clone_repos.sh.j2").render(context)

    assert "run the clone-harness step first" in script


def test_the_clone_repos_script_with_nothing_configured_says_it_cloned_nothing(
    template_context,
):
    """With nothing configured, the clone-repos script says so and clones nothing."""
    context = {**template_context, "repos": []}
    script = environment.get_template("clone_repos.sh.j2").render(context)

    assert "nothing to clone" in script
    assert "git clone" not in script


@PARAMETRIZE_FORWARDING_STEPS
def test_no_forwarding_script_names_a_private_key(step, template_context, settings):
    """A script run with the agent forwarded never writes the key where it can leak."""
    script = environment.get_template(step.template).render(template_context)

    for needle in ("id_ed25519", "id_rsa", "PRIVATE KEY", "SSH_AUTH_SOCK"):
        assert needle not in script
    assert str(settings.ssh_identity) not in script


@PARAMETRIZE_CLONE_STEPS
def test_every_clone_script_leaves_a_repository_already_there_as_found(
    step, template_context
):
    """A clone script leaves an existing repository as found, so the phase reruns."""
    context = {**template_context, "repos": ["git@example.com:org/repo-one.git"]}
    script = environment.get_template(step.template).render(context)

    assert "is already there; leaving it as found" in script


@PARAMETRIZE_CLONE_STEPS
def test_every_clone_script_clones_bare_as_the_admin_and_again_as_the_agent(
    step, template_context
):
    """The admin clones bare over ssh, and the agent's own git clones that copy.

    Only the agent's git writes into the tree, so every file takes the tree's
    group and default ACLs, and origin ends on the HTTPS GitHub URL.
    """
    agent = template_context["agent_user"]
    script = environment.get_template(step.template).render(template_context)

    for needle in (
        "git clone --bare --quiet",
        "--no-local",
        "--upload-pack \"git -c safe.directory='*' upload-pack\"",
        f'sudo -u "{agent}" -H git',
        'remote set-url origin "$origin"',
        f'setfacl -R -m "u:{agent}:rX" "$staging"',
    ):
        assert needle in script


@PARAMETRIZE_CLONE_STEPS
def test_no_clone_script_chowns_or_moves_into_the_tree(step, template_context):
    """No clone script hands the agent a file the admin made."""
    script = environment.get_template(step.template).render(template_context)

    assert "chown -R" not in script
    assert "sudo mv" not in script


def test_the_minter_checkout_runs_as_the_admin_with_the_agent_forwarded():
    """The checkout reaches the code host, so it runs with the forwarded agent."""
    by_slug = {step.slug: step for step in STEPS}

    assert by_slug["minter-checkout"].login is Login.ADMIN
    assert by_slug["minter-checkout"].forward_agent is True


@pytest.mark.parametrize(
    "slug", ["minter", "minter-bearer", "github-cli", "minter-client"]
)
def test_every_root_minter_step_runs_as_root_and_forwards_no_agent(slug):
    """The minter phase's root steps reach no code host over the forwarded agent."""
    by_slug = {step.slug: step for step in STEPS}

    assert by_slug[slug].login is Login.ROOT
    assert by_slug[slug].forward_agent is False


@pytest.mark.parametrize(("slug", "prerequisite"), MINTER_PREREQUISITES)
def test_every_root_minter_step_refuses_to_run_before_its_prerequisite(
    slug, prerequisite, template_context
):
    """Without its prerequisite, a root minter step refuses in the suite's shape."""
    step = next(step for step in STEPS if step.slug == slug)
    script = environment.get_template(step.template).render(template_context)

    refusal = re.compile(
        rf'echo "[^"\n]+ is not there; run the {prerequisite} step first" >&2\n'
        r"  exit 1\n"
    )
    assert refusal.search(script)


def test_the_checkout_script_fast_forwards_or_clones_as_the_admin_alone(
    template_context,
):
    """The checkout script pulls an existing checkout, clones a missing one, no sudo."""
    checkout = template_context["harness_checkout"]
    script = environment.get_template(CHECKOUT_STEP_TEMPLATE).render(template_context)

    assert f'checkout="{checkout}"' in script
    assert 'if [ -d "$checkout/.git" ]; then' in script
    assert 'git -C "$checkout" pull --ff-only --quiet' in script
    assert f'git clone --quiet "{template_context["harness_repo"]}" "$checkout"' in (
        script
    )
    assert script.index("pull --ff-only") < script.index("git clone")
    assert "sudo" not in script


def test_the_minter_script_builds_from_the_checkouts_minter_package(
    template_context,
):
    """The minter script builds from the admin's checkout, on the rootful daemon."""
    checkout = template_context["harness_checkout"]
    script = environment.get_template(MINTER_STEP_TEMPLATE).render(template_context)

    assert f'source_dir="{checkout}/tools/github-app-minter"\n' in script
    assert (
        'docker build --quiet --tag github-app-minter "$source_dir" >/dev/null'
    ) in script
    assert script.index("source_dir=") < script.index("docker build")
    assert "sudo" not in script


def test_the_minter_scripts_docker_run_carries_every_runbook_flag(template_context):
    """The container runs with exactly the runbook's step 7 flags."""
    app_id = template_context["github_app_id"]
    installation_id = template_context["github_app_installation_id"]
    script = environment.get_template(MINTER_STEP_TEMPLATE).render(template_context)

    assert (
        "docker run -d --name github-app-minter --restart unless-stopped \\\n"
        "    --read-only --cap-drop ALL --security-opt no-new-privileges \\\n"
        "    -p 127.0.0.1:8790:8080 -v github-app-minter:/data \\\n"
        f'    -e GITHUB_APP_ID="{app_id}" \\\n'
        f'    -e GITHUB_APP_INSTALLATION_ID="{installation_id}" \\\n'
        "    github-app-minter >/dev/null\n"
    ) in script


def test_the_minter_script_keeps_a_container_only_on_a_matching_image_and_ids(
    template_context,
):
    """A container stays only when its image and both ID variables match.

    Anything else is removed before the run, so a changed image or ID takes
    effect.
    """
    app_id = template_context["github_app_id"]
    installation_id = template_context["github_app_installation_id"]
    script = environment.get_template(MINTER_STEP_TEMPLATE).render(template_context)

    keep_check = script[: script.index("docker rm -f")]
    for needle in (
        "docker image inspect github-app-minter | inspect_field Id",
        "docker container inspect github-app-minter",
        '"$(inspect_field Image <<<"$container")" = "$built_image"',
        f'grep -qxF "GITHUB_APP_ID={app_id}"',
        f'grep -qxF "GITHUB_APP_INSTALLATION_ID={installation_id}"',
    ):
        assert needle in keep_check
    assert "docker rm -f github-app-minter >/dev/null" in script
    assert script.index("docker rm -f") < script.index("docker run")


def test_the_minter_script_starts_a_kept_container_only_when_it_is_stopped(
    template_context,
):
    """A kept container that is not running starts, and a running one is left alone."""
    script = environment.get_template(MINTER_STEP_TEMPLATE).render(template_context)

    assert re.search(
        r'if \[ "\$\(inspect_field State\.Running <<<"\$container"\)" = false \]; '
        r"then\n\s+docker start github-app-minter >/dev/null\n",
        script,
    )
    assert script.count("docker start") == 1


def test_the_minter_scripts_inspect_helper_reads_nested_fields(tmp_path):
    """inspect_field reads a top-level key, a list, and a nested boolean.

    The helper keeps Docker's `{{ }}` format syntax out of a Jinja template.
    """
    source = (STEPS_DIR / MINTER_STEP_TEMPLATE).read_text()
    helper = source[source.index("inspect_field() {") :]
    helper = helper[: helper.index("\n}\n") + 3]
    inspected = (
        '[{"Image": "sha256:abc", "Config": {"Env": ["A=1", "B=2"]},'
        ' "State": {"Running": false}}]'
    )
    binary = shutil.which("bash")
    assert binary is not None

    def field(name: str) -> str:
        result = subprocess.run(  # noqa: S603 — shutil.which resolved the binary
            [binary, "-c", f'{helper}\ninspect_field "$1"', "bash", name],
            input=inspected,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout

    assert field("Image") == "sha256:abc\n"
    assert field("Config.Env") == "A=1\nB=2\n"
    assert field("State.Running") == "false\n"


def test_the_minter_script_waits_on_the_minters_health_endpoint_alone(
    template_context,
):
    """The health wait polls 127.0.0.1:8790/healthz, and fails with the logs."""
    script = environment.get_template(MINTER_STEP_TEMPLATE).render(template_context)

    assert 'curl -fsS -m 2 http://127.0.0.1:8790/healthz)" = ok' in script
    assert re.findall(r"https?://[^\s\"')]+", script) == [
        "http://127.0.0.1:8790/healthz"
    ]
    assert "for _ in $(seq 30); do" in script
    assert "docker logs --tail 20 github-app-minter >&2" in script
    assert (
        'echo "the minter did not answer on 127.0.0.1:8790 within 30 seconds" >&2'
        in script
    )


def test_the_bearer_script_leaves_a_bearer_already_there_before_any_docker_call(
    template_context,
):
    """A non-empty bearer file stays as found, and the check runs before docker."""
    agent = template_context["agent_user"]
    agent_home = template_context["agent_home"]
    script = environment.get_template(BEARER_STEP_TEMPLATE).render(template_context)

    assert f'bearer="{agent_home}/.config/github-app-minter/bearer"' in script
    assert re.search(
        r'if \[ -s "\$bearer" \]; then\n'
        rf'  echo "{agent} already holds a minter bearer; leaving it as found"\n'
        r"  exit 0\n",
        script,
    )
    assert script.index('[ -s "$bearer" ]') < script.index("docker")


def test_the_bearer_script_writes_the_file_as_the_agent_under_umask_077(
    template_context,
):
    """The agent creates the bearer directory and file, both private, from `/`."""
    agent = template_context["agent_user"]
    script = environment.get_template(BEARER_STEP_TEMPLATE).render(template_context)

    assert f'sudo -u "{agent}" mkdir -p -m 700 "$bearer_dir"' in script
    assert (
        f'| sudo -u "{agent}" sh -c \'umask 077 && cat > "$1"\' sh "$bearer"' in script
    )
    assert script.index("cd /") < script.index("sudo -u")


def test_the_bearer_script_pipes_bearer_new_and_never_captures_it(template_context):
    """The bearer flows by pipe alone, so no shell variable ever holds it."""
    script = environment.get_template(BEARER_STEP_TEMPLATE).render(template_context)

    assert re.search(
        r"docker exec github-app-minter github-app-minter bearer new \\\n\s+\| sudo",
        script,
    )
    assert not re.search(r"\$\([^)]*bearer new", script)
    assert script.count("bearer new") == 1


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


def test_the_client_script_installs_every_client_file_as_the_agent_at_755(
    template_context,
):
    """Every file under the checkout's client/ lands in the agent's ~/.local/bin."""
    agent = template_context["agent_user"]
    agent_home = template_context["agent_home"]
    checkout = template_context["harness_checkout"]
    script = environment.get_template(CLIENT_STEP_TEMPLATE).render(template_context)

    install = f'install -o "{agent}" -g "{agent}" -m 755 "$client_dir"/* "$agent_bin/"'
    client_dir = f'client_dir="{checkout}/tools/github-app-minter/client"\n'
    agent_bin = f'agent_bin="{agent_home}/.local/bin"\n'
    assert install in script
    assert client_dir in script
    assert agent_bin in script
    assert script.index(client_dir) < script.index(install)
    assert script.index(agent_bin) < script.index(install)


def test_the_client_script_sets_the_empty_helper_before_github_app(
    template_context,
):
    """The helper list becomes exactly an empty value, then `github-app`.

    git config runs as the agent, from `/`, without a login shell, because
    `sudo -u … -i` drops the empty argument.
    """
    agent = template_context["agent_user"]
    agent_home = template_context["agent_home"]
    script = environment.get_template(CLIENT_STEP_TEMPLATE).render(template_context)

    assert (
        f'agent_git() {{ (cd / && sudo -u "{agent}" git config '
        f'--file "{agent_home}/.gitconfig" "$@"); }}'
    ) in script
    reset = "agent_git --replace-all credential.https://github.com.helper ''\n"
    add = "agent_git --add credential.https://github.com.helper github-app\n"
    assert reset in script
    assert add in script
    assert script.index(reset) < script.index(add)
    assert script.count("credential.https://github.com.helper") == 2


TRICKY_GIT_NAME = "Ada \"Bit\" $HOME 'x' `y`"
TRICKY_GIT_EMAIL = "a\"b$HOME'c`d`@example.com"


def test_the_client_script_sets_the_identity_shell_quoted(settings):
    """A name or email holding `"`, `$`, `'`, or a backtick reaches git as given."""
    tricky = settings.model_copy(
        update={"git_name": TRICKY_GIT_NAME, "git_email": TRICKY_GIT_EMAIL}
    )
    script = environment.get_template(CLIENT_STEP_TEMPLATE).render(step_context(tricky))

    name_line = f"agent_git user.name {shlex.quote(TRICKY_GIT_NAME)}\n"
    email_line = f"agent_git user.email {shlex.quote(TRICKY_GIT_EMAIL)}\n"
    assert name_line in script
    assert email_line in script

    binary = shutil.which("bash")
    assert binary is not None
    for line, value in ((name_line, TRICKY_GIT_NAME), (email_line, TRICKY_GIT_EMAIL)):
        argument = line.split(" ", 2)[2]
        echoed = subprocess.run(  # noqa: S603 — shutil.which resolved the binary
            [binary, "-c", f"printf %s {argument}"],
            capture_output=True,
            text=True,
            check=True,
        )
        assert echoed.stdout == value


def test_the_client_script_fails_when_the_agents_gh_is_not_the_shim(
    template_context,
):
    """`command -v gh` in the agent's login shell must name the shim.

    Any other answer means `~/.local/bin` comes after the real gh on the
    agent's PATH, and the script fails with that message.
    """
    agent = template_context["agent_user"]
    agent_home = template_context["agent_home"]
    script = environment.get_template(CLIENT_STEP_TEMPLATE).render(template_context)

    assert f"sudo -u \"{agent}\" -i sh -c 'command -v gh'" in script
    assert f'agent_bin="{agent_home}/.local/bin"\n' in script
    failing_branch = re.search(
        r'if \[ "\$agent_gh" != "\$agent_bin/gh" \]; then\n'
        r"(?P<body>(?:  .*\n)+)fi\n",
        script,
    )
    assert failing_branch
    body = failing_branch["body"]
    assert "~/.local/bin comes after the real gh on" in body
    assert "PATH" in body
    assert ">&2" in body
    assert body.endswith("  exit 1\n")


GITHUB_REMOTES = (
    "git@github.com:o/n.git",
    "ssh://git@github.com/o/n.git",
    "https://github.com/o/n",
    "https://github.com/o/n.git",
)


def run_github_origin(remote: str) -> subprocess.CompletedProcess[str]:
    """Run the `_github_origin.j2` partial's function in bash on one remote."""
    function = (STEPS_DIR / "_github_origin.j2").read_text()
    binary = shutil.which("bash")
    assert binary is not None

    return subprocess.run(  # noqa: S603 — shutil.which resolved the binary
        [binary, "-c", f'{function}\ngithub_origin "$1"', "bash", remote],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("remote", GITHUB_REMOTES)
def test_github_origin_maps_every_github_remote_to_its_https_url(remote):
    """Every accepted GitHub remote form maps to the one HTTPS origin."""
    result = run_github_origin(remote)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "https://github.com/o/n.git\n"


def test_github_origin_refuses_a_remote_outside_github():
    """A remote on another host fails, and the message says why."""
    result = run_github_origin("git@gitlab.com:o/n.git")

    assert result.returncode != 0
    assert "is not a GitHub remote" in result.stderr


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
    """The base-packages script installs git and the packages later steps need.

    git serves the clone steps, and `_agent_clone.j2` calls `setfacl` from acl.
    Rootless Docker's prerequisites take dbus-user-session. The minter step
    parses `docker inspect` output with python3.
    """
    script = environment.get_template("base_packages.sh.j2").render(template_context)

    assert (
        "apt_get install -y ca-certificates curl git acl dbus-user-session python3"
        in script
    )


def test_the_browser_deps_script_installs_ffmpeg(template_context):
    """The browser-deps script installs ffmpeg, which converts a proof's video."""
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
        value = template_context[name]
        if isinstance(value, list):
            for entry in value:
                assert str(entry) in script
        else:
            assert str(value) in script


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
