"""The provisioning sequence: one Step record per script, in run order."""

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

STEPS_DIR = Path(__file__).parent


class Login(Enum):
    """The account a step's script runs as on the remote host."""

    ROOT = "root"
    ADMIN = "admin"


@dataclass(frozen=True)
class Step:
    """One provisioning step: a script, the account that runs it, and its policy.

    Attributes:
        slug: The step's name within its source: core or one plugin.
        login: The account the script runs as.
        forward_agent: Whether the ssh session forwards the operator's local
            agent, which a step reaching a private repository needs.
        template: The template's posix path relative to `directory`. Core file
            names carry underscores where the slug carries hyphens.
        summary: One line saying what the step does, printed by `gfz steps`.
        mandatory: Whether a failure stops the run. An optional step's
            failure only warns.
        plugin: The name of the plugin that supplies the step, or `None` for
            a core step.
        directory: The template root: STEPS_DIR for a core step, the plugin's
            directory for a plugin step.
    """

    slug: str
    login: Login
    forward_agent: bool
    template: str
    summary: str
    mandatory: bool
    plugin: str | None = None
    directory: Path = STEPS_DIR

    @property
    def name(self) -> str:
        """The step's name on the command line and in every line gfz prints.

        A core step's name is its slug; a plugin step's is `<plugin>/<slug>`.
        """
        if self.plugin is None:
            return self.slug
        return f"{self.plugin}/{self.slug}"


STEPS: tuple[Step, ...] = (
    Step(
        slug="base-packages",
        login=Login.ROOT,
        forward_agent=False,
        template="base_packages.sh.j2",
        summary=(
            "Install ca-certificates, curl, git, acl, dbus-user-session, and python3."
        ),
        mandatory=True,
    ),
    Step(
        slug="docker-repo",
        login=Login.ROOT,
        forward_agent=False,
        template="docker_repo.sh.j2",
        summary="Add Docker's apt keyring and package source.",
        mandatory=True,
    ),
    Step(
        slug="docker-engine",
        login=Login.ROOT,
        forward_agent=False,
        template="docker_engine.sh.j2",
        summary="Install the Docker engine and its rootless tooling.",
        mandatory=True,
    ),
    Step(
        slug="admin-user",
        login=Login.ROOT,
        forward_agent=False,
        template="admin_user.sh.j2",
        summary="Create the admin user with sudo rights and root's ssh keys.",
        mandatory=True,
    ),
    Step(
        slug="agent-user",
        login=Login.ROOT,
        forward_agent=False,
        template="agent_user.sh.j2",
        summary="Create the unprivileged agent user with a private home.",
        mandatory=True,
    ),
    Step(
        slug="agent-linger",
        login=Login.ROOT,
        forward_agent=False,
        template="agent_linger.sh.j2",
        summary="Enable linger so the agent's systemd session outlives logout.",
        mandatory=True,
    ),
    Step(
        slug="rootless-docker",
        login=Login.ROOT,
        forward_agent=False,
        template="rootless_docker.sh.j2",
        summary="Install rootless Docker in the agent's systemd session.",
        mandatory=True,
    ),
    Step(
        slug="docker-host-env",
        login=Login.ROOT,
        forward_agent=False,
        template="docker_host_env.sh.j2",
        summary="Point the agent's shells and session at the rootless socket.",
        mandatory=True,
    ),
    Step(
        slug="agent-tooling",
        login=Login.ROOT,
        forward_agent=False,
        template="agent_tooling.sh.j2",
        summary="Install uv for the agent.",
        mandatory=True,
    ),
    Step(
        slug="agent-node",
        login=Login.ROOT,
        forward_agent=False,
        template="agent_node.sh.j2",
        summary="Install nvm, Node 24, and pnpm for the agent.",
        mandatory=True,
    ),
    Step(
        slug="admin-tools",
        login=Login.ROOT,
        forward_agent=False,
        template="admin_tools.sh.j2",
        summary="Install tmux and htop.",
        mandatory=False,
    ),
    Step(
        slug="admin-node",
        login=Login.ROOT,
        forward_agent=False,
        template="admin_node.sh.j2",
        summary="Install nvm, Node 24, and pnpm for the admin.",
        mandatory=False,
    ),
    Step(
        slug="browser-deps",
        login=Login.ROOT,
        forward_agent=False,
        template="browser_deps.sh.j2",
        summary="Install the Chromium and font packages a browser needs.",
        mandatory=False,
    ),
    Step(
        slug="github-cli",
        login=Login.ROOT,
        forward_agent=False,
        template="github_cli.sh.j2",
        summary="Install the GitHub CLI from GitHub's apt repository.",
        mandatory=False,
    ),
)
