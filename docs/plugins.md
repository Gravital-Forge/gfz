# Writing gfz plugins

A plugin adds setup steps to `gfz`. Its steps run after core provisioning, through the same engine,
so `gfz steps`, `gfz provision`, and `gfz create` treat them exactly as they treat core steps.

A plugin is a directory. It holds a `plugin.toml` manifest and the Jinja templates the manifest
names. Each template renders to a shell script, and `gfz` runs that script on the droplet over ssh.

The `README.md` covers installing and configuring `gfz` itself. This document covers what a plugin
author needs: the manifest, the templates, plugin settings, how steps are named and selected, and a
walkthrough of the example plugin in `examples/hello`.

## The manifest

This is the `examples/hello` manifest. It sets every key a manifest can hold:

```toml
name = "hello"
summary = "Write a greeting into the agent's home."

[settings.greeting]
type = "string"
default = "hello"

[[steps]]
slug = "greet"
script = "greet.sh.j2"
summary = "Write the greeting to ~/hello.txt."
login = "root"
forward_agent = false
mandatory = true
```

### Top-level keys

- `name`: required. The plugin's name. It prefixes every step name and every setting variable, so it
  is the name you type on the command line. It need not match the directory's name.
- `summary`: required. One line saying what the plugin does.
- `settings`: optional. One `[settings.<setting>]` table per setting the plugin reads. See
  [Settings](#settings).
- `steps`: optional. One `[[steps]]` table per step, in run order. A plugin with no steps is valid.

### Step keys

- `slug`: required. The step's name within the plugin.
- `script`: required. The template's path, relative to the plugin directory.
- `summary`: required. One line saying what the step does. `gfz steps` prints it.
- `login`: optional. The account the script runs as: `"root"` or `"admin"`. Defaults to `"root"`.
- `forward_agent`: optional. Whether the ssh session forwards your local ssh agent. Defaults to
  `false`.
- `mandatory`: optional. Whether a failure stops the run. Defaults to `true`.

### Setting keys

- `type`: required. One of `"string"`, `"integer"`, or `"list"`. A list holds strings.
- `default`: optional. The value when no other source sets one. A setting with no `default` is
  required.

### Manifest rules

`gfz` refuses a manifest that breaks any of these rules. The error names the manifest and the key at
fault.

- `name` and every `slug` start with a lowercase letter or a digit, and hold only lowercase letters,
  digits, and hyphens, up to 32 characters in all.
- A setting's name starts with a lowercase letter, and holds only lowercase letters, digits, and
  underscores. A template reads it as `plugin.<setting>`, so it must be a valid attribute name.
- Every value has exactly its declared type. `gfz` converts nothing: `mandatory = "true"` is an
  error, and so is an `integer` setting with `default = "8080"`.
- A key the format does not define is an error, at any level.
- Two steps with the same `slug` are an error.
- `script` is relative, holds no `..` segment, names a file that exists, and resolves inside the
  plugin directory, symbolic links included.

## Templates

A template is a Jinja template that renders to a bash script. `gfz` renders every template with
these Jinja settings:

- An undefined value is an error, never a silent blank.
- `trim_blocks` and `lstrip_blocks` are on, so a line holding only a `{% %}` tag leaves no blank
  line and no indentation behind.
- The template's trailing newline is kept.
- Nothing is HTML-escaped. The output is shell, not HTML.

### Template values

Every template sees the values core provisioning uses:

- `admin_user`: the admin account's name.
- `admin_home`: the admin account's home, `/home/<admin_user>`.
- `agent_user`: the agent account's name.
- `agent_home`: the agent account's home, `/home/<agent_user>`.

A plugin template also sees `plugin`, which holds the plugin's resolved settings. Read a setting as
an attribute: `{{ plugin.greeting }}`. Reading a setting the manifest does not declare is an error.

### Quoting

The `shell_quote` filter turns a value into text and quotes it for the shell:
`{{ plugin.greeting | shell_quote }}`. Use it on every setting value that lands in a command line.
It quotes an `integer` setting too.

A `list` setting is a list, so quoting it whole gives you nothing useful. Iterate over it and quote
each item:

```jinja
{% include "_apt.j2" %}
apt_get install -y{% for name in plugin.names %} {{ name | shell_quote }}{% endfor %}
```

[Includes](#includes) says where `_apt.j2` comes from and what `apt_get` does.

### Includes

`{% include %}` looks in the plugin directory first, then in core's step directory. So a plugin can
include core's `_apt.j2` partial. It sets `DEBIAN_FRONTEND=noninteractive` and defines `apt_get`, an
`apt-get` that waits up to ten minutes for the package lock instead of failing on it. A fresh
droplet's first-boot upgrade holds that lock, so every step that calls apt needs the wait:

```jinja
{% include "_apt.j2" %}
apt_get update
```

A file in the plugin directory shadows a core file of the same name.

### How a step runs

`gfz` opens one ssh session per step, as `root` or as the admin account, according to `login`. It
feeds the rendered script to `bash -s` on the droplet. A `root` step that acts for the agent runs
the command through `sudo -u {{ agent_user }}`, as `examples/hello` does. The admin account has
passwordless sudo.

A script that exits zero succeeded, and any other exit fails the step. The step's `mandatory` key
decides what a failure does, as the README's [`gfz provision`](../README.md#gfz-provision-name)
section describes. `gfz` does not show a script's stdout.

`forward_agent = true` forwards your local ssh agent into the session, which a step needs to reach a
private repository over ssh with your keys. Leave it off for any step that does not need it: a
forwarded agent lets the droplet's root use your keys for as long as the session lasts.

Write each script so it is safe to run again. `gfz provision` re-runs steps, and an operator resumes
a stopped run with `--from`. Check before you act, as the core steps do: skip an install whose
result is already there, and append to a file only what it lacks. Open each script with
`set -euo pipefail`, so a failed command fails the step instead of running on.

To check what your templates render to, preview them and run them through
[shellcheck](https://www.shellcheck.net/):

```bash
gfz provision <droplet> --dry-run --only <plugin> | shellcheck -s bash -
```

## Settings

A setting's value comes from the first of these sources that sets it:

1. The environment variable `GFZ_PLUGIN_<NAME>_<SETTING>`.
2. The `[plugin.<name>]` table in the config file.
3. The manifest's `default`.

In the variable, the plugin's name is uppercased with its hyphens turned into underscores, and the
setting's name is uppercased. So setting `port` of plugin `my-tool` reads the variable
`GFZ_PLUGIN_MY_TOOL_PORT`, and the key `port` in the `[plugin.my-tool]` table.

The table is `[plugin.<name>]`, singular. The config file's `plugins` key already holds the plugin
list, and TOML cannot hold a list and a table under one key. A table for a plugin that is not on the
plugin list is ignored.

A plugin setting has no flag.

### Values and their types

The two sources spell values differently:

- From the environment, a `string` is the variable's text as it stands. An `integer` is the text
  read as a whole number. A `list` is the text split on commas, with each item trimmed of
  surrounding whitespace and empty items dropped, the way `GFZ_SSH_KEYS` reads.
- From the config file, a value must already have its type: a TOML string for `string`, a TOML
  integer for `integer`, and an array of strings for `list`.

### Setting errors

Each of these is an error that names the setting, its environment variable, and its config key:

- A value of the wrong type, from either source.
- A required setting that no source sets.
- A key in a `[plugin.<name>]` table that the manifest does not declare. The error lists the
  settings the plugin does declare.

The hyphen in a plugin's name and the underscore in a setting's name both become `_` in the
variable. So two plugins can produce one variable: setting `b_c` of plugin `a`, and setting `c` of
plugin `a-b`, both read `GFZ_PLUGIN_A_B_C`. `gfz` refuses that pair when both plugins are on the
list, and the error names both settings. Rename a setting or a plugin to resolve it.

## Step names and selection

A core step's name is its slug, such as `docker-engine`. A plugin step's name is `<plugin>/<slug>`,
such as `hello/greet`. That name appears everywhere a step is named: the progress lines, the run
summary, the failure line, and the `--only`, `--from`, and `--skip` flags of `gfz provision`.

A bare plugin name stands for every step of that plugin:

- `--only <plugin>` runs all of the plugin's steps.
- `--skip <plugin>` drops all of them.
- `--from <plugin>` starts at the plugin's place in the run, which is its first step.

A plugin with no steps is still a valid name. It selects no step under `--only` and drops none under
`--skip`. Under `--from` it starts the run at the first step of any plugin after it, or runs nothing
when none of them has a step.

A name that matches no step and no plugin is a usage error. `gfz` lists every valid step name and
every plugin name, and exits 2.

## Choosing plugins

The `plugins` option holds the list of plugins to run. `README.md` covers how to set it. Each entry
takes one of two forms:

- A built-in name: an entry with no `/` that does not start with `~`. It names a plugin that ships
  inside `gfz`, and `gfz plugins` lists them. An unknown name is an error that lists the built-in
  names.
- A path to a plugin directory. It is either absolute, or starts with `~`, which expands to a home
  directory: `~` to yours, `~<user>` to that user's. Any other entry holding a `/` is a relative
  path, and `gfz` refuses it, because a relative path would mean something different in every
  directory you run `gfz` from.

A path must name a directory that holds a `plugin.toml`.

Core steps run first. Then each plugin's steps run, plugin by plugin in list order, each plugin's
steps in manifest order.

`gfz` also refuses a list that holds any of these:

- The same plugin directory twice, under any spelling. `gfz` compares the directories after
  following symbolic links.
- Two plugins with the same `name`.
- A plugin whose `name` is a core step's slug. A bare name on the command line must mean one thing.
  `gfz steps` with `plugins = []` lists the core slugs.

## When gfz checks a plugin

`gfz create` checks every plugin before it makes any DigitalOcean call, and `gfz provision` checks
every plugin before it looks up the droplet. Each loads every manifest, resolves every setting, and
renders every step it will run: the whole run for `create`, and the selected steps for `provision`.
A broken manifest, a missing setting, or a template with an error stops the command there, with a
message naming the cause. So a broken plugin creates no droplet and opens no ssh session.

A template error names the step and its template. For a syntax error it also names the file and
line, which can be a file the template includes.

`gfz steps` loads every manifest but resolves no setting, so it works before you have set any.
`gfz config show` loads every manifest and resolves every setting.

## Walkthrough: examples/hello

`examples/hello` writes a greeting to `~/hello.txt` in the agent's home. Its manifest is the one at
the top of this document. Its one template, `greet.sh.j2`, is:

```jinja
set -euo pipefail

printf '%s\n' {{ plugin.greeting | shell_quote }} \
  | sudo -u "{{ agent_user }}" tee "{{ agent_home }}/hello.txt" >/dev/null
```

The step logs in as root, and writes the file through `sudo -u` as the agent, so the agent owns it.

### Put it on the plugin list

Copy the directory somewhere stable, such as `~/gfz-plugins/hello`, or point at it in a clone of
this repository. Then add it to the `plugins` list in `~/.config/gfz/config.toml`, after `claude`,
and set its one setting:

```toml
plugins = ["claude", "~/gfz-plugins/hello"]

[plugin.hello]
greeting = "good morning"
```

### See where it runs

`gfz steps` now lists the hello step after the `claude` steps:

```text
$ gfz steps | grep -v '  core  '
step             source  login  policy     summary
claude/agent     claude  root   mandatory  Install Claude Code for the agent.
claude/admin     claude  root   optional   Install Claude Code for the admin.
hello/greet      hello   root   mandatory  Write the greeting to ~/hello.txt.
```

`gfz config show` adds a `hello.greeting` row with the value `good morning` and the source `file`.
Run with `GFZ_PLUGIN_HELLO_GREETING="good evening"` set, the row shows `good evening` and the source
`environment` instead. Remove both, and the row shows the manifest's default, `hello`, with the
source `default`.

### Preview the script

On a droplet named `alpha`, a dry run of the plugin prints the rendered script under a header naming
the step and its login:

```text
$ gfz provision alpha --dry-run --only hello
# hello/greet (root)
set -euo pipefail

printf '%s\n' 'good morning' \
  | sudo -u "agent" tee "/home/agent/hello.txt" >/dev/null
```

The greeting arrives quoted by `shell_quote`. The names and paths come from the core values, here
with the agent account named `agent`.

### Run it

`gfz create <name>` runs core, then `claude`, then `hello`, and ends with its closing line. On a
droplet that exists already, `gfz provision <name> --only hello` runs the one step. Then
`gfz ssh <name> --agent` opens a shell as the agent, where `cat ~/hello.txt` prints the greeting.
