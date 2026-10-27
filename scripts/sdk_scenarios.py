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
  cache_ttl             (Phase 31) are the lead's and a subagent's cache writes
                        1-hour (CLAUDE_CODE_*PROMPT_CACHE_TTL from cache_env)?
  round_plan            (Phase 32) does a real lead write a valid round plan
                        when the harness refuses work without one, hand each
                        planned fix to its agent, get a critic's fix check
                        (real fix-check prompt), send the unfixed item back
                        once, and only then run the full critique?
  brief_signoff         (Phase 33) does a real lead stop for the owner's
                        sign-off when the hook refuses work, end its turn with
                        AWAITING_SIGNOFF, and continue once resumed with the
                        owner's answers?
  done_when_tests       (Phase 34) does the real red-team critic (its prompt
                        and model, web off) write done_when tests that are
                        valid, fail on planted problems and pass once fixed?
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
sys.path.insert(0, HERE)

MODEL = os.environ.get("SCENARIO_MODEL", "claude-haiku-5-5")


def _options(**kw):
    from claude_agent_sdk import ClaudeAgentOptions
    from agents import enforcement
    base = dict(model=MODEL, tools=["Bash"], allowed_tools=["Bash"], permission_mode="bypassPermissions",
                setting_sources=[], cwd=REPO, max_turns=6, max_buffer_size=enforcement.MAX_BUFFER_SIZE,
                settings=enforcement.SESSION_SETTINGS,
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


async def _converse(prompt, options, kill_when=None, query_fn=None, kill_fn=None, trace=None):
    """Run one query; optionally SIGKILL the CLI when kill_when(event, t)
    returns True. Returns (session_id, result, error, killed, events)."""
    from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, UserMessage
    try:
        from claude_agent_sdk import TaskNotificationMessage
    except ImportError:  # older SDKs
        TaskNotificationMessage = ()
    if query_fn is None:
        from claude_agent_sdk import query as query_fn
    kill_fn = kill_fn or (lambda: [os.kill(p, signal.SIGKILL) or p for p in _cli_pids()])
    sid, result, err, killed, events, t0 = None, None, None, None, [], time.time()
    try:
        async for m in query_fn(prompt=prompt, options=options):
            t = time.time() - t0
            if TaskNotificationMessage and isinstance(m, TaskNotificationMessage):
                # As main.py records it: the only end-of-agent signal for an agent
                # cut off at its turn limit (no SubagentStop), which the hooks read.
                events.append(("task_notification", round(t, 1), m.task_id))
                if trace is not None:
                    trace.write(json.dumps({"ts": datetime.now().isoformat(), "type": "task_notification",
                                            "task_id": m.task_id, "status": m.status}) + "\n")
                    trace.flush()
            elif isinstance(m, SystemMessage) and m.subtype == "init":
                sid = (m.data or {}).get("session_id") or sid
                events.append(("init", round(t, 1)))
            elif isinstance(m, AssistantMessage):
                tools = [getattr(b, "name", None) for b in m.content if getattr(b, "name", None)]
                events.append(("assistant", round(t, 1), tools))
            elif isinstance(m, UserMessage) and isinstance(m.content, list):
                # What a tool (including a subagent) handed back, so a stalled
                # scenario shows what the lead saw.
                for b in m.content:
                    if getattr(b, "tool_use_id", None):
                        c = b.content if isinstance(b.content, str) else json.dumps(b.content, default=str)
                        events.append(("tool_result", round(t, 1), (c or "")[:300]))
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


async def cache_ttl(query_fn=None):
    """Phase 31: do the lead's and a subagent's cache writes use the 1-hour
    TTL (enforcement.cache_env)? Read from the CLI transcripts, which record
    usage.cache_creation.ephemeral_1h_input_tokens per request."""
    import glob
    from claude_agent_sdk import AgentDefinition
    worker = AgentDefinition(description="Test worker", tools=["Bash"], model="haiku", maxTurns=3,
                             prompt="Run `echo hi` once with the Bash tool, then reply DONE.")
    opts = _options(tools=["Agent", "Bash"], allowed_tools=["Agent", "Bash"], agents={"worker": worker},
                    system_prompt="You are a test fixture. Do not use Bash yourself. Spawn the worker agent once "
                                  "with the task 'start', then reply OK.")
    sid, result, err, _, ev = await _converse("Begin.", opts, query_fn=query_fn)
    found = {"lead": {"1h": 0, "5m": 0}, "subagent": {"1h": 0, "5m": 0}}
    for path in glob.glob(os.path.expanduser(f"~/.claude/projects/*/{sid}.jsonl")) + \
            glob.glob(os.path.expanduser(f"~/.claude/projects/*/{sid}/subagents/*.jsonl")):
        who = "subagent" if "/subagents/" in path else "lead"
        for line in open(path, errors="replace"):
            try:
                cc = ((json.loads(line).get("message") or {}).get("usage") or {}).get("cache_creation") or {}
            except (ValueError, AttributeError):
                continue
            found[who]["1h"] += cc.get("ephemeral_1h_input_tokens") or 0
            found[who]["5m"] += cc.get("ephemeral_5m_input_tokens") or 0
    return {"result": _summ(result), "error": err, "cache_writes": found,
            "env": {k: (opts.env or {}).get(k) for k in ("CLAUDE_CODE_PROMPT_CACHE_TTL", "CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL")},
            "lead_1h": found["lead"]["1h"] > 0 and found["lead"]["5m"] == 0,
            "subagent_1h": found["subagent"]["1h"] > 0 and found["subagent"]["5m"] == 0}


async def round_plan(query_fn=None):
    """Phase 32: the open-problem list, round plan and fix check with real
    models. The lead gets a short version of its STAGE 7 instructions and the
    real hooks; the critic gets the real fix-check note. The critic is told to
    find R-005 only partly fixed, so the lead must send it back once."""
    import tempfile
    from datetime import datetime
    from claude_agent_sdk import AgentDefinition
    from agents import create_hooks
    import open_problems as op
    run = tempfile.mkdtemp(prefix="ma_roundplan_")
    open(os.path.join(run, "validator_history.jsonl"), "w").write(
        "".join(json.dumps({"round": r, "action": "PATCH_OR_RETHINK", "findings": []}) + "\n" for r in (1, 2)))
    doc = {"round": 2, "max_rounds": 8, "items": [
        {"id": "ci_degenerate", "source": "rigor-artifacts", "severity": "HIGH", "target_stage": "MODEL",
         "first_seen_round": 1, "blocks_in": 0, "scope_declarable": False, "fix_check": False,
         "claim": "total_cost_usd 95% CI is zero width after 230 draws", "fix_requires": "", "status": ""},
        {"id": "R-005", "source": "critique-redteam", "severity": "MEDIUM", "target_stage": "ANALYZE",
         "first_seen_round": 1, "blocks_in": 1, "scope_declarable": True, "fix_check": True,
         "claim": "Headline reports 64M cases averted with no qualifier for the 0.8 re-anchoring",
         "fix_requires": "Carry the re-anchored cases range into the Summary", "status": "Narrowed, still present."},
        {"id": "R-017", "source": "critique-redteam", "severity": "MEDIUM", "target_stage": "DATA",
         "first_seen_round": 2, "blocks_in": 2, "scope_declarable": True, "fix_check": True,
         "claim": "No SMC funder map; the funder is charged for all SMC",
         "fix_requires": "Obtain the SMC funder split by state (no such public dataset exists)", "status": ""}]}
    op.write(run, doc)
    # Real artifacts, so the critic judges content: the interval is zero width
    # and the Summary has no qualifier on the cases figure.
    json.dump({"total_cost_usd": {"mean": 412.0, "ci95": [412.0, 412.0], "draws": 230}},
              open(os.path.join(run, "model_output.json"), "w"))
    open(os.path.join(run, "summary.md"), "w").write("# Summary\nThe package averts 64M cases over five years.\n")
    trace = open(os.path.join(run, "trace.jsonl"), "a")
    hooks = create_hooks(run, trace, datetime.now(), registered_agents={"modeler", "analyst", "critique-redteam"},
                         budget_status=lambda: {"spent": 10.0, "cap": 100.0, "reserve": 5.0}, max_rounds=8)
    steps = {
        "modeler": (f"run with Bash: python3 -c \"import json; p='{run}/model_output.json'; d=json.load(open(p)); "
                    "d['total_cost_usd']['ci95']=[371.0, 455.0]; json.dump(d, open(p, 'w'))\""),
        "analyst": (f"if your task quotes a critic's evidence, run with Bash: sed -i 's/64M cases/64M cases "
                    f"(re-anchored range 51-77M)/' {run}/summary.md ; otherwise this test's first attempt is "
                    f"deliberately incomplete: run sed -i 's/64M cases/64M (re-anchored) cases/' {run}/summary.md"),
    }
    worker = lambda who: AgentDefinition(  # noqa: E731
        description=f"Test {who}", tools=["Bash"], model="haiku", maxTurns=4,
        prompt=f"You are the {who} in a test. Do not read or look for any files. To make your fix, {steps[who]} . "
               f"Then append one line "
               f"'<ids> {who}' to {run}/fixes.log and reply 'DONE:' followed by the ids in your task.")
    critic = AgentDefinition(
        description="Test red-team critic", tools=["Read", "Write"], model="haiku", maxTurns=8,
        prompt=("You are critique-redteam in a test. Full critique: reply CRITIQUE-DONE without writing files. "
                "Fix check: judge from the files only. ci_degenerate is fixed when model_output.json gives "
                "total_cost_usd.ci95 a width above zero. R-005 is fixed when summary.md states the re-anchored "
                "cases range as numbers; a bare '(re-anchored)' label is only partly fixed. No new problems. "
                f"Run directory: {run}" + op.FIX_CHECK_NOTE))
    lead_rules = (
        f"You are the lead of a modeling pipeline in a test. Run directory: {run}. The round-2 gate has just run. "
        "Read open_problems.md there. Then: (1) write round_plan.yaml in the run directory with `round: 2` and "
        "`items:` giving every id an action: fix or rethink with stage (PLAN, DATA, MODEL, ANALYZE, WRITE), "
        "declare with a reason (cannot be fixed with the data available), after_write (WRITE findings only), or "
        "defer with a reason (not for findings blocking now or at the next gate). (2) Spawn each stage agent "
        "(MODEL -> modeler, ANALYZE -> analyst) naming its ids. (3) Spawn critique-redteam with a prompt "
        "beginning `This is a fix check after round 2.` listing its fix ids. (4) Send anything not fixed back to "
        "its agent once, naming the id and quoting the critic's evidence. (5) Spawn critique-redteam with a prompt "
        "beginning `This is critique round 3.` If the harness refuses a call, read the reason, fix it and go on. "
        "Finish with the word FINISHED.")
    opts = _options(tools=["Agent", "Read", "Write", "Bash"], allowed_tools=["Agent", "Read", "Write", "Bash"],
                    agents={"modeler": worker("modeler"), "analyst": worker("analyst"), "critique-redteam": critic},
                    hooks=hooks, max_turns=30, system_prompt=lead_rules)
    sid, result, err, _, ev = await _converse("Begin.", opts, query_fn=query_fn, trace=trace)
    trace.close()
    fixes = os.path.join(run, "fixes.log")
    spawns = []
    for line in open(os.path.join(run, "trace.jsonl")):
        r = json.loads(line)
        if r.get("subagent_type"):
            spawns.append({"to": r["subagent_type"], "kind": r.get("critique_kind"), "mentions": r.get("mentions"),
                           "denied": (r.get("denied") or "")[:300] or None})
    plan = open(os.path.join(run, op.PLAN_FILE)).read() if os.path.exists(os.path.join(run, op.PLAN_FILE)) else None
    fc = os.path.join(run, op.fix_check_name(2, "critique-redteam"))
    missing, redo = op.check_fix_checks(run)
    full = [s for s in spawns if s["kind"] == "full" and not s["denied"]]
    return {"result": _summ(result), "error": err, "run": run, "plan_errors": op.check_plan(run), "plan": plan,
            "fix_check_written": os.path.exists(fc), "fix_check_missing": missing, "redo": redo,
            "spawns": spawns, "full_critique_allowed": len(full) >= 1,
            "fix_check_file": open(fc).read()[:1500] if os.path.exists(fc) else None,
            "denials": sum(1 for s in spawns if s["denied"]), "text": ((result and result.result) or "")[-200:],
            "fixes_log": open(fixes).read() if os.path.exists(fixes) else None,
            "summary_md": open(os.path.join(run, "summary.md")).read(),
            "model_output": open(os.path.join(run, "model_output.json")).read(),
            "refusals": [f"{r.get('summary', '')[:60]} :: {r['denied'][:140]}"
                         for r in map(json.loads, open(os.path.join(run, "trace.jsonl"))) if r.get("denied")],
            "tool_results": [e[2] for e in ev if e[0] == "tool_result"][:12]}


async def done_when_tests(query_fn=None):
    """Phase 34: does the real red-team critic (its own prompt and model) write
    done_when tests that are valid, fail on the planted problems, and pass
    once those are fixed? Web tools are off to keep it cheap."""
    import tempfile
    import yaml
    from agents import enforcement, red_team
    import decision_brief as db
    import done_when as dw
    import open_problems as op
    run = tempfile.mkdtemp(prefix="ma_donewhen_")
    os.makedirs(os.path.join(run, "models"))
    open(os.path.join(run, "plan.md"), "w").write(
        "# Plan\nQuestion: which intervention package should each of 4 districts get?\n"
        "Budget envelope: $100M total (hard cap). Outcome: cases averted, with 95% intervals.\n"
        "Packages: P1 nets; P2 nets + SMC; P3 nets + IRS.\n")
    json.dump({"total_cost_usd": {"mean": 98.0, "ci95": [98.0, 98.0], "draws": 230},
               "cases_averted_m": {"mean": 64.0, "ci95": [51.0, 77.0]}},
              open(os.path.join(run, "models", "uq_summary.json"), "w"), indent=1)
    open(os.path.join(run, "models", "allocation.csv"), "w").write(
        "district,package,cost_musd\nA,P2,31\nB,P2,29\nC,P3,27\nD,P1,24\n")
    open(os.path.join(run, "results.md"), "w").write(
        "# Results\nThe allocation (models/allocation.csv) averts 64M cases (95% CI 51-77M) for $98M "
        "(95% CI $98-98M over 230 draws).\n")
    open(os.path.join(run, "summary.md"), "w").write(
        "# Summary\nThe recommended allocation averts 64M cases and stays within the $100M budget.\n")
    prompt = (red_team.SYSTEM_PROMPT + op.FIX_CHECK_NOTE + db.CRITIC_NOTE + dw.CRITIC_NOTE).replace("{run_dir}", run)
    opts = _options(model=os.environ.get("SCENARIO_CRITIC_MODEL", enforcement.MODEL_DEEP),
                    tools=["Read", "Write", "Bash", "Glob", "Grep"],
                    allowed_tools=["Read", "Write", "Bash", "Glob", "Grep"], max_turns=30, system_prompt=prompt,
                    cwd=run)
    task = (f"This is critique round 1. Run directory: {run}. This is a small test run: review plan.md, "
            "results.md, summary.md and models/. Planted issues may include a zero-width cost interval, a "
            "Summary that drops the cases interval, and a cost total close to the cap with no interval. Write "
            f"{run}/critique_redteam.yaml (reviewer: critique-redteam, round: 1, verdict, blockers, "
            "carried_forward: [], structural_mismatch: {detected: false}). No decision_brief.yaml exists, so "
            "use brief_item: GAP. Keep it to the real problems.")
    sid, result, err, _, ev = await _converse(task, opts, query_fn=query_fn)
    path = os.path.join(run, "critique_redteam.yaml")
    try:
        doc = yaml.safe_load(open(path)) or {}
    except (OSError, yaml.YAMLError) as e:
        return {"result": _summ(result), "error": err or f"no critique: {e}"}
    found = [b for b in doc.get("blockers") or [] if isinstance(b, dict) and b.get("severity") in ("HIGH", "MEDIUM")]
    before = {b["id"]: dw.evaluate(b.get("done_when"), run) for b in found}
    # The planted fixes: a real interval, the interval in the Summary.
    j = json.load(open(os.path.join(run, "models", "uq_summary.json")))
    j["total_cost_usd"]["ci95"] = [91.0, 104.0]
    json.dump(j, open(os.path.join(run, "models", "uq_summary.json"), "w"), indent=1)
    open(os.path.join(run, "results.md"), "w").write(
        "# Results\nThe allocation (models/allocation.csv) averts 64M cases (95% CI 51-77M) for $98M "
        "(95% CI $91-104M over 230 draws); the upper bound exceeds the $100M cap in 38% of draws.\n")
    open(os.path.join(run, "summary.md"), "w").write(
        "# Summary\nThe recommended allocation averts 64M cases (95% CI 51-77M) for $98M (95% CI $91-104M); "
        "in 38% of draws it would exceed the $100M budget.\n")
    after = {b["id"]: dw.evaluate(b.get("done_when"), run) for b in found}
    rows = [{"id": b["id"], "severity": b["severity"], "claim": str(b.get("claim"))[:160],
             "done_when": b.get("done_when"), "problems": dw.problems(b.get("done_when")) if b.get("done_when") else ["missing"],
             "mechanical": dw.mechanical(b.get("done_when")),
             "before": before[b["id"]][0], "after": after[b["id"]][0], "after_detail": after[b["id"]][1][:200]}
            for b in found]
    mech = [r for r in rows if r["mechanical"]]
    return {"result": _summ(result), "error": err, "run": run, "findings": len(rows),
            "with_valid_test": sum(1 for r in rows if not r["problems"]), "mechanical": len(mech),
            "fail_before": sum(1 for r in mech if r["before"] is False),
            "pass_after_fix": sum(1 for r in mech if r["after"] is True),
            "trivial_pass_before": [r["id"] for r in mech if r["before"] is True], "rows": rows}


async def brief_signoff(query_fn=None):
    """Phase 33: the sign-off pause with a real lead. A planner stand-in writes
    a valid brief; the lead must stop (owner mode), then continue after the
    harness signs and resumes it with main.SIGNOFF_PROMPT."""
    import tempfile
    import yaml
    from datetime import datetime
    from claude_agent_sdk import AgentDefinition
    from agents import create_hooks
    import decision_brief as db
    from main import SIGNOFF_PROMPT
    run = tempfile.mkdtemp(prefix="ma_signoff_")
    yaml.safe_dump({"hard_blockers": [{"id": "HB-001", "criterion": "cost within envelope"}]},
                   open(os.path.join(run, "success_criteria.yaml"), "w"))
    brief = {"question": "How should a programme allocate its budget across districts?",
             "decision": "Package per district", "decision_maker": "Programme manager",
             "answers": [{"id": "A1", "statement": "Package per district", "resolution": "district"}],
             "objective": "Maximise DALYs averted", "acceptance": ["HB-001"],
             "open_questions": [{"id": "Q1", "question": "Is continuity a constraint?", "proposed": "No"}]}
    brief_path = os.path.join(run, "decision_brief.yaml")
    trace = open(os.path.join(run, "trace.jsonl"), "a")
    hooks = create_hooks(run, trace, datetime.now(), registered_agents={"planner", "data-agent"}, signoff="owner")
    planner = AgentDefinition(description="Test planner", tools=["Bash"], model="haiku", maxTurns=4,
                              prompt=f"Run exactly: cat > {brief_path} <<'B'\n{yaml.safe_dump(brief)}B\nthen reply PLANNED.")
    data = AgentDefinition(description="Test data agent", tools=["Bash"], model="haiku", maxTurns=2,
                           prompt="Do not use tools. Reply DATA-DONE.")
    rules = ("You are the lead of a modeling pipeline in a test. STAGE 1: spawn the planner. STAGE 2: spawn the "
             "data-agent. If the harness refuses a call, read its reason and do exactly what it says. When the "
             "data-agent has returned, finish with the word FINISHED.")
    opts = _options(tools=["Agent", "Bash"], allowed_tools=["Agent", "Bash"],
                    agents={"planner": planner, "data-agent": data}, hooks=hooks, max_turns=12, system_prompt=rules)
    sid, r1, err1, _, ev1 = await _converse("Begin.", opts, query_fn=query_fn)
    t1 = (getattr(r1, "result", None) or "") if r1 else ""
    status_before = db.status(run)
    signed = None
    if status_before == "awaiting":
        signed = db.sign(run, "owner", {"Q1": "Yes: keep districts covered now"})
    opts2 = _options(tools=["Agent", "Bash"], allowed_tools=["Agent", "Bash"],
                     agents={"planner": planner, "data-agent": data}, hooks=hooks, max_turns=8,
                     system_prompt=rules, resume=sid)
    _, r2, err2, _, ev2 = await _converse(SIGNOFF_PROMPT.format(text=db.signoff_text(run)), opts2,
                                          query_fn=query_fn)
    trace.close()
    t2 = (getattr(r2, "result", None) or "") if r2 else ""
    spawns = [(json.loads(l).get("subagent_type"), bool(json.loads(l).get("denied")))
              for l in open(os.path.join(run, "trace.jsonl")) if '"subagent_type"' in l]
    return {"first": _summ(r1), "second": _summ(r2), "errors": [err1, err2], "brief_status_before_signoff": status_before,
            "lead_stopped_for_signoff": "AWAITING_SIGNOFF" in t1, "data_refused_before_signoff":
            ("data-agent", True) in spawns, "data_ran_after_signoff": ("data-agent", False) in spawns[spawns.index(("data-agent", True)) + 1:]
            if ("data-agent", True) in spawns else False,
            "spawns": spawns, "text1": t1[-160:], "text2": t2[-160:]}


SCENARIOS = {"resume_cumulative": resume_cumulative, "readback_after_crash": readback_after_crash,
             "crash_before_init": crash_before_init, "budget_cap": budget_cap, "hook_context": hook_context,
             "cache_ttl": cache_ttl, "round_plan": round_plan, "brief_signoff": brief_signoff,
             "done_when_tests": done_when_tests}


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
