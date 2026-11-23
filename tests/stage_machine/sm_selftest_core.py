"""Self-tests of agents/stage_machine.py (core), run by `python3 agents/stage_machine.py --self-test`.

Moved verbatim from agents/stage_machine.py by scripts/refactor/split_stage_machine.py.
The module under test is imported as _stage_machine_under_test.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import analysis_backend as AB
import findings_ledger as FL
import fix_loop as FX
import _stage_machine_under_test as _SM
from _stage_machine_under_test import (
    ACCEPTED_STATUSES, AWAITING_BUDGET, AWAITING_SPEC_AMENDMENT, CRITICS, DECLARED_FILE, EDGES, Machine,
    OWNER_LIFECYCLE_WIRED, OWNER_REQUEST, PROVENANCE, RunState, SCOPE_SIGNOFF, SIGNOFF_FILE, SIGNOFF_SHEET,
    STATE_FILE, _bearing_item, _owner_rule, _plan_changing, _quarantine_report, _question_paths, _rep_error,
    _signoff, _spec_signed_sha, _write_json, answer_key_path, apply_medium_rule, apply_owner_declines,
    apply_owner_rule, deliverable_problems, drop_declined, init_run, load_state, main, pending_amendments,
    record_cited_inputs, render_report, report_acceptance_findings, report_claim_findings, require_judged,
    route_gate, run_v2, save_state, sign, signed_hash, spec_signed_once, stage_inputs, write_declared,
)
from sm_selftest_auto1a import _self_test_auto1a
from sm_selftest_common import SM_GLOBALS, _t_owner_disposes
from sm_selftest_derived import _self_test_derived
from sm_selftest_evidence import _self_test_evidence
from sm_selftest_geometry import _self_test_geometry
from sm_selftest_hb2_spec import _self_test_hb2_spec
from sm_selftest_lead_amend import _self_test_lead_amend
from sm_selftest_resume import _self_test_resume
from sm_selftest_spec_p1s import _self_test_spec_p1s
from sm_selftest_writeup_rounds import _self_test_writeup_rounds


def _self_test() -> int:
    import tempfile
    import shutil
    from v2_tasks import StubTasks
    fails = []
    # T10-T35 use the spike adapter's toy spec, so they pin that backend: since workstream B
    # merged, the default backend is B's, whose spec schema the spike fixtures do not follow.
    spike = AB.backend("spike")

    def _SpikeMachine(*a, **k):
        k.setdefault("backend", spike)
        return Machine(*a, **k)

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)

    # T1-T9: route_gate, every action in both modes (the golden traces exercise these on stored runs).
    R = route_gate
    ok(R({"action": "ACCEPT"}, False)[:2] == ("WRITE", None) and R({"action": "ACCEPT"}, True)[:2] == ("FINAL", "completed"),
       "T1 ACCEPT: write, then completed after the post-write gate")
    ok(R({"action": "PATCH_OR_RETHINK"}, False)[0] == "FIX_LOOP" and R({"action": "PATCH_OR_RETHINK"}, True)[0] == "FIX_LOOP",
       "T2 PATCH_OR_RETHINK: the fix loop, also for a post-write HIGH")
    ok(R({"action": "RETHINK_STRUCTURAL"}, False)[0] == "ENGINE_CHOICE"
       and R({"action": "RETHINK_STRUCTURAL", "unresolved_high": [{"category": "STRUCTURAL", "target_stage": "PLAN"}]},
             False)[0] == "SPEC", "T3 RETHINK_STRUCTURAL is a back-edge: ENGINE_CHOICE, or SPEC for a PLAN target")
    ok(R({"action": "DECLARE_SCOPE", "rule_matched": "3a"}, False) == ("WRITE", None, "declared")
       and R({"action": "DECLARE_SCOPE"}, True)[:2] == ("FINAL", "completed_scope_declared"),
       "T4 DECLARE_SCOPE (incl. early finish): write with the declarations, then completed_scope_declared")
    ok(R({"action": "RUN_FAILED"}, False) == ("WRITE", None, "not_accepted") and R({"action": "RUN_FAILED"}, True)[:2]
       == ("FINAL", "run_failed"), "T5 RUN_FAILED: a not-accepted report, then run_failed")
    ok(R({"action": "AWAIT_OWNER"}, False)[0] == "OWNER_DECISION", "T6 AWAIT_OWNER (Phase 49 β) waits for the owner")
    ok(R({"action": "RUN_FAILED", "unresolved_high": [{"kind": "scope_declaration_declined"}]}, True)[1]
       == "completed_scope_declined", "T7 an owner decline at exhaustion ends completed_scope_declined")
    ok(R({"schema_errors": ["x"]}, False)[0] == "CRITIQUE" and R(None, False)[1] == "completed_gate_error"
       and R({"action": "VALIDATOR_ERROR"}, True)[1] == "completed_gate_error", "T8 schema errors re-run the critics; no verdict is a gate error")
    for a in ("ACCEPT", "PATCH_OR_RETHINK", "RETHINK_STRUCTURAL", "DECLARE_SCOPE", "RUN_FAILED", "AWAIT_OWNER", None):
        for pw in (False, True):
            nxt = R({"action": a}, pw)[0]
            if nxt not in EDGES["GATE"]:
                ok(False, f"T9 {a} routes outside the state table")
    ok(True, "T9 every route is an edge of the state table")
    # T9b (T16's CI flakes, 2026-11-20/21): a failed step's error is its log tail, a traceback whose first 300
    # characters are only its outer frames; the reason a run ended must keep the exception and where it was raised.
    root = "/home/runner/work/modeling-agentic/modeling-agentic"
    tb = ("Traceback (most recent call last):\n"
          f'  File "{root}/agents/analysis_backend.py", line 1556, in <module>\n    sys.exit(main())\n'
          "             ~~~~^^\n"
          f'  File "{root}/agents/analysis_backend.py", line 776, in main\n    A = _analyze(spec, out)\n'
          f'  File "{root}/agents/analysis_backend.py", line 515, in _analyze\n'
          "    return Analysis(spec, out, log=lambda *a: None).run()\n"
          f'  File "{root}/spike/analysis/run.py", line 120, in solve\n'
          "    raise RuntimeError(f\"optimizer status {r['status']}\")\n"
          "RuntimeError: optimizer status feasible\n")
    why = _rep_error({"ok": False, "steps": [{"id": "configure", "status": "ok"},
                                             {"id": "analyze", "status": "failed", "error": tb}]})
    ok("RuntimeError: optimizer status feasible" in why and "run.py:120 in solve" in why and len(why) <= 300,
       f"T9b a failed step's reason keeps its exception and the frame that raised it: {why!r}")
    why2 = _rep_error({"ok": False, "errors": ["upstream step not rebuilt: configure"],
                       "steps": [{"id": "batch", "status": "failed", "error": "timed out after 8s at its backstop"}]})
    ok(why2 == "upstream step not rebuilt: configure; batch: timed out after 8s at its backstop",
       f"T9c an error that is not a traceback is kept as it was: {why2!r}")
    # T9d (Codex r1 on the T9b fix): real interpreter output, as an 800-character log tail holds it.
    def tail_of(code, n=800):
        r = subprocess.run([sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                           timeout=60)
        return r.stdout[-n:]
    cases = {
        "trailing output": ("import atexit; atexit.register(print, 'cleanup complete')\n"
                            "def solve():\n    raise RuntimeError('boom')\nsolve()", "RuntimeError: boom", "in solve"),
        "multi-line message": ("def f():\n    raise ValueError('invalid row\\ncolumn 7')\nf()",
                               "ValueError: invalid row", "in f"),
        "syntax error": ("compile('x =', 'broken.py', 'exec')", "SyntaxError", "broken.py:1"),
        "frame text in message": ("def g():\n    raise ValueError('see File \"other.py\", line 97, in other')\ng()",
                                  "ValueError: see", "in g"),
        "exception group": ("raise ExceptionGroup('workers', [ValueError('bad row')])", "ExceptionGroup: workers",
                            "<string>:1"),
        "long message": ("def h():\n    raise ValueError('x' * 320)\nh()", "ValueError: xxx", "in h"),
        "header cut": ("".join(f"def f{i}():\n    f{i + 1}()\n" for i in range(40))
                       + "def f40():\n    raise RuntimeError('deep')\nf0()", "RuntimeError: deep", "in f40"),
    }
    for name, (code, want_exc, want_at) in cases.items():
        why_ = _rep_error({"ok": False, "steps": [{"id": "analyze", "status": "failed", "error": tail_of(code)}]})
        ok(want_exc in why_ and want_at in why_ and "Traceback at" in why_ and "at other.py" not in why_
           and "Exception Group Traceback" not in why_,
           f"T9d a traceback with {name} keeps its exception and raising frame: {why_!r}")
    to = "timed out after 8s at its backstop (process group killed); last output: " + tail_of(
        "def s():\n    raise RuntimeError('boom')\ns()", 300)
    why3 = _rep_error({"ok": False, "steps": [{"id": "analyze", "status": "failed", "error": to}]})
    ok(why3.startswith("analyze: timed out after 8s") and "RuntimeError: boom" in why3,
       f"T9e a timeout whose last output is a traceback still reads as the timeout first: {why3!r}")

    with tempfile.TemporaryDirectory() as td:
        def new_run(name, script, signoff="none", max_rounds=3, budget=None):
            d = os.path.join(td, name)
            os.makedirs(d)
            AB.write_toy_units(os.path.join(d, "units.csv"), n=12)
            spec = AB.toy_spec("units.csv")
            spec.pop("engine")
            spec["deliverable_file"] = {"path": "allocation_optimized.csv",
                                        "columns": {"unit": "id", "package": "package", "dalys_averted": "dalys_averted"}}
            spec.update(script.pop("spec_extra", {}))
            # 16 emulator points: the resume test runs the whole machine many times
            script = dict({"spec": spec, "settings": AB.toy_settings(draws=24, points=16)}, **script)
            st = init_run(d, "toy question", max_rounds, signoff, "v2", budget, derived_inputs=False)   # pre-DI fixture
            return d, StubTasks(script), st

        # T10: the happy path with one critic finding fixed in the loop.
        blocker = {"id": "D-001", "severity": "HIGH", "target_stage": "ANALYZE", "claim": "too few draws",
                   "done_when": [{"file": "analysis_settings.yaml", "key": "uncertainty.draws", "op": ">=", "value": 32}]}
        d, T, st = new_run("happy", {"critics": {1: {"critique-domain": [blocker]}},
                                     "fixes": {"D-001": [("analysis_settings.yaml", "uncertainty.draws", 32)],
                                               "uncertainty_draws_low:draws": [("analysis_settings.yaml", "uncertainty.draws", 32)]}})
        status = _SpikeMachine(d, T, st, log=lambda *a: None, cross_engine_check=False).run()
        hist = [h["to"] for h in load_state(d).history if "to" in h]
        ok(status == "completed", f"T10 toy run completes: {status} via {hist}")
        ok(hist[:8] == ["EVIDENCE", "SIGN", "ENGINE_CHOICE", "PREREGISTER", "CONFIGURE", "ANALYZE", "SCORE", "CRITIQUE"]
           and "FIX_LOOP" in hist
           and hist[-3:] == ["PRESENTATION", "GATE", "FINAL"], f"T11 the path SPEC..FIX_LOOP..WRITE..FINAL {hist}")
        from toolkit.spec import evidence_draft as EDR_
        ok("draft_evidence" not in T.calls and (EDR_.read_record(d) or {}).get("status") == "not_required",
           f"T11b a spec whose options need no decision input passes EVIDENCE with no task ({EDR_.read_record(d)})")
        rows = FX.impact_rows(d)
        ok(rows and rows[0]["files_changed"] == ["analysis_settings.yaml"] and "analyze" in rows[0]["steps_rebuilt"]
           and rows[0]["headline_before"] != rows[0]["headline_after"] and rows[0]["passed"],
           f"T12 the fix has an impact row: settings changed, analysis rebuilt, headline moved, check passed")
        led = FL.load(d)
        ok(led["D-001"].status == "fixed" and os.path.exists(os.path.join(d, "allocation_optimized.csv"))
           and "Numbers (rendered by the harness)" in open(os.path.join(d, "report.md")).read(),
           "T13 the ledger closes the finding; report and deliverable are rendered by the harness")
        ok(json.load(open(os.path.join(d, "engine_choice.json")))["engine"]["ref"].endswith("ToyEngine")
           and AB.engine_choice_ref(d).endswith("ToyEngine") and "candidates" in json.load(open(os.path.join(d, AB.ENGINE_CHOICE)))
           and load_state(d).engine.endswith("ToyEngine"), "T14 the engine choice is automatic and recorded")
        ok(json.load(open(os.path.join(d, "metadata.json")))["status"] == "completed", "T15 metadata.json carries the status")

        # T16: resume after every state. Stop after k steps, rebuild the machine from disk, finish.
        d0, T0, st0 = new_run("ref", {"critics": {1: {"critique-domain": [blocker]}},
                                      "fixes": {"D-001": [("analysis_settings.yaml", "uncertainty.draws", 32)]}})
        ref_status = _SpikeMachine(d0, T0, st0, log=lambda *a: None, cross_engine_check=False).run()
        ref_path = [h["to"] for h in load_state(d0).history if "to" in h]

        def _final_why(d_):     # the reason a run reached FINAL (a step's error carries its log tail): CI flakes of
            return [h.get("why") for h in load_state(d_).history if h.get("to") == "FINAL"][-1:]   # T16 named none
        ref_why = _final_why(d0) if ref_status != "completed" else []
        bad = []
        for k in range(1, len(ref_path)):
            dk, Tk, stk = new_run(f"resume{k}", {"critics": {1: {"critique-domain": [blocker]}},
                                                "fixes": {"D-001": [("analysis_settings.yaml", "uncertainty.draws", 32)]}})
            m = _SpikeMachine(dk, Tk, stk, log=lambda *a: None, cross_engine_check=False)
            for _ in range(k):
                m.step()
            del m
            m2 = _SpikeMachine(dk, StubTasks(Tk.s), None, log=lambda *a: None, cross_engine_check=False)   # from run_state.json
            s2 = m2.run()
            p2 = [h["to"] for h in load_state(dk).history if "to" in h]
            if s2 != ref_status or p2 != ref_path:
                bad.append((k, s2, p2, _final_why(dk)))
            shutil.rmtree(dk)
        ok(not bad, f"T16 a run stopped after any of {len(ref_path) - 1} transitions resumes to the same path and status {bad[:1]}"
           + (f" (reference run {ref_status}: {ref_why})" if ref_why else ""))

        # T17: owner sign-off pauses, persists, resumes.
        d, T, st = new_run("owner", {}, signoff="owner")
        m = _SpikeMachine(d, T, st, log=lambda *a: None, cross_engine_check=False)
        s = m.run()
        ok(s == "paused:awaiting_signoff" and load_state(d).state == "SIGN", f"T17 owner mode pauses at SIGN ({s})")
        pz = [(h.get("paused"), h.get("state")) for h in load_state(d).history if "paused" in h]
        ok(pz == [("awaiting_signoff", "SIGN")], f"RR1 a SIGN pause writes one history entry ({pz})")
        from toolkit.spec import review_conditions as _RC
        _rows = _RC.parse(_RC.read_bytes(d))
        try:
            sign(d, "owner", "early")
            _early = "signed"
        except ValueError as e:
            _early = str(e)
        ok(_rows and all(r["source"] == "spec_critique" and not r.get("disposition") for r in _rows)
           and "review conditions" in _early,
           f"T17rc (W0-G, owner 2026-11-21) at SIGN the spec critique's HIGH findings are imported as open conditions; "
           f"the owner's signature waits for their dispositions ({len(_rows)} row(s); {_early[:80]})")
        from toolkit.spec import preconditions as _PC      # #215: this signing path runs the shared preconditions
        _PC.CHECKS["_t17"] = lambda r, sp, by: [f"refused for {by}"]
        try:
            _t_owner_disposes(d)
            sign(d, "owner", "ok")
            ok(False, "T17p the spike sign-off refuses a spec failing a registered precondition")
        except ValueError as e:
            ok("refused for owner" in str(e), f"T17p the spike sign-off refuses a spec failing a registered precondition")
        finally:
            _PC.CHECKS.pop("_t17", None)
        _t_owner_disposes(d)
        sign(d, "owner", "ok")
        ok(_SpikeMachine(d, StubTasks(T.s), None, log=lambda *a: None, cross_engine_check=False).run() == "completed",
           "T18 after the owner signs, the resumed run completes")

        d, T, st = new_run("lead_rc", {}, signoff="lead")
        s = _SpikeMachine(d, T, st, log=lambda *a: None, cross_engine_check=False).run()
        _rows = _RC.parse(_RC.read_bytes(d)) if os.path.exists(os.path.join(d, _RC.FILE)) else []
        _rep = open(os.path.join(d, "report.md")).read() if os.path.exists(os.path.join(d, "report.md")) else ""
        ok(s == "completed" and _rows and all(r["disposition"] == "deferred" and r["owner"] == "owner"
                                              and r.get("provisional") for r in _rows)
           and _RC.REPORT_HEADING in _rep and _rep.index(_RC.REPORT_HEADING) < _rep.index("## Numbers"),
           f"T18rc (W0-G, D2) under --signoff lead each condition is deferred to the owner provisionally, listed at the "
           f"top of the report, and the run proceeds ({s}, {len(_rows)} row(s))")

        # T19: a fix that edits the signed spec, or puts a decision leaf in the settings file, is
        # undone before anything is rebuilt; the spec stays as signed.
        d, T, st = new_run("tamper", {"critics": {1: {"critique-redteam": [dict(blocker, id="R-001")]}},
                                      "fixes": {"R-001": [("decision_spec.yaml", "constraints.envelope_usd", 99999),
                                                          ("analysis_settings.yaml", "constraints.envelope_usd", 99999)]}},
                           signoff="lead", max_rounds=2)
        signed_before = None

        class Spy(StubTasks):
            def fix(self, task, run_dir, evidence):
                nonlocal signed_before
                signed_before = signed_before or _signoff(run_dir)["spec_sha256"]
                return super().fix(task, run_dir, evidence)
        s = _SpikeMachine(d, Spy(T.s), st, log=lambda *a: None, cross_engine_check=False).run()
        rows = FX.impact_rows(d)
        led = FL.load(d)
        ok(rows and all(r.get("rejected") and not r.get("steps_rebuilt") for r in rows)
           and signed_hash(d) == signed_before and "constraints.envelope_usd" not in AB._leaves(
               __import__("yaml").safe_load(open(os.path.join(d, AB.SETTINGS_FILE))) or {})
           and "spec_changed_after_signoff:spec" not in led and s == "completed_scope_declared",
           f"T19 edits to the signed spec are rejected and undone before any rebuild ({s}, {[r.get('rejected') for r in rows][:1]})")

        # T20: a structural mismatch is a back-edge to ENGINE_CHOICE, then the run continues.
        d, T, st = new_run("rethink", {"structural": {1: "critique-domain"}})
        s = _SpikeMachine(d, T, st, log=lambda *a: None, cross_engine_check=False).run()
        p = [h["to"] for h in load_state(d).history if "to" in h]
        i = p.index("GATE")
        ok(p[i + 1] == "ENGINE_CHOICE" and s == "completed" and load_state(d).round == 2,
           f"T20 RETHINK_STRUCTURAL goes back to ENGINE_CHOICE and the next critique is round 2 ({s})")

        # T21: an open blocker at the last round: DECLARE_SCOPE when declarable.
        nofix = {"id": "D-009", "severity": "HIGH", "target_stage": "ANALYZE", "claim": "cannot fix"}
        d, T, st = new_run("declare", {"critics": {1: {"critique-domain": [nofix]}}}, max_rounds=2)
        s = _SpikeMachine(d, T, st, log=lambda *a: None, cross_engine_check=False).run()
        ok(s == "completed_scope_declared", f"T21 an unfixed critic blocker at exhaustion ends scope-declared ({s})")
        rep21 = open(os.path.join(d, "report.md")).read()
        ok(os.path.exists(os.path.join(d, DECLARED_FILE)) and "D-009" in rep21 and "Open findings, declared" in rep21,
           "T46 (Codex #95-5) an ordinary DECLARE_SCOPE (rule 3) also lists what is open in the report")

        # T22: the budget, not the gate, ends the run: the harness writes the report.
        d, T, st = new_run("budget", {"critics": {1: {"critique-domain": [nofix]}}, "usd": 30.0}, budget=100.0)
        s = _SpikeMachine(d, T, st, log=lambda *a: None, cross_engine_check=False).run()
        p = [h["to"] for h in load_state(d).history if "to" in h]
        ok(s == "budget_exhausted" and "WRITE" in p and os.path.exists(os.path.join(d, "report.md")),
           f"T22 with the budget used the run ends budget_exhausted with a report ({s}, {p[-4:]})")

        # T25: an interrupted paid task. The redteam critique crashes once in round 1: on resume the
        # domain critique (completed) is not run again, and the interrupted one is charged its allowance.
        d, T, st = new_run("crash", {"crash": {"critique:critique-redteam:1": 1}, "usd": 1.0}, budget=100.0)
        m = _SpikeMachine(d, T, st, log=lambda *a: None, cross_engine_check=False)
        try:
            m.run()
            ok(False, "T25 the stub crash propagates")
        except RuntimeError:
            pass
        spent_at_crash = load_state(d).spent_usd
        T2 = StubTasks(dict(T.s, crash={}))
        s = _SpikeMachine(d, T2, None, log=lambda *a: None, cross_engine_check=False).run()
        j = load_state(d).journal
        dom = [k for k in j if k.endswith(":r1:critique-domain")]
        red = [k for k in j if k.endswith(":r1:critique-redteam")]
        ok(s == "completed" and "critique:critique-domain:1" not in T2.calls and T2.calls.count("critique:critique-redteam:1") == 1
           and dom and j[dom[0]]["status"] == "completed" and red and j[red[0]].get("interrupted_charged") == 1,
           f"T25 resume skips the completed critique and re-runs the interrupted one ({s}, {T2.calls[:3]})")
        ok(abs(load_state(d).spent_usd - spent_at_crash - 3.0 - 1.0 * len(T2.calls)) < 1e-9,
           f"T26 the interrupted task is charged its allowance ($3) once; completed ones are not charged again "
           f"({spent_at_crash} -> {load_state(d).spent_usd}, {len(T2.calls)} new calls)")

        # T27: each paid task gets an allowance from what is left (less the write-up reserve), set before
        # the call; the second critic of a pair is not called when the first used the budget.
        d, T, st = new_run("allow", {"usd": 9.0}, budget=30.0)
        st.task_cap_usd = 9.0
        save_state(d, st)
        s = _SpikeMachine(d, T, st, log=lambda *a: None, cross_engine_check=False).run()
        ok(T.allowances == [9.0, 9.0, 2.0, 3.0] and "critique:critique-redteam:1" not in T.calls
           and s == "budget_exhausted",
           f"T27 allowances follow the budget and the pair stops when it is used ({T.allowances}, {s})")
        jj = load_state(d).journal
        ok(all("allowance_usd" in e for k, e in jj.items() if not k.endswith("spec_sha256")),
           "T28 every journal entry records the allowance it was given")

        # R3-2 (F6 run 3, gap 2; narrowed, lead decision): a SPEC rethink of a signed spec never redrafts it in the run.
        # The machine writes spec_rethink_request.json (why: the rethink's brief, the findings and their remedies) and
        # pauses for the owner, who amends the spec and relaunches with reuse; the signed files stay as signed.
        rk = {"id": "D-040", "severity": "HIGH", "target_stage": "PLAN", "claim": "the envelope is the wrong year",
              "fix_requires": "redraft the envelope for the decision year"}
        once40 = []

        def plan40(pk):
            if any(f["id"] == "D-040" for f in pk["findings"]) and not once40:
                once40.append(1)
                return {"tasks": [{"id": "T1", "findings": ["D-040"], "action": "rethink", "owner_state": "SPEC",
                                   "brief": "redraft the envelope"}]}
            return {"tasks": []}
        d, T, st = new_run("rethink_signed", {"critics": {1: {"critique-domain": [rk]}}, "plan": plan40},
                           signoff="owner", max_rounds=3)
        _SpikeMachine(d, T, st, log=lambda *a: None, cross_engine_check=False).run()
        _t_owner_disposes(d)
        sign(d, "owner", "first")
        signed_bytes = open(os.path.join(d, AB.SPEC_FILE), "rb").read()
        T2 = StubTasks(T.s)
        s = _SpikeMachine(d, T2, None, log=lambda *a: None, cross_engine_check=False).run()
        try:
            rq = json.load(open(os.path.join(d, "spec_rethink_request.json")))
        except (OSError, ValueError):
            rq = {}
        ok(s == "paused:awaiting_spec_amendment" and "draft_spec" not in T2.calls
           and open(os.path.join(d, AB.SPEC_FILE), "rb").read() == signed_bytes
           and "D-040" in json.dumps(rq.get("findings")) and "redraft the envelope" in json.dumps(rq.get("briefs"))
           and rq.get("signed_spec_sha256") == hashlib.sha256(signed_bytes).hexdigest(),
           f"R3-2 a rethink of a signed spec writes the request and pauses; nothing is redrafted ({s}, {T2.calls[-3:]})")
        pz = [(h.get("paused"), h.get("state")) for h in load_state(d).history if "paused" in h]
        ok(pz[-1:] == [("awaiting_spec_amendment", "SPEC")], f"RR2 an amendment pause writes a history entry ({pz})")
        T3 = StubTasks(T.s)
        s2 = _SpikeMachine(d, T3, None, log=lambda *a: None, cross_engine_check=False).run()
        ok(s2 == "paused:awaiting_spec_amendment" and "draft_spec" not in T3.calls
           and open(os.path.join(d, AB.SPEC_FILE), "rb").read() == signed_bytes,
           f"R3-2b a resume stays paused for the amendment and drafts nothing ({s2})")
        # R3-2c (Codex review of #261): the pause is honoured on resume even if a restored copy lost the sign-off record,
        # and keeps its first request; and it is written with no budget left (it costs no model call)
        rq0 = json.load(open(os.path.join(d, "spec_rethink_request.json")))
        os.remove(os.path.join(d, SIGNOFF_FILE))
        T4 = StubTasks(T.s)
        s3 = _SpikeMachine(d, T4, None, log=lambda *a: None, cross_engine_check=False).run()
        ok(s3 == "paused:awaiting_spec_amendment" and "draft_spec" not in T4.calls
           and json.load(open(os.path.join(d, "spec_rethink_request.json")))["requested_at"] == rq0["requested_at"],
           f"R3-2c a resumed amendment pause is honoured without the sign-off record and keeps its request ({s3})")
        st5 = load_state(d)
        st5.budget_usd, st5.spent_usd = 20.0, 20.0
        save_state(d, st5)
        s5 = _SpikeMachine(d, StubTasks(T.s), None, log=lambda *a: None, cross_engine_check=False).run()
        ok(s5 == "paused:awaiting_spec_amendment", f"R3-2d with no budget left the amendment pause still holds ({s5})")
        # R3-2e (narrow re-review of #261): an unreadable sign-off record still protects the spec; a persisted pause is
        # honoured even without the flag (a state saved before it existed)
        d6 = os.path.join(td, "r32e")
        os.makedirs(d6)
        open(os.path.join(d6, SIGNOFF_FILE), "w").write('{"by": "own')
        st6 = load_state(d)
        st6.tries.pop("spec_amendment", None)
        st6.paused = AWAITING_SPEC_AMENDMENT
        save_state(d, st6)
        T6 = StubTasks(T.s)
        m6 = _SpikeMachine(d, T6, None, log=lambda *a: None, cross_engine_check=False)
        m6.step()
        ok(spec_signed_once(d6) and "draft_spec" not in T6.calls and load_state(d).paused == AWAITING_SPEC_AMENDMENT,
           "R3-2e a truncated sign-off record still protects the spec; a persisted amendment pause is honoured")
        # R3-2f (lead follow-up): the pause says when the signing evidence is missing or unreadable (a warning, no block)
        rq6 = json.load(open(os.path.join(d, "spec_rethink_request.json")))
        ok(rq6.get("signing_evidence") == "missing" and "requested_at" in rq6,
           f"R3-2f the amendment request notes missing signing evidence ({rq6.get('signing_evidence')})")
        _self_test_lead_amend(ok, td)

        # R3-1 (F6 run 3): a write-up finding is never a FIX_LOOP task; the writer gets it with the draft.
        wup = {"id": "R-030", "severity": "HIGH", "target_stage": "WRITE", "claim": "the report overstates the effect"}
        d, T, st = new_run("writeup", {"critics": {1: {"critique-redteam": [wup]}}}, max_rounds=2)
        s = _SpikeMachine(d, T, st, log=lambda *a: None, cross_engine_check=False).run()
        p = [h["to"] for h in load_state(d).history if "to" in h]
        got = getattr(T, "write_findings", None) or []
        ok("FIX_LOOP" in p and not any(c.startswith("fix:") for c in T.calls)
           and any(f.get("id") == "R-030" for f in got),
           f"R3-1 a write-up finding goes to the writer, not to a FIX_LOOP task ({s}, {T.calls}, {got})")
        # HG1 (Codex review of #341, r1 P1-5 and r3 P2-10): a blocking harness gap is an open finding the writer cannot
        # fix, so a loop with no task, write-up findings and a draft goes to CRITIQUE, not the "every finding is the
        # write-up's" WRITE; a non-blocking gap does not hold the writer back. HG1c (r3 P2-8): the fix loop is given the
        # settings of the backend this machine runs.
        def hg_route(blocking):
            d, T, st = new_run(f"harness_gap_route_{len(blocking)}", {}, max_rounds=3)
            mh = _SpikeMachine(d, T, st, log=lambda *a: None, cross_engine_check=False)
            mh.st.state, mh._has_draft = "FIX_LOOP", (lambda: True)
            seen, _rl = {}, FX.run_loop
            try:
                FX.run_loop = lambda *a, **k: seen.update(k) or {
                    "round": 1, "tasks": 0, "rows": 0, "passed": 0, "rethink": None, "write_up": ["R-030"],
                    "owner_amendments": [], "harness_gaps": ["D-001"], "harness_gaps_blocking": blocking}
                return mh.h_FIX_LOOP(), seen.get("settings"), mh.backend
            finally:
                FX.run_loop = _rl
        to_hg, set_hg, be_hg = hg_route(["D-001"])
        ok(to_hg == "CRITIQUE", f"HG1 a blocking harness gap keeps the loop off the write-up-only WRITE route ({to_hg})")
        ok(hg_route([])[0] == "WRITE", "HG1b a non-blocking harness gap leaves the write-up-only WRITE route open")
        ok(set_hg is not None and tuple(set_hg) == tuple(getattr(be_hg, "settings_paths", AB.SETTINGS_PATHS)),
           f"HG1c the fix loop is given the running backend's settings ({set_hg})")

        # R3-3 (F6 run 3): a finding whose remedy is an owner amendment goes to OWNER_DECISION, never a SPEC rethink.
        amend = {"id": "D-031", "severity": "HIGH", "target_stage": "PLAN", "claim": "the comparator omits the funded plan",
                 "fix_requires": "Owner amendment: add the funded plan as the comparator."}
        d, T, st = new_run("amend", {"critics": {1: {"critique-domain": [amend]}},
                                     "plan": lambda pk: {"tasks": [{"id": "T1", "findings": [f["id"] for f in
                                                                                                pk["findings"]][:1] or ["x"],
                                                                    "action": "rethink", "owner_state": "SPEC",
                                                                    "brief": "redraft the comparator"}]
                                                         if pk["findings"] else {"tasks": []}}}, max_rounds=3)
        s = _SpikeMachine(d, T, st, log=lambda *a: None, cross_engine_check=False).run()
        p = [h["to"] for h in load_state(d).history if "to" in h]
        try:
            req = json.load(open(os.path.join(d, "scope_signoff_request.json")))
        except (OSError, ValueError):
            req = {}
        ok(s == "paused:awaiting_scope_signoff" and load_state(d).state == "OWNER_DECISION"
           and "SPEC" not in p and not any(c.startswith("fix:") for c in T.calls)
           and req.get("pending") and [i.get("finding") for i in req.get("items") or []] == ["D-031"],
           f"R3-3 an owner-amendment finding pauses at OWNER_DECISION with a request, no redraft ({s}, {p[-4:]})")
        import harness_metrics as _HM
        pz = _HM.pauses(_HM.load_run(d))
        ok([(x["reason"], x["state"], x["from"]) for x in pz][-1:] == [("awaiting_scope_signoff", "OWNER_DECISION",
                                                                        "record")],
           f"RR3 a scope pause writes a history entry; harness_metrics reads it as a record ({pz[-1:]})")
        if req.get("pending"):
            import scope_signoff as _SS
            # Codex re-review of #258 P1-8: the request the machine made is what must be answered, not whatever request
            # file is there now
            good_req = open(os.path.join(d, "scope_signoff_request.json")).read()
            json.dump(dict(req, items=[dict(req["items"][0], fingerprint="stale0000000000")]),
                      open(os.path.join(d, "scope_signoff_request.json"), "w"))
            _SS.sign(d, "owner", all_decision="accept", note="stale")
            open(os.path.join(d, "scope_signoff_request.json"), "w").write(good_req)
            s_st = _SpikeMachine(d, StubTasks(T.s), None, log=lambda *a: None, cross_engine_check=False).run()
            ok(s_st == "paused:awaiting_scope_signoff",
               f"R3-3d an answer to a request the machine did not make does not release OWNER_DECISION ({s_st})")
            _SS.sign(d, "lead", all_decision="accept", note="R3-3c")     # Codex review of #258 P1-8: not the owner's
            s_l = _SpikeMachine(d, StubTasks(T.s), None, log=lambda *a: None, cross_engine_check=False).run()
            ok(s_l == "paused:awaiting_scope_signoff",
               f"R3-3c an answer that is not the owner's does not release OWNER_DECISION ({s_l})")
            _SS.sign(d, "owner", all_decision="decline", note="R3-3")
            s2 = _SpikeMachine(d, StubTasks(T.s), None, log=lambda *a: None, cross_engine_check=False).run()
            p2 = [h["to"] for h in load_state(d).history if "to" in h]
            ok(s2 == "completed_scope_declined" and "SPEC" not in p2 and FL.load(d)["D-031"].status == "owner_declined"
               and "`completed_scope_declined`" in open(os.path.join(d, "report.md")).read(),
               f"R3-3b the owner's decline resumes the run, is not redrafted, and constrains the gate: the run is not "
               f"declared or accepted (Codex review of #258 P1-11) ({s2}, {p2[-5:]})")
        # W0-F: a held-out target whose automatic test changes the plan pauses the run once at OWNER_DECISION; one
        # whose test changes nothing is a declared limitation, and the run is not interrupted
        def conflict_run(name, changes, **kw):
            d_, T_, st_ = new_run(name, {}, max_rounds=2, **kw)
            os.makedirs(os.path.join(d_, "analysis"), exist_ok=True)
            import yaml as _y
            _y.safe_dump({"data": {"policy": "declare_and_test", "refused": None, "needs_owner": ["g|1"] if changes
                                   else [], "declared": [{"n": 1, "kind": "offset", "key": "g|1", "target": "g",
                                                          "units": ["1"], "why": {}, "tests": ["conflict_1_g_1_lo"],
                                                          "scales": [0.8], "verdicts": {"conflict_1_g_1_lo":
                                                                                        "CHANGES_PLAN" if changes
                                                                                        else "NO_CHANGE"},
                                                          "changes_plan": changes}]}},
                         open(os.path.join(d_, "analysis", "conflicts.yaml"), "w"))
            s_ = _SpikeMachine(d_, T_, st_, log=lambda *a: None, cross_engine_check=False).run()
            return d_, T_, s_
        dq, Tq, sq = conflict_run("conflict_change", True)
        try:
            rq = json.load(open(os.path.join(dq, "scope_signoff_request.json")))
        except (OSError, ValueError):
            rq = {}
        ok(sq == "paused:awaiting_scope_signoff" and [i.get("finding") for i in rq.get("items") or []]
           == ["evidence_conflict_declared:g|1"] and "held-out" in rq["items"][0]["reason"]
           and any("refit without the target" in o for o in rq["items"][0].get("options") or []),
           f"T_CONFLICT_PLAN_CHANGE_PAUSES_ONCE a conflict whose test changes the plan pauses at OWNER_DECISION ({sq})")
        if rq.get("pending"):
            import scope_signoff as _SS
            _SS.sign(dq, "owner", all_decision="accept", note="W0-F")
            sq2 = _SpikeMachine(dq, StubTasks(Tq.s), None, log=lambda *a: None, cross_engine_check=False).run()
            nq = sum(1 for h in load_state(dq).history if h.get("to") == "OWNER_DECISION")
            ok(sq2 in ACCEPTED_STATUSES and nq == 1
               and "Held-out calibration targets" in open(os.path.join(dq, "report.md")).read(),
               f"T_CONFLICT_PLAN_CHANGE_PAUSES_ONCE the owner's accept resumes the run, which is not paused again, and "
               f"the report lists the held-out target ({sq2}, {nq} pause(s))")
        # T_CONFLICT_LEAD_FLAGGED (Codex r4 of #316): under --signoff lead a conflict whose test changes the plan is
        # answered provisionally and flagged as plan-changing: the request item carries the conflict's structured
        # verdict, and the report names it above the draft and labels it "plan changes: yes"
        dl, Tl, sl = conflict_run("conflict_change_lead", True, signoff="lead")
        try:
            rl = json.load(open(os.path.join(dl, "scope_signoff_request.json")))
        except (OSError, ValueError):
            rl = {}
        it_l = next((i for i in rl.get("items") or [] if i.get("finding") == "evidence_conflict_declared:g|1"), {})
        rep_cl = open(os.path.join(dl, "report.md")).read() if os.path.exists(os.path.join(dl, "report.md")) else ""
        try:
            span_cl = json.load(open(os.path.join(dl, "report_render.json")))["writer_span"]
        except (OSError, ValueError, KeyError):
            span_cl = [0, 0]
        top_cl = rep_cl[:span_cl[0]]
        ok(not sl.startswith("paused") and (it_l.get("conflict") or {}).get("status") == "CHANGES_PLAN"
           and (it_l.get("conflict") or {}).get("verdicts") == {"conflict_1_g_1_lo": "CHANGES_PLAN"}
           and "Provisional decisions that change the plan" in top_cl and "evidence_conflict_declared:g|1" in top_cl
           and "**evidence_conflict_declared:g|1**: accepted provisionally; plan changes: yes" in rep_cl[span_cl[1]:],
           f"T_CONFLICT_LEAD_FLAGGED a lead run flags a plan-changing conflict above the draft, with its verdict in the "
           f"request ({sl}, {it_l.get('conflict')}, top has it: {'evidence_conflict_declared' in top_cl})")
        # T_CONFLICT_OLD_REQUEST (Codex r5 of #316): a conflict item saved before the verdict was carried (a run resumed
        # across this change) is plan-changing: the owner request holds only conflicts that change the plan or could
        # not be tested
        ok(_plan_changing({"finding": "evidence_conflict_declared:g|1", "kind": "evidence_conflict_declared"}) is True
           and _plan_changing({"kind": "evidence_conflict_declared", "conflict": {"changes_plan": False}}) is False,
           "T_CONFLICT_OLD_REQUEST a conflict item without its verdict counts as changing the plan")
        dn, Tn, sn = conflict_run("conflict_nochange", False)
        ok(sn in ACCEPTED_STATUSES and not os.path.exists(os.path.join(dn, "scope_signoff_request.json"))
           and FL.load(dn)["evidence_conflict_declared:g|1"].status == "declared"
           and "Held-out calibration targets" in open(os.path.join(dn, "report.md")).read(),
           f"T_CONFLICT_NO_CHANGE_NO_PAUSE a conflict whose test changes nothing is declared, listed, never paused ({sn})")
        # R3-6 (Codex re-review of #258): a declared finding still needs the owner's answer (P1-10), and a decline of a
        # finding its test has since resolved does not hold the gate (new P1-2)
        f6d = FL.Finding(id="D-036", kind="critique_blocker", severity="HIGH", source="critique-domain", status="declared",
                         fix_requires="Owner amendment: x")
        f6x = FL.Finding(id="D-037", kind="critique_blocker", severity="HIGH", source="critique-domain",
                         status="owner_declined", resolution_type="owner_decision")
        ok(pending_amendments({"D-036": f6d}) == ["D-036"]
           and apply_owner_declines({"action": "ACCEPT", "resolved_by_test": ["D-037"]}, {"D-037": f6x})["action"]
           == "ACCEPT" and apply_owner_declines({"action": "ACCEPT"}, {"D-037": f6x})["action"] == "RUN_FAILED",
           "R3-6 a declared amendment is asked; a declined finding its test resolved does not hold the gate")
        # R3-7 (Codex round 3 of #258): a declared amendment whose test passed is not asked (5); a declined finding
        # leaves declared_findings.yaml (1); a pause with no recorded request fails loudly, never waits (2)
        ok(pending_amendments({"D-036": f6d}, resolved=["D-036"]) == [],
           "R3-7a a declared amendment whose test passes is not sent to the owner")
        import yaml as _y7
        d7 = os.path.join(td, "r37")
        os.makedirs(d7)
        _y7.safe_dump({"rule": "3m", "declared": [{"id": "D-037"}, {"id": "D-038"}]},
                      open(os.path.join(d7, DECLARED_FILE), "w"))
        drop_declined(d7, ["D-037"])
        ok([x["id"] for x in _y7.safe_load(open(os.path.join(d7, DECLARED_FILE)))["declared"]] == ["D-038"],
           "R3-7b a finding the owner declined is no longer listed as declared")
        d, T, st = new_run("noreq", {})
        st.state = "OWNER_DECISION"
        save_state(d, st)
        _SpikeMachine(d, T, None, log=lambda *a: None, cross_engine_check=False).step()   # the handler itself
        s7 = load_state(d).status
        ok(s7 == "run_failed" and "relaunch with reuse" in str(load_state(d).history[-1].get("why")),
           f"R3-7c a pause at OWNER_DECISION with no recorded request fails with how to go on ({s7})")
        # R3-7e (narrow Codex review of #272): a resume whose preconditions fail ends the run run_failed before any
        # handler runs; an earlier report.md is not left deliverable under that status; malformed signing evidence is
        # "unreadable", never a crash or "present"
        d, T, st = new_run("noreq_resume", {})
        st.state = "OWNER_DECISION"
        save_state(d, st)
        open(os.path.join(d, "report.md"), "w").write("# Report\n\nStatus: completed\n")
        s7e = _SpikeMachine(d, T, None, log=lambda *a: None, cross_engine_check=False).run()
        ok(s7e == "run_failed" and not os.path.exists(os.path.join(d, "report.md"))
           and os.path.exists(os.path.join(d, "report_withheld.md")),
           f"R3-7e a run failed on resume does not leave an earlier report deliverable ({s7e})")
        d7f = os.path.join(td, "r37f")
        os.makedirs(d7f)
        ok_hex = "a" * 64
        json.dump({"spec_sha256": None, "sha256": {AB.SPEC_FILE: ok_hex}}, open(os.path.join(d7f, SIGNOFF_FILE), "w"))
        ok(_spec_signed_sha(d7f) == ok_hex, "R3-7g a null spec_sha256 beside a valid nested digest is still signed")
        for bad in ({"sha256": "invalid"}, {"spec_sha256": "invalid"}, ["x"], {"spec_sha256": None}):
            json.dump(bad, open(os.path.join(d7f, SIGNOFF_FILE), "w"))
            ok(_spec_signed_sha(d7f) == "unreadable", f"R3-7f a malformed sign-off record reads unreadable ({bad})")
        # R3-5 (Codex review of #258 P1-10): the amendment check runs on every gate route: a structural PLAN blocker
        # whose remedy is an owner amendment does not take GATE -> SPEC
        amend5 = dict(amend, id="D-035", category="STRUCTURAL")
        d, T, st = new_run("amend5", {"critics": {1: {"critique-domain": [amend5]}}, "structural": {1: "critique-domain"}})
        s = _SpikeMachine(d, T, st, log=lambda *a: None, cross_engine_check=False).run()
        p = [h["to"] for h in load_state(d).history if "to" in h]
        ok(s == "paused:awaiting_scope_signoff" and "SPEC" not in p and p[-1] == "OWNER_DECISION",
           f"R3-5 a structural blocker that needs an owner amendment goes to the owner, not to SPEC ({s}, {p[-3:]})")

        # T_OWNER_DECISION_E2E (1F): a decision test that changes the plan, at the last round, goes to OWNER_DECISION
        # with a request the machine recorded; the owner's accept resumes the run, which ends scope-declared
        import scope_signoff as _SS
        dtf_e = {"kind": "decision_test_changes_plan", "severity": "HIGH", "stage": "ANALYZE", "key": "DT9",
                 "claim": "Decision test DT9 changes the plan: a lower effect", "scope_declarable": True,
                 "decision_bearing": True}

        class _DTMachine(Machine):
            def harness_findings(self):
                return super().harness_findings() + [dict(dtf_e)]
        mk = lambda d_, T_, st_: _DTMachine(d_, T_, st_, backend=spike, log=lambda *a: None,  # noqa: E731
                                            cross_engine_check=False)
        d, T, st = new_run("dt_owner", {}, signoff="owner", max_rounds=1)
        mk(d, T, st).run()
        _t_owner_disposes(d)        # W0-G (from v2): the owner disposes the spec critique's conditions before signing
        sign(d, "owner", "ok")
        s_o = mk(d, StubTasks(T.s), None).run()
        st_o = load_state(d)
        req_o = json.load(open(os.path.join(d, OWNER_REQUEST))) if os.path.exists(os.path.join(d, OWNER_REQUEST)) else {}
        fps_o = [i.get("fingerprint") for i in req_o.get("items") or []]
        ok(s_o == "paused:awaiting_scope_signoff" and st_o.state == "OWNER_DECISION" and req_o.get("pending")
           and fps_o and st_o.tries.get("owner_request") == fps_o
           and [i.get("finding") for i in req_o["items"]] == ["decision_test_changes_plan:DT9"],
           f"T_OWNER_DECISION_E2E the AWAIT_OWNER route records a fingerprinted request and pauses ({s_o}, {fps_o})")
        _SS.sign(d, "owner", all_decision="accept", note="the plan stands with this caveat")
        s_o2 = mk(d, StubTasks(T.s), None).run()
        rep_o = open(os.path.join(d, "report.md")).read() if os.path.exists(os.path.join(d, "report.md")) else ""
        ok(s_o2 == "completed_scope_declared" and "DT9" in rep_o and "Provisional decisions" not in rep_o,
           f"T_OWNER_DECISION_E2E2 the owner's accept resumes the run, which ends scope-declared ({s_o2})")
        # T_OWNER_LEAD_PROVISIONAL (1F, D2): under --signoff lead the same finding never waits: the lead answers it
        # provisionally, the answer is recorded and listed, and, as the plan changes, flagged above the report's draft
        d, T, st = new_run("dt_lead", {}, signoff="lead", max_rounds=1)
        held, real_hr = [], _SS.harness_record

        def _hr(rd):     # T_OWNER_LEAD_LOCK (Codex r4 of #316): the lock is held while the answers are recorded
            held.append(("record", getattr(_SS, "lock_held", lambda _r: "no lock_held")(rd)))
            return real_hr(rd)
        _SS.harness_record = _hr
        try:
            s_l = mk(d, T, st).run()
        finally:
            _SS.harness_record = real_hr
        ok(held and all(h == ("record", True) for h in held),
           f"T_OWNER_LEAD_LOCK the harness records the lead's provisional answers holding the sign-off lock, so an "
           f"owner sign that waited on it sees the request closed ({held})")
        prov = _SS.provisional(d)
        rep_l = open(os.path.join(d, "report.md")).read() if os.path.exists(os.path.join(d, "report.md")) else ""
        span = json.load(open(os.path.join(d, "report_render.json")))["writer_span"] if rep_l else [0, 0]
        ok(s_l == "completed_scope_declared" and not any(h.get("paused") for h in load_state(d).history)
           and [(r["finding"], r["decision"], r["plan_changing"], r["review"]) for r in prov]
           == [("decision_test_changes_plan:DT9", "accept", True, None)]
           and "Provisional decisions that change the plan" in rep_l[:span[0]]
           and "## Provisional decisions" in rep_l[span[1]:],
           f"T_OWNER_LEAD_PROVISIONAL a lead run answers the owner decision provisionally and never blocks; the "
           f"plan-changing answer heads the report ({s_l}, {prov})")
        _SS.review(d, "decision_test_changes_plan:DT9", "overturn", note="fix the effect first")
        ok([r["review"]["decision"] for r in _SS.provisional(d)] == ["overturn"],
           "T_OWNER_LEAD_REVIEW the owner later overturns (or confirms) a provisional answer; it is recorded")
        # T_DT_TOOLKIT_FP (lead-gaps α): a toolkit finding's owner item binds the verified row of
        # analysis/decision_tests.yaml, so a changed result is asked again; a stale spike analysis.json in the run dir
        # never stands in for it, and a spike finding still binds analysis.json (Codex r1 of #327)
        from toolkit.analysis import contract as _CTk
        _rec = _CTk.decision_test_record
        dtf_t = dict(dtf_e, source="toolkit/analysis/contract.py")
        try:
            with tempfile.TemporaryDirectory() as dk:
                os.makedirs(os.path.join(dk, os.path.dirname(AB.ANALYSIS_JSON)), exist_ok=True)
                json.dump({"decision_tests": [{"id": "DT9", "plan_changes": True, "text": "stale"}]},
                          open(os.path.join(dk, AB.ANALYSIS_JSON), "w"))
                _CTk.decision_test_record = lambda rd, k: {"test_id": k, "verdict": "CHANGES_PLAN", "share_pct": 40.0}
                fa = _bearing_item(dk, dtf_t)["fingerprint"]
                _CTk.decision_test_record = lambda rd, k: {"test_id": k, "verdict": "CHANGES_PLAN", "share_pct": 60.0}
                fb = _bearing_item(dk, dtf_t)["fingerprint"]
                _CTk.decision_test_record = lambda rd, k: None
                fc = _bearing_item(dk, dtf_t)["fingerprint"]
                fs1 = _bearing_item(dk, dtf_e)["fingerprint"]
                json.dump({"decision_tests": [{"id": "DT9", "plan_changes": True, "text": "new"}]},
                          open(os.path.join(dk, AB.ANALYSIS_JSON), "w"))
                fs2 = _bearing_item(dk, dtf_e)["fingerprint"]
                fc2 = _bearing_item(dk, dtf_t)["fingerprint"]
        finally:
            _CTk.decision_test_record = _rec
        ok(len({fa, fb, fc}) == 3 and fc2 == fc and fs1 != fs2,
           f"T_DT_TOOLKIT_FP each backend's finding binds its own decision-test record ({fa}, {fb}, {fc}, {fs1}, {fs2})")
        # T_OWNER_LEAD_AMEND (1F, D2): an owner-amendment finding under --signoff lead is answered provisionally too
        d, T, st = new_run("amend_lead", {"critics": {1: {"critique-domain": [dict(amend, id="D-041")]}}},
                           signoff="lead", max_rounds=2)
        s_a = _SpikeMachine(d, T, st, log=lambda *a: None, cross_engine_check=False).run()
        led_a = FL.load(d)
        ok(not s_a.startswith("paused") and led_a["D-041"].status == "owner_accepted"
           and [r["finding"] for r in _SS.provisional(d)] == ["D-041"]
           and not _SS.provisional(d)[0]["plan_changing"],
           f"T_OWNER_LEAD_AMEND an amendment finding under --signoff lead is accepted provisionally, not waited on "
           f"({s_a}, {led_a['D-041'].status})")

        # R3-4 (F6 run 3): a paid task whose allowance is below the floor is not started: the run pauses for the owner
        # (resumable), and a resume with a higher cap continues.
        d, T, st = new_run("floor", {}, budget=20.0)
        st.spent_usd = 20.0 - 10.0 - 0.13            # cap less the write-up reserve ($10) leaves $0.13
        save_state(d, st)
        s = _SpikeMachine(d, T, st, log=lambda *a: None, cross_engine_check=False).run()
        ok(s == "paused:awaiting_budget" and T.calls == [] and load_state(d).state == "SPEC"
           and load_state(d).status is None,
           f"R3-4 a $0.13 allowance starts no task; the run pauses awaiting budget ({s}, {T.calls}, {T.allowances})")
        T2 = StubTasks(T.s)
        mp4 = os.path.join(d, "metadata.json")
        json.dump(dict(json.load(open(mp4)), invocations=[{"at": "2020-01-01T00:00:00"}, {"at": "2020-01-01T01:00:00"}]),
                  open(mp4, "w"))
        s2 = run_v2("toy question", d, T2, signoff="none", budget_usd=60.0, log=lambda *a: None,
                    backend=spike, cross_engine_check=False)
        m4, h4 = json.load(open(mp4)), load_state(d).history
        ok(m4["invocations"][-1].get("budget_usd") == 60.0 and m4["invocations"][-1].get("cap_usd") == 60.0
           and "budget_usd" not in m4["invocations"][0] and set(m4.get("models") or {}) == {"deep", "fast"}
           and m4.get("sdk_version") and m4["invocations"][-1].get("models") == m4["models"]
           and sum(1 for h in h4 if h.get("paused") == AWAITING_BUDGET) == 1,
           f"RR4 each invocation records its budget, the cap in force, the model ids and the SDK version; the budget "
           f"pause has one entry ({m4['invocations'][-1]}, {m4.get('sdk_version')})")
        dp = os.path.join(td, "rr5_child")
        os.environ["MA_PARENT_RUN"] = d
        try:
            init_run(dp, "toy question", derived_inputs=False)
        finally:
            os.environ.pop("MA_PARENT_RUN", None)
        pr = json.load(open(os.path.join(dp, "metadata.json"))).get("parent_run") or {}
        ok(pr.get("run_dir") == os.path.basename(d) and pr.get("run_state_sha256") == hashlib.sha256(
            open(os.path.join(d, STATE_FILE), "rb").read()).hexdigest(),
           f"RR5 a run launched with MA_PARENT_RUN records its parent and the parent's run_state hash ({pr})")
        cwd6 = os.getcwd()
        prs = []
        for k6, par in enumerate((os.path.join(td, "no_such_run"), ".")):
            os.chdir(d)
            os.environ["MA_PARENT_RUN"] = par
            try:
                init_run(os.path.join(td, f"rr6_child{k6}"), "toy question", derived_inputs=False)
            finally:
                os.environ.pop("MA_PARENT_RUN", None)
                os.chdir(cwd6)
            prs.append(json.load(open(os.path.join(td, f"rr6_child{k6}", "metadata.json"))).get("parent_run") or {})
        ok(prs[0].get("run_state_sha256") is None and prs[0].get("error") and prs[1].get("run_dir") == os.path.basename(d),
           f"RR6 an unreadable parent records no hash (never linked); a relative parent path records its dir name ({prs})")
        st7 = load_state(d)
        if st7.state == "FINAL":
            st7.paused = "awaiting_scope_signoff"            # a stale flag a terminal run can carry
            save_state(d, st7)
            n7 = len(load_state(d).history)
            _SpikeMachine(d, StubTasks(T.s), st7, log=lambda *a: None, cross_engine_check=False).run()
            ok(len(load_state(d).history) == n7, "RR7 a FINAL run carrying a stale paused flag writes no pause entry")
        else:
            ok(False, f"RR7 fixture: the resumed run should be FINAL ({st7.state})")
        ok(not s2.startswith("paused") and "draft_spec" in T2.calls
           and load_state(d).budget_usd == 60.0
           and any(h.get("budget_raised") for h in load_state(d).history),
           f"R3-4b resumed with a higher cap, the run continues ({s2}, cap {load_state(d).budget_usd})")

        # R3-8 (fix 4 of Codex round 3 on #258): the decline override reads the ledger synced with this gate's decision,
        # so a gate-supplied finding the owner declined, and that this gate states with a revised claim, is not held by
        # the old decline: the answer was given to the old claim, so the finding is open again
        import scope_signoff as _SS8
        d, T, st = new_run("stale_decline", {})
        st.state = "GATE"
        save_state(d, st)
        v8 = {"kind": "k_demo", "key": "x", "severity": "HIGH", "stage": "ANALYZE", "scope_declarable": True,
              "claim": "the new claim", "fix_requires": "use the new data"}
        fid8 = FL.gate_id(v8)
        old = FL.Finding(id=fid8, kind="k_demo", key="x", severity="HIGH", source="rigor-artifacts", claim="the old claim",
                         fix_requires="use the new data", status="owner_declined", resolution_type="owner_decision")
        old.events.append({"round": 1, "status": "owner_declined", "by": "owner"})
        FL.save(d, {fid8: old})
        fp8 = FL.answer_fingerprint(fid8, "the old claim", "use the new data")
        json.dump({"answers": {fp8: {"finding": fid8, "decision": "decline", "by": "owner"}}},
                  open(os.path.join(d, SCOPE_SIGNOFF), "w"))
        _SS8.harness_record(d)
        _mod8 = _SM
        _vg = _mod8.v2_gate
        _mod8.v2_gate = lambda *a, **k: {"action": "DECLARE_SCOPE", "rule_matched": "3", "rounds_remaining": 0,
                                         "rigor_violations": [v8], "unresolved_high": [dict(v8)]}
        try:
            _SpikeMachine(d, T, None, log=lambda *a: None, cross_engine_check=False).step()
        finally:
            _mod8.v2_gate = _vg
        g8 = json.load(open(os.path.join(d, "gate_r1.json")))
        f8 = FL.load(d)[fid8]
        ok(g8.get("action") == "DECLARE_SCOPE" and g8.get("rule_matched") != "owner_declined" and f8.status == "open"
           and f8.claim == "the new claim",
           f"R3-8 a declined gate finding restated with a revised claim is not held by the old decline "
           f"({g8.get('rule_matched')}, {FL.load(d)[fid8].status})")

        # T29: the deliverable destination must be a plain file inside the run, not one the harness owns.
        bad_paths = {}
        for name, path in (("escape", "../escape.csv"), ("owned", "run_state.json"), ("report", "report.md"),
                           ("link", "out/link.csv")):
            d, T, st = new_run(f"deliv_{name}", {"spec_extra": {"deliverable_file": {
                "path": path, "columns": {"unit": "id", "package": "package"}}}}, max_rounds=1)
            if name == "link":
                os.makedirs(os.path.join(td, "elsewhere"), exist_ok=True)
                os.symlink(os.path.join(td, "elsewhere"), os.path.join(d, "out"))
            s = _SpikeMachine(d, T, st, log=lambda *a: None, cross_engine_check=False).run()
            led = FL.load(d)
            bad_paths[name] = ("deliverable_path_invalid:deliverable" in led and s == "run_failed"
                               and not os.path.exists(os.path.join(td, "escape.csv"))
                               and not os.path.exists(os.path.join(td, "elsewhere", "link.csv")))
        ok(all(bad_paths.values()), f"T29 a deliverable outside the run, owned or through a symlink is refused {bad_paths}")

        # T30: a measured decision test that changes the plan cannot be declared without the owner rule.
        import validate_critique_yaml as V
        dd = os.path.join(td, "dt")
        os.makedirs(dd)
        for r in CRITICS:
            __import__("yaml").safe_dump({"reviewer": r, "round": 2, "verdict": "PASS", "structural_mismatch": {"detected": False},
                                          "carried_forward": [], "blockers": []}, open(os.path.join(dd, V.FILENAMES[r]), "w"))
        dtf = {"kind": "decision_test_changes_plan", "severity": "HIGH", "stage": "ANALYZE", "key": "DT1",
               "claim": "DT1 changes the plan", "scope_declarable": True, "decision_bearing": True}
        g = _SM.v2_gate(dd, 2, 2, False, [dict(dtf)], history=False)
        own = [b for b in list(g.get("unresolved_high") or []) + list(g.get("accept_blockers") or [])
               if isinstance(b, dict) and b.get("owner_signoff")]
        ok(OWNER_LIFECYCLE_WIRED and _owner_rule() is not None and g["action"] == "AWAIT_OWNER" and own
           and (own[0].get("owner_item") or {}).get("fingerprint"),
           f"T30 (1F) a measured decision test that changes the plan goes to the owner: AWAIT_OWNER with a "
           f"fingerprinted item ({g['action']})")
        gn = _SM.v2_gate(dd, 2, 2, False, [dict(dtf)], history=False, signoff="none")
        ok(gn["action"] == "RUN_FAILED", f"T30b under --signoff none nobody can accept it: not declarable ({gn['action']})")
        # T_OWNER_ANSWERED (1F): the harness-recorded answer to that item releases it; whose answer counts follows the
        # run's sign-off mode (a lead's only under --signoff lead, D2)
        import scope_signoff as _ss
        _ss.record(dd, g, 2)
        _ss.sign(dd, "lead", all_decision="accept", note="t")          # not provisional: counts nowhere
        _ss.harness_record(dd)
        g_ln = _SM.v2_gate(dd, 2, 2, False, [dict(dtf)], history=False, signoff="lead")
        _ss.provisional_answers(dd, json.load(open(os.path.join(dd, _ss.REQUEST)))["items"], 2)
        _ss.harness_record(dd)
        g_lo = _SM.v2_gate(dd, 2, 2, False, [dict(dtf)], history=False)
        g_ll = _SM.v2_gate(dd, 2, 2, False, [dict(dtf)], history=False, signoff="lead")
        _ss.sign(dd, "owner", all_decision="decline", note="t")
        _ss.harness_record(dd)
        g_od = _SM.v2_gate(dd, 2, 2, False, [dict(dtf)], history=False)
        ok(g_ln["action"] == "AWAIT_OWNER" and g_lo["action"] == "AWAIT_OWNER" and g_ll["action"] == "DECLARE_SCOPE"
           and g_od["action"] == "RUN_FAILED"
           and any(isinstance(u, dict) and u.get("kind") == "scope_declaration_declined"
                   for u in g_od.get("unresolved_high") or []),
           f"T_OWNER_ANSWERED a lead's accept counts only under --signoff lead; the owner's decline holds the gate "
           f"({g_lo['action']}, {g_ll['action']}, {g_od['action']})")
        # T_OWNER_DECLINE_HOLDS and T_OWNER_LEAD_NONPROV (Codex r2 of #316, P1-2 and P1-3): with a policy-mapped
        # finding beside the decision test, the owner's decline on it keeps holding the gate once the decision-test
        # finding clears; and a lead answer that is not provisional never releases it, even in a lead run
        for f_ in (_ss.SIGNOFF, _ss.LEDGER, _ss.REQUEST, "trace.jsonl"):
            if os.path.exists(os.path.join(dd, f_)):
                os.remove(os.path.join(dd, f_))
        open(os.path.join(dd, "decision_rule.md"), "w").write("rule\n")          # the run allocates
        alloc = {"kind": "allocation_unstable", "severity": "HIGH", "stage": "OPTIMIZATION",
                 "key": "allocation_robustness", "claim": "UNSTABLE", "scope_declarable": True}
        g2 = _SM.v2_gate(dd, 2, 2, False, [dict(dtf), dict(alloc)], history=False)
        _ss.record(dd, g2, 2)
        its = {i["kind"]: i for i in json.load(open(os.path.join(dd, _ss.REQUEST)))["items"]}
        both = "decision_test_changes_plan" in its and "allocation_unstable" in its
        if both:
            _ss.sign(dd, "owner", {its["decision_test_changes_plan"]["fingerprint"]: "accept",
                                   its["allocation_unstable"]["fingerprint"]: "decline"}, note="t")
            _ss.harness_record(dd)
        g_both = _SM.v2_gate(dd, 2, 2, False, [dict(dtf), dict(alloc)], history=False)
        g_alone = _SM.v2_gate(dd, 2, 2, False, [dict(alloc)], history=False)
        ok(both and g2["action"] == "AWAIT_OWNER" and g_both["action"] == "RUN_FAILED"
           and g_alone["action"] == "RUN_FAILED",
           f"T_OWNER_DECLINE_HOLDS the owner's decline of a policy-mapped finding holds the gate when the decision-test "
           f"finding clears ({g2['action']}, {g_both['action']}, {g_alone['action']}, {sorted(its)})")
        if both:
            doc_ = json.load(open(os.path.join(dd, _ss.SIGNOFF)))
            doc_["answers"][its["allocation_unstable"]["fingerprint"]].update(decision="accept", by="lead")
            json.dump(doc_, open(os.path.join(dd, _ss.SIGNOFF), "w"))
            _ss.harness_record(dd)
        g_np = _SM.v2_gate(dd, 2, 2, False, [dict(dtf), dict(alloc)], history=False, signoff="lead")
        ok(both and g_np["action"] != "DECLARE_SCOPE" and _ss.provisional(dd) == [],
           f"T_OWNER_LEAD_NONPROV a lead answer that is not provisional does not release a policy-mapped item, even "
           f"under --signoff lead ({g_np['action']})")
        os.remove(os.path.join(dd, "decision_rule.md"))
        for f_ in (_ss.SIGNOFF, _ss.LEDGER, _ss.REQUEST, "trace.jsonl"):
            if os.path.exists(os.path.join(dd, f_)):
                os.remove(os.path.join(dd, f_))
        seen = {}

        class Rule:
            @staticmethod
            def gate_findings(rd, rn, viols, non_declarable=None, mode=None, **k):
                seen["marked"] = [v.get("owner_signoff") for v in viols if v.get("kind") == "decision_test_changes_plan"]
                return []
        vs = [dict(dtf)]
        apply_owner_rule(dd, 2, vs, rule=Rule)
        ok(seen.get("marked") == [True] and vs[0].get("owner_item"), "T31 with the owner rule the finding goes to it, marked for the owner")

        class Broken:
            @staticmethod
            def gate_findings(*a, **k):
                raise RuntimeError("x")
        vs = [dict(dtf)]
        apply_owner_rule(dd, 2, vs, rule=Broken)
        ok(vs[0]["scope_declarable"] is False and not vs[0].get("owner_signoff"), "T32 an owner rule that fails is fail-closed")

        class Declines:
            @staticmethod
            def gate_findings(rd, rn, viols, non_declarable=None, mode=None, **k):
                for v in viols:
                    v["owner_signoff"] = True
                return [{"kind": "scope_declaration_declined", "key": "allocation_unstable"}, dict(viols[0])]
        vs = [{"kind": "allocation_unstable", "severity": "HIGH", "claim": "c"}]
        got = apply_owner_rule(dd, 2, vs, rule=Declines)
        ok([f["kind"] for f in got] == ["scope_declaration_declined"] and not vs[0].get("owner_signoff")
           and apply_owner_rule(dd, 2, [dict(vs[0])], rule=Declines, mode="none") == [],
           "T32b (Codex r2 of #316) a recorded decline holds the gate without a decision-bearing finding; nothing new "
           "is put to the owner there")

        # T33 (Codex R2-1): the first spec draft is interrupted. On resume the started-but-uncompleted
        # entry is charged its allowance once and the draft runs again under the same key.
        d, T, st = new_run("spec_crash", {"crash": {"draft_spec": 1}, "usd": 1.0}, budget=100.0)
        try:
            _SpikeMachine(d, T, st, log=lambda *a: None, cross_engine_check=False).run()
        except RuntimeError:
            pass
        T2 = StubTasks(dict(T.s, crash={}))
        m2 = _SpikeMachine(d, T2, None, log=lambda *a: None, cross_engine_check=False)
        s = m2.run()
        j = load_state(d).journal
        spec_keys = sorted(k for k in j if k.startswith("SPEC#"))
        ok(s == "completed" and spec_keys == ["SPEC#0:try1"] and j["SPEC#0:try1"].get("interrupted_charged") == 1
           and j["SPEC#0:try1"]["status"] == "completed",
           f"T33 an interrupted spec draft is charged once and re-run under its own key {spec_keys}")
        d, T, st = new_run("orphan", {}, budget=100.0)
        st.journal["X#0:orphan"] = {"task": "critique", "status": "started", "allowance_usd": 2.5}
        save_state(d, st)
        m3 = _SpikeMachine(d, StubTasks({}), None, log=lambda *a: None)
        ok(abs(m3.st.spent_usd - 2.5) < 1e-9 and m3.st.journal["X#0:orphan"]["status"] == "interrupted"
           and abs(_SpikeMachine(d, StubTasks({}), None, log=lambda *a: None).st.spent_usd - 2.5) < 1e-9,
           f"T34 on resume every started-but-uncompleted entry is charged once ({m3.st.spent_usd})")

        # T35 (Codex R2-5): case aliases of owned files are refused (case-insensitive filesystems).
        alias = {}
        for name, path in (("state", "RUN_STATE.json"), ("report", "Report.MD"), ("spec", "Decision_Spec.yaml"),
                           ("analysis", "Analysis/x.csv")):
            dd2 = os.path.join(td, f"alias_{name}")
            os.makedirs(dd2)
            __import__("yaml").safe_dump({"deliverable_file": {"path": path, "columns": {"a": "b"}}},
                                         open(os.path.join(dd2, AB.SPEC_FILE), "w"))
            alias[name] = bool(deliverable_problems(dd2))
        os.makedirs(os.path.join(td, "alias_link"))
        __import__("yaml").safe_dump({"deliverable_file": {"path": "out.csv", "columns": {"a": "b"}}},
                                     open(os.path.join(td, "alias_link", AB.SPEC_FILE), "w"))
        os.link(os.path.join(td, "alias_link", AB.SPEC_FILE), os.path.join(td, "alias_link", "out.csv"))
        alias["hardlink"] = bool(deliverable_problems(os.path.join(td, "alias_link")))
        ok(all(alias.values()), f"T35 case aliases and a hard link to an owned file are refused {alias}")

        # T36: the default backend. Once workstream B's analysis engine is on the branch, the
        # machine runs on it by default: B's toy spec with engine: auto, B's sign-off, B's step.
        if type(AB.backend()).__name__ == "_ToolkitApi":
            import yaml
            from toolkit.spec import schema
            src = os.path.join(td, "b_src")
            os.makedirs(src)
            raw = yaml.safe_load(open(schema.toy_spec(src)))
            raw["engine"] = "auto"
            for k in ("draws", "seed"):
                (raw.get("uncertainty") or {}).pop(k, None)
            em = (raw.get("analysis") or {}).get("emulator") or {}
            pts, eseed = em.pop("points", 24), em.pop("seed", 5)
            (raw.get("analysis") or {}).pop("workers", None)
            d = os.path.join(td, "b_run")
            os.makedirs(d)
            for f in os.listdir(src):
                if f != AB.SPEC_FILE and os.path.isfile(os.path.join(src, f)):
                    shutil.copy(os.path.join(src, f), d)
            bl = {"id": "D-001", "severity": "HIGH", "target_stage": "ANALYZE", "claim": "too few draws",
                  "done_when": [{"file": "analysis_settings.yaml", "key": "uncertainty.draws", "op": ">=", "value": 12}]}
            T = StubTasks({"spec": raw, "critics": {1: {"critique-domain": [bl]}},
                           "settings": {"uncertainty": {"draws": 6, "seed": 1},
                                        "analysis": {"workers": 2, "emulator": {"points": pts, "seed": eseed}}},
                           "fixes": {"D-001": [("analysis_settings.yaml", "uncertainty.draws", 12)]}})
            st = init_run(d, "toy question", 3, "lead", "v2", None, derived_inputs=False)   # pre-DI fixture
            s = Machine(d, T, st, log=lambda *a: None, cross_engine_check=False).run()
            rows = FX.impact_rows(d)
            man = os.path.exists(os.path.join(d, "analysis", "manifest.json"))
            # lead-gaps α: the toy's automatic decision tests change the plan, so the toolkit backend now emits
            # decision_test_changes_plan for each; this --signoff lead run answers them provisionally (never pausing) and
            # is declared, not accepted, with each flagged above the draft. Everything else T36 checked is unchanged.
            # the expected ids are read from the artifact itself, not through the reader under test (Codex r1 of #327)
            _dt36 = (yaml.safe_load(open(os.path.join(d, "analysis", "decision_tests.yaml"))) or {}).get("data") or {}
            want = sorted(f"decision_test_changes_plan:{t.get('test_id')}" for t in _dt36.get("tests") or []
                          if t.get("verdict") == "CHANGES_PLAN")
            import scope_signoff as _SS36
            prov36 = sorted(r["finding"] for r in _SS36.provisional(d) if r.get("plan_changing"))
            rep36 = open(os.path.join(d, "report.md")).read() if os.path.exists(os.path.join(d, "report.md")) else ""
            sp36 = json.load(open(os.path.join(d, "report_render.json")))["writer_span"] if rep36 else [0, 0]
            exp36 = "completed_scope_declared"
            ok(want and s == exp36 and man and rows and rows[0]["files_changed"] == ["analysis_settings.yaml"]
               and FL.load(d)["D-001"].status == "fixed" and prov36 == want
               and not any(h.get("paused") for h in load_state(d).history)
               and all(w in rep36[:sp36[0]] for w in want),
               f"T36 the default backend (workstream B) runs the machine end to end with engine: auto; each decision "
               f"test that changes the plan is answered provisionally and flagged above the draft ({s}, {want}, {prov36}"
               + ("" if s == exp36 else   # why it ended: CI showed only the status (flaky once on #156 and #160)
                  f"; path {[h.get('to') for h in load_state(d).history if 'to' in h]}; "
                  f"final {[h.get('why') for h in load_state(d).history if h.get('to') == 'FINAL'][-1:]}") + ")")
        else:
            ok(True, "T36 skipped: workstream B's analysis engine is not on this branch")

        # T37 (first real run, v2blind1): critics wrote an invalid category three times because the
        # repair re-ran them without the schema errors. The repair now re-runs only the critics whose
        # YAML failed, and gives each its errors.
        class Sloppy(StubTasks):
            def critique(self, run_dir, reviewer, round_n, feedback=None):
                res = super().critique(run_dir, reviewer, round_n)
                if reviewer == "critique-domain" and not feedback:
                    import yaml
                    p = os.path.join(run_dir, "critique_domain.yaml")
                    doc = yaml.safe_load(open(p))
                    doc["blockers"] = [{"id": "D-001", "severity": "HIGH", "category": "DOMAIN", "target_stage": "ANALYZE",
                                        "first_seen_round": round_n, "claim": "c", "evidence": "e", "fix_requires": "f",
                                        "resolved": False}]
                    doc["verdict"] = "REVISE"
                    yaml.safe_dump(doc, open(p, "w"))
                self.calls.append(f"feedback:{reviewer}:{bool(feedback)}")
                return res
        d, T, st = new_run("schema_repair", {})
        T = Sloppy(T.s)
        s = _SpikeMachine(d, T, st, log=lambda *a: None, cross_engine_check=False).run()
        ok(s in ("completed", "completed_scope_declared") and "feedback:critique-domain:True" in T.calls
           and T.calls.count("critique:critique-redteam:1") == 1,
           f"T37 a schema repair re-runs only the failing critic, with its errors ({s}, {[c for c in T.calls if 'critique' in c][:6]})")

        # T74-T75 (two v2 eval runs, 2026-11-05, both completed_gate_error with no verdict): in round 2
        # the critics marked blockers resolved without resolved_in_round or resolved_evidence and wrote
        # carried_forward as a list of ids. The gate named one error per file, the critic fixed exactly
        # that one, the next surfaced, and the two repairs ran out. The gate now names every error in
        # a file at once, so one repair carries all of them.
        import yaml
        r2 = os.path.join(td, "resolved_shape")
        os.makedirs(r2)
        resolved_b = {"id": "D-001", "severity": "MEDIUM", "category": "HYPOTHESES", "target_stage": "WRITE",
                      "first_seen_round": 1, "claim": "c", "evidence": "e", "fix_requires": "f (done)",
                      "resolved": True, "done_when": [{"file": "manuscript.md", "contains": "draft"}]}
        yaml.safe_dump({"reviewer": "critique-domain", "round": 2, "verdict": "PASS",
                        "structural_mismatch": {"detected": False}, "carried_forward": ["D-001", "D-002"],
                        "blockers": [resolved_b, dict(resolved_b, id="D-002")]},
                       open(os.path.join(r2, "critique_domain.yaml"), "w"), sort_keys=False)
        g = _SM.v2_gate(r2, 2, 3, False, [], history=False, strict_done_when=True)
        dom = [e for e in g.get("schema_errors") or [] if e.startswith("critique_domain.yaml")]
        want = ("D-001 missing 'resolved_in_round'", "D-001 missing 'resolved_evidence'",
                "D-002 missing 'resolved_in_round'", "D-002 missing 'resolved_evidence'",
                "carried_forward entry must be a mapping")
        ok(g.get("action") is None and all(any(w in e for e in dom) for w in want),
           f"T74 the gate names every schema error in a critic's file at once, not the first ({dom})")

        # T76: a refused file's open HIGH/MEDIUM blockers still have their done_when tests checked, so
        # a prose test reaches the same repair as the file's schema errors.
        r3 = os.path.join(td, "refused_done_when")
        os.makedirs(r3)
        yaml.safe_dump({"reviewer": "critique-domain", "round": 2, "verdict": "REVISE",
                        "structural_mismatch": {"detected": False}, "carried_forward": ["D-001"],
                        "blockers": [dict(resolved_b, resolved=False, done_when="the text says so")]},
                       open(os.path.join(r3, "critique_domain.yaml"), "w"), sort_keys=False)
        g = _SM.v2_gate(r3, 2, 3, False, [], history=False, strict_done_when=True)
        dom = [e for e in g.get("schema_errors") or [] if e.startswith("critique_domain.yaml")]
        ok(g.get("action") is None and any("carried_forward entry must be a mapping" in e for e in dom)
           and any("blocker D-001 done_when" in e for e in dom),
           f"T76 a refused file's schema errors and its open blockers' done_when problems reach one repair ({dom})")
        yaml.safe_dump({"reviewer": "critique-domain", "round": 2, "verdict": "REVISE",
                        "structural_mismatch": {"detected": False}, "carried_forward": ["D-001"],
                        "blockers": [dict(resolved_b, resolved=False,
                                          done_when=[{"file": "a.md", "contains": "a{4294967296}"}])]},
                       open(os.path.join(r3, "critique_domain.yaml"), "w"), sort_keys=False)
        try:
            g = _SM.v2_gate(r3, 2, 3, False, [], history=False, strict_done_when=True)
        except Exception as e:                                # noqa: BLE001 - the test is that it does not raise
            g = {"crash": repr(e)}
        dom = [e for e in g.get("schema_errors") or [] if e.startswith("critique_domain.yaml")]
        ok(g.get("action") is None and any("carried_forward" in e for e in dom)
           and any("D-001 done_when" in e for e in dom),
           f"T76b a done_when the checker cannot evaluate is a schema error, not a crash ({g.get('crash') or dom})")

        class Literal(StubTasks):
            """A critic that, like the real ones, fixes exactly the errors it is told and nothing else."""
            def critique(self, run_dir, reviewer, round_n, feedback=None):
                p = os.path.join(run_dir, "critique_domain.yaml")
                if reviewer != "critique-domain" or round_n < 2:
                    return super().critique(run_dir, reviewer, round_n, feedback)
                if not feedback:
                    res = super().critique(run_dir, reviewer, round_n)
                    doc = yaml.safe_load(open(p))
                    for b in doc["blockers"]:
                        b.pop("resolved_in_round", None)
                        b.pop("resolved_evidence", None)
                    doc["carried_forward"] = [c["id"] for c in doc["carried_forward"]]
                    yaml.safe_dump(doc, open(p, "w"))
                    return res
                self.calls.append(f"repair:{len(feedback)}")
                doc = yaml.safe_load(open(p))
                for e in feedback:
                    for b in doc["blockers"]:
                        for key in ("resolved_in_round", "resolved_evidence"):
                            if f"blocker {b['id']} missing '{key}'" in e:
                                b[key] = round_n if key == "resolved_in_round" else "done_when passes"
                    if "carried_forward entry must be a mapping" in e:
                        doc["carried_forward"] = [{"id": i, "prior_round": round_n - 1, "still_present": False,
                                                   "notes": "fixed"} for i in doc["carried_forward"]]
                yaml.safe_dump(doc, open(p, "w"))
                return self._r(f"critique:{reviewer}:{round_n}")
        d, T, st = new_run("resolved_repair", {"critics": {1: {"critique-domain": [blocker]}},
                                               "fixes": {"D-001": [("analysis_settings.yaml", "uncertainty.draws", 32)]}})
        T = Literal(T.s)
        s = _SpikeMachine(d, T, st, log=lambda *a: None, cross_engine_check=False).run()
        ok(s in ("completed", "completed_scope_declared") and "repair:3" in T.calls,
           f"T75 a round-2 critic that resolved a blocker in the wrong shape is repaired in one pass and the "
           f"gate decides ({s}, {[c for c in T.calls if 'repair' in c]})")

        # T38-T41: from the first blind real run (v2blind1). Fixtures: its three critique YAMLs,
        # final_gate.json and claims ledger (tests/fixtures/v2blind1; no question text, no evals content).
        import yaml
        FX_DIR = os.path.join(_SM.REPO, "tests", "fixtures", "v2blind1")
        fb = os.path.join(td, "blind1")
        os.makedirs(fb)
        for f in ("critique_domain.yaml", "critique_redteam.yaml", "critique_presentation.yaml"):
            shutil.copy(os.path.join(FX_DIR, f), fb)
        open(os.path.join(fb, "manuscript.md"), "w").write("draft\n")
        g = _SM.v2_gate(fb, 1, 2, True, [], history=False, strict_done_when=True)
        errs = " ".join(g.get("schema_errors") or [])
        ok(g.get("action") is None and "critique_domain.yaml: blocker D-001 done_when" in errs
           and "critique_presentation.yaml: blocker P-001 done_when" in errs,
           f"T38 a prose done_when is a schema error the critic is asked to repair ({errs[:160]})")
        g = _SM.v2_gate(fb, 1, 2, True, [], history=False, strict_done_when=False)
        ok(g.get("action") == "ACCEPT", f"T38b after the repairs the gate itself still accepts ({g.get('action')})")
        # T39: an open MEDIUM with a valid test goes to the fix loop while rounds remain; else it is declared.
        for f in ("critique_domain.yaml", "critique_redteam.yaml", "critique_presentation.yaml"):
            doc = yaml.safe_load(open(os.path.join(fb, f)))
            for b in doc["blockers"]:
                b["done_when"] = [{"judgement": str(b.get("done_when"))[:200]}]
            yaml.safe_dump(doc, open(os.path.join(fb, f), "w"))
        g = _SM.v2_gate(fb, 1, 2, True, [], history=False, strict_done_when=True)
        opened = _SM.open_findings(fb, g)
        ids = sorted(f["id"] for f in opened if f["tested"])
        ok("D-001" in ids and "P-001" in ids and "R-001" in ids and len(ids) >= 8,
           f"T39 the run's open MEDIUM critic findings are found, with their tests ({ids})")
        a = apply_medium_rule(dict(g), opened, rounds_remaining=1, budget_ok=True)
        b2 = apply_medium_rule(dict(g), opened, rounds_remaining=0, budget_ok=True)
        c2 = apply_medium_rule(dict(g), opened, rounds_remaining=1, budget_ok=False)
        ok(a["action"] == "PATCH_OR_RETHINK" and a["rule_matched"] == "4m" and "before ACCEPT" in a["rationale"]
           and b2["action"] == "DECLARE_SCOPE" and b2["rule_matched"] == "3m" and c2["action"] == "DECLARE_SCOPE"
           and sorted(b2["declared_open"]) == sorted(f["id"] for f in opened),
           f"T39b open findings: fixed while rounds and budget remain, else declared, never accepted ({a['action']}, {b2['action']})")
        # T40: the write-up shows values, not claim ids.
        fr = os.path.join(td, "render")
        os.makedirs(os.path.join(fr, "models"))
        shutil.copy(os.path.join(FX_DIR, "claims_ledger.yaml"), os.path.join(fr, "models", "claims_ledger.yaml"))
        yaml.safe_dump({"schema_version": 1}, open(os.path.join(fr, AB.SPEC_FILE), "w"))
        open(os.path.join(fr, "manuscript.md"), "w").write(
            "The plan averts [CLAIM:headline.point] cases at [CLAIM:cost.allocated].\n")
        render_report(fr, AB)
        rep = open(os.path.join(fr, "report.md")).read()
        ok("[CLAIM:" not in rep and "headline.point" not in rep.split("<!-- harness")[0] and not report_claim_findings(fr),
           f"T40 claim references are rendered inline ({rep.splitlines()[0][:120]})")
        open(os.path.join(fr, "manuscript.md"), "w").write("ITN in `mix.itn` districts; [CLAIM:no.such.claim].\n")
        render_report(fr, AB)
        kinds = [(f["kind"], f["severity"]) for f in report_claim_findings(fr)]
        ok(("report_claim_id_raw", "HIGH") in kinds and ("report_claim_unresolved", "HIGH") in kinds,
           f"T40b a raw claim id or an unresolved reference in the report is a HIGH finding ({kinds})")
        # T41: every input the question names is copied into data/ with its sha256; a spec citing a
        # file outside the run is refused.
        fp = os.path.join(td, "prov")
        os.makedirs(fp)
        staged = stage_inputs(fp, "Inputs: requirements.txt (the pins) and no/such/file.csv.")
        prov = json.load(open(os.path.join(fp, "data", "inputs_provenance.json")))
        src = open(os.path.join(_SM.REPO, "requirements.txt"), "rb").read()
        ok(staged == ["data/inputs/requirements.txt"]
           and prov["inputs"]["data/inputs/requirements.txt"]["sha256"] == hashlib.sha256(src).hexdigest()
           and open(os.path.join(fp, "data", "inputs", "requirements.txt"), "rb").read() == src,
           f"T41 the question's input files are copied into data/inputs/ with their sha256 ({staged})")
        yaml.safe_dump({"decision_units": {"csv": "data/inputs/requirements.txt"}, "calibration": {"targets": {"csv": "../outside/targets.csv"}}},
                       open(os.path.join(fp, AB.SPEC_FILE), "w"))
        probs = record_cited_inputs(fp)
        prov = json.load(open(os.path.join(fp, "data", "inputs_provenance.json")))
        ok(any("../outside/targets.csv" in x and "outside the run" in x for x in probs) and "data/inputs/requirements.txt" in prov["cited"],
           f"T41b cited inputs are recorded with their sha256; one outside the run is refused ({probs})")

        # T41c: a path at the end of a sentence or clause keeps its input (trailing punctuation is
        # stripped before the extension test, for bare, quoted and parenthesised paths).
        qp = _question_paths('Use data/a/one.csv. Also data/b/two.csv, data/c/three.json; '
                             '(data/d/four.yaml) "data/e/five.tsv." \'data/f/six.txt,\' `data/g/seven.csv;` '
                             'and "./data/h/eight.csv").')
        ok(sorted(qp) == ["data/a/one.csv", "data/b/two.csv", "data/c/three.json", "data/d/four.yaml",
                          "data/e/five.tsv", "data/f/six.txt", "data/g/seven.csv", "data/h/eight.csv"],
           f"T41c paths ending in . , ; ) or quotes are kept once each, punctuation stripped ({qp})")
        # T41d (Codex r1 of #332): a quoted name that ends in its extension is kept as written; "../" and "/" are kept
        # (stage_inputs refuses them as outside the repository); a quoted name is not re-scanned for a bare suffix;
        # an apostrophe span naming no file still is
        q2 = _question_paths('Use "[draft] t.csv" and ../requirements.txt. and /requirements.txt; also '
                             '"evals/private requirements.txt." and the program\'s file data/k/nine.csv in the '
                             'district\'s table.')
        ok(q2 == ["[draft] t.csv", "evals/private requirements.txt", "../requirements.txt", "/requirements.txt",
                  "data/k/nine.csv"],
           f"T41d brackets in a quoted name kept; ../ and / kept; no bare suffix from a quoted name; apostrophes "
           f"do not hide a path ({q2})")
        fpq = os.path.join(td, "prov_outside")
        os.makedirs(fpq)
        staged = stage_inputs(fpq, 'Inputs: ../requirements.txt. and "evals/private requirements.txt."')
        ok(staged == [], f"T41d a path outside the repo or into the evaluation tree stages nothing ({staged})")
        fpp = os.path.join(td, "prov_punct")
        os.makedirs(fpp)
        staged = stage_inputs(fpp, "The pins are in requirements.txt.")
        ok(staged == ["data/inputs/requirements.txt"], f"T41c a file named at the end of a sentence is staged ({staged})")

        # T42: through the machine. A MEDIUM with a runnable test goes to the fix loop (rule 4m) and
        # is fixed before ACCEPT; one that cannot be fixed, with no rounds left, is declared (3m) and
        # the report says so.
        med = {"id": "D-010", "severity": "MEDIUM", "target_stage": "ANALYZE", "claim": "more draws",
               "done_when": [{"file": "analysis_settings.yaml", "key": "uncertainty.draws", "op": ">=", "value": 40}]}
        d, T, st = new_run("medium_fix", {"critics": {1: {"critique-domain": [med]}},
                                          "fixes": {"D-010": [("analysis_settings.yaml", "uncertainty.draws", 40)]}})
        s = _SpikeMachine(d, T, st, log=lambda *a: None, cross_engine_check=False).run()
        g1 = json.load(open(os.path.join(d, "gate_r1.json")))
        ok(s == "completed" and g1["action"] == "PATCH_OR_RETHINK" and g1["rule_matched"] == "4m"
           and FX.impact_rows(d) and FX.impact_rows(d)[0]["findings"] == ["D-010"],
           f"T42 an open MEDIUM with a test is fixed in the loop before ACCEPT ({s}, {g1['action']} {g1['rule_matched']})")
        d, T, st = new_run("medium_declare", {"critics": {1: {"critique-domain": [dict(med, id="D-011")]}}}, max_rounds=1)
        s = _SpikeMachine(d, T, st, log=lambda *a: None, cross_engine_check=False).run()
        g1 = json.load(open(os.path.join(d, "gate_r1.json")))
        rep = open(os.path.join(d, "report.md")).read()
        ok(s == "completed_scope_declared" and g1["rule_matched"] == "3m" and "D-011" in rep
           and "Open findings, declared" in rep,
           f"T42b with no rounds left the open MEDIUM is declared, and the report says so ({s}, {g1['rule_matched']})")

        # T43-T51: Codex round 1 on #95 (each failed on a3a31b7).
        d43 = os.path.join(td, "t43")
        os.makedirs(d43)
        g43 = {"action": "ACCEPT", "rigor_violations": [
            {"kind": "done_when_unmet", "severity": "MEDIUM", "key": "D-001", "claim": "the test fails"},
            {"kind": "critique_presentation_missing", "severity": "MEDIUM", "claim": "no review"}]}
        ok({o["id"] for o in _SM.open_findings(d43, g43)} == {"done_when_unmet:D-001", "critique_presentation_missing"},
           "T43 a resolved blocker whose test fails, and a missing presentation review, stay open")
        # OF1-OF2 (external review 2026-11-17): an unreadable reviewer YAML is fail-closed. The gate refuses to
        # decide (a schema error the critic is asked to repair; once the repairs are used the run ends
        # completed_gate_error), so open_findings, which runs only on a gate action, never sees it; and
        # open_findings itself reports such a file as an open HIGH, never skips it or raises.
        for tag, body in (("garbled", "blockers: [unclosed\n  : -\n"), ("list", "- a\n- b\n"), ("text", "just prose\n"),
                          ("empty", ""), ("no_blockers", "reviewer: critique-domain\n"), ("false", "blockers: false\n")):
            dof = os.path.join(td, f"of_{tag}")
            os.makedirs(dof)
            shutil.copy(os.path.join(FX_DIR, "critique_redteam.yaml"), dof)
            open(os.path.join(dof, "critique_domain.yaml"), "w").write(body)
            gof = _SM.v2_gate(dof, 1, 2, False, [], history=False)
            ok(gof.get("action") is None and any("critique_domain.yaml" in str(e) for e in gof.get("schema_errors") or []),
               f"OF1 [{tag}] the gate takes no action on an unreadable reviewer YAML and names it ({gof})")
            try:
                oof = _SM.open_findings(dof, {"action": "ACCEPT", "rigor_violations": []})
            except Exception as e:  # noqa: BLE001
                oof = [f"raised {type(e).__name__}"]
            ok(any(isinstance(o, dict) and o.get("id") == "critique_unreadable:critique-domain"
                   and o.get("severity") == "HIGH" for o in oof),
               f"OF2 [{tag}] open_findings reports the unreadable reviewer YAML as an open HIGH ({oof})")
        fr = os.path.join(td, "t44")
        os.makedirs(os.path.join(fr, "models"))
        shutil.copy(os.path.join(FX_DIR, "claims_ledger.yaml"), os.path.join(fr, "models", "claims_ledger.yaml"))
        yaml.safe_dump({"schema_version": 1, "constraints": {"envelope_usd": 18000000}}, open(os.path.join(fr, AB.SPEC_FILE), "w"))
        open(os.path.join(fr, "manuscript.md"), "w").write(
            "1. Within the 18000000 budget for 2026-2028, D09 gets nothing; the plan averts 999,999 cases.\n")
        k44 = [f for f in report_claim_findings(fr) if f["kind"] == "report_number_typed"]
        ok(k44 and "999,999" in k44[0]["claim"] and "18000000" not in k44[0]["claim"] and "2026" not in k44[0]["claim"],
           f"T44 a typed result number is HIGH; the owner's given numbers, years, ids and list numbers are not ({k44[:1]})")
        open(os.path.join(fr, "manuscript.md"), "w").write("Intro.\n<!-- harness: copied -->\n`headline.point` and [CLAIM:nope].\n")
        k44b = {f["kind"] for f in report_claim_findings(fr)}
        ok({"report_claim_id_raw", "report_claim_unresolved"} <= k44b, f"T44b no marker the writer copies hides an id ({k44b})")
        open(os.path.join(fr, "manuscript.md"), "w").write("Averts [CLAIM:headline.point] cases.\n")
        open(os.path.join(fr, "report.md"), "w").write("stale [CLAIM:headline.point]\n")
        m45 = Machine(fr, StubTasks({}), RunState(question="q", state="FIX_LOOP"), backend=spike, log=lambda *a: None)
        m45.rebuild(fr)
        ok("Averts 1.3M" in open(os.path.join(fr, "report.md")).read(),
           "T45 the fix loop's rebuild re-renders the report from the edited draft")
        fp = os.path.join(td, "t47", "run")
        os.makedirs(os.path.join(fp, "data"))
        os.makedirs(os.path.join(td, "t47", "x y"))
        open(os.path.join(td, "t47", "x y", "units.csv"), "w").write("a\n")
        open(os.path.join(fp, "data", "targets"), "w").write("t\n")
        yaml.safe_dump({"decision_units": {"csv": "../x y/units.csv"}, "calibration": {"targets": {"csv": "data/targets"}}},
                       open(os.path.join(fp, AB.SPEC_FILE), "w"))
        p47 = record_cited_inputs(fp)
        prov = json.load(open(os.path.join(fp, PROVENANCE)))
        ok(any("../x y/units.csv" in x for x in p47) and "data/targets" in prov["cited"],
           f"T47 a cited path with a space outside the run is refused; a path field without an extension is recorded ({p47})")
        fq = os.path.join(td, "t48")
        outside = os.path.join(td, "t48_outside")
        os.makedirs(os.path.join(fq, "data"))
        os.makedirs(outside)
        os.symlink(outside, os.path.join(fq, "data", "inputs"))
        try:
            stage_inputs(fq, "Inputs: requirements.txt")
            raised = False
        except ValueError:
            raised = True
        ok(raised and not os.listdir(outside), "T48 a symlinked data/inputs is refused; nothing is written outside the run")
        ok(answer_key_path("EVALS/q/rubric.yaml") and answer_key_path("data/_TRUTH/truth.json")
           and not answer_key_path("data/evals/q/districts.csv"),
           "T49 the answer-key exclusion is case-insensitive; the questions' public inputs are not excluded")
        fs = os.path.join(td, "t50")
        os.makedirs(fs)
        stage_inputs(fs, "requirements.txt")
        yaml.safe_dump({"decision_units": {"csv": "data/inputs/requirements.txt"}}, open(os.path.join(fs, AB.SPEC_FILE), "w"))
        ok(record_cited_inputs(fs) == [], "T50 an untouched staged input passes")
        open(os.path.join(fs, "data", "inputs", "requirements.txt"), "a").write("tampered\n")
        ok(any("changed or removed" in x for x in record_cited_inputs(fs)),
           "T50b a staged input changed after it was copied is refused")
        import pipeline as P_
        fk = os.path.join(td, "t50k")         # a joint calibration group target's key names columns, not files
        os.makedirs(fk)
        stage_inputs(fk, "requirements.txt")
        for csv, want in (("data/inputs/requirements.txt", False), ("data/missing.csv", True)):
            spec = {"decision_units": {"csv": "data/inputs/requirements.txt"}, "calibration": {"joint": {"targets": [
                {"id": "g", "kind": "group", "csv": csv, "key": {"csv": "unit", "units": "state"}}]}}}
            yaml.safe_dump(spec, open(os.path.join(fk, AB.SPEC_FILE), "w"))
            rc = record_cited_inputs(fk)
            dr = [p["claim"] for p in P_.derivation_problems(fk, spec, required=True) if p["code"] == "data_ref_not_derived"]
            ok(not any("cites unit" in x for x in rc) and not any("reads unit" in x for x in dr),
               f"T50c a joint target's key.csv (a column) is not a cited file ({rc}; {dr})")
            if want:
                ok(any("data/missing.csv" in x for x in rc) and any("data/missing.csv" in x for x in dr),
                   f"T50d the joint target's own csv is still a cited file, and a missing one is refused ({rc}; {dr})")
        for kind, key in (("group", {"csv": {"file": "data/missing.csv"}, "units": "state"}),
                          ("unit", {"csv": "data/missing.csv"})):    # Codex: only a group target's column name
            spec = {"decision_units": {"csv": "data/inputs/requirements.txt"}, "calibration": {"joint": {"targets": [
                {"id": "g", "kind": kind, "csv": "data/inputs/requirements.txt", "key": key}]}}}
            yaml.safe_dump(spec, open(os.path.join(fk, AB.SPEC_FILE), "w"))
            ok(any("data/missing.csv" in x for x in record_cited_inputs(fk)),
               f"T50e a {kind} target's key.csv that is not a group column name is still scanned for files")
        open(os.path.join(fr, "manuscript.md"), "w").write("`headline.point`\n")
        os.remove(os.path.join(fr, STATE_FILE))           # T45's machine left its state here
        m51 = Machine(fr, StubTasks({}), RunState(question="q", state="GATE", v2_rules=False), backend=spike,
                      log=lambda *a: None)
        ok(not any(f["kind"].startswith("report_") for f in m51.harness_findings()),
           "T51 golden replays (v2_rules off) do not run the v2 report checks")

        # T52-T55: Codex round 2 on #95 (each failed on 7aa9642).
        for txt in ("Benefit is -987654 cases.", "Benefit is 987K cases.", "Benefit is 987e6 cases."):
            open(os.path.join(fr, "manuscript.md"), "w").write(txt + "\n")
            if not any(f["kind"] == "report_number_typed" for f in report_claim_findings(fr)):
                ok(False, f"T52 signed, magnitude-suffixed and scientific numbers are typed numbers: {txt}")
                break
        else:
            ok(True, "T52 signed, magnitude-suffixed and scientific numbers are typed numbers")
        json.dump({"question": "Choose packages for 10 districts.", "started": "2026-10-31T16:42:57Z"},
                  open(os.path.join(fr, "metadata.json"), "w"))
        open(os.path.join(fr, "manuscript.md"), "w").write("Across 10 districts it reduces cases by 57%.\n")
        k53 = [f for f in report_claim_findings(fr) if f["kind"] == "report_number_typed"]
        ok(k53 and "57%" in k53[0]["claim"] and "10" not in k53[0]["claim"].split("(")[1].split(")")[0].split(", "),
           f"T53 only the question's own numbers are given; a timestamp's digits are not ({k53[:1]})")
        open(os.path.join(fr, "manuscript.md"), "w").write("The plan averts `[CLAIM:headline.point]` cases.\n")
        render_report(fr, spike)
        ok(any(f["kind"] == "report_claim_unrendered" for f in report_claim_findings(fr)),
           "T54 a claim reference left unrendered in the report (inside code) is a HIGH finding")
        # T56: the typed-number rule is a text heuristic: MEDIUM and declarable (process rule 4), and the
        # interval level and any value in the spec, the ledger or the question are not typed numbers.
        open(os.path.join(fr, "manuscript.md"), "w").write(
            "A 95% interval; about 1.3M cases for $17.9M across 10 districts; 3 years.\n")
        k56 = [f for f in report_claim_findings(fr) if f["kind"] == "report_number_typed"]
        ok(k56 and k56[0]["severity"] == "LOW" and "(3)" in k56[0]["claim"],
           f"T56 typed numbers are LOW (advisory); 95%, 1.3M, $17.9M and 10 are exempt ({k56[:1]})")
        # T57: a run whose only open finding is a typed number reaches ACCEPT; the finding is reported
        # in findings.jsonl (advisory), not planned and not declared.
        d57, T57, st57 = new_run("typed_only", {"manuscript": "The plan averts 999 cases. It averts [CLAIM:headline.point] cases.\n"})
        os.makedirs(os.path.join(d57, "models"), exist_ok=True)
        shutil.copy(os.path.join(FX_DIR, "claims_ledger.yaml"), os.path.join(d57, "models", "claims_ledger.yaml"))
        s57 = _SpikeMachine(d57, T57, st57, log=lambda *a: None, cross_engine_check=False).run()
        led57 = FL.load(d57)
        typed57 = [f for f in led57.values() if f.kind == "report_number_typed"]
        ok(s57 == "completed" and typed57 and typed57[0].severity == "LOW" and not typed57[0].blocking
           and not os.path.exists(os.path.join(d57, DECLARED_FILE))
           and json.load(open(os.path.join(d57, "final_gate.json")))["action"] == "ACCEPT",
           f"T57 a run whose only open finding is a typed number reaches ACCEPT; the finding is advisory ({s57})")
        fe = os.path.join(td, "t55")
        os.makedirs(os.path.join(fe, "data"))
        yaml.safe_dump({"engine": {"ref": "toolkit.engines.contract:ToyEngine",
                                   "config": {"results_path": os.path.join(td, "outside_bench")}}},
                       open(os.path.join(fe, AB.SPEC_FILE), "w"))
        os.makedirs(os.path.join(td, "outside_bench"), exist_ok=True)
        ok(any("outside_bench" in x for x in record_cited_inputs(fe)),
           "T55 an engine config path outside the run, without a file extension, is refused")

        try:
            _self_test_resume(ok, td, spike, new_run)
        except Exception as e:  # noqa: BLE001 -- before the resume path exists this is the failure
            import traceback
            traceback.print_exc()
            ok(False, f"T58-T60 analysis timeout and resume: {type(e).__name__}: {e}")

        _self_test_auto1a(ok, td, spike, new_run)
        _self_test_writeup_rounds(ok, td, spike, new_run)
        try:
            _self_test_hb2_spec(ok, td, spike, new_run)
        except Exception as e:  # noqa: BLE001 -- before the fix this is the failure
            ok(False, f"HS: {type(e).__name__}: {e}")
        try:
            _self_test_spec_p1s(ok, td, spike, new_run)
        except Exception as e:  # noqa: BLE001 -- before the fix this is the failure
            ok(False, f"SP: {type(e).__name__}: {e}")

        # T23: a transition outside the table is refused.
        m = _SpikeMachine(os.path.join(td, "ref"), StubTasks({}), None, log=lambda *a: None)
        m.st.state = "SPEC"
        try:
            m._go("WRITE")
            ok(False, "T23 an illegal transition raises")
        except RuntimeError:
            ok(True, "T23 an illegal transition raises")
        p = os.path.join(td, "ref", STATE_FILE)
        json.dump(dict(json.load(open(p)), state="LEAD_PROMPT"), open(p, "w"))
        try:
            load_state(os.path.join(td, "ref"))
            ok(False, "T24 an unknown persisted state fails closed")
        except ValueError:
            ok(True, "T24 an unknown persisted state fails closed")
        try:
            _self_test_derived(ok, td)
        except Exception as e:  # noqa: BLE001 -- before the derivation step exists (step α) this is the failure
            import traceback
            traceback.print_exc()
            ok(False, f"DI machine tests: {type(e).__name__}: {e}")
        try:
            _self_test_geometry(ok, td)
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            ok(False, f"IG machine tests: {type(e).__name__}: {e}")
        try:
            _self_test_evidence(ok, td)
        except Exception as e:  # noqa: BLE001 -- before the evidence stage exists (step α) this is the failure
            import traceback
            traceback.print_exc()
            ok(False, f"EV machine tests: {type(e).__name__}: {e}")
        # T70 (run console review, finding 6): `sign --expect-sha256` refuses a spec other than the one shown.
        from toolkit.spec import schema as _S, signoff as _SO
        d70 = os.path.join(td, "t61")
        os.makedirs(d70)
        _S.toy_spec(d70)
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            try:
                rc = main(["sign", d70, "--by", "owner", "--note", "n", "--expect-sha256", "0" * 64])
            except SystemExit as e:
                rc = e.code
            st70 = _SO.status(d70)
            try:
                rc2 = main(["sign", d70, "--by", "owner", "--note", "n", "--expect-sha256",
                            hashlib.sha256(open(os.path.join(d70, AB.SPEC_FILE), "rb").read()).hexdigest()])
            except SystemExit as e:
                rc2 = e.code
        d72, o72 = os.path.join(td, "t72"), os.path.join(td, "t72out")
        os.makedirs(d72)
        os.makedirs(o72)
        _S.toy_spec(d72)
        open(os.path.join(o72, "v"), "w").write("untouched")
        os.symlink(os.path.join(o72, "v"), os.path.join(d72, SIGNOFF_SHEET))
        try:
            Machine._write_sheet(type("M", (), {"run_dir": d72, "backend": None})())
        except (ValueError, OSError):
            pass
        ok(open(os.path.join(o72, "v")).read() == "untouched",
           "T72 the sign-off sheet is published without following a link (round 3, P1-2)")
        # T73 (round 4, P1-2): run_state.json and the machine's records never follow a planted .tmp link
        d73, o73 = os.path.join(td, "t73"), os.path.join(td, "t73out")
        os.makedirs(d73)
        os.makedirs(o73)
        open(os.path.join(o73, "v"), "w").write("untouched")
        os.symlink(os.path.join(o73, "v"), os.path.join(d73, STATE_FILE + ".tmp"))
        os.symlink(os.path.join(o73, "v"), os.path.join(d73, "rec.json.tmp"))
        for fn in (lambda: save_state(d73, RunState(question="q", state="SPEC")), lambda: _write_json(d73, "rec.json", {})):
            try:
                fn()
            except (ValueError, OSError):
                pass
        ok(open(os.path.join(o73, "v")).read() == "untouched",
           "T73 save_state and the machine's JSON records do not write through a planted .tmp link")
        d71 = os.path.join(td, "t71")
        os.makedirs(d71)
        _S.toy_spec(d71)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            try:
                rc71 = main(["sign", d71, "--by", "owner", "--note", "n", "--expect-sha256",
                             hashlib.sha256(open(os.path.join(d71, AB.SPEC_FILE), "rb").read()).hexdigest(),
                             "--expect-derivations-sha256", "0" * 64])
            except SystemExit as e:
                rc71 = e.code
            st71 = _SO.status(d71)
            try:
                rc71b = main(["sign", d71, "--by", "owner", "--note", "n", "--expect-sha256",
                              hashlib.sha256(open(os.path.join(d71, AB.SPEC_FILE), "rb").read()).hexdigest(),
                              "--expect-derivations-sha256", "none"])
            except SystemExit as e:
                rc71b = e.code
        ok(rc71 not in (0, None) and st71 == "awaiting" and rc71b == 0 and _SO.status(d71) == "signed",
           f"T71 sign --expect-derivations-sha256 refuses a derivation record other than the one shown and signs "
           f"with the one shown (rc {rc71}, {st71}; rc {rc71b})")
        ok(rc not in (0, None) and st70 == "awaiting" and rc2 == 0 and _SO.status(d70) == "signed",
           f"T70 stage_machine.py sign --expect-sha256 refuses another spec's sha and signs the shown one "
           f"(rc {rc}, {st70}; rc {rc2}, {_SO.status(d70)})")
        # T80-T83 (F6 S1-7): declared findings keep their full text and remedy; not judged is not a pass;
        # a declared-scope report says so and uses no acceptance language. The fixture mimics F5's three
        # declarations whose claims were cut mid-sentence (D-003, R-001, R-002).
        import yaml
        f80 = os.path.join(td, "t80")
        os.makedirs(f80)
        longc = {i: (f"{i}: the effect used for this component is a fixed value with no cited source, and the "
                     "chosen package in each unit depends on it; " * 4 + f"END-{i}.") for i in ("D-003", "R-001", "R-002")}
        fix = {i: "Replace the fixed value with a sourced range and rerun the decision test; " * 3 + f"FIX-{i}."
               for i in longc}
        jd = [{"file": "manuscript.md", "exists": True}, {"judgement": "a sourced range is used"}]
        for fn, ids in (("critique_domain.yaml", ["D-003"]), ("critique_redteam.yaml", ["R-001", "R-002"])):
            yaml.safe_dump({"blockers": [{"id": i, "severity": "MEDIUM", "target_stage": "MODEL", "resolved": False,
                                          "first_seen_round": 1, "claim": longc[i], "fix_requires": fix[i],
                                          "done_when": jd} for i in ids]}, open(os.path.join(f80, fn), "w"))
        yaml.safe_dump({"schema_version": 1}, open(os.path.join(f80, AB.SPEC_FILE), "w"))
        open(os.path.join(f80, "manuscript.md"), "w").write("The plan is described here.\n")
        try:
            op80 = _SM.open_findings(f80, {"action": "ACCEPT", "rigor_violations": []})
            g80 = apply_medium_rule({"action": "ACCEPT"}, op80, rounds_remaining=0, budget_ok=True)
            write_declared(f80, op80, g80)
            dec80 = yaml.safe_load(open(os.path.join(f80, DECLARED_FILE)))["declared"]
            ok(g80["action"] == "DECLARE_SCOPE" and len(dec80) == 3
               and all(e["claim"] == longc[e["id"]] and e.get("required_remedy") == fix[e["id"]]
                       and 0 < len(e.get("label") or "") <= 160 for e in dec80),
               f"T80 declared_findings.yaml keeps each full claim and required remedy, with a separate short label "
               f"({[(e['id'], len(e['claim']), bool(e.get('required_remedy'))) for e in dec80]})")
            sdc = "SD-1 text: the decision depends on an unsourced effect, " * 8 + "END-SD."
            yaml.safe_dump({"declarations": [{"id": "SD-1", "blocker_id": "D-003", "claim": sdc,
                                              "description": "DESC-SD", "fix_requires": "REMEDY-SD"}]},
                           open(os.path.join(f80, "scope_declaration.yaml"), "w"))
            render_report(f80, AB, status="completed_scope_declared")
            rep80 = open(os.path.join(f80, "report.md")).read()
            ok(all(longc[i] in rep80 and fix[i] in rep80 for i in longc)
               and all(x in rep80 for x in (sdc, "DESC-SD", "REMEDY-SD"))
               and os.path.exists(os.path.join(f80, "report_render.json"))
               and "Run status: declared scope, not accepted" in rep80.split("\n\n")[0],
               "T81 the report repeats each declared finding and each scope declaration in full with its remedy, "
               "under a 'declared scope, not accepted' status line, and records its harness blocks")
        except Exception as e:  # noqa: BLE001 -- the α tests fail, they do not crash the suite
            ok(False, f"T80/T81 raised {type(e).__name__}: {e}")
        try:
            open(os.path.join(f80, "manuscript.md"), "w").write(
                "<!-- harness: rendered from analysis/analysis.json -->\nThe allocation was accepted and is validated.\n")
            k82 = [(f["kind"], f["severity"]) for f in report_acceptance_findings(f80, "declared")]
            ok(("report_acceptance_language", "MEDIUM") in k82 and not report_acceptance_findings(f80, "accepted"),
               f"T82 acceptance language in a declared-scope write-up is found, behind a forged harness marker too; "
               f"in an accepted one it is not ({k82})")
        except Exception as e:  # noqa: BLE001
            ok(False, f"T82 raised {type(e).__name__}: {e}")
        try:   # round 3: an indented block marker still ends the negated clause, in the machine's check too
            hits82 = []
            for t82 in ("Not run\n  - Validated against trials.\n", "Not accepted\n   ## Validated allocation\n",
                        "Not tested\n  > Validated by the trial\n"):
                open(os.path.join(f80, "manuscript.md"), "w").write(t82)
                hits82.append(bool(report_acceptance_findings(f80, "declared")))
            ok(all(hits82), f"T82b indented bullets, headings and quotes keep their clause boundary ({hits82})")
        except Exception as e:  # noqa: BLE001
            ok(False, f"T82b raised {type(e).__name__}: {e}")
        try:
            nj = {"action": "ACCEPT", "rule_matched": 4,
                  "done_when_status": {"D-009": {"passes": None, "result": "not_judged", "open": True,
                                                 "severity": "MEDIUM", "detail": "not judged"}}}
            r83 = require_judged(dict(nj))
            ok(r83["action"] != "ACCEPT" and r83.get("check_summary", {}).get("not_judged") == 1
               and "D-009" in json.dumps(r83),
               f"T83 a required check that is not judged cannot yield ACCEPT ({r83.get('action')})")
        except Exception as e:  # noqa: BLE001
            ok(False, f"T83 raised {type(e).__name__}: {e}")
        # T84-T85: a declared report is not delivered with acceptance language: the writer repairs it, or the
        # report is withheld. The draft is written in accepted mode first (the pre-write gate accepts).
        nofix84 = {"id": "P-084", "severity": "HIGH", "target_stage": "WRITE", "claim": "cannot fix"}
        for name, extra, want in (("t84", {"manuscript_repair": "The plan was tested against the trials.\n"},
                                   "completed_scope_declared"), ("t85", {}, "completed_no_report")):
            d, T, st = new_run(name, dict({"critics": {1: {"critique-presentation": [nofix84]}},
                                           "manuscript": "The plan is validated against the trials.\n"}, **extra),
                               max_rounds=1)
            try:
                s = _SpikeMachine(d, T, st, log=lambda *a: None, cross_engine_check=False).run()
            except Exception as e:  # noqa: BLE001
                s = f"raised {type(e).__name__}: {e}"
            rp = os.path.join(d, "report.md")
            rec = json.load(open(os.path.join(d, "final_gate.json"))) if os.path.exists(os.path.join(d, "final_gate.json")) else {}
            al = rec.get("report_acceptance_language") or {}
            if name == "t84":
                ok(s == want and any(c.endswith(":repair") for c in T.calls) and os.path.exists(rp)
                   and "validated" not in open(rp).read() and al.get("repairs") == 1 and not al.get("remaining"),
                   f"T84 a declared report with acceptance language goes back to the writer and is delivered repaired ({s}, {al})")
            else:
                ok(s == want and not os.path.exists(rp) and os.path.exists(os.path.join(d, "report_withheld.md"))
                   and al.get("remaining"),
                   f"T85 an unrepaired declared report is withheld, not delivered ({s}, {al})")
        # T88 (round 2): a repaired draft goes through the whole post-write gate again: a repair that adds an
        # unresolved claim reference (non-declarable HIGH) ends RUN_FAILED, not a delivered declared report.
        # T89: a Markdown soft break inside a negated sentence is not acceptance language (nothing withheld).
        for name, extra, want in (("t88", {"manuscript": "The plan is validated against the trials.\n",
                                           "manuscript_repair": "The plan was tested; see [CLAIM:missing_claim].\n"},
                                   "run_failed"),
                                  ("t89", {"manuscript": "The model is not yet\nvalidated against the trials.\n"},
                                   "completed_scope_declared")):
            d, T, st = new_run(name, dict({"critics": {1: {"critique-presentation": [nofix84]}}}, **extra), max_rounds=1)
            os.makedirs(os.path.join(d, "models"), exist_ok=True)
            if not os.path.exists(os.path.join(d, "models", "claims_ledger.yaml")):
                shutil.copy(os.path.join(_SM.REPO, "tests", "fixtures", "v2blind1", "claims_ledger.yaml"),
                            os.path.join(d, "models", "claims_ledger.yaml"))
            try:
                s = _SpikeMachine(d, T, st, log=lambda *a: None, cross_engine_check=False).run()
            except Exception as e:  # noqa: BLE001
                s = f"raised {type(e).__name__}: {e}"
            if name == "t88":
                ok(s == want and any(c.endswith(":repair") for c in T.calls),
                   f"T88 a language repair is re-checked by the full post-write gate ({s})")
            else:
                ok(s == want and not any(c.endswith(":repair") for c in T.calls)
                   and os.path.exists(os.path.join(d, "report.md")),
                   f"T89 'not yet' + soft break + 'validated' is negated: delivered, no repair ({s})")
        # T90-T91 (round 3): the rejected report is quarantined before its repair; a repair with no budget or a
        # failed repair task ends with no deliverable report.md (the rejected one stays in report_withheld.md).
        from v2_tasks import TaskResult as _TR
        for name, outcome in (("t90", None), ("t91", _TR(False, None, 0.0, "stub: failed"))):
            d, T, st = new_run(name, {"critics": {1: {"critique-presentation": [nofix84]}},
                                      "manuscript": "The plan is validated against the trials.\n"}, max_rounds=1)
            m = _SpikeMachine(d, T, st, log=lambda *a: None, cross_engine_check=False)
            real_task = m._task

            def _task(key, name_, *args, _real=real_task, _out=outcome, **kw):
                if name_ == "write" and len(args) > 2:      # the language repair (feedback given)
                    return _out
                return _real(key, name_, *args, **kw)
            m._task = _task
            try:
                s = m.run()
            except Exception as e:  # noqa: BLE001
                s = f"raised {type(e).__name__}: {e}"
            rp, wp = os.path.join(d, "report.md"), os.path.join(d, "report_withheld.md")
            ok(s == "completed_no_report" and not os.path.exists(rp) and os.path.exists(wp)
               and "validated" in open(wp).read(),
               f"T{name[1:]} a repair {'with no budget' if outcome is None else 'that fails'} leaves no deliverable "
               f"report with acceptance language ({s}, report.md exists: {os.path.exists(rp)})")
        # T92: the quarantine move goes through lib.safe_io: a planted link at report_withheld.md is replaced,
        # never written through, and a linked report.md is refused, never moved.
        d92, o92 = os.path.join(td, "t92"), os.path.join(td, "t92out")
        os.makedirs(d92)
        os.makedirs(o92)
        open(os.path.join(o92, "v"), "w").write("untouched")
        open(os.path.join(d92, "report.md"), "w").write("rejected report\n")
        os.symlink(os.path.join(o92, "v"), os.path.join(d92, "report_withheld.md"))
        try:
            _quarantine_report(d92)
            moved = (not os.path.exists(os.path.join(d92, "report.md"))
                     and not os.path.islink(os.path.join(d92, "report_withheld.md"))
                     and open(os.path.join(d92, "report_withheld.md")).read() == "rejected report\n")
            os.symlink(os.path.join(o92, "v"), os.path.join(d92, "report.md"))
            try:
                _quarantine_report(d92)
                refused = False
            except (ValueError, OSError):
                refused = True
        except Exception as e:  # noqa: BLE001
            moved, refused = f"raised {type(e).__name__}: {e}", False
        ok(moved is True and refused and open(os.path.join(o92, "v")).read() == "untouched",
           f"T92 the report quarantine never follows a link ({moved}, refused a linked report: {refused})")
        # T86-T87 through h_GATE: an open finding whose only test is a judgement is never accepted. The fix loop
        # and rule 3m handle it first; rule 3j is the backstop when no open finding is listed for it.
        judged_only = {"id": "D-086", "severity": "MEDIUM", "target_stage": "ANALYZE", "claim": "needs a judgement",
                       "done_when": [{"judgement": "the critic reads the caveat"}]}
        g_ = SM_GLOBALS
        orig_open = g_["open_findings"]
        for name, patch, rule in (("t86", None, "3m"), ("t87", lambda rd, dd: [], "3j")):
            d, T, st = new_run(name, {"critics": {1: {"critique-domain": [judged_only]}}}, max_rounds=1)
            if patch:
                g_["open_findings"] = patch
            try:
                s = _SpikeMachine(d, T, st, log=lambda *a: None, cross_engine_check=False).run()
            except Exception as e:  # noqa: BLE001
                s = f"raised {type(e).__name__}: {e}"
            finally:
                g_["open_findings"] = orig_open
            fg = json.load(open(os.path.join(d, "final_gate.json"))) if os.path.exists(os.path.join(d, "final_gate.json")) else {}
            ok(s == "completed_scope_declared" and fg.get("rule_matched") == rule
               and (fg.get("check_summary") or {}).get("required_not_judged") == ["D-086"],
               f"T{name[1:]} a judgement-only finding at exhaustion is declared by rule {rule}, with the check "
               f"summary in final_gate.json ({s}, {fg.get('rule_matched')}, {fg.get('check_summary')})")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0
