---
phase: "Diag"
commit: "MCP γ"
---

## Changelog

**Diagnostics: `follow.py` refreshes the local copies.** `tools/diagnostics/follow.py` is the one part of the diagnostics that uses the network, read-only (`docs/design/diagnostic-data.md` §5). Each pass: each recorded label's latest checkpoint (`run_cluster.recorded_labels`) through `cloud/run_cluster.py`'s `checkpoint_record` and `fetch_checkpoint`, staged outside the folders the server reads and published in one rename (a launch and generation already mirrored is skipped; the generation recorded in `<label>/mirror.json` is the one the verified fetch returned; a mirror overlapping the launch's output folder is refused; the storage account from `$MA_STORAGE_ACCOUNT` / `$COILED_STORAGE_ACCOUNT`, else the config's); `cloud/budget.py status --json` (new flag: the same numbers as one JSON document); `gh pr list --json` (with a truncation flag); each config's launch records read with no link followed, with the cap projection of the launch's own invocation; and the supervisor's `status.json` from storage. Each cache carries its fetch time; a failed refresh keeps the last good snapshot and records the error, which the tools report. A failing part is recorded in `follow_status.json`, never stops the others, and makes `--once` exit 1; nothing is launched, stopped, attached or written remotely. Self-tests FO1-FO9.
