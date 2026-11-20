"""The tool set the live preflight (agents/run_preflight.py, `main.py --preflight-only`) gives its probe session.

This was the v1 lead's tool set. The lead's trace log and result helpers were removed with pipeline v1
(owner, 2026-11-20; docs/design/v1-retirement.md)."""

# Phase 21 α: the lead's tool set is passed as `tools=` (the base set the
# CLI exposes). `allowed_tools` alone is only a permission allowlist,
# which bypassPermissions makes moot, so before Phase 21 the lead had
# every default tool (including Edit) and used it on gate inputs.
LEAD_TOOLS = ["Agent", "Bash", "Read", "Write", "Glob", "Grep", "Skill"]
