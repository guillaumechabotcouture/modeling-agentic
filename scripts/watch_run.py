#!/usr/bin/env python3
"""
Phase 29: watch a cloud run from a laptop through its storage checkpoints
(agents/observability.py uploads status.json every minute). Prints one line
per event, so it can feed a monitor; never attaches to the VM.

    python3 scripts/watch_run.py outputs/runs/e7r6 [--account A] [--interval 60]
    python3 scripts/watch_run.py --self-test

Events: run found; stage / session / active-agent change; new gate decision;
spend crossing 50% / 80% / 100% of the cap; memory above 85%; heartbeat
older than 10 minutes (possible crash); the final run archive stored (done).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
STALE_S = 600
MEM_ALERT = 85.0


def events(prev: dict | None, cur: dict, now: datetime, alerted: set) -> list[str]:
    """Pure: the event lines implied by going from prev to cur status."""
    out = []
    p = prev or {}
    if prev is None:
        out.append(f"found: session {cur.get('session')} stage {cur.get('stage')} "
                   f"elapsed {cur.get('elapsed_min')} min")
    if cur.get("session") != p.get("session") and prev is not None:
        out.append(f"session {cur.get('session')} started")
    if cur.get("stage") != p.get("stage") and prev is not None:
        out.append(f"stage {p.get('stage')} -> {cur.get('stage')} ({cur.get('last_agent')})")
    g, pg = cur.get("gate") or {}, p.get("gate") or {}
    if g and (g.get("round"), g.get("action"), g.get("high")) != (pg.get("round"), pg.get("action"), pg.get("high")):
        out.append(f"gate round {g.get('round')}: {g.get('action')} ({g.get('high')} HIGH, {g.get('medium')} MEDIUM)")
    led = cur.get("ledger") or {}
    cap = led.get("cap_usd")
    spent = max(float(led.get("spent_usd") or 0), float(cur.get("est_spend_this_process_usd") or 0))
    if cap:
        for frac in (0.5, 0.8, 1.0):
            key = f"spend{frac}"
            if spent >= frac * cap and key not in alerted:
                alerted.add(key)
                out.append(f"SPEND {int(frac * 100)}% of cap: ~${spent:.0f} of ${cap:.0f}")
    mem = (cur.get("resources") or {}).get("mem_used_pct")
    if mem is not None and mem >= MEM_ALERT and "mem" not in alerted:
        alerted.add("mem")
        out.append(f"MEMORY {mem:.0f}% used ({(cur.get('resources') or {}).get('mem_avail_gb')} GB free)")
    if mem is not None and mem < MEM_ALERT - 10:
        alerted.discard("mem")
    try:
        hb = datetime.fromisoformat(cur["heartbeat"])
        age = (now - hb).total_seconds()
        if age > STALE_S and "stale" not in alerted:
            alerted.add("stale")
            out.append(f"STALE heartbeat ({age / 60:.0f} min old): the run may have crashed")
        elif age <= STALE_S:
            alerted.discard("stale")
    except (KeyError, ValueError, TypeError):
        pass
    return out


def _latest(prefix: str, account: str | None):
    import results_store
    blobs = results_store.list_blobs(f"{prefix}/live/", account)
    st = [b for b in blobs if b["name"].endswith("/status.json")]
    if not st:
        return None, None
    b = max(st, key=lambda x: x["last_modified"])
    run = b["name"].split("/live/")[1].split("/")[0]
    tmp = os.path.join("/tmp" if os.name != "nt" else os.environ.get("TEMP", "."), f".watch_{os.getpid()}.json")
    results_store.download(b["name"], tmp, account)
    return run, json.load(open(tmp))


def watch(prefix: str, account: str | None, interval: float) -> int:
    import results_store
    prev, alerted, run = None, set(), None
    while True:
        try:
            run, cur = _latest(prefix, account)
        except Exception as e:  # noqa: BLE001 — storage hiccup: say so, keep watching
            print(f"storage read failed: {type(e).__name__}: {str(e)[:120]}", flush=True)
            time.sleep(interval)
            continue
        if cur is not None:
            for line in events(prev, cur, datetime.now(timezone.utc), alerted):
                print(f"[{run}] {line}", flush=True)
            prev = cur
            done = [b for b in results_store.list_blobs(f"{prefix}/{run}.tar.gz", account)]
            if done:
                print(f"[{run}] DONE: final archive stored ({done[0]['name']})", flush=True)
                return 0
        time.sleep(interval)


def _self_test() -> int:
    fails = []
    now = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
    base = {"heartbeat": "2026-10-25T11:59:30+00:00", "session": 1, "stage": "PLAN", "last_agent": "planner",
            "elapsed_min": 5, "ledger": {"cap_usd": 450, "spent_usd": 0}, "est_spend_this_process_usd": 10,
            "resources": {"mem_used_pct": 40}}
    alerted: set = set()
    e = events(None, base, now, alerted)
    if not e or not e[0].startswith("found"):
        fails.append(f"W1 first status is announced: {e}")
    nxt = dict(base, stage="MODEL", last_agent="modeler", est_spend_this_process_usd=230,
               gate={"round": 1, "action": "PATCH_OR_RETHINK", "high": 9, "medium": 3},
               resources={"mem_used_pct": 91, "mem_avail_gb": 2.1})
    e = events(base, nxt, now, alerted)
    want = ["stage PLAN -> MODEL", "gate round 1: PATCH_OR_RETHINK (9 HIGH", "SPEND 50%", "MEMORY 91%"]
    if not all(any(w in x for x in e) for w in want):
        fails.append(f"W2 stage, gate, spend and memory events: {e}")
    if events(nxt, nxt, now, alerted):
        fails.append("W3 no repeated events for an unchanged status")
    stale = dict(nxt, heartbeat="2026-10-25T11:40:00+00:00")
    if not any("STALE" in x for x in events(nxt, stale, now, alerted)):
        fails.append("W4 a 20-minute-old heartbeat is reported as a possible crash")
    if fails:
        print("SELF-TEST FAILURES:", *fails, sep="\n  - ")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Watch a cloud run through its storage checkpoints")
    ap.add_argument("prefix", nargs="?", help="e.g. outputs/runs/e7r6")
    ap.add_argument("--account", default=os.environ.get("MA_STORAGE_ACCOUNT"))
    ap.add_argument("--interval", type=float, default=60)
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return _self_test()
    if not a.prefix:
        ap.error("prefix required")
    return watch(a.prefix.rstrip("/"), a.account, a.interval)


if __name__ == "__main__":
    sys.exit(main())
