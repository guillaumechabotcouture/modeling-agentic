#!/usr/bin/env python3
"""Round, resume, and stage-loop tests extracted from agents/stage_machine.py."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(TESTS_DIR)
AGENTS_DIR = os.path.join(REPO, "agents")
for _p in (REPO, os.path.join(REPO, "scripts"), AGENTS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import stage_machine as sm
from stage_machine import (
    AB, ACQUIRE_CAP_USD, AWAITING_BUDGET, AWAITING_INPUTS, CALIBRATION_REFUSED, CRITICS, DECLARED_FILE, FL, MAX_ACQUIRE_ROUNDS, MAX_CALIBRATION_REFUSALS, MAX_SPEC_TRIES, Machine, PROVENANCE, READBACK_CAP_USD, RunState, SIGNOFF_FILE, STATE_FILE, _bearing_fp, _bearing_item, _bearing_record, _carry_feedback, _lead_may_accept, _now, _put, _render_stale, _signoff, _write_json, analysis_failure_cause, apply_medium_rule, calibration_refusal, deliverable_problems, dependence_lines, harness_owned, init_run, load_state, main as sm_main, open_findings, owned_path, owner_accepted_now, owner_held, render_report, report_claim_findings, resumable_failure, rethink_request, run_v2, save_state, scope_request_view, stage_inputs, v2_gate, write_declared,
)


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    ap.parse_args(argv)
    return _self_test()


_TOY_BATCH_FN = '''import os
import time


def f(p):
    with open(os.environ["MA_TOY_CALLS"], "a") as fh:
        fh.write(f"{p['i']}\\n")
    time.sleep(float(os.environ.get("MA_TOY_SLEEP_S") or 0))
    if p["i"] >= int(os.environ.get("MA_TOY_SLOW_FROM") or 99):
        parent = os.getppid()
        for _ in range(600):              # until the step is killed (this process is then orphaned)
            time.sleep(0.1)
            if os.getppid() != parent:
                break
    return {"v": p["i"]}
'''
_TOY_BATCH_DRIVER = '''import json
import os
import sys
sys.path.insert(0, {scripts!r})
import compute

if os.environ.get("MA_TOY_INFRA_FROM"):
    # T61: rows from this input on come back as compute.py reports a chunk lost with its worker.
    _real_map = compute.map_fn

    def _lossy(run_dir, fn_spec, inputs, **kw):
        rows, s = _real_map(run_dir, fn_spec, inputs, **kw)
        k0 = int(os.environ["MA_TOY_INFRA_FROM"])
        return [r if inputs[r["i"]]["i"] < k0 else {{"i": r["i"], "ok": False, "kind": "infra",
                                                    "error": "chunk failed: its worker left the cluster"}}
                for r in rows], s
    compute.map_fn = _lossy


def sm_main(rd):
    os.makedirs(os.path.join(rd, "analysis", "batch"), exist_ok=True)
    rows, _ = compute.run_map(rd, os.path.join(rd, "models", "toy_fn.py") + "::f", [{{"i": i}} for i in range(6)],
                              os.path.join(rd, "analysis", "batch", "rows.jsonl"), backend="local", max_workers=1,
                              purpose="toy batch")
    json.dump([bool(r.get("ok")) for r in rows], open(os.path.join(rd, "analysis", "toy_rows.json"), "w"))
    return 0 if all(r.get("ok") for r in rows) else 1


if __name__ == "__main__":         # the local pool's spawned processes import this file again
    sys.exit(sm_main(sys.argv[1]))
'''


def _self_test_auto1a(ok, td: str, toolkit, new_run) -> None:
    """v2 autonomy brief, PR 1A (docs/design/v2-autonomy-brief.md §4): inline presentation review
    budget stop, the gate before a rewrite, harness ids, the RETHINK back-edges and claim references."""
    from types import SimpleNamespace as NS
    import yaml
    from v2_tasks import StubTasks

    def mk(d, T, st, backend=toolkit):
        return Machine(d, T, st, backend=backend, log=lambda *a: None, cross_engine_check=False)

    def case(name, fn):
        try:
            fn()
        except Exception as e:  # noqa: BLE001 -- before the fix exists this is the failure
            ok(False, f"{name}: {type(e).__name__}: {e}")

    def budget():
        d, T, st = new_run("a1_pres", {}, budget=20.0)
        st.state, st.write_mode, st.spent_usd = "WRITE", "accepted", 16.5        # past cap less reserve, $3.50 left
        save_state(d, st)
        mk(d, T, st).step()
        s = load_state(d)
        ok(s.state == "GATE" and s.forced_status is None
           and T.calls == ["write:accepted", "critique:critique-presentation:1"],
           f"T_PRES_BUDGET the write & presentation review run on the write-up reserve ({s.state}, {s.forced_status}, {T.calls})")
        d, T, st = new_run("a1_pres_out", {}, budget=20.0)
        st.state, st.write_mode, st.spent_usd = "WRITE", "accepted", 20.0
        st.journal["WRITE#0:accepted"] = {"task": "write", "status": "completed", "ok": True}
        open(os.path.join(d, "manuscript.md"), "w").write("# Report\n")
        save_state(d, st)
        mk(d, T, st).step()
        s = load_state(d)
        ok(s.state == "FINAL" and s.status == "budget_exhausted" and T.calls == [],
           f"T_PRES_BUDGET2 with the whole cap spent the run still ends budget_exhausted ({s.state}, {s.status})")

        class RBT(StubTasks):
            def set_allowance(self, usd, readback_usd=None):
                self.rb = readback_usd
                super().set_allowance(usd)
        d, T, st = new_run("a1_rb", {}, budget=20.0)
        st.spent_usd = 16.5
        save_state(d, st)
        T = RBT(T.s)
        m = mk(d, T, st)
        m._task("k1", "sign_spec", d, writeup=True)
        ok(T.rb == READBACK_CAP_USD and m._task("k2", "sign_spec", d) is None,
           f"T_WRITE_RB a write-up task keeps its read-back allowance; other tasks still stop at the reserve ({T.rb})")

    def post_write():
        d = os.path.join(td, "a1_pw")
        os.makedirs(d)
        S = StubTasks({"critics": {1: {"critique-presentation": [
            {"id": "P-001", "severity": "MEDIUM", "target_stage": "WRITE", "claim": "a caption is unclear",
             "done_when": [{"file": "manuscript.md", "contains": "Figure 1 shows"}]}]}}})
        S.critique(d, "critique-presentation", 1)
        for r in CRITICS:
            S.critique(d, r, 1)
        open(os.path.join(d, "manuscript.md"), "w").write("# Report\n")
        g = v2_gate(d, 1, 3, False, [], history=False)
        for r in CRITICS:
            S.critique(d, r, 2)
        gp = v2_gate(d, 2, 3, True, [], history=False)
        kinds = {v.get("kind") for v in g.get("rigor_violations") or []}
        ok(not g.get("schema_errors") and g.get("action") and "P-001" in {o["id"] for o in open_findings(d, g)}
           and not kinds & {"done_when_dropped", "critique_presentation_missing"} and gp.get("schema_errors"),
           f"T_PW_GATE round-1 gate reads round-1 presentation review; round-2 gate rejects stale round-1 "
           f"({g.get('schema_errors')}, {g.get('action')}, {sorted(kinds)}, post {bool(gp.get('schema_errors'))})")
        d = os.path.join(td, "a1_ofp")
        os.makedirs(d)
        yaml.safe_dump({"reviewer": "critique-domain", "round": 1, "blockers": [
            {"id": "D-061", "severity": "MEDIUM", "category": "METHODS", "target_stage": "WRITE", "claim": "c"},
            {"id": "D-062", "severity": "MEDIUM", "category": "METHODS", "target_stage": "ANALYZE", "claim": "c"}]},
            open(os.path.join(d, "critique_domain.yaml"), "w"))
        got = [o["id"] for o in open_findings(d, {"action": "ACCEPT", "rigor_violations": []})]
        ok(got == ["D-062"], f"T_OF_PRES a WRITE-targeted finding waits for the draft ({got})")

    def ids():
        class B:
            def __getattr__(self, n):
                return getattr(toolkit, n)

            def gate_findings(self, rd):
                return []

            def spec_violations(self, rd):
                return [{"kind": "spec_x", "key": "k1", "severity": "HIGH", "claim": "c"}]

            def registry_violations(self, rd):
                return [{"kind": "reg_y", "key": "k2", "severity": "HIGH", "claim": "c"}]
        d, T, st = new_run("a1_ids", {})
        got = mk(d, T, st, backend=B()).harness_ids(d)
        ok({"spec_x:k1", "reg_y:k2"} <= got, f"F_HARNESS_IDS spec and registry findings are harness ids ({sorted(got)})")

    def rethink():
        # T_RETHINK_SPEC_TRIES (5.1, 5.2): unsigned specs only; a signed one pauses for the owner.
        class ST(StubTasks):
            def draft_spec(self, q, rd, fb):
                self.fbs = getattr(self, "fbs", []) + [fb]
                return super().draft_spec(q, rd, fb)
        sb = {"id": "D-051", "severity": "HIGH", "target_stage": "PLAN", "category": "STRUCTURAL",
              "claim": "the decision unit is too coarse", "fix_requires": "Redraft the units."}
        d, T, st = new_run("a1_rspec", {"critics": {1: {"critique-domain": [sb]}}, "structural": {1: "critique-domain"}},
                           max_rounds=2)
        st.tries["spec"] = MAX_SPEC_TRIES - 1
        save_state(d, st)
        T = ST(T.s)
        s = mk(d, T, st).run()
        fbs = T.fbs
        ok(len(fbs) >= 2 and fbs[0] is None and any("D-051" in x and "Redraft the units." in x for x in fbs[1] or []),
           f"T_RETHINK_SPEC_TRIES a rethink of an unsigned spec redrafts it with the findings ({s}, {fbs})")
        ok(any("cannot represent the question" in x for x in (fbs + [None, None])[1] or []),
           "CX1-8 a structural rethink's reasons carry the critic's description of the mismatch")

        # T_RETHINK_ENGINE_CONTEXT (5.3): the engine pick after a rethink is told the engine left and why.
        class ET(StubTasks):
            def pick_engine(self, spec, eligible, rethink=None):
                self.rth = getattr(self, "rth", []) + [rethink]
                return super().pick_engine(spec, eligible)
        blk = {"id": "D-052", "severity": "HIGH", "target_stage": "MODEL", "claim": "the engine has no age structure"}
        plan = lambda pk: {"tasks": [{"id": "T1", "findings": [f["id"] for f in pk["findings"]], "action": "rethink",   # noqa: E731
                                      "owner_state": "ENGINE_CHOICE", "brief": "pick an engine with age structure",
                                      "files": ["engine_choice.json"]}] if pk["findings"] else []}
        d, T, st = new_run("a1_reng", {"critics": {1: {"critique-domain": [blk]}}, "plan": plan}, max_rounds=2)
        T = ET(T.s)
        s = mk(d, T, st).run()
        r = T.rth + [None, None]
        ok(r[0] is None and isinstance(r[1], dict) and str(r[1].get("engine")).endswith("ToyEngine")
           and any("age structure" in w for w in r[1].get("why") or []),
           f"T_RETHINK_ENGINE_CONTEXT the second engine pick gets the engine left and the reasons ({s}, {T.rth})")

    def claims():
        # T_CLAIM_WS (6.1): [CLAIM: id ] is the reference the renderer accepts; it is not an unresolved one.
        # T_CLAIM_ISOLATE (6.2, 6.3): one invalid claim is its own finding, and the valid claims still render.
        led = [{"id": "a.good", "claim_kind": "scalar", "value": 5}]
        bad = {"id": "b.ci", "claim_kind": "scalar", "value": 5, "ci_low": 6, "ci_high": 7}
        out = {}
        for name, cl in (("ws", led), ("iso", led + [bad])):
            d = os.path.join(td, "a1_claims_" + name)
            os.makedirs(os.path.join(d, "models"))
            yaml.safe_dump({"claims": cl}, open(os.path.join(d, "models", "claims_ledger.yaml"), "w"))
            open(os.path.join(d, "manuscript.md"), "w").write("# Report\n\nIt averts [CLAIM: a.good ] cases.\n")
            open(os.path.join(d, AB.SPEC_FILE), "w").write("{}\n")
            render_report(d, NS(headline=lambda rd: {}))
            out[name] = ({(f["kind"], f.get("key")) for f in report_claim_findings(d)},
                         open(os.path.join(d, "report.md")).read())
        ok(not {k for k, _ in out["ws"][0]} & {"report_claim_unresolved", "report_claim_unrendered", "report_claim_id_raw"},
           f"T_CLAIM_WS a reference with spaces around its id is resolved ({out['ws'][0]})")
        ok(("report_claim_invalid", "b.ci") in out["iso"][0] and "[CLAIM" not in out["iso"][1]
           and not {k for k, _ in out["iso"][0]} & {"report_claims_unreadable", "report_claim_unrendered"},
           f"T_CLAIM_ISOLATE an invalid claim is one finding; the valid claims render ({out['iso'][0]})")

    # Narrow Codex review of #297, round 1 (each failed on 650b2417).
    def r1_resume():
        # CX1-3: a presentation review that spent the last of the cap, then a crash before the transition.
        d, T, st = new_run("a1_cx3", {}, budget=20.0)
        st.state, st.write_mode, st.spent_usd = "WRITE", "accepted", 20.0
        st.journal["WRITE#0:accepted"] = {"task": "write", "status": "completed", "ok": True}
        st.journal["WRITE#0:r1:presentation"] = {"task": "critique", "status": "completed", "ok": True}
        open(os.path.join(d, "manuscript.md"), "w").write("# Report\n")
        save_state(d, st)
        mk(d, T, st).step()
        s = load_state(d)
        ok(s.state == "GATE" and s.forced_status is None and T.calls == [],
           f"CX1-3 a completed presentation review is replayed on resume with the cap spent ({s.state}, {s.status})")

    def r1_readback():
        d, T, st = new_run("a1_cx4", {}, budget=20.0)
        st.spent_usd = 16.5
        st.journal["k1"] = {"task": "sign_spec", "status": "started", "allowance_usd": 3.0, "readback_usd": 0.25,
                            "writeup": True}
        save_state(d, st)
        m = mk(d, T, st)
        ok(abs(m.st.spent_usd - 19.75) < 1e-9, f"CX1-4 an interrupted write-up task is charged its read-back too "
                                                f"({m.st.spent_usd})")
        d, T, st = new_run("a1_cx4b", {}, budget=20.0)
        st.spent_usd = 16.5
        save_state(d, st)
        m = mk(d, T, st)
        m._task("k1", "sign_spec", d, writeup=True)
        ok(m.st.journal["k1"].get("readback_usd") == READBACK_CAP_USD,
           f"CX1-4b a write-up task's journal entry records its read-back allowance ({m.st.journal['k1']})")

    def r1_open():
        # CX1-5: a rendered report without its manuscript is a draft too (the gate's predicate): nothing is deferred.
        d = os.path.join(td, "a1_cx5")
        os.makedirs(d)
        yaml.safe_dump({"reviewer": "critique-domain", "round": 1, "blockers": [
            {"id": "D-061", "severity": "MEDIUM", "category": "METHODS", "target_stage": "WRITE", "claim": "c"}]},
            open(os.path.join(d, "critique_domain.yaml"), "w"))
        open(os.path.join(d, "report.md"), "w").write("# Report\n")
        got = [o["id"] for o in open_findings(d, {"action": "ACCEPT", "rigor_violations": []})]
        ok(got == ["D-061"], f"CX1-5 with report.md and no manuscript.md a WRITE finding is not deferred ({got})")

    def r1_rethink():
        # CX1-7: a fix plan file that is not a plan does not stop a back-edge; a gate's own back-edge reads none.
        d, T, st = new_run("a1_cx7", {})
        st.state = "GATE"
        save_state(d, st)
        open(os.path.join(d, "fix_plan_r1.json"), "w").write('{"plan": ["stale"]}')
        m = mk(d, T, st)
        m._rethink("ENGINE_CHOICE", "test", planned=True)
        ok(load_state(d).state == "ENGINE_CHOICE", "CX1-7 a malformed fix plan does not stop a RETHINK back-edge")

    def r1_claims():
        d = os.path.join(td, "a1_cx2")
        os.makedirs(os.path.join(d, "models"))
        yaml.safe_dump({"claims": [{"id": "a.good", "claim_kind": "scalar", "value": 5},
                                   {"id": "b.unit", "claim_kind": "label", "value": "case"},
                                   {"id": "b.ci", "claim_kind": "scalar", "value": 5, "ci_low": 6, "ci_high": 7}]},
                       open(os.path.join(d, "models", "claims_ledger.yaml"), "w"))
        open(os.path.join(d, "manuscript.md"), "w").write("# Report\n\na.good[CLAIM:b.unit]s and [CLAIM:b.ci].\n")
        got = {(f["kind"], f.get("key")) for f in report_claim_findings(d)}
        ok(("report_claim_id_raw", "a.good") in got, f"CX1-2 a bare id beside a reference is still reported ({got})")
        ok(("report_claim_invalid", "b.ci") in got and ("report_claim_unresolved", "b.ci") not in got,
           f"CX1-9 a reference to an invalid claim is reported once, as invalid ({got})")

    # Narrow Codex review of #297, round 2 (each failed on b148196a).
    def r2_machine():
        d, T, st = new_run("a1_cx21", {})                    # CX2-1: no cap
        m = mk(d, T, st)
        m._task("k1", "sign_spec", d, writeup=True)
        ok(m.st.journal["k1"].get("readback_usd") == READBACK_CAP_USD,
           f"CX2-1 without a cap a write-up task's read-back allowance is recorded too ({m.st.journal['k1']})")
        for name, setup, want in (
                ("CX2-2 an unreadable ledger does not stop a RETHINK back-edge",
                 lambda d: _put(d, FL.LEDGER, "not json\n"), None),
                ("CX2-3 a structural mismatch any reviewer reports is among the reasons",
                 lambda d: yaml.safe_dump({"structural_mismatch": {"detected": True, "description": "no age classes"},
                                           "blockers": [{"id": "M-9", "category": "STRUCTURAL", "claim": "needs ages"}]},
                                          open(os.path.join(d, "critique_methods.yaml"), "w")),
                 "M-9: needs ages")):       # round 3 (5): the blocker's own text, whatever the ledger holds
            d, T, st = new_run("a1_" + name[:5].replace("-", ""), {})
            st.state = "GATE"
            save_state(d, st)
            setup(d)
            try:
                mk(d, T, st)._rethink("ENGINE_CHOICE", "test")
            except Exception as e:  # noqa: BLE001
                ok(False, f"{name}: {type(e).__name__}: {e}")
                continue
            s = load_state(d)
            why = (s.tries.get("rethink_engine") or {}).get("why") or []
            ok(s.state == "ENGINE_CHOICE" and (want is None or any(want in w for w in why)), f"{name} ({why})")

    def r2_findings():
        # CX2-4: with report.md alone the presentation review's findings are open findings, as its file is read.
        d = os.path.join(td, "a1_cx24")
        os.makedirs(os.path.join(d, "models"))
        yaml.safe_dump({"reviewer": "critique-presentation", "round": 1, "blockers": [
            {"id": "P-002", "severity": "MEDIUM", "category": "METHODS", "target_stage": "WRITE", "claim": "c"}]},
            open(os.path.join(d, "critique_presentation.yaml"), "w"))
        open(os.path.join(d, "report.md"), "w").write("# Report\n")
        got = [o["id"] for o in open_findings(d, {"action": "ACCEPT", "rigor_violations": []})]
        ok(got == ["P-002"], f"CX2-4 with report.md alone the presentation review's findings are read ({got})")
        # CX2-9: a reference broken over two lines is not one the renderer accepts: still reported.
        yaml.safe_dump({"claims": [{"id": "a.good", "claim_kind": "scalar", "value": 5}]},
                       open(os.path.join(d, "models", "claims_ledger.yaml"), "w"))
        open(os.path.join(d, "manuscript.md"), "w").write("# Report\n\nIt averts [CLAIM:\na.good] cases.\n")
        os.remove(os.path.join(d, "report.md"))
        got = {f["kind"] for f in report_claim_findings(d)}
        ok(got & {"report_claim_unresolved", "report_claim_id_raw"},
           f"CX2-9 a reference broken over two lines is still reported ({got})")
        # CX3-6 (round 3): an open finding whose test regressed is listed with its regression, as before the key.
        yaml.safe_dump({"reviewer": "critique-domain", "round": 1, "blockers": [
            {"id": "D-071", "severity": "MEDIUM", "category": "METHODS", "target_stage": "ANALYZE", "claim": "c"}]},
            open(os.path.join(d, "critique_domain.yaml"), "w"))
        got = [o["id"] for o in open_findings(d, {"action": "PATCH_OR_RETHINK", "rigor_violations": [
            {"kind": "done_when_regressed", "key": "D-071", "severity": "HIGH", "claim": "c"}]})]
        ok("done_when_regressed:D-071" in got and "D-071" in got, f"CX3-6 a regression is listed beside its finding ({got})")

    def r3_presentation():
        # CX3-1: a failed presentation task in WRITE goes to GATE, live and replayed alike.
        class NoReview(StubTasks):
            def critique(self, run_dir, reviewer, round_n, feedback=None):
                return self._r(f"critique:{reviewer}:{round_n}", False, note="no review written")
        states = []
        for name, journal in (("live", None), ("replay", {"task": "critique", "status": "completed", "ok": False})):
            d, T, st = new_run("a1_cx31" + name, {}, budget=20.0)
            st.state, st.write_mode, st.spent_usd = "WRITE", "accepted", 20.0 if journal else 16.5
            if journal:
                st.journal["WRITE#0:accepted"] = {"task": "write", "status": "completed", "ok": True}
                st.journal["WRITE#0:r1:presentation"] = journal
                open(os.path.join(d, "manuscript.md"), "w").write("# Report\n")
            save_state(d, st)
            mk(d, NoReview(T.s), st).step()
            states.append(load_state(d).state)
        open(os.path.join(d, "manuscript.md"), "w").write("# Report\n")
        for r in CRITICS:
            StubTasks({}).critique(d, r, 1)
        kinds = [(v.get("kind"), v.get("severity")) for v in v2_gate(d, 1, 3, True, [], history=False).get("rigor_violations") or []]
        ok(states == ["GATE", "GATE"] and ("critique_presentation_missing", "MEDIUM") in kinds,
           f"CX3-1 a failed presentation task goes to the gate, live and replayed alike; the gate reports the missing "
           f"review ({states}, {kinds})")

    for name, fn in (("T_PRES_BUDGET", budget), ("T_PW_GATE", post_write), ("F_HARNESS_IDS", ids),
                     ("T_RETHINK", rethink), ("T_CLAIM", claims), ("CX1-3", r1_resume), ("CX1-4", r1_readback),
                     ("CX1-5", r1_open), ("CX1-7", r1_rethink), ("CX1-2", r1_claims), ("CX2-1", r2_machine),
                     ("CX2-4", r2_findings), ("CX3-1", r3_presentation)):
        case(name, fn)


def _self_test_writeup_rounds(ok, td: str, toolkit, new_run) -> None:
    """Harness baseline hb1 (ranked fixes 6 and 7): the rounds a run spent with nothing to fix.

    WR1-WR3 (cause G; dengue R1 and R2, measles R1, tb R2): a write-up-only fix went FIX_LOOP -> WRITE with the round
    raised, and the post-write gate refused the domain and red-team files as a stale round ("round must be N, got
    N-1"), re-running them through the schema repair. They now re-review the draft as a route of their own.
    WR4-WR7 (cause H; malaria R2, measles R2 round 2): the medium rule sent a finding the owner (or the lead,
    provisionally) had accepted to the fix loop, which had no task for it, and two more critique rounds ran."""
    from v2_tasks import StubTasks

    def mk(d, T, st):
        return Machine(d, T, st, backend=toolkit, log=lambda *a: None, cross_engine_check=False)

    def hist(d):
        return [h for h in load_state(d).history if "to" in h]

    def case(name, fn):
        try:
            fn()
        except Exception as e:  # noqa: BLE001 -- before the fix exists this is the failure
            import traceback
            traceback.print_exc()
            ok(False, f"{name}: {type(e).__name__}: {e}")

    def writeup_rereview():
        # WR1-WR3: a HIGH write-up finding: ACCEPT pre-write, the post-write gate patches it, the writer revises
        wup = {"id": "R-030", "severity": "HIGH", "target_stage": "WRITE", "claim": "the report overstates the effect",
               "done_when": [{"file": "manuscript.md", "contains": "(?i)effect is bounded"}]}
        d, T, st = new_run("wr_writeup_rev", {"critics": {1: {"critique-redteam": [wup]}}}, max_rounds=3)
        s = mk(d, T, st).run()
        h = hist(d)
        edges = [(x["from"], x["to"]) for x in h]
        whys = [str(x.get("why")) for x in h]
        st2 = load_state(d)
        ok(("FIX_LOOP", "WRITE") in edges and ("WRITE", "CRITIQUE") in edges
           and not any(w.startswith("schema errors") for w in whys),
           f"WR1 a write-up-only fix goes WRITE -> CRITIQUE, never through a schema repair "
           f"({s}, {edges[-8:]}, {[w for w in whys if 'schema' in w]})")
        i = edges.index(("WRITE", "CRITIQUE"))
        r = h[i]["round"]
        ok(f"critique:critique-domain:{r}" in T.calls
           and f"critique:critique-redteam:{r}" in T.calls and edges[i + 1] == ("CRITIQUE", "GATE")
           and h[i + 1]["round"] == r,
           f"WR2 the re-review runs the domain and red-team critics at the revised draft's round, then the gate "
           f"({whys[i]!r}, round {r}, {T.calls[-6:]})")
        ok("schema" not in (st2.tries or {}),
           f"WR3 the re-review uses no schema repair ({st2.tries})")

    def writeup_rereview_not_accepted():
        # WR1b: a revised not-accepted draft when domain/redteam are behind round goes WRITE -> CRITIQUE -> GATE
        import shutil
        src = os.path.join(td, "wr_writeup_rev")
        d = os.path.join(td, "wr_writeup_rev_na")
        shutil.copytree(src, d)
        T, st = StubTasks({}), load_state(d)
        st.state, st.round, st.post_write, st.round_pending, st.write_mode = "WRITE", 2, False, False, "not_accepted"
        st.status, st.forced_status = None, None
        save_state(d, st)
        for r in CRITICS:
            StubTasks({}).critique(d, r, 1)
        m = mk(d, T, st)
        for _ in range(3):
            m.step()
        h = hist(d)
        e = [(x["from"], x["to"]) for x in h]
        ok(e[-3:-1] == [("WRITE", "CRITIQUE"), ("CRITIQUE", "GATE")] and e[-1][0] == "GATE"
           and "critique:critique-domain:2" in T.calls and "critique:critique-redteam:2" in T.calls
           and not os.path.exists(os.path.join(d, "critique_presentation.yaml"))
           and not str(h[-1].get("why")).startswith("schema errors"),
           f"WR1b a revised not-accepted draft goes WRITE -> CRITIQUE -> GATE ({e[-3:]}, {h[-1].get('why')!r}, {T.calls})")

    def owner_held_run():
        # WR4-WR6: malaria R2 in miniature. A MEDIUM finding whose remedy is an owner amendment, with a test that fails;
        # the lead accepts it provisionally. It is declared at once: no fix loop with no task, no second critique round.
        dw = [{"file": "analysis_settings.yaml", "key": "uncertainty.draws", "op": ">=", "value": 9999}]
        amend = {"id": "D-001", "severity": "MEDIUM", "target_stage": "PLAN", "claim": "net durability is untested",
                 "fix_requires": "Owner amendment: add a durability decision test.", "done_when": dw}
        d, T, st = new_run("wr_owner_held", {"critics": {1: {"critique-domain": [amend]}}}, signoff="lead",
                           max_rounds=3)
        s = mk(d, T, st).run()
        h = hist(d)
        to = [x["to"] for x in h]
        crit = [c for c in T.calls if c.startswith("critique:critique-domain")]
        ok(s == "completed_scope_declared" and "FIX_LOOP" not in to and crit == ["critique:critique-domain:1"],
           f"WR4 an owner-accepted finding is declared, not sent to a fix loop with no task ({s}, {to[-8:]}, {crit})")
        ok(to.count("OWNER_DECISION") == 1,
           f"WR5 the same finding (same fingerprint) is asked once ({to.count('OWNER_DECISION')} request(s))")
        # WR5b (hb2 fix 3, malaria R2 D-002): the critic carries the answered finding into round 2 reworded, beside a
        # HIGH a fix closed in round 1; the answer holds through the rewording: asked once, declared in round 2
        hi = {"id": "D-009", "severity": "HIGH", "target_stage": "ANALYZE", "claim": "too few draws",
              "done_when": [{"file": "analysis_settings.yaml", "key": "uncertainty.draws", "op": ">=", "value": 32}]}

        import yaml

        class _Reword(StubTasks):
            def critique(self, run_dir, reviewer, round_n, feedback=None):
                res = super().critique(run_dir, reviewer, round_n, feedback)
                p_ = os.path.join(run_dir, "critique_domain.yaml")
                if reviewer == "critique-domain" and os.path.exists(p_):
                    doc = yaml.safe_load(open(p_))
                    for b in doc.get("blockers") or []:
                        if b.get("id") == "D-001":
                            b["claim"] = f"net durability is untested; the lead accepted it before (round {round_n})"
                    yaml.safe_dump(doc, open(p_, "w"), sort_keys=False)
                return res
        d5, T5, st5 = new_run("wr_owner_reworded", {"critics": {1: {"critique-domain": [amend, hi]}},
                                                    "fixes": {"D-009": [("analysis_settings.yaml",
                                                                         "uncertainty.draws", 32)]}},
                              signoff="lead", max_rounds=3)
        tk5 = _Reword(T5.s)
        s5 = mk(d5, tk5, st5).run()
        to5 = [x["to"] for x in hist(d5)]
        crit5 = [c for c in tk5.calls if c.startswith("critique:critique-domain")]
        rounds5 = load_state(d5).round
        ok(to5.count("OWNER_DECISION") == 1 and s5 == "completed_scope_declared" and rounds5 == 2
           and "(round 2)" in FL.load(d5)["D-001"].claim
           and FL.load(d5)["D-001"].status == "owner_accepted",
           f"WR5b a reworded re-raise of an answered finding is not asked again ({to5.count('OWNER_DECISION')} "
           f"request(s), {s5}, round {rounds5}, {to5[-10:]}, {crit5})")
        dec = yaml.safe_load(open(os.path.join(d, DECLARED_FILE))) or {}
        g = json.load(open(os.path.join(d, "final_gate.json")))
        ok([x.get("id") for x in dec.get("declared") or []] == ["D-001"] and g.get("rule_matched") == "3m"
           and "owner" in str(g.get("rationale")),
           f"WR6 the write-up declares it, and the gate says the owner answered it ({dec.get('declared')}, "
           f"{g.get('rule_matched')}, {g.get('rationale')!r})")
        # T_LEAD_NO_CRITIC_HIGH (owner policy 2026-11-24): under --signoff lead, an open critic HIGH
        # finding whose remedy is an owner amendment is NEVER provisionally accepted by the lead — it pauses for the
        # owner at OWNER_DECISION, and releases once the owner answers.
        import scope_signoff as _SS_hi
        amend_hi = {"id": "D-002", "severity": "HIGH", "target_stage": "PLAN",
                    "claim": "the recommended plan leaves part of the fixed supply unallocated",
                    "fix_requires": "Owner amendment: require the full supply to be allocated.", "done_when": dw}
        dh, Th, sth = new_run("wr_lead_critic_high", {"critics": {1: {"critique-domain": [amend_hi]}}},
                              signoff="lead", max_rounds=2)
        sh1 = mk(dh, Th, sth).run()
        meta_h = json.load(open(os.path.join(dh, "metadata.json")))
        req_h = json.load(open(os.path.join(dh, "scope_signoff_request.json")))
        ok(sh1 == "paused:awaiting_scope_signoff" and load_state(dh).state == "OWNER_DECISION"
           and [i.get("finding") for i in req_h.get("items") or []] == ["D-002"]
           and req_h["items"][0].get("severity") == "HIGH"
           and "critic HIGH" in str(meta_h.get("paused_why") or "")
           and not _SS_hi.provisional(dh),
           f"T_LEAD_NO_CRITIC_HIGH lead mode refuses to provisionally accept an open critic HIGH finding "
           f"({sh1}, why={meta_h.get('paused_why')!r}, prov={_SS_hi.provisional(dh)})")
        # Direct call to scope_signoff.provisional_answers with a critic HIGH item also fails closed
        try:
            _SS_hi.provisional_answers(dh, req_h["items"], 1)
            ok(False, "T_LEAD_NO_CRITIC_HIGH_SS scope_signoff.provisional_answers refuses a critic HIGH item")
        except ValueError as e:
            ok("critic HIGH" in str(e), f"T_LEAD_NO_CRITIC_HIGH_SS scope_signoff refuses critic HIGH ({e})")
        _SS_hi.sign(dh, "owner", all_decision="accept", note="owner accepts D-002")
        sh2 = mk(dh, StubTasks(Th.s), None).run()
        ok(sh2 == "completed_scope_declared",
           f"T_LEAD_NO_CRITIC_HIGH_RESUME owner sign releases OWNER_DECISION in a lead run ({sh2})")
        # Codex r1 P1-1 & P1-2: stale/unknown request severity never overrides a ledger HIGH, and a prior lead MEDIUM
        # acceptance does not survive escalation to HIGH
        led_hi = {"D-002": FL.Finding(id="D-002", kind="critique_blocker", severity="HIGH", source="critique-domain",
                                      status="open")}
        ok(not _lead_may_accept({"finding": "D-002", "kind": "critique_blocker", "severity": "MEDIUM"}, led_hi)
           and not _lead_may_accept({"finding": "D-002", "kind": "critique_blocker", "severity": "unknown"}, led_hi)
           and _lead_may_accept({"finding": "D-001", "kind": "critique_blocker"}, {
               "D-001": FL.Finding(id="D-001", kind="critique_blocker", severity="MEDIUM", source="critique-domain")}),
           "T_LEAD_NO_CRITIC_HIGH_GUARD stale/unknown severity refused; legacy MEDIUM request allowed via ledger")
        esc_hi = {"id": "D-001", "severity": "HIGH", "claim": "net durability is untested",
                  "fix_requires": "Owner amendment: add a durability decision test."}
        ok(owner_accepted_now(os.path.join(td, "wr_owner_held"), [esc_hi], True) == set(),
           "T_LEAD_NO_CRITIC_HIGH_ESC prior lead MEDIUM acceptance does not hold if finding escalates to HIGH")

    def pending_post_write():
        # WR9 (Codex r2 of #344, P1-1): the owner accepted D-001 pre-write; the presentation review raises P-001, whose
        # remedy is an owner amendment. The post-write gate asks the owner before it publishes a final disposition:
        # no final_gate.json, and the report's status is pending, while P-001 waits.
        import scope_signoff as _SS
        dw = [{"file": "analysis_settings.yaml", "key": "uncertainty.draws", "op": ">=", "value": 9999}]
        amend = {"id": "D-001", "severity": "MEDIUM", "target_stage": "PLAN", "claim": "durability untested",
                 "fix_requires": "Owner amendment: add a durability test.", "done_when": dw}
        pres = {"id": "P-001", "severity": "MEDIUM", "target_stage": "WRITE", "claim": "the comparator label is unclear",
                "fix_requires": "Owner amendment: rename the comparator in the spec.",
                "done_when": [{"file": "manuscript.md", "contains": "(?i)funded plan comparator"}]}
        d, T, st = new_run("wr_pending_pw", {"critics": {1: {"critique-domain": [amend],
                                                            "critique-presentation": [pres]}}}, max_rounds=1)
        s1 = mk(d, T, st).run()
        _SS.sign(d, "owner", all_decision="accept", note="WR9")
        json.dump({"action": "DECLARE_SCOPE", "stale": True}, open(os.path.join(d, "final_gate.json"), "w"))
        s2 = mk(d, StubTasks(T.s), None).run()
        rep = open(os.path.join(d, "report.md")).read() if os.path.exists(os.path.join(d, "report.md")) else ""
        req = json.load(open(os.path.join(d, "scope_signoff_request.json")))
        ok(s1 == s2 == "paused:awaiting_scope_signoff" and [i.get("finding") for i in req.get("items") or []] == ["P-001"]
           and not os.path.exists(os.path.join(d, "final_gate.json")) and "completed_scope_declared" not in rep
           and "P-001" not in open(os.path.join(d, DECLARED_FILE)).read(),
           f"WR9 a post-write gate with a finding awaiting the owner publishes no final disposition ({s1}, {s2}, "
           f"final_gate: {os.path.exists(os.path.join(d, 'final_gate.json'))}, declared status in report: "
           f"{'completed_scope_declared' in rep})")

    def answer_binds_current_text():
        # WR10 (Codex r2 of #344, P1-2): the owner's accept holds a finding only for the claim and remedy it was given
        # for, as the gate reads them now
        d = os.path.join(td, "wr_owner_held")          # WR4's run: the lead accepted D-001 as stated
        o = {"id": "D-001", "claim": "net durability is untested",
             "fix_requires": "Owner amendment: add a durability decision test."}
        ok(owner_accepted_now(d, [o], True) == {"D-001"} and owner_accepted_now(d, [dict(o, fix_requires=None)], True)
           == set() and owner_accepted_now(d, [o], False) == set(),
           "WR10 an accept holds the finding as answered; a revised remedy, or a lead answer in an owner run, does not")

    def medium_rule_unit():
        # WR7: the rule itself. A tested finding the owner holds is declared; a tested one the fix loop can work to is
        # still patched while rounds remain.
        op = [{"id": "D-001", "tested": True, "severity": "MEDIUM", "source": "critique-domain", "claim": "c"}]
        acc = {"action": "ACCEPT", "rule_matched": "4"}
        d1 = apply_medium_rule(acc, op, 2, True, owner_held={"D-001"})
        d2 = apply_medium_rule(acc, op, 2, True)
        d3 = apply_medium_rule(acc, op + [dict(op[0], id="R-002")], 2, True, owner_held={"D-001"})
        ok(d1["action"] == "DECLARE_SCOPE" and d1["rule_matched"] == "3m" and d2["action"] == "PATCH_OR_RETHINK"
           and d3["action"] == "PATCH_OR_RETHINK" and d3["open_tested"] == ["R-002"],
           f"WR7 the medium rule never sends an owner-held finding to the fix loop ({d1['action']}, {d2['action']}, "
           f"{d3.get('open_tested')})")
        led = {"A": FL.Finding(id="A", kind="critique_blocker", severity="MEDIUM", source="critique-domain",
                               status="owner_accepted", resolution_type="owner_decision"),
               "B": FL.Finding(id="B", kind="critique_blocker", severity="MEDIUM", source="critique-domain",
                               status="open", fix_requires="Owner amendment: x"),
               "C": FL.Finding(id="C", kind="critique_blocker", severity="MEDIUM", source="critique-domain",
                               status="open", fix_requires="re-run the batch"),
               "E": FL.Finding(id="E", kind="critique_blocker", severity="MEDIUM", source="critique-domain",
                               status="owner_declined", resolution_type="owner_decision")}
        ok(owner_held(led) == {"A"}, f"WR7b owner-held: answered and accepted only; one awaiting the owner's answer is "
                                     f"not declared before it (Codex r1 of #344, P1-1) ({sorted(owner_held(led))})")
        # WR7c (Codex r1 of #344, P1-1): post-write, an unanswered amendment is not declared (no DECLARE_SCOPE is
        # published while the owner has not answered); the gate keeps it as fix-loop work and asks the owner first
        d4 = apply_medium_rule(acc, op, 2, True, owner_held=owner_held({"D-001": led["B"]}))
        ok(d4["action"] == "PATCH_OR_RETHINK", f"WR7c an unanswered amendment is not declared ({d4['action']})")

    def zero_tasks_prewrite():
        # WR8-WR9: pre-write, a fix loop with no task where every open finding the gate holds is the write-up's or
        # the owner's goes to WRITE, not to another critique round; one with an open finding a task could fix does not.
        import fix_loop as _FX
        real = _FX.run_loop
        # Codex r1 of #344: a rendered report.md counts as a draft (P1-2), and a gate that holds an unresolved HIGH (a
        # declared finding it still blocks on: P1-3) or patches by another rule (P2-4) keeps its critique round
        gate4m = {"action": "PATCH_OR_RETHINK", "rule_matched": "4m", "open_tested": ["W"], "unresolved_high": [],
                  "owner_held": ["A"], "open_tested_ids": ["W"]}
        # Codex r2 of #344: the mode follows what this gate holds, not an older owner-accepted finding (P1-4: wr_zero_wu2,
        # whose gate holds only the write-up's W), and the pre-write plan is kept apart from this round's post-write one
        # (P1-3)
        for name, extra, want, gate, report in (
                ("wr_zero_wu", None, "WRITE", gate4m, False),
                ("wr_zero_wu2", None, "WRITE", dict(gate4m, owner_held=[]), False),
                # Codex r3 of #344 (P1-1): the ledger still says A is accepted, but this gate read A as revised (not
                # held, in open_tested); and a gate record with no owner_held list (an older gate) takes no shortcut
                ("wr_zero_revised", None, "CRITIQUE", dict(gate4m, open_tested=["W", "A"], owner_held=[]), False),
                ("wr_zero_oldgate", None, "CRITIQUE", {k: v for k, v in gate4m.items() if k != "owner_held"}, False),
                ("wr_zero_fixable", "C", "CRITIQUE", gate4m, False),
                ("wr_zero_report", None, "CRITIQUE", gate4m, True),
                ("wr_zero_high", None, "CRITIQUE", dict(gate4m, unresolved_high=[{"id": "D-9", "severity": "HIGH"}]),
                 False),
                ("wr_zero_rule2", None, "CRITIQUE", dict(gate4m, rule_matched="2"), False),
                ("wr_zero_nogate", None, "CRITIQUE", None, False)):
            d, T, st = new_run(name, {}, max_rounds=3)
            if gate is not None:
                json.dump(gate, open(os.path.join(d, "gate_r1.json"), "w"))
            if report:
                open(os.path.join(d, "report.md"), "w").write("# Report\n")
            led = {"W": FL.Finding(id="W", kind="critique_blocker", severity="MEDIUM", source="critique-redteam",
                                   status="open", owner_state="WRITE", target_stage="WRITE"),
                   "A": FL.Finding(id="A", kind="critique_blocker", severity="MEDIUM", source="critique-domain",
                                   status="owner_accepted", resolution_type="owner_decision", blocking=True)}
            if extra:
                led[extra] = FL.Finding(id=extra, kind="critique_blocker", severity="MEDIUM", source="critique-domain",
                                        status="open", owner_state="ANALYZE", blocking=True)
            FL.save(d, led)
            open(os.path.join(d, "fix_plan_r1.json"), "w").write('{"round": 1, "plan": {"tasks": []}}')
            st.state, st.round, st.round_pending, st.post_write = "FIX_LOOP", 1, True, False
            save_state(d, st)
            _FX.run_loop = lambda *a, **k: {"round": 1, "tasks": 0, "rows": 0, "passed": 0, "rethink": None,
                                            "write_up": ["W"], "owner_amendments": []}
            try:
                mk(d, T, st).step()
            finally:
                _FX.run_loop = real
            s2 = load_state(d)
            last = hist(d)[-1]
            mode = "accepted" if name == "wr_zero_wu2" else "declared"
            kept = (os.path.exists(os.path.join(d, "fix_plan_r1_prewrite.json"))
                    and not os.path.exists(os.path.join(d, "fix_plan_r1.json")))
            ok(s2.state == want and (want == "CRITIQUE" or (s2.round == 1 and not s2.round_pending
                                                            and s2.write_mode == mode and kept)),
               f"WR8 pre-write, a fix loop with no task goes to {want} ({name}: {s2.state}, round {s2.round}, "
               f"pending {s2.round_pending}, mode {s2.write_mode}, {last.get('why')!r})")

    def hb2_fp_and_stale():
        # HB2-7 (Codex r3 of #357): an accept given under the earlier contract does not authorize ACCEPT (P1-1);
        # a report rendering that could not state an accepted test is read as stale, unreadable too (P1-7)
        dg = os.path.join(td, "hb2_fp_stale")
        os.makedirs(dg)
        v7 = {"kind": "decision_test_changes_plan", "key": "DT1", "claim": "c"}
        old7 = hashlib.sha256(json.dumps({"finding": FL.gate_id(v7), "claim": "c", "evidence": None}, sort_keys=True,
                                         default=str).encode()).hexdigest()[:16]
        st_missing = _render_stale(dg)
        json.dump({"dependence_stale": []}, open(os.path.join(dg, "report_render.json"), "w"))
        ok(_bearing_fp(v7, None) != old7
           and _bearing_item(dg, v7)["fingerprint"] == _bearing_fp(v7, None)
           and st_missing and _render_stale(dg) == [],
           f"HB2-7 the accept binds the disposition and a stale rendering is read ({st_missing})")

    def hb2_request_and_report():
        # HB2-5 (Codex r1 of #357, P1-4): rule 2o asks only the findings whose resolution is the owner's; a
        # policy-mapped owner item waits for the round cap
        dt_ = {"reviewer": "rigor-artifacts", "id": "RIG-001", "kind": "decision_test_changes_plan",
               "owner_signoff": True, "owner_resolution": True}
        al_ = {"reviewer": "rigor-artifacts", "id": "RIG-002", "kind": "allocation_unstable", "owner_signoff": True}
        v2o = scope_request_view({"rule_matched": "2o", "unresolved_high": [dt_, al_], "accept_blockers": [
            dict(al_, id="REQ-001")]})
        v3o = scope_request_view({"rule_matched": "3o", "unresolved_high": [dt_, al_]})
        ok([b["id"] for b in v2o["unresolved_high"]] == ["RIG-001"] and v2o["accept_blockers"] == []
           and len(v3o["unresolved_high"]) == 2,
           f"HB2-5 rule 2o requests only the owner-resolution findings ({v2o['unresolved_high']})")
        # HB2-6 (Codex r1-r3 of #357): the report's statement of an accepted dependence. Each accept binds the record
        # it was given for (its fingerprint, with the disposition); a record changed since, a toolkit result whose
        # bytes are not the bound ones or that has no bound hash is never shown as accepted
        from toolkit.analysis import contract as _CTd
        import yaml as _yd
        dd = os.path.join(td, "hb2_dependence")
        os.makedirs(os.path.join(dd, "models", "decision_tests"))
        ids13 = [f"u{i}" for i in range(1, 14)]
        _yd.safe_dump({"varied": "the unit costs of the listed cost items multiplied together", "results": [
            {"value": "case", "cost_multipliers": {"transport": 2}, "decision_changes": True, "units_changed": 13,
             "units_changed_ids": ids13, "recommendation": "A: 13 units", "point_plan_feasible": False,
             "within_solver_gap": True}]}, open(os.path.join(dd, "models", "decision_tests", "DTc.yaml"), "w"))
        for k_ in ("DTx", "DTn"):
            _yd.safe_dump({"varied": "the envelope", "results": [{"value": 9, "decision_changes": True,
                                                                   "units_changed": 1}]},
                          open(os.path.join(dd, "models", "decision_tests", f"{k_}.yaml"), "w"))
        sha_c = hashlib.sha256(open(os.path.join(dd, "models", "decision_tests", "DTc.yaml"), "rb").read()).hexdigest()
        recs = {"DTc": {"path": "models/decision_tests/DTc.yaml", "result_sha256": sha_c},
                "DTx": {"path": "models/decision_tests/DTx.yaml", "result_sha256": "0" * 64},
                "DTn": {"path": "models/decision_tests/DTn.yaml", "result_sha256": None}}
        real_rec, real_doc = _CTd.decision_test_record, _CTd._verified_doc
        _CTd.decision_test_record = lambda rd, k: recs.get(k)
        _CTd._verified_doc = lambda rd: ([], {}, None, {"reference_plan": "point_plan"})

        def row(key, by="owner", fp=None):
            v_ = {"kind": "decision_test_changes_plan", "key": key, "claim": "c",
                  "source": "toolkit/analysis/contract.py"}
            f_ = FL.Finding(id=f"decision_test_changes_plan:{key}", kind="decision_test_changes_plan", key=key,
                            severity="HIGH", source="rigor-artifacts", status="owner_accepted", claim="c")
            f_.events.append({"round": 1, "status": "owner_accepted", "by": by,
                              "record": "toolkit",
                              "fingerprint": fp or _bearing_fp(v_, _bearing_record(dd, v_)),
                              "result_sha256": (recs.get(key) or {}).get("result_sha256")})
            return f_
        try:
            FL.save(dd, {"c": row("DTc"), "x": row("DTx"), "n": row("DTn")})
            st6: list = []
            txt = "".join(dependence_lines(dd, st6))
            FL.save(dd, {"c": row("DTc", by="lead")})
            txt_l = "".join(dependence_lines(dd))
        finally:
            _CTd.decision_test_record, _CTd._verified_doc = real_rec, real_doc
        ln = {k: next((x for x in txt.splitlines() if f"**{k}**" in x), "") for k in ("DTc", "DTx", "DTn")}
        ok("counted against the point-value plan" in ln["DTc"] and "transport x2" in ln["DTc"]
           and "(" + ", ".join(ids13) + ")" in ln["DTc"] and "not settled within the solver's gap" in ln["DTc"]
           and "has changed since it was accepted" in ln["DTx"]
           and "no verified values are recorded" in ln["DTn"] and "at 9" not in ln["DTn"]
           and st6 == ["decision_test_changes_plan:DTx"]
           and "The lead accepted the plan with that dependence, provisionally" in txt_l
           and "inputs the lead accepted provisionally" in txt_l and "inputs the owner accepted" not in txt_l,
           f"HB2-6 the accepted dependence names its reference plan, the price case, every unit, an unsettled solve, "
           f"who accepted it, and never shows a changed or unbound record as accepted ({txt!r}, {txt_l!r}, {st6})")

    for name, fn in (("HB2-5-6", hb2_request_and_report), ("HB2-7", hb2_fp_and_stale), ("WR1-WR3", writeup_rereview), ("WR1b", writeup_rereview_not_accepted),
                     ("WR4-WR6", owner_held_run),
                     ("WR7", medium_rule_unit), ("WR8", zero_tasks_prewrite), ("WR9", pending_post_write),
                     ("WR10", answer_binds_current_text)):
        case(name, fn)


def _self_test_resume(ok, td: str, toolkit, new_run) -> None:
    """T58-T60 (Nigeria v2 full run, 2026-11-02: ANALYZE -> FINAL run_failed when the runner's 1-h limit killed the
    engine batch). A toy engine batch through scripts/compute.py, as a pipeline step before the toolkit's analysis."""
    import contextlib
    import io
    import yaml
    from v2_tasks import StubTasks
    quiet = lambda *a: None  # noqa: E731
    drv = os.path.join(td, "toy_batch_driver.py")
    open(drv, "w").write(_TOY_BATCH_DRIVER.format(scripts=os.path.join(REPO, "scripts")))

    class Batch:
        """The toolkit backend with a toy engine batch step first; `run` replaces the batch command."""
        def __init__(self, tmo, run=None):
            self.tmo, self.run = tmo, run

        def __getattr__(self, k):
            return getattr(toolkit, k)

        def pipeline_steps(self, spec_path, run_dir):
            steps = [dict(s) for s in toolkit.pipeline_steps(spec_path, run_dir)]
            for s in steps:
                if s["id"] == "analyze":
                    s["inputs"] = s["inputs"] + ["analysis/toy_rows.json"]
            return [{"id": "batch", "run": self.run or f"{sys.executable} {drv} {run_dir}", "inputs": [AB.SPEC_FILE],
                     "outputs": ["analysis/toy_rows.json"], "timeout_s": self.tmo}] + steps

    def toy_run(name):
        d, T, _st = new_run(name, {})
        os.makedirs(os.path.join(d, "models"), exist_ok=True)
        open(os.path.join(d, "models", "toy_fn.py"), "w").write(_TOY_BATCH_FN)
        return d, T

    # T58s (owner decision, 2026-11-17: warn, don't kill): a batch step that runs past its timeout_s (1 s here) is
    # warned about and recorded while it runs, and the run goes on to finish; nothing is killed at the soft deadline.
    import pipeline as P_
    d, T = toy_run("analyze_soft")
    os.environ.update(MA_TOY_CALLS=os.path.join(td, "toy_calls_soft.txt"), MA_TOY_SLEEP_S="1.5")
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            s0 = run_v2("toy question", d, T, signoff="none", log=quiet, backend=Batch(1), cross_engine_check=False)
    finally:
        os.environ.pop("MA_TOY_SLEEP_S", None)
    try:
        prog = json.load(open(os.path.join(d, P_.PROGRESS)))["steps"]["batch"]
        row = next(r for r in json.load(open(os.path.join(d, P_.REPORT)))["steps"] if r["id"] == "batch")
    except (OSError, ValueError, KeyError, StopIteration):
        prog, row = {}, {}
    ok(s0 in ("completed", "completed_scope_declared") and prog.get("state") == "ok"
       and prog.get("soft_deadline_s") == 1 and prog.get("backstop_s") == 24 * 3600 and row.get("over_soft_deadline")
       and "past its soft deadline" in err.getvalue(),
       f"T58s a step past its timeout_s warns, records its progress and runs on; the run finishes ({s0}, {prog}, "
       f"{row})")
    # T58: past its backstop (made 8 s here: BACKSTOP_MULTIPLE 1, no 24 h floor) the batch step is killed with 3 of
    # 6 rows done: run_failed, recorded as resumable.
    d, T = toy_run("analyze_timeout")
    calls = os.path.join(td, "toy_calls.txt")
    os.environ.update(MA_TOY_CALLS=calls, MA_TOY_SLOW_FROM="3")
    saved = (P_.BACKSTOP_MULTIPLE, P_.BACKSTOP_FLOOR_S)
    P_.BACKSTOP_MULTIPLE, P_.BACKSTOP_FLOOR_S = 1, 0.0
    try:
        s1 = run_v2("toy question", d, T, signoff="none", log=quiet, backend=Batch(8), cross_engine_check=False)
    finally:
        os.environ.pop("MA_TOY_SLOW_FROM", None)
        P_.BACKSTOP_MULTIPLE, P_.BACKSTOP_FLOOR_S = saved
    st = load_state(d)
    first = open(calls).read().split() if os.path.exists(calls) else []
    kill = next((r for r in json.load(open(os.path.join(d, P_.REPORT)))["steps"] if r["id"] == "batch"), {})
    ok(s1 == "run_failed" and st.history[-1]["from"] == "ANALYZE" and st.history[-1].get("resumable") == "timeout"
       and resumable_failure(d, st) == "timeout" and first[:3] == ["0", "1", "2"]
       and "at its backstop" in str(kill.get("error")),
       f"T58 an analysis step killed at its backstop ends run_failed, marked resumable ({s1}, {st.history[-1]}, "
       f"calls {first}, {kill.get('error')})")
    # T59: --resume (run_v2 on the same run) continues at ANALYZE with the step limit as the backend gives it now,
    # and the batch computes only the rows the killed attempt had not finished.
    s2 = run_v2("toy question", d, T, signoff="none", log=quiet, backend=Batch(120), cross_engine_check=False)
    st = load_state(d)
    again = open(calls).read().split()[len(first):]
    back = [h for h in st.history if h.get("from") == "FINAL"]
    tmo = {s["id"]: s.get("timeout_s") for s in yaml.safe_load(open(os.path.join(d, "models", "pipeline.yaml")))["steps"]}
    ok(s2 in ("completed", "completed_scope_declared") and len(back) == 1 and back[0]["to"] == "ANALYZE"
       and back[0].get("resumed_from") == "run_failed" and tmo["batch"] == 120,
       f"T59 --resume reopens the run at ANALYZE, rewrites the step limits and finishes ({s2}, {back}, {tmo})")
    ok(sorted(again) == ["3", "4", "5"],
       f"T59b the resumed batch reuses the rows the killed attempt kept and computes the rest only ({again})")
    ok(run_v2("toy question", d, T, signoff="none", log=quiet, backend=Batch(120), cross_engine_check=False) == s2
       and len([h for h in load_state(d).history if h.get("from") == "FINAL"]) == 1,
       "T59c a finished run is not reopened")
    # T60: other run_failed causes stay terminal.
    d, T = toy_run("analyze_error")
    open(os.path.join(td, "toy_fail.py"), "w").write("import sys\nsys.exit(3)\n")
    fail = Batch(30, run=f"{sys.executable} {os.path.join(td, 'toy_fail.py')}")
    s1 = run_v2("toy question", d, T, signoff="none", log=quiet, backend=fail, cross_engine_check=False)
    n = len(load_state(d).history)
    s2 = run_v2("toy question", d, T, signoff="none", log=quiet, backend=fail, cross_engine_check=False)
    st = load_state(d)
    ok(s1 == s2 == "run_failed" and len(st.history) == n and st.history[-1]["from"] == "ANALYZE"
       and "resumable" not in st.history[-1]
       and resumable_failure(d, st) is None,
       f"T60 an analysis step that fails on its own stays run_failed on resume ({s1}, {s2}, {st.history[-1]})")
    _self_test_calibration_refused(ok, td, toy_run, Batch, quiet)
    rep = {"ok": False, "errors": [], "steps": [{"id": "analyze", "status": "failed", "returncode": 1, "error": ""}]}
    causes = [analysis_failure_cause(dict(rep, steps=[dict(rep["steps"][0], returncode=rc, error=e)]))
              for rc, e in ((-9, ""), (-15, ""), (-11, ""), (1, "boom"), (None, "OSError: no such file"))]
    ok(causes == ["killed", "killed", None, None, None],
       f"T60b killed by SIGKILL/SIGTERM is resumable; a crash, an error exit or a failed start is not ({causes})")
    st_spec = RunState(question="q", state="FINAL", status="run_failed",
                       history=[{"from": "SPEC", "to": "FINAL", "why": "the spec did not validate"}])
    os.makedirs(os.path.join(d, "models"), exist_ok=True)
    json.dump(dict(rep, steps=[dict(rep["steps"][0], returncode=None, error="timed out after 9s (process group killed)")]),
              open(os.path.join(d, "models", "pipeline_report.json"), "w"))
    ok(resumable_failure(d, st_spec) is None and resumable_failure(d, RunState(
        question="q", state="FINAL", status="completed", history=[{"from": "ANALYZE", "to": "FINAL"}])) is None,
       "T60c a run_failed outside ANALYZE, or another status, is not resumable whatever the report says")

    class Unreadable(Batch):
        def pipeline_steps(self, spec_path, run_dir):
            raise ValueError("MA_COMPUTE_MAX_CORE_HOURS='lots' is not a number")
    d, T = toy_run("analyze_unreadable")
    s1 = run_v2("toy question", d, T, signoff="none", log=quiet, backend=Unreadable(30), cross_engine_check=False)
    st = load_state(d)
    ok(s1 == "run_failed" and st.history[-1]["from"] == "ANALYZE" and "MA_COMPUTE_MAX_CORE_HOURS" in st.history[-1]["why"],
       f"T60d an unreadable compute budget fails the run at ANALYZE (fail closed): {st.history[-1]}")
    # T61 (Nigeria v2 full run F3, 2026-11-02: 16 of 256 design points failed with kind infra when the autoscaler
    # retired busy workers, the analysis failed closed, and the run could not be resumed): an analysis that failed
    # only because engine rows failed with kind infra is resumable, read from the batch summary compute.py wrote.
    d, T = toy_run("analyze_infra")
    calls2 = os.path.join(td, "toy_calls_infra.txt")
    os.environ.update(MA_TOY_CALLS=calls2, MA_TOY_INFRA_FROM="3")
    try:
        s1 = run_v2("toy question", d, T, signoff="none", log=quiet, backend=Batch(120), cross_engine_check=False)
    finally:
        os.environ.pop("MA_TOY_INFRA_FROM", None)
    st = load_state(d)
    first = open(calls2).read().split() if os.path.exists(calls2) else []
    ok(s1 == "run_failed" and st.history[-1]["from"] == "ANALYZE" and st.history[-1].get("resumable") == "infra"
       and resumable_failure(d, st) == "infra" and sorted(first) == ["0", "1", "2", "3", "4", "5"],
       f"T61 an analysis that failed only on infra engine rows ends run_failed, marked resumable ({s1}, "
       f"{st.history[-1]}, calls {first})")
    s2 = run_v2("toy question", d, T, signoff="none", log=quiet, backend=Batch(120), cross_engine_check=False)
    st = load_state(d)
    again = open(calls2).read().split()[len(first):]
    back = [h for h in st.history if h.get("from") == "FINAL"]
    ok(s2 in ("completed", "completed_scope_declared") and len(back) == 1 and back[0]["to"] == "ANALYZE"
       and "infra" in back[0]["why"] and sorted(again) == ["3", "4", "5"],
       f"T61b --resume reopens the run at ANALYZE and the batch recomputes only the infra rows ({s2}, {back}, "
       f"{again})")
    # T61c: the cause is read from the batch summaries this run of the pipeline wrote: infra only.
    d = os.path.join(td, "infra_cases")
    os.makedirs(os.path.join(d, "models"), exist_ok=True)
    os.makedirs(os.path.join(d, "analysis", "batch"), exist_ok=True)
    rp = os.path.join(d, "models", "pipeline_report.json")
    sp = os.path.join(d, "analysis", "batch", "points.jsonl.summary.json")
    rep1 = {"ok": False, "errors": [], "seconds": 30.0,
            "steps": [{"id": "analyze", "status": "failed", "returncode": 1, "error": "ValueError: 512 engine results "
                       "missing"}]}

    def cause_with(kinds, age_s=0.0, summary=True):
        json.dump(rep1, open(rp, "w"))
        if os.path.exists(sp):
            os.remove(sp)
        if summary:
            json.dump({"n": 256, "n_failed": sum(kinds.values()), "failed_by_kind": kinds, "stopped": None},
                      open(sp, "w"))
            t_ = time.time() - age_s
            os.utime(sp, (t_, t_))
        return analysis_failure_cause(rep1, d)
    got = [cause_with({"infra": 16}), cause_with({"infra": 16, "model": 1}), cause_with({"stopped": 4}),
           cause_with({}), cause_with({"infra": 16}, age_s=3600), cause_with({"infra": 16}, summary=False),
           analysis_failure_cause(rep1)]
    ok(got == ["infra", None, None, None, None, None, None],
       f"T61c infra only, in a summary written during this pipeline run, is resumable; a model or stopped row, no "
       f"failed row, an older summary, no summary, or no run dir is not ({got})")
    # T62 (F6 preview, 2026-11-11): the joint calibration's bench batch lost one task to infra, in a run dir restored
    # from a checkpoint (every mtime reset); "this pipeline run's" summaries are known by the pipeline run id
    r = os.path.join(td, "restored")
    for sub in ("models", "analysis/batch", "analysis/joint_calibration/call2"):
        os.makedirs(os.path.join(r, sub), exist_ok=True)
    rep2 = dict(rep1, pipeline_run="P1")
    files = {"models/pipeline_report.json": rep2,
             "analysis/joint_calibration/bench_call3_phase1.jsonl.summary.json":
                 {"pipeline_run": "P1", "n": 42, "n_failed": 1, "failed_by_kind": {"infra": 1}},
             "analysis/joint_calibration/call2/points.jsonl.summary.json":
                 {"pipeline_run": "P1", "n": 64, "n_failed": 0, "failed_by_kind": {}},
             "analysis/batch/results.jsonl.summary.json":            # an earlier pipeline run's: never read now
                 {"pipeline_run": "P0", "n": 8, "n_failed": 1, "failed_by_kind": {"model": 1}}}

    def restored(report_age=0.0, **over):
        for rel, obj in {**files, **over}.items():
            json.dump(obj, open(os.path.join(r, rel), "w"))
            os.utime(os.path.join(r, rel), (1e9, 1e9))
        os.utime(os.path.join(r, "models", "pipeline_report.json"), (1e9 + report_age, 1e9 + report_age))
        return analysis_failure_cause(rep2, r)
    got2 = [restored(), restored(report_age=3600.0),
            restored(**{"analysis/joint_calibration/bench_call3_phase1.jsonl.summary.json":
                        {"pipeline_run": "P1", "n": 42, "n_failed": 1, "failed_by_kind": {"model": 1}}})]
    # T62b (Codex on #200): an optional batch (a sensitivity run under its own subdirectory) that lost rows to infra
    # does not make a model rejection resumable; only the primary joint-calibration batches and analysis/batch count
    ok_bench = {"pipeline_run": "P1", "n": 42, "n_failed": 0, "failed_by_kind": {}}
    sens = os.path.join(r, "analysis", "joint_calibration", "sensitivity_s1", "call1")
    os.makedirs(sens, exist_ok=True)
    json.dump({"pipeline_run": "P1", "n": 9, "n_failed": 2, "failed_by_kind": {"infra": 2}},
              open(os.path.join(sens, "points.jsonl.summary.json"), "w"))
    optional = restored(**{"analysis/joint_calibration/bench_call3_phase1.jsonl.summary.json": ok_bench})
    os.remove(os.path.join(sens, "points.jsonl.summary.json"))
    # T62c (Codex on #200): when the report has a pipeline run id, an untagged summary never counts, however new
    untagged = os.path.join(r, "analysis", "batch", "old.jsonl.summary.json")
    json.dump({"n": 4, "n_failed": 1, "failed_by_kind": {"infra": 1}}, open(untagged, "w"))
    old_only = restored(**{"analysis/joint_calibration/bench_call3_phase1.jsonl.summary.json": ok_bench})
    os.utime(untagged, None)
    old_only_fresh = analysis_failure_cause(rep2, r)
    os.remove(untagged)
    ok(optional is None and old_only is None and old_only_fresh is None,
       f"T62b-c an infra-only optional (sensitivity) batch, or an untagged summary when the report has a run id, "
       f"never makes the failure resumable: {optional} {old_only} {old_only_fresh}")
    st2 = RunState(question="q", state="FINAL", status="run_failed",
                   history=[{"from": "ANALYZE", "to": "FINAL", "why": "analysis failed"}])
    restored()
    ok(got2 == ["infra", "infra", None] and resumable_failure(r, st2) == "infra",
       f"T62 a restored run dir whose joint-calibration bench batch lost one task to infra is resumable 'infra' "
       f"(summaries matched by pipeline run id, not mtimes; an earlier run's model failure is ignored; a model "
       f"failure in this run is not resumable), so --resume reopens it at ANALYZE: {got2}")


_TOY_REFUSAL = """import json, hashlib, os, sys
rd = sys.argv[1]
os.makedirs(os.path.join(rd, "analysis"), exist_ok=True)
blocked = ("schema: analysis.blocked/1\\ndata:\\n  reason: 'calibration_ridge: the likelihood has 1 flat direction(s); 3 of "
           "4 parameter(s) sit at a bound: z:u0, z:u1, z:u2'\\n  where: calibration\\n  exception: CalibrationRefused\\n"
           "  code: calibration_ridge\\n  at_bound: [z:u0, z:u1, z:u2]\\n")
p = os.path.join(rd, "analysis", "blocked.yaml")
open(p, "w").write(blocked)
sha = hashlib.sha256(open(p, "rb").read()).hexdigest()
json.dump({"status": "blocked", "artifacts": {"analysis/blocked.yaml": {"kind": "blocked", "sha256": sha}}},
          open(os.path.join(rd, "analysis", "manifest.json"), "w"))
sys.stderr.write("toolkit.analysis.calibration.CalibrationRefused: calibration_ridge: the likelihood has 1 flat "
                 "direction(s)\\n")
sys.exit(1)
"""


def _self_test_calibration_refused(ok, td: str, toy_run, Batch, quiet) -> None:
    """CRF1-4 (eval baseline: a refused calibration ended the run at ANALYZE): it goes to the spec revision with the
    refusal and the parameters at a bound as a finding, at most MAX_CALIBRATION_REFUSALS times, then ends run_failed
    as before; conflicts.policy: stop keeps the old stop; another failure is not one."""
    drv = os.path.join(td, "toy_refusal.py")
    open(drv, "w").write(_TOY_REFUSAL)

    class Refuse(Batch):
        """The toy batch step replaced by one that refuses the calibration as the analysis does."""
        def pipeline_steps(self, spec_path, run_dir):
            steps = super().pipeline_steps(spec_path, run_dir)
            steps[0] = dict(steps[0], run=f"{sys.executable} {drv} {run_dir}")
            return steps
    refuse = Refuse(60)
    d, T = toy_run("calibration_refused")
    s1 = run_v2("toy question", d, T, signoff="none", log=quiet, backend=refuse, cross_engine_check=False)
    st = load_state(d)
    back = [h for h in st.history if h.get("from") == "ANALYZE"]
    led = FL.load(d)
    f = led.get(CALIBRATION_REFUSED)
    ok(s1 == "run_failed" and [h.get("to") for h in back] == ["SPEC"] * MAX_CALIBRATION_REFUSALS + ["FINAL"]
       and all("calibration refused (calibration_ridge" in h.get("why", "") for h in back[:-1])
       and "cap 2" in back[-1].get("why", "") and st.tries.get(CALIBRATION_REFUSED) == MAX_CALIBRATION_REFUSALS,
       f"CRF1 a refused calibration goes back to SPEC {MAX_CALIBRATION_REFUSALS} times, then ends run_failed "
       f"({s1}, {[(h.get('to'), h.get('why', '')[:60]) for h in back]})")
    ok(f is not None and f.owner_state == "SPEC" and f.severity == "HIGH" and not f.scope_declarable
       and "z:u0, z:u1, z:u2" in f.claim and "calibration.scale" in (f.fix_requires or "")
       and "calibration_ridge): calibration_ridge" not in f.claim,
       f"CRF2 the finding names the refusal, the parameters at a bound and the remedy ({f and f.claim[:160]})")
    req = rethink_request(d, st.round + 99)
    ok(any(x.get("id") == CALIBRATION_REFUSED for x in req.get("findings") or []),
       "CRF3 a signed spec's amendment request carries the refusal (the spec owns it)")
    # CRF4: conflicts.policy stop keeps today's behaviour: the first refusal ends the run
    d2, T2 = toy_run("calibration_refused_stop")
    orig = sm.conflicts_policy_stop
    sm.conflicts_policy_stop = lambda run_dir: True
    try:
        s2 = run_v2("toy question", d2, T2, signoff="none", log=quiet, backend=refuse, cross_engine_check=False)
    finally:
        sm.conflicts_policy_stop = orig
    back2 = [h.get("to") for h in load_state(d2).history if h.get("from") == "ANALYZE"]
    rep_other = {"ok": False, "errors": [], "steps": [{"id": "analyze", "status": "failed", "returncode": 1,
                                                       "error": "ValueError: boom"}]}
    ok(s2 == "run_failed" and back2 == ["FINAL"] and calibration_refusal(rep_other, d2) is None,
       f"CRF4 under conflicts.policy stop the first refusal ends the run; another failure is no refusal ({back2})")


def _self_test_geometry(ok, td: str) -> None:
    """IG-M1-3 (docs/design/v2-input-geometry.md §4): the input geometry check through the machine. A units table
    whose two rows carry each other's points is refused before SIGN; accepted by name, it reaches SIGN; the gate
    carries the open finding."""
    import yaml
    from v2_tasks import StubTasks
    from toolkit.spec import schema as S
    from toolkit.spec import geometry as GEO
    toolkit = AB.backend("toolkit")

    def setup(d, accept=None):
        os.makedirs(d)
        raw = yaml.safe_load(open(S.toy_spec(d)))
        os.remove(os.path.join(d, AB.SPEC_FILE))
        rows = open(os.path.join(d, "units.csv")).read().strip().split("\n")
        feats, out = [], [rows[0] + ",lon,lat"]
        for k, line in enumerate(rows[1:]):
            x = k * 0.6
            feats.append({"type": "Feature", "properties": {"fid": str(k)}, "geometry": {
                "type": "Polygon", "coordinates": [[[x, 10], [x + 0.5, 10], [x + 0.5, 10.5], [x, 10.5], [x, 10]]]}})
            j = {0: 1, 1: 0}.get(k, k)                      # units 0 and 1 carry each other's points
            out.append(f"{line},{j * 0.6 + 0.25:.3f},10.25")
        open(os.path.join(d, "units.csv"), "w").write("\n".join(out) + "\n")
        json.dump({"type": "FeatureCollection", "features": feats}, open(os.path.join(d, "b.geojson"), "w"))
        yaml.safe_dump({"boundaries": {"path": "b.geojson", "feature_id": "fid"}},
                       open(os.path.join(d, "presentation.yaml"), "w"))
        if accept:
            raw["decision_units"][GEO.ACCEPT_KEY] = accept
        st = init_run(d, "toy question", 3, "owner", "v2", None, derived_inputs=False)
        return Machine(d, StubTasks({"spec": raw}), st, backend=toolkit, log=lambda *a: None, cross_engine_check=False)
    d1 = os.path.join(td, "ig1")
    m1 = setup(d1)
    status = m1.run(max_steps=12)
    fb = load_state(d1).tries.get("spec_feedback") or []
    ok(status == "run_failed" and any(x.startswith("reciprocal_swap") and "reciprocal_swap:0+1" in x for x in fb)
       and "SIGN" not in [h.get("to") for h in load_state(d1).history],
       f"IG-M1 a units table whose rows carry each other's points is refused before SIGN ({status}; {fb[:1]})")
    # SH1 (baseline hb2 §4.6): each failed try's feedback is kept whole in its history entry (the `why` line is cut
    # short, and spec_feedback is cleared once a try passes), so a later reader can tell what each try was told.
    sh = [h for h in load_state(d1).history if h.get("from") == "SPEC" and h.get("to") == "SPEC"]
    ok(sh and all(isinstance(h.get("feedback"), list) and h["feedback"] for h in sh)
       and sh[-1]["feedback"] == fb and any(len("; ".join(h["feedback"])) > len(h["why"]) for h in sh),
       f"SH1 every SPEC retry records its try's whole feedback in history ({[len(h.get('feedback') or []) for h in sh]})")
    hf = {f["kind"]: f for f in m1.harness_findings()}
    ok(hf.get("reciprocal_swap", {}).get("severity") == "HIGH" and hf["reciprocal_swap"]["scope_declarable"] is False,
       f"IG-M3 the gate carries the open swap as a HIGH finding that cannot be declared ({sorted(hf)})")
    d2 = os.path.join(td, "ig2")
    why = "the owner checked both rows against the source"
    m2 = setup(d2, {"reciprocal_swap:0+1": why})
    m2.run(max_steps=12)
    rep = json.load(open(os.path.join(d2, GEO.ARTIFACT)))
    still = "reciprocal_swap" in {f["kind"] for f in m2.harness_findings()}
    ok(m2.st.state == "SIGN" and m2.st.paused == "awaiting_signoff"
       and [f.get("proposed") for f in rep["findings"]] == [why] and still,
       f"IG-M2a proposed by name with a reason, the spec reaches SIGN, where the owner decides; until then the gate "
       f"still carries the swap ({m2.st.state}, {m2.st.paused}, {still})")
    toolkit.sign(d2, "owner")
    m2.step()
    ok(m2.st.state == "ENGINE_CHOICE" and "reciprocal_swap" not in {f["kind"] for f in m2.harness_findings()},
       f"IG-M2b the owner's sign-off approves the proposed exception: the run goes on and the gate no longer carries "
       f"it ({m2.st.state})")
    # IG-M4 (review P1): under --signoff lead a proposed HIGH exception pauses for the owner, as a spec sign-off does
    d4 = os.path.join(td, "ig4")
    os.makedirs(d4)
    m4 = setup(os.path.join(d4, "run"), {"reciprocal_swap:0+1": why})
    m4.st.signoff = "lead"
    m4.run(max_steps=12)
    from toolkit.spec import signoff as SO
    ok(m4.st.state == "SIGN" and m4.st.paused == "awaiting_signoff" and SO.status(m4.run_dir) == "awaiting",
       f"IG-M4a --signoff lead: a HIGH geometry exception is not lead-signed; the run pauses for the owner "
       f"({m4.st.state}, {m4.st.paused}, {SO.status(m4.run_dir)})")
    toolkit.sign(m4.run_dir, "owner")
    m4.step()
    ok(m4.st.state == "ENGINE_CHOICE" and "reciprocal_swap" not in {f["kind"] for f in m4.harness_findings()},
       f"IG-M4b once the owner signs, the run goes on with the exception approved ({m4.st.state})")
    import v2_sdk_tasks as SDK
    ok({GEO.ARTIFACT, GEO.SIGNOFF_FILE} <= set(SDK.HARNESS_FILES),
       "IG-M5 the check's record and the owner's geometry sign-off are harness files no task can write")


def _self_test_derived(ok, td: str) -> None:
    """DI1, DI8 and the development flag at the gate, through the machine (docs/design/v2-derived-inputs.md §4)."""
    import shutil
    import yaml
    from v2_tasks import StubTasks
    from toolkit.spec import schema as S
    import pipeline as P
    toolkit = AB.backend("toolkit")
    flag = P.sandbox_kind() is None
    # DI1: the evidence's shape. The task writes the units table itself; the spec is refused before SIGN.
    d = os.path.join(td, "di1")
    os.makedirs(d)
    raw = yaml.safe_load(open(S.toy_spec(d)))
    os.remove(os.path.join(d, AB.SPEC_FILE))
    st = init_run(d, "toy question", 3, "none", "v2", None, derived_inputs=True, derive_unsandboxed=flag)
    status = Machine(d, StubTasks({"spec": raw}), st, backend=toolkit, log=lambda *a: None,
                     cross_engine_check=False).run(max_steps=12)
    s1 = load_state(d)
    fb = s1.tries.get("spec_feedback") or []
    ok(status == "run_failed" and any(x.startswith("units_not_derived") for x in fb)
       and "SIGN" not in [h.get("to") for h in s1.history],
       f"DI1c through the machine: a units table the task wrote is refused before SIGN ({status}; {fb[:2]})")
    # DI8: engine auto with two candidates; the second needs an attribute no derived column binds.
    mods = os.path.join(td, "di8mods")
    os.makedirs(mods)
    with open(os.path.join(mods, "di8_engines.py"), "w") as f:
        f.write("from toolkit.engines.contract import ToyEngine\n\n\nclass NeedsGroup(ToyEngine):\n"
                "    \"\"\"The toy engine with a card that gives `group` no default.\"\"\"\n")
    with open(os.path.join(mods, "attribute_binding.yaml"), "w") as f:
        yaml.safe_dump({"attribute_binding": {"x": "required", "group": "required"}}, f)
    sys.path.insert(0, mods)
    from toolkit import import_guard as _G
    _G.allow_for_test("engine", "di8_engines")             # a test engine outside toolkit/engines (allowlist)
    _G.register_test_engine("di8_engines:NeedsGroup")      # and outside the engine registry: this self-test only
    fake = os.path.join(td, "di8repo")
    os.makedirs(os.path.join(fake, "toyraw"))
    tmp = os.path.join(td, "di8src")
    os.makedirs(tmp)
    base = yaml.safe_load(open(S.toy_spec(tmp)))
    shutil.copyfile(os.path.join(tmp, "units.csv"), os.path.join(fake, "toyraw", "units_raw.csv"))
    cols = open(os.path.join(fake, "toyraw", "units_raw.csv")).readline().strip().split(",")
    staged = "data/inputs/toyraw/units_raw.csv"

    def draft(question, run_dir):
        os.makedirs(os.path.join(run_dir, "data", "derive"), exist_ok=True)
        open(os.path.join(run_dir, "data", "derive", "units.py"), "w").write(S.TOY_DERIVE_SCRIPT)
        yaml.safe_dump({"derivations": [{"id": "units", "script": "data/derive/units.py", "inputs": [staged],
                                         "outputs": ["data/derived/units.csv"],
                                         "columns": {"uses": {staged: cols}}}]},
                       open(os.path.join(run_dir, "data", "derivations.yaml"), "w"), sort_keys=False)
        r = json.loads(json.dumps(base))
        r["engine"] = {"auto": True, "candidates": ["toolkit.engines.contract:ToyEngine", "di8_engines:NeedsGroup"]}
        r["decision_units"]["csv"] = "data/derived/units.csv"
        r["decision_units"]["defaults_accepted"] = {"group": "the toy units carry no group label"}
        r["calibration"]["target_frame"] = {"band": "prev", "source": "the engine's own window metric"}
        return r
    d = os.path.join(td, "di8")
    os.makedirs(d)
    global REPO
    old_repo, REPO = REPO, fake
    try:
        st = init_run(d, "Which package per unit? Inputs: toyraw/units_raw.csv", 3, "owner", "v2", None,
                      derived_inputs=True, derive_unsandboxed=flag)
        m = Machine(d, StubTasks({"spec": draft}), st,
                    backend=toolkit, log=lambda *a: None, cross_engine_check=False)
        m.run(max_steps=4)
        from toolkit.spec import signoff as _SO
        sheet = _SO.sheet(d, getattr(toolkit, "candidates", None))
        ok(m.st.state == "SIGN" and m.st.paused == "awaiting_signoff" and "NeedsGroup" in sheet
           and "engine_attribute_unbound" in sheet and "group" in sheet,
           f"DI8a engine auto: the full checks run per candidate in SPEC, before SIGN; the candidate that needs an "
           f"unbound attribute is shown ineligible with the reason ({m.st.state}, {m.st.tries.get('spec_feedback')})")
        toolkit.sign(d, "owner")
        for _ in range(6):
            if m.st.state in ("ANALYZE", "FINAL"):
                break
            m.step()
        ec = json.load(open(os.path.join(d, AB.ENGINE_CHOICE)))
        rows = {r["ref"]: r for r in ec["candidates"]}
        ng = rows.get("di8_engines:NeedsGroup") or {}
        ok(ec["engine"]["ref"] == "toolkit.engines.contract:ToyEngine" and ng.get("eligible") is False
           and any("engine_attribute_unbound" in w and "group" in w for w in ng.get("why_not") or []),
           f"DI8b engine_choice.json records that candidate as ineligible with the unbound list: {ng.get('why_not')}")
        kinds = {f["kind"]: f for f in m.harness_findings()}
        rec_kind = json.load(open(os.path.join(d, P.DERIVATIONS_RECORD)))["derivations"]["units"]["sandbox"]["kind"]
        ok(("derivation_unsandboxed" in kinds and kinds["derivation_unsandboxed"]["severity"] == "HIGH"
            and not kinds["derivation_unsandboxed"]["scope_declarable"]) if rec_kind == "none"
           else "derivation_unsandboxed" not in kinds,
           f"DI-U3 a run that used the development flag carries a HIGH non-declarable gate finding (no ACCEPT); a "
           f"sandboxed one does not (sandbox {rec_kind}; {sorted(kinds)})")
    finally:
        REPO = old_repo
        sys.path.remove(mods)


def _self_test_evidence(ok, td: str) -> None:
    """v2 autonomy 1B-ii (D1): the EVIDENCE stage before SIGN, on the toy engine whose component declares decision
    inputs (toolkit/spec/toy_engines.py ToyDossierEngine)."""
    import yaml
    from v2_tasks import StubTasks
    from toolkit.spec import evidence_dossier as ED, toy_engines as TE
    EDR = ED
    TE.enable_for_test()
    toolkit = AB.backend("toolkit")
    full = ED.toy_entries()

    def setup(name, signoff, script, dossier=True):
        d = os.path.join(td, name)
        os.makedirs(d)
        raw = yaml.safe_load(open(ED.toy_dossier_spec(d, dossier=dossier)))
        for f in (AB.SPEC_FILE, ED.DOSSIER_FILE):
            if os.path.exists(os.path.join(d, f)):
                os.remove(os.path.join(d, f))
        st = init_run(d, "toy question", 3, signoff, "v2", None, derived_inputs=False)
        T = StubTasks(dict({"spec": raw}, **script))
        return Machine(d, T, st, backend=toolkit, log=lambda *a: None, cross_engine_check=False), T

    def until(m, stop=("ENGINE_CHOICE",), n=14):
        for _ in range(n):
            if m.st.state in stop or m.st.state == "FINAL" or m.st.paused:
                break
            m.step()
        return [h.get("to") for h in m.st.history if "to" in h]

    def fill(run_dir, feedback):        # the evidence task: two inputs observed or calibrated, one with no data
        doc = yaml.safe_load(open(os.path.join(run_dir, ED.DOSSIER_FILE)))["inputs"]
        k = dict(doc["parameter:k"], no_data={"reason": "no field measurement", "borrowing_rule": "the card's value"})
        return {"parameter:eff_a": full["parameter:eff_a"], "unit_attribute:x": full["unit_attribute:x"],
                "parameter:k": k}

    # T_DOSSIER_AUTO: lead sign-off, the spec names the dossier and no one wrote it: scaffold, one task, signed
    m, T = setup("ev_lead", "lead", {"evidence": fill})
    hist = until(m)
    rec = EDR.read_record(m.run_dir) or {}
    so = _signoff(m.run_dir) or {}
    ok(hist[:3] == ["EVIDENCE", "SIGN", "ENGINE_CHOICE"] and T.calls.count("draft_evidence") == 1
       and rec.get("status") == "complete" and so.get("signed_by") == "lead"
       and so.get("evidence", {}).get("dossier") == ED.DOSSIER_FILE,
       f"T_DOSSIER_AUTO under --signoff lead the run drafts the dossier before SIGN and the lead signs it ({hist}; "
       f"{T.calls}; {rec.get('status')}; {rec.get('problems')})")
    g = rec.get("gaps") or {}
    ok(g.get("no_data") == ["parameter:k", "unit_attribute:x"] and g.get("default_kept") == ["parameter:k"]
       and g.get("sources_unchecked") == ["parameter:eff_a"]
       and "provisional" in so.get("note", "") and EDR.RECORD in so.get("note", ""),
       f"T_DOSSIER_GAPS the gaps are declared in the record and the lead's note ({rec.get('gaps')}; {so.get('note')})")
    # T_SIGN_NO_REPAIR: a refused lead sign-off pauses cleanly for the owner at SIGN without auto-repair loops
    class Stale(StubTasks):
        def sign_spec(self, run_dir):
            if self.calls.count("sign_spec") == 0:
                os.remove(os.path.join(run_dir, ED.DOSSIER_FILE))
            return super().sign_spec(run_dir)
    d = os.path.join(td, "ev_repair")
    os.makedirs(d)
    raw = yaml.safe_load(open(ED.toy_dossier_spec(d, dossier=False)))
    raw["evidence"] = {"dossier": ED.DOSSIER_FILE}
    os.remove(os.path.join(d, AB.SPEC_FILE))
    T = Stale({"spec": raw, "evidence": fill})
    m = Machine(d, T, init_run(d, "toy question", 3, "lead", "v2", None, derived_inputs=False), backend=toolkit,
                log=lambda *a: None, cross_engine_check=False)
    hist = until(m)
    ok(m.st.state == "SIGN" and m.st.paused == "awaiting_signoff"
       and not os.path.exists(os.path.join(d, SIGNOFF_FILE)),
       f"T_SIGN_NO_REPAIR a refused lead sign-off waits for the owner at SIGN without looping ({hist}; {T.calls})")
    # T_EVIDENCE_EXHAUSTED: a task that never supplies the evidence pauses at SIGN for the owner, unsigned
    m, T = setup("ev_none", "lead", {})
    hist = until(m)
    rec = EDR.read_record(m.run_dir) or {}
    ok(m.st.state == "SIGN" and m.st.paused == "awaiting_signoff" and T.calls.count("draft_evidence") == EDR.MAX_TRIES
       and rec.get("status") == "incomplete"
       and not os.path.exists(os.path.join(m.run_dir, SIGNOFF_FILE)),
       f"T_EVIDENCE_EXHAUSTED the check is never loosened: after {EDR.MAX_TRIES} drafts "
       f"the run waits for the owner, unsigned ({hist}; {T.calls})")
    # T_EVIDENCE_OWNER: under --signoff owner the drafted dossier is on the sheet the owner views
    m, T = setup("ev_owner", "owner", {"evidence": fill})
    until(m)
    from toolkit.spec import signoff as SO
    sheet = SO.sheet(m.run_dir)
    ok(m.st.state == "SIGN" and m.st.paused == "awaiting_signoff" and ED.SECTION in sheet, f"T_EVIDENCE_OWNER the "
       f"owner is shown the drafted dossier on the sheet ({m.st.state}, {m.st.paused})")
    toolkit.sign(m.run_dir, "owner")
    m.step()
    ok(m.st.state == "ENGINE_CHOICE", f"T_EVIDENCE_OWNER_SIGNED once the owner signs, the run goes on ({m.st.state})")
    # T_EVIDENCE_NONE: --signoff none: the drafted dossier covers the engine at ENGINE_CHOICE (it failed there before)
    m, T = setup("ev_unsigned", "none", {"evidence": fill})
    hist = until(m, stop=("ANALYZE",))
    ok(hist[:4] == ["EVIDENCE", "SIGN", "ENGINE_CHOICE", "ANALYZE"]
       and m.st.state == "ANALYZE",
       f"T_EVIDENCE_NONE --signoff none passes ENGINE_CHOICE's coverage check with the drafted dossier ({hist})")
    # BE27: --signoff none refuses an engine.build spec at SPEC so no paid build starts unsigned
    from toolkit.engines.built import requested as _BREQ
    d_b27 = os.path.join(td, "be27_build_none")
    os.makedirs(d_b27)
    raw_b27 = yaml.safe_load(open(ED.toy_dossier_spec(d_b27, dossier=False)))
    raw_b27["engine"] = {"build": _BREQ.example_build()}
    for f in (AB.SPEC_FILE, ED.DOSSIER_FILE):
        if os.path.exists(os.path.join(d_b27, f)):
            os.remove(os.path.join(d_b27, f))
    st_b27 = init_run(d_b27, "toy build question", 3, "none", "v2", None, derived_inputs=False)
    T_b27 = StubTasks({"spec": raw_b27})
    m_b27 = Machine(d_b27, T_b27, st_b27, backend=toolkit, log=lambda *a: None, cross_engine_check=False)
    m_b27.step()
    fb27 = m_b27.st.tries.get("spec_feedback") or []
    ok(m_b27.st.state == "SPEC" and any("signed request" in str(x) for x in fb27),
       f"BE27 --signoff none refuses a build spec at SPEC ({m_b27.st.state}, {fb27})")
    # BE20b: harness_owned() refuses deliverables targeting engine_build/* or models/ext/engine_*
    ok(owned_path(d_b27, "engine_build/admission.json", harness_owned())
       and owned_path(d_b27, "models/ext/engine_x/engine.py", harness_owned())
       and not owned_path(d_b27, "allocation_out.csv", harness_owned()),
       "BE20b harness_owned() covers engine_build/* and models/ext/engine_*")
    # T_EVIDENCE_NAME: the options need inputs and the spec names no dossier: back to the draft, nothing written
    d = os.path.join(td, "ev_name")
    os.makedirs(d)
    raw = yaml.safe_load(open(ED.toy_dossier_spec(d, dossier=False)))
    os.remove(os.path.join(d, AB.SPEC_FILE))
    m = Machine(d, StubTasks({"spec": raw}), init_run(d, "toy question", 3, "lead", "v2", None, derived_inputs=False),
                backend=toolkit, log=lambda *a: None, cross_engine_check=False)
    m.step()        # hb2 (HS6): the SPEC try itself hears it, before the evidence stage
    ok(m.st.state == "SPEC" and m.st.tries.get("spec") == 1
       and any("name the evidence dossier" in x for x in m.st.tries.get("spec_feedback") or [])
       and not os.path.exists(os.path.join(d, ED.DOSSIER_FILE)),
       f"T_EVIDENCE_NAME a spec that needs a dossier and names none is redrafted with the reason "
       f"({m.st.state}; {m.st.tries.get('spec_feedback')})")
    # T_EVIDENCE_RESUME: a run signed under the earlier order (SIGN, then EVIDENCE) resumes past EVIDENCE untouched
    d = os.path.join(td, "ev_resume")
    os.makedirs(d)
    ED.toy_dossier_spec(d)
    toolkit.sign(d, "owner")
    before = open(os.path.join(d, ED.DOSSIER_FILE), "rb").read()
    T = StubTasks({})
    m = Machine(d, T, RunState(question="q", state="EVIDENCE", signoff="owner"), backend=toolkit, log=lambda *a: None,
                cross_engine_check=False)
    m.step()
    m.step()
    ok(m.st.state == "ENGINE_CHOICE" and T.calls == [] and open(os.path.join(d, ED.DOSSIER_FILE), "rb").read() == before
       and [h.get("to") for h in m.st.history] == ["SIGN", "ENGINE_CHOICE"],
       f"T_EVIDENCE_RESUME a spec already signed is left as signed and SIGN checks it ({m.st.state}, {T.calls})")
    # T_EVIDENCE_OWNER_MODE (Codex r1 P1-2): an owner-mode run whose spec the lead signed does not pass SIGN
    d = os.path.join(td, "ev_leadsigned")
    os.makedirs(d)
    ED.toy_dossier_spec(d)
    toolkit.sign(d, "lead")
    m = Machine(d, StubTasks({}), RunState(question="q", state="EVIDENCE", signoff="owner"), backend=toolkit,
                log=lambda *a: None, cross_engine_check=False)
    until(m, n=4)
    ok(m.st.state == "SIGN" and m.st.paused == "awaiting_signoff",
       f"T_EVIDENCE_OWNER_MODE a lead's signature does not satisfy --signoff owner ({m.st.state}, {m.st.paused})")
    # T_STALE_NO_LEAD (Codex r1 P1-1): a signature made stale by a dossier edit is never replaced by the lead's
    d = os.path.join(td, "ev_stale")
    os.makedirs(d)
    ED.toy_dossier_spec(d)
    toolkit.sign(d, "owner")
    rec0 = open(os.path.join(d, SIGNOFF_FILE), "rb").read()
    open(os.path.join(d, ED.DOSSIER_FILE), "a").write("# edited after signing\n")
    T = StubTasks({})
    m = Machine(d, T, RunState(question="q", state="SIGN", signoff="lead"), backend=toolkit, log=lambda *a: None,
                cross_engine_check=False)
    until(m, n=6)
    ok(m.st.paused == "awaiting_signoff" and "sign_spec" not in T.calls
       and open(os.path.join(d, SIGNOFF_FILE), "rb").read() == rec0,
       f"T_STALE_NO_LEAD the owner's stale record stays and the run waits for the owner ({m.st.state}, {T.calls})")
    # T_LOST_RECORD (Codex r1 P1-3): a run whose history records a signature is not a draft when the record is lost
    os.remove(os.path.join(d, SIGNOFF_FILE))
    st = RunState(question="q", state="EVIDENCE", signoff="lead",
                  history=[{"from": "SIGN", "to": "EVIDENCE", "why": "spec signed by the lead"}])
    T = StubTasks({})
    m = Machine(d, T, st, backend=toolkit, log=lambda *a: None, cross_engine_check=False)
    until(m, n=6)
    ok(m.st.paused == "awaiting_signoff" and T.calls == [],
       f"T_LOST_RECORD a lost sign-off record waits for the owner; nothing is redrafted or re-signed ({T.calls})")
    # T_LEGACY_SIGN (Codex r1 P2-6): an unsigned run saved at SIGN under the earlier order runs the evidence stage
    m, T = setup("ev_legacy", "owner", {"evidence": fill})
    m.st.state = "SIGN"
    m.step()
    ok(m.st.state == "EVIDENCE", f"T_LEGACY_SIGN an unsigned run at SIGN with no evidence record goes to EVIDENCE "
                                 f"({m.st.state})")
    # T_EVIDENCE_JOURNAL (Codex r1 P2-7): the drafts are counted from the journal, not a counter a crash can lose
    m, T = setup("ev_journal", "lead", {})
    m.step()                                    # SPEC -> EVIDENCE: the spec is drafted
    m.st.journal.update({f"EVIDENCE#0:try{i}": {"status": "completed", "ok": True} for i in (1, 2, 3)})
    m.step()
    ok("draft_evidence" not in T.calls and (EDR.read_record(m.run_dir) or {}).get("tries") == 3,
       f"T_EVIDENCE_JOURNAL three completed drafts in the journal: no fourth ({T.calls})")
    # T_SUPPLIED_OWNED (Codex r2 P1-5): a supplied dossier at a path the harness writes goes back to the draft
    m, T = setup("ev_owned", "lead", {})
    m.step()                                    # SPEC -> EVIDENCE
    rawo = yaml.safe_load(open(os.path.join(m.run_dir, AB.SPEC_FILE)))
    rawo["evidence"] = {"dossier": "report.md"}
    yaml.safe_dump(rawo, open(os.path.join(m.run_dir, AB.SPEC_FILE), "w"))
    m.step()
    ok(m.st.state == "SPEC" and any("report.md" in x for x in m.st.tries.get("spec_feedback") or []),
       f"T_SUPPLIED_OWNED a dossier named report.md is refused before anything is written ({m.st.state})")
    # T_SIGN_BUDGET (Codex r2 P2-3): no allowance for the lead's sign task is the budget path, not an owner pause
    m, T = setup("ev_budget", "lead", {"evidence": fill})
    until(m, stop=("SIGN",))
    m._task = lambda *a, **k: None
    m.step()
    ok(m.st.state == "FINAL" and m.st.status == "budget_exhausted",
       f"T_SIGN_BUDGET ({m.st.state}, {m.st.status}, {m.st.paused})")
    # T_DELIVERABLE_DOSSIER (Codex r1 P1-5): the deliverable may not be the dossier
    raw = yaml.safe_load(open(os.path.join(d, AB.SPEC_FILE)))
    for name in (ED.DOSSIER_FILE, "src[1].yaml"):     # r2 P1-1: a name that is not its own glob
        raw["evidence"] = {"dossier": "src[1].yaml"}
        raw["deliverable_file"] = {"path": name, "columns": {"unit": "id"}}
        yaml.safe_dump(raw, open(os.path.join(d, AB.SPEC_FILE), "w"))
        ok(any("owns" in p or "evidence dossier" in p for p in deliverable_problems(d)),
           f"T_DELIVERABLE_DOSSIER a deliverable named {name} is refused ({deliverable_problems(d)})")


def _self_test_claim_refs(ok, td: str, toolkit, new_run) -> None:
    """Harness baseline hb2: CR1: the harness quoted a critic's remedy holding [CLAIM:x] in the report's
    declared-findings block. CR2: the writer copied a declared remedy's placeholder [CLAIM:id]
    into Limitations. CR3: the writer named ledger ids in inline code."""
    import yaml
    from v2_tasks import StubTasks

    def case(name, fn):
        try:
            fn()
        except Exception as e:  # noqa: BLE001 -- before the fix exists this is the failure
            import traceback
            traceback.print_exc()
            ok(False, f"{name}: {type(e).__name__}: {e}")

    def bare_run(name, manuscript):
        d = os.path.join(td, name)
        os.makedirs(os.path.join(d, "models"))
        yaml.safe_dump({"claims": [{"id": "a.good", "claim_kind": "scalar", "value": 5}]},
                       open(os.path.join(d, "models", "claims_ledger.yaml"), "w"))
        yaml.safe_dump({"schema_version": 1}, open(os.path.join(d, AB.SPEC_FILE), "w"))
        open(os.path.join(d, "manuscript.md"), "w").write(manuscript)
        return d

    opened = [{"id": "R-002", "severity": "MEDIUM", "source": "critique-redteam", "tested": True,
               "claim": "The writer then cites them as [CLAIM:id].",
               "fix_requires": "Cite [CLAIM:a.good] as the regret; write `[CLAIM:a.good]` as [CLAIM:id]."}]

    def quoted():
        d = bare_run("cr1", "# Report\n\nIt averts [CLAIM:a.good] cases.\n")
        write_declared(d, opened, {"rule_matched": "3", "rationale": "rounds exhausted"})
        doc = yaml.safe_load(open(os.path.join(d, DECLARED_FILE)))
        doc["declared"][0]["required_remedy"] += " Also [CLAIM:id] and `[CLAIM:a.good]`."   # an older file, unquoted
        yaml.safe_dump(doc, open(os.path.join(d, DECLARED_FILE), "w"))
        render_report(d, toolkit)
        rep = open(os.path.join(d, "report.md")).read()
        kinds = {f["kind"] for f in report_claim_findings(d)}
        ok("[CLAIM:" not in rep and "Open findings, declared" in rep
           and not kinds & {"report_claim_unrendered", "report_claim_unresolved"},
           f"CR1 a finding the harness quotes in report.md shows no claim reference, so no write-up finding comes from "
           f"the harness's own text ({kinds}, {[x for x in rep.splitlines() if 'CLAIM' in x][:2]})")
        d2 = bare_run("cr2", "")
        write_declared(d2, opened, {"rule_matched": "3", "rationale": "rounds exhausted"})
        dec2 = yaml.safe_load(open(os.path.join(d2, DECLARED_FILE)))["declared"][0]
        ok("[CLAIM:id]" not in dec2["claim"] + dec2["required_remedy"] and "[CLAIM:a.good]" in dec2["required_remedy"],
           f"CR2 declared_findings.yaml, which the writer repeats in full, holds no reference outside the ledger; a "
           f"ledger reference stays one ({dec2})")

    def code_ids():
        d = bare_run("cr3", "# Report\n\nThe anchors `a.good` to `[CLAIM:a.good]` and ` a.good `.\n")
        render_report(d, toolkit)
        a, b = json.load(open(os.path.join(d, "report_render.json")))["writer_span"]
        body = open(os.path.join(d, "report.md")).read()[a:b]
        kinds = {f["kind"] for f in report_claim_findings(d)}
        ok(not kinds & {"report_claim_id_raw", "report_claim_unrendered"} and "a.good" not in body
           and "[CLAIM:" not in body,
           f"CR3 a known claim id or reference alone in inline code is the reference: the harness renders it "
           f"({kinds}, {body.splitlines()[-1][:120]!r})")
        for txt, want in (("It averts `[CLAIM:no.such]` cases.\n", "report_claim_unresolved"),
                          ("It averts a.good cases.\n", "report_claim_id_raw"),
                          ("```\n[CLAIM:a.good]\n```\n", "report_claim_unrendered")):
            open(os.path.join(d, "manuscript.md"), "w").write(txt)
            render_report(d, toolkit)
            got = {(f["kind"], f["severity"]) for f in report_claim_findings(d)}
            ok((want, "HIGH") in got, f"CR3b still a HIGH finding (fail closed): {txt.strip()!r} -> {got}")

    case("CR1-CR2", quoted)
    case("CR3", code_ids)

    # Codex round 1 of #353.
    def r1_units():
        k = {"a.good": {"id": "a.good", "claim_kind": "scalar", "value": 5}}
        RCL = __import__("render_claims")
        fenced = "x\n  ```\n  SELECT `a.good` FROM t\n  ```\n"
        ok(RCL.normalise_refs(fenced, k) == fenced, "CR8a an indented fenced block is code: nothing in it is normalised")
        ok(RCL.normalise_refs("Use ``a.good`` here.", k) == "Use [CLAIM:a.good] here.",
           "CR8b a known id in a double-backtick span is its reference too")
        q = RCL.quote_refs("The prefix is `[CLAIM:`. See [source](notes.md).\nNext [CLAIM:a.good] and `a.good`.", k)
        ok("[source](notes.md)" in q and "[CLAIM:" not in q and q.count("5") == 1 and "`a.good`" in q,
           f"CR8c quoting never eats a link's bracket, leaves no marker, renders a reference and leaves a recorded "
           f"identifier as it is ({q!r})")
        # Codex round 2 of #353
        for txt in ("> ```sql\n> SELECT `a.good` FROM t\n> ```\n", "  ````\n  ```\n  SELECT `a.good`\n  ````\n"):
            ok(RCL.normalise_refs(txt, k) == txt, f"CR8d a fence in a block quote, or a longer fence, is code ({txt!r})")
        k2 = dict(k, **{"uq.draws": {"id": "uq.draws", "claim_kind": "scalar", "value": 1000}})
        q15 = RCL.quote_refs("This run continues under amendment `uq.draws`.", k2)
        ok(q15 == "This run continues under amendment `uq.draws`.",
           f"CR15 an identifier the harness records is never replaced by a claim value ({q15!r})")
        q16 = RCL.quote_refs("Fix [CLAIM:[CLAIM:a.good]] now.", k) + RCL.quote_refs("[CLAIM:[CLAIM:a.good]]", k, False)
        ok("[CLAIM:" not in q16.replace("[CLAIM:a.good]", ""),
           f"CR16 a nested marker leaves no reference outside the ledger after quoting ({q16!r})")
        d = bare_run("cr9", "# Report\n\nIt averts [CLAIM:bad] cases.\n")
        yaml.safe_dump({"claims": [{"id": "bad", "claim_kind": "scalar", "value": "lots"}]},
                       open(os.path.join(d, "models", "claims_ledger.yaml"), "w"))
        render_report(d, toolkit)
        kinds = {f["kind"] for f in report_claim_findings(d)}
        ok("report_claim_invalid" in kinds
           and not (kinds & {"report_claim_unresolved", "report_claim_id_raw", "report_claim_unrendered"}),
           f"CR9 a reference to an invalid ledger entry is reported once, as invalid: no writer repair ({kinds})")
        t = StubTasks({"manuscript": "revised\n"})
        dd = bare_run("cr10", "old\n")
        t.write(dd, "accepted", findings=[{"id": "report_claim_id_raw:a.good", "claim": "c"}])
        ok("revised" in open(os.path.join(dd, "manuscript.md")).read(),
           "CR10 an ordinary WRITE visit with claim findings writes the draft; only a marked repair keeps it")

    case("CR8-CR10", r1_units)

    def units3():
        RCL = __import__("render_claims")
        k = {"a.good": {"id": "a.good", "claim_kind": "scalar", "value": 5},
             "note": {"id": "note", "claim_kind": "label", "value": "See [CLAIM:note]"}}
        lf = "- ```sql\n  SELECT `a.good` FROM t\n  ```\n\nThen `a.good`.\n"
        got = RCL.normalise_refs(lf, k)
        ok(got == lf.replace("Then `a.good`", "Then [CLAIM:a.good]"),
           f"CR21 a fence opened after a list marker is code, and the prose after it is still read ({got!r})")
        q = RCL.quote_refs("[CLAIM:note]", k)
        ok(q.upper().count("SEE") == 1 and "[CLAIM:" not in q, f"CR22 a rendered value is never substituted again ({q!r})")
    case("CR21-CR22", units3)


def _self_test_hb2_spec(ok, td: str, toolkit, new_run) -> None:
    """HS1-HS7 (baseline hb1, 2026-11-22): a SPEC try that returns no draft (the 30-turn cap) replaced the
    validation feedback, so the next try never saw the error and repeated it (tb R1, run_failed at the try cap); the
    checks stopped at the first class that failed, so each try heard one class of problem (HS4-HS6); the drafter's
    prompt did not state the two rules that cost retries in measles and dengue (HS7)."""
    from v2_tasks import StubTasks

    class Scripted(StubTasks):
        """script["tries"]: per try, "bad" (an invalid draft), "good" (a valid one), "capped" (no draft written,
        the call fails) or "capped_good" (a valid draft written, then the call fails, as at the turn cap)."""
        def draft_spec(self, q, rd, fb):
            self.fbs = getattr(self, "fbs", []) + [list(fb or [])]
            kind = (self.s["tries"] + ["capped"] * 9)[len(self.fbs) - 1]
            if kind == "capped":
                return self._r("draft_spec", False, note="max turns")
            spec = dict(self.s["spec"])
            if kind in ("bad", "bad2"):
                spec["decision_units"] = dict(spec["decision_units"], csv="missing_units_table.csv")
            if kind in ("bad2", "noobj"):
                spec.pop("objective", None)
            if kind == "outside":
                spec["decision_units"] = dict(spec["decision_units"], csv="../outside_units.csv")
            if kind == "settings_capped":       # repairs only the settings, then hits the cap
                from v2_tasks import _yaml_dump
                _yaml_dump(self.s["settings"], os.path.join(rd, "analysis_settings.yaml"), rd)
                return self._r("draft_spec", False, note="max turns")
            settings = dict(self.s["settings"], bogus_key=1) if kind == "bad_settings" else self.s["settings"]
            r = StubTasks(dict(self.s, spec=spec, settings=settings)).draft_spec(q, rd, fb)
            return self._r("draft_spec", False, note="max turns") if kind == "capped_good" else r

    def go(name, tries, cls=None):
        d, T, st = new_run(name, {})
        T = Scripted(dict(T.s, tries=tries))
        m = (cls or Machine)(d, T, st, backend=toolkit, log=lambda *a: None, cross_engine_check=False)
        for _ in range(len(tries) + 1):
            if m.st.state != "SPEC" or m.st.paused or m.st.status:
                break
            m.step()
        return m, T

    m, T = go("hs1", ["bad", "capped", "bad"])
    first = [x for x in T.fbs[1] if "missing_units_table" in x]
    ok(len(T.fbs) == 3 and first and any("missing_units_table" in x for x in T.fbs[2])
       and any("max turns" in x for x in T.fbs[2]),
       f"HS1 a try that returns no draft keeps the earlier validation feedback, and adds its own note ({T.fbs})")
    ok(sum("missing_units_table" in x for x in T.fbs[2]) == len(first),
       f"HS1b feedback carried from an earlier try is not repeated ({T.fbs[2]})")
    m, T = go("hs2", ["capped_good"])
    ok(m.st.state != "SPEC" and m.st.tries.get("spec") == 1,
       f"HS2 a valid draft written before the call failed (the turn cap) is validated and used ({m.st.state}, "
       f"{m.st.tries.get('spec_feedback')})")
    # HS4: one try hears every check class (a missing cited file and a missing section), not only the first
    m, T = go("hs4", ["bad2", "good"])
    ok(len(T.fbs) >= 2 and any("missing_units_table" in x for x in T.fbs[1])
       and any("missing section objective" in x for x in T.fbs[1]),
       f"HS4 one SPEC try reports the cited-input and the pre-sign problems together ({T.fbs[1:2]})")

    # HS5: a check class that raises is reported by name, and the other classes' problems are kept
    class GeoBoom(Machine):
        def _geometry_problems(self, rd=None):
            raise RuntimeError("geometry boom")
    m, T = go("hs5", ["bad", "good"], GeoBoom)
    ok(len(T.fbs) >= 2 and any("missing_units_table" in x for x in T.fbs[1])
       and any("geometry boom" in x for x in T.fbs[1]),
       f"HS5 a check that raises is named and does not hide the others' problems ({T.fbs[1:2]})")

    # HS6: the evidence stage's spec problems (decision inputs with no dossier named) are heard at SPEC, in the same
    # try, not one stage later (tb R2: EVIDENCE sent the spec back for 7 missing decision inputs)
    from toolkit.spec import evidence_dossier as EDR
    real = EDR.assess
    try:
        EDR.assess = lambda rd, raw=None: dict(real(rd, raw), spec_problems=["the options need 7 decision input(s)"])
        m, T = go("hs6", ["good", "good"])
    finally:
        EDR.assess = real
    ok(len(T.fbs) >= 2 and any("7 decision input" in x for x in T.fbs[1]) and m.st.tries.get("spec") >= 2,
       f"HS6 the evidence stage's spec problems come back from the SPEC try ({T.fbs[1:2]}, {m.st.state})")

    # HS7: the drafter's prompt states the calibrated/fitted engine_parameters rule; a fitted parameter is never moved
    # under calibration (Codex r1 P2-7: calibration accepts kind calibrated only)
    from v2_sdk_tasks import PROMPTS
    pr = PROMPTS["draft_spec"]
    ok("engine_parameters" in pr and "calibrated" in pr and "a fitted one" in pr,
       "HS7 the draft_spec prompt says: no engine_parameters value for a calibrated or fitted parameter "
       "(a fitted one is left to the engine)")

    class Calls(Machine):
        """Records which reading checks ran."""
        def _geometry_problems(self, rd=None):
            self.ran = getattr(self, "ran", []) + ["geometry"]
            return super()._geometry_problems(rd)

        def _evidence_assess(self, rd):
            self.ran = getattr(self, "ran", []) + ["evidence"]
            return super()._evidence_assess(rd)
    # HS8 (Codex r1 P1-1): a cited file outside the run is refused, and no check reads it
    AB.write_toy_units(os.path.join(td, "outside_units.csv"), n=12)
    m, T = go("hs8", ["outside"], Calls)
    fb8 = m.st.tries.get("spec_feedback") or []
    ok(any("outside the run directory" in x for x in fb8) and not getattr(m, "ran", []),
       f"HS8 a cited file outside the run is refused and no later check reads it ({getattr(m, 'ran', [])}, {fb8})")
    # HS9 (Codex r1 P1-2): the evidence assessment (it builds the engine) runs only on a spec the pre-sign checks pass
    m, T = go("hs9", ["noobj"], Calls)
    ok("evidence" not in getattr(m, "ran", []) and any("objective" in x for x in m.st.tries.get("spec_feedback") or []),
       f"HS9 a spec the pre-sign checks refuse is not given to the evidence assessment ({getattr(m, 'ran', [])})")

    # HS10 (Codex r1 P1-3): interrupted after the task's journal entry completed, a resume reaches the same decision
    class Crash(Exception):
        pass

    class CrashOnce(Machine):
        def _task(self, *a, **k):
            r = super()._task(*a, **k)
            if not getattr(self, "crashed", False):
                self.crashed = True
                save_state(self.run_dir, self.st)
                raise Crash()
            return r
    d10, T10, st10 = new_run("hs10", {})
    T10 = Scripted(dict(T10.s, tries=["capped_good"]))
    m = CrashOnce(d10, T10, st10, backend=toolkit, log=lambda *a: None, cross_engine_check=False)
    try:
        m.step()
    except Crash:
        pass
    m2 = Machine(d10, T10, load_state(d10), backend=toolkit, log=lambda *a: None, cross_engine_check=False)
    m2.step()
    ok(m2.st.state == "EVIDENCE" and len(T10.fbs) == 1,
       f"HS10 a resume after the completed task replays it and still checks the draft it wrote ({m2.st.state}, "
       f"{m2.st.tries.get('spec_feedback')})")
    # HS11 (Codex r1 P1-4): a capped try that repaired only the settings has its draft checked again
    m, T = go("hs11", ["bad_settings", "settings_capped"])
    ok(any("bogus_key" in x for x in T.fbs[1]) and m.st.state == "EVIDENCE",
       f"HS11 a capped try that changed only the settings is checked, and goes on ({m.st.state}, "
       f"{m.st.tries.get('spec_feedback')})")
    # HS14 (Codex r2 P1-2): when containment of the cited paths cannot be established, that is a problem, never a pass
    import pipeline as _P
    real_cp, n_cp = _P.cited_paths, []

    def flaky(*a, **k):
        n_cp.append(1)
        if len(n_cp) >= 2:
            raise OSError("listing failed")
        return real_cp(*a, **k)
    try:
        _P.cited_paths = flaky
        m, T = go("hs14", ["good"])
    finally:
        _P.cited_paths = real_cp
    fb14 = m.st.tries.get("spec_feedback") or []
    ok(m.st.state == "SPEC" and any("listing failed" in x for x in fb14),
       f"HS14 a citation listing that fails is a problem, not a pass ({m.st.state}, {fb14})")
    # HS15 (Codex r2 P1-5/P1-7/P2-8): a try with no completion output is checked on whatever draft files exist, with no
    # comparison of the files before and after (a resume, a byte-identical rewrite or an odd file never skips it)
    d15, T15, st15 = new_run("hs15", {})
    T15 = Scripted(dict(T15.s, tries=["good"]))
    StubTasks(T15.s).draft_spec("q", d15, None)        # a valid draft already on disk (an earlier, older journal)
    T15 = Scripted(dict(T15.s, tries=["capped"]))
    m = Machine(d15, T15, st15, backend=toolkit, log=lambda *a: None, cross_engine_check=False)
    m.step()
    ok(m.st.state == "EVIDENCE", f"HS15 a capped try leaves a valid draft on disk: it is checked and used, whatever "
                                 f"changed during the try ({m.st.state}, {m.st.tries.get('spec_feedback')})")
    # HS16 (Codex r3 P1-1): the signed-configuration guard's problems go back to the drafter, whatever the backend
    real_sc = AB.signed_config_problems
    try:
        AB.signed_config_problems = lambda spec: ["engine.config: more components than the engine's limit"]
        m, T = go("hs16", ["good"])
    finally:
        AB.signed_config_problems = real_sc
    ok(m.st.state == "SPEC" and any("engine's limit" in x for x in m.st.tries.get("spec_feedback") or []),
       f"HS16 the configuration guard's refusal reaches the drafter ({m.st.state}, {m.st.tries.get('spec_feedback')})")
    # HS17 (Codex r3 P1-2): feedback the pre-sign checks cannot verify (a rethink's reasons) is never cleared by a
    # capped try whose old draft passes those checks; the reasons are carried to the next try
    d17, T17, st17 = new_run("hs17", {})
    StubTasks(T17.s).draft_spec("q", d17, None)          # the earlier, valid draft
    st17.tries["spec_feedback"] = ["rethink after review round 1: the comparator answers another question"]
    save_state(d17, st17)
    T17 = Scripted(dict(T17.s, tries=["capped"]))
    m = Machine(d17, T17, st17, backend=toolkit, log=lambda *a: None, cross_engine_check=False)
    m.step()
    ok(m.st.state == "SPEC" and any("comparator" in x for x in m.st.tries.get("spec_feedback") or []),
       f"HS17 a capped try after a rethink keeps the rethink's reasons and does not pass on the old draft "
       f"({m.st.state}, {m.st.tries.get('spec_feedback')})")
    # HS12 (Codex r1 P2-6): the cap on the feedback never drops this try's own problems
    many = [f"problem {i}" for i in range(45)]
    got12 = _carry_feedback(many, ["old one"])
    ok(all(x in got12 for x in many), f"HS12 every current problem reaches the next try ({len(got12)})")

    from types import SimpleNamespace as NS
    from v2_sdk_tasks import SPEC_MAX_TURNS, SdkTasks
    got = []

    def q(prompt=None, options=None):
        async def gen():
            got.append(options.max_turns)
            yield NS(total_cost_usd=0.0, session_id="hs3", is_error=False)
        return gen()
    hd = os.path.join(td, "hs3")
    os.makedirs(hd)
    t = SdkTasks(hd, query_fn=q)
    t.set_allowance(1.0)
    t._session("draft_spec", "x", "y", ())
    t._session("pick_engine", "x", "y", ())
    ok(got == [SPEC_MAX_TURNS, 30] and SPEC_MAX_TURNS > 30,
       f"HS3 the spec draft has its own turn cap, above the 30 of the other tasks ({got})")
    # HS13 (Codex r1 P1-5): an explicit cap is honoured by every task, the spec draft included
    got.clear()
    t5 = SdkTasks(hd, max_turns=5, query_fn=q)
    t5.set_allowance(1.0)
    t5._session("draft_spec", "x", "y", ())
    ok(got == [5], f"HS13 a caller's explicit turn cap holds for the spec draft ({got})")


def _self_test_spec_p1s(ok, td: str, toolkit, new_run) -> None:
    """SP1-SP4 (Codex round 4 on #348, on code merged through #342): a capped try with no draft must not stop later
    capped tries' drafts from being checked; a cited input that is not a regular file (a FIFO) is never opened by a later
    check; the spec and its settings are refused as links or hard links before any check reads them; an explicit zero
    turn cap is refused (it would remove the session's limit)."""
    from v2_tasks import StubTasks, TaskResult

    class Capped(StubTasks):
        """Per try: "none" (no draft, capped) or "good" (a valid draft written, then capped)."""
        def draft_spec(self, q, rd, fb):
            self.n_ = getattr(self, "n_", 0) + 1
            if self.s["tries"][self.n_ - 1] == "good":
                StubTasks(self.s).draft_spec(q, rd, fb)
            self.calls.append("draft_spec")
            return TaskResult(False, None, 0.0, "max turns")
    d1, T1, st1 = new_run("sp1", {})
    T1 = Capped(dict(T1.s, tries=["none", "good", "good"]))
    m = Machine(d1, T1, st1, backend=toolkit, log=lambda *a: None, cross_engine_check=False)
    for _ in range(3):
        if m.st.state != "SPEC" or m.st.status:
            break
        m.step()
    ok(m.st.state == "EVIDENCE", f"SP1 a first capped try with no draft does not stop the next capped try's valid draft "
                                 f"from being checked ({m.st.state}, {m.st.status}, {m.st.tries.get('spec_feedback')})")

    class Spy(Machine):
        def _geometry_problems(self, rd=None):
            self.ran = getattr(self, "ran", []) + ["geometry"]
            return []

        def _evidence_assess(self, rd):
            self.ran = getattr(self, "ran", []) + ["evidence"]
            return super()._evidence_assess(rd)
    # SP3: a cited FIFO inside the run is refused and no later check opens it (it would block with no writer)
    d3, T3, st3 = new_run("sp3", {})
    m3 = Spy(d3, T3, st3, backend=toolkit, log=lambda *a: None, cross_engine_check=False)
    spec3 = dict(T3.s["spec"], decision_units=dict(T3.s["spec"]["decision_units"], csv="units_fifo.csv"))
    StubTasks(dict(T3.s, spec=spec3)).draft_spec("q", d3, None)
    os.mkfifo(os.path.join(d3, "units_fifo.csv"))
    got3 = m3._spec_checks(d3)
    ok(any("units_fifo.csv" in x and "regular file" in x for x in got3) and not getattr(m3, "ran", []),
       f"SP3 a cited FIFO is refused and never opened by a later check ({getattr(m3, 'ran', [])}, {got3})")
    # SP4: the spec and analysis_settings.yaml are refused as a symlink or a hard link, before any check reads them
    marker = os.path.join(td, "sp4_fixture.yaml")
    open(marker, "w").write("fixture_marker: 1\n")
    res4 = {}
    for name, rel, how in (("settings link", "analysis_settings.yaml", os.symlink),
                           ("settings hard link", "analysis_settings.yaml", os.link),
                           ("spec hard link", AB.SPEC_FILE, os.link)):
        d4, T4, st4 = new_run("sp4_" + name.replace(" ", "_"), {})
        T4.draft_spec("q", d4, None)
        os.remove(os.path.join(d4, rel))
        how(marker, os.path.join(d4, rel))
        m4 = Spy(d4, T4, st4, backend=toolkit, log=lambda *a: None, cross_engine_check=False)
        got4 = m4._spec_checks(d4)
        res4[name] = bool(any(rel in x and "link" in x for x in got4) and not any("fixture_marker" in x for x in got4)
                          and not getattr(m4, "ran", []))
    ok(all(res4.values()), f"SP4 a linked spec or settings file is refused before any check reads it ({res4})")
    # SP5 (Codex r1 P1-1): through h_SPEC, a hard-linked spec is refused before the conflicts default rewrites it
    d5, T5, st5 = new_run("sp5", {})

    class Linking(StubTasks):
        def draft_spec(self, q, rd, fb):
            r = StubTasks(self.s).draft_spec(q, rd, fb)
            os.link(os.path.join(rd, AB.SPEC_FILE), os.path.join(td, "sp5_other_name.yaml"))
            return r
    m5 = Machine(d5, Linking(T5.s), st5, backend=toolkit, log=lambda *a: None, cross_engine_check=False)
    m5.step()
    ok(m5.st.state == "SPEC" and any("hard links" in x for x in m5.st.tries.get("spec_feedback") or []),
       f"SP5 a hard-linked spec is refused at the try's end, before the conflicts default is applied ({m5.st.state}, "
       f"{m5.st.tries.get('spec_feedback')})")

    # SP6 (Codex r1 P1-2/P1-3): a FIFO anywhere in the run, whatever path names it (a staged input a derivation reads,
    # a name with a leading space), is refused before derivations or any check runs
    class SpyAll(Spy):
        def _derive(self, rd=None):
            self.ran = getattr(self, "ran", []) + ["derive"]
            return []
    res6 = {}
    for name, rel, cite in (("staged input", "data/inputs/t.csv", None), ("spaced name", " units.csv", " units.csv")):
        d6, T6, st6 = new_run("sp6_" + name.replace(" ", "_"), {})
        spec6 = T6.s["spec"] if cite is None else dict(
            T6.s["spec"], decision_units=dict(T6.s["spec"]["decision_units"], csv=cite))
        StubTasks(dict(T6.s, spec=spec6)).draft_spec("q", d6, None)
        os.makedirs(os.path.dirname(os.path.join(d6, rel)), exist_ok=True)
        os.mkfifo(os.path.join(d6, rel))
        m6 = SpyAll(d6, T6, st6, backend=toolkit, log=lambda *a: None, cross_engine_check=False)
        m6.st.derived_inputs = True
        got6 = m6._spec_checks(d6)
        res6[name] = bool(any("regular file" in x for x in got6) and not getattr(m6, "ran", []))
    ok(all(res6.values()), f"SP6 a FIFO in the run is refused before derivations or any reading check ({res6})")
    # SP7 (Codex r1 P1-5): a run saved before this fix (feedback "draft_spec failed: ...", no checked copy) still has a
    # later capped try's valid draft checked
    d7, T7, st7 = new_run("sp7", {})
    st7.tries.update(spec=1, spec_feedback=["draft_spec failed: max turns"])
    save_state(d7, st7)
    T7 = Capped(dict(T7.s, tries=["good"]))
    m7 = Machine(d7, T7, st7, backend=toolkit, log=lambda *a: None, cross_engine_check=False)
    m7.step()
    ok(m7.st.state == "EVIDENCE", f"SP7 a capped try after an earlier no-draft note is checked ({m7.st.state}, "
                                  f"{m7.st.tries.get('spec_feedback')})")
    # SP2: an explicit zero turn cap is refused, never passed on as "no limit"
    from v2_sdk_tasks import SdkTasks
    try:
        SdkTasks(os.path.join(td, "sp2"), max_turns=0)
        refused = False
    except ValueError:
        refused = True
    ok(refused, "SP2 SdkTasks(max_turns=0) is refused: the SDK reads 0 as no turn limit")


def _self_test_acquire(ok, td: str) -> None:
    """All 14 stage-machine ACQUIRE self-tests (docs/design/acquire.md §9.1):
    T_ACQ_OFF, T_ACQ_OFF_GOLDEN, T_ACQ_SUPPLIED, T_ACQ_PROMOTE, T_ACQ_EMPTY, T_ACQ_TOPUP,
    T_ACQ_CLOSED, T_ACQ_TAMPER_PRESPEC, T_ACQ_BALANCE, T_ACQ_OLD_STATE, T_ACQ_SIGN,
    T_ACQ_QUOTE, T_ACQ_DELIVERABLE, T_ACQ_AMEND."""
    import shutil
    import yaml
    from agents import acquire as ACQ
    from agents import v2_sdk_tasks as VSDK
    from v2_tasks import StubTasks, TaskResult
    from toolkit.spec import amend as AM, evidence_dossier as ED, preconditions as PC, schema, signoff as SO

    # T_ACQ_OFF: default acquire="off" starts at SPEC and history never names ACQUIRE
    d_off = os.path.join(td, "t_acq_off")
    st_off = init_run(d_off, "Bare question?", signoff="none", derived_inputs=False)
    meta_off = json.load(open(os.path.join(d_off, "metadata.json")))
    m_off = Machine(d_off, StubTasks({}), st_off, log=lambda *a: None, cross_engine_check=False)
    m_off.step()
    ok(st_off.acquire == "off" and meta_off.get("acquire") == "off"
       and not any(h.get("from") == "ACQUIRE" or h.get("to") == "ACQUIRE" for h in m_off.st.history),
       f"T_ACQ_OFF acquire='off' starts at SPEC and history never names ACQUIRE ({m_off.st.history[:2]})")

    # T_ACQ_OFF_GOLDEN (§8): off-path provenance has no origin/acquired_inventory_sha256, draft_spec has no
    # manifest sentence, TOOLS is unchanged, and history never names ACQUIRE
    d_gold = os.path.join(td, "t_acq_gold")
    st_gold = init_run(d_gold, "Use requirements.txt for allocation.", signoff="none",
                       derived_inputs=False)
    staged_g = stage_inputs(d_gold, st_gold.question)
    prov_g = json.load(open(os.path.join(d_gold, PROVENANCE)))
    gold_prompts = {}
    orig_sess = VSDK.SdkTasks._session

    def _cap_sess(self_, kind, system, prompt, owns, model=None, **kw):
        gold_prompts[kind] = (system, prompt, owns)
        return TaskResult(True, {"ok": True}, 0.0, "fake")

    VSDK.SdkTasks._session = _cap_sess
    try:
        VSDK.SdkTasks(d_gold).draft_spec(st_gold.question, d_gold, None)
    finally:
        VSDK.SdkTasks._session = orig_sess
    ok(bool(staged_g) and "acquired_inventory_sha256" not in prov_g
       and all("origin" not in v for v in (prov_g.get("inputs") or {}).values())
       and "acquired_manifest.json" not in (gold_prompts.get("draft_spec", ("", "", ()))[1])
       and VSDK.TOOLS == ["Read", "Glob", "Grep", "Write", "Edit"]
       and st_gold.state == "SPEC",
       f"T_ACQ_OFF_GOLDEN off-path provenance, prompt, tools, and state are unchanged ({list(prov_g.keys())})")

    # T_ACQ_SUPPLIED: acquire="auto" with a question naming a repo file skips ACQUIRE at $0 and closes back-edges
    d_sup = os.path.join(td, "t_acq_sup")
    st_sup = init_run(d_sup, "Allocate using requirements.txt.", signoff="none", acquire="auto",
                      derived_inputs=False)
    T_sup = StubTasks({"usd": 1.0})
    m_sup = Machine(d_sup, T_sup, st_sup, log=lambda *a: None, cross_engine_check=False)
    nxt_sup = m_sup.step()
    ok(nxt_sup == "SPEC" and not T_sup.calls and m_sup.st.spent_usd == 0.0
       and m_sup.st.acquire == "off"
       and (m_sup.st.acquired or {}).get("status") == "skipped_question_supplies_inputs"
       and m_sup._acquire_closed(),
       f"T_ACQ_SUPPLIED question supplying files skips ACQUIRE at $0 and closes back-edges ({nxt_sup}, {m_sup.st.acquired})")

    # Fixture server for T_ACQ_PROMOTE, T_ACQ_EMPTY, T_ACQ_TOPUP, T_ACQ_CLOSED, T_ACQ_TAMPER_PRESPEC,
    # T_ACQ_BALANCE, T_ACQ_SIGN, T_ACQ_QUOTE, T_ACQ_AMEND
    routes = {
        "/api/catalogue": (200, "application/json", json.dumps([{"code": "IND1"}, {"code": "IND2"}]).encode("utf-8"), {}),
        "/api/slice/IND1?mode=csv": (200, "text/csv", b"unit,pop\nU1,1000\nU2,2000\n", {}),
        "/api/slice/IND2?mode=csv": (200, "text/csv", b"unit,inc\nU1,0.12\nU2,0.25\n", {}),
        "/api/slice/IND1?mode=xml_ok": (
            200, "application/xml",
            b"<article><body><p>Trial result showed efficacy 0.65 (95% CI 0.50-0.78) in 500 sites.</p></body></article>",
            {},
        ),
    }
    with ACQ.AcquireFixtureServer(routes) as srv:
        al_p = os.path.join(td, "acq_sm_allowlist.yaml")
        yaml.safe_dump(ACQ._test_allowlist_yaml(srv.port), open(al_p, "w"), sort_keys=False)

        def _fetch_keep(rd, code="IND1", mode="csv", wanted=None):
            F = ACQ.Fetcher(rd, al_p, resolver=lambda h, p: ["127.0.0.1"],
                            _allow_test_http=True, _test_port=srv.port, _allow_loopback_ip=True)
            F.search("svc_data", "catalogue", {})
            fid = F.fetch("svc_data", "slice", {"code": code, "mode": mode})["fetch_id"]
            return {"keep": [fid], "wanted_map": {fid: wanted or ["population"]}}

        # T_ACQ_PROMOTE: fixture fetch -> promoted files, manifest, views, SPEC prompt sees manifest
        d_pr = os.path.join(td, "t_acq_promote")
        st_pr = init_run(d_pr, "Bare policy question.", signoff="none", acquire="auto",
                         derived_inputs=False)
        T_pr = StubTasks({"acquire": lambda q, rd, pkt: _fetch_keep(rd, "IND1", "csv", ["population"])})
        m_pr = Machine(d_pr, T_pr, st_pr, log=lambda *a: None, cross_engine_check=False)
        m_pr.acquire_allowlist = al_p
        nxt_pr = m_pr.step()
        pr_prompts = {}
        VSDK.SdkTasks._session = lambda self_, k, s, p, o, model=None, **kw: (
            pr_prompts.__setitem__(k, p) or TaskResult(True, {"ok": True}, 0.0, "fake")
        )
        try:
            VSDK.SdkTasks(d_pr).draft_spec(st_pr.question, d_pr, None)
        finally:
            VSDK.SdkTasks._session = orig_sess
        views_pr = os.listdir(os.path.join(d_pr, ACQ.VIEWS_ROOT)) if os.path.isdir(os.path.join(d_pr, ACQ.VIEWS_ROOT)) else []
        ok(nxt_pr == "SPEC" and os.path.isfile(os.path.join(d_pr, ACQ.MANIFEST_REL)) and bool(views_pr)
           and "evidence/acquired_manifest.json" in pr_prompts.get("draft_spec", "")
           and "data/inputs/acquired/_views/" in pr_prompts.get("draft_spec", ""),
           f"T_ACQ_PROMOTE promotes files, writes manifest & views, and SPEC receives them ({nxt_pr}, {views_pr})")

        # T_ACQ_EMPTY: nothing promoted -> paused:awaiting_inputs and acquire/inputs_request.md written
        d_em = os.path.join(td, "t_acq_empty")
        st_em = init_run(d_em, "Bare question.", signoff="owner", acquire="auto",
                         derived_inputs=False)
        T_em = StubTasks({"acquire": {"keep": [], "wanted_map": {}}})
        m_em = Machine(d_em, T_em, st_em, log=lambda *a: None, cross_engine_check=False)
        m_em.acquire_allowlist = al_p
        res_em = m_em.run()
        ok(res_em == f"paused:{AWAITING_INPUTS}"
           and os.path.isfile(os.path.join(d_em, "acquire", "inputs_request.md"))
           and "Wanted kinds" in open(os.path.join(d_em, "acquire", "inputs_request.md")).read(),
           f"T_ACQ_EMPTY nothing promoted pauses awaiting_inputs and writes inputs_request.md ({res_em})")

        # T_ACQ_TOPUP: missing_input after 3rd SPEC try goes to ACQUIRE once, grants 4th SPEC try; 2nd gap does not go back
        d_tu = os.path.join(td, "t_acq_topup")
        st_tu = init_run(d_tu, "Bare question.", signoff="none", acquire="auto",
                         derived_inputs=False)
        acq_rounds_seen = []

        def _acq_tu(q, rd, pkt):
            acq_rounds_seen.append(pkt.get("round"))
            return _fetch_keep(rd, "IND1" if pkt.get("round") == 1 else "IND2", "csv",
                               ["population"] if pkt.get("round") == 1 else ["burden"])

        T_tu = StubTasks({"acquire": _acq_tu, "spec": {"schema_version": 1}})

        class _TopupMachine(Machine):
            def _spec_checks(self, rd):
                tr = int(self.st.tries.get("spec") or 0)
                if tr in (1, 2):
                    return ["plain spec error"]
                if tr == 3:
                    return ["missing_input: burden:incidence"]
                return ["missing_input: coverage:second_gap"]

        m_tu = _TopupMachine(d_tu, T_tu, st_tu, log=lambda *a: None, cross_engine_check=False)
        m_tu.acquire_allowlist = al_p
        res_tu = m_tu.run(max_steps=20)
        ok(res_tu == "run_failed" and acq_rounds_seen == [1, 2]
           and m_tu.st.tries.get("spec") == MAX_SPEC_TRIES + 1 and m_tu.st.tries.get("spec_bonus") == 1
           and int((m_tu.st.acquired or {}).get("rounds") or 0) == MAX_ACQUIRE_ROUNDS,
           f"T_ACQ_TOPUP missing_input after 3rd SPEC try tops up once and grants bonus try; 2nd gap does not loop "
           f"(res={res_tu}, acq_rounds={acq_rounds_seen}, tries={m_tu.st.tries})")

        # T_ACQ_CLOSED: --signoff none closes acquisition on SIGN -> ENGINE_CHOICE; later calibration refusal to SPEC
        # never reaches ACQUIRE; restored state past SIGN without closed flag is treated as closed
        d_cl = os.path.join(td, "t_acq_closed")
        st_cl = init_run(d_cl, "Bare question.", signoff="none", acquire="auto",
                         derived_inputs=False)
        T_cl = StubTasks({"acquire": lambda q, rd, pkt: _fetch_keep(rd, "IND1", "csv"), "spec": {"schema_version": 1}})

        class _ClosedMachine(Machine):
            def _spec_checks(self, rd):
                return ["missing_input: burden:after_sign"]

        m_cl = _ClosedMachine(d_cl, T_cl, st_cl, log=lambda *a: None, cross_engine_check=False)
        m_cl.acquire_allowlist = al_p
        m_cl.step()  # ACQUIRE -> SPEC (round 1 promoted)
        # Simulate reaching SIGN -> ENGINE_CHOICE under --signoff none
        _write_json(d_cl, "evidence/dossier_draft.json", {"status": "not_required", "required": [], "tries": 0})
        yaml.safe_dump({"schema_version": 1}, open(os.path.join(d_cl, AB.SPEC_FILE), "w"))
        m_cl.st.state = "SIGN"
        nxt_ec = m_cl.step()
        closed_after_sign = bool((m_cl.st.acquired or {}).get("closed"))
        # Simulate ANALYZE calibration refusal back to SPEC
        m_cl.st.state = "SPEC"
        m_cl.st.tries["spec"] = 0
        nxt_after_ref = m_cl.step()
        # Part B: restored state past SIGN missing closed flag
        d_cl2 = os.path.join(td, "t_acq_closed_restored")
        st_cl2 = init_run(d_cl2, "Bare question.", signoff="none", acquire="auto",
                          derived_inputs=False)
        st_cl2.acquired = {"rounds": 1}  # deliberately omit closed flag
        st_cl2.history.append({"at": _now(), "from": "SIGN", "to": "ENGINE_CHOICE", "why": "sign-off none"})
        st_cl2.state = "ACQUIRE"
        save_state(d_cl2, st_cl2)
        m_cl2 = Machine(d_cl2, StubTasks({}), None, log=lambda *a: None, cross_engine_check=False)
        nxt_cl2 = m_cl2.step()
        ok(nxt_ec == "ENGINE_CHOICE" and closed_after_sign and nxt_after_ref == "SPEC"
           and m_cl2._acquire_closed() and nxt_cl2 == "FINAL" and m_cl2.st.status == "run_failed",
           f"T_ACQ_CLOSED --signoff none closes acquisition at SIGN and restored state past SIGN stays closed "
           f"({nxt_ec}, closed={closed_after_sign}, after_ref={nxt_after_ref}, restored={nxt_cl2}/{m_cl2.st.status})")

        # T_ACQ_TAMPER_PRESPEC: tampering promoted file between ACQUIRE and SPEC -> artifact_tampered before draft_spec
        d_tp = os.path.join(td, "t_acq_tamper")
        st_tp = init_run(d_tp, "Bare question.", signoff="none", acquire="auto",
                         derived_inputs=False)
        T_tp = StubTasks({"acquire": lambda q, rd, pkt: _fetch_keep(rd, "IND1", "csv"), "spec": {"schema_version": 1}})
        m_tp = Machine(d_tp, T_tp, st_tp, log=lambda *a: None, cross_engine_check=False)
        m_tp.acquire_allowlist = al_p
        m_tp.step()  # ACQUIRE -> SPEC
        prov_tp = json.load(open(os.path.join(d_tp, PROVENANCE)))
        acq_p = next(p for p, e in prov_tp["inputs"].items() if isinstance(e, dict) and e.get("origin") == "acquired")
        abs_tp = os.path.join(d_tp, acq_p)
        os.chmod(abs_tp, 0o644)
        open(abs_tp, "ab").write(b"TAMPER\n")
        os.chmod(abs_tp, 0o444)
        m_tp.step()
        ok(m_tp.st.state == "FINAL" and m_tp.st.status == "artifact_tampered" and "draft_spec" not in T_tp.calls,
           f"T_ACQ_TAMPER_PRESPEC tampered acquired file stops artifact_tampered before draft_spec "
           f"({m_tp.st.state}/{m_tp.st.status}, calls={T_tp.calls})")

        # T_ACQ_BALANCE: interrupted call + retry + top-up + read-backs cannot exceed ACQUIRE_CAP_USD ($6)
        d_bal = os.path.join(td, "t_acq_balance")
        st_bal = init_run(d_bal, "Bare question.", signoff="none", acquire="auto", budget_usd=50.0,
                          derived_inputs=False)
        st_bal.journal["ACQUIRE#r1:a0"] = {
            "task": "acquire", "status": "started", "allowance_usd": 3.0, "readback_usd": 0.25, "acquire_budget": True,
        }
        save_state(d_bal, st_bal)
        T_bal = StubTasks({"acquire": lambda q, rd, pkt: _fetch_keep(rd, "IND1", "csv"), "usd": 2.50})
        m_bal = Machine(d_bal, T_bal, None, log=lambda *a: None, cross_engine_check=False)
        m_bal.acquire_allowlist = al_p
        after_int = float((m_bal.st.acquired or {}).get("spent_usd") or 0.0)
        m_bal.step()  # retry runs with allowance <= 2.75, spends 2.50 -> total 5.75
        after_retry = float((m_bal.st.acquired or {}).get("spent_usd") or 0.0)
        # Attempt top-up round 2 with only $0.25 left (< $0.50 floor) -> must pause awaiting_budget
        m_bal.st.state = "ACQUIRE"
        res_bal2 = m_bal.run()
        ok(abs(after_int - 3.25) < 1e-6 and T_bal.allowances and T_bal.allowances[0] <= 2.75 + 1e-9
           and abs(after_retry - 5.75) < 1e-6 and after_retry <= ACQUIRE_CAP_USD
           and res_bal2 == f"paused:{AWAITING_BUDGET}"
           and float((m_bal.st.acquired or {}).get("spent_usd") or 0.0) <= ACQUIRE_CAP_USD,
           f"T_ACQ_BALANCE interrupted + retry + top-up + read-backs bounded by ACQUIRE_CAP_USD "
           f"(int={after_int}, retry={after_retry}, allow={T_bal.allowances}, res2={res_bal2})")

        # T_ACQ_OLD_STATE: run_state.json without acquire/acquired loads as off and resumes cleanly
        d_old = os.path.join(td, "t_acq_old")
        os.makedirs(d_old)
        json.dump({"question": "Old Q?", "state": "SPEC", "round": 1, "max_rounds": 3, "signoff": "none"},
                  open(os.path.join(d_old, STATE_FILE), "w"))
        st_old = load_state(d_old)
        ok(st_old is not None and st_old.acquire == "off" and st_old.acquired == {} and st_old.state == "SPEC",
           f"T_ACQ_OLD_STATE old run_state.json without acquire loads as off ({st_old})")

        # T_ACQ_SIGN: sheet shows acquired-inputs tables; changing raw file, adding extra file, or changing manifest
        # URL after signing makes signature stale through sign, status, preconditions, and resume (even with NO dossier)
        d_sg = os.path.join(td, "t_acq_sign")
        os.makedirs(d_sg)
        _write_json(d_sg, "metadata.json", {"pipeline": "v2", "commit": "0" * 40, "launcher": "t", "acquire": "auto"})
        raw_sg = yaml.safe_load(open(schema.toy_spec(d_sg)))
        raw_sg["principle_departures"] = [{"principle": 2, "check": "headline_comparator", "reason": "toy"},
                                          {"principle": 3, "check": "baseline_burden", "reason": "toy"}]
        yaml.safe_dump(raw_sg, open(os.path.join(d_sg, AB.SPEC_FILE), "w"), sort_keys=False)
        AM._pre_sign(d_sg, False)
        fk_sg = _fetch_keep(d_sg, "IND1", "csv", ["population"])
        promo_sg = ACQ.promote(d_sg, fk_sg["keep"], fk_sg["wanted_map"], allowlist_path=al_p)
        sheet_txt = SO.sheet(d_sg)
        rec_sg = SO.sign(d_sg, "owner")
        st_sg = RunState(question="q", state="ANALYZE", signoff="owner", acquire="auto",
                         acquired={"rounds": 1, "closed": True, "inventory_sha256": promo_sg["inventory_sha256"]})
        save_state(d_sg, st_sg)
        signed_clean = (
            "### Acquired inputs (fetched by the harness)" in sheet_txt
            and "#### Outgoing acquisition queries" in sheet_txt
            and "#### Fetched and not promoted" in sheet_txt
            and SO.status(d_sg) == "signed"
            and rec_sg.get("acquired_inventory_sha256") == promo_sg["inventory_sha256"]
            and PC.problems(d_sg, None, None, mode="resume") == []
        )
        tamper_results = {}
        acq_sg_rel = promo_sg["promoted"][0]
        for tname in ("raw_file", "extra_file", "manifest_url"):
            cp = os.path.join(td, f"t_acq_sign_{tname}")
            shutil.copytree(d_sg, cp)
            if tname == "raw_file":
                ap = os.path.join(cp, acq_sg_rel)
                os.chmod(ap, 0o644)
                open(ap, "ab").write(b"changed\n")
                os.chmod(ap, 0o444)
            elif tname == "extra_file":
                open(os.path.join(cp, ACQ.ACQUIRED_ROOT, "svc_data", "extra.csv"), "w").write("x\n1\n")
            elif tname == "manifest_url":
                mp = os.path.join(cp, ACQ.MANIFEST_REL)
                mdoc = json.load(open(mp))
                mdoc["files"][acq_sg_rel]["url"] = "https://tampered.example/x"
                json.dump(mdoc, open(mp, "w"))
            st_chg = SO.status(cp) == "changed"
            pc_bad = bool(PC.problems(cp, None, None, mode="resume"))
            try:
                SO.sign(cp, "owner")
                sign_ref = False
            except ValueError:
                sign_ref = True
            res_cp = Machine(cp, StubTasks({}), None, log=lambda *a: None, cross_engine_check=False).run()
            tamper_results[tname] = (st_chg and pc_bad and sign_ref and res_cp == "artifact_tampered")
        ok(signed_clean and all(tamper_results.values()),
           f"T_ACQ_SIGN sheet renders acquired tables; tampering raw/extra/manifest invalidates sign, status, "
           f"preconditions, and resume with no dossier (clean={signed_clean}, tampers={tamper_results})")

        # T_ACQ_QUOTE: acquired literature quote verification (bad quote -> HIGH observation_quote_not_found;
        # exact quote passes)
        d_qt = os.path.join(td, "t_acq_quote")
        os.makedirs(d_qt)
        fk_qt = _fetch_keep(d_qt, "IND1", "xml_ok", ["trial_effect"])
        promo_qt = ACQ.promote(d_qt, fk_qt["keep"], fk_qt["wanted_map"], allowlist_path=al_p)
        fid_qt = fk_qt["keep"][0]
        sha_qt = json.load(open(os.path.join(d_qt, ACQ.MANIFEST_REL)))["files"][promo_qt["promoted"][0]]["sha256"]
        base_obs = {"source": "Trial X", "where": "Results", "year": 2025, "sites": 1, "n": 500,
                    "sampling_frame": "adults", "value": 0.65}
        doss_bad_q = yaml.safe_dump({"inputs": {"parameter:eff": {
            "observations": [dict(base_obs, acquired={"id": fid_qt, "sha256": sha_qt, "locator": "p1",
                                                      "quote": "not present in xml"})]}}}, sort_keys=False).encode()
        doss_ok = yaml.safe_dump({"inputs": {"parameter:eff": {
            "observations": [dict(base_obs, acquired={
                "id": fid_qt, "sha256": sha_qt, "locator": "p1",
                "quote": "Trial result showed efficacy 0.65 (95% CI 0.50-0.78)"})]}}}, sort_keys=False).encode()
        f_bad = ED.quote_findings({}, d_qt, doss_bad_q)
        f_ok = ED.quote_findings({}, d_qt, doss_ok)
        ok(len(f_bad) == 1 and f_bad[0]["kind"] == "observation_quote_not_found" and f_bad[0]["severity"] == "HIGH"
           and f_bad[0]["scope_declarable"] is False and f_ok == [],
           f"T_ACQ_QUOTE quote mismatch is HIGH non-declarable; exact quote passes ({f_bad}, {f_ok})")

        # T_ACQ_DELIVERABLE: deliverable_file.path under acquire/ is refused
        d_del = os.path.join(td, "t_acq_deliv")
        os.makedirs(d_del)
        yaml.safe_dump({"deliverable_file": {"path": "acquire/out.csv", "columns": {"u": "unit"}}},
                       open(os.path.join(d_del, AB.SPEC_FILE), "w"))
        dp_probs = deliverable_problems(d_del)
        ok(bool(dp_probs), f"T_ACQ_DELIVERABLE deliverable path under acquire/ is refused ({dp_probs})")

        # T_ACQ_AMEND: amendment child carries acquired bytes, provenance, record, manifest, metadata fields,
        # and closed acquisition state (without copying acquire/quarantine/)
        st_sg.state = "FINAL"
        st_sg.status = "completed"
        save_state(d_sg, st_sg)
        os.makedirs(os.path.join(d_sg, "acquire", "quarantine"), exist_ok=True)
        open(os.path.join(d_sg, "acquire", "quarantine", "scratch.part"), "w").write("drop")
        m_sg_meta = json.load(open(os.path.join(d_sg, "metadata.json")))
        m_sg_meta.update(acquired_inventory_sha256=promo_sg["inventory_sha256"], allowlist_version=1,
                         allowlist_sha256=ACQ.load_allowlist(al_p)[1])
        _write_json(d_sg, "metadata.json", m_sg_meta)
        child_am = os.path.join(td, "t_acq_amend_child")
        patch_p = os.path.join(td, "t_acq_amend_patch.yaml")
        yaml.safe_dump({"id": "A_ACQ", "reason": "amend envelope",
                        "spec": [{"set": "constraints.envelope_usd", "value": 9500}]},
                       open(patch_p, "w", encoding="utf-8"), sort_keys=False)
        AM.amend(d_sg, patch=patch_p, out=child_am, signoff=None, log=lambda *a: None)
        c_meta = json.load(open(os.path.join(child_am, "metadata.json")))
        c_st = load_state(child_am)
        ok(os.path.isfile(os.path.join(child_am, acq_sg_rel))
           and os.path.isfile(os.path.join(child_am, ACQ.RECORD_REL))
           and os.path.isfile(os.path.join(child_am, ACQ.MANIFEST_REL))
           and os.path.isfile(os.path.join(child_am, PROVENANCE))
           and not os.path.exists(os.path.join(child_am, "acquire", "quarantine"))
           and c_meta.get("acquire") == "auto"
           and c_meta.get("acquired_inventory_sha256") == promo_sg["inventory_sha256"]
           and c_st is not None and (c_st.acquired or {}).get("closed") is True
           and ACQ.verify_inventory(child_am)["inventory_sha256"] == promo_sg["inventory_sha256"],
           f"T_ACQ_AMEND amendment child carries acquired bytes, record, manifest, metadata, and closed state "
           f"(meta_keys={sorted(c_meta)}, closed={(c_st.acquired if c_st else None)})")


def _self_test() -> int:
    import tempfile
    import traceback
    import yaml as _y
    from toolkit.spec import schema as _schema
    from v2_tasks import StubTasks
    fails = []
    toolkit = AB.backend("toolkit")

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)

    with tempfile.TemporaryDirectory() as td:
        def new_run(name, script, signoff="none", max_rounds=3, budget=None):
            d = os.path.join(td, name)
            os.makedirs(d)
            spec_p = _schema.toy_spec(d, n=12)
            with open(spec_p) as fh:
                spec = _schema.direct_toy(_y.safe_load(fh))
            os.unlink(spec_p)
            spec["engine"] = "auto"
            spec["uncertainty"].pop("draws", None)
            spec["uncertainty"].pop("seed", None)
            spec["deliverable_file"] = {"path": "allocation_optimized.csv",
                                        "columns": {"unit": "id", "package": "package", "dalys_averted": "dalys_averted"}}
            spec.update(script.pop("spec_extra", {}))
            script = dict({"spec": spec, "settings": {"uncertainty": {"draws": 24, "seed": 1}}}, **script)
            st = init_run(d, "toy question", max_rounds, signoff, "v2", budget, derived_inputs=False)
            return d, StubTasks(script), st

        try:
            _self_test_resume(ok, td, toolkit, new_run)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            ok(False, f"T58-T60 analysis timeout and resume: {type(e).__name__}: {e}")
        _self_test_auto1a(ok, td, toolkit, new_run)
        _self_test_writeup_rounds(ok, td, toolkit, new_run)
        _self_test_claim_refs(ok, td, toolkit, new_run)
        try:
            _self_test_hb2_spec(ok, td, toolkit, new_run)
        except Exception as e:  # noqa: BLE001
            ok(False, f"HS: {type(e).__name__}: {e}")
        try:
            _self_test_spec_p1s(ok, td, toolkit, new_run)
        except Exception as e:  # noqa: BLE001
            ok(False, f"SP: {type(e).__name__}: {e}")
        try:
            _self_test_acquire(ok, td)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            ok(False, f"T_ACQ_* acquire stage machine: {type(e).__name__}: {e}")
        try:
            _self_test_derived(ok, td)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            ok(False, f"DI machine tests: {type(e).__name__}: {e}")
        try:
            _self_test_geometry(ok, td)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            ok(False, f"IG machine tests: {type(e).__name__}: {e}")
        try:
            _self_test_evidence(ok, td)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            ok(False, f"EV machine tests: {type(e).__name__}: {e}")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
