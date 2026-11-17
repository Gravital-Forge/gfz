"""The gfz command line: argument parsing and output rendering only."""

import os
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Annotated

import typer
from pydantic import ValidationError

from gfz import commands, provision, remote
from gfz.digitalocean import DigitalOceanClient, DigitalOceanError
from gfz.hosts import ActionStatus, Host, PowerAction
from gfz.plugin import Plugin, PluginError, builtin_names, load_plugins
from gfz.provision import (
    ProvisionResult,
    RenderedStep,
    RenderError,
    StepContexts,
    StepSelectionError,
)
from gfz.settings import (
    PluginList,
    Settings,
    configuration_error_message,
    plugin_tables,
    resolved_options,
)
from gfz.steps.registry import Step

app = typer.Typer(
    no_args_is_help=True,
    help="Create and provision DigitalOcean droplets for coding agents.",
)
config_app = typer.Typer(
    no_args_is_help=True, help="Inspect gfz's resolved configuration."
)
app.add_typer(config_app, name="config")


@app.callback()
def main(
    ctx: typer.Context,
    admin_user: Annotated[
        str | None, typer.Option(help="The admin account created on each droplet.")
    ] = None,
    agent_user: Annotated[
        str | None, typer.Option(help="The agent account created on each droplet.")
    ] = None,
    ssh_keys: Annotated[
        str | None,
        typer.Option(help="DigitalOcean account SSH keys to install, comma-separated."),
    ] = None,
    ssh_identity: Annotated[
        str | None,
        typer.Option(help="Local private key path used to reach a droplet."),
    ] = None,
    region: Annotated[
        str | None,
        typer.Option(help="The DigitalOcean region a droplet is created in."),
    ] = None,
    size: Annotated[
        str | None, typer.Option(help="The DigitalOcean droplet size slug.")
    ] = None,
    image: Annotated[
        str | None, typer.Option(help="The DigitalOcean droplet image slug.")
    ] = None,
    tag: Annotated[
        str | None, typer.Option(help="The tag applied to a droplet gfz creates.")
    ] = None,
    plugin: Annotated[
        list[str] | None,
        typer.Option(
            "--plugin",
            help=(
                "A plugin whose steps run after core: a built-in name, or a path"
                " that is absolute or starts with ~. Repeat for more."
            ),
        ),
    ] = None,
    config: Annotated[
        str | None, typer.Option(help="Path to gfz's TOML config file.")
    ] = None,
) -> None:
    """Create and provision DigitalOcean droplets for coding agents.

    Every option here overrides the matching `GFZ_` environment variable and
    config file entry for the subcommand that follows it.
    """
    overrides = {
        "admin_user": admin_user,
        "agent_user": agent_user,
        "ssh_keys": ssh_keys,
        "ssh_identity": ssh_identity,
        "region": region,
        "size": size,
        "image": image,
        "tag": tag,
        "plugins": plugin or None,
        "config": config,
    }
    ctx.obj = {name: value for name, value in overrides.items() if value is not None}


def _aligned_table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    """Render `header` and `rows` as a table, columns padded to their widest cell."""
    all_rows = [tuple(header), *(tuple(row) for row in rows)]
    widths = [
        max(len(row[column]) for row in all_rows) for column in range(len(header))
    ]
    lines = [
        "  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True))
        for row in all_rows
    ]
    return "\n".join(line.rstrip() for line in lines)


def _settings_or_exit(ctx: typer.Context) -> Settings:
    """Resolve Settings from the parent callback's overrides.

    Raises:
        typer.Exit: Exit code 2, after printing the validation failure to
            stderr, when required configuration is missing or invalid.
    """
    try:
        return Settings(**ctx.obj)
    except ValidationError as error:
        typer.echo(configuration_error_message(error), err=True)
        raise typer.Exit(code=2) from error


def _plugin_list_or_exit(ctx: typer.Context) -> PluginList:
    """Resolve the plugin list alone from the parent callback's overrides.

    Raises:
        typer.Exit: Exit code 2, after printing the validation failure to
            stderr, when the plugin list or the config path is invalid.
    """
    try:
        return PluginList(**ctx.obj)
    except ValidationError as error:
        typer.echo(configuration_error_message(error), err=True)
        raise typer.Exit(code=2) from error


@contextmanager
def _reporting_failures() -> Iterator[None]:
    """Catch the failures this CLI reports, print each, and exit 1.

    These are the only failure types this CLI catches: every provider failure
    already arrives as a DigitalOceanError, every command refusal as a
    CommandError, every plugin it cannot use as a PluginError, and every step
    template that does not render as a RenderError. None prints a traceback.
    """
    try:
        yield
    except (
        commands.CommandError,
        DigitalOceanError,
        PluginError,
        RenderError,
    ) as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=1) from error


def _planned_run(settings: Settings) -> tuple[tuple[Plugin, ...], StepContexts]:
    """Load the configured plugins and resolve their settings.

    Returns the plugins in run order, and the context each step renders against.

    Raises:
        PluginError: A plugin fails to load, or its settings fail to resolve.
    """
    plugins = load_plugins(settings.plugins)
    contexts = provision.plugin_contexts(
        settings, plugins, os.environ, plugin_tables(settings.config)
    )
    return plugins, contexts


@config_app.command("show")
def show(ctx: typer.Context) -> None:
    """Print every resolved option with the source that supplied it."""
    settings = _settings_or_exit(ctx)
    with _reporting_failures():
        options = resolved_options(settings, ctx.obj)
    rows = [(option.name, option.value, option.source) for option in options]
    typer.echo(_aligned_table(("option", "value", "source"), rows))


LIST_HEADER = ("name", "id", "status", "region", "size", "address", "created")


def _list_row(host: Host) -> tuple[str, ...]:
    """Render one host as a row of the `list` table; a missing address is empty."""
    return (
        host.name,
        str(host.id),
        host.status,
        host.region,
        host.size,
        host.ip if host.ip is not None else "",
        host.created_at,
    )


def _host_line(host: Host) -> str:
    """Render a host's name, id, status, and address as one line."""
    address = host.ip if host.ip is not None else "no address"
    return f"{host.name} (id {host.id}): status {host.status}, address {address}"


def _power_line(host: Host, status: ActionStatus) -> str:
    """Render a host and the status a power action reported for it."""
    return f"{_host_line(host)}; action {status}"


def _echo_power_result(host: Host, status: ActionStatus) -> None:
    """Print what the host is now, and exit 1 if the provider reported it errored.

    The command has already waited for the host to reach the status the
    action leads to, so the line states that state and nothing about the
    action. Only `errored` fails the command, and its line names the action
    status that failed.
    """
    if status == "errored":
        typer.echo(_power_line(host, status), err=True)
        raise typer.Exit(code=1)
    typer.echo(_host_line(host))


def _destroyed_line(host: Host) -> str:
    """Render the confirmation that a droplet is gone."""
    return f"{host.name} (id {host.id}): destroyed"


def _destroy_summary(host: Host) -> str:
    """Render the name, id, region, and creation time of a droplet to destroy."""
    return (
        f"name: {host.name}\n"
        f"id: {host.id}\n"
        f"region: {host.region}\n"
        f"created: {host.created_at}"
    )


def _report_progress(line: str) -> None:
    """Write one progress line to stderr, keeping stdout the data channel."""
    typer.echo(line, err=True)


@app.command("list")
def list_hosts_command(
    ctx: typer.Context,
    all_hosts: Annotated[
        bool, typer.Option("--all", help="List every droplet in the account.")
    ] = False,
    tag: Annotated[
        str | None,
        typer.Option(
            "--tag",
            help="List droplets carrying this tag instead of the configured one.",
        ),
    ] = None,
) -> None:
    """List the droplets carrying the configured tag, or every droplet with --all."""
    settings = _settings_or_exit(ctx)
    with _reporting_failures():
        hosts = commands.list_hosts(
            DigitalOceanClient(), settings, all_hosts=all_hosts, tag=tag
        )
    rows = [_list_row(host) for host in hosts]
    typer.echo(_aligned_table(LIST_HEADER, rows))


@app.command("create")
def create_command(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Name for the new droplet.")],
    no_provision: Annotated[
        bool,
        typer.Option(
            "--no-provision", help="Create the droplet and provision nothing."
        ),
    ] = False,
) -> None:
    """Create a droplet carrying the configured tag, wait for ssh, then provision it.

    Every plugin loads, every plugin setting resolves, and every step renders
    first, so a plugin gfz cannot use creates no droplet.
    """
    settings = _settings_or_exit(ctx)
    with _reporting_failures():
        plugins, contexts = _planned_run(settings)
        rendered = provision.render_steps(provision.run_order(plugins), contexts)
        host = commands.create_host(
            DigitalOceanClient(), settings, name, report=_report_progress
        )
        typer.echo(_host_line(host))
        if not no_provision:
            _run_provisioning(settings, host, rendered)
            typer.echo(provision.closing_line("created", host, settings))


@app.command("destroy")
def destroy_command(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Name of the droplet to destroy.")],
    force: Annotated[
        bool,
        typer.Option(
            "--force", help="Destroy even if the droplet lacks the configured tag."
        ),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Skip the typed-name confirmation prompt."),
    ] = False,
) -> None:
    """Destroy a droplet, after confirming its name, and wait for it to go."""
    settings = _settings_or_exit(ctx)
    with _reporting_failures():
        client = DigitalOceanClient()
        host = commands.host_to_destroy(client, settings, name, force=force)
        typer.echo(_destroy_summary(host))
        typed_name = None if yes else typer.prompt("Type the droplet's name to confirm")
        commands.destroy_host(
            client, host, typed_name=typed_name, report=_report_progress
        )
        typer.echo(_destroyed_line(host))


@app.command("start")
def start_command(
    name: Annotated[str, typer.Argument(help="Name of the droplet to power on.")],
) -> None:
    """Power a droplet on, and wait for it to report active."""
    with _reporting_failures():
        host, status = commands.power_host(
            DigitalOceanClient(), name, "power_on", report=_report_progress
        )
    _echo_power_result(host, status)


@app.command("stop")
def stop_command(
    name: Annotated[str, typer.Argument(help="Name of the droplet to stop.")],
    hard: Annotated[
        bool,
        typer.Option(
            "--hard",
            help="Power off immediately instead of shutting down gracefully.",
        ),
    ] = False,
) -> None:
    """Shut a droplet down and wait for it to stop; --hard cuts power at once."""
    action: PowerAction = "power_off" if hard else "shutdown"
    with _reporting_failures():
        host, status = commands.power_host(
            DigitalOceanClient(), name, action, report=_report_progress
        )
    _echo_power_result(host, status)


@app.command("ssh")
def ssh_command_entry(
    ctx: typer.Context,
    name: Annotated[
        str, typer.Argument(help="Name of the droplet to open a shell on.")
    ],
    agent: Annotated[
        bool,
        typer.Option(
            "--agent",
            help="Open the shell as the agent user instead of the admin user.",
        ),
    ] = False,
) -> None:
    """Open an interactive ssh session on a droplet."""
    settings = _settings_or_exit(ctx)
    with _reporting_failures():
        exit_code = commands.open_shell(
            DigitalOceanClient(), settings, name, as_agent=agent
        )
    raise typer.Exit(code=exit_code)


STEPS_HEADER = ("step", "source", "login", "policy", "summary")
PLUGINS_HEADER = ("name", "summary")


def _steps_row(step: Step) -> tuple[str, ...]:
    """Render one step as a row of the `steps` table, naming its source."""
    policy = "mandatory" if step.mandatory else "optional"
    source = step.plugin if step.plugin is not None else "core"
    return (step.name, source, step.login.value, policy, step.summary)


def _selected_steps_or_exit(
    plugins: Sequence[Plugin],
    *,
    only: Sequence[str],
    start_from: str | None,
    skip: Sequence[str],
) -> tuple[Step, ...]:
    """Select the steps to run, exiting 2 on a selection mistake.

    A selection mistake is a usage error, not a run failure, so it exits 2
    beside the configuration errors rather than 1 beside the command
    failures.

    Raises:
        typer.Exit: Exit code 2, after printing the selection failure to
            stderr.
    """
    try:
        return provision.select_steps(
            plugins, only=only, start_from=start_from, skip=skip
        )
    except StepSelectionError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=2) from error


def _dry_run_output(rendered: Sequence[RenderedStep]) -> str:
    """Render every selected step's script under a header naming the step."""
    blocks = [
        f"# {item.step.name} ({item.step.login.value})\n{item.script}"
        for item in rendered
    ]
    return "\n".join(blocks)


def _plural(count: int, noun: str) -> str:
    """Render `count` with `noun`, adding an `s` for any count but one."""
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _provision_summary(result: ProvisionResult) -> str:
    """Render the end-of-run summary, naming every optional step that failed."""
    warned = [outcome for outcome in result.outcomes if outcome.status == "warned"]
    ran = _plural(len(result.outcomes), "step")
    if not warned:
        return f"{ran} ran, all succeeded"
    lines = [f"{ran} ran, {_plural(len(warned), 'optional step')} failed:"]
    lines += [f"  {outcome.name}: exit {outcome.exit_code}" for outcome in warned]
    return "\n".join(lines)


def _run_provisioning(
    settings: Settings, host: Host, rendered: Sequence[RenderedStep]
) -> None:
    """Run each rendered step on `host`, then print the result.

    Progress lines go to stderr, keeping stdout the data channel for the
    run summary this prints once it finishes.

    Raises:
        typer.Exit: Exit code 1, after printing the failing step's name and
            the remote stderr, when a mandatory step failed.
    """
    runner = remote.ssh_runner(settings, host)
    result = provision.provision_host(rendered, runner, _report_progress)

    if result.aborted_at is None:
        typer.echo(_provision_summary(result))
        return

    failure = result.outcomes[-1]
    typer.echo(f"provisioning stopped at {failure.name}; remote stderr:", err=True)
    typer.echo(failure.stderr, err=True)
    raise typer.Exit(code=1)


@app.command("steps")
def steps_command(ctx: typer.Context) -> None:
    """Print every core and plugin step in the order `provision` runs them.

    It resolves only the plugin list and loads each plugin's manifest, so it
    needs no other option and resolves no plugin setting.
    """
    plugin_list = _plugin_list_or_exit(ctx)
    with _reporting_failures():
        plugins = load_plugins(plugin_list.plugins)
    rows = [_steps_row(step) for step in provision.run_order(plugins)]
    typer.echo(_aligned_table(STEPS_HEADER, rows))


@app.command("plugins")
def plugins_command() -> None:
    """Print each plugin that ships with gfz, with its summary."""
    with _reporting_failures():
        builtins = load_plugins(builtin_names())
    rows = [(plugin.name, plugin.summary) for plugin in builtins]
    typer.echo(_aligned_table(PLUGINS_HEADER, rows))


@app.command("provision")
def provision_command(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Name of the droplet to provision.")],
    only: Annotated[
        list[str] | None,
        typer.Option(
            "--only",
            help="Run only this step, or this plugin's steps. Repeat for more.",
        ),
    ] = None,
    start_from: Annotated[
        str | None,
        typer.Option(
            "--from",
            help="Start at this step or plugin and run every step after it.",
        ),
    ] = None,
    skip: Annotated[
        list[str] | None,
        typer.Option(
            "--skip",
            help="Skip this step, or this plugin's steps. Repeat for more.",
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print the rendered scripts and run nothing."),
    ] = False,
) -> None:
    """Run the core and plugin steps on a droplet.

    Every plugin loads, every plugin setting resolves, and every selected step
    renders before gfz looks up the droplet, so a plugin gfz cannot use opens
    no ssh session.
    """
    settings = _settings_or_exit(ctx)
    with _reporting_failures():
        plugins, contexts = _planned_run(settings)
    steps = _selected_steps_or_exit(
        plugins, only=only or (), start_from=start_from, skip=skip or ()
    )
    with _reporting_failures():
        rendered = provision.render_steps(steps, contexts)
        host = commands.resolve_host(DigitalOceanClient(), name)
        # Refuse a host with no address before --dry-run prints any script.
        commands.host_address(host)
        if dry_run:
            typer.echo(_dry_run_output(rendered))
        else:
            _run_provisioning(settings, host, rendered)
            typer.echo(provision.closing_line("provisioned", host, settings))
