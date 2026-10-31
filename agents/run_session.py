"""Session plumbing for the runner: the lead's tool set and the trace log.

Phase 48 γ moved this code out of main.py unchanged; main.py imports every
name back, so `main.<name>` (the self-tests, scripts/sdk_scenarios.py) still
works. Tested by `python3 main.py --self-test`.
"""

import json
from datetime import datetime

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    TaskStartedMessage,
    TaskProgressMessage,
    TaskNotificationMessage,
)
try:   # Phase 32 γ (Codex review): a background task can end with only an update
    from claude_agent_sdk import TaskUpdatedMessage
except ImportError:  # older SDKs
    TaskUpdatedMessage = ()


# Phase 21 α: the lead's tool set is passed as `tools=` (the base set the
# CLI exposes). `allowed_tools` alone is only a permission allowlist,
# which bypassPermissions makes moot, so before Phase 21 the lead had
# every default tool (including Edit) and used it on gate inputs.
LEAD_TOOLS = ["Agent", "Bash", "Read", "Write", "Glob", "Grep", "Skill"]


TERMINAL_TASK_STATUSES = ("completed", "failed", "killed", "stopped", "cancelled", "canceled", "error")


def log_message(message, trace_file):
    """Log a message from the lead session to console and trace."""
    if isinstance(message, TaskStartedMessage):
        print(f"  [task started] {message.description}", flush=True)
        trace_file.write(json.dumps({
            "ts": datetime.now().isoformat(),
            "type": "task_started",
            "task_id": message.task_id,
            "description": message.description,
        }) + "\n")
    elif TaskUpdatedMessage and isinstance(message, TaskUpdatedMessage):
        if str(message.status or "").lower() in TERMINAL_TASK_STATUSES:
            trace_file.write(json.dumps({"ts": datetime.now().isoformat(), "type": "task_updated",
                                         "task_id": message.task_id, "status": message.status}) + "\n")
    elif isinstance(message, TaskProgressMessage):
        tool = message.last_tool_name or ""
        if tool:
            print(f"  [progress] {message.description} | {tool}", flush=True)
    elif isinstance(message, TaskNotificationMessage):
        status_marker = "+" if message.status == "completed" else "!"
        print(f"  [{status_marker} {message.status}] {message.summary[:120]}", flush=True)
        usage = None
        if message.usage:
            u = message.usage
            if isinstance(u, dict):
                usage = u
            else:
                usage = {
                    "total_tokens": u.total_tokens,
                    "tool_uses": u.tool_uses,
                    "duration_ms": u.duration_ms,
                }
        trace_file.write(json.dumps({
            "ts": datetime.now().isoformat(),
            "type": "task_notification",
            "task_id": message.task_id,
            "status": message.status,
            "summary": message.summary,
            "usage": usage,
        }) + "\n")
    elif isinstance(message, AssistantMessage):
        for block in message.content:
            if hasattr(block, "text") and block.text:
                # Print lead's own reasoning (truncated for readability)
                text = block.text.strip()
                if text:
                    for line in text.split("\n")[:5]:
                        print(f"[lead] {line[:150]}", flush=True)
                    if text.count("\n") > 5:
                        print(f"[lead] ... ({text.count(chr(10))} lines total)", flush=True)
    elif isinstance(message, ResultMessage):
        print(f"\n[result] {message.subtype} | {message.duration_ms/1000:.0f}s"
              f" | error={message.is_error}", flush=True)
        trace_file.write(json.dumps({
            "ts": datetime.now().isoformat(),
            "type": "result",
            **result_summary(message),
        }) + "\n")
    trace_file.flush()


def result_summary(message) -> dict:
    """JSON-safe record of a ResultMessage (cost, usage, models, errors)."""
    return {
        "subtype": getattr(message, "subtype", None),
        "is_error": bool(getattr(message, "is_error", False)),
        "num_turns": getattr(message, "num_turns", None),
        "duration_ms": getattr(message, "duration_ms", None),
        "total_cost_usd": getattr(message, "total_cost_usd", None),
        "usage": getattr(message, "usage", None),
        "model_usage": getattr(message, "model_usage", None),
        "stop_reason": getattr(message, "stop_reason", None),
        "errors": getattr(message, "errors", None),
        "session_id": getattr(message, "session_id", None),
    }


def result_is_success(message) -> bool:
    """Phase 21 α: a ResultMessage ends the stream for success AND for
    error_max_turns / error_during_execution. Only subtype "success"
    without is_error counts; everything else goes down the retry path."""
    return (message is not None
            and not getattr(message, "is_error", False)
            and getattr(message, "subtype", None) == "success")


def is_policy_block(error_str: str) -> bool:
    # Phase 21 α: the bare substring "violate" matched ordinary errors
    # (e.g. "constraint violated") and ended runs without retry.
    low = error_str.lower()
    return "usage policy" in low or "acceptable use policy" in low
