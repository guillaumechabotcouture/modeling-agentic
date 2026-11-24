---
phase: "Diag"
commit: "MCP γ"
---

## Changelog

**Diagnostics: `follow.py` refreshes the local copies.** `tools/diagnostics/follow.py` is the one part of the diagnostics that uses the network, read-only (`docs/design/diagnostic-data.md` §5). Each pass: each config label's latest checkpoint through `cloud/run_cluster.py`'s `checkpoint_record` and `fetch_checkpoint` into the local mirror (a generation already mirrored is skipped, recorded in `<label>/mirror.json`; the storage account from `$MA_STORAGE_ACCOUNT` / `$COILED_STORAGE_ACCOUNT`, else the config's; older local copies removed inside the mirror only), `cloud/budget.py status --json` (new flag: the same numbers as one JSON document), `gh pr list --json`, each config's local launch records and `cloud/supervisor.py pull`, each cached with its fetch time. A failing part is recorded in `follow_status.json` and never stops the others; nothing is launched, stopped, attached or written remotely. Self-tests FO1-FO6.
