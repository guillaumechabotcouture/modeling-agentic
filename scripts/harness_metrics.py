#!/usr/bin/env python3
"""Harness autonomy metrics: per run chain, did it complete, how many owner interventions, how long it waited.

The definitions are the harness benchmark protocol's (section 2): a chain is a first run directory plus every
relaunch made from it; "completes" is decided by the chain's last directory; the nine intervention types I1-I9;
owner-wait split into owner response and operator latency, censored at the cut-off. Read-only: it reads
metadata.json, run_state.json, the sign-off records, findings.jsonl and whether report.md / report_withheld.md
exist, through lib.safe_io (no link followed). It refuses any path under `_truth` or `evals_private`.

Every field says how it was derived: `record` (a harness record states it), `inferred` (reconstructed from
other records, for example a pause from a transition followed by a resume), or `unavailable`. A run directory
that cannot be read is a row with status `unreadable`, never a skipped row.

Times: history and journal times are UTC with an offset; `metadata.started` and `invocations[].at` are naive
local time, converted with `metadata.utc_offset_s`. With no offset a naive time is not computable, and so is
any wait that needs it.

    python3 scripts/harness_metrics.py <run_dir>... [--chain a,b ...] [--cutoff ISO] [--ledger L] [--json out.json]
    python3 scripts/harness_metrics.py --self-test

Importable (PR 4B run_harvest / feedback_store): `load_run`, `chains`, `summarise`, `rows`, `wave_summary`.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib import safe_io  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REFUSED_PARTS = ("_truth", "evals_private")
CENSOR_H = 24.0
COMPLETES = ("completed", "completed_scope_declared")
NOT_ACCEPTED = ("completed_gate_not_accepted", "completed_scope_declined", "completed_no_report")
SIGNED_BY_OWNER = "spec signed by owner"
SCOPE_ANSWERED = "the owner answered the scope declarations"
PAUSE_STATES = {"SIGN": "awaiting_signoff", "OWNER_DECISION": "awaiting_scope_signoff",
                "SPEC": "awaiting_spec_amendment"}
REC, INF, NA = "record", "inferred", "unavailable"


def F(value, source, note=None) -> dict:
    """One derived field."""
    return {"value": value, "from": source, **({"note": note} if note else {})}


# ------------------------------------------------------------------ API (α: the self-tests first; β implements)
def _todo(*a, **k):
    raise NotImplementedError("harness_metrics β")


load_run = completion = pauses = owner_wait = interventions = chains = summarise = rows = wave_summary = _todo
table = utc = question_key = _todo


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("run_dirs", nargs="*")
    ap.add_argument("--chain", action="append", default=[], help="comma-separated run dirs of one chain, in order")
    ap.add_argument("--cutoff", help="ISO time with offset (default: first launch + 24 h)")
    ap.add_argument("--ledger", help="JSON operator ledger: {run dir name: {i9: n}}")
    ap.add_argument("--json", help="write the rows and the wave summary here")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)
    if a.self_test:
        return _self_test()
    explicit = [[x for x in c.split(",") if x] for c in a.chain]
    dirs = list(a.run_dirs) + [d for c in explicit for d in c]
    if not dirs:
        ap.error("no run directory")
    ledger = json.load(open(a.ledger)) if a.ledger else None
    rs = rows(dirs, explicit, a.cutoff, ledger)
    print(table(rs))
    if a.json:
        with open(a.json, "w") as f:
            json.dump({"rows": rs, "summary": wave_summary(rs)}, f, indent=1, default=str)
    return 0


# ------------------------------------------------------------------ self-test (synthetic run dirs only)
def _self_test() -> int:
    import tempfile
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)
    T0 = datetime(2020, 1, 1, 0, 0, tzinfo=timezone.utc)

    def at(h):
        return (T0 + timedelta(hours=h)).isoformat(timespec="seconds")

    def local(h, off=3600):          # naive local time, as main.py writes it
        return (T0 + timedelta(hours=h, seconds=off or 0)).replace(tzinfo=None).isoformat(timespec="seconds")

    def mk(root, name, st=None, meta=None, files=None, off=3600, inv=(0,)):
        d = os.path.join(root, name)
        os.makedirs(os.path.join(d, "data"), exist_ok=True)
        m = {"started": local(0, off), "commit": "abc1234", "status": (st or {}).get("status"),
             "invocations": [{"at": local(h, off), "commit": "abc1234"} for h in inv]}
        if off is not None:
            m["utc_offset_s"] = off
        m.update(meta or {})
        files = dict(files or {})
        files.setdefault("metadata.json", json.dumps(m))
        if st is not None:
            files.setdefault("run_state.json", json.dumps({"state": "FINAL", "round": 1, "history": [], **st}))
        for rel, txt in files.items():
            open(os.path.join(d, rel), "w").write(txt)
        return d

    with tempfile.TemporaryDirectory() as root:
        fin = {"history": [{"at": at(1), "from": "GATE", "to": "FINAL", "why": "accepted"}]}
        d = mk(root, "r_ok", {**fin, "status": "completed"}, {"finished": at(1)}, {"report.md": "x"})
        r = summarise([d])
        ok(r["completes"] == F(True, REC) and r["interventions"]["headline"]["value"] == 0
           and r["owner_wait"]["total_h"]["value"] == 0 and r["autonomous_wall_h"]["value"] == 1.0,
           f"HM1 a completed run with report.md completes, 0 interventions, 1 h autonomous ({r['autonomous_wall_h']})")
        d = mk(root, "r_wh", {**fin, "status": "completed"}, None, {"report_withheld.md": "x"})
        ok(summarise([d])["completes"]["value"] is False, "HM2 a withheld report does not complete")
        d = mk(root, "r_sd", {**fin, "status": "completed_scope_declared"}, None, {"report.md": "x"})
        r = summarise([d])
        ok(r["completes"]["value"] and wave_summary([r])["scope_declared"] == 1, "HM3 scope declared completes, apart")
        d = mk(root, "r_bad", None, None, {"run_state.json": "{not json"})
        r = rows([d, os.path.join(root, "missing")])
        ok(len(r) == 2 and all(x["status"]["value"] == "unreadable" and not x["completes"]["value"] for x in r),
           "HM4 an unparseable run_state.json and a missing dir are unreadable rows, not skipped")
        os.makedirs(os.path.join(root, "_truth"))
        d = mk(os.path.join(root, "_truth"), "r_t", {**fin, "status": "completed"}, None, {"report.md": "x"})
        ok("refused" in summarise([d])["status"].get("note", ""), "HM5 a path under _truth is refused, unread")
        # HM6 explicit awaiting_budget pause, raised and resumed 2 h later
        h = [{"at": at(1), "paused": "awaiting_budget", "state": "CRITIQUE"},
             {"at": at(3), "budget_raised": {"from": 5, "to": 9}}, {"at": at(4), "from": "GATE", "to": "FINAL"}]
        d = mk(root, "r_bud", {"history": h, "status": "completed"}, None, {"report.md": "x"}, inv=(0, 3))
        r = summarise([d])
        iv, ow = r["interventions"], r["owner_wait"]
        ok(iv["I1_pauses"] == F(1, REC) and iv["I5_resumes"]["value"] == 1 and iv["I7_budget_raises"]["value"] == 1
           and iv["headline"]["value"] == 2 and ow["total_h"] == F(2.0, REC) and ow["owner_response_h"]["value"] == 2.0,
           f"HM6 an explicit budget pause: I1 record, I5, I7, wait 2 h all owner response ({iv['headline']}, {ow['total_h']})")
        # HM7 inferred owner SIGN pause; owner signs at +1 h; resume at +3 h (naive local, offset 1 h)
        h = [{"at": at(0.5), "from": "SPEC", "to": "SIGN"}, {"at": at(3.2), "from": "SIGN", "to": "EVIDENCE",
                                                               "why": SIGNED_BY_OWNER}]
        so = json.dumps({"signed_by": "owner", "signed_at": at(1.5)})
        d = mk(root, "r_sign", {"history": h, "status": "completed"}, None,
               {"report.md": "x", "decision_spec.signoff.json": so}, inv=(0, 3))
        r = summarise([d])
        iv, ow = r["interventions"], r["owner_wait"]
        ok(iv["I1_pauses"] == F(1, INF) and iv["I2_spec_signatures"]["value"] == 1 and iv["headline"]["from"] == INF
           and ow["total_h"]["value"] == 2.5 and ow["owner_response_h"]["value"] == 1.0
           and ow["operator_latency_h"]["value"] == 1.5,
           f"HM7 a SIGN pause is inferred; naive local times are converted (wait {ow['total_h']}, "
           f"response {ow['owner_response_h']}, latency {ow['operator_latency_h']})")
        # HM8 the time-zone trap: no utc_offset_s, naive invocation times
        d = mk(root, "r_tz", {"history": h, "status": "completed"}, None, {"report.md": "x"}, off=None, inv=(0, 3))
        r = summarise([d], cutoff=at(30))
        ok(r["owner_wait"]["total_h"]["from"] == NA and r["autonomous_wall_h"]["from"] == NA,
           f"HM8 naive times with no offset: wait not computable ({r['owner_wait']['total_h']})")
        # HM9 a pending lead SIGN pause: starts at the sign_spec journal time; censored at launch + 24 h
        st = {"state": "SIGN", "paused": "awaiting_signoff", "status": None,
              "history": [{"at": at(1), "from": "SPEC", "to": "SIGN"}],
              "journal": {"SIGN#1:sign_spec": {"status": "completed", "completed": at(2)}}}
        d = mk(root, "r_pend", st)
        r = summarise([d])
        iv, ow = r["interventions"], r["owner_wait"]
        ok(r["status"]["value"] == "paused:awaiting_signoff" and not r["completes"]["value"]
           and iv["I1_pauses"]["value"] == 0 and iv["pending_pauses"] == 1 and ow["censored"]
           and ow["total_h"]["value"] == 22.0 and ow["pauses"][0]["pause_at"] == at(2),
           f"HM9 a pending pause is censored at 24 h, not an intervention ({ow['total_h']}, {iv['I1_pauses']})")
        r = summarise([d], cutoff=at(5))
        ok(r["owner_wait"]["total_h"]["value"] == 3.0, "HM10 an explicit cut-off censors there")
        # HM11 a relaunch chain through parent_run, and the same chain from --chain
        st = {"state": "SPEC", "paused": "awaiting_spec_amendment", "status": None,
              "history": [{"at": at(1), "from": "GATE", "to": "SPEC"}]}
        p = mk(root, "r_par", st)
        c = mk(root, "r_child", {**fin, "status": "completed"},
               {"parent_run": {"run_dir": "r_par"}, "started": local(4)}, {"report.md": "x"})
        r = rows([p, c])
        ok(len(r) == 1 and r[0]["chain"] == ["r_par", "r_child"] and r[0]["interventions"]["I6_relaunches"] == F(1, REC)
           and r[0]["interventions"]["I1_pauses"]["value"] == 1 and r[0]["owner_wait"]["total_h"]["value"] == 3.0
           and r[0]["completes"]["value"], f"HM11 parent_run links a relaunch: I6 record, the pause ends at the "
           f"child's start ({r[0]['interventions']['I6_relaunches']}, {r[0]['owner_wait']['total_h']})")
        c2 = mk(root, "r_child2", {**fin, "status": "completed"}, {"started": local(4)}, {"report.md": "x"})
        r = rows([], explicit=[[p, c2]])
        ok(r[0]["interventions"]["I6_relaunches"]["from"] == INF, "HM12 an operator --chain makes I6 inferred")
        # HM13 an OWNER_DECISION pause answered through the scope records
        h = [{"at": at(1), "from": "GATE", "to": "OWNER_DECISION"},
             {"at": at(6), "from": "OWNER_DECISION", "to": "GATE", "why": SCOPE_ANSWERED}]
        sc = json.dumps({"answers": {"f1": {"by": "owner", "at": at(2)}, "f2": {"by": "owner", "at": at(4)}}})
        d = mk(root, "r_scope", {"history": h, "status": "completed"}, None,
               {"report.md": "x", "scope_signoff.json": sc}, inv=(0, 5))
        r = summarise([d])
        iv, ow = r["interventions"], r["owner_wait"]
        ok(iv["I4_scope_answers"]["value"] == 1 and "2 item" in iv["I4_scope_answers"]["note"]
           and ow["owner_response_h"]["value"] == 3.0 and ow["operator_latency_h"]["value"] == 1.0,
           f"HM13 a scope answer: I4 1 (2 items), response to the last answer ({ow['owner_response_h']})")
        # HM14 an explicit pause entry suppresses the inference (no double count)
        h = [{"at": at(1), "from": "SPEC", "to": "SIGN"}, {"at": at(1), "paused": "awaiting_signoff", "state": "SIGN"},
             {"at": at(3), "from": "SIGN", "to": "EVIDENCE", "why": SIGNED_BY_OWNER}]
        d = mk(root, "r_both", {"history": h, "status": "completed"}, None, {"report.md": "x"}, inv=(0, 2))
        r = summarise([d])
        ok(r["interventions"]["I1_pauses"] == F(1, REC) and len(r["owner_wait"]["pauses"]) == 1,
           f"HM14 an explicit pause entry is the record; no inferred copy ({r['interventions']['I1_pauses']})")
        # HM15 geometry approvals, a ledger, the JSON and the table
        g = json.dumps({"approvals": {"G1": {"approved_by": "owner"}, "G2": {"approved_by": "lead"}}})
        d = mk(root, "r_geo", {**fin, "status": "run_failed"}, None, {"data/geometry_signoff.json": g})
        r = rows([d], ledger={"r_geo": {"i9": 2}})
        iv = r[0]["interventions"]
        out = os.path.join(root, "o.json")
        main([d, "--json", out])
        js = json.load(open(out))
        ok(iv["I3_geometry_approvals"]["value"] == 1 and iv["I9_operator_actions"] == F(2, REC, "operator ledger")
           and r[0]["class"] == "not_completed" and js["summary"]["chains"] == 1
           and all("from" in v for v in js["rows"][0]["interventions"].values() if isinstance(v, dict))
           and "r_geo" in table(r),
           "HM15 geometry approvals by the owner, the operator ledger, JSON fields carry their derivation")
        d = mk(root, "r_raw", {**fin, "status": "completed"}, {"models": {"deep": "m"}}, {"report.md": "x"})
        r = summarise([d])
        ok(r["models"] == F({"deep": "m"}, REC) and r["sdk_version"]["from"] == NA,
           "HM16 model ids are read from metadata when recorded, else unavailable")
        ok(utc(local(0)[:-3] + "xx") is None and utc("2020-01-01T01:00:00", 3600) == T0
           and utc("2020-01-01T00:00:00+00:00") == T0, "HM17 utc(): naive local with offset, aware, garbage")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
