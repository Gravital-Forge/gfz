# version-control binding

Two checkouts serve the project, each at a fixed place under `<workspace root>/projects/`:

- The **primary clone**, `projects/gfz`, stays on `main` and takes no edits.
- An **item worktree**, `projects/gfz-<slug>`, is on `task/<slug>`, cut from the primary clone's
  freshly pulled `main`. Each slug is new: no existing `task/` branch carries it.

## How to use it

- Native `git` and `gh` carry every operation, per the workspace root's `version-control` topic.
- Pull `main` and add or remove an item worktree from the primary clone, because an item branch is
  cut from the primary clone's `main`. Run every command that acts on the item's branch from the
  item worktree: status, add, commit, push, and diff.
- The CI watch runs on the root topic's inconclusive threshold, because this project's CI waits on
  no outside build.
- `.github/rulesets/` holds the repository's rulesets as JSON. The operator syncs them to GitHub
  after a change merges.

Everything else resolves to the workspace root's `version-control` topic: the `task/` branch prefix,
the one pull request to `main` that lands an item and that only the operator merges, staging from
explicit file lists, keeping every merged branch, and the CI watch's shape.
