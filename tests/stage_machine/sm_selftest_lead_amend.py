"""Self-tests of agents/stage_machine.py (lead_amend), run by `python3 agents/stage_machine.py --self-test`.

Moved verbatim from agents/stage_machine.py by scripts/refactor/split_stage_machine.py.
The module under test is imported as _stage_machine_under_test.
"""
from __future__ import annotations

import hashlib
import json
import os
import analysis_backend as AB
import findings_ledger as FL
from _stage_machine_under_test import (
    AMENDED_PROVISIONAL, AMENDMENT_RECORD, AMEND_LOCK, AmendmentLocked, Machine, RunState, SPEC_RETHINK_REQUEST,
    STATE_FILE, _amend_lock, _write_json, amendment_lines, handoff_chain, handoff_child, harness_owned, load_state,
    run_v2, save_state,
)
from sm_selftest_lead_amend_r3 import _self_test_lead_amend_r3


def _self_test_lead_amend(ok, td: str) -> None:
    """lead-gaps β self-tests: S7 (a rethink of a signed spec) under --signoff lead with the fix plan's patch becomes a
    child run signed provisionally by the lead; under owner sign-off, or without a valid patch, the run pauses."""
    import yaml
    from v2_tasks import StubTasks
    from toolkit.spec import amend as AM, schema, signoff as SO

    def parent(name, mode, patch, spent=3.0):
        d = os.path.join(os.path.realpath(td), name)
        os.makedirs(d)
        _write_json(d, "metadata.json", {"pipeline": "v2", "commit": "0" * 40, "launcher": "t"})
        raw = yaml.safe_load(open(schema.toy_spec(d)))      # the toy's critique findings declared, as in amend's tests
        raw["principle_departures"] = [{"principle": 2, "check": "headline_comparator", "reason": "toy"},
                                       {"principle": 3, "check": "baseline_burden", "reason": "toy"}]
        yaml.safe_dump(raw, open(os.path.join(d, AB.SPEC_FILE), "w"), sort_keys=False)
        AM._pre_sign(d, False)
        SO.write_sheet(d)
        SO.sign(d, mode)
        open(os.path.join(d, FL.LEDGER), "w").write("")
        task = {"id": "T1", "findings": [], "action": "rethink", "owner_state": "SPEC", "brief": "raise the envelope"}
        _write_json(d, "fix_plan_r2.json", {"round": 2, "plan": {"tasks": [dict(task, **({"patch": patch} if patch
                                                                                     else {}))]}})
        st = RunState(question="q", state="SPEC", round=2, max_rounds=3, signoff=mode, budget_usd=5.0, spent_usd=spent)
        save_state(d, st)
        return d, open(os.path.join(d, AB.SPEC_FILE), "rb").read()

    good = {"id": "A1", "reason": "the envelope is the decision year's", "spec": [
        {"set": "constraints.envelope_usd", "value": 9500}]}
    d, spec0 = parent("lead_amend", "lead", good)
    m = Machine(d, StubTasks({}), log=lambda *a: None, cross_engine_check=False)
    m.step()
    st, child = load_state(d), handoff_child(d)
    rq = json.load(open(os.path.join(d, SPEC_RETHINK_REQUEST)))
    cst = load_state(child) if child else None
    ok(st.state == "FINAL" and st.status == AMENDED_PROVISIONAL and child == d + ".amend1" and rq.get("patch") == good
       and (rq.get("lead_amendment") or {}).get("status") == "signed_provisionally"
       and open(os.path.join(d, AB.SPEC_FILE), "rb").read() == spec0 and not any(h.get("paused") for h in st.history),
       f"T_LEAD_AMEND under --signoff lead the rethink's patch makes a child run and the parent ends "
       f"amended_provisional, unchanged and never paused ({st.state}, {st.status}, {child}, {rq.get('lead_amendment')})")
    ok(cst is not None and SO.status(child) == "signed" and (SO._spec_record(child) or {}).get("signed_by") == "lead"
       and "Provisional" in (SO._spec_record(child) or {}).get("note", "") and cst.state == "SIGN" and not cst.paused
       and cst.signoff == "lead" and cst.budget_usd == 2.0
       and yaml.safe_load(open(os.path.join(child, AB.SPEC_FILE)))["constraints"]["envelope_usd"] == 9500
       and Machine(child, StubTasks({}), log=lambda *a: None)._signed_by() == "lead",
       f"T_LEAD_AMEND_CHILD the child's spec carries the patch, signed by the lead provisionally; it resumes at SIGN with "
       f"what is left of the cap ({cst and (cst.state, cst.paused, cst.budget_usd)})")
    meta_c = json.load(open(os.path.join(child, "metadata.json"))) if child else {}
    ok((meta_c.get("parent_run") or {}).get("run_state_sha256")
       == hashlib.sha256(open(os.path.join(d, STATE_FILE), "rb").read()).hexdigest() and handoff_chain(d) == [child],
       "T_LEAD_AMEND_LINK the child's parent link binds the parent's final state; the chain lists the child")
    al = amendment_lines(child) if child else []
    rec_sha = hashlib.sha256(open(os.path.join(child, AB.SPEC_FILE), "rb").read()).hexdigest() if child else None
    ok(al and "Provisional amendment" in al[0] and "`A1`" in al[0] and "constraints.envelope_usd" in al[0]
       and "9500" in al[0] and amendment_lines(d) == [],
       f"T_LEAD_AMEND_REPORT the child's report heads with the amendment, as provisional ({al[:1]})")
    if child:            # run_v2 continues in the child (stopped here at FINAL to keep the test small)
        cst.state, cst.status = "FINAL", "completed"
        save_state(child, cst)
        seen = []
        s = run_v2("q", d, StubTasks({}), signoff="lead", log=seen.append, cross_engine_check=False)
        inv = json.load(open(os.path.join(child, "metadata.json"))).get("invocations") or []
        ok(s == "completed" and any("continuing in" in str(x) for x in seen) and inv and inv[-1].get("commit") == "0" * 40,
           f"T_LEAD_AMEND_HANDOFF run_v2 continues in the child and returns its status, stamped with the parent's "
           f"identity ({s}, {inv[-1:]})")
        rec2 = AM.amend(d, from_request=True, signoff="owner", log=lambda *a: None)     # the owner's confirmation
        SO.sign(rec2["child"], "owner", expect_sha256=rec2["spec_sha256"], expect_sheet_sha256=rec2["sheet_sha256"],
                expect_derivations_sha256=rec2["derivations_sha256"])
        ok("--from-request --signoff owner" in al[0] and amendment_lines(rec2["child"]) == []
           and rec2["spec_sha256"] == rec_sha,
           "T_LEAD_AMEND_OWNER the owner confirms by amending the parent with the same patch (the same spec) and "
           "signing; an owner-signed amendment is not provisional")
    for name, mode, patch, why in (("owner_amend", "owner", good, None),
                                   ("lead_nopatch", "lead", None, "no machine-readable patch"),
                                   ("lead_badpatch", "lead", {"id": "A2", "reason": "r", "spec": [
                                       {"set": "constraints.no_such_leaf", "value": 1}]}, "not found")):
        d, spec0 = parent(name, mode, patch)
        m = Machine(d, StubTasks({}), log=lambda *a: None, cross_engine_check=False)
        s = m.run()
        rq = json.load(open(os.path.join(d, SPEC_RETHINK_REQUEST)))
        la = rq.get("lead_amendment")
        ok(s == "paused:awaiting_spec_amendment" and not os.path.lexists(d + ".amend1")
           and open(os.path.join(d, AB.SPEC_FILE), "rb").read() == spec0
           and (la is None if why is None else (la.get("status") == "refused" and why in la.get("why", "")))
           and (mode == "lead" or "patch" not in rq),
           f"T_LEAD_AMEND_PAUSE {name}: the run pauses for the owner as before, nothing amended; an owner run's "
           f"request carries no generated patch, so `amend --from-request --patch` stays the owner's ({s}, {la})")
        if name == "lead_badpatch":
            s2 = Machine(d, StubTasks({}), log=lambda *a: None, cross_engine_check=False).run()
            la2 = json.load(open(os.path.join(d, SPEC_RETHINK_REQUEST))).get("lead_amendment")
            ok(s2 == s and la2 == la and handoff_chain(d, kept=True) == [],
               "T_LEAD_AMEND_REPLAY a resume keeps the first refusal (its reason) and amends nothing")
    # Codex r1: no budget left pauses (a zero cap is not an unlimited one)
    d, _ = parent("lead_nobudget", "lead", good, spent=4.99999)      # rounds to nothing left (Codex r2)
    s = Machine(d, StubTasks({}), log=lambda *a: None, cross_engine_check=False).run()
    la = json.load(open(os.path.join(d, SPEC_RETHINK_REQUEST))).get("lead_amendment") or {}
    ok(s == "paused:awaiting_spec_amendment" and "no budget" in la.get("why", "") and not os.path.lexists(d + ".amend1"),
       f"T_LEAD_AMEND_BUDGET with no budget left the lead does not amend ({s}, {la})")
    # Codex r1: an interruption after the child is signed is finished on resume, never repeated
    d, _ = parent("lead_interrupt", "lead", good)
    mi = Machine(d, StubTasks({}), log=lambda *a: None, cross_engine_check=False)

    def _boom(*a, **k):
        raise KeyboardInterrupt("stopped")
    mi._go = _boom
    try:
        mi.step()
    except KeyboardInterrupt:
        pass
    Machine(d, StubTasks({}), log=lambda *a: None, cross_engine_check=False).step()
    sti = load_state(d)
    ok(sti.status == AMENDED_PROVISIONAL and handoff_child(d) == d + ".amend1" and not os.path.lexists(d + ".amend2"),
       f"T_LEAD_AMEND_RESUME an interrupted handoff is finished with the same child ({sti.status})")
    # Codex r1: a restored copy (parent and child moved together) resumes its own child; a lost record keeps the flag
    import shutil as _sh
    mv = os.path.join(os.path.realpath(td), "moved")
    os.makedirs(mv)
    _sh.copytree(d, os.path.join(mv, "lead_interrupt"))
    _sh.copytree(d + ".amend1", os.path.join(mv, "lead_interrupt.amend1"))
    ok(handoff_child(os.path.join(mv, "lead_interrupt")) == os.path.join(mv, "lead_interrupt.amend1"),
       "T_LEAD_AMEND_MOVED a moved parent resolves its child beside it, not at the recorded absolute path")
    mc = os.path.join(mv, "lead_interrupt.amend1", "metadata.json")
    meta_m = json.load(open(mc))
    json.dump(dict(meta_m, parent_run=dict(meta_m["parent_run"], run_state_sha256="0" * 64)), open(mc, "w"))
    cyc = os.path.join(mv, "cyc")
    os.makedirs(cyc)
    save_state(cyc, RunState(question="q", state="FINAL", status=AMENDED_PROVISIONAL, history=[{"amended_to": cyc}]))
    _write_json(cyc, "metadata.json", {"parent_run": {"run_dir": cyc}})
    ok(handoff_child(os.path.join(mv, "lead_interrupt")) is None and handoff_child(cyc) is None
       and handoff_chain(cyc, kept=True) == [],
       "T_LEAD_AMEND_STRICT a sibling bound to another parent state, or the run itself, is never resumed (Codex r2)")
    json.dump(meta_m, open(mc, "w"))
    lost = os.path.join(mv, "lead_interrupt.amend1")
    os.remove(os.path.join(lost, AMENDMENT_RECORD))
    al2 = amendment_lines(lost)
    ok(al2 and "could not be read" in al2[0] and Machine(lost, StubTasks({}), log=lambda *a: None)._provisional_amendment()
       and AMENDMENT_RECORD in harness_owned(),
       f"T_LEAD_AMEND_LOST without its record the child still heads its report as provisional and refuses a second lead "
       f"amendment; no deliverable may overwrite the record ({al2[:1]})")
    # Owner decision (#330 review): a legacy decision-brief signature never satisfies the lead's check in v2
    d, spec0 = parent("lead_brief", "lead", good)
    _write_json(d, "decision_brief.signoff.json", {"signed_by": "owner", "sha256": {AB.SPEC_FILE: "0" * 64}})
    s = Machine(d, StubTasks({}), log=lambda *a: None, cross_engine_check=False).run()
    la = json.load(open(os.path.join(d, SPEC_RETHINK_REQUEST))).get("lead_amendment") or {}
    ok(s == "paused:awaiting_spec_amendment" and la.get("status") == "refused" and "decision brief" in la.get("why", "")
       and not os.path.lexists(d + ".amend1") and open(os.path.join(d, AB.SPEC_FILE), "rb").read() == spec0,
       f"T_LEAD_AMEND_BRIEF a legacy decision-brief signature is refused for a lead amendment, with the reason recorded "
       f"({s}, {la})")
    # Owner decision (#330 review): two processes resuming the same parent never both amend it. A held lock stops the
    # second resumer with a clear error and nothing written; a real race leaves one child and one clear failure.
    d, _ = parent("lead_locked", "lead", good)
    rq0 = open(os.path.join(d, SPEC_RETHINK_REQUEST), "rb").read() if os.path.exists(
        os.path.join(d, SPEC_RETHINK_REQUEST)) else None
    import threading
    errs = []

    def _second():                 # another holder: a thread of its own (the lock nests within one thread)
        try:
            Machine(d, StubTasks({}), log=lambda *a: None, cross_engine_check=False).step()
            errs.append(None)
        except AmendmentLocked as e:
            errs.append(str(e))
    with _amend_lock(d):
        t1 = threading.Thread(target=_second)
        t1.start()
        t1.join(60)
    err = errs[0] if errs else None
    rq1 = open(os.path.join(d, SPEC_RETHINK_REQUEST), "rb").read() if os.path.exists(
        os.path.join(d, SPEC_RETHINK_REQUEST)) else None
    ok(err and "another process" in err and not os.path.lexists(d + ".amend1") and rq1 == rq0
       and load_state(d).status is None,
       f"T_LEAD_AMEND_LOCKED a second resumer fails clearly while the lock is held and amends nothing ({err}; "
       f"{rq0 is None})")
    d, _ = parent("lead_race", "lead", good)
    real_amend, gate = AM.amend, threading.Barrier(2)

    def slow_amend(*a, **k):
        import time as _t
        _t.sleep(1.0)                 # both resumers overlap inside the amendment
        return real_amend(*a, **k)
    got = []

    def resume():
        try:
            gate.wait()
            Machine(d, StubTasks({}), log=lambda *a: None, cross_engine_check=False).step()
            got.append("ok")
        except AmendmentLocked as e:
            got.append(f"locked: {e}")
    AM.amend = slow_amend
    try:
        th = [threading.Thread(target=resume) for _ in range(2)]
        [t.start() for t in th]
        [t.join(60) for t in th]
    finally:
        AM.amend = real_amend
    ok(sorted(g[:6] for g in got) == ["locked", "ok"] and handoff_child(d) == d + ".amend1"
       and not os.path.lexists(d + ".amend2") and load_state(d).status == AMENDED_PROVISIONAL,
       f"T_LEAD_AMEND_RACE two racing resumers: one amends and hands off, the other fails clearly ({got})")
    # Codex r3: run_v2 holds the lock from before it reads the state to the hand-off, so a second process resuming a
    # handed-off parent fails before writing to the parent or its child
    child = handoff_child(d)
    snap = {f: open(f, "rb").read() for f in (os.path.join(d, STATE_FILE), os.path.join(child, "metadata.json"),
                                               os.path.join(child, STATE_FILE))}
    with _amend_lock(d):
        def other():
            try:
                run_v2("q", d, StubTasks({}), signoff="lead", log=lambda *a: None, cross_engine_check=False)
                got2.append("ran")
            except AmendmentLocked:
                got2.append("locked")
        got2 = []
        t2 = threading.Thread(target=other)
        t2.start()
        t2.join(60)
    ok(got2 == ["locked"] and all(open(f, "rb").read() == b for f, b in snap.items()),
       f"T_LEAD_AMEND_RUNV2_LOCK a second run_v2 on a held lead run fails clearly, parent and child untouched ({got2})")
    # Codex r3: recovering an interrupted hand-off still refuses a legacy decision-brief signature (the child is kept)
    d, _ = parent("lead_interrupt_brief", "lead", good)
    mi = Machine(d, StubTasks({}), log=lambda *a: None, cross_engine_check=False)
    mi._go = _boom
    try:
        mi.step()
    except KeyboardInterrupt:
        pass
    _write_json(d, "decision_brief.signoff.json", {"signed_by": "owner", "sha256": {AB.SPEC_FILE: "0" * 64}})
    s = Machine(d, StubTasks({}), log=lambda *a: None, cross_engine_check=False).run()
    la = json.load(open(os.path.join(d, SPEC_RETHINK_REQUEST))).get("lead_amendment") or {}
    ok(s == "paused:awaiting_spec_amendment" and la.get("status") == "refused" and "decision brief" in la.get("why", "")
       and la.get("child") == d + ".amend1" and handoff_child(d) is None,
       f"T_LEAD_AMEND_BRIEF_RESUME an interrupted hand-off with a legacy brief signature is refused, the child kept ({la})")
    ok(AMEND_LOCK.startswith("analysis/") and "analysis/*" in harness_owned(),
       "T_LEAD_AMEND_LOCK_PATH the lock lives under a path reserved for every run (no owner-mode deliverable changes)")
    ok(handoff_chain(d, kept=True) == [d + ".amend1"],
       "T_LEAD_AMEND_KEPT_CHAIN a refused amendment's verified kept child is stored with the run")
    _self_test_lead_amend_r3(ok, td, parent, good, _boom)
