---
phase: "51"
commit: "α"
status_group: "Merged (Phase 51, development process)"
---

## Changelog

**Change fragments, so PRs stop conflicting.** Nearly every PR of Phases 45-49 conflicted on the end of this ledger table and on the plan's Status section (PR #74's merge: "keep both CHANGELOG and plan entries"; #79, #80 and #81 all add a group at the same line), because two PRs appending at the same point always conflict. A PR now adds one file, `changes/<branch>.md` (front matter `phase`, `commit`, optional `status_group`; a `## Changelog` paragraph and an optional `## Status` list; format in `changes/README.md`). `scripts/assemble_changes.py --write` folds the fragments into the ledger (phase and Greek-letter order) and into the Status section (joining an existing group, or above a new marker line), then deletes them; a maintainer runs it at release and commits the result, so CI needs no write access. CI runs `--check-pr HEAD^1` on pull requests: a fragment that does not parse, or any edit of the ledger table or the Status section other than the exact fold of fragments the same PR deletes, fails; edits elsewhere in either file (Gate internals) are not checked. Self-test: 38 cases, the first reproducing the conflict with `git merge-file`; the PR check runs against a scratch git repository. Nothing old migrated.

## Status

- **α:** change fragments in `changes/` replace direct edits of the CHANGELOG ledger and of this Status section; `scripts/assemble_changes.py` folds them at release; CI fails a direct edit.
