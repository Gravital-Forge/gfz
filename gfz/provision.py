"""The provisioning engine: render the step registry and run it on a host."""

import shlex
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from gfz.commands import host_address
from gfz.hosts import Host
from gfz.remote import ScriptRunner
from gfz.settings import Settings
from gfz.steps.registry import STEPS, STEPS_DIR, Step

environment = Environment(
    loader=FileSystemLoader(STEPS_DIR),
    undefined=StrictUndefined,
    keep_trailing_newline=True,
    trim_blocks=True,
    lstrip_blocks=True,
    autoescape=False,  # noqa: S701 — these render shell; HTML escaping corrupts it
)
environment.filters["shell_quote"] = shlex.quote

StepStatus = Literal["ok", "warned", "failed"]

ClosingVerb = Literal["created", "provisioned"]


class StepSelectionError(Exception):
    """A step selection names no step, or combines flags that cannot combine."""


@dataclass(frozen=True)
class RenderedStep:
    """One selected step and the script rendered for this host."""

    step: Step
    script: str


@dataclass(frozen=True)
class StepOutcome:
    """What one step's run produced.

    Attributes:
        slug: The step's slug.
        status: `"ok"` when the script exited zero. `"warned"` when an
            optional step failed and the run continued. `"failed"` when a
            mandatory step failed and the run stopped.
        exit_code: The exit code the remote shell returned.
        stderr: Everything the script wrote to stderr.
    """

    slug: str
    status: StepStatus
    exit_code: int
    stderr: str


@dataclass(frozen=True)
class ProvisionResult:
    """What a provisioning run did.

    Attributes:
        outcomes: One StepOutcome per step that ran, in run order.
        aborted_at: The slug of the mandatory step that stopped the run, or
            `None` when every selected step ran.
    """

    outcomes: tuple[StepOutcome, ...]
    aborted_at: str | None


def step_context(settings: Settings) -> dict[str, object]:
    """Return the Jinja context every step template renders against."""
    return {
        "admin_user": settings.admin_user,
        "admin_home": _admin_home(settings),
        "agent_user": settings.agent_user,
        "agent_home": _agent_home(settings),
    }


def _admin_home(settings: Settings) -> str:
    """Return the admin user's home directory."""
    return f"/home/{settings.admin_user}"


def _agent_home(settings: Settings) -> str:
    """Return the agent user's home directory."""
    return f"/home/{settings.agent_user}"


def closing_line(verb: ClosingVerb, host: Host, settings: Settings) -> str:
    """Return the line that ends a provisioning run, naming what it left behind.

    Raises:
        CommandError: `host` has no address.
    """
    return (
        f"droplet {host.name} ({host_address(host)}) {verb}"
        f" with agent user {settings.agent_user}"
    )


def _validate_slugs(
    *, only: Sequence[str], start_from: str | None, skip: Sequence[str]
) -> None:
    """Raise StepSelectionError naming every valid slug if any named slug is unknown."""
    valid_slugs = {step.slug for step in STEPS}
    named_slugs = [*only, *skip]
    if start_from is not None:
        named_slugs.append(start_from)
    for slug in named_slugs:
        if slug not in valid_slugs:
            valid = ", ".join(step.slug for step in STEPS)
            raise StepSelectionError(f"unknown step {slug!r}: valid steps are {valid}")


def select_steps(
    *,
    only: Sequence[str] = (),
    start_from: str | None = None,
    skip: Sequence[str] = (),
) -> tuple[Step, ...]:
    """Select and order steps from the module-global STEPS.

    `only` keeps just the named steps, in registry order. `start_from` drops
    every step before the named one and keeps it onward. With neither, every
    step is kept. `skip` then removes the named steps from that result.

    Raises:
        StepSelectionError: `only` and `start_from` are both given, or a
            named slug matches no step in STEPS.
    """
    if only and start_from is not None:
        raise StepSelectionError("--only and --from cannot be combined")
    _validate_slugs(only=only, start_from=start_from, skip=skip)

    if only:
        selected = [step for step in STEPS if step.slug in only]
    elif start_from is not None:
        index = next(i for i, step in enumerate(STEPS) if step.slug == start_from)
        selected = list(STEPS[index:])
    else:
        selected = list(STEPS)

    return tuple(step for step in selected if step.slug not in skip)


def render_steps(
    steps: Sequence[Step], context: Mapping[str, object]
) -> tuple[RenderedStep, ...]:
    """Render each step's template against `context`."""
    return tuple(
        RenderedStep(
            step=step, script=environment.get_template(step.template).render(context)
        )
        for step in steps
    )


def provision_host(
    rendered: Sequence[RenderedStep],
    runner: ScriptRunner,
    report: Callable[[str], None],
) -> ProvisionResult:
    """Run each rendered step on the host in order, stopping on a mandatory failure.

    Reports progress through `report`: one "running" line before a step
    runs, and one result line after it. Returns the outcome of every step
    that ran, and the slug of the mandatory step that stopped the run, if
    any.
    """
    outcomes: list[StepOutcome] = []
    for item in rendered:
        slug = item.step.slug
        report(f"{slug}: running")
        result = runner(item.step, item.script)

        if result.exit_code == 0:
            report(f"{slug}: ok")
            outcomes.append(StepOutcome(slug, "ok", 0, result.stderr))
            continue

        if not item.step.mandatory:
            report(f"{slug}: failed with exit {result.exit_code} (optional)")
            outcomes.append(
                StepOutcome(slug, "warned", result.exit_code, result.stderr)
            )
            continue

        report(f"{slug}: failed with exit {result.exit_code}")
        outcomes.append(StepOutcome(slug, "failed", result.exit_code, result.stderr))
        return ProvisionResult(outcomes=tuple(outcomes), aborted_at=slug)

    return ProvisionResult(outcomes=tuple(outcomes), aborted_at=None)
