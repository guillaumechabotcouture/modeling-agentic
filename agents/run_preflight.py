"""Preflight: a short real lead + subagent session checked before a paid run.

Phase 48 γ moved this code out of main.py unchanged; main.py imports every
name back, so `main.<name>` (the self-tests, scripts/sdk_scenarios.py) still
works. Tested by `python3 main.py --self-test`.
"""

import json
import os
import re
import shutil
import sys

from claude_agent_sdk import (
    query,
    ClaudeAgentOptions,
    AssistantMessage,
    ResultMessage,
    UserMessage,
)
from agents import enforcement
from agents.run_session import LEAD_TOOLS

REPO_ROOT = enforcement.REPO_ROOT


# Phase 21 α: preflight. Every self-test mocks the SDK, so nothing
# exercised the real CLI / model / PATH until a multi-hour run was already
# spending money. 2026-10-20_150152 ran six hours on 4.x-era models (the
# bundled CLI's alias table) with a system-Python PATH that made the
# modeler give up on the requested framework. This ~1-minute check runs a
# real lead + a real registered-style subagent and verifies both.
PREFLIGHT_IMPORTS = ("laser.core", "laser.generic", "starsim", "yaml",
                     "numpy", "scipy")


WRITER_DRAFT = "manuscript.md"   # agents/writer.py writes this; render_claims.py builds report.md


def preflight_command() -> str:
    mods = ", ".join(PREFLIGHT_IMPORTS)
    return (f"python3 -c \"import sys, {mods}; "
            f"print('PREFLIGHT_OK', sys.executable)\"")


def pinned_sdk_version() -> str | None:
    """claude-agent-sdk version pinned in requirements.txt."""
    try:
        for line in open(os.path.join(REPO_ROOT, "requirements.txt")):
            m = re.match(r"\s*claude-agent-sdk==([\w.]+)", line)
            if m:
                return m.group(1)
    except OSError:
        pass
    return None


def evaluate_preflight(text: str, model_usage: dict | None,
                       is_error: bool, subagent_wrote: bool | None = None,
                       sdk_version: str | None = None, sdk_pinned: str | None = None) -> list[str]:
    """Return problems found (empty list = pass). Pure, so it is self-tested.
    subagent_wrote: whether the probe subagent's Write of the writer's draft
    file name landed on disk (Phase 25; None = not checked)."""
    problems = []
    if sdk_pinned and sdk_version and sdk_version != sdk_pinned:
        # The bundled CLI changes behaviour between versions (Phase 25: a
        # subagent Write rule cost two full E7 runs); test what is pinned.
        problems.append(f"claude-agent-sdk {sdk_version} installed, {sdk_pinned} pinned in requirements.txt")
    if subagent_wrote is False:
        problems.append(f"a subagent could not Write {WRITER_DRAFT} (the writer's draft); "
                        f"the WRITE stage would fail")
    if is_error:
        problems.append("preflight session ended with an error result")
    # Absolute paths only: the model's prose can say "PREFLIGHT_OK line"
    # (seen on Coiled 2026-10-21), which must not count as an interpreter.
    hits = re.findall(r"PREFLIGHT_OK\s+(/\S+)", text or "")
    want = os.path.realpath(sys.executable)
    if len(hits) < 2:
        problems.append(
            f"expected the import probe to succeed in both the lead and the "
            f"subagent (saw {len(hits)} PREFLIGHT_OK line(s)); a framework "
            f"import failed or python3 is not the harness interpreter")
    bad = sorted({h for h in hits if os.path.realpath(h) != want})
    if bad:
        problems.append(f"agents' python3 is {bad}, not the harness interpreter {want}")
    used = set((model_usage or {}).keys())
    if not used:
        problems.append("ResultMessage.model_usage is empty; cannot verify models")
    else:
        # The CLI itself uses a small Haiku model for internal chores
        # (Bash command summaries, titles); no pipeline agent runs on it.
        unexpected = sorted(m for m in used
                            if not m.startswith((enforcement.MODEL_DEEP,
                                                 enforcement.MODEL_FAST,
                                                 "claude-haiku-")))
        if unexpected:
            problems.append(f"unexpected models used: {unexpected} "
                            f"(pinned: {enforcement.MODEL_DEEP}, {enforcement.MODEL_FAST})")
        if not any(m.startswith(enforcement.MODEL_DEEP) for m in used):
            problems.append(f"{enforcement.MODEL_DEEP} was not used")
    return problems


async def run_preflight(allow_cloud: bool = False) -> dict:
    from claude_agent_sdk import AgentDefinition
    cmd = preflight_command()
    probe = AgentDefinition(
        description="Preflight probe: runs one shell command and reports its output.",
        prompt="Run exactly the shell command you are given with Bash and "
               "reply with its full output verbatim. Do nothing else.",
        tools=["Bash"], model=enforcement.MODEL_DEEP, maxTurns=4)
    options = ClaudeAgentOptions(
        system_prompt="You are a harness preflight check. Follow instructions literally.",
        model=enforcement.MODEL_DEEP,
        max_buffer_size=enforcement.MAX_BUFFER_SIZE, settings=enforcement.SESSION_SETTINGS,
        agents={"preflight-probe": probe},
        tools=["Bash", "Agent"], allowed_tools=["Bash", "Agent"],
        permission_mode="bypassPermissions", setting_sources=[],
        cwd=REPO_ROOT, env=enforcement.filtered_env(dict(os.environ), allow_cloud),
        max_turns=8)
    prompt = (f"1. Run this with Bash and copy its output verbatim:\n{cmd}\n"
              f"2. Spawn the `preflight-probe` agent and give it exactly the same "
              f"command. "
              f"3. Reply with both outputs verbatim, one per line.")
    texts, result = [], None
    async for m in query(prompt=prompt, options=options):
        if isinstance(m, AssistantMessage):
            for b in m.content:
                if hasattr(b, "text"):
                    texts.append(b.text)
        elif isinstance(m, UserMessage) and isinstance(m.content, list):
            # The probe's real Bash output (tool results), not a paraphrase.
            for b in m.content:
                c = getattr(b, "content", None)
                if isinstance(c, str):
                    texts.append(c)
                elif isinstance(c, list):
                    texts.extend(x.get("text", "") for x in c if isinstance(x, dict))
        elif isinstance(m, ResultMessage):
            result = m
            if getattr(m, "result", None):
                texts.append(m.result)
    usage = getattr(result, "model_usage", None) if result else None
    wrote, write_log, write_cost = await _probe_subagent_write(allow_cloud)
    problems = evaluate_preflight("\n".join(texts), usage,
                                  bool(result is None or result.is_error), subagent_wrote=wrote,
                                  sdk_version=_sdk_version(), sdk_pinned=pinned_sdk_version())
    cost = getattr(result, "total_cost_usd", None) if result else None
    return {"ok": not problems, "problems": problems,
            "model_usage": usage,
            "cost_usd": (cost or 0) + (write_cost or 0),
            "sdk_version": _sdk_version(),
            "write_probe": None if wrote else write_log[-1500:]}


async def _probe_subagent_write(allow_cloud: bool, attempts: int = 2) -> tuple[bool, str, float]:
    """A subagent must be able to Write the writer's draft file name with the
    Write tool, as the writer does (Phase 25: the bundled CLI blocks some
    names for subagents). One job per probe, retried once, so a model that
    skips the step is not mistaken for a block; the tool results are kept
    for the log."""
    import tempfile
    from claude_agent_sdk import AgentDefinition
    log, cost = [], 0.0
    for attempt in range(attempts):
        wdir = tempfile.mkdtemp(prefix="ma_preflight_")
        wpath = os.path.join(wdir, WRITER_DRAFT)
        probe = AgentDefinition(
            description="Preflight write probe: saves one file with the Write tool.",
            prompt="Use the Write tool to save the exact text PREFLIGHT_WRITE to the file "
                   "path you are given. Then reply with the tool's result verbatim.",
            tools=["Write"], model=enforcement.MODEL_DEEP, maxTurns=3)
        options = ClaudeAgentOptions(
            system_prompt="You are a harness preflight check. Follow instructions literally.",
            model=enforcement.MODEL_DEEP, max_buffer_size=enforcement.MAX_BUFFER_SIZE, settings=enforcement.SESSION_SETTINGS,
            # The lead's real tool list: a subagent only gets tools its parent has.
            agents={"write-probe": probe}, tools=LEAD_TOOLS, allowed_tools=LEAD_TOOLS,
            permission_mode="bypassPermissions", setting_sources=[], cwd=REPO_ROOT,
            env=enforcement.filtered_env(dict(os.environ), allow_cloud), max_turns=4)
        by_subagent, sub_ids, lead_wrote, write_ok = False, set(), False, False
        async for m in query(prompt=f"Spawn the `write-probe` agent and give it this file path: {wpath}. "
                                    f"Do not write anything yourself. Reply with what it reported.",
                             options=options):
            if isinstance(m, AssistantMessage):
                for b in m.content:
                    if getattr(b, "name", None) != "Write":
                        continue
                    if getattr(m, "parent_tool_use_id", None):
                        # A Write issued inside the subagent...
                        if os.path.realpath(str((getattr(b, "input", None) or {}).get("file_path", ""))) \
                                == os.path.realpath(wpath):
                            by_subagent = True
                            sub_ids.add(getattr(b, "id", None))
                    else:
                        lead_wrote = True        # ...not the lead's own
            if isinstance(m, UserMessage) and isinstance(m.content, list):
                for b in m.content:
                    # ...and its result must not be an error (a refused Write).
                    if getattr(b, "tool_use_id", None) in sub_ids and not getattr(b, "is_error", False):
                        write_ok = True
                for b in m.content:
                    c = getattr(b, "content", None)
                    log.append(c if isinstance(c, str) else json.dumps(c, default=str)[:500])
            elif isinstance(m, ResultMessage):
                cost += m.total_cost_usd or 0
                log.append(f"[attempt {attempt + 1}] result: {(m.result or '')[:300]}")
        wrote = (by_subagent and write_ok and not lead_wrote
                 and os.path.exists(wpath) and "PREFLIGHT_WRITE" in open(wpath).read())
        if lead_wrote:
            log.append(f"[attempt {attempt + 1}] the lead used Write itself; not a subagent test")
        if not by_subagent and os.path.exists(wpath):
            log.append(f"[attempt {attempt + 1}] the file was written, but not by the subagent")
        shutil.rmtree(wdir, ignore_errors=True)
        if wrote:
            return True, "\n".join(log), cost
    return False, "\n".join(log), cost


def _sdk_version() -> str | None:
    try:
        from importlib.metadata import version
        return version("claude-agent-sdk")
    except Exception:
        return None
