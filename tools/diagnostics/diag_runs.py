#!/usr/bin/env python3
"""The run records of the diagnostics contract (docs/design/diagnostic-data.md §6), read-only.

    python3 tools/diagnostics/diag_runs.py --self-test

Every read goes through the review workbench's confined reader (tools/workbench/workbench.py `Run`: the run folder
pinned by device and inode at discovery, each file opened below it with no link followed, hard-linked files
refused). A path with a `_truth` or `evals_private` component is never discovered or read. Each builder returns
(data, sources, notes, derived); mcp_server.py wraps it in the versioned envelope. Nothing here writes.
"""
from __future__ import annotations

import fnmatch
import json
import os
import statistics
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
for _p in (os.path.join(REPO, "tools", "workbench"), os.path.join(REPO, "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
import workbench as WB  # noqa: E402

REFUSED_PARTS = ("_truth", "evals_private")       # scripts/harness_metrics.py REFUSED_PARTS
HEARTBEAT_S = 300.0                               # agents/run_events.py HEARTBEAT_S default
OPEN = ("open", "owner_declined")                 # agents/findings_ledger.py open_findings
# run_artifact's allow-list (§8): harness records a diagnosis needs, nothing an agent drafts beyond the report
ARTIFACTS = ("metadata.json", "run_state.json", "events.jsonl", "findings.jsonl", "gate_r*.json", "final_gate.json",
             "validator_history.jsonl", "fix_plan_r*.json", "impact_ledger.jsonl", "scope_signoff_request.json",
             "spec_rethink_request.json", "decision_spec.yaml", "decision_spec.signoff_sheet.md", "engine_choice.json",
             "budget_ledger.json", "pipeline_progress.json", "models/pipeline_report.json", "models/compute_ledger.json",
             "models/logs/*.log", "report.md", "data/inputs_provenance.json", "analysis/*.yaml")
MAX_ARTIFACT = 256 * 1024
LOG_TAIL = 64 * 1024


def refused(path: str) -> bool:
    return any(p in REFUSED_PARTS for p in os.path.normpath(path).split(os.sep))


def discover(roots: dict) -> dict:
    """{run id: Run} for every run under the roots ({root name: folder}); the id is '<root>/<path under it>'."""
    out = {}
    for name, folder in roots.items():
        folder = os.path.realpath(os.path.expanduser(folder))
        if not os.path.isdir(folder) or refused(folder):
            continue
        for d in WB.discover([folder]):
            if refused(d):
                continue
            rid = f"{name}/{os.path.relpath(d, folder)}".rstrip("/.")
            out[rid] = WB.Run(d)
    return out


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds")


def _age(at, now: float):
    try:
        return round(now - datetime.fromisoformat(str(at)).timestamp(), 1)
    except (TypeError, ValueError):
        return None


def _mtime(run, rel):
    st, _ = run._open(rel, lambda fd, st: st)
    return _iso(st.st_mtime) if st is not None else None


def _src(run, rels):
    return [{"path": r, "mtime": _mtime(run, r)} for r in rels if _mtime(run, r)]


def jsonl(run, rel) -> tuple[list | None, str | None]:
    def row(n, raw):
        try:
            r = json.loads(raw)
        except ValueError:
            return None
        return r if isinstance(r, dict) else None
    return run.lines(rel, row)


def events(run, kind=None, since_seq=None, limit=200):
    """(events, derived): events.jsonl when the run has one, else rebuilt from history and journal (inferred)."""
    ev, _ = jsonl(run, "events.jsonl")
    if ev:
        derived = "record"
    else:
        st, _ = run.json("run_state.json")
        st, ev, derived = st or {}, [], "inferred"
        for h in st.get("history") or []:
            if isinstance(h, dict) and "to" in h:
                ev.append({"kind": "stage", "at": h.get("at"), "from": h.get("from"), "to": h.get("to"),
                           "why": h.get("why"), "round": h.get("round"), "spent_usd": h.get("spent_usd")})
            elif isinstance(h, dict) and h.get("paused"):
                ev.append({"kind": "owner_question", "at": h.get("at"), "paused": h.get("paused"),
                           "state": h.get("state")})
        for k, j in (st.get("journal") or {}).items():
            if isinstance(j, dict) and j.get("started"):
                ev.append({"kind": "task_start", "at": j["started"], "key": k, "task": j.get("task")})
            if isinstance(j, dict) and j.get("completed"):
                ev.append({"kind": "task_end", "at": j["completed"], "key": k, "task": j.get("task"),
                           "ok": j.get("ok"), "usd": j.get("usd")})
        ev.sort(key=lambda e: str(e.get("at") or ""))
    sel = [e for e in ev if (kind is None or e.get("kind") == kind)
           and (since_seq is None or (isinstance(e.get("seq"), int) and e["seq"] > since_seq))]
    return sel[-int(limit):] if limit else sel, derived, len(ev)


def run_status(run, now=None):
    now = time.time() if now is None else now
    st, why = run.json("run_state.json")
    if st is None:
        return None, [], [why], {}
    meta, _ = run.json("metadata.json")
    meta = meta or {}
    ev, derived, _n = events(run, limit=0)
    last = ev[-1] if ev else None
    hb = next((e for e in reversed(ev) if e.get("kind") == "heartbeat"), None)
    running = next(({"key": k, "task": j.get("task"), "started": j.get("started"),
                     "elapsed_s": _age(j.get("started"), now)}
                    for k, j in (st.get("journal") or {}).items() if isinstance(j, dict) and j.get("status") == "started"),
                   None)
    hist = [h for h in st.get("history") or [] if isinstance(h, dict)]
    since = next((h.get("at") for h in reversed(hist) if "to" in h), None)
    led, _ = jsonl(run, "findings.jsonl")
    q = None
    if st.get("paused") and st.get("state") != "FINAL":
        req, _ = run.json("scope_signoff_request.json") if st["paused"] == "awaiting_scope_signoff" else (None, None)
        q = {"kind": st["paused"], "items": [i.get("finding") for i in (req or {}).get("items") or []
                                             if isinstance(i, dict)]}
    last_age = _age(last.get("at"), now) if last else None
    live = st.get("state") != "FINAL" and not st.get("paused")
    notes = []
    stale = bool(live and last_age is not None and last_age > 3 * HEARTBEAT_S)
    if stale:
        notes.append(f"stale: the last event is {last_age:.0f} s old while the run is neither FINAL nor paused; on a "
                     "cloud run the local mirror may be behind, or the process stopped")
    data = {"run": run.path, "question": st.get("question"), "pipeline": meta.get("pipeline"),
            "commit": meta.get("commit"), "state": st.get("state"), "round": st.get("round"),
            "max_rounds": st.get("max_rounds"), "status": st.get("status"), "paused": st.get("paused"),
            "since": since, "running_task": running, "last_event_age_s": last_age,
            "last_heartbeat_age_s": _age(hb.get("at"), now) if hb else None,
            "eta": {"s": hb.get("eta_s"), "basis": hb.get("basis")} if hb and hb.get("eta_s") is not None else None,
            "spent_usd": st.get("spent_usd"), "cap_usd": st.get("budget_usd"),
            "open_findings": sum(1 for f in led or [] if f.get("status") in OPEN), "owner_question": q,
            "events_dropped": st.get("events_dropped"), "stale": stale}
    srcs = _src(run, ["run_state.json", "metadata.json", "events.jsonl", "findings.jsonl"])
    return data, srcs, notes, {"events": derived, "eta": "record" if data["eta"] else "unavailable"}


def _journal_rounds(st):
    """{(round, task id): usd} of the fix loop's journal keys FIX_LOOP#<v>:r<N>:fix:<task id>:a<k>."""
    out = {}
    for k, j in (st.get("journal") or {}).items():
        parts = str(k).split(":")
        if k.startswith("FIX_LOOP#") and len(parts) >= 4 and parts[2] == "fix" and parts[1][1:].isdigit():
            key = (int(parts[1][1:]), parts[3])
            out[key] = out.get(key, 0.0) + float((j or {}).get("usd") or 0.0)
    return out


def run_findings(run, status=None):
    led, why = jsonl(run, "findings.jsonl")
    if led is None:
        return None, [], [why], {}
    st, _ = run.json("run_state.json")
    st = st or {}
    spend = _journal_rounds(st)
    tasks = {}                                   # finding id -> [{round, task_id, usd}]
    for rn in range(1, int(st.get("max_rounds") or 3) + 3):
        plan, _ = run.json(f"fix_plan_r{rn}.json")
        rows = ((plan or {}).get("plan") or {}).get("tasks") if isinstance((plan or {}).get("plan"), dict) else None
        for t in rows if isinstance(rows, list) else []:
            if isinstance(t, dict):
                for fid in t.get("findings") if isinstance(t.get("findings"), list) else []:
                    tasks.setdefault(str(fid), []).append({"round": rn, "task_id": t.get("id"),
                                                           "usd": spend.get((rn, str(t.get("id"))))})
    cur = st.get("round")
    out = []
    for f in led:
        if status and f.get("status") != status:
            continue
        a, b = f.get("first_seen_round"), f.get("last_seen_round") or cur
        out.append({k: f.get(k) for k in ("id", "kind", "severity", "status", "blocking", "owner_state",
                                          "first_seen_round", "last_seen_round", "claim", "fix_requires", "events")}
                   | {"rounds_open": (b - a + 1) if isinstance(a, int) and isinstance(b, int) else None,
                      "fix_tasks": tasks.get(str(f.get("id")), [])})
    out.sort(key=lambda r: (-(r["rounds_open"] or 0), str(r["id"])))
    return {"findings": out}, _src(run, ["findings.jsonl", "run_state.json"]), [], {
        "rounds_open": "record", "fix_tasks": "inferred"}


def _stage_times(hist):
    """{state: seconds spent in it}, from consecutive transitions."""
    out, prev = {}, None
    for h in hist:
        if not isinstance(h, dict) or "to" not in h:
            continue
        if prev is not None:
            dt = _age(prev["at"], datetime.fromisoformat(str(h["at"])).timestamp()) if h.get("at") else None
            if dt is not None:
                out[h["from"]] = round(out.get(h["from"], 0.0) + dt, 1)
        prev = h
    return out


def run_spend(run):
    st, why = run.json("run_state.json")
    if st is None:
        return None, [], [why], {}
    by_stage, by_task = {}, []
    for k, j in (st.get("journal") or {}).items():
        if not isinstance(j, dict) or j.get("task") == "harness":
            continue
        stage = str(k).split("#", 1)[0]
        usd = float(j.get("usd") or 0.0)
        s = by_stage.setdefault(stage, {"usd": 0.0, "tasks": 0, "elapsed_s": None})
        s["usd"], s["tasks"] = round(s["usd"] + usd, 4), s["tasks"] + 1
        by_task.append({"key": k, "task": j.get("task"), "status": j.get("status"), "usd": usd,
                        "allowance_usd": j.get("allowance_usd")})
    for stage, sec in _stage_times(st.get("history") or []).items():
        by_stage.setdefault(stage, {"usd": 0.0, "tasks": 0, "elapsed_s": None})["elapsed_s"] = sec
    comp, _ = run.json("models/compute_ledger.json")
    compute = None
    if comp:
        calls = comp.get("calls")
        compute = {"calls": len(calls) if isinstance(calls, list) else None,
                   **{k: v for k, v in comp.items() if "core_hours" in k and isinstance(v, (int, float))}}
    return ({"cap_usd": st.get("budget_usd"), "spent_usd": st.get("spent_usd"), "by_stage": by_stage,
             "by_task": by_task, "compute": compute},
            _src(run, ["run_state.json", "models/compute_ledger.json"]),
            [] if compute else ["no models/compute_ledger.json: no cloud compute recorded"],
            {"by_stage.usd": "record", "by_stage.elapsed_s": "inferred", "compute": "record" if compute else "unavailable"})


def run_compare(run, others):
    """This run's elapsed time and spend by stage against the FINAL runs of the same question."""
    st, why = run.json("run_state.json")
    if st is None:
        return None, [], [why], {}
    q = " ".join(str(st.get("question") or "").lower().split())
    ref = {}
    n = 0
    for o in others:
        if o.path == run.path:
            continue
        so, _ = o.json("run_state.json")
        if not so or so.get("state") != "FINAL" or " ".join(str(so.get("question") or "").lower().split()) != q:
            continue
        n += 1
        sp = run_spend(o)[0] or {}
        for stage, v in (sp.get("by_stage") or {}).items():
            r = ref.setdefault(stage, {"s": [], "usd": []})
            if v.get("elapsed_s") is not None:
                r["s"].append(v["elapsed_s"])
            r["usd"].append(v.get("usd") or 0.0)
    this = run_spend(run)[0] or {}
    reference = {s: {"median_s": statistics.median(v["s"]) if v["s"] else None,
                     "median_usd": round(statistics.median(v["usd"]), 4) if v["usd"] else None, "n": len(v["usd"])}
                 for s, v in ref.items()}
    seen = [h.get("to") for h in st.get("history") or [] if isinstance(h, dict) and "to" in h]
    notes = [] if n else ["no FINAL run of the same question under the roots: nothing to compare with"]
    return ({"question": st.get("question"), "n_runs": n, "this": {"state": st.get("state"),
                                                                    "by_stage": this.get("by_stage")},
             "reference": reference,
             "remaining_stages": [s for s in reference if s not in seen and st.get("state") != "FINAL"]},
            _src(run, ["run_state.json"]), notes, {"reference": "inferred"})


def run_artifact(run, rel):
    rel = WB.check_rel(rel)
    if refused(rel) or not any(fnmatch.fnmatchcase(rel, p) for p in ARTIFACTS):
        raise ValueError(f"{rel}: not in run_artifact's allow-list ({', '.join(ARTIFACTS)})")
    is_log = rel.endswith(".log")
    b, why = run.read(rel, None if is_log else MAX_ARTIFACT + 1)
    if b is None:
        return None, [], [why], {}
    notes = []
    if is_log and len(b) > LOG_TAIL:
        b, notes = b[-LOG_TAIL:], [f"the last {LOG_TAIL // 1024} KB of the log"]
    elif len(b) > MAX_ARTIFACT:
        b, notes = b[:MAX_ARTIFACT], [f"cut at {MAX_ARTIFACT // 1024} KB"]
    text = b.decode("utf-8", "replace")
    data = {"path": rel, "text": text}
    if rel.endswith(".json") and not notes:
        try:
            data = {"path": rel, "json": json.loads(text)}
        except ValueError:
            pass
    return data, _src(run, [rel]), notes, {}


def owner_inbox(runs: dict):
    """Every run waiting on the owner: its question, since when, and the command that answers it."""
    how = {"awaiting_signoff": "python3 agents/stage_machine.py sign <run> --by owner (after reading the sign-off sheet)",
           "awaiting_scope_signoff": "python3 scripts/scope_signoff.py show <run>, then sign <run> --by owner",
           "awaiting_spec_amendment": "read spec_rethink_request.json, amend the spec, relaunch with reuse",
           "awaiting_budget": "python3 main.py --v2 --resume <run> --max-budget-usd <higher cap>"}
    items = []
    for rid, run in sorted(runs.items()):
        st, _ = run.json("run_state.json")
        if not st or st.get("state") == "FINAL" or not st.get("paused"):
            continue
        p = next((h for h in reversed(st.get("history") or []) if isinstance(h, dict) and h.get("paused")), {})
        items.append({"where": "run", "ref": rid, "question": st["paused"], "state": st.get("state"),
                      "since": p.get("at"), "how_to_answer": how.get(st["paused"], "see the run's history")})
    return {"items": items}, [], [], {"items": "record"}


def _self_test() -> int:
    import tempfile
    fails = []

    def ok(c, label):
        print(("  ok   " if c else "  FAIL ") + label)
        if not c:
            fails.append(label)

    with tempfile.TemporaryDirectory() as td:
        runs_root = os.path.join(td, "runs")
        a = make_fixture(runs_root, "a_run", paused=None)
        b = make_fixture(runs_root, "b_run", paused="awaiting_scope_signoff", events=False)
        os.makedirs(os.path.join(td, "runs", "_truth", "x"))
        open(os.path.join(td, "runs", "_truth", "x", "run_state.json"), "w").write("{}")
        found = discover({"runs": runs_root})
        ok(sorted(found) == ["runs/a_run", "runs/b_run"], f"DR1 discovery skips _truth ({sorted(found)})")
        now = datetime.fromisoformat("2026-11-24T10:20:00+00:00").timestamp()
        d, srcs, notes, der = run_status(found["runs/a_run"], now)
        ok(d["state"] == "CRITIQUE" and d["round"] == 2 and d["running_task"]["key"] == "CRITIQUE#2:r2:critique-domain"
           and d["eta"] == {"s": 120.0, "basis": "b"} and d["open_findings"] == 1 and der["events"] == "record"
           and d["last_heartbeat_age_s"] == 600.0 and not d["stale"], f"DR2 run_status from the records ({d})")
        d2 = run_status(found["runs/a_run"], now + 3600)[0]
        ok(d2["stale"], "DR3 an old last event on a live run is flagged stale")
        db = run_status(found["runs/b_run"], now)
        ok(db[0]["owner_question"] == {"kind": "awaiting_scope_signoff", "items": ["D-001"]} and db[3]["events"] == "inferred",
           f"DR4 a paused run names its question; events rebuilt from history ({db[0]['owner_question']})")
        evb, der_b, _ = events(found["runs/b_run"])
        ok([e["kind"] for e in evb][:2] == ["task_start", "stage"] and der_b == "inferred",
           f"DR5 a run without events.jsonl gets events from history and journal ({[e['kind'] for e in evb]})")
        fnd = run_findings(found["runs/a_run"])[0]["findings"]
        ok(fnd[0]["id"] == "D-001" and fnd[0]["rounds_open"] == 2 and fnd[0]["fix_tasks"] == [
            {"round": 1, "task_id": "t1", "usd": 0.5}], f"DR6 findings with rounds open and their fix tasks ({fnd[:1]})")
        sp = run_spend(found["runs/a_run"])[0]
        ok(sp["by_stage"]["FIX_LOOP"]["usd"] == 0.5 and sp["by_stage"]["EVIDENCE"]["elapsed_s"] == 60.0,
           f"DR7 spend and time by stage ({sp['by_stage']})")
        try:
            run_artifact(found["runs/a_run"], "secrets.yaml")
            ok(False, "DR8 run_artifact refuses a path outside its allow-list")
        except ValueError:
            ok(True, "DR8 run_artifact refuses a path outside its allow-list")
        try:
            run_artifact(found["runs/a_run"], "../b_run/run_state.json")
            ok(False, "DR8b run_artifact refuses a parent path")
        except ValueError:
            ok(True, "DR8b run_artifact refuses a parent path")
        art = run_artifact(found["runs/a_run"], "run_state.json")[0]
        ok(art["json"]["state"] == "CRITIQUE", "DR9 run_artifact returns an allow-listed record, parsed")
        os.symlink(os.path.join(td, "elsewhere.json"), os.path.join(a, "final_gate.json"))
        open(os.path.join(td, "elsewhere.json"), "w").write("{}")
        ok(run_artifact(found["runs/a_run"], "final_gate.json")[0] is None, "DR10 a link inside the run is not followed")
        inbox = owner_inbox(found)[0]["items"]
        ok([i["ref"] for i in inbox] == ["runs/b_run"] and "scope_signoff.py" in inbox[0]["how_to_answer"],
           f"DR11 the owner inbox lists the paused run and how to answer ({inbox})")
        cmp_ = run_compare(found["runs/a_run"], list(found.values()))
        ok(cmp_[0]["n_runs"] == 0 and cmp_[2], "DR12 run_compare says when there is nothing to compare with")
    if fails:
        print(f"FAIL: {len(fails)} self-test case(s) failed")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def make_fixture(root: str, name: str, paused=None, events=True) -> str:
    """A small synthetic run folder (no run values): states, a journal, one finding, one fix plan."""
    d = os.path.join(root, name)
    os.makedirs(d)
    hist = [{"at": "2026-11-24T10:00:00+00:00", "from": "SPEC", "to": "EVIDENCE", "round": 1},
            {"at": "2026-11-24T10:01:00+00:00", "from": "EVIDENCE", "to": "SIGN", "round": 1},
            {"at": "2026-11-24T10:05:00+00:00", "from": "GATE", "to": "FIX_LOOP", "round": 1},
            {"at": "2026-11-24T10:10:00+00:00", "from": "FIX_LOOP", "to": "CRITIQUE", "round": 1}]
    if paused:
        hist.append({"at": "2026-11-24T10:11:00+00:00", "paused": paused, "state": "OWNER_DECISION"})
    st = {"question": "toy question", "state": "OWNER_DECISION" if paused else "CRITIQUE", "round": 2, "max_rounds": 3,
          "paused": paused, "status": None, "spent_usd": 1.5, "budget_usd": 20.0, "history": hist,
          "journal": {"SPEC#1:draft": {"task": "draft_spec", "status": "completed", "usd": 1.0,
                                       "started": "2026-11-24T09:59:00+00:00", "completed": "2026-11-24T10:00:00+00:00"},
                      "FIX_LOOP#1:r1:fix:t1:a1": {"task": "fix", "status": "completed", "usd": 0.5,
                                                  "started": "2026-11-24T10:06:00+00:00",
                                                  "completed": "2026-11-24T10:08:00+00:00"},
                      "CRITIQUE#2:r2:critique-domain": {"task": "critique", "status": "started",
                                                        "started": "2026-11-24T10:10:00+00:00"}}}
    json.dump(st, open(os.path.join(d, "run_state.json"), "w"))
    json.dump({"pipeline": "v2", "commit": "abc1234"}, open(os.path.join(d, "metadata.json"), "w"))
    with open(os.path.join(d, "findings.jsonl"), "w") as f:
        f.write(json.dumps({"id": "D-001", "kind": "critic", "severity": "HIGH", "status": "open",
                            "first_seen_round": 1, "claim": "toy claim", "events": []}) + "\n")
        f.write(json.dumps({"id": "G-002", "kind": "gate", "severity": "MEDIUM", "status": "fixed",
                            "first_seen_round": 1, "last_seen_round": 1, "events": []}) + "\n")
    json.dump({"plan": {"tasks": [{"id": "t1", "findings": ["D-001"]}]}}, open(os.path.join(d, "fix_plan_r1.json"), "w"))
    if paused:
        json.dump({"pending": True, "items": [{"finding": "D-001"}]}, open(os.path.join(d, "scope_signoff_request.json"), "w"))
    if events:
        with open(os.path.join(d, "events.jsonl"), "w") as f:
            f.write(json.dumps({"v": 1, "seq": 1, "kind": "stage", "at": "2026-11-24T10:10:00+00:00"}) + "\n")
            f.write(json.dumps({"v": 1, "seq": 2, "kind": "heartbeat", "at": "2026-11-24T10:10:00+00:00",
                                "eta_s": 120.0, "basis": "b"}) + "\n")
            f.write("not json\n")
    return d


if __name__ == "__main__":
    if "--self-test" in sys.argv[1:]:
        sys.exit(_self_test())
    print(__doc__)
    sys.exit(2)
