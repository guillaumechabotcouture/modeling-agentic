"""Shared infrastructure for pipeline agents."""

import json
from datetime import datetime

from claude_agent_sdk import (
    query,
    ClaudeAgentOptions,
    AgentDefinition,
    AssistantMessage,
    ResultMessage,
    HookMatcher,
)

# Stage ordering for the pipeline state machine
STAGES = ["plan", "data", "model", "analyze", "critique", "write"]


def stage_index(name: str) -> int:
    return STAGES.index(name)


async def run_agent(
    system_prompt: str,
    prompt: str,
    tools: list[str],
    run_path: str,
    stage_name: str,
    trace_file,
    agents: dict | None = None,
    start_time: datetime | None = None,
) -> None:
    """Run a single pipeline stage as a query() call."""

    start_time = start_time or datetime.now()
    tool_count = 0

    def format_tool(name: str, input_data: dict) -> str:
        if name == "WebSearch":
            return f'WebSearch: "{input_data.get("query", "")}"'
        elif name == "WebFetch":
            return f"WebFetch: {input_data.get('url', '')[:80]}"
        elif name == "Bash":
            return f"Bash: {input_data.get('command', '')[:80]}"
        elif name == "Write":
            return f"Write: {input_data.get('file_path', '')}"
        elif name == "Edit":
            return f"Edit: {input_data.get('file_path', '')}"
        elif name == "Read":
            return f"Read: {input_data.get('file_path', '')}"
        elif name == "Glob":
            return f"Glob: {input_data.get('pattern', '')}"
        elif name == "Grep":
            return f'Grep: "{input_data.get("pattern", "")}"'
        elif name in ("Agent", "Task"):
            return f"Agent: {input_data.get('subagent_type', input_data.get('description', ''))}"
        return name

    async def pre_tool_hook(input_data, tool_use_id, context):
        nonlocal tool_count
        tool_count += 1
        tool_name = input_data.get("tool_name", "unknown")
        tool_input = input_data.get("tool_input", {})
        summary = format_tool(tool_name, tool_input)
        elapsed = (datetime.now() - start_time).total_seconds()
        print(f"[{stage_name:>8} {elapsed:6.0f}s | #{tool_count}] {summary}", flush=True)
        trace_file.write(json.dumps({
            "ts": datetime.now().isoformat(),
            "elapsed_s": elapsed,
            "stage": stage_name,
            "type": "tool_use",
            "tool": tool_name,
            "summary": summary,
        }) + "\n")
        trace_file.flush()
        return {}

    trace_file.write(json.dumps({
        "ts": datetime.now().isoformat(),
        "type": "stage_start",
        "stage": stage_name,
    }) + "\n")
    trace_file.flush()

    print(f"\n--- {stage_name.upper()} ---", flush=True)

    # Retry up to 2 times on CLI errors
    max_retries = 2
    for attempt in range(max_retries + 1):
        try:
            return await _run_agent_inner(
                system_prompt, prompt, tools, run_path, stage_name,
                trace_file, agents, start_time, pre_tool_hook, tool_count,
            )
        except Exception as e:
            if attempt < max_retries and "I/O operation" in str(e):
                import time
                print(f"[{stage_name}] CLI error, retrying ({attempt+1}/{max_retries})...", flush=True)
                time.sleep(3)
            else:
                raise


async def _run_agent_inner(
    system_prompt, prompt, tools, run_path, stage_name,
    trace_file, agents, start_time, pre_tool_hook, tool_count,
):
    async for message in query(
        prompt=prompt,
        options=ClaudeAgentOptions(
            system_prompt=system_prompt,
            allowed_tools=tools,
            permission_mode="bypassPermissions",
            setting_sources=["project"],
            agents=agents or {},
            hooks={
                "PreToolUse": [
                    HookMatcher(matcher=None, hooks=[pre_tool_hook])
                ],
            },
        ),
    ):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if hasattr(block, "text") and block.text:
                    print(block.text, flush=True)
        elif isinstance(message, ResultMessage):
            print(f"[{stage_name}] Done: {message.subtype}", flush=True)

    trace_file.write(json.dumps({
        "ts": datetime.now().isoformat(),
        "type": "stage_complete",
        "stage": stage_name,
        "tool_count": tool_count,
    }) + "\n")
    trace_file.flush()

    print(f"--- {stage_name.upper()} complete ({tool_count} tools) ---", flush=True)


def parse_critique_target(run_path: str) -> str:
    """Read all critique files and return the earliest target stage.
    Returns 'accept' if all pass, or a stage name to jump back to."""
    import os
    import re

    earliest = "accept"
    for fname in ["critique_methods.md", "critique_domain.md", "critique_presentation.md"]:
        path = os.path.join(run_path, fname)
        if not os.path.exists(path):
            continue
        with open(path) as f:
            content = f.read()

        # Look for verdict
        verdict_match = re.search(r"##\s*Verdict:\s*(PASS|REVISE|ACCEPT)", content, re.IGNORECASE)
        if not verdict_match or verdict_match.group(1).upper() == "PASS":
            continue

        # Look for target stage
        target_match = re.search(r"##\s*Target:\s*(\w+)", content, re.IGNORECASE)
        if target_match:
            target = target_match.group(1).lower()
            # Map common names
            target = {"model": "model", "data": "data", "plan": "plan",
                       "hypotheses": "plan", "planner": "plan",
                       "modeler": "model", "analyse": "analyze",
                       "analyze": "analyze"}.get(target, "model")
        else:
            target = "model"  # default: back to modeler

        # Keep the earliest stage
        if earliest == "accept" or stage_index(target) < stage_index(earliest):
            earliest = target

    return earliest
