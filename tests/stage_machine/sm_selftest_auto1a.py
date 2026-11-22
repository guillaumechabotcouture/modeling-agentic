"""Self-tests of agents/stage_machine.py (auto1a), run by `python3 agents/stage_machine.py --self-test`.

Moved verbatim from agents/stage_machine.py by scripts/refactor/split_stage_machine.py.
The module under test is imported as _stage_machine_under_test.
"""
from __future__ import annotations

import os
import analysis_backend as AB
import findings_ledger as FL
import _stage_machine_under_test as _SM
from _stage_machine_under_test import (
    CRITICS, MAX_SPEC_TRIES, Machine, READBACK_CAP_USD, _put, load_state, render_report, report_claim_findings,
    save_state,
)


def _self_test_auto1a(ok, td: str, spike, new_run) -> None:
    """v2 autonomy brief, PR 1A (docs/design/v2-autonomy-brief.md §4; item numbers are the re-audit's): the
    PRESENTATION budget stop, the gate before a rewrite, harness ids, the RETHINK back-edges and claim references."""
    from types import SimpleNamespace as NS
    import yaml
    from v2_tasks import StubTasks

    def mk(d, T, st, backend=spike):
        return Machine(d, T, st, backend=backend, log=lambda *a: None, cross_engine_check=False)

    def case(name, fn):
        try:
            fn()
        except Exception as e:  # noqa: BLE001 -- before the fix exists this is the failure
            ok(False, f"{name}: {type(e).__name__}: {e}")

    def budget():
        d, T, st = new_run("a1_pres", {}, budget=20.0)
        st.state, st.spent_usd = "PRESENTATION", 16.5        # past cap less reserve, $3.50 of the reserve left
        save_state(d, st)
        mk(d, T, st).step()
        s = load_state(d)
        ok(s.state == "GATE" and s.forced_status is None and T.calls == ["critique:critique-presentation:1"],
           f"T_PRES_BUDGET the presentation review runs on the write-up reserve ({s.state}, {s.forced_status}, {T.calls})")
        d, T, st = new_run("a1_pres_out", {}, budget=20.0)
        st.state, st.spent_usd = "PRESENTATION", 20.0
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
        # T_PW_GATE (2.1): the pre-write gate after a post-write modelling fix, one round on.
        d = os.path.join(td, "a1_pw")
        os.makedirs(d)
        S = StubTasks({"critics": {1: {"critique-presentation": [
            {"id": "P-001", "severity": "MEDIUM", "target_stage": "WRITE", "claim": "a caption is unclear",
             "done_when": [{"file": "manuscript.md", "contains": "Figure 1 shows"}]}]}}})
        S.critique(d, "critique-presentation", 1)
        for r in CRITICS:
            S.critique(d, r, 2)
        open(os.path.join(d, "manuscript.md"), "w").write("# Report\n")
        g, gp = (_SM.v2_gate(d, 2, 3, pw, [], history=False) for pw in (False, True))
        kinds = {v.get("kind") for v in g.get("rigor_violations") or []}
        ok(not g.get("schema_errors") and g.get("action") and "P-001" in {o["id"] for o in _SM.open_findings(d, g)}
           and not kinds & {"done_when_dropped", "critique_presentation_missing"} and gp.get("schema_errors"),
           f"T_PW_GATE a pre-write gate reads the last draft's presentation review as of its own round; the "
           f"post-write gate needs this round's ({g.get('schema_errors')}, {g.get('action')}, {sorted(kinds)}, "
           f"post {bool(gp.get('schema_errors'))})")
        d = os.path.join(td, "a1_ofp")
        os.makedirs(d)
        yaml.safe_dump({"reviewer": "critique-domain", "round": 1, "blockers": [
            {"id": "D-061", "severity": "MEDIUM", "category": "PRESENTATION", "target_stage": "ANALYZE", "claim": "c"},
            {"id": "D-062", "severity": "MEDIUM", "category": "METHODS", "target_stage": "ANALYZE", "claim": "c"}]},
            open(os.path.join(d, "critique_domain.yaml"), "w"))
        got = [o["id"] for o in _SM.open_findings(d, {"action": "ACCEPT", "rigor_violations": []})]
        ok(got == ["D-062"], f"T_OF_PRES a PRESENTATION finding waits for the draft ({got})")

    def ids():
        class B:
            def __getattr__(self, n):
                return getattr(spike, n)

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
        st.state, st.spent_usd = "PRESENTATION", 20.0
        st.journal["PRESENTATION#0:r1"] = {"task": "critique", "status": "completed", "ok": True}
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
            {"id": "D-061", "severity": "MEDIUM", "category": "PRESENTATION", "target_stage": "ANALYZE", "claim": "c"}]},
            open(os.path.join(d, "critique_domain.yaml"), "w"))
        open(os.path.join(d, "report.md"), "w").write("# Report\n")
        got = [o["id"] for o in _SM.open_findings(d, {"action": "ACCEPT", "rigor_violations": []})]
        ok(got == ["D-061"], f"CX1-5 with report.md and no manuscript.md a PRESENTATION finding is not deferred ({got})")

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
        got = [o["id"] for o in _SM.open_findings(d, {"action": "ACCEPT", "rigor_violations": []})]
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
        got = [o["id"] for o in _SM.open_findings(d, {"action": "PATCH_OR_RETHINK", "rigor_violations": [
            {"kind": "done_when_regressed", "key": "D-071", "severity": "HIGH", "claim": "c"}]})]
        ok("done_when_regressed:D-071" in got and "D-071" in got, f"CX3-6 a regression is listed beside its finding ({got})")

    def r3_presentation():
        # CX3-1: documented behaviour, pinned (the base does this in an uninterrupted run).
        class NoReview(StubTasks):
            def critique(self, run_dir, reviewer, round_n, feedback=None):
                return self._r(f"critique:{reviewer}:{round_n}", False, note="no review written")
        states = []
        for name, journal in (("live", None), ("replay", {"task": "critique", "status": "completed", "ok": False})):
            d, T, st = new_run("a1_cx31" + name, {}, budget=20.0)
            st.state, st.spent_usd = "PRESENTATION", 20.0 if journal else 16.5
            if journal:
                st.journal["PRESENTATION#0:r1"] = journal
            save_state(d, st)
            mk(d, NoReview(T.s), st).step()
            states.append(load_state(d).state)
        open(os.path.join(d, "manuscript.md"), "w").write("# Report\n")
        for r in CRITICS:
            StubTasks({}).critique(d, r, 1)
        kinds = [(v.get("kind"), v.get("severity")) for v in _SM.v2_gate(d, 1, 3, True, [], history=False).get("rigor_violations") or []]
        ok(states == ["GATE", "GATE"] and ("critique_presentation_missing", "MEDIUM") in kinds,
           f"CX3-1 a failed presentation task goes to the gate, live and replayed alike; the gate reports the missing "
           f"review ({states}, {kinds})")

    for name, fn in (("T_PRES_BUDGET", budget), ("T_PW_GATE", post_write), ("F_HARNESS_IDS", ids),
                     ("T_RETHINK", rethink), ("T_CLAIM", claims), ("CX1-3", r1_resume), ("CX1-4", r1_readback),
                     ("CX1-5", r1_open), ("CX1-7", r1_rethink), ("CX1-2", r1_claims), ("CX2-1", r2_machine),
                     ("CX2-4", r2_findings), ("CX3-1", r3_presentation)):
        case(name, fn)
