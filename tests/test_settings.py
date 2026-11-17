"""Tests for gfz's configuration surface: precedence, provenance, and errors."""

import pytest
from pydantic import ValidationError

from gfz.settings import (
    TOKEN_VARIABLE,
    Settings,
    configuration_error_message,
    resolved_options,
)


def test_flag_beats_environment_and_file_for_same_option(
    required_environment, monkeypatch, tmp_path
):
    """A flag-supplied value overrides the same option's environment and file values."""
    monkeypatch.setenv("GFZ_REGION", "env-region")
    config_file = tmp_path / "config.toml"
    config_file.write_text('region = "file-region"\n', encoding="utf-8")
    monkeypatch.setenv("GFZ_CONFIG", str(config_file))

    settings = Settings(region="flag-region")

    assert settings.region == "flag-region"


def test_environment_beats_file_and_default(
    required_environment, monkeypatch, tmp_path
):
    """An environment-supplied value overrides the same option's file and default."""
    monkeypatch.setenv("GFZ_REGION", "env-region")
    config_file = tmp_path / "config.toml"
    config_file.write_text('region = "file-region"\n', encoding="utf-8")
    monkeypatch.setenv("GFZ_CONFIG", str(config_file))

    settings = Settings()

    assert settings.region == "env-region"


def test_file_beats_default(required_environment, monkeypatch, tmp_path):
    """A file-supplied value overrides the option's field default."""
    config_file = tmp_path / "config.toml"
    config_file.write_text('region = "file-region"\n', encoding="utf-8")
    monkeypatch.setenv("GFZ_CONFIG", str(config_file))

    settings = Settings()

    assert settings.region == "file-region"


def test_unsupplied_optional_options_take_their_field_default(required_environment):
    """An option nothing supplies resolves to its declared field default."""
    settings = Settings()

    assert settings.region == "tor1"
    assert settings.size == "s-4vcpu-8gb"
    assert settings.image == "ubuntu-24-04-x64"
    assert settings.tag == "gfz"
    assert settings.repos == []


def test_missing_config_file_is_not_an_error(
    required_environment, monkeypatch, tmp_path
):
    """A GFZ_CONFIG path with no file there resolves via environment and defaults."""
    monkeypatch.setenv("GFZ_CONFIG", str(tmp_path / "does-not-exist.toml"))

    settings = Settings()

    assert settings.admin_user == "admin"
    assert settings.region == "tor1"


def test_gfz_config_relocates_the_file_read(
    required_environment, monkeypatch, tmp_path
):
    """GFZ_CONFIG changes which config file gfz reads."""
    config_file = tmp_path / "custom.toml"
    config_file.write_text('region = "relocated"\n', encoding="utf-8")
    monkeypatch.setenv("GFZ_CONFIG", str(config_file))

    settings = Settings()

    assert settings.region == "relocated"


def test_config_init_kwarg_beats_gfz_config(
    required_environment, monkeypatch, tmp_path
):
    """A --config-style init keyword argument beats GFZ_CONFIG for the file read."""
    env_config_file = tmp_path / "env.toml"
    env_config_file.write_text('region = "from-env-config"\n', encoding="utf-8")
    monkeypatch.setenv("GFZ_CONFIG", str(env_config_file))

    flag_config_file = tmp_path / "flag.toml"
    flag_config_file.write_text('region = "from-flag-config"\n', encoding="utf-8")

    settings = Settings(config=str(flag_config_file))

    assert settings.region == "from-flag-config"


def test_ssh_keys_env_comma_separated_resolves_to_list(
    required_environment, monkeypatch
):
    """GFZ_SSH_KEYS as a comma-separated string resolves to stripped values."""
    monkeypatch.setenv("GFZ_SSH_KEYS", "alpha,bravo")

    settings = Settings()

    assert settings.ssh_keys == ["alpha", "bravo"]


def test_ssh_keys_toml_array_resolves_to_same_list(
    required_environment, monkeypatch, tmp_path
):
    """A TOML array for ssh_keys resolves to the same list as a comma-separated form."""
    monkeypatch.delenv("GFZ_SSH_KEYS", raising=False)
    config_file = tmp_path / "config.toml"
    config_file.write_text('ssh_keys = ["alpha", "bravo"]\n', encoding="utf-8")
    monkeypatch.setenv("GFZ_CONFIG", str(config_file))

    settings = Settings()

    assert settings.ssh_keys == ["alpha", "bravo"]


def test_ssh_keys_init_kwarg_comma_separated_resolves_to_same_list(
    required_environment,
):
    """A comma-separated ssh_keys init keyword argument resolves to the same list."""
    settings = Settings(ssh_keys="alpha,bravo")

    assert settings.ssh_keys == ["alpha", "bravo"]


def test_repos_env_comma_separated_resolves_to_list(required_environment, monkeypatch):
    """GFZ_REPOS as a comma-separated string resolves to a list of stripped values."""
    monkeypatch.setenv("GFZ_REPOS", "repo-one, repo-two")

    settings = Settings()

    assert settings.repos == ["repo-one", "repo-two"]


def test_repos_toml_array_resolves_to_same_list(
    required_environment, monkeypatch, tmp_path
):
    """A TOML array for repos resolves to the same list as the comma-separated form."""
    config_file = tmp_path / "config.toml"
    config_file.write_text('repos = ["repo-one", "repo-two"]\n', encoding="utf-8")
    monkeypatch.setenv("GFZ_CONFIG", str(config_file))

    settings = Settings()

    assert settings.repos == ["repo-one", "repo-two"]


def test_repos_init_kwarg_comma_separated_resolves_to_same_list(required_environment):
    """A comma-separated repos init keyword argument resolves to the same list."""
    settings = Settings(repos="repo-one,repo-two")

    assert settings.repos == ["repo-one", "repo-two"]


def test_git_identity_options_resolve_from_the_environment(required_environment):
    """The committer name and email resolve from their GFZ_ environment variables."""
    settings = Settings()

    assert settings.git_name == "Agent Example"
    assert settings.git_email == "agent@example.com"


def test_missing_git_name_names_its_flag_variable_and_config_key(
    required_environment, monkeypatch
):
    """A missing committer name reports its flag, variable, and config key."""
    monkeypatch.delenv("GFZ_GIT_NAME")

    with pytest.raises(ValidationError) as excinfo:
        Settings()

    message = configuration_error_message(excinfo.value)

    assert "--git-name" in message
    assert "GFZ_GIT_NAME" in message
    assert "set git_name in the config file" in message


def test_empty_git_name_environment_variable_reads_as_unsupplied(
    required_environment, monkeypatch
):
    """An empty GFZ_GIT_NAME fails with the message an absent option gets."""
    monkeypatch.setenv("GFZ_GIT_NAME", "")

    with pytest.raises(ValidationError) as excinfo:
        Settings()

    assert configuration_error_message(excinfo.value).startswith(
        "git_name is required: pass --git-name, set GFZ_GIT_NAME"
    )


def test_missing_git_email_names_its_flag_variable_and_config_key(
    required_environment, monkeypatch
):
    """A missing committer email reports its flag, variable, and config key."""
    monkeypatch.delenv("GFZ_GIT_EMAIL")

    with pytest.raises(ValidationError) as excinfo:
        Settings()

    message = configuration_error_message(excinfo.value)

    assert "--git-email" in message
    assert "GFZ_GIT_EMAIL" in message
    assert "set git_email in the config file" in message


def test_agent_user_of_lowercase_letters_digits_and_hyphens_is_accepted(
    required_environment,
):
    """An agent user like agent-01 passes the user-name rule."""
    settings = Settings(agent_user="agent-01")

    assert settings.agent_user == "agent-01"


def test_agent_user_of_32_characters_is_accepted(required_environment):
    """A 32-character agent user fits the Linux user-name limit."""
    name = "a" * 32

    assert Settings(agent_user=name).agent_user == name


@pytest.mark.parametrize("name", ["Agent", "agent_01", "a" * 33])
def test_agent_user_outside_the_user_name_rule_is_refused(required_environment, name):
    """An uppercase, underscored, or 33-character agent user raises ValidationError."""
    with pytest.raises(ValidationError):
        Settings(agent_user=name)


def test_github_app_ids_resolve_from_the_environment_as_integers(
    required_environment,
):
    """The App ID and the installation ID resolve from GFZ_ variables as integers."""
    settings = Settings()

    assert settings.github_app_id == 123456
    assert settings.github_app_installation_id == 7654321


@pytest.mark.parametrize(
    ("field_name", "variable"),
    [
        ("github_app_id", "GFZ_GITHUB_APP_ID"),
        ("github_app_installation_id", "GFZ_GITHUB_APP_INSTALLATION_ID"),
    ],
)
def test_missing_github_app_id_names_its_flag_variable_and_config_key(
    required_environment, monkeypatch, field_name, variable
):
    """A missing App ID or installation ID is required, named by all three names."""
    monkeypatch.delenv(variable)

    with pytest.raises(ValidationError) as excinfo:
        Settings()

    flag = f"--{field_name.replace('_', '-')}"
    assert configuration_error_message(excinfo.value) == (
        f"{field_name} is required: pass {flag}, set {variable}, "
        f"or set {field_name} in the config file"
    )


@pytest.mark.parametrize("field_name", ["github_app_id", "github_app_installation_id"])
@pytest.mark.parametrize(
    ("value", "error_type"),
    [
        ("0", "greater_than"),
        ("-1", "greater_than"),
        ("abc", "int_parsing"),
        ("1.5", "int_parsing"),
    ],
)
def test_github_app_id_that_is_not_a_positive_integer_is_refused(
    required_environment, field_name, value, error_type
):
    """Zero, a negative, or a non-integer App ID or installation ID is refused."""
    with pytest.raises(ValidationError) as excinfo:
        Settings(**{field_name: value})

    assert [(error["loc"], error["type"]) for error in excinfo.value.errors()] == [
        ((field_name,), error_type)
    ]


def test_missing_required_option_raises_validation_error(
    required_environment_missing_admin_user,
):
    """A missing required option raises a pydantic ValidationError."""
    with pytest.raises(ValidationError):
        Settings()


def test_configuration_error_message_names_flag_variable_and_config_key(
    required_environment_missing_admin_user,
):
    """The message for a missing option names its flag, variable, and config key."""
    with pytest.raises(ValidationError) as excinfo:
        Settings()

    message = configuration_error_message(excinfo.value)

    assert "--admin-user" in message
    assert "GFZ_ADMIN_USER" in message
    assert "admin_user" in message


def test_empty_ssh_keys_environment_variable_reads_as_unsupplied(
    required_environment, monkeypatch
):
    """An empty GFZ_SSH_KEYS fails with the message an absent option gets."""
    monkeypatch.setenv("GFZ_SSH_KEYS", "")

    with pytest.raises(ValidationError) as excinfo:
        Settings()

    message = configuration_error_message(excinfo.value)

    assert "--ssh-keys" in message
    assert "GFZ_SSH_KEYS" in message
    assert "set ssh_keys in the config file" in message


def test_empty_ssh_keys_flag_reads_as_unsupplied(required_environment):
    """An empty --ssh-keys flag fails even where the environment supplies keys."""
    with pytest.raises(ValidationError) as excinfo:
        Settings(ssh_keys="")

    message = configuration_error_message(excinfo.value)

    assert "--ssh-keys" in message
    assert "GFZ_SSH_KEYS" in message
    assert "set ssh_keys in the config file" in message


def test_resolved_options_reports_the_supplying_source_for_each_field(
    monkeypatch, tmp_path
):
    """resolved_options reports flag, environment, file, and default per field."""
    monkeypatch.setenv("GFZ_AGENT_USER", "env-agent")
    monkeypatch.setenv("GFZ_SSH_KEYS", "key-one")
    monkeypatch.setenv("GFZ_SSH_IDENTITY", "/home/admin/.ssh/id_ed25519")
    monkeypatch.setenv("GFZ_HARNESS_REPO", "git@example.com:org/harness.git")
    monkeypatch.setenv("GFZ_GIT_NAME", "Agent Example")
    monkeypatch.setenv("GFZ_GIT_EMAIL", "agent@example.com")
    monkeypatch.setenv("GFZ_GITHUB_APP_ID", "123456")
    monkeypatch.setenv("GFZ_GITHUB_APP_INSTALLATION_ID", "7654321")

    config_file = tmp_path / "config.toml"
    config_file.write_text('region = "file-region"\n', encoding="utf-8")
    monkeypatch.setenv("GFZ_CONFIG", str(config_file))

    overrides = {"admin_user": "flag-admin"}
    settings = Settings(**overrides)

    options = resolved_options(settings, overrides)
    by_name = {option.name: option for option in options}

    assert by_name["admin_user"].source == "flag"
    assert by_name["agent_user"].source == "environment"
    assert by_name["region"].source == "file"
    assert by_name["size"].source == "default"


def test_token_row_reports_set_and_never_leaks_the_value(
    required_environment, monkeypatch
):
    """The token row reports 'set' for a present token, without leaking its value."""
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", "sekrit-token-value")

    settings = Settings()
    options = resolved_options(settings, {})

    token_option = next(option for option in options if option.name == TOKEN_VARIABLE)
    assert token_option.value == "set"
    assert token_option.source == "environment"
    for option in options:
        assert "sekrit-token-value" not in option.value
        assert "sekrit-token-value" not in option.name


def test_token_row_reports_not_set_when_absent(required_environment):
    """The token row reports 'not set' and source 'unset' when the token is absent."""
    settings = Settings()
    options = resolved_options(settings, {})

    token_option = next(option for option in options if option.name == TOKEN_VARIABLE)
    assert token_option.value == "not set"
    assert token_option.source == "unset"
