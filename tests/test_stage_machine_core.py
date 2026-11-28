#!/usr/bin/env python3
"""Core stage-machine tests extracted from agents/stage_machine.py."""
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
    AB, ACCEPTED_STATUSES, AWAITING_BUDGET, AWAITING_SPEC_AMENDMENT, CRITICS, DECLARED_FILE, EDGES, FL, FX, Machine, OWNER_LIFECYCLE_WIRED, OWNER_REQUEST, PROVENANCE, RunState, SCOPE_SIGNOFF, SIGNOFF_FILE, SPEC_RETHINK_REQUEST, STATE_FILE, _bearing_item, _owner_rule, _plan_changing, _question_paths, _relaunch_motivation, _rep_error, _spec_signed_sha, _withhold_report, _write_json, answer_key_path, apply_medium_rule, apply_owner_declines, apply_owner_rule, deliverable_problems, drop_declined, init_run, load_state, main as sm_main, open_findings, pending_amendments, provisional_lines, record_cited_inputs, render_report, report_claim_findings, require_judged, route_gate, run_v2, save_state, signed_hash, spec_signed_once, stage_inputs, v2_gate, write_declared,
)


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    ap.parse_args(argv)
    return _self_test()


def _self_test() -> int:
    import tempfile
    import shutil
    from v2_tasks import StubTasks
    fails = []
    toolkit = AB.backend("toolkit")

    def _ToolkitMachine(*a, **k):
        k.setdefault("backend", toolkit)
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
          f'  File "{root}/agents/analysis_backend.py", line 1556, in <module>\n    sys.exit(sm_main())\n'
          "             ~~~~^^\n"
          f'  File "{root}/agents/analysis_backend.py", line 776, in main\n    A = _analyze(spec, out)\n'
          f'  File "{root}/agents/analysis_backend.py", line 515, in _analyze\n'
          "    return Analysis(spec, out, log=lambda *a: None).run()\n"
          f'  File "{root}/toolkit/analysis/run.py", line 120, in solve\n'
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
            import yaml as _y
            from toolkit.spec import schema as _schema
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
            st = init_run(d, "toy question", max_rounds, signoff, "v2", budget, derived_inputs=False)   # pre-DI fixture
            return d, StubTasks(script), st

        # T10: the happy path with one critic finding fixed in the loop.
        blocker = {"id": "D-001", "severity": "HIGH", "target_stage": "ANALYZE", "claim": "too few draws",
                   "done_when": [{"file": "analysis_settings.yaml", "key": "uncertainty.draws", "op": ">=", "value": 32}]}
        d, T, st = new_run("happy", {"critics": {1: {"critique-domain": [blocker]}},
                                     "fixes": {"D-001": [("analysis_settings.yaml", "uncertainty.draws", 32)],
                                               "uncertainty_draws_low:draws": [("analysis_settings.yaml", "uncertainty.draws", 32)]}})
        status = _ToolkitMachine(d, T, st, log=lambda *a: None, cross_engine_check=False).run()
        hist = [h["to"] for h in load_state(d).history if "to" in h]
        ok(status == "completed", f"T10 toy run completes: {status} via {hist}")
        ok(hist[:5] == ["EVIDENCE", "SIGN", "ENGINE_CHOICE", "ANALYZE", "CRITIQUE"]
           and "FIX_LOOP" in hist
           and hist[-2:] == ["GATE", "FINAL"], f"T11 the path SPEC..FIX_LOOP..WRITE..FINAL {hist}")
        from toolkit.spec import evidence_dossier as EDR_
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
        ref_status = _ToolkitMachine(d0, T0, st0, log=lambda *a: None, cross_engine_check=False).run()
        ref_path = [h["to"] for h in load_state(d0).history if "to" in h]

        def _final_why(d_):     # the reason a run reached FINAL (a step's error carries its log tail): CI flakes of
            return [h.get("why") for h in load_state(d_).history if h.get("to") == "FINAL"][-1:]   # T16 named none
        ref_why = _final_why(d0) if ref_status != "completed" else []
        bad = []
        for k in range(1, len(ref_path)):
            dk, Tk, stk = new_run(f"resume{k}", {"critics": {1: {"critique-domain": [blocker]}},
                                                "fixes": {"D-001": [("analysis_settings.yaml", "uncertainty.draws", 32)]}})
            m = _ToolkitMachine(dk, Tk, stk, log=lambda *a: None, cross_engine_check=False)
            for _ in range(k):
                m.step()
            del m
            m2 = _ToolkitMachine(dk, StubTasks(Tk.s), None, log=lambda *a: None, cross_engine_check=False)   # from run_state.json
            s2 = m2.run()
            p2 = [h["to"] for h in load_state(dk).history if "to" in h]
            if s2 != ref_status or p2 != ref_path:
                bad.append((k, s2, p2, _final_why(dk)))
            shutil.rmtree(dk)
        ok(not bad, f"T16 a run stopped after any of {len(ref_path) - 1} transitions resumes to the same path and status {bad[:1]}"
           + (f" (reference run {ref_status}: {ref_why})" if ref_why else ""))

        # T17: owner sign-off pauses, persists, resumes.
        d, T, st = new_run("owner", {}, signoff="owner")
        m = _ToolkitMachine(d, T, st, log=lambda *a: None, cross_engine_check=False)
        s = m.run()
        ok(s == "paused:awaiting_signoff" and load_state(d).state == "SIGN", f"T17 owner mode pauses at SIGN ({s})")
        pz = [(h.get("paused"), h.get("state")) for h in load_state(d).history if "paused" in h]
        ok(pz == [("awaiting_signoff", "SIGN")], f"RR1 a SIGN pause writes one history entry ({pz})")
        _mp = json.load(open(os.path.join(d, "metadata.json")))     # PS1 (baseline hb3 §4.2: a pause wrote no status)
        ok(_mp.get("status") == "paused:awaiting_signoff" and _mp.get("paused_state") == "SIGN"
           and _mp.get("paused_why") and "finished" not in _mp,
           f"PS1 a paused run records paused:<reason>, its state and why in metadata.json, and no finish time "
           f"({[_mp.get(k) for k in ('status', 'paused_state', 'paused_why', 'finished')]})")
        toolkit.sign(d, "owner", "ok")
        ok(_ToolkitMachine(d, StubTasks(T.s), None, log=lambda *a: None, cross_engine_check=False).run() == "completed",
           "T18 after the owner signs, the resumed run completes")

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
                signed_before = signed_before or signed_hash(run_dir)
                return super().fix(task, run_dir, evidence)
        s = _ToolkitMachine(d, Spy(T.s), st, log=lambda *a: None, cross_engine_check=False).run()
        rows = FX.impact_rows(d)
        led = FL.load(d)
        ok(rows and all(r.get("rejected") and not r.get("steps_rebuilt") for r in rows)
           and signed_hash(d) == signed_before and "constraints.envelope_usd" not in AB._leaves(
               __import__("yaml").safe_load(open(os.path.join(d, AB.SETTINGS_FILE))) or {})
           and "spec_changed_after_signoff:spec" not in led and s == "completed_scope_declared",
           f"T19 edits to the signed spec are rejected and undone before any rebuild ({s}, {[r.get('rejected') for r in rows][:1]})")

        # T20: a structural mismatch is a back-edge to ENGINE_CHOICE, then the run continues.
        d, T, st = new_run("rethink", {"structural": {1: "critique-domain"}})
        s = _ToolkitMachine(d, T, st, log=lambda *a: None, cross_engine_check=False).run()
        p = [h["to"] for h in load_state(d).history if "to" in h]
        i = p.index("GATE")
        ok(p[i + 1] == "ENGINE_CHOICE" and s == "completed" and load_state(d).round == 2,
           f"T20 RETHINK_STRUCTURAL goes back to ENGINE_CHOICE and the next critique is round 2 ({s})")

        # T21: an open blocker at the last round: DECLARE_SCOPE when declarable.
        nofix = {"id": "D-009", "severity": "HIGH", "target_stage": "ANALYZE", "claim": "cannot fix"}
        d, T, st = new_run("declare", {"critics": {1: {"critique-domain": [nofix]}}}, max_rounds=2)
        s = _ToolkitMachine(d, T, st, log=lambda *a: None, cross_engine_check=False).run()
        ok(s == "completed_scope_declared", f"T21 an unfixed critic blocker at exhaustion ends scope-declared ({s})")
        rep21 = open(os.path.join(d, "report.md")).read()
        ok(os.path.exists(os.path.join(d, DECLARED_FILE)) and "D-009" in rep21 and "Open findings, declared" in rep21,
           "T46 (Codex #95-5) an ordinary DECLARE_SCOPE (rule 3) also lists what is open in the report")

        # T22: the budget, not the gate, ends the run: the harness writes the report.
        d, T, st = new_run("budget", {"critics": {1: {"critique-domain": [nofix]}}, "usd": 30.0}, budget=100.0)
        s = _ToolkitMachine(d, T, st, log=lambda *a: None, cross_engine_check=False).run()
        p = [h["to"] for h in load_state(d).history if "to" in h]
        ok(s == "budget_exhausted" and "WRITE" in p and os.path.exists(os.path.join(d, "report.md")),
           f"T22 with the budget used the run ends budget_exhausted with a report ({s}, {p[-4:]})")

        # T25: an interrupted paid task. The redteam critique crashes once in round 1: on resume the
        # domain critique (completed) is not run again, and the interrupted one is charged its allowance.
        d, T, st = new_run("crash", {"crash": {"critique:critique-redteam:1": 1}, "usd": 1.0}, budget=100.0)
        m = _ToolkitMachine(d, T, st, log=lambda *a: None, cross_engine_check=False)
        try:
            m.run()
            ok(False, "T25 the stub crash propagates")
        except RuntimeError:
            pass
        spent_at_crash = load_state(d).spent_usd
        T2 = StubTasks(dict(T.s, crash={}))
        s = _ToolkitMachine(d, T2, None, log=lambda *a: None, cross_engine_check=False).run()
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
        s = _ToolkitMachine(d, T, st, log=lambda *a: None, cross_engine_check=False).run()
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
        _ToolkitMachine(d, T, st, log=lambda *a: None, cross_engine_check=False).run()
        toolkit.sign(d, "owner", "first")
        signed_bytes = open(os.path.join(d, AB.SPEC_FILE), "rb").read()
        T2 = StubTasks(T.s)
        s = _ToolkitMachine(d, T2, None, log=lambda *a: None, cross_engine_check=False).run()
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
        s2 = _ToolkitMachine(d, T3, None, log=lambda *a: None, cross_engine_check=False).run()
        ok(s2 == "paused:awaiting_spec_amendment" and "draft_spec" not in T3.calls
           and open(os.path.join(d, AB.SPEC_FILE), "rb").read() == signed_bytes,
           f"R3-2b a resume stays paused for the amendment and drafts nothing ({s2})")
        # R3-2c (Codex review of #261): the pause is honoured on resume even if a restored copy lost the sign-off record,
        # and keeps its first request; and it is written with no budget left (it costs no model call)
        rq0 = json.load(open(os.path.join(d, "spec_rethink_request.json")))
        os.remove(os.path.join(d, SIGNOFF_FILE))
        T4 = StubTasks(T.s)
        s3 = _ToolkitMachine(d, T4, None, log=lambda *a: None, cross_engine_check=False).run()
        ok(s3 == "paused:awaiting_spec_amendment" and "draft_spec" not in T4.calls
           and json.load(open(os.path.join(d, "spec_rethink_request.json")))["requested_at"] == rq0["requested_at"],
           f"R3-2c a resumed amendment pause is honoured without the sign-off record and keeps its request ({s3})")
        st5 = load_state(d)
        st5.budget_usd, st5.spent_usd = 20.0, 20.0
        save_state(d, st5)
        s5 = _ToolkitMachine(d, StubTasks(T.s), None, log=lambda *a: None, cross_engine_check=False).run()
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
        m6 = _ToolkitMachine(d, T6, None, log=lambda *a: None, cross_engine_check=False)
        m6.step()
        ok(spec_signed_once(d6) and "draft_spec" not in T6.calls and load_state(d).paused == AWAITING_SPEC_AMENDMENT,
           "R3-2e a truncated sign-off record still protects the spec; a persisted amendment pause is honoured")
        # R3-2f (lead follow-up): the pause says when the signing evidence is missing or unreadable (a warning, no block)
        rq6 = json.load(open(os.path.join(d, "spec_rethink_request.json")))
        ok(rq6.get("signing_evidence") == "missing" and "requested_at" in rq6,
           f"R3-2f the amendment request notes missing signing evidence ({rq6.get('signing_evidence')})")

        # R3-1 (F6 run 3): a write-up finding is never a FIX_LOOP task; the writer gets it with the draft.
        wup = {"id": "R-030", "severity": "HIGH", "target_stage": "WRITE", "claim": "the report overstates the effect"}
        d, T, st = new_run("writeup", {"critics": {1: {"critique-redteam": [wup]}}}, max_rounds=2)
        s = _ToolkitMachine(d, T, st, log=lambda *a: None, cross_engine_check=False).run()
        p = [h["to"] for h in load_state(d).history if "to" in h]
        got = getattr(T, "write_findings", None) or []
        ok("FIX_LOOP" in p and not any(c.startswith("fix:") for c in T.calls)
           and any(f.get("id") == "R-030" for f in got),
           f"R3-1 a write-up finding goes to the writer, not to a FIX_LOOP task ({s}, {T.calls}, {got})")

        # R3-3 (F6 run 3): a finding whose remedy is an owner amendment goes to OWNER_DECISION, never a SPEC rethink.
        amend = {"id": "D-031", "severity": "HIGH", "target_stage": "PLAN", "claim": "the comparator omits the funded plan",
                 "fix_requires": "Owner amendment: add the funded plan as the comparator."}
        d, T, st = new_run("amend", {"critics": {1: {"critique-domain": [amend]}},
                                     "plan": lambda pk: {"tasks": [{"id": "T1", "findings": [f["id"] for f in
                                                                                                pk["findings"]][:1] or ["x"],
                                                                    "action": "rethink", "owner_state": "SPEC",
                                                                    "brief": "redraft the comparator"}]
                                                         if pk["findings"] else {"tasks": []}}}, max_rounds=3)
        s = _ToolkitMachine(d, T, st, log=lambda *a: None, cross_engine_check=False).run()
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
            s_st = _ToolkitMachine(d, StubTasks(T.s), None, log=lambda *a: None, cross_engine_check=False).run()
            ok(s_st == "paused:awaiting_scope_signoff",
               f"R3-3d an answer to a request the machine did not make does not release OWNER_DECISION ({s_st})")
            _SS.sign(d, "lead", all_decision="accept", note="R3-3c")     # Codex review of #258 P1-8: not the owner's
            s_l = _ToolkitMachine(d, StubTasks(T.s), None, log=lambda *a: None, cross_engine_check=False).run()
            ok(s_l == "paused:awaiting_scope_signoff",
               f"R3-3c an answer that is not the owner's does not release OWNER_DECISION ({s_l})")
            _SS.sign(d, "owner", all_decision="decline", note="R3-3")
            s2 = _ToolkitMachine(d, StubTasks(T.s), None, log=lambda *a: None, cross_engine_check=False).run()
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
            s_ = _ToolkitMachine(d_, T_, st_, log=lambda *a: None, cross_engine_check=False).run()
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
            sq2 = _ToolkitMachine(dq, StubTasks(Tq.s), None, log=lambda *a: None, cross_engine_check=False).run()
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
        # T_NOT_RUN_PROVISIONAL (Codex r1 of #375, P1-3): a lead-accepted decision test that did not run reads "not
        # tested", never "plan changes: no", and is named above the draft
        import scope_signoff as _SSn
        _prov = _SSn.provisional
        _SSn.provisional = lambda rd: [{"finding": "decision_test_not_run:DT3", "plan_changing": False, "review": None}]
        try:
            os.makedirs(os.path.join(td, "not_run_prov"), exist_ok=True)
            top_n, sec_n = provisional_lines(os.path.join(td, "not_run_prov"))
        finally:
            _SSn.provisional = _prov
        ok(any("did not run" in x and "decision_test_not_run:DT3" in x for x in top_n)
           and any("decision_test_not_run:DT3**: accepted provisionally; plan changes: not tested" in x for x in sec_n),
           f"T_NOT_RUN_PROVISIONAL a provisionally accepted unrun test reads not tested ({top_n}, {sec_n[-1:]})")
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
        _ToolkitMachine(d, T, None, log=lambda *a: None, cross_engine_check=False).step()   # the handler itself
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
        s7e = _ToolkitMachine(d, T, None, log=lambda *a: None, cross_engine_check=False).run()
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
        s = _ToolkitMachine(d, T, st, log=lambda *a: None, cross_engine_check=False).run()
        p = [h["to"] for h in load_state(d).history if "to" in h]
        ok(s == "paused:awaiting_scope_signoff" and "SPEC" not in p and p[-1] == "OWNER_DECISION",
           f"R3-5 a structural blocker that needs an owner amendment goes to the owner, not to SPEC ({s}, {p[-3:]})")

        # T_OWNER_DECISION_E2E (1F): a decision test that changes the plan goes to OWNER_DECISION with a request the
        # machine recorded; the owner's accept resumes the run, which ends accepted with the dependence at the top of
        # the report (owner decision (b), 2026-11-23; it ended scope-declared before)
        import scope_signoff as _SS
        dtf_e = {"kind": "decision_test_changes_plan", "severity": "HIGH", "stage": "ANALYZE", "key": "DT9",
                 "claim": "Decision test DT9 changes the plan: a lower effect", "scope_declarable": True,
                 "decision_bearing": True}

        class _DTMachine(Machine):
            def harness_findings(self):
                return super().harness_findings() + [dict(dtf_e)]
        mk = lambda d_, T_, st_: _DTMachine(d_, T_, st_, backend=toolkit, log=lambda *a: None,  # noqa: E731
                                            cross_engine_check=False)
        d, T, st = new_run("dt_owner", {}, signoff="owner", max_rounds=1)
        mk(d, T, st).run()
        toolkit.sign(d, "owner", "ok")
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
        span_o = json.load(open(os.path.join(d, "report_render.json")))["writer_span"] if rep_o else [0, 0]
        top_o = rep_o[:span_o[0]]
        ok(s_o2 == "completed" and "The plan depends on inputs the owner accepted" in top_o and "**DT9**" in top_o
           and "accepted by the owner" in top_o and "Provisional decisions" not in rep_o
           and FL.load(d)["decision_test_changes_plan:DT9"].status == "owner_accepted",
           f"T_OWNER_DECISION_E2E2 the owner's accept resumes the run, which ends accepted with the dependence stated "
           f"above the draft ({s_o2}, {top_o[-400:]!r})")
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
        ok(s_l == "completed" and not any(h.get("paused") for h in load_state(d).history)
           and "accepted by the lead, provisionally" in rep_l[:span[0]]
           and [(r["finding"], r["decision"], r["plan_changing"], r["review"]) for r in prov]
           == [("decision_test_changes_plan:DT9", "accept", True, None)]
           and "Provisional decisions that change the plan" in rep_l[:span[0]]
           and "## Provisional decisions" in rep_l[span[1]:],
           f"T_OWNER_LEAD_PROVISIONAL a lead run answers the owner decision provisionally and never blocks; the "
           f"plan-changing answer heads the report ({s_l}, {prov})")
        # T_HB2_FIRST_GATE (hb2 fix 1; malaria R1/R2, measles R1/R2, tb R1/R2 used all 3 rounds): with rounds left the
        # plan-changing test is asked at the first gate, once; accepted, the run writes in round 1, with no fix loop and
        # no second critique round, and ends accepted
        d1, T1, st1 = new_run("dt_lead_r3", {}, signoff="lead", max_rounds=3)
        s_r3 = mk(d1, T1, st1).run()
        h_r3 = [h for h in load_state(d1).history if "to" in h]
        to_r3 = [h["to"] for h in h_r3]
        g_r1 = json.load(open(os.path.join(d1, "gate_r1.json")))
        crit_r3 = sorted({c.rsplit(":", 1)[-1] for c in T1.calls if c.startswith("critique:critique-domain")})
        ok(s_r3 == "completed" and to_r3.count("OWNER_DECISION") == 1 and "FIX_LOOP" not in to_r3
           and crit_r3 == ["1"] and load_state(d1).round == 1
           and any(h["from"] == "GATE" and h["to"] == "OWNER_DECISION" and "rule 2o" in str(h.get("why")) for h in h_r3)
           and g_r1.get("action") == "ACCEPT",
           f"T_HB2_FIRST_GATE an owner-resolved HIGH is asked at the first gate, once, and the run ends in round 1 "
           f"({s_r3}, {to_r3[-8:]}, critique rounds {crit_r3}, gate r1 {g_r1.get('action')})")
        d2, T2, st2 = new_run("dt_owner_r3", {}, signoff="owner", max_rounds=3)
        mk(d2, T2, st2).run()
        toolkit.sign(d2, "owner", "ok")
        s_p = mk(d2, StubTasks(T2.s), None).run()
        st_p = load_state(d2)
        ok(s_p == "paused:awaiting_scope_signoff" and st_p.round == 1
           and "rule 2o" in str([h for h in st_p.history if "to" in h][-1].get("why")),
           f"T_HB2_FIRST_GATE_OWNER under --signoff owner the run pauses for the owner at round 1, not at round 3 "
           f"({s_p}, round {st_p.round})")
        _SS.sign(d2, "owner", all_decision="decline", note="the plan must not depend on this")
        s_d = mk(d2, StubTasks(T2.s), None).run()
        ok(s_d != "completed" and s_d.startswith(("completed_scope_declined", "run_failed")),
           f"T_HB2_DECLINED a declined plan-changing test never accepts ({s_d})")
        _SS.review(d, "decision_test_changes_plan:DT9", "overturn", note="fix the effect first")
        ok([r["review"]["decision"] for r in _SS.provisional(d)] == ["overturn"],
           "T_OWNER_LEAD_REVIEW the owner later overturns (or confirms) a provisional answer; it is recorded")
        # T_DT_TOOLKIT_FP (lead-gaps α): a toolkit finding's owner item binds the verified row of
        # analysis/decision_tests.yaml, so a changed result is asked again; a stale analysis.json in the run dir
        # never stands in for it (Codex r1 of #327)
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
                fc2 = _bearing_item(dk, dtf_t)["fingerprint"]
        finally:
            _CTk.decision_test_record = _rec
        ok(len({fa, fb, fc}) == 3 and fc2 == fc,
           f"T_DT_TOOLKIT_FP the toolkit finding binds its own decision-test record ({fa}, {fb}, {fc})")
        # T_OWNER_LEAD_AMEND (1F, D2): a non-HIGH owner-amendment finding under --signoff lead is answered
        # provisionally (owner policy 2026-11-24: critic HIGH amendments pause for the owner; tested by T_LEAD_NO_CRITIC_HIGH)
        d, T, st = new_run("amend_lead", {"critics": {1: {"critique-domain": [dict(amend, id="D-041",
                                                                                   severity="MEDIUM")]}}},
                           signoff="lead", max_rounds=2)
        s_a = _ToolkitMachine(d, T, st, log=lambda *a: None, cross_engine_check=False).run()
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
        s = _ToolkitMachine(d, T, st, log=lambda *a: None, cross_engine_check=False).run()
        ok(s == "paused:awaiting_budget" and T.calls == [] and load_state(d).state == "SPEC"
           and load_state(d).status is None,
           f"R3-4 a $0.13 allowance starts no task; the run pauses awaiting budget ({s}, {T.calls}, {T.allowances})")
        T2 = StubTasks(T.s)
        mp4 = os.path.join(d, "metadata.json")
        _mp = json.load(open(mp4))
        ok(_mp.get("status") == "paused:awaiting_budget" and "allowance" in str(_mp.get("paused_why")),
           f"PS3 the budget pause is recorded in metadata.json with the task's allowance ({_mp.get('paused_why')})")
        json.dump(dict(json.load(open(mp4)), invocations=[{"at": "2020-01-01T00:00:00"}, {"at": "2020-01-01T01:00:00"}]),
                  open(mp4, "w"))
        s2 = run_v2("toy question", d, T2, signoff="none", budget_usd=60.0, log=lambda *a: None,
                    backend=toolkit, cross_engine_check=False)
        m4, h4 = json.load(open(mp4)), load_state(d).history
        ok(m4["invocations"][-1].get("budget_usd") == 60.0 and m4["invocations"][-1].get("cap_usd") == 60.0
           and "budget_usd" not in m4["invocations"][0] and set(m4.get("models") or {}) == {"deep", "fast"}
           and m4.get("sdk_version") and m4["invocations"][-1].get("models") == m4["models"]
           and sum(1 for h in h4 if h.get("paused") == AWAITING_BUDGET) == 1
           and m4.get("status") == s2 and (s2.startswith("paused:") or "paused_state" not in m4),
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
        mb5 = json.load(open(os.path.join(dp, "metadata.json"))).get("motivated_by") or {}
        dq = os.path.join(td, "rr5_parent_rq")
        os.makedirs(dq)
        _write_json(dq, SPEC_RETHINK_REQUEST, {"round": 1, "findings": [{"id": "X-404"}], "briefs": []})
        mq = _relaunch_motivation(dq)
        open(os.path.join(dq, SPEC_RETHINK_REQUEST), "w").write("[]")
        mz = _relaunch_motivation(dq)
        ok(mb5.get("parent_run") == os.path.basename(d) and mb5.get("source") == (
            "rethink_request" if os.path.isfile(os.path.join(d, SPEC_RETHINK_REQUEST)) else "unstated")
           and mq.get("source") == "rethink_request" and [f.get("id") for f in mq.get("findings") or []] == ["X-404"]
           and mz.get("source") == "unreadable",
           f"RR5b a relaunch records motivated_by: the parent's rethink request's findings, else unstated; a malformed "
           f"request is unreadable ({mb5}, {mq}, {mz})")
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
        mb6 = json.load(open(os.path.join(td, "rr6_child0", "metadata.json"))).get("motivated_by") or {}
        ok(prs[0].get("run_state_sha256") is None and prs[0].get("error") and prs[1].get("run_dir") == os.path.basename(d)
           and mb6.get("source") == "unreadable",
           f"RR6 an unreadable parent records no hash (never linked); a relative parent path records its dir name ({prs})")
        st7 = load_state(d)
        if st7.state == "FINAL":
            st7.paused = "awaiting_scope_signoff"            # a stale flag a terminal run can carry
            save_state(d, st7)
            n7 = len(load_state(d).history)
            _ToolkitMachine(d, StubTasks(T.s), st7, log=lambda *a: None, cross_engine_check=False).run()
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
        fp8 = FL.answer_fingerprint(fid8, kind="k_demo", fix_requires="use the new data", claim="the old claim")
        json.dump({"answers": {fp8: {"finding": fid8, "decision": "decline", "by": "owner"}}},
                  open(os.path.join(d, SCOPE_SIGNOFF), "w"))
        _SS8.harness_record(d)
        _mod8 = sys.modules[Machine.__module__]
        _vg = _mod8.v2_gate
        _mod8.v2_gate = lambda *a, **k: {"action": "DECLARE_SCOPE", "rule_matched": "3", "rounds_remaining": 0,
                                         "rigor_violations": [v8], "unresolved_high": [dict(v8)]}
        try:
            _ToolkitMachine(d, T, None, log=lambda *a: None, cross_engine_check=False).step()
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
            s = _ToolkitMachine(d, T, st, log=lambda *a: None, cross_engine_check=False).run()
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
        g = v2_gate(dd, 2, 2, False, [dict(dtf)], history=False)
        own = [b for b in list(g.get("unresolved_high") or []) + list(g.get("accept_blockers") or [])
               if isinstance(b, dict) and b.get("owner_signoff")]
        ok(OWNER_LIFECYCLE_WIRED and _owner_rule() is not None and g["action"] == "AWAIT_OWNER" and own
           and (own[0].get("owner_item") or {}).get("fingerprint"),
           f"T30 (1F) a measured decision test that changes the plan goes to the owner: AWAIT_OWNER with a "
           f"fingerprinted item ({g['action']})")
        gn = v2_gate(dd, 2, 2, False, [dict(dtf)], history=False, signoff="none")
        ok(gn["action"] == "RUN_FAILED", f"T30b under --signoff none nobody can accept it: not declarable ({gn['action']})")
        # T_OWNER_ANSWERED (1F): the harness-recorded answer to that item releases it; whose answer counts follows the
        # run's sign-off mode (a lead's only under --signoff lead, D2)
        import scope_signoff as _ss
        _ss.record(dd, g, 2)
        _ss.sign(dd, "lead", all_decision="accept", note="t")          # not provisional: counts nowhere
        _ss.harness_record(dd)
        g_ln = v2_gate(dd, 2, 2, False, [dict(dtf)], history=False, signoff="lead")
        _ss.provisional_answers(dd, json.load(open(os.path.join(dd, _ss.REQUEST)))["items"], 2)
        _ss.harness_record(dd)
        g_lo = v2_gate(dd, 2, 2, False, [dict(dtf)], history=False)
        g_ll = v2_gate(dd, 2, 2, False, [dict(dtf)], history=False, signoff="lead")
        _ss.sign(dd, "owner", all_decision="decline", note="t")
        _ss.harness_record(dd)
        g_od = v2_gate(dd, 2, 2, False, [dict(dtf)], history=False)
        # owner decision (b), 2026-11-23: an accepted plan-changing test lets the gate ACCEPT (it was DECLARE_SCOPE)
        ok(g_ln["action"] == "AWAIT_OWNER" and g_lo["action"] == "AWAIT_OWNER" and g_ll["action"] == "ACCEPT"
           and [b.get("kind") for b in g_ll.get("owner_accepted_high") or []] == ["decision_test_changes_plan"]
           and g_od["action"] == "RUN_FAILED"
           and any(isinstance(u, dict) and u.get("kind") == "scope_declaration_declined"
                   for u in g_od.get("unresolved_high") or []),
           f"T_OWNER_ANSWERED a lead's accept counts only under --signoff lead; the owner's decline holds the gate "
           f"({g_lo['action']}, {g_ll['action']}, {g_od['action']})")
        # T_INTERVAL (hb3 fix 5, lead on #377): a degenerate headline interval is an owner item: unanswered the run
        # waits; accepted (the lead, provisionally, under --signoff lead) it is declared: DECLARE_SCOPE, not RUN_FAILED
        di = os.path.join(td, "interval")
        os.makedirs(di)
        for r in CRITICS:
            __import__("yaml").safe_dump({"reviewer": r, "round": 2, "verdict": "PASS", "structural_mismatch": {"detected": False},
                                          "carried_forward": [], "blockers": []}, open(os.path.join(di, V.FILENAMES[r]), "w"))
        from toolkit.analysis import contract as _CTi
        ivf = _CTi.interval_findings({"headline": {"name": "total_x", "outcome": "x"}, "fixed_plan": {
            "x": {"ci_low": 999.9995, "median": 1000.0, "ci_high": 1000.0005, "level": 0.95}}})[0]
        gi = v2_gate(di, 2, 2, False, [dict(ivf)], history=False)
        _ss.record(di, gi, 2)
        _ss.provisional_answers(di, json.load(open(os.path.join(di, _ss.REQUEST)))["items"], 2)
        _ss.harness_record(di)
        gi_l = v2_gate(di, 2, 2, False, [dict(ivf)], history=False, signoff="lead")
        gi_n = v2_gate(di, 2, 2, False, [dict(ivf)], history=False, signoff="none")
        ok(gi["action"] == "AWAIT_OWNER" and gi_l["action"] == "DECLARE_SCOPE" and gi_n["action"] == "RUN_FAILED",
           f"T_INTERVAL a degenerate headline interval goes to the owner; declared once accepted, not a run failure "
           f"({gi['action']}, {gi_l['action']}, {gi_n['action']})")
        # T_OWNER_DECLINE_HOLDS and T_OWNER_LEAD_NONPROV (Codex r2 of #316, P1-2 and P1-3): with a policy-mapped
        # finding beside the decision test, the owner's decline on it keeps holding the gate once the decision-test
        # finding clears; and a lead answer that is not provisional never releases it, even in a lead run
        for f_ in (_ss.SIGNOFF, _ss.LEDGER, _ss.REQUEST, "trace.jsonl"):
            if os.path.exists(os.path.join(dd, f_)):
                os.remove(os.path.join(dd, f_))
        open(os.path.join(dd, "decision_rule.md"), "w").write("rule\n")          # the run allocates
        alloc = {"kind": "allocation_unstable", "severity": "HIGH", "stage": "OPTIMIZATION",
                 "key": "allocation_robustness", "claim": "UNSTABLE", "scope_declarable": True}
        g2 = v2_gate(dd, 2, 2, False, [dict(dtf), dict(alloc)], history=False)
        _ss.record(dd, g2, 2)
        its = {i["kind"]: i for i in json.load(open(os.path.join(dd, _ss.REQUEST)))["items"]}
        both = "decision_test_changes_plan" in its and "allocation_unstable" in its
        if both:
            _ss.sign(dd, "owner", {its["decision_test_changes_plan"]["fingerprint"]: "accept",
                                   its["allocation_unstable"]["fingerprint"]: "decline"}, note="t")
            _ss.harness_record(dd)
        g_both = v2_gate(dd, 2, 2, False, [dict(dtf), dict(alloc)], history=False)
        g_alone = v2_gate(dd, 2, 2, False, [dict(alloc)], history=False)
        ok(both and g2["action"] == "AWAIT_OWNER" and g_both["action"] == "RUN_FAILED"
           and g_alone["action"] == "RUN_FAILED",
           f"T_OWNER_DECLINE_HOLDS the owner's decline of a policy-mapped finding holds the gate when the decision-test "
           f"finding clears ({g2['action']}, {g_both['action']}, {g_alone['action']}, {sorted(its)})")
        if both:
            doc_ = json.load(open(os.path.join(dd, _ss.SIGNOFF)))
            doc_["answers"][its["allocation_unstable"]["fingerprint"]].update(decision="accept", by="lead")
            json.dump(doc_, open(os.path.join(dd, _ss.SIGNOFF), "w"))
            _ss.harness_record(dd)
        g_np = v2_gate(dd, 2, 2, False, [dict(dtf), dict(alloc)], history=False, signoff="lead")
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
            _ToolkitMachine(d, T, st, log=lambda *a: None, cross_engine_check=False).run()
        except RuntimeError:
            pass
        T2 = StubTasks(dict(T.s, crash={}))
        m2 = _ToolkitMachine(d, T2, None, log=lambda *a: None, cross_engine_check=False)
        s = m2.run()
        j = load_state(d).journal
        spec_keys = sorted(k for k in j if k.startswith("SPEC#"))
        ok(s == "completed" and spec_keys == ["SPEC#0:try1"] and j["SPEC#0:try1"].get("interrupted_charged") == 1
           and j["SPEC#0:try1"]["status"] == "completed",
           f"T33 an interrupted spec draft is charged once and re-run under its own key {spec_keys}")
        d, T, st = new_run("orphan", {}, budget=100.0)
        st.journal["X#0:orphan"] = {"task": "critique", "status": "started", "allowance_usd": 2.5}
        save_state(d, st)
        m3 = _ToolkitMachine(d, StubTasks({}), None, log=lambda *a: None)
        ok(abs(m3.st.spent_usd - 2.5) < 1e-9 and m3.st.journal["X#0:orphan"]["status"] == "interrupted"
           and abs(_ToolkitMachine(d, StubTasks({}), None, log=lambda *a: None).st.spent_usd - 2.5) < 1e-9,
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
            # Owner decision (b), 2026-11-23 (hb2 fix 1): the lead's provisional accept lets it end accepted, with the
            # dependence stated above the draft (it ended completed_scope_declared before)
            # the expected ids are read from the artifact itself, not through the reader under test (Codex r1 of #327)
            _dt36 = (yaml.safe_load(open(os.path.join(d, "analysis", "decision_tests.yaml"))) or {}).get("data") or {}
            want = sorted(f"decision_test_changes_plan:{t.get('test_id')}" for t in _dt36.get("tests") or []
                          if t.get("verdict") == "CHANGES_PLAN")
            import scope_signoff as _SS36
            # (asked again after the fix moved their evidence: one finding may carry two answers)
            prov36 = sorted({r["finding"] for r in _SS36.provisional(d) if r.get("plan_changing")})
            rep36 = open(os.path.join(d, "report.md")).read() if os.path.exists(os.path.join(d, "report.md")) else ""
            sp36 = json.load(open(os.path.join(d, "report_render.json")))["writer_span"] if rep36 else [0, 0]
            # hb3 fix 4 (#375): a test the spec declares that did not run (the toy's DT1 has no benchmark results here)
            # is a declarable HIGH, so the run is declared, not accepted, and the ledger holds it; read from the artifact
            _decl36 = {str(t.get("id")) for t in (raw.get("analysis") or {}).get("decision_tests") or []}
            nr36 = sorted(f"decision_test_not_run:{t.get('test_id')}" for t in _dt36.get("tests") or []
                          if t.get("status") == "not_run" and str(t.get("test_id")) in _decl36)
            led36 = FL.load(d)
            exp36 = "completed_scope_declared" if nr36 else "completed"
            # Codex r1 of #375 P2-5 (seen here): no fix loop with nothing to do (no task, no owner amendment, no
            # write-up) runs: an accepted not-run test is declared at the gate, not sent to an empty fix loop
            hist36 = load_state(d).history
            empty36 = [h["fix_loop"].get("round") for h in hist36 if isinstance(h.get("fix_loop"), dict)
                       and not h["fix_loop"].get("tasks") and not h["fix_loop"].get("owner_amendments")
                       and not h["fix_loop"].get("write_up")]
            c36 = {"status": s == exp36, "manifest": bool(man), "fix_row": bool(rows) and rows[0]["files_changed"] == [
                       "analysis_settings.yaml"],
                   "dependence_top": bool(nr36) or "The plan depends on inputs the lead accepted provisionally"
                   in rep36[:sp36[0]],
                   "not_run_in_ledger": all(i in led36 for i in nr36), "no_empty_fix_loop": empty36 == [],
                   "d001_fixed": led36["D-001"].status == "fixed", "provisional": prov36 == want,
                   "never_paused": not any(h.get("paused") for h in hist36),
                   "flagged_top": all(w in rep36[:sp36[0]] for w in want)}
            bad36 = [k for k, v in c36.items() if not v]
            ok(want and not bad36,
               f"T36 the default backend (workstream B) runs the machine end to end with engine: auto; each decision "
               f"test that changes the plan is answered provisionally and flagged above the draft ({s}, {want}, {prov36}, "
               f"not run: {nr36}, failing: {bad36}, empty fix loops: {empty36}"
               + ("" if not bad36 else   # why it ended: CI showed only the status (flaky once on #156 and #160)
                  f"; path {[h.get('to') for h in hist36 if 'to' in h]}; "
                  f"final {[h.get('why') for h in hist36 if h.get('to') == 'FINAL'][-1:]}; "
                  f"D-001 {led36['D-001'].status}") + ")")
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
        s = _ToolkitMachine(d, T, st, log=lambda *a: None, cross_engine_check=False).run()
        ok(s in ("completed", "completed_scope_declared") and "feedback:critique-domain:True" in T.calls
           and T.calls.count("critique:critique-redteam:1") == 1,
           f"T37 a schema repair re-runs only the failing critic, with its errors ({s}, {[c for c in T.calls if 'critique' in c][:6]})")

        # T37b: invalid YAML in critique_presentation.yaml ("invalid YAML in /.../critique_presentation.yaml: ...")
        # routes directly back to CRITIQUE with its errors, never re-runs domain/redteam, and finishes cleanly.
        class BadPresYaml(StubTasks):
            def critique(self, run_dir, reviewer, round_n, feedback=None):
                res = super().critique(run_dir, reviewer, round_n)
                if reviewer == "critique-presentation" and not feedback:
                    p = os.path.join(run_dir, "critique_presentation.yaml")
                    open(p, "w").write("reviewer: critique-presentation\nround: 1\nevidence: a (events: bad)\n")
                self.calls.append(f"feedback:{reviewer}:{bool(feedback)}")
                return res
        d_p, T_p, st_p = new_run("pres_yaml_repair", {})
        T_p = BadPresYaml(T_p.s)
        s_p = _ToolkitMachine(d_p, T_p, st_p, log=lambda *a: None, cross_engine_check=False).run()
        hist_p = load_state(d_p).history
        pres_route = any(h.get("from") == "GATE" and h.get("to") == "CRITIQUE" for h in hist_p)
        ok(s_p in ("completed", "completed_scope_declared") and pres_route
           and "feedback:critique-presentation:True" in T_p.calls
           and T_p.calls.count("critique:critique-domain:1") == 1
           and T_p.calls.count("critique:critique-redteam:1") == 1
           and os.path.exists(os.path.join(d_p, "report.md")),
           f"T37b invalid presentation YAML routes to CRITIQUE with errors and keeps report.md "
           f"({s_p}, pres_route={pres_route}, calls={[c for c in T_p.calls if 'critique' in c or 'feedback' in c]})")
        # T37c: a presentation schema error whose body mentions another critique filename (e.g. reviewer: critique_domain.yaml)
        # still routes to CRITIQUE via schema_errors_by_reviewer, never re-running critique-domain.
        class BadPresReviewer(StubTasks):
            def critique(self, run_dir, reviewer, round_n, feedback=None):
                res = super().critique(run_dir, reviewer, round_n)
                if reviewer == "critique-presentation" and not feedback:
                    import yaml
                    p = os.path.join(run_dir, "critique_presentation.yaml")
                    doc = yaml.safe_load(open(p))
                    doc["reviewer"] = "critique_domain.yaml"
                    yaml.safe_dump(doc, open(p, "w"))
                self.calls.append(f"feedback:{reviewer}:{bool(feedback)}")
                return res
        d_p2, T_p2, st_p2 = new_run("pres_reviewer_repair", {})
        T_p2 = BadPresReviewer(T_p2.s)
        s_p2 = _ToolkitMachine(d_p2, T_p2, st_p2, log=lambda *a: None, cross_engine_check=False).run()
        hist_p2 = load_state(d_p2).history
        ok(s_p2 in ("completed", "completed_scope_declared")
           and any(h.get("from") == "GATE" and h.get("to") == "CRITIQUE" for h in hist_p2)
           and "feedback:critique-presentation:True" in T_p2.calls
           and T_p2.calls.count("critique:critique-domain:1") == 1,
           f"T37c presentation error naming critique_domain.yaml routes to CRITIQUE ({s_p2})")

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
        g = v2_gate(r2, 2, 3, False, [], history=False, strict_done_when=True)
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
        g = v2_gate(r3, 2, 3, False, [], history=False, strict_done_when=True)
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
            g = v2_gate(r3, 2, 3, False, [], history=False, strict_done_when=True)
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
        s = _ToolkitMachine(d, T, st, log=lambda *a: None, cross_engine_check=False).run()
        ok(s in ("completed", "completed_scope_declared") and "repair:3" in T.calls,
           f"T75 a round-2 critic that resolved a blocker in the wrong shape is repaired in one pass and the "
           f"gate decides ({s}, {[c for c in T.calls if 'repair' in c]})")

        # T38-T41: from the first blind real run (v2blind1). Fixtures: its three critique YAMLs,
        # final_gate.json and claims ledger (tests/fixtures/v2blind1; no question text, no evals content).
        import yaml
        FX_DIR = os.path.join(REPO, "tests", "fixtures", "v2blind1")
        fb = os.path.join(td, "blind1")
        os.makedirs(fb)
        for f in ("critique_domain.yaml", "critique_redteam.yaml", "critique_presentation.yaml"):
            shutil.copy(os.path.join(FX_DIR, f), fb)
        open(os.path.join(fb, "manuscript.md"), "w").write("draft\n")
        g = v2_gate(fb, 1, 2, True, [], history=False, strict_done_when=True)
        errs = " ".join(g.get("schema_errors") or [])
        ok(g.get("action") is None and "critique_domain.yaml: blocker D-001 done_when" in errs
           and "critique_presentation.yaml: blocker P-001 done_when" in errs,
           f"T38 a prose done_when is a schema error the critic is asked to repair ({errs[:160]})")
        g = v2_gate(fb, 1, 2, True, [], history=False, strict_done_when=False)
        ok(g.get("action") == "ACCEPT", f"T38b after the repairs the gate itself still accepts ({g.get('action')})")
        # T39: an open MEDIUM with a valid test goes to the fix loop while rounds remain; else it is declared.
        for f in ("critique_domain.yaml", "critique_redteam.yaml", "critique_presentation.yaml"):
            doc = yaml.safe_load(open(os.path.join(fb, f)))
            for b in doc["blockers"]:
                b["done_when"] = [{"judgement": str(b.get("done_when"))[:200]}]
            yaml.safe_dump(doc, open(os.path.join(fb, f), "w"))
        g = v2_gate(fb, 1, 2, True, [], history=False, strict_done_when=True)
        opened = open_findings(fb, g)
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
        render_report(fr, toolkit)
        rep = open(os.path.join(fr, "report.md")).read()
        ok("[CLAIM:" not in rep and "headline.point" not in rep.split("<!-- harness")[0] and not report_claim_findings(fr),
           f"T40 claim references are rendered inline ({rep.splitlines()[0][:120]})")
        open(os.path.join(fr, "manuscript.md"), "w").write("ITN in mix.itn districts; [CLAIM:no.such.claim].\n")   # a known id in inline code renders (CR3)
        render_report(fr, toolkit)
        kinds = [(f["kind"], f["severity"]) for f in report_claim_findings(fr)]
        ok(("report_claim_id_raw", "HIGH") in kinds and ("report_claim_unresolved", "HIGH") in kinds,
           f"T40b a raw claim id or an unresolved reference in the report is a HIGH finding ({kinds})")
        # T41: every input the question names is copied into data/ with its sha256; a spec citing a
        # file outside the run is refused.
        fp = os.path.join(td, "prov")
        os.makedirs(fp)
        staged = stage_inputs(fp, "Inputs: requirements.txt (the pins) and no/such/file.csv.")
        prov = json.load(open(os.path.join(fp, "data", "inputs_provenance.json")))
        src = open(os.path.join(REPO, "requirements.txt"), "rb").read()
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
        s = _ToolkitMachine(d, T, st, log=lambda *a: None, cross_engine_check=False).run()
        g1 = json.load(open(os.path.join(d, "gate_r1.json")))
        ok(s == "completed" and g1["action"] == "PATCH_OR_RETHINK" and g1["rule_matched"] == "4m"
           and FX.impact_rows(d) and FX.impact_rows(d)[0]["findings"] == ["D-010"],
           f"T42 an open MEDIUM with a test is fixed in the loop before ACCEPT ({s}, {g1['action']} {g1['rule_matched']})")
        d, T, st = new_run("medium_declare", {"critics": {1: {"critique-domain": [dict(med, id="D-011")]}}}, max_rounds=1)
        s = _ToolkitMachine(d, T, st, log=lambda *a: None, cross_engine_check=False).run()
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
        ok({o["id"] for o in open_findings(d43, g43)} == {"done_when_unmet:D-001", "critique_presentation_missing"},
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
            gof = v2_gate(dof, 1, 2, False, [], history=False)
            ok(gof.get("action") is None and any("critique_domain.yaml" in str(e) for e in gof.get("schema_errors") or []),
               f"OF1 [{tag}] the gate takes no action on an unreadable reviewer YAML and names it ({gof})")
            try:
                oof = open_findings(dof, {"action": "ACCEPT", "rigor_violations": []})
            except Exception as e:  # noqa: BLE001
                oof = [f"raised {type(e).__name__}"]
            ok(any(isinstance(o, dict) and o.get("id") == "critique_unreadable:critique-domain"
                   and o.get("severity") == "HIGH" for o in oof),
               f"OF2 [{tag}] open_findings reports the unreadable reviewer YAML as an open HIGH ({oof})")
        fr = os.path.join(td, "t44")
        os.makedirs(os.path.join(fr, "models"))
        shutil.copy(os.path.join(FX_DIR, "claims_ledger.yaml"), os.path.join(fr, "models", "claims_ledger.yaml"))
        yaml.safe_dump({"schema_version": 1, "constraints": {"envelope_usd": 18000000}}, open(os.path.join(fr, AB.SPEC_FILE), "w"))
        open(os.path.join(fr, "manuscript.md"), "w").write("Intro.\n<!-- harness: copied -->\nheadline.point and [CLAIM:nope].\n")
        k44b = {f["kind"] for f in report_claim_findings(fr)}
        ok({"report_claim_id_raw", "report_claim_unresolved"} <= k44b, f"T44b no marker the writer copies hides an id ({k44b})")
        open(os.path.join(fr, "manuscript.md"), "w").write("Averts [CLAIM:headline.point] cases.\n")
        open(os.path.join(fr, "report.md"), "w").write("stale [CLAIM:headline.point]\n")
        m45 = Machine(fr, StubTasks({}), RunState(question="q", state="FIX_LOOP"), backend=toolkit, log=lambda *a: None)
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
        # T50f (baseline hb2 §4.6): the spec cited a question file by its repository path; its staged copy
        # existed. Both refusals name the staged path, so the next try cites it.
        for cited in ("requirements.txt", "./requirements.txt"):
            spec = {"decision_units": {"csv": "data/inputs/requirements.txt"},
                    "calibration": {"targets": [{"id": "t", "csv": cited}]}}
            yaml.safe_dump(spec, open(os.path.join(fk, AB.SPEC_FILE), "w"))
            rc = [x for x in record_cited_inputs(fk) if "does not exist" in x]
            dr = [p["claim"] for p in P_.derivation_problems(fk, spec, required=True) if p["code"] == "data_ref_not_derived"]
            ok(rc and all("data/inputs/requirements.txt" in x for x in rc)
               and dr and all("data/inputs/requirements.txt" in x for x in dr),
               f"T50f a cited path whose staged copy exists: the refusals name the staged path ({rc}; {dr})")
        # T50g (Codex r1 of #355): a qualified path in another folder (a derived table) is not matched to a staged
        # input by its file name: following that hint would skip the derivation.
        spec = {"decision_units": {"csv": "data/inputs/requirements.txt"},
                "calibration": {"targets": [{"id": "t", "csv": "data/derived/requirements.txt"}]}}
        yaml.safe_dump(spec, open(os.path.join(fk, AB.SPEC_FILE), "w"))
        rc = [x for x in record_cited_inputs(fk) if "does not exist" in x]
        ok(rc and not any("staged copy" in x for x in rc),
           f"T50g a qualified path is matched to a staged input only by its full path ({rc})")
        # T50h (Codex r2 of #355): the units table must be a derivation's output, so its hint names the staged file as
        # the derivation's input, never as the path to cite.
        spec = {"decision_units": {"csv": "requirements.txt"}}
        yaml.safe_dump(spec, open(os.path.join(fk, AB.SPEC_FILE), "w"))
        rc = [x for x in record_cited_inputs(fk) if "does not exist" in x]
        ok(rc and all("derivation" in x and "cite that path" not in x for x in rc),
           f"T50h the units table's hint says to read the staged file in a derivation ({rc})")
        rc = [x for x in record_cited_inputs(fk, derived_inputs=False) if "does not exist" in x]
        ok(rc and all("cite that path" in x for x in rc),
           f"T50i (Codex r3) a run that runs no derivations is told to cite the staged units table itself ({rc})")
        for kind, key in (("group", {"csv": {"file": "data/missing.csv"}, "units": "state"}),
                          ("unit", {"csv": "data/missing.csv"})):    # Codex: only a group target's column name
            spec = {"decision_units": {"csv": "data/inputs/requirements.txt"}, "calibration": {"joint": {"targets": [
                {"id": "g", "kind": kind, "csv": "data/inputs/requirements.txt", "key": key}]}}}
            yaml.safe_dump(spec, open(os.path.join(fk, AB.SPEC_FILE), "w"))
            ok(any("data/missing.csv" in x for x in record_cited_inputs(fk)),
               f"T50e a {kind} target's key.csv that is not a group column name is still scanned for files")
        open(os.path.join(fr, "manuscript.md"), "w").write("`headline.point`\n")
        os.remove(os.path.join(fr, STATE_FILE))           # T45's machine left its state here
        m51 = Machine(fr, StubTasks({}), RunState(question="q", state="GATE", v2_rules=False), backend=toolkit,
                      log=lambda *a: None)
        ok(not any(f["kind"].startswith("report_") for f in m51.harness_findings()),
           "T51 golden replays (v2_rules off) do not run the v2 report checks")

        open(os.path.join(fr, "manuscript.md"), "w").write("The plan averts\n```\n[CLAIM:headline.point]\n```\n")
        render_report(fr, toolkit)
        ok(any(f["kind"] == "report_claim_unrendered" for f in report_claim_findings(fr)),
           "T54 a claim reference left unrendered in the report (in a fenced block) is a HIGH finding")
        fe = os.path.join(td, "t55")
        os.makedirs(os.path.join(fe, "data"))
        yaml.safe_dump({"engine": {"ref": "toolkit.engines.contract:ToyEngine",
                                   "config": {"results_path": os.path.join(td, "outside_bench")}}},
                       open(os.path.join(fe, AB.SPEC_FILE), "w"))
        os.makedirs(os.path.join(td, "outside_bench"), exist_ok=True)
        ok(any("outside_bench" in x for x in record_cited_inputs(fe)),
           "T55 an engine config path outside the run, without a file extension, is refused")

        # T23: a transition outside the table is refused.
        m = _ToolkitMachine(os.path.join(td, "ref"), StubTasks({}), None, log=lambda *a: None)
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
        # T70 (run console review, finding 6): `sign --expect-sha256` refuses a spec other than the one shown.
        from toolkit.spec import schema as _S, signoff as _SO
        d70 = os.path.join(td, "t61")
        os.makedirs(d70)
        _S.toy_spec(d70)
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            try:
                rc = sm_main(["sign", d70, "--by", "owner", "--note", "n", "--expect-sha256", "0" * 64])
            except SystemExit as e:
                rc = e.code
            st70 = _SO.status(d70)
            try:
                rc2 = sm_main(["sign", d70, "--by", "owner", "--note", "n", "--expect-sha256",
                            hashlib.sha256(open(os.path.join(d70, AB.SPEC_FILE), "rb").read()).hexdigest()])
            except SystemExit as e:
                rc2 = e.code
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
                rc71 = sm_main(["sign", d71, "--by", "owner", "--note", "n", "--expect-sha256",
                             hashlib.sha256(open(os.path.join(d71, AB.SPEC_FILE), "rb").read()).hexdigest(),
                             "--expect-derivations-sha256", "0" * 64])
            except SystemExit as e:
                rc71 = e.code
            st71 = _SO.status(d71)
            try:
                rc71b = sm_main(["sign", d71, "--by", "owner", "--note", "n", "--expect-sha256",
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
            op80 = open_findings(f80, {"action": "ACCEPT", "rigor_violations": []})
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
            render_report(f80, toolkit, status="completed_scope_declared")
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
            nj = {"action": "ACCEPT", "rule_matched": 4,
                  "done_when_status": {"D-009": {"passes": None, "result": "not_judged", "open": True,
                                                 "severity": "MEDIUM", "detail": "not judged"}}}
            r83 = require_judged(dict(nj))
            ok(r83["action"] != "ACCEPT" and r83.get("check_summary", {}).get("not_judged") == 1
               and "D-009" in json.dumps(r83),
               f"T83 a required check that is not judged cannot yield ACCEPT ({r83.get('action')})")
        except Exception as e:  # noqa: BLE001
            ok(False, f"T83 raised {type(e).__name__}: {e}")
        # T84c-T84e (Codex r1-r3 of #355): only a post-write gate's decision retires a stale report_withheld.md;
        # a run that ends before it (the budget, at WRITE) keeps it, and a directory at that name never stops FINAL.
        t84 = {}
        for case, frm, gate, wh_dir in (("budget_at_write", "WRITE", {}, False),
                                        ("gate_no_action", "GATE", {"post_write": True, "action": None}, False),
                                        ("gate_dir", "GATE", {"post_write": True, "action": "ACCEPT"}, True),
                                        ("gate_ok", "GATE", {"post_write": True, "action": "ACCEPT"}, False)):
            dc = os.path.join(td, "t84c_" + case)
            os.makedirs(dc)
            open(os.path.join(dc, "report.md"), "w").write("repaired\n")
            if wh_dir:
                os.makedirs(os.path.join(dc, "report_withheld.md"))
            else:
                open(os.path.join(dc, "report_withheld.md"), "w").write("rejected\n")
            mc = Machine(dc, StubTasks({}), RunState(question="q", state=frm, v2_rules=False, last_gate=gate),
                         backend=toolkit, log=lambda *a: None)
            try:
                mc._go("FINAL", "t", "budget_exhausted" if frm == "WRITE" else "completed")
                t84[case] = os.path.lexists(os.path.join(dc, "report_withheld.md"))
            except Exception as e:  # noqa: BLE001 -- the test is that it does not raise
                t84[case] = repr(e)
        dc = os.path.join(td, "t84d")
        os.makedirs(dc)
        open(os.path.join(dc, "report.md"), "w").write("repaired\n")
        open(os.path.join(dc, "report_withheld.md"), "w").write("rejected\n")
        mc = Machine(dc, StubTasks({}), RunState(question="q", state="GATE", v2_rules=False,
                                                 last_gate={"post_write": True, "action": "VALIDATOR_ERROR"}),
                     backend=toolkit, log=lambda *a: None)
        mc._go("FINAL", "validator error", "completed_gate_error")
        t84["validator_error"] = os.path.lexists(os.path.join(dc, "report_withheld.md"))
        dc = os.path.join(td, "t84e")
        os.makedirs(dc)
        open(os.path.join(dc, "report.md"), "w").write("repaired\n")
        open(os.path.join(dc, "report_withheld.md"), "w").write("rejected\n")
        mc = Machine(dc, StubTasks({}), RunState(question="q", state="GATE", v2_rules=False,
                                                 last_gate={"post_write": True, "action": "RUN_FAILED"}),
                     backend=toolkit, log=lambda *a: None)
        mc._go("FINAL", "declined", "completed_scope_declined")
        t84["declined"] = os.path.lexists(os.path.join(dc, "report_withheld.md"))
        ok(t84 == {"budget_at_write": True, "gate_no_action": True, "gate_dir": True, "gate_ok": False,
                   "validator_error": True, "declined": False},
           f"T84c the withheld draft is retired only after a post-write gate decided, never a directory ({t84})")
        # T92: _withhold_report goes through lib.safe_io: a planted link at report_withheld.md is replaced,
        # never written through, and a linked report.md is removed without following the link.
        d92, o92 = os.path.join(td, "t92"), os.path.join(td, "t92out")
        os.makedirs(d92)
        os.makedirs(o92)
        open(os.path.join(o92, "v"), "w").write("untouched")
        open(os.path.join(d92, "report.md"), "w").write("rejected report\n")
        os.symlink(os.path.join(o92, "v"), os.path.join(d92, "report_withheld.md"))
        try:
            _withhold_report(d92)
            moved = (not os.path.exists(os.path.join(d92, "report.md"))
                     and not os.path.islink(os.path.join(d92, "report_withheld.md"))
                     and open(os.path.join(d92, "report_withheld.md")).read() == "rejected report\n")
            os.symlink(os.path.join(o92, "v"), os.path.join(d92, "report.md"))
            _withhold_report(d92)
            link_removed = not os.path.lexists(os.path.join(d92, "report.md"))
        except Exception as e:  # noqa: BLE001
            moved, link_removed = f"raised {type(e).__name__}: {e}", False
        ok(moved is True and link_removed and open(os.path.join(o92, "v")).read() == "untouched",
           f"T92 _withhold_report never follows a link ({moved}, linked report removed: {link_removed})")
        # T86-T87 through h_GATE: an open finding whose only test is a judgement is never accepted. The fix loop
        # and rule 3m handle it first; rule 3j is the backstop when no open finding is listed for it.
        judged_only = {"id": "D-086", "severity": "MEDIUM", "target_stage": "ANALYZE", "claim": "needs a judgement",
                       "done_when": [{"judgement": "the critic reads the caveat"}]}
        orig_open = sm.open_findings
        for name, patch, rule in (("t86", None, "3m"), ("t87", lambda rd, dd: [], "3j")):
            d, T, st = new_run(name, {"critics": {1: {"critique-domain": [judged_only]}}}, max_rounds=1)
            if patch:
                sm.open_findings = patch
            try:
                s = _ToolkitMachine(d, T, st, log=lambda *a: None, cross_engine_check=False).run()
            except Exception as e:  # noqa: BLE001
                s = f"raised {type(e).__name__}: {e}"
            finally:
                sm.open_findings = orig_open
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

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
