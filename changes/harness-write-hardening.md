---
phase: "v2"
commit: "HW α-β3"
---

## Changelog

**Harness write hardening (follow-up to #129, Codex round 5).** Under the hostile-artifact model, harness writes in a run dir go through `scripts/lib/safe_io.py`. `safe_io` gains `anchored` / `publish` / `append_text` / `remove_path`, which anchor a path at the trusted run dir and traverse every component below it with no link followed. `compute.run_map` and its lock now anchor at the run dir, so a planted `analysis/batch` link is refused rather than resolved. The following now publish, append or remove through `safe_io`:
- compute: the ledger lock and the ledger, progress records, and detached-job records;
- the fix loop: impact ledger, plan, progress and restore;
- the budget ledger;
- toolkit/analysis: batch design and results, allocation robustness, the deliverable CSV, claims spec and results, decision-test records, the uncertainty report, figures and their provenance, and shipped bundles;
- decision_brief restore and lock, the validator history, the done-when registry, the figure-validator sidecar, the claims ledger, the rendered report and its snapshot, the optimisation report, and `v2_sdk_tasks`' outputs.

`scripts/write_audit.py` is now AST-based and resolves import aliases. It covers multi-line `open`, `Path.open`/`write_text`/`write_bytes`, `io.open`/`os.fdopen`, dynamic modes, `os` removals and renames, `shutil`, numpy saves, `tofile`, `savefig`, dumps to handles, `tempfile` with `dir=`, and `os.open` write flags. It scans agents/, scripts/, toolkit/ and tools/, excluding self-test code. Each site is either REVIEWED, meaning it cannot reach a run dir an agent writes to, with a reason, or a counted OPEN_ITEM. The OPEN_ITEMS are 58 v1 sites, tagged "removed at v1 retirement". WA4-WA9 run the writers Codex named against planted links. A second step migrated the remaining v2 items. The pipeline sandbox's aside and restore moves, copies and removals now go through the new `safe_io.rename_nofollow` and `copy_nofollow` and through `remove_nofollow`. A planted aside entry that is a link is refused rather than moved. So are the `derived_rebuild` sidecars and report and the v2 dry-run stub tasks (WA10-WA12).

Following Codex's review of this PR (WA13-WA20, each reproduced first):
- The run root is now mandatory for any run-dir path. `safe_io.anchored`, `publish`, `append_text` and `remove_path` take it as a required argument. A path under a run dir (found from the outermost `metadata.json` ancestor) is refused when the root is omitted, None, or a dir below the run root.
- The audit flags any `safe_io` call that has no root.
- The detached launch (`compute._detach`) now passes the run root to its launch lock, lock, log and job record. The child's log descriptor comes from `open_append_nofollow`, with its link-count check, instead of a reopen by name.
- The `--no-ledger` CLI outputs now publish through `safe_io` against `run_dir`. Their REVIEWED exemption is removed.
- Shipped bundles (`batch.ship` / `merge_shipped`, new `run_dir`) and figure provenance (`validate_figure`, which defaults to the PNG's run dir) are now anchored at the run root.
- `rename_nofollow` opens the source with no link followed, as a plain file, and publishes its bytes from that fd. It then removes the source only if it is still the same inode.
- The audit also finds `getattr(x, "open")`, `functools.partial(open, <write mode>)`, assigned writer aliases, and shell commands (`shell=True`, `sh -c`, `os.system`). Each shell command is reviewed.
- A helper in another module that returns `open` is a documented limit of the per-module scan.
