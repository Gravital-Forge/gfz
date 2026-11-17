# verification binding

The repository root is the one package. Its green bar is three checks, each run from the root:

1. `poe lint`, which runs ruff's rules and its format check.
2. `poe test`, which runs pytest. The suite runs shellcheck over every rendered step script.
3. `uvx rumdl@0.3.4 check .`, the markdown lint.

The bar is green only when all three pass. An implementer runs it at every task checkpoint, and CI
runs the same three checks on every pull request.

## Constraints

- All three checks are isolated checks: no test binds a fixed port or a fixed host path, and rumdl
  runs with its cache off.
- A **deployment acceptance check** is a check that needs a DigitalOcean token or a real droplet.
  The operator runs it from a runbook handed over once CI is green, and the result is recorded on
  the pull request. It never runs in the project.

Everything else resolves to the workspace root's `verification` topic.
