#!/usr/bin/env python3
"""Per-turn telemetry from Claude CLI session transcripts (architecture v2, step 1).

Turns the lead transcript plus every subagent transcript of one run into a
per-turn table, so cost, latency and context growth are attributable to an
agent, stage, round and tool kind instead of needing ad-hoc forensics.

    python3 scripts/telemetry.py <session_dir> --lead <lead.jsonl> [--run-dir R] [--out turns.csv]
    python3 scripts/telemetry.py --self-test

<session_dir> is ~/.claude/projects/<key>/<session_id>/ (contains subagents/*.jsonl
and *.meta.json); --lead is the matching <session_id>.jsonl. If --run-dir is
given, its metadata.json session_results calibrate per-model cost.

Cost per turn is not in the transcripts. It is estimated from Anthropic's
standard price *ratios* between token categories (cache read 0.1x, cache write
1.25x, output 5x the base input price), then scaled per model so the run total
matches the cost the CLI recorded. Relative attributions are therefore
reliable; absolute per-turn dollars inherit that approximation.
"""
from __future__ import annotations

import argparse
import collections
import csv
import glob
import json
import os
import re
import sys
import tempfile
from datetime import datetime

RATIOS = {"input": 1.0, "cache_read": 0.1, "cache_write": 1.25, "output": 5.0}

STAGE_OF = {
    "planner": "PLAN", "data-agent": "DATA", "critique-premortem": "PRE_MORTEM",
    "modeler": "MODEL", "analyst": "ANALYZE", "writer": "WRITE",
    "critique-methods": "CRITIQUE", "critique-domain": "CRITIQUE",
    "critique-presentation": "CRITIQUE", "critique-redteam": "CRITIQUE",
    "critique-sufficiency": "POST_WRITE", "lead": "ORCHESTRATE",
}
_WAIT = re.compile(r"\bsleep\b|wait_for|grep -q[^|]*&& break")
_COMPUTE = re.compile(r"python3?\s+\S*(calib|propagat|uncertain|sensitiv|identif|compare_models|optimi|loo|scenario|model)", re.I)
_ROUND = re.compile(r"\bround[\s_-]*(\d+)\b", re.I)

FIELDS = ["agent", "agent_id", "stage", "round", "turn", "model", "ts", "latency_s",
          "input", "cache_read", "cache_write", "output", "context", "tool", "tool_kind",
          "weighted", "cost_usd"]


def _ts(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None
    except ValueError:
        return None


def _tool_kind(name, inp):
    if name == "Bash":
        cmd = json.dumps(inp)
        if _WAIT.search(cmd):
            return "wait"
        if _COMPUTE.search(cmd):
            return "compute"
        return "bash"
    return {"Read": "read", "Write": "write", "Edit": "write", "Agent": "spawn",
            "Task": "spawn", "Skill": "skill", "Glob": "search", "Grep": "search",
            "WebFetch": "web", "WebSearch": "web"}.get(name, name.lower() if name else "none")


def _agent_meta(path):
    meta = path[:-len(".jsonl")] + ".meta.json"
    try:
        m = json.load(open(meta))
    except (OSError, ValueError):
        return "?", None
    desc = m.get("description") or m.get("prompt") or ""
    r = _ROUND.search(desc)
    return (m.get("agentType") or m.get("subagent_type") or "?"), (int(r.group(1)) if r else None)


def parse_transcript(path, agent, rnd, agent_id):
    """One row per API response. The CLI writes one transcript line per
    content block (thinking / text / tool_use), each repeating the same
    message id and usage; counting lines double-counted tokens ~2x."""
    rows, prev_user_ts, turn, by_id = [], None, 0, {}
    for line in open(path, errors="replace"):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        t = _ts(d.get("timestamp"))
        msg = d.get("message") or {}
        if d.get("type") == "user":
            prev_user_ts = t or prev_user_ts
            continue
        if d.get("type") != "assistant" or not msg.get("usage"):
            continue
        u = msg["usage"]
        tools = [b for b in msg.get("content") or [] if isinstance(b, dict) and b.get("type") == "tool_use"]
        mid = msg.get("id")
        if mid and mid in by_id:          # same response, another content block
            r0 = by_id[mid]
            if tools and not r0["tool"]:
                r0["tool"] = tools[0]["name"]
                r0["tool_kind"] = _tool_kind(tools[0]["name"], tools[0].get("input") or {})
            continue
        name = tools[0]["name"] if tools else None
        row = {
            "agent": agent, "agent_id": agent_id, "stage": STAGE_OF.get(agent, "OTHER"),
            "round": rnd, "turn": turn, "model": msg.get("model") or "?",
            "ts": t.isoformat() if t else "",
            "latency_s": round((t - prev_user_ts).total_seconds(), 1) if t and prev_user_ts else None,
            "input": u.get("input_tokens") or 0, "cache_read": u.get("cache_read_input_tokens") or 0,
            "cache_write": u.get("cache_creation_input_tokens") or 0, "output": u.get("output_tokens") or 0,
            "tool": name or "", "tool_kind": _tool_kind(name, tools[0].get("input") if tools else {}),
        }
        row["context"] = row["input"] + row["cache_read"] + row["cache_write"]
        row["weighted"] = sum(row[k] * RATIOS[k] for k in RATIOS)
        rows.append(row)
        if mid:
            by_id[mid] = row
        turn += 1
    return rows


def collect(session_dir, lead_path):
    rows = []
    if lead_path and os.path.exists(lead_path):
        rows += parse_transcript(lead_path, "lead", None, "lead")
    for p in sorted(glob.glob(os.path.join(session_dir, "subagents", "*.jsonl"))):
        agent, rnd = _agent_meta(p)
        rows += parse_transcript(p, agent, rnd, os.path.basename(p)[:-6])
    return rows


def calibrate_costs(rows, run_dir=None):
    """Scale weighted tokens per model so totals match recorded model costs."""
    recorded = collections.Counter()
    if run_dir:
        try:
            meta = json.load(open(os.path.join(run_dir, "metadata.json")))
            for s in meta.get("session_results") or []:
                for model, mu in (s.get("model_usage") or {}).items():
                    recorded[model] += mu.get("costUSD") or 0
        except (OSError, ValueError):
            pass
    weight = collections.Counter()
    for r in rows:
        weight[r["model"]] += r["weighted"]
    for r in rows:
        tot = recorded.get(r["model"]) or 0
        r["cost_usd"] = round(r["weighted"] / weight[r["model"]] * tot, 6) if tot and weight[r["model"]] else None
    return {m: round(v, 2) for m, v in recorded.items()}


def summarize(rows):
    by = collections.defaultdict(lambda: collections.Counter())
    for r in rows:
        k = (r["stage"], r["agent"])
        c = by[k]
        c["turns"] += 1; c["context"] += r["context"]; c["output"] += r["output"]
        c["cost"] += r["cost_usd"] or 0; c["latency"] += r["latency_s"] or 0
        c["wait_turns"] += r["tool_kind"] == "wait"
        c["wait_context"] += r["context"] if r["tool_kind"] == "wait" else 0
        c["start_ctx"] = c["start_ctx"] or (r["context"] if r["turn"] == 0 else 0)
    tot_cost = sum(c["cost"] for c in by.values()) or 0
    tot_ctx = sum(c["context"] for c in by.values()) or 1
    lines = [f"{'stage':12} {'agent':22} {'turns':>6} {'ctx_M':>7} {'ctx%':>5} {'cost$':>8} {'hours':>6} {'wait%ctx':>8}"]
    for (stage, agent), c in sorted(by.items(), key=lambda x: -x[1]["context"]):
        lines.append(f"{stage:12} {agent:22} {c['turns']:6d} {c['context']/1e6:7.1f} "
                     f"{100*c['context']/tot_ctx:4.0f}% {c['cost']:8.2f} {c['latency']/3600:6.2f} "
                     f"{100*c['wait_context']/max(c['context'],1):7.0f}%")
    lines.append(f"TOTAL turns={len(rows)} context={tot_ctx/1e6:.1f}M est_cost=${tot_cost:.2f}")
    return "\n".join(lines)


def _self_test() -> int:
    failures = []
    def ok(c, m):
        if not c: failures.append(m)
    with tempfile.TemporaryDirectory() as td:
        sess = os.path.join(td, "sid"); os.makedirs(os.path.join(sess, "subagents"))
        def turn(ts, model, cr, cw, inp, out, tool=None, tin=None, mid=None):
            content = [{"type": "tool_use", "id": "x", "name": tool, "input": tin or {}}] if tool else []
            return json.dumps({"type": "assistant", "timestamp": ts,
                               "message": {"id": mid or ts, "model": model, "content": content,
                                           "usage": {"input_tokens": inp, "cache_read_input_tokens": cr,
                                                     "cache_creation_input_tokens": cw, "output_tokens": out}}})
        user = lambda ts: json.dumps({"type": "user", "timestamp": ts, "message": {"content": []}})
        open(os.path.join(td, "sid.jsonl"), "w").write("\n".join([
            user("2026-10-21T10:00:00Z"), turn("2026-10-21T10:00:05Z", "claude-opus-5-5", 1000, 100, 10, 50, "Agent")]))
        sub = os.path.join(sess, "subagents", "agent-a1.jsonl")
        open(sub, "w").write("\n".join([
            user("2026-10-21T10:01:00Z"),
            turn("2026-10-21T10:01:09Z", "claude-opus-5-5", 170000, 3000, 5, 200, mid="m1"),  # thinking block
            turn("2026-10-21T10:01:10Z", "claude-opus-5-5", 170000, 3000, 5, 200, "Bash", {"command": "python3 calibrate.py"}, mid="m1"),
            user("2026-10-21T10:02:00Z"),
            turn("2026-10-21T10:02:04Z", "claude-opus-5-5", 300000, 1000, 5, 40, "Bash", {"command": "sleep 60; tail -3 log"})]))
        open(os.path.join(sess, "subagents", "agent-a1.meta.json"), "w").write(
            json.dumps({"agentType": "modeler", "description": "Modeler round 3 - fix calibration"}))
        rd = os.path.join(td, "run"); os.makedirs(rd)
        json.dump({"session_results": [{"model_usage": {"claude-opus-5-5": {"costUSD": 10.0}}}]},
                  open(os.path.join(rd, "metadata.json"), "w"))
        rows = collect(sess, os.path.join(td, "sid.jsonl"))
        ok(len(rows) == 3, f"T1 expected 3 turns, got {len(rows)}")
        mod = [r for r in rows if r["agent"] == "modeler"]
        ok(mod and mod[0]["stage"] == "MODEL" and mod[0]["round"] == 3, "T2 agent/stage/round from meta")
        ok([r["tool_kind"] for r in mod] == ["compute", "wait"], f"T3 tool kinds: {[r['tool_kind'] for r in mod]}")
        ok(mod[0]["context"] == 173005 and mod[0]["latency_s"] == 9.0, f"T4 context and latency: {mod[0]['latency_s']}")
        rec = calibrate_costs(rows, rd)
        ok(abs(sum(r["cost_usd"] for r in rows) - 10.0) < 1e-6 and rec == {"claude-opus-5-5": 10.0},
           "T5 calibrated costs sum to recorded total")
        ok(mod[1]["cost_usd"] > rows[0]["cost_usd"], "T6 bigger context costs more")
        ok("MODEL" in summarize(rows) and "TOTAL turns=3" in summarize(rows), "T7 summary")
    if failures:
        print("SELF-TEST FAILURES:", *failures, sep="\n  - ")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("session_dir", nargs="?")
    ap.add_argument("--lead")
    ap.add_argument("--run-dir")
    ap.add_argument("--out")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return _self_test()
    rows = collect(a.session_dir, a.lead)
    rec = calibrate_costs(rows, a.run_dir)
    if a.out:
        with open(a.out, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS); w.writeheader(); w.writerows(rows)
    print(f"recorded model costs: {rec}")
    print(summarize(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
