"""Self-tests of agents/stage_machine.py (lead_amend_r3), run by `python3 agents/stage_machine.py --self-test`.

Moved verbatim from agents/stage_machine.py by scripts/refactor/split_stage_machine.py.
The module under test is imported as _stage_machine_under_test.
"""
from __future__ import annotations

import json
import os
import shlex
import analysis_backend as AB
import _stage_machine_under_test as _SM
from _stage_machine_under_test import (
    AMENDED_PROVISIONAL, AMENDMENT_RECORD, AmendmentLocked, Machine, SPEC_RETHINK_REQUEST, STATE_FILE,
    amendment_lines, handoff_chain, handoff_child, run_v2,
)
from sm_selftest_common import SM_GLOBALS
from sm_selftest_lead_amend_r4 import _self_test_lead_amend_r4


def _self_test_lead_amend_r3(ok, td: str, parent, good: dict, _boom) -> None:
    """Codex round 3 of #342 on the lead amendment (#330): findings 4-12, each from its reproduction."""
    import shutil as _sh
    import threading
    from v2_tasks import StubTasks
    tdr = os.path.realpath(td)

    def machine(rd):
        return Machine(rd, StubTasks({}), log=lambda *a: None, cross_engine_check=False)

    def interrupted(name):       # the child made and signed by the lead, the parent not yet FINAL
        d_ = parent(name, "lead", good)[0]
        mi = machine(d_)
        mi._go = _boom
        try:
            mi.step()
        except KeyboardInterrupt:
            pass
        return d_

    def pair(src, where):        # the parent and its child, copied together to another directory
        os.makedirs(os.path.join(tdr, where))
        for x in (src, src + ".amend1"):
            _sh.copytree(x, os.path.join(tdr, where, os.path.basename(x)))
        return os.path.join(tdr, where, os.path.basename(src))
    # P1-4: an interruption just after the parent's FINAL state is saved still leaves a child bound to that state
    d = parent("r3_final_window", "lead", good)[0]
    real_save = SM_GLOBALS["save_state"]

    def save_then_stop(rd, st):
        real_save(rd, st)
        if st.status == AMENDED_PROVISIONAL and os.path.realpath(rd) == d:
            raise KeyboardInterrupt("stopped after FINAL")
    SM_GLOBALS["save_state"] = save_then_stop
    try:
        machine(d).step()
    except KeyboardInterrupt:
        pass
    finally:
        SM_GLOBALS["save_state"] = real_save
    ok(_SM.load_state(d).status == AMENDED_PROVISIONAL and handoff_child(d) == d + ".amend1",
       f"T_R3_FINAL_WINDOW (P1-4) interrupted after the parent's FINAL is saved, the child already binds it "
       f"({handoff_child(d)})")
    # P1-5: recovering a restored copy of the parent alone never writes to the original tree
    d = interrupted("r3_restore")
    c0 = d + ".amend1"
    snap = {f: open(os.path.join(c0, f), "rb").read() for f in ("metadata.json", STATE_FILE, AMENDMENT_RECORD)}
    os.makedirs(os.path.join(tdr, "r3_restored"))
    r = os.path.join(tdr, "r3_restored", "r3_restore")
    _sh.copytree(d, r)
    s = machine(r).run()
    la = json.load(open(os.path.join(r, SPEC_RETHINK_REQUEST))).get("lead_amendment") or {}
    ok(all(open(os.path.join(c0, f), "rb").read() == b for f, b in snap.items()) and handoff_child(r) is None
       and s == "paused:awaiting_spec_amendment" and la.get("status") == "refused" and c0 not in json.dumps(la),
       f"T_R3_RESTORED_ALONE (P1-5) a restored parent without its child is refused, the original child untouched "
       f"({s}, {la})")
    # P1-6: the parent's signature is checked again when an interrupted hand-off is recovered
    d = interrupted("r3_resig")
    open(os.path.join(d, AB.SPEC_FILE), "ab").write(b"# edited after signing\n")
    s = machine(d).run()
    la = json.load(open(os.path.join(d, SPEC_RETHINK_REQUEST))).get("lead_amendment") or {}
    ok(s == "paused:awaiting_spec_amendment" and la.get("status") == "refused"
       and "sign-off no longer holds" in la.get("why", "") and handoff_child(d) is None,
       f"T_R3_RECOVER_PARENT_SIG (P1-6) a recovered hand-off whose parent spec changed since signing is refused ({la})")
    # P1-10 and P1-11: strict lineage needs the matching parent digest; the kept-child fallback only follows a refusal
    src = interrupted("r3_lineage")
    machine(src).step()
    for where, digest in (("r3_nodigest", None), ("r3_otherdigest", "0" * 64)):
        p = pair(src, where)
        mc = os.path.join(p + ".amend1", "metadata.json")
        meta = json.load(open(mc))
        pr = {k: v for k, v in meta["parent_run"].items() if k != "run_state_sha256"}
        json.dump(dict(meta, parent_run=dict(pr, **({"run_state_sha256": digest} if digest else {}))), open(mc, "w"))
        ok(_SM.load_state(p).status == AMENDED_PROVISIONAL and handoff_child(p) is None
           and handoff_chain(p) == [] and handoff_chain(p, kept=True) == [],
           f"T_R3_STRICT_LINEAGE (P1-10, P1-11) {where}: a child without the parent's final digest is neither resumed "
           f"nor stored with it ({handoff_chain(p, kept=True)})")
    # P2-12: a parent and child moved together, the original tree gone: the confirmation names the moved parent
    p = pair(src, "r3_moved")
    _sh.move(src, src + ".gone")
    try:
        al = amendment_lines(p + ".amend1")
    finally:
        _sh.move(src + ".gone", src)
    ok(al and shlex.quote(p) in al[0] and f"`{src}`" not in al[0] and f"{shlex.quote(src)} " not in al[0],
       f"T_R3_MOVED_COMMAND (P2-12) the owner's confirmation command names the moved parent ({al[:1]})")
    # P1-8: the parent stays held across the hand-off; a second resumer cannot take over its child
    d = parent("r3_handover", "lead", good)[0]
    paused, go, got_b = threading.Event(), threading.Event(), []

    def log_a(msg):
        if "continuing in" in str(msg):
            cst = _SM.load_state(d + ".amend1")
            cst.state, cst.status = "FINAL", "completed"     # the child stops at once (the test stays small)
            _SM.save_state(d + ".amend1", cst)
            paused.set()
            go.wait(60)
    res_a = []
    ta = threading.Thread(target=lambda: res_a.append(run_v2("q", d, StubTasks({}), signoff="lead", log=log_a,
                                                             cross_engine_check=False)))
    ta.start()
    paused.wait(60)

    def second():
        try:
            got_b.append(run_v2("q", d, StubTasks({}), signoff="lead", log=lambda *a: None, cross_engine_check=False))
        except AmendmentLocked:
            got_b.append("locked")
    tb = threading.Thread(target=second)
    tb.start()
    tb.join(60)
    go.set()
    ta.join(60)
    ok(got_b == ["locked"] and res_a == ["completed"],
       f"T_R3_HANDOVER (P1-8) a second resumer during the hand-off fails clearly; the first continues in the child "
       f"({got_b}, {res_a})")
    # P1-9: a direct Machine whose constructor read the state before another process's hand-off never writes it back
    d = parent("r3_stale_ctor", "lead", good)[0]
    real_load, read, release, errs = SM_GLOBALS["load_state"], threading.Event(), threading.Event(), []
    blocked = []

    def slow_load(rd):
        st_ = real_load(rd)
        if threading.current_thread().name == "r3-second" and not blocked:
            blocked.append(1)
            read.set()
            release.wait(30)
        return st_

    def ctor():
        try:
            machine(d)
        except AmendmentLocked as e:
            errs.append(str(e))
    SM_GLOBALS["load_state"] = slow_load
    try:
        t2 = threading.Thread(target=ctor, name="r3-second")
        t2.start()
        read.wait(30)
        try:
            machine(d).step()
        except AmendmentLocked as e:
            errs.append(str(e))
        release.set()
        t2.join(60)
    finally:
        SM_GLOBALS["load_state"] = real_load
    made = os.path.lexists(d + ".amend1")
    ok(made == (_SM.load_state(d).status == AMENDED_PROVISIONAL),
       f"T_R3_STALE_CTOR (P1-9) a constructor that read the state before the hand-off never writes it back "
       f"(child made: {made}; parent status {_SM.load_state(d).status}; {len(errs)} refused)")
    _self_test_lead_amend_r4(ok, tdr, parent, good, interrupted, pair, machine)
