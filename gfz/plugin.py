"""Plugins: load a plugin directory, resolve plugin entries, resolve settings."""

import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from gfz.steps.registry import STEPS, Login, Step

BUILTIN_DIR = Path(__file__).parent / "plugins"
MANIFEST_NAME = "plugin.toml"

NAME_PATTERN = r"^[a-z0-9][a-z0-9-]{0,31}$"
# A setting reaches a template as `plugin.<setting>`, so its name must be a
# valid Jinja attribute name.
SettingName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]

SettingType = Literal["string", "integer", "list"]
SettingValue = str | int | list[str]
SettingSource = Literal["environment", "file", "default"]

_EXPECTED = {
    "string": "a string",
    "integer": "an integer",
    "list": "a list of strings",
}


class PluginError(Exception):
    """A plugin entry, manifest, or setting gfz cannot use."""


def split_comma_separated(text: str) -> list[str]:
    """Split a comma-separated string into stripped, non-empty parts.

    Every list gfz reads from an environment variable, core or plugin, splits
    this way.
    """
    return [part.strip() for part in text.split(",") if part.strip()]


def _has_type(value: object, setting_type: SettingType) -> bool:
    """Return whether `value` is a value of the declared setting type."""
    match setting_type:
        case "string":
            return isinstance(value, str)
        case "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        case "list":
            return isinstance(value, list) and all(
                isinstance(item, str) for item in value
            )


class SettingSpec(BaseModel):
    """One setting a manifest declares. A setting with no default is required."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    type: SettingType
    default: SettingValue | None = None

    @model_validator(mode="after")
    def _default_has_the_declared_type(self) -> Self:
        """Refuse a default that is not a value of the declared type."""
        if self.default is not None and not _has_type(self.default, self.type):
            raise ValueError(
                f"default must be {_EXPECTED[self.type]}, got {self.default!r}"
            )
        return self


class StepSpec(BaseModel):
    """One step a manifest declares."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    slug: Annotated[str, Field(pattern=NAME_PATTERN)]
    script: str
    summary: str
    login: Literal["root", "admin"] = "root"
    forward_agent: bool = False
    mandatory: bool = True

    @field_validator("script")
    @classmethod
    def _script_stays_relative(cls, script: str) -> str:
        """Refuse an absolute script path, or one with a `..` segment.

        Jinja's FileSystemLoader refuses a `..` segment at render time, so the
        manifest fails here instead, before any work starts.
        """
        path = PurePosixPath(script)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(
                "must be a path relative to the plugin directory with no '..',"
                f" got {script!r}"
            )
        return script


class Manifest(BaseModel):
    """A plugin's `plugin.toml`."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    name: Annotated[str, Field(pattern=NAME_PATTERN)]
    summary: str
    settings: dict[SettingName, SettingSpec] = Field(default_factory=dict)
    steps: list[StepSpec] = Field(default_factory=list)


@dataclass(frozen=True)
class Plugin:
    """A loaded plugin: its manifest's identity, settings, and steps.

    Attributes:
        name: The plugin's name, which prefixes its step names.
        summary: One line saying what the plugin does.
        directory: The plugin directory, which holds `plugin.toml` and the
            step templates.
        settings: Each declared setting, keyed by setting name.
        steps: The plugin's steps, in manifest order.
    """

    name: str
    summary: str
    directory: Path
    settings: Mapping[str, SettingSpec]
    steps: tuple[Step, ...]


@dataclass(frozen=True)
class ResolvedSettings:
    """A plugin's setting values and the source that supplied each.

    Both mappings are keyed by setting name.
    """

    values: dict[str, SettingValue]
    sources: dict[str, SettingSource]


def _manifest_error(manifest_path: Path, error: ValidationError) -> str:
    """Render a manifest ValidationError as one line per offending key."""
    lines = [f"{manifest_path}: invalid plugin manifest"]
    for detail in error.errors():
        location = ".".join(str(part) for part in detail["loc"])
        lines.append(f"  {location}: {detail['msg']}")
    return "\n".join(lines)


def _read_manifest(manifest_path: Path) -> Manifest:
    """Parse and validate a manifest.

    Raises:
        PluginError: The manifest is not TOML, or breaks a manifest rule.
    """
    try:
        with manifest_path.open("rb") as manifest_file:
            raw = tomllib.load(manifest_file)
    except tomllib.TOMLDecodeError as error:
        raise PluginError(f"{manifest_path}: not valid TOML: {error}") from error
    try:
        return Manifest.model_validate(raw)
    except ValidationError as error:
        raise PluginError(_manifest_error(manifest_path, error)) from error


def _plugin_step(
    spec: StepSpec, plugin_name: str, directory: Path, manifest_path: Path
) -> Step:
    """Build the Step a manifest step declares.

    Raises:
        PluginError: The step's script does not exist, or resolves outside
            the plugin directory.
    """
    script = directory / spec.script
    if not script.is_file():
        raise PluginError(
            f"{manifest_path}: step {spec.slug!r} names script {spec.script!r},"
            " which does not exist"
        )
    if not script.resolve().is_relative_to(directory.resolve()):
        raise PluginError(
            f"{manifest_path}: step {spec.slug!r} script {spec.script!r}"
            " resolves outside the plugin directory"
        )
    return Step(
        slug=spec.slug,
        login=Login(spec.login),
        forward_agent=spec.forward_agent,
        template=PurePosixPath(spec.script).as_posix(),
        summary=spec.summary,
        mandatory=spec.mandatory,
        plugin=plugin_name,
        directory=directory,
    )


def load_plugin(directory: Path) -> Plugin:
    """Load the plugin whose `plugin.toml` is in `directory`.

    Raises:
        PluginError: The manifest is not TOML, breaks a manifest rule, repeats
            a slug, or names a script that is missing or outside `directory`.
    """
    manifest_path = directory / MANIFEST_NAME
    manifest = _read_manifest(manifest_path)

    seen_slugs: set[str] = set()
    for spec in manifest.steps:
        if spec.slug in seen_slugs:
            raise PluginError(f"{manifest_path}: duplicate step slug {spec.slug!r}")
        seen_slugs.add(spec.slug)

    return Plugin(
        name=manifest.name,
        summary=manifest.summary,
        directory=directory,
        settings=dict(manifest.settings),
        steps=tuple(
            _plugin_step(spec, manifest.name, directory, manifest_path)
            for spec in manifest.steps
        ),
    )


def builtin_names() -> list[str]:
    """Return the sorted names of the plugins shipped inside the package."""
    return sorted(
        entry.name
        for entry in BUILTIN_DIR.iterdir()
        if (entry / MANIFEST_NAME).is_file()
    )


def resolve_entry(entry: str) -> Path:
    """Return the plugin directory a `plugins` entry names.

    An entry with no `/` and no leading `~` names a built-in plugin. An entry
    starting with `~` expands to a home directory. An absolute entry stands.

    Raises:
        PluginError: The entry names no built-in, is a relative path, names a
            home directory that does not exist, or names a path that is not a
            directory holding `plugin.toml`.
    """
    if entry.startswith("~"):
        try:
            directory = Path(entry).expanduser()
        except RuntimeError as error:
            raise PluginError(
                f"plugin path {entry!r} names a home directory that does not exist"
            ) from error
    elif "/" not in entry:
        names = builtin_names()
        if entry not in names:
            raise PluginError(
                f"unknown built-in plugin {entry!r}: built-in plugins are"
                f" {', '.join(names)}"
            )
        return BUILTIN_DIR / entry
    elif Path(entry).is_absolute():
        directory = Path(entry)
    else:
        raise PluginError(
            f"plugin path {entry!r} is relative:"
            " plugin paths must be absolute or start with ~"
        )

    if not (directory / MANIFEST_NAME).is_file():
        raise PluginError(
            f"plugin {entry!r}: {directory} is not a directory holding {MANIFEST_NAME}"
        )
    return directory


def load_plugins(entries: Sequence[str]) -> tuple[Plugin, ...]:
    """Resolve and load each `plugins` entry, in order.

    Raises:
        PluginError: An entry fails to resolve or load, two entries name one
            directory, two plugins share a name, a plugin's name is a core
            step's slug, which would make a bare name in `--only` ambiguous, or
            two plugin settings map to one environment variable.
    """
    core_slugs = {step.slug for step in STEPS}
    entries_by_directory: dict[Path, str] = {}
    directories_by_name: dict[str, Path] = {}
    plugins: list[Plugin] = []

    for entry in entries:
        directory = resolve_entry(entry)
        resolved = directory.resolve()
        if resolved in entries_by_directory:
            raise PluginError(
                f"plugin directory {resolved} is listed twice:"
                f" as {entries_by_directory[resolved]!r} and as {entry!r}"
            )
        entries_by_directory[resolved] = entry

        plugin = load_plugin(directory)
        if plugin.name in core_slugs:
            raise PluginError(
                f"{directory / MANIFEST_NAME}: plugin name {plugin.name!r} is a core"
                " step's slug; a plugin name must differ from every core step"
            )
        if plugin.name in directories_by_name:
            raise PluginError(
                f"two plugins are named {plugin.name!r}:"
                f" {directories_by_name[plugin.name]} and {directory}"
            )
        directories_by_name[plugin.name] = directory
        plugins.append(plugin)

    _refuse_shared_variables(plugins)
    return tuple(plugins)


def _refuse_shared_variables(plugins: Sequence[Plugin]) -> None:
    """Refuse two plugin settings that one environment variable would set.

    A hyphen in a name and an underscore in a setting both become `_` in the
    variable, so plugin `a` setting `b_c` and plugin `a-b` setting `c` both read
    `GFZ_PLUGIN_A_B_C`.

    Raises:
        PluginError: Two settings map to one variable.
    """
    owners: dict[str, str] = {}
    for plugin in plugins:
        for setting in plugin.settings:
            variable = setting_variable(plugin.name, setting)
            owner = f"{plugin.name}.{setting}"
            if variable in owners:
                raise PluginError(
                    f"{owners[variable]} and {owner} are both set by {variable};"
                    " rename a setting or a plugin so each variable means one thing"
                )
            owners[variable] = owner


def setting_variable(plugin_name: str, setting: str) -> str:
    """Return the environment variable that sets one plugin setting."""
    return f"GFZ_PLUGIN_{plugin_name.upper().replace('-', '_')}_{setting.upper()}"


def _config_key(plugin_name: str, setting: str) -> str:
    """Return the config file key that sets one plugin setting."""
    return f"[plugin.{plugin_name}] {setting}"


def _setting_label(plugin_name: str, setting: str) -> str:
    """Name a setting with both the variable and the config key that set it."""
    return (
        f"{plugin_name}.{setting} ({setting_variable(plugin_name, setting)},"
        f" {_config_key(plugin_name, setting)})"
    )


def _type_error(
    plugin_name: str,
    setting: str,
    setting_type: SettingType,
    value: object,
    origin: str,
) -> PluginError:
    """Build the error for a setting value that is not of its declared type."""
    return PluginError(
        f"{_setting_label(plugin_name, setting)}: must be {_EXPECTED[setting_type]},"
        f" got {value!r} from {origin}"
    )


def _from_environment(
    plugin_name: str, setting: str, setting_type: SettingType, raw: str
) -> SettingValue:
    """Parse a setting's environment variable as its declared type.

    Raises:
        PluginError: An integer setting's variable is not an integer.
    """
    match setting_type:
        case "string":
            return raw
        case "list":
            return split_comma_separated(raw)
        case "integer":
            try:
                return int(raw)
            except ValueError as error:
                raise _type_error(
                    plugin_name,
                    setting,
                    setting_type,
                    raw,
                    setting_variable(plugin_name, setting),
                ) from error


def resolve_settings(
    plugin: Plugin, environ: Mapping[str, str], table: Mapping[str, object]
) -> ResolvedSettings:
    """Resolve each of a plugin's settings: environment, then table, then default.

    `environ` is the process environment. `table` is this plugin's own
    `[plugin.<name>]` table, or an empty mapping when the config file has none.

    Raises:
        PluginError: The table holds a key that is no setting of the plugin,
            a value is not of its setting's type, or a required setting has no
            value.
    """
    for key in table:
        if key not in plugin.settings:
            known = ", ".join(sorted(plugin.settings)) or "none"
            raise PluginError(
                f"{_setting_label(plugin.name, key)}: plugin {plugin.name!r} has no"
                f" such setting; its settings are: {known}"
            )

    values: dict[str, SettingValue] = {}
    sources: dict[str, SettingSource] = {}
    for setting, spec in plugin.settings.items():
        variable = setting_variable(plugin.name, setting)
        if variable in environ:
            values[setting] = _from_environment(
                plugin.name, setting, spec.type, environ[variable]
            )
            sources[setting] = "environment"
        elif setting in table:
            value = table[setting]
            if not _has_type(value, spec.type):
                raise _type_error(
                    plugin.name, setting, spec.type, value, "the config file"
                )
            values[setting] = value
            sources[setting] = "file"
        elif spec.default is not None:
            values[setting] = spec.default
            sources[setting] = "default"
        else:
            raise PluginError(
                f"{plugin.name}.{setting} is required: set {variable},"
                f" or set {_config_key(plugin.name, setting)} in the config file"
            )
    return ResolvedSettings(values=values, sources=sources)
