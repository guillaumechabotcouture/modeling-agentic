# Diagnostics for Claude Desktop

A read-only MCP server that lets Claude Desktop answer questions about runs, following the contract in
`docs/design/diagnostic-data.md`. Ask it, for example, "what is run X doing now?", "why did it pause?" or "which
finding cost rounds?".

- `mcp_server.py`: the server (MCP over stdio). No network calls and no tool that changes anything.
- `diag_runs.py`: the run records it returns, read through the review workbench's confined reader.

## Add it to Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json`. Use the repository's virtual environment,
which has PyYAML, and absolute paths:

```json
{
  "mcpServers": {
    "modeling-agentic-diagnostics": {
      "command": "/Users/<you>/modeling-agentic/.venv/bin/python",
      "args": ["/Users/<you>/modeling-agentic/tools/diagnostics/mcp_server.py"],
      "env": {
        "MA_DIAG_RUNS": "/Users/<you>/modeling-agentic/runs",
        "MA_DIAG_MIRROR": "/Users/<you>/modeling-agentic-keep/mirror",
        "MA_DIAG_HOME": "/Users/<you>/modeling-agentic-keep/diagnostics"
      }
    }
  }
}
```

Restart Claude Desktop. The tools appear under the server's name, and Claude Desktop asks before each call.

## What it reads and writes

- **Reads:** run folders under `MA_DIAG_RUNS` and `MA_DIAG_MIRROR`. Anything under a `_truth` or `evals_private`
  folder is skipped. Every read goes through `lib.safe_io` with no link followed. `run_artifact` returns only
  allow-listed harness records.
- **Writes:** two files under `MA_DIAG_HOME`, both outside the repository. `usage.jsonl` gets one line per tool
  call: the tool, its arguments in short, how many records it returned, and any error. `feedback.jsonl` gets
  what `note_feedback` records. A failed write never fails a call.
- **Privacy:** results can hold run values, which is fine on this machine. Never paste them into the public
  repository. Credential-like strings are redacted from every result.

## Tools

- Runs: `list_runs`, `run_status`, `run_events`, `run_findings`, `run_spend`, `run_compare`, `run_artifact`.
- Operations: `launches`, `clusters`, `supervisor_status`, `open_prs`, `baseline_summary`, `check_value`,
  `smoke_markers`, `machine_status`.
- The owner: `owner_inbox` (paused runs, PRs with CI passing, budgets near their end) and `note_feedback`.

## Caches

The operations tools read local caches under `MA_DIAG_HOME/cache` and the pulled supervisor status. Each tool says
when a cache is missing or older than 30 minutes. `follow.py`, in the next PR, refreshes them and mirrors cloud
runs. Save a check-value table for `check_value` with
`scripts/harness_metrics.py --check-value <runs> --json ~/modeling-agentic-keep/diagnostics/cache/check_value/<baseline>.json`.
