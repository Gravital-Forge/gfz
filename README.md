# gfz

`gfz` creates and provisions DigitalOcean droplets for coding agents. Each droplet gets an admin
account you work from and an unprivileged agent account the coding agent works from. Plugins add
your own setup steps on top.

It is an operator tool: you run it from your own machine, against your own DigitalOcean account. It
never runs on a droplet, and no agent runs it.

## Install

`gfz` needs Python 3.12 or newer, and [uv](https://docs.astral.sh/uv/) to install it:

```bash
uv tool install git+https://github.com/Gravital-Forge/gfz
```

Provisioning also needs the `ssh` client on your `PATH`.

## What it does

`gfz` has two halves. One manages a droplet's life: create it, list it, power it, destroy it, open a
shell on it. The other provisions it, running an ordered sequence of shell scripts over ssh.
`gfz steps` prints that sequence and `gfz provision` runs it.

The sequence starts with core: the steps every droplet gets. Core assumes a stock image and installs
everything it needs. It installs Docker, creates the admin and the agent accounts, and gives the
agent a rootless Docker daemon of its own and a toolchain to build with. Some optional extras
follow. `gfz steps` prints the live list, with what each step does.

Root's ssh keys carry over to both accounts, so the key that created the droplet reaches either one.

After core, each configured plugin's steps run, plugin by plugin. By default that is the built-in
`claude` plugin, which installs Claude Code. [Plugins](#plugins) says how to change that.

The scripts are written for the Debian/Ubuntu family. They call `apt-get` and they configure
Docker's apt repository, so they will not run on a host outside that family. That is the tool's
limit, stated rather than worked around.

## Boundaries

The agent account exists so that what the agent does stays inside a fence. Each limit has a reason:

- **No sudo.** An agent runs commands it chose itself, some of them shaped by text it read on the
  way. Without sudo, a bad command can damage the agent's own account and nothing beyond it. The
  admin account holds sudo, through a drop-in at `/etc/sudoers.d/90-gfz-admin`, for the work that
  needs it.
- **No `docker` group.** Membership in that group is root by another name: anyone who can start a
  container on the system daemon can mount the host's filesystem into it.
- **Its own rootless Docker daemon.** The agent still builds and runs containers. They run under the
  agent's own user ID, so a container that breaks out lands in the agent's account, not in root's.
- **A private home.** The agent's home is mode `700`. What it keeps there, such as a login token, is
  closed to every other unprivileged account on the droplet.

## Plugins

A plugin is a directory holding a `plugin.toml` manifest and the shell templates it names. Its steps
run after core, in the same engine, and every step command treats them the same way.

`gfz` ships the `claude` plugin. Its `claude/agent` step installs Claude Code for the agent, and its
optional `claude/admin` step installs it for the admin. `gfz plugins` lists every plugin that ships
with `gfz`.

The `plugins` option chooses which plugins run, in order. Each entry names a built-in plugin or a
plugin directory. [Choosing plugins](docs/plugins.md#choosing-plugins) gives the forms an entry
takes. Setting `plugins` from any source replaces the default, so list `claude` too if you want to
keep it. To run your own plugin after `claude`, put this in your config file:

```toml
plugins = ["claude", "~/gfz-plugins/hello"]
```

To swap `claude` for something else, such as a plugin of your own that installs a different coding
agent, leave `claude` out of the list. To run core alone, set `plugins = []`.

`--plugin` does the same for one run. It is a `gfz` option, so it goes before the command, once per
plugin:

```bash
gfz --plugin claude --plugin ~/gfz-plugins/hello steps
```

`docs/plugins.md` is the plugin author's guide: the manifest, the template values, plugin settings,
and a walkthrough of the `examples/hello` plugin in this repository.

## What it prints

Every operation that takes more than a moment says what it is starting, then confirms when it is
done. Those progress lines go to stderr, and so does every failure. Stdout carries the result and
nothing else: the tables, the state lines, the run summary, the closing line. So a redirected
`gfz list` captures a clean table, and a long `gfz create` still scrolls its progress past you.

## The API token

`gfz` reads `DIGITALOCEAN_TOKEN` from the environment, and only from the environment. It has no flag
and no config-file key.

That is a choice, not a gap. A token on the command line lands in shell history and in the process
list, where any other process on the machine can read it. A token in a file is a secret sitting at
rest. The environment is the least bad place for a value this sensitive to live.

`gfz config show` reports whether the token is set. It never prints the value.

### The scopes the token needs

A custom-scoped DigitalOcean token needs these scopes:

- `droplet:create` — `gfz create` makes the droplet.
- `droplet:read` — `gfz list` prints droplets, and every command that takes a name finds one.
- `droplet:update` — `gfz start` and `gfz stop` are droplet power actions.
- `droplet:delete` — `gfz destroy` deletes the droplet.
- `ssh_key:read` — `gfz` resolves a key name in `GFZ_SSH_KEYS` against the account's keys.
- `tag:create` — `gfz create` tags every droplet it makes with the configured tag.
- `tag:read` — DigitalOcean grants no scope on a resource without that resource's read scope, so the
  two tag scopes travel together.

A full-access token works too. It also lets anything holding it reach every resource in the account,
which is the reason to scope one instead.

## Configuration

Every setting resolves in the same order: the flag, then the environment variable, then the config
file, then the field's default. The config file is TOML, at `~/.config/gfz/config.toml` by default,
moved with `GFZ_CONFIG`. No config file is fine; `gfz` falls back to the environment and the
defaults.

Every setting's flag belongs to `gfz` itself, so it goes before the command:
`gfz --region nyc3 create beta`. One flag, one place, whichever command follows it. A flag overrides
that setting for the one run. The flags a single command owns, such as `--all`, `--force`, and
`--yes`, go after the command, where [Commands](#commands) documents them.

The config path itself is the one option that order does not cover. It comes from the flag, the
environment, or the default, and never from the file: a config file cannot relocate itself. `gfz`
ignores a `config` key inside `config.toml`.

Each option's config-file key is its flag without the dashes and with underscores between the words:
`--ssh-identity` on the command line is `ssh_identity` in `config.toml`. The one exception is
`--plugin`, which you repeat once per plugin; its key is `plugins`.

A required option with no value from any source is an error. The message names that option's flag,
its environment variable, and its config-file key, so you never have to look one up from another.

`GFZ_SSH_KEYS` and `GFZ_PLUGINS` each hold more than one value. Type them comma-separated as an
environment variable; write them as a TOML array in the config file. `--ssh-keys` takes the
comma-separated form, and `--plugin` takes one entry each time you pass it.

`GFZ_AGENT_USER` takes lowercase letters, digits, and hyphens, starts with a letter or a digit, and
runs to at most 32 characters, the Linux user-name limit. `gfz` refuses any other agent name before
it reaches DigitalOcean.

Each option, with its flag, its environment variable, and its default:

- Admin user: `--admin-user`, `GFZ_ADMIN_USER`. Required.
- Agent user: `--agent-user`, `GFZ_AGENT_USER`. Required.
- Account SSH keys: `--ssh-keys`, `GFZ_SSH_KEYS`. Required.
- Local SSH key: `--ssh-identity`, `GFZ_SSH_IDENTITY`. Required.
- Region: `--region`, `GFZ_REGION`. Defaults to `tor1`.
- Size: `--size`, `GFZ_SIZE`. Defaults to `s-4vcpu-8gb`.
- Image: `--image`, `GFZ_IMAGE`. Defaults to `ubuntu-24-04-x64`.
- Tag: `--tag`, `GFZ_TAG`. Defaults to `gfz`.
- Plugins: `--plugin` (repeatable), `GFZ_PLUGINS`. Defaults to `claude`.
- Config path: `--config`, `GFZ_CONFIG`. Defaults to `~/.config/gfz/config.toml`.
- API token: no flag, `DIGITALOCEAN_TOKEN`. Required.

A plugin's own settings have no flag. Each one reads an environment variable of the form
`GFZ_PLUGIN_<NAME>_<SETTING>`, then a `[plugin.<name>]` table in the config file, then the
manifest's default. `docs/plugins.md` gives the details.

### The SSH keys

`gfz` has two SSH-related settings, and they are not the same thing.

`GFZ_SSH_KEYS` names keys already registered in your DigitalOcean account. When `gfz create` makes a
droplet, DigitalOcean installs those public keys on it.

`GFZ_SSH_IDENTITY` names a private key file on your own machine. `gfz ssh` and `gfz provision` both
pass that file to the `ssh` binary.

One setting is the account side, the other is your machine. They must correspond. If they do not, a
droplet you just created will refuse your login, and a provisioning run stops at its first step.

An entry in `GFZ_SSH_KEYS` takes one of three forms: a numeric key ID, a fingerprint, or a key name.
DigitalOcean's API accepts only the first two, so `gfz` resolves a name against the account's keys
itself. A name that matches no account key is an error, and the error lists the names the account
does hold.

### Host keys

`gfz` runs every provisioning step over ssh with `StrictHostKeyChecking=accept-new`. A host key you
have never seen is accepted and recorded; a key that has changed is refused. So a brand new droplet
takes its first connection without a prompt, which is what an unattended run needs, and a
substituted host still stops you.

DigitalOcean reuses addresses. A fresh droplet can land on an address your `known_hosts` already
binds to a destroyed droplet's key, and ssh then refuses the connection: provisioning stops, and
`gfz ssh` hands you the same warning. Drop the stale entry and try again:

```bash
ssh-keygen -R <address>
```

## Commands

Every command that takes a droplet name finds it the same way: it matches that name among the
account's droplets. Two droplets sharing a name is an error naming both IDs. No match is an error
naming the name.

### `gfz config show`

Prints every resolved option with the source that supplied it: flag, environment, file, or default.
The `plugins` row shows each entry as the plugin directory it resolves to. A row follows for each
setting of each configured plugin, named `<plugin>.<setting>`, with its value and its source:
environment, file, or default. Prints the token as set or not set, never its value.

### `gfz list`

Prints the droplets carrying the configured tag, as a table of name, ID, status, region, size,
address, and creation time. A droplet with no address yet still appears, with the address column
empty.

- `--all` prints every droplet in the account, not only the tagged ones.
- `--tag <tag>` selects a different tag.

### `gfz create <name>`

Creates one droplet with the configured region, size, image, SSH keys, and tag. Monitoring is on.
Backups and IPv6 are off.

`create` checks every plugin before it makes any DigitalOcean call, so a plugin it cannot use
creates no droplet. [When gfz checks a plugin](docs/plugins.md#when-gfz-checks-a-plugin) says what
that check covers.

`create` then waits, up to 300 seconds, for the droplet to become active, to get a public IPv4
address, and to accept a TCP connection on port 22. On timeout, `gfz` reports the droplet's ID and
any address it has, and exits non-zero. It never deletes the droplet: a slow boot is not a reason to
throw one away.

Once the droplet answers ssh, `create` provisions it, running every core and plugin step. It prints
the droplet's state line, then the run summary, then one closing line:

```text
droplet alpha (203.0.113.10) created with agent user agent
```

`--no-provision` stops after the wait and leaves the droplet bare. It prints the droplet's state
line and no closing line. A provisioning run that stops on a failure leaves the droplet in place;
fix the cause and resume with `gfz provision <name> --from <step>`.

### `gfz steps`

Prints every core and plugin step in run order. Each line names one step: its name, its source
(`core` or the plugin's name), the account that runs it, whether a failure stops the run, and what
it does. With the default plugin list, the plugin rows look like this:

```text
$ gfz steps | grep -v '  core  '
step             source  login  policy     summary
claude/agent     claude  root   mandatory  Install Claude Code for the agent.
claude/admin     claude  root   optional   Install Claude Code for the admin.
```

`steps` reads only the plugin list and the config path, and loads each plugin's manifest. It needs
no other option, and asks for no plugin setting.

### `gfz plugins`

Prints each plugin that ships with `gfz`, with its summary. It needs no option.

### `gfz provision <name>`

Renders the selected steps against the resolved configuration, then runs each one over ssh on the
named droplet. Each step reports as it starts and again as it finishes. Like `create`, it checks
every plugin before it looks up the droplet, so a plugin it cannot use opens no ssh session.

A freshly created droplet is still running its first-boot package upgrade when provisioning starts,
and that upgrade holds the package lock. Every core apt call waits up to ten minutes for the lock,
rather than failing against it. While it waits, the step prints nothing, so a root step that looks
idle for up to ten minutes on a new droplet is waiting for the lock, not stuck.

Every core step either checks before it acts or does something safe to repeat. A package already
installed is left alone, a generated file is written back the same, and a missing key is added to
`authorized_keys` without disturbing the keys already there. A re-run may move Node to a newer 24.x
release.

A step is either mandatory or optional. A mandatory step that fails stops the run: `gfz` prints that
step's name and what the remote host wrote to stderr, and exits non-zero. An optional step that
fails only warns. The run carries on, the end-of-run summary names every optional step that failed,
and the run still exits zero.

These flags select and preview:

- `--only <name>` runs only the steps that name selects. Repeat it for more than one.
- `--from <name>` starts at that name and runs every step after it. It cannot be combined with
  `--only`.
- `--skip <name>` drops the steps that name selects from whatever the other flags selected. Repeat
  it for more.
- `--dry-run` prints the rendered scripts and opens no connection. It still resolves the droplet
  through the API, so what it prints is what would have run.

Each `<name>` is a step name, as `gfz steps` prints it, or a plugin name, which stands for every
step of that plugin: `--only claude` runs every `claude` step.
[Step names and selection](docs/plugins.md#step-names-and-selection) gives the naming rules, and
what `gfz` does with a name that matches nothing.

A finished run, other than a dry run, prints the run summary and then one closing line:

```text
droplet alpha (203.0.113.10) provisioned with agent user agent
```

### `gfz destroy <name>`

Deletes one droplet.

It refuses a droplet that does not carry the configured tag. `--force` is the only override.

It prints the droplet's name, ID, region, and creation time, then asks you to type the name back
before it deletes anything. `--yes` skips that prompt.

It deletes that droplet, and nothing else.

`destroy` then waits, up to 300 seconds, for the account to stop listing the droplet, and confirms
the deletion:

```text
alpha (id 101): destroyed
```

On timeout it reports that the account still lists the droplet, and exits non-zero.

### `gfz start <name>`

Powers the droplet on, then waits, up to 300 seconds, for it to report status `active`. It prints
the droplet's state once it gets there:

```text
alpha (id 101): status active, address 203.0.113.10
```

On timeout it reports the last status it saw, and exits non-zero.

### `gfz stop <name>`

Shuts the droplet down gracefully. `--hard` powers it off instead, for a guest that does not answer.

Either way, `stop` waits, up to 300 seconds, for the droplet to report status `off`, and prints that
state the same way. On timeout it reports the last status it saw, and exits non-zero.

### `gfz ssh <name>`

Opens an interactive shell on the droplet as the admin user. `--agent` opens it as the agent user
instead.

`gfz ssh` uses the configured identity file, and the real `ssh` binary found on `PATH`.

## Working on gfz

The repository is one uv package. From a clone, `uv run gfz --help` runs the tool from source.
`poe lint` and `poe test` run the linters and the test suite; the suite replaces DigitalOcean and
ssh with fakes, so it needs no token and no droplet.

## License

`gfz` is released under the MIT License. See [LICENSE](LICENSE).
