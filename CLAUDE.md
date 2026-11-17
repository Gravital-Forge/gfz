# gfz — agent instructions

`gfz` creates and provisions DigitalOcean droplets for coding agents. Core provisioning builds the
droplet with an admin account and an unprivileged agent account; plugins add setup steps after it.

- `gfz` runs only on an operator's machine, against the operator's DigitalOcean account. It never
  runs on a droplet, and an agent never runs a live `gfz` command. An agent works on the code and
  runs the test suite, which replaces DigitalOcean and ssh with fakes.
- `gfz` reads the DigitalOcean API token from the `DIGITALOCEAN_TOKEN` environment variable alone.
  No flag, config key, or output carries it.

The project bindings are in `.dev/`.
