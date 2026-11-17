"""The gfz configuration surface: sources, precedence, and provenance."""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, ValidationError, field_validator
from pydantic_settings import (
    BaseSettings,
    EnvSettingsSource,
    InitSettingsSource,
    NoDecode,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

DEFAULT_CONFIG_PATH = Path("~/.config/gfz/config.toml")
TOKEN_VARIABLE = "DIGITALOCEAN_TOKEN"  # noqa: S105 — an env var name, not a secret

OptionSource = Literal["flag", "environment", "file", "default", "unset"]

_UNSUPPLIED_ERROR_TYPES = frozenset({"missing", "too_short"})


def _config_path(*mappings: Mapping[str, object]) -> Path:
    """Return the config file to read: the flag, then GFZ_CONFIG, then the default."""
    for mapping in mappings:
        if "config" in mapping:
            return Path(str(mapping["config"])).expanduser()
    return DEFAULT_CONFIG_PATH.expanduser()


class _ConfigFileSource(TomlConfigSettingsSource):
    """The config file's values, less any attempt to relocate the config file.

    A file that set `config` would make `config show` report a path other than the
    one it read.
    """

    def __call__(self) -> dict[str, object]:
        """Return the file's values, with any `config` key removed."""
        values = super().__call__()
        values.pop("config", None)
        return values


class Settings(BaseSettings):
    """Resolved gfz configuration, sourced from flags, the environment, and a file."""

    model_config = SettingsConfigDict(env_prefix="GFZ_")

    admin_user: str
    # The check keeps the name inside the Linux user-name limit of 32
    # characters, and fails a bad name on the operator's machine, not mid-run
    # on the droplet.
    agent_user: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,31}$")]
    ssh_keys: Annotated[list[str], NoDecode, Field(min_length=1)]
    ssh_identity: Path
    region: str = "tor1"
    size: str = "s-4vcpu-8gb"
    image: str = "ubuntu-24-04-x64"
    tag: str = "gfz"
    config: Path = DEFAULT_CONFIG_PATH

    @field_validator("ssh_keys", mode="before")
    @classmethod
    def _split_comma_separated(cls, value: object) -> object:
        """Split a comma-separated string into stripped, non-empty parts.

        A TOML array arrives as a list already and passes through untouched.
        """
        if not isinstance(value, str):
            return value
        return [part.strip() for part in value.split(",") if part.strip()]

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type["Settings"],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Order sources as flag, then environment, then config file, then default."""
        config_path = _config_path(init_settings(), env_settings())
        return (
            init_settings,
            env_settings,
            _ConfigFileSource(settings_cls, toml_file=config_path),
        )


@dataclass(frozen=True)
class ResolvedOption:
    """One resolved configuration option and the source that supplied its value."""

    name: str
    value: str
    source: OptionSource


def _render_value(value: object) -> str:
    """Render a resolved field value the way `config show` prints it."""
    if isinstance(value, list):
        return ", ".join(value)
    return str(value)


def _field_sources(overrides: Mapping[str, object]) -> dict[str, OptionSource]:
    """Return the source that supplied each Settings field's value.

    A field present in none of the flag, environment, or file mappings took its
    default.
    """
    init_source = InitSettingsSource(Settings, init_kwargs=dict(overrides))
    env_source = EnvSettingsSource(Settings)
    init_values = init_source()
    env_values = env_source()
    file_values = _ConfigFileSource(
        Settings, toml_file=_config_path(init_values, env_values)
    )()

    ranked_mappings: list[tuple[OptionSource, Mapping[str, object]]] = [
        ("flag", init_values),
        ("environment", env_values),
        ("file", file_values),
    ]

    sources: dict[str, OptionSource] = {}
    for field_name in Settings.model_fields:
        sources[field_name] = next(
            (source for source, mapping in ranked_mappings if field_name in mapping),
            "default",
        )
    return sources


def resolved_options(
    settings: Settings, overrides: Mapping[str, object]
) -> list[ResolvedOption]:
    """Return one ResolvedOption per Settings field, then a token row, in that order.

    The token row reports only whether `DIGITALOCEAN_TOKEN` is set in the
    environment; the variable's actual value never appears in the returned data.
    """
    sources = _field_sources(overrides)
    options = [
        ResolvedOption(
            name=field_name,
            value=_render_value(getattr(settings, field_name)),
            source=sources[field_name],
        )
        for field_name in Settings.model_fields
    ]

    token_is_set = bool(os.environ.get(TOKEN_VARIABLE))
    options.append(
        ResolvedOption(
            name=TOKEN_VARIABLE,
            value="set" if token_is_set else "not set",
            source="environment" if token_is_set else "unset",
        )
    )
    return options


def configuration_error_message(error: ValidationError) -> str:
    """Render a Settings ValidationError as a message a human can act on.

    A required option is unsupplied when no source supplies it, and equally when
    the winning source supplies nothing: `GFZ_SSH_KEYS=""` resolves to no keys at
    all. Either way the message names that option's flag, its `GFZ_` environment
    variable, and its config key together, since this is the only place a human
    sees all three names for the same option.
    """
    lines = []
    for detail in error.errors():
        field_name = ".".join(str(part) for part in detail["loc"])
        if detail["type"] in _UNSUPPLIED_ERROR_TYPES:
            flag = f"--{field_name.replace('_', '-')}"
            variable = f"GFZ_{field_name.upper()}"
            lines.append(
                f"{field_name} is required: pass {flag}, set {variable}, "
                f"or set {field_name} in the config file"
            )
        else:
            lines.append(f"{field_name}: {detail['msg']}")
    return "\n".join(lines)
