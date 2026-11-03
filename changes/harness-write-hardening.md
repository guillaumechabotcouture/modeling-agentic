---
phase: "v2"
commit: "HW α-β"
---

## Changelog

**Harness write hardening (follow-up to #129, Codex round 5).** Under the hostile-artifact model, harness writes in a run dir go through `scripts/lib/safe_io.py`. `safe_io` gains `anchored` / `publish` / `append_text` / `remove_path`, which anchor a path at the trusted run dir and traverse every component below it with no link followed. `compute.run_map` and its lock now anchor at the run dir, so a planted `analysis/batch` link is refused rather than resolved. The following now publish, append or remove through `safe_io`:
- compute: the ledger lock and the ledger, progress records, and detached-job records;
- the fix loop: impact ledger, plan, progress and restore;
- the budget ledger;
- toolkit/analysis: batch design and results, allocation robustness, the deliverable CSV, claims spec and results, decision-test records, the uncertainty report, figures and their provenance, and shipped bundles;
- decision_brief restore and lock, the validator history, the done-when registry, the figure-validator sidecar, the claims ledger, the rendered report and its snapshot, the optimisation report, and `v2_sdk_tasks`' outputs.

`scripts/write_audit.py` is now AST-based and resolves import aliases. It covers multi-line `open`, `Path.open`/`write_text`/`write_bytes`, `io.open`/`os.fdopen`, dynamic modes, `os` removals and renames, `shutil`, numpy saves, `tofile`, `savefig`, dumps to handles, `tempfile` with `dir=`, and `os.open` write flags. It scans agents/, scripts/, toolkit/ and tools/, excluding self-test code. Each site is either REVIEWED, meaning it cannot reach a run dir an agent writes to, with a reason, or a counted OPEN_ITEM: 95 attacker-reachable sites that are not yet migrated (the v1 pipeline, pipeline sandbox asides, derived_rebuild, and v2 stub tasks). WA4-WA9 run the writers Codex named against planted links.
