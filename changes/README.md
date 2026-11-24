# Change fragments

Each PR adds one file here, named after its branch (for example
`changes/phase51a-change-fragments.md`; a `/` in the branch name becomes
`__`, so `feat/x` is `changes/feat__x.md`, and
`python3 scripts/assemble_changes.py --name <branch>` prints it), instead of editing the phase
ledger in `CHANGELOG.md` or the Status section of
`docs/design/improvement-plan.md`. No two PRs touch the same fragment,
so these entries no longer conflict.

```markdown
---
phase: "51"
commit: "α"
status_group: "Merged (Phase 51, development process)"   # optional
goal: diagnostics                                         # optional
markers:                                                  # optional
  - "run_state.json:history"
  - "events.jsonl~\"kind\": \"stage\""
---

## Changelog

**One-line title.** The ledger row's Change text: one paragraph, as it
would appear in the table (line breaks become spaces, `|` is escaped).

## Status

- **α:** the line(s) for the plan's Status section (optional; needs
  `status_group`).
```

At release, a maintainer runs

```bash
python3 scripts/assemble_changes.py           # dry run: lists what will be folded
python3 scripts/assemble_changes.py --write   # folds the fragments, deletes them
```

and commits the result (directly on main or as a release PR). Ledger rows
go to the end of the table in (phase, Greek letter, file name) order.
Status lines join an existing `<status_group>:` line in the Status
section, or start a new group above the
`<!-- change-fragments ... -->` marker line.

CI runs `python3 scripts/assemble_changes.py --check-pr HEAD^1` on every
pull request: it fails a PR whose fragment does not parse, or that edits
the ledger table or the Status section in any way other than the exact
fold of fragments the same PR deletes, or whose folded entry is still
queued under another name. Fragments are flat: a nested path or a non-`.md`
file under `changes/` is an error for both the check and the release (one
discovery rule). `phase`, `commit` and `status_group` are single lines. This
README is not a fragment.

Two optional, machine-readable fields (owner, 2026-11-24;
`docs/design/diagnostic-data.md`), which the diagnostics tools read:

- `goal`: the one goal this change builds toward, an `id` in
  `docs/design/goals.yaml` as it stands at the PR's head. Add a goal there
  in the same PR if none fits.
- `markers`: the smoke markers only this change's code produces, as a list
  of `scripts/smoke_check.py` specs (`PATH`, `PATH:KEY`, `PATH:KEY=VALUE`,
  `PATH:KEY^=PREFIX`, `PATH~REGEX`), each path relative to the run
  directory. They are the same markers the PR description names, so a
  baseline's collected runs can be checked against them.

The check refuses an unknown goal or a marker that does not parse. Older
fragments, written before these fields, stay prose and still pass. The
fold ignores both fields.
