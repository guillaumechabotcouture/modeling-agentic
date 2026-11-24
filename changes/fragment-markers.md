---
phase: "Diag"
commit: "FM α"
goal: diagnostics
---

## Changelog

**Change fragments name their goal and smoke markers.** Two optional front-matter fields (owner, 2026-11-24; `docs/design/diagnostic-data.md` §4, P2 and P3): `goal`, an id in the new `docs/design/goals.yaml` (autonomy, quality-vs-bare-agents, architecture, f6, diagnostics) as it stands at the PR's head, and `markers`, a list of `scripts/smoke_check.py` specs with paths relative to the run directory. `scripts/assemble_changes.py` checks both in `--check-pr` and at assembly (an unknown goal, a marker that does not parse, an absolute or parent path, or a malformed goals file is refused); fragments without them stay prose, and the fold ignores them. Self-tests CF-G1 to CF-G6.
