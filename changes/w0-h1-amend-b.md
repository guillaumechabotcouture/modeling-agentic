---
phase: "Wave 0"
commit: "W0-H1 ε"
---

## Changelog

**W0-H1 (b): `amend` after the second Codex round, and `--finish`.** Stacked on #318. Codex round 2 raised 15 P1s and 1 P2 on #318's fixes. Fixed here:
- A child path is checked against the parent by inode, so a case-insensitive spelling of the parent is refused.
- A sign-off record that is present but binds no readable digest refuses the amendment.
- Directory modes are kept, and a rewritten spec or dossier keeps its file mode.
- The whole parent is checked for links and unreadable directories before anything is left out.
- A digit path segment keeps its spelling (an index only in a list; the YAML integer key only when no text key matches).
- A patch value is copied at each insertion.
- `record_cited_inputs` runs as at SPEC, and the backend's pre-sign checks get `derived=True` exactly when the run derives its inputs.
- A request that names findings needs the parent's `findings.jsonl`.
- No `run_state.json` is written unless the dry-sign passes. The state is kept in the record, and `python3 -m toolkit.spec.amend --finish <child>` re-checks the child once its open review conditions are disposed of. It regenerates the sheet with the backend candidates, redoes the dry-sign and the sign command, and writes the run state when the dry-sign passes.
- The job-keyed zero override applies only to calibration call files. Each job is counted once as reusable, or once as to compute, across files and archives.
- The child's `max_rounds` is the typed value the launch command prints.
- `code_pin` reports `clean` only when the repo is the git top level.
- The recovery command quotes the child path.

Self-tests AM_NUMERIC_KEY, AM_PATCH_ALIAS_COPIED, AM_UNREADABLE_SIGNOFF_REFUSED, AM_PRUNED_LINK_REFUSED, AM_DIR_MODE_KEPT, AM_FAILED_DRY_SIGN_NO_STATE, AM_FINISH.
