#!/usr/bin/env python3
"""The run records of the diagnostics contract (docs/design/diagnostic-data.md §6), read-only.

    python3 tools/diagnostics/diag_runs.py --self-test

Every read goes through the review workbench's confined reader (tools/workbench/workbench.py `Run`: the run folder
pinned by device and inode at discovery, each file opened below it with no link followed, hard-linked files
refused). A path with a `_truth` or `evals_private` component is never discovered or read. Each builder returns
(data, sources, notes, derived); mcp_server.py wraps it in the versioned envelope. Nothing here writes.
"""
from __future__ import annotations

import json
import os
import re
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


def refused(path: str) -> bool:
    return any(p in REFUSED_PARTS for p in os.path.normpath(path).split(os.sep))


def discover(roots: dict, max_depth: int = 9) -> dict:
    """{run id: Run} for every run under the roots ({root name: folder}); the id is '<root>/<path under it>'.
    `_truth` and `evals_private` are pruned before they are entered (Codex r1 of #386), links are not followed, and
    a folder whose pinned identity is not inside its root (swapped after the walk) is left out."""
    out = {}
    for name, folder in roots.items():
        folder = os.path.realpath(os.path.expanduser(folder))
        if not os.path.isdir(folder) or refused(folder):
            continue
        for d, dirs, files in os.walk(folder, followlinks=False):
            depth = d[len(folder):].count(os.sep)
            if "run_state.json" in files:
                dirs[:] = []
                run = WB.Run(d)
                if run.ident == (None, None) or not (run.path == folder or run.path.startswith(folder + os.sep)):
                    continue
                rel = os.path.relpath(d, folder)
                out[name if rel == "." else f"{name}/{rel.replace(os.sep, '/')}"] = run
                continue
            dirs[:] = [] if depth >= max_depth else [
                x for x in dirs if x not in WB.SKIP_DIRS and x not in REFUSED_PARTS and not x.startswith(".")]
    return out


def last_written(run) -> str | None:
    """The newest of the run's own harness records (never a scan of its subtree, which may hold private folders)."""
    ts = [_mtime(run, r) for r in ("run_state.json", "events.jsonl", "findings.jsonl", "metadata.json")]
    ts = [t for t in ts if t]
    return max(ts) if ts else None


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
        for i, e in enumerate(ev, 1):
            e["seq"] = i                       # synthetic, stable for the same records (Codex r1 of #386)
    sel = [e for e in ev if (kind is None or e.get("kind") == kind)
           and (since_seq is None or (isinstance(e.get("seq"), int) and e["seq"] > since_seq))]
    if not limit:
        return sel, derived, len(ev)
    # with a cursor: the next `limit` events after it; without one: the latest `limit` (Codex r1 of #386)
    return (sel[:int(limit)] if since_seq is not None else sel[-int(limit):]), derived, len(ev)


def run_status(run, now=None):
    now = time.time() if now is None else now
    st, why = run.json("run_state.json")
    if st is None:
        return None, [], [why], {}
    meta, _ = run.json("metadata.json")
    meta = meta or {}
    ev, derived, _n = events(run, limit=0)
    last = ev[-1] if ev else None
    hb = None
    for e in reversed(ev):                     # a heartbeat of the step still running, never an earlier one's
        if e.get("kind") == "heartbeat":
            hb = e
            break
        if e.get("kind") in ("stage", "task_start", "task_end", "status"):
            break
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
    notes = [] if led is not None else ["findings.jsonl could not be read: the open-finding count is unknown"]
    if not live:
        hb = None
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
            "open_findings": sum(1 for f in led if f.get("status") in OPEN) if led is not None else None,
            "owner_question": q,
            "events_dropped": st.get("events_dropped"), "stale": stale}
    srcs = _src(run, ["run_state.json", "metadata.json", "events.jsonl", "findings.jsonl"])
    return data, srcs, notes, {"events": derived, "eta": "record" if data["eta"] else "unavailable",
                               "open_findings": "record" if led is not None else "unavailable"}


def charged(j: dict, st: dict) -> float:
    """What a journal entry cost the run: its usd plus each interrupted charge (its allowance, the task cap when it
    had none, and its read-back allowance), as Machine._interrupted_charge counts it (Codex r1 of #386)."""
    a, rb = j.get("allowance_usd"), j.get("readback_usd")
    per = float(a if a is not None else st.get("task_cap_usd") or 3.0) + float(rb if isinstance(rb, (int, float)) else 0)
    return round(float(j.get("usd") or 0.0) + int(j.get("interrupted_charged") or 0) * per, 4)


def _journal_rounds(st):
    """{(round, task id): usd} of the fix loop's journal keys FIX_LOOP#<v>:r<N>:fix:<task id>:a<k>."""
    out = {}
    for k, j in (st.get("journal") or {}).items():
        parts = str(k).split(":")
        if k.startswith("FIX_LOOP#") and len(parts) >= 4 and parts[2] == "fix" and parts[1][1:].isdigit():
            key = (int(parts[1][1:]), parts[3])
            out[key] = out.get(key, 0.0) + charged(j or {}, st)
    return out


def run_findings(run, status=None):
    led, why = jsonl(run, "findings.jsonl")
    if led is None:
        return None, [], [why], {}
    st, _ = run.json("run_state.json")
    st = st or {}
    spend = _journal_rounds(st)
    tasks = {}                                   # finding id -> [{round, task_id, usd}]
    try:
        fd = run._root()
        try:
            names = os.listdir(fd)
        finally:
            os.close(fd)
    except OSError:
        names = []
    for rn in sorted(int(m.group(1)) for m in (re.fullmatch(r"fix_plan_r(\d+)\.json", n) for n in names) if m):
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
        if f.get("status") not in OPEN:        # closed: open until the round its status was set (Codex r1 of #386)
            b = next((e.get("round") for e in reversed(f.get("events") or []) if isinstance(e, dict)
                      and e.get("status") == f.get("status") and isinstance(e.get("round"), int)), b)
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
        usd = charged(j, st)
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
                   **{k: v for k, v in comp.items() if "core" in k and isinstance(v, (int, float))}}
        if isinstance(comp.get("core_seconds"), (int, float)):
            compute["core_hours_used"] = round(comp["core_seconds"] / 3600.0, 3)
    return ({"cap_usd": st.get("budget_usd"), "spent_usd": st.get("spent_usd"), "by_stage": by_stage,
             "by_task": by_task, "compute": compute},
            _src(run, ["run_state.json", "models/compute_ledger.json"]),
            [] if compute else ["no models/compute_ledger.json: no cloud compute recorded"],
            {"by_stage.usd": "record", "by_stage.elapsed_s": "inferred", "compute": "record" if compute else "unavailable"})


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
        ev_all = events(found["runs/b_run"], limit=0)[0]
        p1 = events(found["runs/b_run"], since_seq=0, limit=2)[0]
        p2 = events(found["runs/b_run"], since_seq=p1[-1]["seq"], limit=2)[0]
        ok([e["seq"] for e in p1 + p2] == [e["seq"] for e in ev_all][:4] and p1[0]["seq"] == 1,
           f"DR14 rebuilt events carry sequence numbers and pages follow the cursor forward ({[e['seq'] for e in p1 + p2]})")
        st_b = json.load(open(os.path.join(b, "run_state.json")))
        st_b["journal"]["SPEC#2:x"] = {"task": "draft_spec", "status": "interrupted", "allowance_usd": 2.0,
                                       "interrupted_charged": 1}
        json.dump(st_b, open(os.path.join(b, "run_state.json"), "w"))
        spb = run_spend(discover({"runs": runs_root})["runs/b_run"])[0]
        ok(spb["by_stage"]["SPEC"]["usd"] == 3.0, f"DR15 an interrupted task's charge counts in its stage ({spb['by_stage']['SPEC']})")
        os.remove(os.path.join(a, "findings.jsonl"))
        d3 = run_status(discover({"runs": runs_root})["runs/a_run"], now)
        ok(d3[0]["open_findings"] is None and d3[2], "DR16 an unreadable ledger is an unknown count, with a note")
        os.makedirs(os.path.join(runs_root, "a_run", "_truth"))
        os.makedirs(os.path.join(runs_root, "trial"))
        os.makedirs(os.path.join(runs_root, "trial."))
        for x in ("trial", "trial."):
            json.dump({"state": "SPEC"}, open(os.path.join(runs_root, x, "run_state.json"), "w"))
        ok({"runs/trial", "runs/trial."} <= set(discover({"runs": runs_root})), "DR17 sibling folders keep their own ids")
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
