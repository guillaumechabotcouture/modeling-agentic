#!/usr/bin/env python3
"""
Phase 29: the post-mortem of a pipeline run, so every run, failed or not,
turns into findings without hand-digging through logs.

    python3 scripts/run_autopsy.py <run_dir> [--json]   # writes autopsy.json + autopsy.md
    python3 scripts/run_autopsy.py --self-test

Reads whatever the run left (all optional): metadata.json, trace.jsonl,
validator_history.jsonl, final_gate.json, budget_ledger.json,
artifact_integrity.json, resources.jsonl, models/compute_ledger.json,
report.md / manuscript.md.

Reports:
- outcome: status, report, final-gate verdict, sessions, spend, duration;
- time by agent (subagent start/stop pairs) and session errors;
- the gate across rounds: action, HIGH count, the finding kinds that
  persisted to the end and those that came back after being cleared;
- peak memory and the last resource sample; compute-service use;
- the failure classified by layer, so the right layer gets fixed:
    infrastructure  — process killed, memory exhausted, API limit, storage
    harness         — budget rule, session limit, gate or write-up errors
    agent behaviour — integrity violations, heavy compute not offloaded
    model quality   — blocking findings at the end
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys
from datetime import datetime


def _json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _jsonl(path):
    out = []
    try:
        with open(path, errors="replace") as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return out


def _ts(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def time_by_agent(trace: list[dict]) -> dict:
    """Hours per agent type from subagent start/stop pairs. A start with no
    recorded stop (crash, background task) ends at the next session boundary
    or the next start of the same agent type, whichever comes first, and is
    reported as "<agent> (no stop recorded)"."""
    hours = collections.Counter()
    open_ = {}                                  # agent_id -> (t0, type)
    def close(aid, t1, label_suffix=""):
        t0, at = open_.pop(aid)
        if t0 and t1 and t1 >= t0:
            hours[at + label_suffix] += (t1 - t0).total_seconds() / 3600
    for e in trace:
        t, typ = _ts(e.get("ts")), e.get("type")
        if typ == "subagent_stop" and e.get("agent_id") in open_:
            close(e["agent_id"], t)
        elif typ == "subagent_start":
            for aid, (_, at) in list(open_.items()):
                if at == e.get("agent_type"):
                    close(aid, t, " (no stop recorded)")
            open_[e.get("agent_id")] = (t, e.get("agent_type"))
        elif typ in ("session_start", "session_error"):
            for aid in list(open_):
                close(aid, t, " (no stop recorded)")
    last = max((_ts(e.get("ts")) for e in trace if _ts(e.get("ts"))), default=None)
    for aid in list(open_):
        close(aid, last, " (no stop recorded)")
    return {k: round(v, 2) for k, v in hours.most_common()}


def gate_trend(history: list[dict]) -> dict:
    rounds, kinds_by_round = [], []
    for h in history:
        f = [x for x in h.get("findings") or [] if isinstance(x, dict)]
        high = [x.get("kind") or x.get("id") or "?" for x in f if x.get("severity") == "HIGH"]
        rounds.append({"round": h.get("round"), "action": h.get("action"), "high": len(high),
                       "medium": sum(1 for x in f if x.get("severity") == "MEDIUM")})
        kinds_by_round.append(set(high))
    persistent = set.intersection(*kinds_by_round[-3:]) if len(kinds_by_round) >= 3 else set()
    returned = set()
    for i in range(2, len(kinds_by_round)):
        seen_before = set().union(*kinds_by_round[:i - 1])
        returned |= (kinds_by_round[i] - kinds_by_round[i - 1]) & seen_before
    final = kinds_by_round[-1] if kinds_by_round else set()
    return {"rounds": rounds, "final_high_kinds": sorted(final), "persistent_last3": sorted(persistent),
            "came_back": sorted(returned)}


def resources_summary(samples: list[dict]) -> dict:
    if not samples:
        return {"available": False}
    peak = max(samples, key=lambda s: s.get("mem_used_pct") or 0)
    return {"available": True, "samples": len(samples), "peak_mem_pct": peak.get("mem_used_pct"),
            "peak_at": peak.get("ts"), "peak_top": (peak.get("top") or [])[:3],
            "last": {k: samples[-1].get(k) for k in ("ts", "mem_used_pct", "mem_avail_gb", "cpu_pct")}}


def classify(a: dict) -> list[dict]:
    """Failure layers, most fundamental first. Empty when the run was ACCEPTed."""
    out = []
    errs = " ".join(a["session_errors"]).lower()
    if "exit code -9" in errs or "sigkill" in errs:
        out.append({"layer": "infrastructure", "why": "a session's process was killed (SIGKILL)"
                    + (f"; peak memory {a['resources'].get('peak_mem_pct')}%" if a["resources"].get("available") else
                       "; no resource samples to say why")})
    if "usage limit" in errs or "rate limit" in errs or "overloaded" in errs:
        out.append({"layer": "infrastructure", "why": "the model API refused requests (limit/overload)"})
    if (a["resources"].get("peak_mem_pct") or 0) >= 95:
        out.append({"layer": "infrastructure", "why": f"memory reached {a['resources']['peak_mem_pct']}%"})
    if a.get("store_error"):
        out.append({"layer": "infrastructure", "why": f"results not stored: {a['store_error']}"})
    if any("full remaining allowance" in s for s in a["ledger_sources"]):
        out.append({"layer": "harness", "why": "a session was charged its whole allowance (cost unknown)"})
    if a["status"] in ("budget_exhausted",):
        out.append({"layer": "harness", "why": "the run stopped on its budget cap"})
    if a["status"] == "max_sessions_reached":
        out.append({"layer": "harness", "why": "the run used all its sessions"})
    if a.get("final_gate_error"):
        out.append({"layer": "harness", "why": f"the final gate could not run: {a['final_gate_error']}"})
    if (a.get("final_writeup") or {}).get("error"):
        out.append({"layer": "harness", "why": f"the final write-up failed: {a['final_writeup']['error']}"})
    if a["integrity_open"]:
        out.append({"layer": "agent behaviour",
                    "why": f"{a['integrity_open']} protected output(s) changed outside their producer: "
                           f"{', '.join(a['integrity_files'][:4])}"})
    model_h = a["time_by_agent"].get("modeler", 0) + a["time_by_agent"].get("modeler (no stop recorded)", 0)
    if model_h >= 1.0 and not a["compute"]["calls"]:
        out.append({"layer": "agent behaviour",
                    "why": f"the modeler worked {model_h:.1f} h without using the compute service"})
    if a["final_gate_action"] not in (None, "ACCEPT"):
        kinds = a["gate"]["persistent_last3"] or a["gate"]["final_high_kinds"]
        out.append({"layer": "model quality",
                    "why": f"final gate {a['final_gate_action']}"
                           + (f"; blocking kinds that persisted: {', '.join(kinds[:6])}" if kinds else "")})
    if a.get("writer_blocked"):
        out.append({"layer": "harness", "why": f"the writer's save was refused {a['writer_blocked']} time(s) "
                                               "(CLI rule on REPORT*.md for subagents)"})
    if not a["report"]:
        out.append({"layer": "outcome", "why": "no report was produced"})
    return out


def autopsy(run_dir: str) -> dict:
    meta = _json(os.path.join(run_dir, "metadata.json")) or {}
    trace = _jsonl(os.path.join(run_dir, "trace.jsonl"))
    history = _jsonl(os.path.join(run_dir, "validator_history.jsonl"))
    gate = _json(os.path.join(run_dir, "final_gate.json")) or {}
    ledger = _json(os.path.join(run_dir, "budget_ledger.json")) or {}
    integ = _json(os.path.join(run_dir, "artifact_integrity.json")) or {}
    comp = _json(os.path.join(run_dir, "models", "compute_ledger.json")) or {}
    ts = [t for t in (_ts(e.get("ts")) for e in trace) if t]
    open_v = [v for v in integ.get("violations") or [] if isinstance(v, dict) and v.get("open", True)]
    a = {
        "run": os.path.basename(os.path.normpath(run_dir)),
        "status": meta.get("status"),
        "report": os.path.exists(os.path.join(run_dir, "report.md")),
        "manuscript": os.path.exists(os.path.join(run_dir, "manuscript.md")),
        "final_gate_action": gate.get("action"),
        "final_gate_error": gate.get("error"),
        "final_writeup": meta.get("final_writeup"),
        "sessions": sum(1 for e in trace if e.get("type") == "session_start"),
        "session_errors": [str(e.get("error"))[:200] for e in trace if e.get("type") == "session_error"],
        "span_hours": round((max(ts) - min(ts)).total_seconds() / 3600, 2) if ts else None,
        "spent_usd": ledger.get("spent_usd"), "cap_usd": ledger.get("cap_usd"),
        "ledger_sources": [str(s.get("source")) for s in ledger.get("sessions") or []],
        "time_by_agent": time_by_agent(trace),
        "gate": gate_trend(history),
        "resources": resources_summary(_jsonl(os.path.join(run_dir, "resources.jsonl"))),
        "compute": {"calls": len(comp.get("calls") or []),
                    "core_hours": round(float(comp.get("core_seconds") or 0) / 3600, 2)},
        "integrity_open": len(open_v),
        "integrity_files": sorted({str(v.get("file")) for v in open_v}),
        "store_error": meta.get("store_error"),
        "writer_blocked": sum(1 for e in trace if "should return findings as text" in json.dumps(e)),
    }
    a["failure"] = classify(a)
    a["primary_layer"] = a["failure"][0]["layer"] if a["failure"] else None
    return a


def to_markdown(a: dict) -> str:
    L = [f"# Run autopsy: {a['run']}", "",
         f"- **Outcome:** {a['status']}; report {'yes' if a['report'] else 'no'}; final gate "
         f"{a['final_gate_action'] or 'not run'}; {a['sessions']} session(s); span {a['span_hours']} h (first to last event); "
         f"${a['spent_usd']} of ${a['cap_usd']}",
         f"- **Primary failure layer:** {a['primary_layer'] or 'none (accepted)'}", "", "## Why it ended this way"]
    L += [f"- **{f['layer']}:** {f['why']}" for f in a["failure"]] or ["- Accepted."]
    L += ["", "## Time by agent (hours)"] + [f"- {k}: {v}" for k, v in a["time_by_agent"].items()]
    if a["gate"]["rounds"]:
        L += ["", "## Gate by round", "| Round | Action | HIGH | MEDIUM |", "|---|---|---|---|"]
        L += [f"| {r['round']} | {r['action']} | {r['high']} | {r['medium']} |" for r in a["gate"]["rounds"]]
        L += [f"- Blocking kinds persisting over the last 3 rounds: {', '.join(a['gate']['persistent_last3']) or 'none'}",
              f"- Kinds that came back after being cleared: {', '.join(a['gate']['came_back']) or 'none'}"]
    r = a["resources"]
    L += ["", "## Machine",
          (f"- Peak memory {r['peak_mem_pct']}% at {r['peak_at']}; last sample {r['last']}" if r.get("available")
           else "- No resource samples (run predates Phase 29)."),
          f"- Compute service: {a['compute']['calls']} call(s), {a['compute']['core_hours']} core-hours"]
    if a["session_errors"]:
        L += ["", "## Session errors"] + [f"- {e}" for e in a["session_errors"]]
    return "\n".join(L) + "\n"


def write(run_dir: str) -> dict:
    a = autopsy(run_dir)
    json.dump(a, open(os.path.join(run_dir, "autopsy.json"), "w"), indent=1, default=str)
    open(os.path.join(run_dir, "autopsy.md"), "w").write(to_markdown(a))
    return a


def register_row(a: dict) -> dict:
    """One row for the cross-run register."""
    return {k: a.get(k) for k in ("run", "status", "report", "final_gate_action", "sessions", "span_hours",
                                  "spent_usd", "primary_layer")} | {
        "layers": sorted({f["layer"] for f in a["failure"]}),
        "persistent_kinds": a["gate"]["persistent_last3"][:6],
        "peak_mem_pct": a["resources"].get("peak_mem_pct"), "compute_calls": a["compute"]["calls"]}


def _self_test() -> int:
    import tempfile
    fails = []
    with tempfile.TemporaryDirectory() as d:
        a = autopsy(d)
        if a["primary_layer"] != "outcome" or a["report"]:
            fails.append(f"A1 an empty run dir is a no-report outcome, not a crash: {a['failure']}")
        # attempt-5-like: modeler SIGKILLed, allowance charged, integrity open, no gate
        tr = [{"ts": "2026-10-25T05:53:05", "type": "session_start"},
              {"ts": "2026-10-25T06:24:00", "type": "subagent_start", "agent_id": "m", "agent_type": "modeler"},
              {"ts": "2026-10-25T08:50:42", "type": "session_error", "error": "Command failed with exit code -9"}]
        open(os.path.join(d, "trace.jsonl"), "w").write("\n".join(json.dumps(x) for x in tr) + "\n")
        json.dump({"status": "artifact_tampered"}, open(os.path.join(d, "metadata.json"), "w"))
        json.dump({"sessions": [{"source": "unreconciled: charged the full remaining allowance"}],
                   "spent_usd": 450, "cap_usd": 450}, open(os.path.join(d, "budget_ledger.json"), "w"))
        json.dump({"violations": [{"file": "identifiability.yaml", "open": True}]},
                  open(os.path.join(d, "artifact_integrity.json"), "w"))
        a = autopsy(d)
        layers = [f["layer"] for f in a["failure"]]
        if layers[:2] != ["infrastructure", "harness"] or "agent behaviour" not in layers:
            fails.append(f"A2 attempt-5 pattern: killed process first, then the budget rule, then agent behaviour: {layers}")
        if not any("without using the compute service" in f["why"] for f in a["failure"]):
            fails.append(f"A3 2.4 h of modeling without the compute service is flagged: {a['failure']}")
        if abs(a["time_by_agent"].get("modeler (no stop recorded)", 0) - 2.44) > 0.02:
            fails.append(f"A4 an agent with no stop counts to the next session boundary: {a['time_by_agent']}")
        tr2 = [{"ts": "2026-10-25T05:00:00", "type": "subagent_start", "agent_id": "p1", "agent_type": "planner"},
               {"ts": "2026-10-25T05:10:00", "type": "subagent_start", "agent_id": "p2", "agent_type": "planner"},
               {"ts": "2026-10-25T05:20:00", "type": "subagent_stop", "agent_id": "p2", "agent_type": "planner"},
               {"ts": "2026-10-25T09:00:00", "type": "session_start"}]
        tb = time_by_agent(tr2)
        if tb != {"planner": 0.17, "planner (no stop recorded)": 0.17}:
            fails.append(f"A4b a stop-less start ends at the next start of the same agent (attempt-5 trace): {tb}")
        hist = [{"round": 1, "action": "PATCH_OR_RETHINK", "findings": [{"severity": "HIGH", "kind": k} for k in ("a", "b")]},
                {"round": 2, "action": "PATCH_OR_RETHINK", "findings": [{"severity": "HIGH", "kind": "b"}]},
                {"round": 3, "action": "DECLARE_SCOPE", "findings": [{"severity": "HIGH", "kind": k} for k in ("a", "b")]}]
        g = gate_trend(hist)
        if g["persistent_last3"] != ["b"] or g["came_back"] != ["a"]:
            fails.append(f"A5 persistent and returning finding kinds: {g}")
        with open(os.path.join(d, "trace.jsonl"), "a") as f:
            f.write(json.dumps({"type": "task_notification", "summary": "Write blocked: Subagents should return findings as text"}) + "\n")
        if not any("writer's save was refused" in f["why"] for f in autopsy(d)["failure"]):
            fails.append("A6b a refused writer save (attempts 3-4) is recognised")
        if "Gate by round" in to_markdown(a) or "Run autopsy" not in to_markdown(a):
            fails.append("A6 markdown renders (no gate table without gate history)")
        row = register_row(write(d))
        if not os.path.exists(os.path.join(d, "autopsy.md")) or row["primary_layer"] != "infrastructure":
            fails.append(f"A7 files written and register row built: {row}")
    if fails:
        print("SELF-TEST FAILURES:", *fails, sep="\n  - ")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Post-mortem of a pipeline run")
    ap.add_argument("run_dir", nargs="?")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return _self_test()
    if not a.run_dir:
        ap.error("run_dir required")
    res = write(a.run_dir)
    print(json.dumps(res, indent=1, default=str) if a.json else to_markdown(res))
    return 0


if __name__ == "__main__":
    sys.exit(main())
