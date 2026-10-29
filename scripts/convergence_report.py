#!/usr/bin/env python3
"""Does the loop converge? Per-round measures for one run (E7 attempt 7 pre-registration).

    python3 scripts/convergence_report.py <run_dir> [--json]
    python3 scripts/convergence_report.py --self-test

Reads only run files: validator_history.jsonl (gate decisions), critique_*.yaml
(findings by first round and resolution), done_when_registry.json (finding
tests), trace.jsonl (holds and their reasons; uncertainty and gate runs),
models/compute_ledger.json and budget_ledger.json. Works on a finished run or a
live one (on its VM).

Uncertainty runs per round (Phase 42 ε, improvement plan A8): each Bash call
that runs scripts/propagate_uncertainty.py (not --self-test or --help, not
refused) counts once, in the round of the next gate run
(validate_critique_yaml.py --current-round N) after it; runs after the last
gate are under "after". Traces from Phase 42 on record the full command
(`scripts_run`, `command`); older ones only an 80-character summary, so their
counts can miss calls whose script name was cut off.
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import re
import sys
import tempfile

import yaml

CRITIC_FILES = ("critique_methods.yaml", "critique_domain.yaml", "critique_presentation.yaml",
                "critique_redteam.yaml")
FINDING_ID = re.compile(r"\b[A-Z]{1,4}-\d{1,4}\b")
HOLD_KINDS = (("round plan", "write round_plan.yaml"), ("plan changed", "stays that way this round"),
              ("plan handed out", "handed to the agent that does it"), ("fix check", "fix check after round"),
              ("send back", "back to its agent once"), ("still working", "still working"),
              ("sign-off", "AWAITING_SIGNOFF"), ("writer", "write-up waits"),
              ("critic prompt", "prompt with one of"), ("list missing", "missing or stale"))


def _jsonl(path):
    try:
        with open(path, errors="replace") as f:
            for line in f:
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
    except OSError:
        return


def _json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def report(run_dir: str, trace_utc_offset_s: float | None = None) -> dict:
    out: dict = {"run_dir": run_dir}
    # Gates: open blocking findings per in-round gate (last run of each round).
    gates = {}
    for r in _jsonl(os.path.join(run_dir, "validator_history.jsonl")):
        if r.get("post_write"):
            continue
        gates[r.get("round")] = {"action": r.get("action"),
                                 "open_blocking": len(r.get("findings") or [])}   # the gate's recorded findings
    out["gates"] = {k: gates[k] for k in sorted(gates, key=lambda x: (x is None, x))}
    # Findings: arrivals per round, resolution, rounds to resolve.
    arrivals, closed, ages, still_open, state = collections.Counter(), 0, [], [], {}
    for f in CRITIC_FILES:
        try:
            doc = yaml.safe_load(open(os.path.join(run_dir, f))) or {}
        except (OSError, yaml.YAMLError):
            continue
        for b in doc.get("blockers") or []:
            if not isinstance(b, dict) or b.get("severity") not in ("HIGH", "MEDIUM"):
                continue
            first = b.get("first_seen_round")
            arrivals[first] += 1
            state[str(b.get("id"))] = b.get("resolved_in_round") if b.get("resolved") is True else None
            if b.get("resolved") is True:
                closed += 1
                if isinstance(b.get("resolved_in_round"), int) and isinstance(first, int):
                    ages.append(b["resolved_in_round"] - first)
            else:
                still_open.append(str(b.get("id")))
    out["new_findings_by_round"] = {k: arrivals[k] for k in sorted(arrivals, key=lambda x: (x is None, x))}
    out["findings_resolved"] = closed
    out["findings_open"] = len(still_open)
    out["median_rounds_to_resolve"] = sorted(ages)[len(ages) // 2] if ages else None
    # Finding tests.
    tests = (_json(os.path.join(run_dir, "done_when_registry.json"), {}) or {}).get("tests") or {}
    kinds = collections.Counter()
    for t in tests.values():
        dw = t.get("done_when") if isinstance(t, dict) else None
        lst = dw if isinstance(dw, list) else [dw] if isinstance(dw, dict) else []
        j = sum(1 for c in lst if isinstance(c, dict) and "judgement" in c)
        kinds["judgement" if j and j == len(lst) else "mixed" if j else "runnable"] += 1
    hist_last = {k: (t.get("history") or [[None, None]])[-1][1] for k, t in tests.items() if isinstance(t, dict)}
    out["tests"] = {"registered": len(tests), "by_kind": dict(kinds),
                    "passed_once": sum(1 for t in tests.values() if isinstance(t, dict) and t.get("passed_round")),
                    "passing_now": sum(1 for v in hist_last.values() if v is True),
                    "regressed_now": sum(1 for k, t in tests.items() if isinstance(t, dict) and t.get("passed_round")
                                         and hist_last.get(k) is False),
                    "withdrawn": sum(1 for t in tests.values() if isinstance(t, dict) and t.get("withdrawn"))}
    # Holds that fired.
    holds = collections.Counter()
    for r in _jsonl(os.path.join(run_dir, "trace.jsonl")):
        reason = r.get("denied")
        if not reason:
            continue
        holds[next((name for name, key in HOLD_KINDS if key in reason), "other")] += 1
    out["refusals"] = dict(holds.most_common())
    uq = uq_runs_by_round(run_dir, trace_utc_offset_s)
    out["uq_runs_by_round"], out["uq_rounds_from"] = uq["by_round"], uq["rounds_from"]
    # Compute.
    led = _json(os.path.join(run_dir, "models", "compute_ledger.json"), {}) or {}
    calls = led.get("calls") or []
    unrun = sum(int(c.get("n_failed") or 0) for c in calls if c.get("stopped"))
    total = sum(int(c.get("n") or 0) for c in calls)
    # Phase 35: every failed row counts, not only rows of stopped calls (E7
    # attempt 7: 1,544 of 1,920 rows failed in a call that was not stopped and
    # the report showed an unrun share of 0.0).
    failed = sum(int(c.get("n_failed") or 0) for c in calls)
    by_kind = collections.Counter()
    for c in calls:
        by_kind.update(c.get("failed_by_kind") or {})
    out["compute"] = {"calls": len(calls), "core_hours": round(float(led.get("core_seconds") or 0) / 3600, 1),
                      "stopped_calls": sum(1 for c in calls if c.get("stopped")),
                      "unrun_share": round(unrun / total, 3) if total else None,
                      "failed_share": round(failed / total, 3) if total else None,
                      "failed_by_kind": dict(by_kind), "pool_breaks": sum(int(c.get("breaks") or 0) for c in calls),
                      "last_call": calls[-1].get("ts") if calls else None,
                      **_compute_by_purpose(calls, state)}
    bl = _json(os.path.join(run_dir, "budget_ledger.json"), {}) or {}
    out["spend_usd"] = bl.get("spent_usd")
    out["cap_usd"] = bl.get("cap_usd")
    return out


_ROUND_ARG = re.compile(r"--current-round[= ]+(\d+)")
_NOT_A_RUN = re.compile(r"(?<![\w-])(--self-test|--help|-h)(?![\w-])")


def _runs(rec: dict, script: str) -> str | None:
    """The command when this trace record is an allowed Bash call that runs `script`."""
    if rec.get("type") != "tool_use" or rec.get("tool") != "Bash" or rec.get("denied"):
        return None
    if "scripts_run" in rec:
        return str(rec.get("command") or "") if script in (rec.get("scripts_run") or []) else None
    # Traces before Phase 42: an 80-character summary; the script must be what Python runs
    # (not a grep or sed of it).
    s = str(rec.get("summary") or "")
    return s if re.search(r"\bpython[\d.]*\s+(?:-u\s+)?\S*scripts/" + re.escape(script), s) else None


def _epoch(ts, utc_offset_s=None) -> float | None:
    """Seconds since the epoch. A naive time (the trace's) is in the run machine's zone:
    `utc_offset_s` when known, else this machine's local zone."""
    import datetime as _dt
    try:
        t = _dt.datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return None
    if t.tzinfo is None and utc_offset_s is not None:
        t = t.replace(tzinfo=_dt.timezone(_dt.timedelta(seconds=int(utc_offset_s))))
    return t.timestamp()


def uq_runs_by_round(run_dir: str, trace_utc_offset_s=None) -> dict:
    """{"by_round": {round: uncertainty runs}, "rounds_from": ...} (A8). A run counts in the
    round of the next gate run after it: gate runs from the trace (Phase 42 records the
    command and its --current-round), else from validator_history.jsonl times."""
    tz = trace_utc_offset_s
    if tz is None:      # main.py records the run machine's zone from Phase 42 on
        tz = (_json(os.path.join(run_dir, "metadata.json"), {}) or {}).get("utc_offset_s")
    tz_known = isinstance(tz, (int, float)) and not isinstance(tz, bool)
    tz = tz if tz_known else None
    uq, gates = [], []
    for i, rec in enumerate(_jsonl(os.path.join(run_dir, "trace.jsonl"))):
        c = _runs(rec, "propagate_uncertainty.py")
        if c is not None and not _NOT_A_RUN.search(c):
            uq.append((i, _epoch(rec.get("ts"), tz)))
        g = _runs(rec, "validate_critique_yaml.py")
        m = _ROUND_ARG.search(g or "")
        if m and not _NOT_A_RUN.search(g):
            gates.append((i, int(m.group(1))))
    source = "trace"
    hist = []
    for r in _jsonl(os.path.join(run_dir, "validator_history.jsonl")):
        t = _epoch(r.get("at"))
        if not r.get("post_write") and isinstance(r.get("round"), int) and t is not None:
            hist.append((t, r["round"]))
    # The trace's gate runs are used only when they cover every round the gate recorded
    # (Codex review: a trace spanning the Phase 42 change has some gate rounds cut off).
    if not gates or not {rnd for _t, rnd in hist} <= {rnd for _i, rnd in gates}:
        # Codex review: trace times are naive; without the run's zone this machine's is assumed.
        source = "validator_history" + ("" if tz_known else ", trace times taken as this machine's local time")
        gates = sorted(hist)
        uq = [(t, t) for _i, t in uq if t is not None]
    by = collections.Counter()
    for pos, _t in uq:
        by[next((rnd for gpos, rnd in gates if gpos >= pos), "after")] += 1
    out = {k: by[k] for k in sorted(k for k in by if k != "after")}
    if by.get("after"):
        out["after"] = by["after"]
    return {"by_round": out, "rounds_from": source if gates else None}


def _compute_by_purpose(calls, state) -> dict:
    """Phase 37: what the compute bought. Core-hours per stated purpose, the
    share lost to failed rows (estimated pro rata: every failed row is charged
    its call's mean cost, including rows stopped before they started, so the
    loss is over- rather than under-stated), and per finding ID named in a purpose whether that
    finding has since been resolved. Batches before Phase 37 are 'unstated'."""
    by, per_id = {}, {}
    for c in calls:
        p = str(c.get("purpose") or "").strip() or "unstated"
        n, nf = int(c.get("n") or 0), int(c.get("n_failed") or 0)
        h = float(c.get("task_core_s") or 0) / 3600
        e = by.setdefault(p, {"calls": 0, "core_hours": 0.0, "lost_core_hours": 0.0, "rows": 0, "failed": 0})
        e["calls"] += 1
        e["core_hours"] += h
        e["lost_core_hours"] += h * nf / n if n else h
        e["rows"] += n
        e["failed"] += nf
        ids = sorted(set(FINDING_ID.findall(p)))
        for i in ids:      # a batch serving several findings is split evenly between them
            per_id[i] = per_id.get(i, 0.0) + h / len(ids)
    for e in by.values():
        e["core_hours"], e["lost_core_hours"] = round(e["core_hours"], 1), round(e["lost_core_hours"], 1)
    findings = {i: {"core_hours": round(h, 1), "resolved_in_round": state.get(i),
                    "known": i in state} for i, h in sorted(per_id.items(), key=lambda kv: -kv[1])}
    return {"by_purpose": dict(sorted(by.items(), key=lambda kv: -kv[1]["core_hours"])), "by_finding": findings,
            "lost_core_hours": round(sum(e["lost_core_hours"] for e in by.values()), 1)}


def render(r: dict) -> str:
    lines = [f"# Convergence report: {os.path.basename(os.path.normpath(r['run_dir']))}", ""]
    lines.append("Gate findings recorded per round (validator history): " + ", ".join(f"r{k} {v['open_blocking']} ({v['action']})"
                                                      for k, v in r["gates"].items()) or "none")
    lines.append("New HIGH/MEDIUM by first round: " + ", ".join(f"r{k} {v}" for k, v in r["new_findings_by_round"].items()))
    lines.append(f"Findings resolved {r['findings_resolved']}, open {r['findings_open']}, median rounds to "
                 f"resolve {r['median_rounds_to_resolve']}")
    t = r["tests"]
    lines.append(f"Tests: {t['registered']} registered {t['by_kind']}; passed once {t['passed_once']}, passing now "
                 f"{t['passing_now']}, regressed now {t['regressed_now']}, withdrawn {t['withdrawn']}")
    lines.append("Refusals: " + (", ".join(f"{k} {v}" for k, v in r["refusals"].items()) or "none"))
    lines.append("Uncertainty runs by round: " + (", ".join(
        f"{'r' + str(k) if k != 'after' else 'after the last gate'} {v}"
        for k, v in r.get("uq_runs_by_round", {}).items()) or "none")
        + (f" (rounds from {r['uq_rounds_from']})" if r.get("uq_rounds_from") else ""))
    c = r["compute"]
    lines.append(f"Compute: {c['calls']} calls, {c['core_hours']} core-hours, {c['stopped_calls']} stopped, "
                 f"unrun share {c['unrun_share']}, failed share {c['failed_share']} {c['failed_by_kind']}, "
                 f"pool breaks healed {c['pool_breaks']}, last call {c['last_call']}")
    if c.get("by_purpose"):
        lines.append(f"Compute by purpose (core-hours, of which lost to failed rows, est.; {c['lost_core_hours']} lost in all):")
        for p, e in c["by_purpose"].items():
            lines.append(f"  - {p}: {e['core_hours']} ({e['lost_core_hours']} lost), {e['calls']} calls, "
                         f"{e['failed']}/{e['rows']} rows failed")
    if c.get("by_finding"):
        lines.append("Compute by finding: " + ", ".join(
            f"{i} {e['core_hours']} h " + (f"(resolved r{e['resolved_in_round']})" if e["resolved_in_round"] is not None
                                           else "(open)" if e["known"] else "(not in critiques)")
            for i, e in c["by_finding"].items()))
    lines.append(f"Spend: ${r['spend_usd']} of ${r['cap_usd']}")
    return "\n".join(lines) + "\n"


def _self_test() -> int:
    fails = []

    def ok(c, m):
        if not c:
            fails.append(m)

    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "validator_history.jsonl"), "w") as f:
            for rnd, n in ((1, 5), (2, 3), (2, 2)):
                f.write(json.dumps({"round": rnd, "action": "PATCH_OR_RETHINK", "findings": list(range(n))}) + "\n")
            f.write(json.dumps({"round": 2, "action": "ACCEPT", "post_write": True, "findings": []}) + "\n")
        yaml.safe_dump({"blockers": [
            {"id": "R-1", "severity": "HIGH", "first_seen_round": 1, "resolved": True, "resolved_in_round": 2},
            {"id": "R-2", "severity": "MEDIUM", "first_seen_round": 2, "resolved": False},
            {"id": "R-3", "severity": "LOW", "first_seen_round": 2, "resolved": False}]},
            open(os.path.join(d, "critique_redteam.yaml"), "w"))
        json.dump({"tests": {"R-1": {"done_when": {"file": "a", "exists": True}, "passed_round": 2,
                                     "history": [[1, False], [2, True]]},
                             "R-2": {"done_when": [{"file": "a", "exists": True}, {"judgement": "x"}],
                                     "passed_round": None, "history": [[2, None]]},
                             "R-9": {"done_when": {"judgement": "y"}, "passed_round": 1, "history": [[2, False]],
                                     "withdrawn": {"round": 2}}}}, open(os.path.join(d, "done_when_registry.json"), "w"))
        with open(os.path.join(d, "trace.jsonl"), "w") as f:
            f.write(json.dumps({"denied": "Before the next round's work, write round_plan.yaml (round: 1)"}) + "\n")
            f.write(json.dumps({"denied": "critique-redteam reviews finished work, and these agents are still working"}) + "\n")
            f.write("bad\n")
            uq = "python3 scripts/propagate_uncertainty.py runs/x --n-draws 1100"
            gate = "python3 scripts/validate_critique_yaml.py runs/x --max-rounds 8 --current-round {}"
            for rec in (
                    {"summary": "Bash: " + uq},                                     # pre-42: summary only
                    {"summary": "Bash: " + gate.format(1)},
                    {"scripts_run": ["propagate_uncertainty.py"], "command": uq},
                    {"scripts_run": ["propagate_uncertainty.py"], "command": uq},
                    {"scripts_run": ["propagate_uncertainty.py"], "command": uq, "denied": "no"},
                    {"scripts_run": ["propagate_uncertainty.py"], "command": uq.replace("runs/x", "--self-test")},
                    {"scripts_run": ["sanity_checks.py"], "command": "python3 scripts/sanity_checks.py runs/x"},
                    {"scripts_run": ["validate_critique_yaml.py"], "command": gate.format(2)},
                    {"scripts_run": ["propagate_uncertainty.py"], "command": uq},
                    {"scripts_run": ["validate_critique_yaml.py"], "command": gate.format(2)},   # same-round rerun
                    {"scripts_run": ["validate_critique_yaml.py"], "command": gate.format(3)},
                    {"scripts_run": ["propagate_uncertainty.py"], "command": uq}):
                f.write(json.dumps({"type": "tool_use", "tool": "Bash", **rec}) + "\n")
        json.dump({"core_seconds": 7200, "calls": [{"n": 10, "n_failed": 4, "stopped": "timed out", "ts": "t1"},
                                                   {"n": 10, "n_failed": 0, "stopped": None, "ts": "t2"}]},
                  open(os.path.join(d, "models", "compute_ledger.json"), "w"))
        r0 = report(d)
        ok(list(r0["compute"]["by_purpose"]) == ["unstated"] and r0["compute"]["by_finding"] == {},
           f"CV8 batches without a purpose are reported as unstated: {r0['compute']}")
        json.dump({"spent_usd": 12.5, "cap_usd": 450}, open(os.path.join(d, "budget_ledger.json"), "w"))
        r = report(d)
        ok(r["gates"] == {1: {"action": "PATCH_OR_RETHINK", "open_blocking": 5},
                          2: {"action": "PATCH_OR_RETHINK", "open_blocking": 2}},
           f"CV1 last in-round gate per round; post-write excluded: {r['gates']}")
        ok(r["new_findings_by_round"] == {1: 1, 2: 1} and r["findings_resolved"] == 1 and r["findings_open"] == 1
           and r["median_rounds_to_resolve"] == 1, f"CV2 HIGH/MEDIUM arrivals and resolution: {r}")
        ok(r["tests"]["registered"] == 3 and r["tests"]["by_kind"] == {"runnable": 1, "mixed": 1, "judgement": 1}
           and r["tests"]["passing_now"] == 1 and r["tests"]["regressed_now"] == 1 and r["tests"]["withdrawn"] == 1,
           f"CV3 tests by kind and state: {r['tests']}")
        ok(r["refusals"] == {"round plan": 1, "still working": 1, "other": 1}, f"CV4 refusals by hold: {r['refusals']}")
        ok(r["uq_runs_by_round"] == {1: 1, 2: 3, "after": 1} and "Uncertainty runs by round: r1 1, r2 3, "
           "after the last gate 1" in render(r),
           f"CV12 uncertainty runs counted in the round of the next gate; refused, self-test and other "
           f"scripts not counted; a same-round gate rerun keeps its round: {r['uq_runs_by_round']}")
        ok(r["compute"]["unrun_share"] == 0.2 and r["compute"]["core_hours"] == 2.0 and r["compute"]["stopped_calls"] == 1
           and r["compute"]["failed_share"] == 0.2,
           f"CV5 compute: {r['compute']}")
        ok("r1 5" in render(r) and "Spend: $12.5 of $450" in render(r), "CV6 rendering")
        ok(report(os.path.join(d, "nope"))["gates"] == {}, "CV7 an empty or missing run reports nothing, not a crash")
        json.dump({"core_seconds": 7200, "calls": [
            {"n": 10, "n_failed": 4, "stopped": "timed out", "ts": "t1", "task_core_s": 3600,
             "failed_by_kind": {"stopped": 4}, "purpose": "plan: impact library"},
            {"n": 10, "n_failed": 5, "stopped": None, "ts": "t2", "task_core_s": 3600,
             "failed_by_kind": {"infra": 5}, "purpose": "R-1, R-2"},
            {"n": 10, "n_failed": 0, "stopped": None, "ts": "t3", "task_core_s": 1800, "purpose": "R-1 retry"},
            {"n": 4, "n_failed": 4, "stopped": "interrupted: KeyboardInterrupt", "ts": "t4", "task_core_s": 360,
             "purpose": "Z-9"}]},
                  open(os.path.join(d, "models", "compute_ledger.json"), "w"))
        r2 = report(d)
        bp, bf = r2["compute"]["by_purpose"], r2["compute"]["by_finding"]
        ok(bp["plan: impact library"]["lost_core_hours"] == 0.4 and bp["R-1, R-2"]["lost_core_hours"] == 0.5
           and bp["Z-9"]["lost_core_hours"] == 0.1 and r2["compute"]["lost_core_hours"] == 1.0
           and list(bp)[0] == "plan: impact library",
           f"CV9 failed rows are charged pro rata, whatever their kind; largest purpose first: {bp}")
        ok(bf["R-1"] == {"core_hours": 1.0, "resolved_in_round": 2, "known": True}
           and bf["R-2"] == {"core_hours": 0.5, "resolved_in_round": None, "known": True}
           and bf["Z-9"]["known"] is False,
           f"CV10 core-hours per finding, split across the findings a batch names, joined to resolution: {bf}")
        ok("R-1 1.0 h (resolved r2)" in render(r2) and "Z-9 0.1 h (not in critiques)" in render(r2),
           f"CV11 rendering by finding: {render(r2)}")
    # CV13: a trace from before Phase 42 (80-character summaries, gate rounds cut off) takes
    # its rounds from validator_history times; a grep of the script is not a run.
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "validator_history.jsonl"), "w") as f:
            for rnd, at in ((1, "2026-10-21T10:00:00+00:00"), (2, "2026-10-21T12:00:00+00:00")):
                f.write(json.dumps({"round": rnd, "at": at, "action": "PATCH_OR_RETHINK", "findings": []}) + "\n")
        with open(os.path.join(d, "trace.jsonl"), "w") as f:
            for ts, summ in (("2026-10-21T02:00:00", "Bash: cd runs/x && python3 ../../scripts/propagate_uncertainty.py ."),
                             ("2026-10-21T04:00:00", "Bash: grep -n def scripts/propagate_uncertainty.py"),
                             ("2026-10-21T04:30:00", "Bash: python3 scripts/propagate_uncertainty.py runs/x --n"),
                             ("2026-10-21T04:40:00", "Bash: python3 scripts/validate_critique_yaml.py runs/2026-10-20_2133"),
                             ("2026-10-21T06:00:00", "Bash: nohup python3 -u scripts/propagate_uncertainty.py runs/x")):
                f.write(json.dumps({"type": "tool_use", "tool": "Bash", "ts": ts, "summary": summ}) + "\n")
        json.dump({"utc_offset_s": -7 * 3600}, open(os.path.join(d, "metadata.json"), "w"))
        r3 = report(d)
        ok(report(d, trace_utc_offset_s=0)["uq_runs_by_round"] == {1: 3},
           "CV15 (Codex) naive trace times are read in the run machine's zone (metadata.json), not this one's")
        ok(r3["uq_runs_by_round"] == {1: 1, 2: 1, "after": 1} and r3["uq_rounds_from"] == "validator_history"
           and "(rounds from validator_history)" in render(r3),
           f"CV13 an old trace is binned by validator-history times; a grep is not a run: {r3['uq_runs_by_round']}")
        with open(os.path.join(d, "trace.jsonl"), "a") as f:   # a recorded gate run for round 1 only
            f.write(json.dumps({"type": "tool_use", "tool": "Bash", "ts": "2026-10-21T06:30:00",
                                "scripts_run": ["validate_critique_yaml.py"],
                                "command": "python3 scripts/validate_critique_yaml.py runs/x --current-round 1"}) + "\n")
        r4 = report(d)
        ok(r4["uq_runs_by_round"] == r3["uq_runs_by_round"] and r4["uq_rounds_from"] == "validator_history",
           f"CV14 (Codex) trace gate runs that miss a recorded round are not used: {r4['uq_runs_by_round']}")
    if fails:
        print("SELF-TEST FAILURES:")
        for f in fails:
            print("  -", f)
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_dir", nargs="?")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--trace-utc-offset-h", type=float,
                    help="the run machine's UTC offset, for traces from before metadata.json recorded it")
    a = ap.parse_args(argv)
    if a.self_test:
        return _self_test()
    if not a.run_dir:
        ap.error("run_dir required")
    r = report(a.run_dir, None if a.trace_utc_offset_h is None else a.trace_utc_offset_h * 3600)
    print(json.dumps(r, indent=1, default=str) if a.json else render(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
