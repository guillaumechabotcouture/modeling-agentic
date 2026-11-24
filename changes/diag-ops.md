---
phase: "Diag"
commit: "MCP β"
---

## Changelog

**Diagnostics: more run tools and the operations tools.** The read-only server gains `run_artifact` (allow-list matched per path component, step logs read as their tail from the end), `run_compare` (reference runs named as sources) and `owner_inbox` (`tools/diagnostics/diag_records.py`: the scope sign-off command with its answers, mirrored cloud runs routed to their cluster, answered requests shown as waiting for a resume), and `launches`, `clusters`, `supervisor_status`, `open_prs`, `baseline_summary`, `check_value`, `smoke_markers` and `machine_status` (`tools/diagnostics/diag_ops.py`; `docs/design/diagnostic-data.md` §4); `owner_inbox` also lists PRs with CI passing and budgets near their end. They read local caches with their fetch time (stale after 30 minutes), the pulled supervisor status, the baselines' metrics files and the change fragments' markers (checked through the workbench's confined reader); `machine_status` runs `ps`, `pmset` and `git worktree list` locally and returns command names only. The caches are refreshed by `follow.py` (next PR). Self-tests DX1-DX8, DO1-DO8, MS13-MS14.
