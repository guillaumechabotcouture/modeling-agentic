#!/usr/bin/env python3
"""
Phase 28 — SDK scenario harness: how the real Claude CLI behaves when it is
killed, resumed, or capped, measured directly in about a minute per scenario
instead of through a 25-minute whole-pipeline smoke.

Each scenario runs a tiny real conversation (cheap model, one Bash tool),
does one thing to it at a known point, and records what the SDK reported.
It calls the harness's own functions (main.reconcile_conversation_cost,
charge_session) so the result is what a real run would record.

    python3 scripts/sdk_scenarios.py run [name ...]   # needs ANTHROPIC_API_KEY; ~cents each
    python3 scripts/sdk_scenarios.py list
    python3 scripts/sdk_scenarios.py --self-test      # no API calls

Run it where real runs run (a Coiled VM with the pinned SDK): CLI behaviour
differs between versions, and a nested CLI inside a Claude Code session has
no network.

Scenarios:
  resume_cumulative     a finished turn, then a resumed turn: is the second
                        report cumulative (>= the first)?
  readback_after_crash  SIGKILL the CLI mid-tool-call, then read the cost back
                        by resuming (immediately, and again after 20 s)
  crash_before_init     SIGKILL the CLI before it reports a session id
  budget_cap            max_budget_usd far below one turn's cost: how does the
                        session end and what cost does it report?
  hook_context          (Phase 31) do PostToolUse notes reach the model: the
                        turn warning to a subagent, the budget note to the lead?
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

MODEL = os.environ.get("SCENARIO_MODEL", "claude-haiku-5-5")


def _options(**kw):
    from claude_agent_sdk import ClaudeAgentOptions
    from agents import enforcement
    base = dict(model=MODEL, tools=["Bash"], allowed_tools=["Bash"], permission_mode="bypassPermissions",
                setting_sources=[], cwd=REPO, max_turns=6, max_buffer_size=enforcement.MAX_BUFFER_SIZE,
                env=enforcement.filtered_env(dict(os.environ), False),
                system_prompt="You are a test fixture. Follow instructions literally and briefly.")
    base.update(kw)
    return ClaudeAgentOptions(**base)


def _cli_pids() -> list[int]:
    """The SDK's CLI subprocesses (children of this process)."""
    import psutil
    me = psutil.Process()
    return [c.pid for c in me.children(recursive=True)
            if "claude" in " ".join(c.cmdline() or []).lower() and "_bundled" in " ".join(c.cmdline() or [])]


def _summ(m) -> dict | None:
    if m is None:
        return None
    return {"subtype": getattr(m, "subtype", None), "is_error": getattr(m, "is_error", None),
            "cost": getattr(m, "total_cost_usd", None), "session_id": getattr(m, "session_id", None),
            "num_turns": getattr(m, "num_turns", None)}


async def _converse(prompt, options, kill_when=None, query_fn=None, kill_fn=None):
    """Run one query; optionally SIGKILL the CLI when kill_when(event, t)
    returns True. Returns (session_id, result, error, killed, events)."""
    from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage
    if query_fn is None:
        from claude_agent_sdk import query as query_fn
    kill_fn = kill_fn or (lambda: [os.kill(p, signal.SIGKILL) or p for p in _cli_pids()])
    sid, result, err, killed, events, t0 = None, None, None, None, [], time.time()
    try:
        async for m in query_fn(prompt=prompt, options=options):
            t = time.time() - t0
            if isinstance(m, SystemMessage) and m.subtype == "init":
                sid = (m.data or {}).get("session_id") or sid
                events.append(("init", round(t, 1)))
            elif isinstance(m, AssistantMessage):
                tools = [getattr(b, "name", None) for b in m.content if getattr(b, "name", None)]
                events.append(("assistant", round(t, 1), tools))
            elif isinstance(m, ResultMessage):
                result = m
                sid = m.session_id or sid
                events.append(("result", round(t, 1)))
            if killed is None and kill_when and kill_when(events[-1] if events else None, t):
                killed = kill_fn()
                events.append(("killed", round(t, 1), killed))
    except Exception as e:  # noqa: BLE001 — a killed CLI surfaces here
        err = f"{type(e).__name__}: {str(e)[:200]}"
    return sid, result, err, killed, events


async def resume_cumulative(query_fn=None):
    sid, r1, err, _, ev = await _converse("Reply with the word ONE.", _options(), query_fn=query_fn)
    _, r2, err2, _, ev2 = await _converse("Reply with the word TWO.", _options(resume=sid), query_fn=query_fn)
    c1, c2 = (r1 and r1.total_cost_usd), (r2 and r2.total_cost_usd)
    return {"first": _summ(r1), "resumed": _summ(r2), "errors": [err, err2],
            "cumulative": bool(c1 and c2 and c2 >= c1 and r2.session_id == sid)}


async def readback_after_crash(query_fn=None, kill_fn=None, delay_s=20):
    import main
    opts = _options()

    def when(ev, t):      # first Bash call issued: the CLI is mid-tool-call
        return bool(ev and ev[0] == "assistant" and "Bash" in (ev[2] or []))
    sid, result, err, killed, ev = await _converse(
        "Run `sleep 120` with the Bash tool, then reply DONE.", opts, kill_when=when,
        query_fn=query_fn, kill_fn=kill_fn)
    now = await main.reconcile_conversation_cost(opts, sid, query_fn=query_fn) if sid else None
    raw_now = await _raw_readback(opts, sid, query_fn) if sid else None
    await asyncio.sleep(delay_s if query_fn is None else 0)
    later = await main.reconcile_conversation_cost(opts, sid, query_fn=query_fn) if sid else None
    return {"session_id": sid, "killed": killed, "crash_error": err, "result_before_kill": _summ(result),
            "readback_now": _summ(now), "readback_now_raw": raw_now, "readback_later": _summ(later),
            "events": ev}


async def _raw_readback(opts, sid, query_fn=None):
    """What the read-back query actually returned, before the harness's checks."""
    import dataclasses
    from claude_agent_sdk import ResultMessage
    if query_fn is None:
        from claude_agent_sdk import query as query_fn
    probe = dataclasses.replace(opts, resume=sid, max_turns=1, tools=[], allowed_tools=[])
    try:
        async for m in query_fn(prompt="Reply with the single word OK.", options=probe):
            if isinstance(m, ResultMessage):
                return _summ(m) | {"result": (m.result or "")[:120]}
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {str(e)[:200]}"}
    return None


async def crash_before_init(query_fn=None, kill_fn=None):
    sid, result, err, killed, ev = await _converse(
        "Reply with the word HELLO.", _options(), kill_when=lambda ev, t: True, query_fn=query_fn, kill_fn=kill_fn)
    return {"session_id": sid, "killed": killed, "crash_error": err, "result": _summ(result), "events": ev}


async def budget_cap(query_fn=None):
    sid, result, err, _, ev = await _converse("Write three sentences about measles.",
                                              _options(max_budget_usd=0.00001), query_fn=query_fn)
    return {"result": _summ(result), "error": err, "events": ev}


async def hook_context(query_fn=None):
    """Phase 31: do the harness's PostToolUse notes reach the model? A lead
    spawns one subagent with a 12-turn cap that runs Bash repeatedly; the
    subagent should see the turn warning, and the lead the budget note."""
    import tempfile
    from datetime import datetime
    from claude_agent_sdk import AgentDefinition
    from agents import create_hooks
    run = tempfile.mkdtemp(prefix="ma_hookctx_")
    trace = open(os.path.join(run, "trace.jsonl"), "a")
    hooks = create_hooks(run, trace, datetime.now(), registered_agents={"worker"}, max_turns={"worker": 12},
                         budget_status=lambda: {"spent": 1.0, "cap": 10.0, "reserve": 1.0})
    worker = AgentDefinition(
        description="Test worker", tools=["Bash"], model="haiku", maxTurns=12,
        prompt=("Run `echo step N` with the Bash tool for N = 1, 2, 3, ... up to 11, ONE call per turn. "
                "If any tool result contains text starting with [harness], stop at once and reply with "
                "HARNESS-SEEN followed by the first six words after [harness]."))
    # A subagent may only use tools its parent has, so the lead gets Bash too (told not to use it).
    opts = _options(tools=["Agent", "Bash"], allowed_tools=["Agent", "Bash"], agents={"worker": worker}, hooks=hooks,
                    max_turns=8,
                    system_prompt=("You are a test fixture. Do not use Bash yourself. "
                                   "Spawn the worker agent once with the task 'start'. "
                                   "When it returns, repeat its last line verbatim. If any tool result you received "
                                   "contains text starting with [harness] Budget, also end your reply with BUDGET-SEEN."))
    sid, result, err, _, ev = await _converse("Begin.", opts, query_fn=query_fn)
    trace.close()
    text = (getattr(result, "result", None) or "") if result else ""
    warned = sum(1 for line in open(os.path.join(run, "trace.jsonl")) if '"turn_warning"' in line)
    return {"result": _summ(result), "error": err, "text": text[-300:], "turn_warnings_issued": warned,
            "subagent_saw_warning": "HARNESS-SEEN" in text, "lead_saw_budget": "BUDGET-SEEN" in text,
            "events": ev[-6:]}


SCENARIOS = {"resume_cumulative": resume_cumulative, "readback_after_crash": readback_after_crash,
             "crash_before_init": crash_before_init, "budget_cap": budget_cap, "hook_context": hook_context}


def run(names: list[str]) -> dict:
    out = {"sdk": None, "model": MODEL}
    try:
        from importlib.metadata import version
        out["sdk"] = version("claude-agent-sdk")
    except Exception:  # noqa: BLE001
        pass
    for n in names or list(SCENARIOS):
        t0 = time.time()
        try:
            res = asyncio.run(SCENARIOS[n]())
        except Exception as e:  # noqa: BLE001 — report, continue with the others
            res = {"harness_error": f"{type(e).__name__}: {e}"}
        out[n] = res | {"seconds": round(time.time() - t0, 1)}
    return out


def _self_test() -> int:
    """Scenario plumbing against a fake SDK stream (no API calls)."""
    from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, ToolUseBlock
    fails = []

    def fake(kill_after_bash=False):
        state = {"killed": False}

        async def q(prompt, options):
            sid = getattr(options, "resume", None) or "S1"
            yield SystemMessage(subtype="init", data={"session_id": sid})
            if "sleep" in prompt:
                yield AssistantMessage(content=[ToolUseBlock("t1", "Bash", {"command": "sleep 120"})], model="m")
                if state["killed"]:
                    raise RuntimeError("Command failed with exit code -9")
            yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                                num_turns=1, session_id=sid, total_cost_usd=0.01 if "ONE" in prompt else 0.02)

        def kill():
            state["killed"] = True
            return [123]
        return q, kill
    q, kill = fake()
    r = asyncio.run(resume_cumulative(query_fn=q))
    if not r["cumulative"]:
        fails.append(f"SC1 resume_cumulative reads two reports: {r}")
    q, kill = fake()
    r = asyncio.run(readback_after_crash(query_fn=q, kill_fn=kill))
    if r["killed"] != [123] or "exit code -9" not in (r["crash_error"] or "") or r["session_id"] != "S1":
        fails.append(f"SC2 kill lands at the first Bash call and the crash is recorded: {r}")
    if not (r["readback_now"] and r["readback_now"]["cost"] == 0.02 and r["readback_now_raw"]):
        fails.append(f"SC3 the read-back goes through the harness function and is also recorded raw: {r}")
    q, kill = fake()
    r = asyncio.run(crash_before_init(query_fn=q, kill_fn=kill))
    if r["killed"] != [123]:
        fails.append(f"SC4 crash_before_init kills at the first event: {r}")
    if fails:
        print("SELF-TEST FAILURES:", *fails, sep="\n  - ")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main_cli() -> int:
    ap = argparse.ArgumentParser(description="Real-SDK scenarios (crash, resume, cap)")
    ap.add_argument("cmd", nargs="?", choices=["run", "list"])
    ap.add_argument("names", nargs="*")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return _self_test()
    if a.cmd == "list":
        print("\n".join(SCENARIOS))
        return 0
    if a.cmd == "run":
        unknown = [n for n in a.names if n not in SCENARIOS]
        if unknown:
            ap.error(f"unknown scenarios: {unknown}")
        print(json.dumps(run(a.names), indent=1, default=str))
        return 0
    ap.error("usage: sdk_scenarios.py run [name ...] | list | --self-test")
    return 2


if __name__ == "__main__":
    sys.exit(main_cli())
