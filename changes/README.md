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
