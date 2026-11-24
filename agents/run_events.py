#!/usr/bin/env python3
"""The run event stream: `events.jsonl`, one JSON object per line, appended by the v2 harness.

    python3 agents/run_events.py --self-test

Design and schema: docs/design/diagnostic-data.md §7. What happened when: stage transitions, tasks, rounds, gate
decisions, finding status changes, owner questions and answers, heartbeats while a long step runs, and the run's
terminal or paused status. Each kind is written from one place (the stage machine, the findings ledger's save).

Rules:
  - appended through lib.safe_io (no link followed at any component; a file with other hard links is refused), one
    O_APPEND write per line, a line at most MAX_LINE bytes (long text is cut);
  - never fatal: `emit` returns False on any failure and counts it; the next event that succeeds carries
    `dropped_before` (the count since the last success in this process);
  - outside every hashed or protected record (not in the integrity record, not under models/ or data/, excluded
    from pipeline step inputs), so reuse keys do not change;
  - no secrets: callers pass harness fields only (keys, states, counts, the ledger's own claim text, cut).
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

EVENTS = "events.jsonl"
SCHEMA_VERSION = 1
MAX_LINE = 4096
MAX_TEXT = 300
HEARTBEAT_S = float(os.environ.get("MA_EVENTS_HEARTBEAT_S") or 300.0)
PROGRESS = "pipeline_progress.json"        # scripts/pipeline.py PROGRESS: a step's projection past its soft deadline
# The owner question each pause asks, and the file that holds it.
QUESTIONS = {"awaiting_signoff": ("spec_signoff", "decision_spec.signoff_sheet.md"),
             "awaiting_scope_signoff": ("scope_signoff", "scope_signoff_request.json"),
             "awaiting_spec_amendment": ("spec_amendment", "spec_rethink_request.json"),
             "awaiting_budget": ("budget", None)}

_LOCK = threading.Lock()
_SEQ = [0]
_DROPPED: dict[str, int] = {}             # run dir -> appends failed since the last success (this process)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _cut(v, n: int = MAX_TEXT):
    if isinstance(v, str):
        return v if len(v) <= n else v[:n - 1] + "…"
    if isinstance(v, float):
        return round(v, 4)
    if isinstance(v, (list, tuple)):
        return [_cut(x, n) for x in list(v)[:50]]
    if isinstance(v, dict):
        return {str(k): _cut(x, n) for k, x in list(v.items())[:50]}
    return v


def _line(rec: dict) -> bytes:
    """One line, at most MAX_LINE bytes: long text is cut harder, then list fields become their length."""
    data = json.dumps(rec, default=str, ensure_ascii=False)
    if len(data.encode()) > MAX_LINE:
        rec = {k: _cut(v, 80) for k, v in rec.items()}
        data = json.dumps(rec, default=str, ensure_ascii=False)
    if len(data.encode()) > MAX_LINE:
        rec = {k: (len(v) if isinstance(v, (list, dict)) else v) for k, v in rec.items()}
        rec["cut"] = True
        data = json.dumps(rec, default=str, ensure_ascii=False)
    return (data.encode()[:MAX_LINE - 1] if len(data.encode()) >= MAX_LINE else data.encode()) + b"\n"


def emit(run_dir: str, kind: str, state=None, round_n=None, **fields) -> bool:
    """Append one event. True when it was written; False (counted, never raised) when it was not."""
    key = os.path.abspath(run_dir) if isinstance(run_dir, str) else str(run_dir)
    try:
        with _LOCK:
            _SEQ[0] += 1
            rec = {"v": SCHEMA_VERSION, "at": _now(), "seq": _SEQ[0], "pid": os.getpid(), "kind": str(kind),
                   "state": state, "round": round_n}
            rec.update({k: _cut(v) for k, v in fields.items()})
            if _DROPPED.get(key):
                rec["dropped_before"] = _DROPPED[key]
            from lib.safe_io import write_nofollow
            write_nofollow(run_dir, EVENTS, _line(rec), append=True)
            _DROPPED.pop(key, None)
        return True
    except Exception:  # noqa: BLE001 -- a diagnostic record never stops the run
        with _LOCK:
            _DROPPED[key] = _DROPPED.get(key, 0) + 1
        return False


def pipeline_progress(run_dir: str) -> dict:
    """{progress, eta_s, basis} from pipeline_progress.json's running step, read with no link followed; {} if none."""
    try:
        from lib.safe_io import read_nofollow
        doc = json.loads(read_nofollow(run_dir, PROGRESS, max_bytes=1 << 20)[0])
        steps = doc.get("steps") if isinstance(doc, dict) else None
        run = [(k, s) for k, s in (steps or {}).items() if isinstance(s, dict) and s.get("state") == "running"]
        if not run:
            return {}
        sid, s = run[-1]
        tot, el = s.get("projected_total_s"), s.get("elapsed_s")
        eta = round(tot - el, 1) if isinstance(tot, (int, float)) and isinstance(el, (int, float)) else None
        return {"progress": f"step {sid}", "eta_s": eta, "basis": s.get("projection_basis")}
    except Exception:  # noqa: BLE001 -- no progress known
        return {}


@contextlib.contextmanager
def heartbeat(run_dir: str, step: str, state=None, round_n=None, every_s: float | None = None, progress=None):
    """While the block runs: a `heartbeat` event every `every_s` seconds (HEARTBEAT_S by default) with the elapsed
    time and, when `progress()` knows them, the step's progress and ETA. The timer thread never raises."""
    every = HEARTBEAT_S if every_s is None else float(every_s)
    stop, t0 = threading.Event(), time.monotonic()
    prog = progress or (lambda: pipeline_progress(run_dir))

    def beat():
        while not stop.wait(every):
            try:
                p = prog() or {}
            except Exception:  # noqa: BLE001
                p = {}
            emit(run_dir, "heartbeat", state, round_n, step=step, elapsed_s=round(time.monotonic() - t0, 1),
                 progress=p.get("progress"), eta_s=p.get("eta_s"), basis=p.get("basis"))
    th = None
    if every > 0:
        try:
            th = threading.Thread(target=beat, daemon=True, name=f"events-heartbeat-{step}"[:60])
            th.start()
        except RuntimeError:           # the OS refused a thread: the step runs without heartbeats
            th = None
    try:
        yield
    finally:
        stop.set()
        if th is not None:
            th.join(timeout=2.0)


def finding_changes(old: dict, new: dict) -> list[dict]:
    """The status changes between two ledgers ({id: Finding-like}): one row per id whose status differs."""
    out = []
    for fid, f in sorted(new.items()):
        o = old.get(fid)
        a, b = getattr(o, "status", None) if o is not None else None, getattr(f, "status", None)
        if a != b:
            ev = (getattr(f, "events", None) or [{}])[-1]
            out.append({"id": fid, "finding_kind": getattr(f, "kind", None), "severity": getattr(f, "severity", None),
                        "from": a, "to": b, "by": ev.get("by") if isinstance(ev, dict) else None})
    return out


def question_of(paused: str | None) -> tuple[str | None, str | None]:
    """(question kind, request file) for a pause reason; (the reason, None) for one not listed here."""
    return QUESTIONS.get(paused or "", (paused, None))


def read(run_dir: str) -> list[dict]:
    """The events of a run (lines that do not parse are skipped), read with no link followed."""
    from lib.safe_io import read_nofollow
    try:
        data = read_nofollow(run_dir, EVENTS)[0]
    except (FileNotFoundError, OSError, ValueError):
        return []
    out = []
    for ln in data.decode("utf-8", "replace").splitlines():
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if isinstance(r, dict):
            out.append(r)
    return out


def _self_test() -> int:
    import tempfile
    fails = []

    def ok(c, label):
        print(("  ok   " if c else "  FAIL ") + label)
        if not c:
            fails.append(label)

    with tempfile.TemporaryDirectory() as td:
        d = os.path.join(td, "run")
        os.makedirs(d)
        ok(emit(d, "stage", "SPEC", 1, **{"from": "SPEC", "to": "EVIDENCE", "why": "x" * 5000}), "EV1 emit writes")
        ev = read(d)
        ok(len(ev) == 1 and ev[0]["v"] == SCHEMA_VERSION and ev[0]["kind"] == "stage" and ev[0]["round"] == 1
           and len(ev[0]["why"]) == MAX_TEXT and {"at", "seq", "pid", "state"} <= set(ev[0]),
           "EV2 one versioned line with the common fields; long text cut")
        big = emit(d, "finding", "GATE", 2, items=["y" * 290] * 50)
        last = read(d)[-1]
        with open(os.path.join(d, EVENTS), "rb") as fh:
            lines = fh.read().splitlines()
        ok(big and all(len(x) < MAX_LINE for x in lines) and last["kind"] == "finding",
           "EV3 a line never exceeds MAX_LINE")
        # never fatal: a directory at the name, then a link, are refused and counted, never raised
        e2 = os.path.join(td, "run2")
        os.makedirs(os.path.join(e2, EVENTS))
        ok(emit(e2, "status") is False and emit(e2, "status") is False, "EV4 a failed append returns False, no raise")
        os.rmdir(os.path.join(e2, EVENTS))
        ok(emit(e2, "status", status="completed") and read(e2)[-1].get("dropped_before") == 2,
           "EV5 the next written event says how many were dropped before it")
        ok("dropped_before" not in (emit(e2, "status") and read(e2)[-1]), "EV5b the count resets after a success")
        e3 = os.path.join(td, "run3")
        os.makedirs(e3)
        target = os.path.join(td, "elsewhere.jsonl")
        open(target, "w").close()
        os.symlink(target, os.path.join(e3, EVENTS))
        ok(emit(e3, "status") is False and os.path.getsize(target) == 0, "EV6 a link at the name is never followed")
        ok(emit(os.path.join(td, "missing", "deeper"), "status") in (True, False), "EV7 a missing run dir never raises")
        # heartbeat: beats while the block runs, stops after; a failing progress function still beats
        e4 = os.path.join(td, "run4")
        os.makedirs(e4)

        def boom():
            raise RuntimeError("no progress")
        with heartbeat(e4, "CRITIQUE#1:r1:x", "CRITIQUE", 1, every_s=0.05, progress=boom):
            time.sleep(0.3)
        n = len([e for e in read(e4) if e["kind"] == "heartbeat"])
        time.sleep(0.2)
        n2 = len([e for e in read(e4) if e["kind"] == "heartbeat"])
        ok(n >= 2 and n2 == n and read(e4)[0]["step"] == "CRITIQUE#1:r1:x" and read(e4)[0]["eta_s"] is None,
           f"EV8 heartbeats while the block runs, none after; a failing progress still beats ({n}, {n2})")
        with open(os.path.join(e4, PROGRESS), "w") as fh:
            json.dump({"steps": {"a": {"state": "ok"}, "analyze": {"state": "running", "elapsed_s": 100.0,
                                                                   "projected_total_s": 400.0,
                                                                   "projection_basis": "40 of 160 rows written"}}}, fh)
        p = pipeline_progress(e4)
        ok(p == {"progress": "step analyze", "eta_s": 300.0, "basis": "40 of 160 rows written"},
           f"EV9 the ETA comes from the pipeline's projection ({p})")
        ok(pipeline_progress(d) == {}, "EV9b no progress record: no ETA")

        class F:
            def __init__(self, s, by="gate"):
                self.status, self.kind, self.severity, self.events = s, "k", "HIGH", [{"by": by}]
        ch = finding_changes({"a": F("open"), "b": F("open")}, {"a": F("fixed"), "b": F("open"), "c": F("open", "critique")})
        ok([(c["id"], c["from"], c["to"]) for c in ch] == [("a", "open", "fixed"), ("c", None, "open")]
           and ch[1]["by"] == "critique", f"EV10 finding status changes, new findings from None ({ch})")
        ok(question_of("awaiting_scope_signoff") == ("scope_signoff", "scope_signoff_request.json")
           and question_of("other")[0] == "other", "EV11 each pause names its owner question")
        # concurrent writers in one process: every line parses, seq unique
        e5 = os.path.join(td, "run5")
        os.makedirs(e5)
        ths = [threading.Thread(target=lambda: [emit(e5, "heartbeat", note="z" * 200) for _ in range(50)])
               for _ in range(4)]
        [t.start() for t in ths]
        [t.join() for t in ths]
        ev5 = read(e5)
        ok(len(ev5) == 200 and len({e["seq"] for e in ev5}) == 200, f"EV12 concurrent appends interleave by line ({len(ev5)})")
    if fails:
        print(f"FAIL: {len(fails)} self-test case(s) failed")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv[1:]:
        sys.exit(_self_test())
    print(__doc__)
    sys.exit(2)
