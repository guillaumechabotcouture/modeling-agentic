---
phase: "Diag"
commit: "MCP α"
---

## Changelog

**A read-only diagnostics server for Claude Desktop (runs).** `tools/diagnostics/mcp_server.py`: MCP over stdio (JSON-RPC, standard library), no network calls and no tool that changes anything (`docs/design/diagnostic-data.md` §8). Tools `list_runs`, `run_status` (state, round, running task and elapsed, heartbeat age, ETA, spend against cap, open findings, the owner question of a pause, stale flag), `run_events` (events.jsonl, or rebuilt from history and journal for older runs, marked inferred), `run_findings` (rounds open, fix tasks with spend inferred from the journal), `run_spend` (by stage and task, with interrupted charges, and the compute ledger) and `note_feedback`. Every result is the versioned envelope `diag.<tool>/1` with sources, notes and record/inferred/unavailable flags; credential-like strings are redacted. Reads go through the review workbench's confined reader (`tools/diagnostics/diag_runs.py`); `_truth` and `evals_private` are never discovered. Its only writes are `usage.jsonl` and `feedback.jsonl` in `~/modeling-agentic-keep/diagnostics`, through `lib.safe_io`, never failing a call. Self-tests DR1-DR17, MS1-MS12; Claude Desktop set-up in `tools/diagnostics/README.md`.
