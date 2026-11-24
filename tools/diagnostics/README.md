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
  folder is skipped. Every read goes through `lib.safe_io` with no link followed.
- **Writes:** two files under `MA_DIAG_HOME`, both outside the repository. `usage.jsonl` gets one line per tool
  call: the tool, its arguments in short, how many records it returned, and any error. `feedback.jsonl` gets
  what `note_feedback` records. A failed write never fails a call.
- **Privacy:** results can hold run values, which is fine on this machine. Never paste them into the public
  repository. Credential-like strings are redacted from every result.

## Tools

`list_runs`, `run_status`, `run_events`, `run_findings`, `run_spend` and `note_feedback`. The next PR adds
`run_artifact`, `run_compare`, `owner_inbox`, the operations tools and `follow.py`.
