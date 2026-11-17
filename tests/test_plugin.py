"""Tests for the plugin loader: manifests, entries, and plugin settings."""

from pathlib import Path

import pytest

from gfz.plugin import (
    BUILTIN_DIR,
    PluginError,
    ResolvedSettings,
    builtin_names,
    load_plugin,
    load_plugins,
    resolve_entry,
    resolve_settings,
    setting_variable,
)
from gfz.steps.registry import Login

EXAMPLE_DIR = Path(__file__).parent.parent / "examples" / "hello"

MINIMAL_MANIFEST = """\
name = "probe"
summary = "A plugin a test invented."

[[steps]]
slug = "one"
script = "one.sh.j2"
summary = "The first step."
"""


def _write_plugin(directory, manifest, scripts=("one.sh.j2",)):
    """Write a plugin directory holding `manifest` and an empty file per script."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "plugin.toml").write_text(manifest, encoding="utf-8")
    for script in scripts:
        path = directory / script
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("set -euo pipefail\n", encoding="utf-8")
    return directory


def _settings_plugin(tmp_path, settings_toml):
    """Load a plugin named `probe` with no steps and the given settings tables."""
    manifest = f'name = "probe"\nsummary = "Settings."\n{settings_toml}'
    return load_plugin(_write_plugin(tmp_path / "probe", manifest, scripts=()))


class TestLoadPlugin:
    """Tests for load_plugin."""

    def test_the_hello_example_loads_into_a_plugin_record(self):
        """The example's manifest becomes a Plugin with one named, rooted step."""
        plugin = load_plugin(EXAMPLE_DIR)

        assert plugin.name == "hello"
        assert plugin.summary == "Write a greeting into the agent's home."
        assert plugin.directory == EXAMPLE_DIR
        assert plugin.settings["greeting"].type == "string"
        assert plugin.settings["greeting"].default == "hello"
        (step,) = plugin.steps
        assert step.name == "hello/greet"
        assert step.plugin == "hello"
        assert step.directory == EXAMPLE_DIR
        assert step.template == "greet.sh.j2"
        assert step.summary == "Write the greeting to ~/hello.txt."

    def test_a_step_takes_the_documented_defaults(self, tmp_path):
        """A step naming only slug, script, and summary runs as root, mandatory."""
        (step,) = load_plugin(_write_plugin(tmp_path, MINIMAL_MANIFEST)).steps

        assert step.login is Login.ROOT
        assert step.forward_agent is False
        assert step.mandatory is True

    def test_a_step_takes_the_policy_its_manifest_states(self, tmp_path):
        """login, forward_agent, and mandatory carry over from the manifest."""
        manifest = (
            MINIMAL_MANIFEST
            + 'login = "admin"\nforward_agent = true\nmandatory = false\n'
        )

        (step,) = load_plugin(_write_plugin(tmp_path, manifest)).steps

        assert step.login is Login.ADMIN
        assert step.forward_agent is True
        assert step.mandatory is False

    def test_steps_keep_their_manifest_order(self, tmp_path):
        """Steps appear in the order the manifest lists them."""
        manifest = (
            MINIMAL_MANIFEST
            + '[[steps]]\nslug = "two"\nscript = "two.sh.j2"\nsummary = "Second."\n'
        )

        plugin = load_plugin(
            _write_plugin(tmp_path, manifest, scripts=("one.sh.j2", "two.sh.j2"))
        )

        assert [step.slug for step in plugin.steps] == ["one", "two"]

    def test_a_script_in_a_subdirectory_keeps_its_relative_posix_path(self, tmp_path):
        """A script under a subdirectory is the template path relative to the plugin."""
        manifest = MINIMAL_MANIFEST.replace("one.sh.j2", "./scripts/one.sh.j2")

        (step,) = load_plugin(
            _write_plugin(tmp_path, manifest, scripts=("scripts/one.sh.j2",))
        ).steps

        assert step.template == "scripts/one.sh.j2"

    def test_a_plugin_with_no_steps_is_valid(self, tmp_path):
        """A manifest with no steps loads into a plugin with no steps."""
        manifest = 'name = "probe"\nsummary = "Nothing to run."\n'

        plugin = load_plugin(_write_plugin(tmp_path, manifest, scripts=()))

        assert plugin.steps == ()

    def test_a_setting_with_no_default_is_required(self, tmp_path):
        """A setting with no default loads with no default."""
        plugin = _settings_plugin(tmp_path, '[settings.port]\ntype = "integer"\n')

        assert plugin.settings["port"].default is None

    @pytest.mark.parametrize(
        ("manifest", "fragment"),
        [
            ('summary = "s"\n', "name"),
            ('name = "probe"\n', "summary"),
            (
                'name = "probe"\nsummary = "s"\n'
                '[[steps]]\nscript = "one.sh.j2"\nsummary = "s"\n',
                "steps.0.slug",
            ),
            (
                'name = "probe"\nsummary = "s"\n'
                '[[steps]]\nslug = "one"\nsummary = "s"\n',
                "steps.0.script",
            ),
            (
                'name = "probe"\nsummary = "s"\n'
                '[[steps]]\nslug = "one"\nscript = "one.sh.j2"\n',
                "steps.0.summary",
            ),
        ],
        ids=["name", "summary", "slug", "script", "step-summary"],
    )
    def test_a_missing_required_key_is_refused(self, tmp_path, manifest, fragment):
        """A manifest lacking a required key raises PluginError naming the key."""
        directory = _write_plugin(tmp_path, manifest)

        with pytest.raises(PluginError) as excinfo:
            load_plugin(directory)

        assert fragment in str(excinfo.value)
        assert str(directory / "plugin.toml") in str(excinfo.value)

    @pytest.mark.parametrize(
        ("old", "new", "fragment"),
        [
            ('name = "probe"', 'name = "Probe"', "name"),
            ('name = "probe"', 'name = "-probe"', "name"),
            ('name = "probe"', f'name = "{"p" * 33}"', "name"),
            ('name = "probe"', 'name = "pro_be"', "name"),
            ('slug = "one"', 'slug = "One"', "steps.0.slug"),
            ('slug = "one"', 'slug = "on/e"', "steps.0.slug"),
        ],
        ids=["upper", "leading-hyphen", "too-long", "underscore", "slug", "slash"],
    )
    def test_a_name_or_slug_outside_the_name_rule_is_refused(
        self, tmp_path, old, new, fragment
    ):
        """A name or slug that breaks ^[a-z0-9][a-z0-9-]{0,31}$ raises PluginError."""
        directory = _write_plugin(tmp_path, MINIMAL_MANIFEST.replace(old, new))

        with pytest.raises(PluginError) as excinfo:
            load_plugin(directory)

        assert fragment in str(excinfo.value)

    def test_a_32_character_name_is_accepted(self, tmp_path):
        """A 32-character name fits the name rule."""
        name = "p" * 32
        manifest = MINIMAL_MANIFEST.replace('name = "probe"', f'name = "{name}"')

        assert load_plugin(_write_plugin(tmp_path, manifest)).name == name

    @pytest.mark.parametrize(
        ("manifest", "fragment"),
        [
            ('colour = "red"\n' + MINIMAL_MANIFEST, "colour"),
            (MINIMAL_MANIFEST + "retries = 3\n", "steps.0.retries"),
            (
                'name = "probe"\nsummary = "s"\n'
                '[settings.greeting]\ntype = "string"\nsecret = true\n',
                "settings.greeting.secret",
            ),
        ],
        ids=["top-level", "step", "setting"],
    )
    def test_an_unknown_key_at_any_level_is_refused(self, tmp_path, manifest, fragment):
        """An unknown key raises PluginError naming the key and the manifest."""
        directory = _write_plugin(tmp_path, manifest)

        with pytest.raises(PluginError) as excinfo:
            load_plugin(directory)

        assert fragment in str(excinfo.value)
        assert str(directory / "plugin.toml") in str(excinfo.value)

    @pytest.mark.parametrize(
        ("field", "value"),
        [("login", '"user"'), ("forward_agent", '"yes"'), ("mandatory", "1")],
    )
    def test_a_step_policy_value_of_the_wrong_kind_is_refused(
        self, tmp_path, field, value
    ):
        """A login other than root or admin, or a non-boolean flag, raises."""
        directory = _write_plugin(tmp_path, MINIMAL_MANIFEST + f"{field} = {value}\n")

        with pytest.raises(PluginError) as excinfo:
            load_plugin(directory)

        assert f"steps.0.{field}" in str(excinfo.value)

    @pytest.mark.parametrize(
        "settings_toml",
        [
            '[settings.greeting]\ntype = "float"\n',
            '[settings.greeting]\ndefault = "hi"\n',
            '[settings.greeting]\ntype = "string"\ndefault = 5\n',
            '[settings.greeting]\ntype = "integer"\ndefault = "5"\n',
            '[settings.greeting]\ntype = "integer"\ndefault = true\n',
            '[settings.greeting]\ntype = "list"\ndefault = "a,b"\n',
            '[settings.greeting]\ntype = "list"\ndefault = ["a", 5]\n',
        ],
        ids=[
            "unknown-type",
            "no-type",
            "string-given-integer",
            "integer-given-string",
            "integer-given-boolean",
            "list-given-string",
            "list-given-integer-item",
        ],
    )
    def test_a_setting_type_or_default_that_does_not_fit_is_refused(
        self, tmp_path, settings_toml
    ):
        """A type outside string, integer, list, or a mismatched default, raises."""
        with pytest.raises(PluginError) as excinfo:
            _settings_plugin(tmp_path, settings_toml)

        assert "settings.greeting" in str(excinfo.value)

    @pytest.mark.parametrize("setting", ["Greeting", "1greeting", "greet-ing"])
    def test_a_setting_name_that_is_not_a_template_attribute_is_refused(
        self, tmp_path, setting
    ):
        """A setting name outside ^[a-z][a-z0-9_]*$ raises PluginError."""
        with pytest.raises(PluginError) as excinfo:
            _settings_plugin(tmp_path, f'[settings.{setting}]\ntype = "string"\n')

        assert setting in str(excinfo.value)

    @pytest.mark.parametrize(
        "script", ["/etc/passwd", "../outside.sh.j2", "sub/../../outside.sh.j2"]
    )
    def test_a_script_that_is_absolute_or_climbs_out_is_refused(self, tmp_path, script):
        """An absolute script path, or one with a .. segment, raises at load."""
        manifest = MINIMAL_MANIFEST.replace("one.sh.j2", script)
        directory = _write_plugin(tmp_path / "probe", manifest, scripts=())

        with pytest.raises(PluginError) as excinfo:
            load_plugin(directory)

        assert "steps.0.script" in str(excinfo.value)

    def test_a_script_that_resolves_outside_the_directory_is_refused(self, tmp_path):
        """A script that is a symlink out of the plugin directory raises."""
        outside = tmp_path / "outside.sh.j2"
        outside.write_text("set -euo pipefail\n", encoding="utf-8")
        directory = _write_plugin(tmp_path / "probe", MINIMAL_MANIFEST, scripts=())
        (directory / "one.sh.j2").symlink_to(outside)

        with pytest.raises(PluginError) as excinfo:
            load_plugin(directory)

        assert "one.sh.j2" in str(excinfo.value)
        assert "outside the plugin directory" in str(excinfo.value)

    def test_a_missing_script_is_refused(self, tmp_path):
        """A step whose script file does not exist raises PluginError naming it."""
        directory = _write_plugin(tmp_path, MINIMAL_MANIFEST, scripts=())

        with pytest.raises(PluginError) as excinfo:
            load_plugin(directory)

        assert "one.sh.j2" in str(excinfo.value)
        assert str(directory / "plugin.toml") in str(excinfo.value)

    def test_duplicate_slugs_are_refused(self, tmp_path):
        """Two steps with one slug raise PluginError naming the slug."""
        manifest = (
            MINIMAL_MANIFEST
            + '[[steps]]\nslug = "one"\nscript = "one.sh.j2"\nsummary = "Again."\n'
        )
        directory = _write_plugin(tmp_path, manifest)

        with pytest.raises(PluginError) as excinfo:
            load_plugin(directory)

        assert "'one'" in str(excinfo.value)
        assert str(directory / "plugin.toml") in str(excinfo.value)

    def test_a_manifest_that_is_not_toml_is_refused(self, tmp_path):
        """A manifest TOML cannot parse raises PluginError naming the manifest."""
        directory = _write_plugin(tmp_path, 'name = "probe\n')

        with pytest.raises(PluginError) as excinfo:
            load_plugin(directory)

        assert str(directory / "plugin.toml") in str(excinfo.value)


class TestBuiltins:
    """Tests for the plugins shipped inside the package."""

    def test_claude_is_the_one_built_in(self):
        """builtin_names lists the claude plugin."""
        assert builtin_names() == ["claude"]

    def test_the_claude_plugin_installs_for_the_agent_then_optionally_the_admin(self):
        """The claude plugin's agent step is mandatory, its admin step optional."""
        plugin = load_plugin(BUILTIN_DIR / "claude")

        assert plugin.settings == {}
        assert [(step.name, step.mandatory) for step in plugin.steps] == [
            ("claude/agent", True),
            ("claude/admin", False),
        ]
        for step in plugin.steps:
            assert step.login is Login.ROOT
            assert step.forward_agent is False


class TestResolveEntry:
    """Tests for resolve_entry."""

    def test_a_built_in_name_resolves_to_the_packaged_directory(self):
        """An entry with no slash and no tilde names a built-in plugin."""
        assert resolve_entry("claude") == BUILTIN_DIR / "claude"

    def test_an_unknown_built_in_name_is_refused_listing_the_built_ins(self):
        """An unknown name raises PluginError naming it and every built-in."""
        with pytest.raises(PluginError) as excinfo:
            resolve_entry("codex")

        assert "'codex'" in str(excinfo.value)
        assert "claude" in str(excinfo.value)

    def test_an_absolute_path_stands(self, tmp_path):
        """An absolute entry resolves to that directory."""
        directory = _write_plugin(tmp_path / "probe", MINIMAL_MANIFEST)

        assert resolve_entry(str(directory)) == directory

    def test_a_tilde_path_expands_to_the_home_directory(self):
        """An entry starting with ~ expands against HOME."""
        directory = _write_plugin(Path.home() / "plugins" / "probe", MINIMAL_MANIFEST)

        assert resolve_entry("~/plugins/probe") == directory

    def test_a_tilde_path_naming_an_unknown_user_is_refused(self):
        """An entry like ~nobody/x whose user has no home raises PluginError."""
        entry = "~gfz-no-such-user/plugins/probe"

        with pytest.raises(PluginError) as excinfo:
            resolve_entry(entry)

        assert repr(entry) in str(excinfo.value)

    @pytest.mark.parametrize("entry", ["plugins/probe", "./probe", "probe/"])
    def test_a_relative_path_is_refused(self, entry):
        """An entry with a slash that is neither absolute nor ~ raises PluginError."""
        with pytest.raises(PluginError) as excinfo:
            resolve_entry(entry)

        assert repr(entry) in str(excinfo.value)
        assert "plugin paths must be absolute or start with ~" in str(excinfo.value)

    @pytest.mark.parametrize("make", [False, True], ids=["missing", "no-manifest"])
    def test_a_path_with_no_manifest_is_refused(self, tmp_path, make):
        """A path that is not a directory holding plugin.toml raises PluginError."""
        directory = tmp_path / "probe"
        if make:
            directory.mkdir()

        with pytest.raises(PluginError) as excinfo:
            resolve_entry(str(directory))

        assert str(directory) in str(excinfo.value)
        assert "plugin.toml" in str(excinfo.value)


class TestLoadPlugins:
    """Tests for load_plugins."""

    def test_plugins_load_in_entry_order(self, tmp_path):
        """Each entry loads into a Plugin, in the order the entries list them."""
        plugins = load_plugins(["claude", str(EXAMPLE_DIR)])

        assert [plugin.name for plugin in plugins] == ["claude", "hello"]

    def test_no_entries_load_no_plugins(self):
        """An empty plugin list means core only."""
        assert load_plugins([]) == ()

    def test_the_same_directory_listed_twice_is_refused(self, tmp_path):
        """Two entries resolving to one directory raise PluginError."""
        directory = _write_plugin(tmp_path / "probe", MINIMAL_MANIFEST)
        (tmp_path / "alias").symlink_to(directory)

        with pytest.raises(PluginError) as excinfo:
            load_plugins([str(directory), str(tmp_path / "alias")])

        assert "listed twice" in str(excinfo.value)
        assert str(directory) in str(excinfo.value)

    def test_two_plugins_with_one_name_are_refused(self, tmp_path):
        """Two directories whose manifests share a name raise PluginError."""
        first = _write_plugin(tmp_path / "first", MINIMAL_MANIFEST)
        second = _write_plugin(tmp_path / "second", MINIMAL_MANIFEST)

        with pytest.raises(PluginError) as excinfo:
            load_plugins([str(first), str(second)])

        assert "'probe'" in str(excinfo.value)
        assert str(first) in str(excinfo.value)
        assert str(second) in str(excinfo.value)

    def test_a_plugin_named_after_a_core_step_is_refused(self, tmp_path):
        """A plugin whose name is a core step's slug raises PluginError."""
        manifest = MINIMAL_MANIFEST.replace('name = "probe"', 'name = "github-cli"')
        directory = _write_plugin(tmp_path / "probe", manifest)

        with pytest.raises(PluginError) as excinfo:
            load_plugins([str(directory)])

        assert "'github-cli'" in str(excinfo.value)
        assert "core step" in str(excinfo.value)


@pytest.mark.parametrize(
    ("plugin", "setting", "variable"),
    [
        ("hello", "greeting", "GFZ_PLUGIN_HELLO_GREETING"),
        ("my-plugin", "max_count", "GFZ_PLUGIN_MY_PLUGIN_MAX_COUNT"),
    ],
)
def test_setting_variable_uppercases_and_turns_hyphens_into_underscores(
    plugin, setting, variable
):
    """setting_variable builds GFZ_PLUGIN_<NAME>_<SETTING>."""
    assert setting_variable(plugin, setting) == variable


class TestResolveSettings:
    """Tests for resolve_settings."""

    SETTINGS = (
        '[settings.greeting]\ntype = "string"\ndefault = "hello"\n'
        '[settings.port]\ntype = "integer"\ndefault = 8080\n'
        '[settings.names]\ntype = "list"\ndefault = ["ada"]\n'
    )

    def test_every_setting_takes_its_default_when_nothing_supplies_it(self, tmp_path):
        """With no variable and no table, each setting is its manifest default."""
        plugin = _settings_plugin(tmp_path, self.SETTINGS)

        resolved = resolve_settings(plugin, {}, {})

        assert resolved == ResolvedSettings(
            values={"greeting": "hello", "port": 8080, "names": ["ada"]},
            sources={"greeting": "default", "port": "default", "names": "default"},
        )

    def test_the_table_beats_the_default(self, tmp_path):
        """A [plugin.<name>] value overrides the default, with source 'file'."""
        plugin = _settings_plugin(tmp_path, self.SETTINGS)

        resolved = resolve_settings(plugin, {}, {"greeting": "hi", "port": 22})

        assert resolved.values["greeting"] == "hi"
        assert resolved.values["port"] == 22
        assert resolved.sources["greeting"] == "file"

    def test_the_environment_beats_the_table(self, tmp_path):
        """A GFZ_PLUGIN_ variable overrides the table, with source 'environment'."""
        plugin = _settings_plugin(tmp_path, self.SETTINGS)

        resolved = resolve_settings(
            plugin, {"GFZ_PLUGIN_PROBE_GREETING": "hey"}, {"greeting": "hi"}
        )

        assert resolved.values["greeting"] == "hey"
        assert resolved.sources["greeting"] == "environment"

    def test_a_list_from_the_environment_is_comma_separated(self, tmp_path):
        """A list variable splits on commas into stripped, non-empty items."""
        plugin = _settings_plugin(tmp_path, self.SETTINGS)

        resolved = resolve_settings(plugin, {"GFZ_PLUGIN_PROBE_NAMES": "ada, bo,,"}, {})

        assert resolved.values["names"] == ["ada", "bo"]

    def test_an_integer_from_the_environment_is_parsed(self, tmp_path):
        """An integer variable resolves to an int."""
        plugin = _settings_plugin(tmp_path, self.SETTINGS)

        resolved = resolve_settings(plugin, {"GFZ_PLUGIN_PROBE_PORT": "2222"}, {})

        assert resolved.values["port"] == 2222

    def test_an_integer_variable_that_is_not_a_number_is_refused(self, tmp_path):
        """A non-numeric integer variable raises naming setting, variable, and key."""
        plugin = _settings_plugin(tmp_path, self.SETTINGS)

        with pytest.raises(PluginError) as excinfo:
            resolve_settings(plugin, {"GFZ_PLUGIN_PROBE_PORT": "ssh"}, {})

        message = str(excinfo.value)
        assert "probe.port" in message
        assert "GFZ_PLUGIN_PROBE_PORT" in message
        assert "[plugin.probe] port" in message
        assert "'ssh'" in message

    @pytest.mark.parametrize(
        ("setting", "value"),
        [
            ("greeting", 5),
            ("port", "8080"),
            ("port", True),
            ("names", "ada"),
            ("names", ["ada", 5]),
        ],
        ids=[
            "string-given-integer",
            "integer-given-string",
            "integer-given-boolean",
            "list-given-string",
            "list-given-integer-item",
        ],
    )
    def test_a_table_value_of_the_wrong_type_is_refused(self, tmp_path, setting, value):
        """A table value not of the declared type raises naming all three names."""
        plugin = _settings_plugin(tmp_path, self.SETTINGS)

        with pytest.raises(PluginError) as excinfo:
            resolve_settings(plugin, {}, {setting: value})

        message = str(excinfo.value)
        assert f"probe.{setting}" in message
        assert f"GFZ_PLUGIN_PROBE_{setting.upper()}" in message
        assert f"[plugin.probe] {setting}" in message

    def test_an_unknown_table_key_is_refused(self, tmp_path):
        """A table key that is no setting of the plugin raises naming it."""
        plugin = _settings_plugin(tmp_path, self.SETTINGS)

        with pytest.raises(PluginError) as excinfo:
            resolve_settings(plugin, {}, {"greting": "hi"})

        message = str(excinfo.value)
        assert "probe.greting" in message
        assert "GFZ_PLUGIN_PROBE_GRETING" in message
        assert "[plugin.probe] greting" in message

    def test_a_required_setting_with_no_value_is_refused(self, tmp_path):
        """A setting with no default and no value raises naming variable and key."""
        plugin = _settings_plugin(tmp_path, '[settings.token_path]\ntype = "string"\n')

        with pytest.raises(PluginError) as excinfo:
            resolve_settings(plugin, {}, {})

        message = str(excinfo.value)
        assert "probe.token_path is required" in message
        assert "GFZ_PLUGIN_PROBE_TOKEN_PATH" in message
        assert "[plugin.probe] token_path" in message

    def test_a_required_setting_is_satisfied_by_the_table(self, tmp_path):
        """A required setting the table supplies resolves with source 'file'."""
        plugin = _settings_plugin(tmp_path, '[settings.token_path]\ntype = "string"\n')

        resolved = resolve_settings(plugin, {}, {"token_path": "/run/token"})

        assert resolved.values == {"token_path": "/run/token"}
        assert resolved.sources == {"token_path": "file"}
