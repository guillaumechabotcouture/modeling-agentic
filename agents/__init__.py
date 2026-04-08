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

MAX_FIGURE_PIXELS = 10_000_000  # 10MP -- anything larger is likely a bug

def _check_figure_size(path: str, stage_name: str) -> None:
    """Warn and resize oversized PNG files."""
    import os
    if not os.path.exists(path) or not path.endswith(".png"):
        return
    try:
        from PIL import Image
        Image.MAX_IMAGE_PIXELS = 2_000_000_000  # Allow opening for inspection
        img = Image.open(path)
        pixels = img.size[0] * img.size[1]
        if pixels > MAX_FIGURE_PIXELS:
            # Resize to reasonable dimensions preserving aspect ratio
            ratio = (MAX_FIGURE_PIXELS / pixels) ** 0.5
            new_size = (int(img.size[0] * ratio), int(img.size[1] * ratio))
            print(
                f"[{stage_name}] WARNING: {os.path.basename(path)} is "
                f"{img.size[0]}x{img.size[1]} ({pixels/1e6:.0f}MP). "
                f"Resizing to {new_size[0]}x{new_size[1]}.",
                flush=True,
            )
            img = img.resize(new_size, Image.LANCZOS)
            img.save(path)
        img.close()
    except Exception:
        pass  # Don't let figure checking crash the pipeline


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

    async def post_tool_hook(input_data, tool_use_id, context):
        """Check written PNG files for oversized figures."""
        import os
        tool_name = input_data.get("tool_name", "")
        tool_input = input_data.get("tool_input", {})
        # Check after Bash or Write -- figure might have been created
        if tool_name in ("Bash", "Write"):
            file_path = tool_input.get("file_path", "")
            if file_path.endswith(".png"):
                _check_figure_size(file_path, stage_name)
        # Also scan figures/ after any Bash (model scripts save PNGs)
        if tool_name == "Bash":
            fig_dir = os.path.join(run_path, "figures")
            if os.path.isdir(fig_dir):
                for fname in os.listdir(fig_dir):
                    if fname.endswith(".png"):
                        _check_figure_size(
                            os.path.join(fig_dir, fname), stage_name
                        )
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
                trace_file, agents, start_time, pre_tool_hook, post_tool_hook,
                tool_count,
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
    trace_file, agents, start_time, pre_tool_hook, post_tool_hook,
    tool_count,
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
                "PostToolUse": [
                    HookMatcher(matcher=None, hooks=[post_tool_hook])
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


async def run_critique_with_fallback(
    system_prompt: str,
    prompt: str,
    tools: list[str],
    run_path: str,
    stage_name: str,
    trace_file,
    output_filename: str,
    start_time: datetime | None = None,
) -> None:
    """Run a critique agent with cascading fallback:
    1. Claude via Agent SDK (default)
    2. Claude via Agent SDK (sonnet model)
    3. OpenAI GPT via direct API call
    """
    import os, glob as glob_mod

    start_time = start_time or datetime.now()
    output_path = os.path.join(run_path, output_filename)

    # Attempt 1: Claude (default model) via Agent SDK
    try:
        await run_agent(
            system_prompt=system_prompt,
            prompt=prompt,
            tools=tools,
            run_path=run_path,
            stage_name=stage_name,
            trace_file=trace_file,
            start_time=start_time,
        )
        if os.path.exists(output_path):
            return  # Success
    except Exception as e:
        print(f"[{stage_name}] Claude default failed: {e}", flush=True)

    # Attempt 2: Claude (sonnet) -- different model may have different filter
    try:
        print(f"[{stage_name}] Retrying with sonnet...", flush=True)
        import time
        time.sleep(3)
        # Agent SDK doesn't expose model selection per-query easily,
        # so we skip to the OpenAI fallback
    except Exception:
        pass

    # Attempt 3: OpenAI GPT fallback
    openai_key = os.environ.get("OPENAI_API_KEY")
    if not openai_key:
        print(f"[{stage_name}] No OPENAI_API_KEY set, cannot fall back to GPT.", flush=True)
        return

    print(f"[{stage_name}] Falling back to OpenAI GPT...", flush=True)
    trace_file.write(json.dumps({
        "ts": datetime.now().isoformat(),
        "type": "fallback",
        "stage": stage_name,
        "provider": "openai",
    }) + "\n")
    trace_file.flush()

    try:
        from openai import OpenAI
        client = OpenAI()

        # Gather file contents that the critique needs
        file_contents = []
        for pattern in ["*.md", "*.py"]:
            for fpath in sorted(glob_mod.glob(os.path.join(run_path, pattern))):
                fname = os.path.basename(fpath)
                if fname == output_filename:
                    continue  # Don't read our own output
                try:
                    with open(fpath) as f:
                        content = f.read()
                    if len(content) > 50000:
                        content = content[:50000] + "\n\n[TRUNCATED]"
                    file_contents.append(f"=== {fname} ===\n{content}")
                except Exception:
                    pass

        # List figures
        fig_dir = os.path.join(run_path, "figures")
        if os.path.isdir(fig_dir):
            figs = os.listdir(fig_dir)
            file_contents.append(f"=== figures/ ({len(figs)} files) ===\n" + "\n".join(figs))

        context = "\n\n".join(file_contents)

        user_message = (
            f"{prompt}\n\n"
            f"--- FILES IN RUN DIRECTORY ---\n\n{context}"
        )

        response = client.chat.completions.create(
            model="gpt-5.4",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            max_tokens=8000,
        )

        result_text = response.choices[0].message.content

        with open(output_path, "w") as f:
            f.write(result_text)

        elapsed = (datetime.now() - start_time).total_seconds()
        print(f"[{stage_name}] GPT fallback complete ({elapsed:.0f}s)", flush=True)
        trace_file.write(json.dumps({
            "ts": datetime.now().isoformat(),
            "type": "fallback_complete",
            "stage": stage_name,
            "provider": "openai",
            "model": "gpt-5.4",
        }) + "\n")
        trace_file.flush()

    except Exception as e:
        print(f"[{stage_name}] OpenAI fallback failed: {e}", flush=True)


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

        # Look for verdict -- handle **bold**, whitespace, and multiline
        verdict_match = re.search(
            r"##\s*Verdict[:\s]*\*{0,2}\s*(PASS|REVISE|ACCEPT)\s*\*{0,2}",
            content, re.IGNORECASE
        )
        if not verdict_match:
            # Fallback: search anywhere for REVISE/ACCEPT as standalone word
            if re.search(r"\bREVISE\b", content[:500]):
                verdict = "REVISE"
            else:
                continue
        else:
            verdict = verdict_match.group(1).upper()

        if verdict == "PASS" or verdict == "ACCEPT":
            continue

        # Look for target stage -- handle **bold** and various formats
        target_match = re.search(r"##?\s*Target[:\s]*\*{0,2}\s*(\w+)", content, re.IGNORECASE)
        if not target_match:
            target_match = re.search(r"Target\s*(?:Stage|stage)?[:\s]+\*{0,2}(\w+)", content, re.IGNORECASE)
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
