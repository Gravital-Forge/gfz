"""The provisioning engine: order, select, render, and run core and plugin steps."""

import functools
import shlex
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Literal

from jinja2 import (
    Environment,
    FileSystemLoader,
    StrictUndefined,
    TemplateError,
    TemplateSyntaxError,
)

from gfz.commands import host_address
from gfz.hosts import Host
from gfz.plugin import Plugin, resolve_settings
from gfz.remote import ScriptRunner
from gfz.settings import Settings
from gfz.steps.registry import STEPS, STEPS_DIR, Step

StepStatus = Literal["ok", "warned", "failed"]

# Keyed by a step's `plugin`: None for core, else the plugin's name.
StepContexts = Mapping[str | None, Mapping[str, object]]

ClosingVerb = Literal["created", "provisioned"]


class StepSelectionError(Exception):
    """A step selection names no step, or combines flags that cannot combine."""


class RenderError(Exception):
    """A step's template does not render."""


@dataclass(frozen=True)
class RenderedStep:
    """One selected step and the script rendered for this host."""

    step: Step
    script: str


@dataclass(frozen=True)
class StepOutcome:
    """What one step's run produced.

    Attributes:
        name: The step's name.
        status: `"ok"` when the script exited zero. `"warned"` when an
            optional step failed and the run continued. `"failed"` when a
            mandatory step failed and the run stopped.
        exit_code: The exit code the remote shell returned.
        stderr: Everything the script wrote to stderr.
    """

    name: str
    status: StepStatus
    exit_code: int
    stderr: str


@dataclass(frozen=True)
class ProvisionResult:
    """What a provisioning run did.

    Attributes:
        outcomes: One StepOutcome per step that ran, in run order.
        aborted_at: The name of the mandatory step that stopped the run, or
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


def plugin_contexts(
    settings: Settings,
    plugins: Sequence[Plugin],
    environ: Mapping[str, str],
    table: Mapping[str, Mapping[str, object]],
) -> dict[str | None, Mapping[str, object]]:
    """Return the context each step renders against, keyed by the step's plugin.

    Core steps, under `None`, see the core values alone. Each plugin sees the
    core values plus `plugin`, its resolved settings. `table` is the config
    file's whole `plugin` table; each plugin reads only its own entry.

    `plugin` is a namespace, not a dict, so a setting named `items` or `keys`
    reaches the template instead of a dict method.

    Raises:
        PluginError: A plugin's settings fail to resolve.
    """
    core = step_context(settings)
    contexts: dict[str | None, Mapping[str, object]] = {None: core}
    for plugin in plugins:
        resolved = resolve_settings(plugin, environ, table.get(plugin.name, {}))
        contexts[plugin.name] = {**core, "plugin": SimpleNamespace(**resolved.values)}
    return contexts


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


def run_order(plugins: Sequence[Plugin]) -> tuple[Step, ...]:
    """Return every step in run order: core, then each plugin's, in list order."""
    return (*STEPS, *(step for plugin in plugins for step in plugin.steps))


def _selects(name: str, step: Step) -> bool:
    """Return whether `name` selects `step`: its own name, or its plugin's."""
    return name in (step.name, step.plugin)


def _validate_names(
    steps: Sequence[Step], plugin_names: Sequence[str], names: Iterable[str]
) -> None:
    """Raise StepSelectionError naming every valid name if any name is unknown."""
    valid_names = {step.name for step in steps} | set(plugin_names)
    for name in names:
        if name not in valid_names:
            message = (
                f"unknown step {name!r}: valid steps are"
                f" {', '.join(step.name for step in steps)}"
            )
            if plugin_names:
                message += f"; valid plugins are {', '.join(plugin_names)}"
            raise StepSelectionError(message)


def _start_index(
    steps: Sequence[Step], plugin_names: Sequence[str], start_from: str
) -> int:
    """Return the index in `steps` where a run that starts at `start_from` begins.

    A plugin name starts the run at its place in the plugin order, so a plugin
    with no steps starts it at the next plugin's first step.
    """
    if start_from in plugin_names:
        onward = set(plugin_names[plugin_names.index(start_from) :])
        return next(
            (i for i, step in enumerate(steps) if step.plugin in onward), len(steps)
        )
    return next(i for i, step in enumerate(steps) if step.name == start_from)


def select_steps(
    plugins: Sequence[Plugin],
    *,
    only: Sequence[str] = (),
    start_from: str | None = None,
    skip: Sequence[str] = (),
) -> tuple[Step, ...]:
    """Select steps from the run of core and `plugins`, keeping their run order.

    Each name is a step's name, or a bare plugin name that stands for every
    step of that plugin, which is none for a plugin with no steps. `only` keeps
    just the named steps. `start_from` drops every step before the named step,
    or before the named plugin's place in the run, and keeps the rest. With
    neither, every step is kept. `skip` then removes the named steps from that
    result.

    Raises:
        StepSelectionError: `only` and `start_from` are both given, or a
            name is no step's name and no plugin's.
    """
    if only and start_from is not None:
        raise StepSelectionError("--only and --from cannot be combined")
    steps = run_order(plugins)
    plugin_names = [plugin.name for plugin in plugins]
    named = [*only, *skip] if start_from is None else [*only, *skip, start_from]
    _validate_names(steps, plugin_names, named)

    if only:
        selected = [step for step in steps if any(_selects(n, step) for n in only)]
    elif start_from is not None:
        selected = list(steps[_start_index(steps, plugin_names, start_from) :])
    else:
        selected = list(steps)

    return tuple(step for step in selected if not any(_selects(n, step) for n in skip))


@functools.cache
def template_environment(directory: Path) -> Environment:
    """Return the one Jinja environment for templates rooted at `directory`.

    A template's includes resolve from `directory` first, then from core's
    step directory, so a plugin template can include a core partial.
    """
    environment = Environment(
        loader=FileSystemLoader([directory, STEPS_DIR]),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
        autoescape=False,  # noqa: S701 — these render shell; HTML escaping corrupts it
    )
    environment.filters["shell_quote"] = lambda value: shlex.quote(str(value))
    return environment


def _render(step: Step, context: Mapping[str, object]) -> str:
    """Render one step's template against `context`.

    Raises:
        RenderError: The template, or a template it includes, has a syntax
            error, reads a value the context lacks, or cannot be found.
    """
    try:
        return (
            template_environment(step.directory)
            .get_template(step.template)
            .render(context)
        )
    except TemplateSyntaxError as error:
        raise RenderError(
            f"step {step.name}: {step.directory / step.template} does not render:"
            f" {error.filename}, line {error.lineno}: {error.message}"
        ) from error
    except TemplateError as error:
        raise RenderError(
            f"step {step.name}: {step.directory / step.template} does not render:"
            f" {error.message}"
        ) from error


def render_steps(
    steps: Sequence[Step], contexts: StepContexts
) -> tuple[RenderedStep, ...]:
    """Render each step's template against the context for its plugin.

    Raises:
        RenderError: A step's template does not render.
    """
    return tuple(
        RenderedStep(step=step, script=_render(step, contexts[step.plugin]))
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
    that ran, and the name of the mandatory step that stopped the run, if
    any.
    """
    outcomes: list[StepOutcome] = []
    for item in rendered:
        name = item.step.name
        report(f"{name}: running")
        result = runner(item.step, item.script)

        if result.exit_code == 0:
            report(f"{name}: ok")
            outcomes.append(StepOutcome(name, "ok", 0, result.stderr))
            continue

        if not item.step.mandatory:
            report(f"{name}: failed with exit {result.exit_code} (optional)")
            outcomes.append(
                StepOutcome(name, "warned", result.exit_code, result.stderr)
            )
            continue

        report(f"{name}: failed with exit {result.exit_code}")
        outcomes.append(StepOutcome(name, "failed", result.exit_code, result.stderr))
        return ProvisionResult(outcomes=tuple(outcomes), aborted_at=name)

    return ProvisionResult(outcomes=tuple(outcomes), aborted_at=None)
