"""Self-tests of agents/stage_machine.py (lead_amend_r5), run by `python3 agents/stage_machine.py --self-test`.

Moved verbatim from agents/stage_machine.py by scripts/refactor/split_stage_machine.py.
The module under test is imported as _stage_machine_under_test.
"""
from __future__ import annotations

import json
import os
import analysis_backend as AB
import _stage_machine_under_test as _SM
from _stage_machine_under_test import (
    AMENDED_PROVISIONAL, SPEC_RETHINK_REQUEST, STATE_FILE, amendment_lines, handoff_chain, handoff_child,
)
from sm_selftest_common import SM_GLOBALS


def _self_test_lead_amend_r5(ok, tdr: str, interrupted, pair, machine) -> None:
    """Codex round 2 of #349: the child matched by its patch's content and the parent's own sign-off; storage of a
    kept or unfinished child."""
    import shutil as _sh
    from toolkit.spec import signoff as SO
    # R2-1: the same patch id with other content is not this request's child
    d = interrupted("r5_patch")
    rp = os.path.join(d, SPEC_RETHINK_REQUEST)
    req = json.load(open(rp))
    _SM._write_json(d, SPEC_RETHINK_REQUEST, dict(req, patch=dict(req["patch"], spec=[
        {"set": "constraints.envelope_usd", "value": 8700}])))
    s = machine(d).run()
    la = json.load(open(rp)).get("lead_amendment") or {}
    ok(s == "paused:awaiting_spec_amendment" and la.get("status") == "refused" and handoff_child(d) is None,
       f"T_R5_PATCH_CONTENT a child made from another patch under the same id is not adopted ({s}, {la.get('why')})")
    # R2-2: a kept child and its request must match the parent's own sign-off record, not only each other
    d = interrupted("r5_signoff")
    _SM._write_json(d, "decision_brief.signoff.json", {"signed_by": "owner", "sha256": {AB.SPEC_FILE: "0" * 64}})
    machine(d).run()                                  # refused (the legacy brief), the child kept
    sop = os.path.join(d, SO.SPEC_SIGNOFF)
    so0 = open(sop, "rb").read()
    rec = json.loads(so0)
    rec["sha256"] = dict(rec.get("sha256") or {}, **{AB.SPEC_FILE: "e" * 64})
    open(sop, "w").write(json.dumps(rec))
    other = handoff_chain(d, kept=True)
    open(sop, "wb").write(so0)
    # R2-7: a damaged parent state does not drop a recorded refusal's kept child
    stp = os.path.join(d, STATE_FILE)
    st0 = open(stp, "rb").read()
    damaged = []
    for bad_state in (st0[:20], b"[]"):              # unparseable, and parseable but not a state (Codex r3 of #349)
        open(stp, "wb").write(bad_state)
        try:
            damaged.append(handoff_chain(d, kept=True))
        except Exception as e:  # noqa: BLE001 -- the finding
            damaged.append(type(e).__name__)
    open(stp, "wb").write(st0)
    damaged = damaged[0] if damaged[0] == damaged[1] else damaged
    # R1-5 follow-up: a malformed lead_amendment record is not an error
    req = json.load(open(os.path.join(d, SPEC_RETHINK_REQUEST)))
    _SM._write_json(d, SPEC_RETHINK_REQUEST, dict(req, lead_amendment=["bad"]))
    try:
        bad = handoff_chain(d, kept=True)
    except Exception as e:  # noqa: BLE001 -- the finding
        bad = type(e).__name__
    _SM._write_json(d, SPEC_RETHINK_REQUEST, req)
    ok(other == [] and damaged == [d + ".amend1"] and bad == [],
       f"T_R5_KEPT_PARENT_SIGNOFF a kept child is stored only when the parent's own sign-off binds the request's spec; "
       f"a damaged parent state keeps it; a malformed record stores nothing ({other}, {damaged}, {bad})")
    # R2-8: an unfinished hand-off stores its child only once the child has its state and the lead's signature
    d = interrupted("r5_unsigned")
    os.remove(os.path.join(d + ".amend1", SO.SPEC_SIGNOFF))
    unsigned = handoff_chain(d, kept=True)
    d = interrupted("r6_bad_child_state")            # Codex r3 of #349: a child state that cannot be resumed
    open(os.path.join(d + ".amend1", STATE_FILE), "wb").write(b"")
    ok(unsigned == [] and handoff_chain(d, kept=True) == [],
       f"T_R5_UNFINISHED_UNSIGNED an unfinished hand-off's unsigned child, or one whose state cannot be read, is not "
       f"stored ({unsigned}, {handoff_chain(d, kept=True)})")
    # Codex r3 of #349: a failure anywhere before the parent's FINAL state is saved (its own metadata write included)
    # puts the machine back, and parent metadata the hand-off created is removed again
    for name, where, drop_meta in (("r6_parent_meta", "parent", False), ("r6_no_meta", "child", True)):
        d = interrupted(name)
        if drop_meta:
            os.remove(os.path.join(d, "metadata.json"))
        real_wj, failed = SM_GLOBALS["_write_json"], []

        def fail_once(rd, rel, doc):
            if os.path.realpath(rd) == (d if where == "parent" else d + ".amend1") and not failed \
                    and (where == "child" or rel == "metadata.json"):
                failed.append(rel)
                raise PermissionError("injected")
            return real_wj(rd, rel, doc)
        m6 = machine(d)
        SM_GLOBALS["_write_json"] = fail_once
        try:
            m6.run()
        except OSError:
            pass
        finally:
            SM_GLOBALS["_write_json"] = real_wj
        meta6 = os.path.join(d, "metadata.json")
        meta_ok = not os.path.exists(meta6) if drop_meta else json.load(open(meta6)).get("status") != AMENDED_PROVISIONAL
        st6 = _SM.load_state(d)
        s = m6.run()
        ok(failed and meta_ok and st6.state == "SPEC" and s == AMENDED_PROVISIONAL and handoff_child(d) == d + ".amend1",
           f"T_R6_ROLLBACK {name}: a failed write before the parent's FINAL is saved leaves it at SPEC with its metadata as "
           f"it was; the same machine's retry finishes the hand-off ({failed}, {meta_ok}, {st6.state}, {s})")
    # R2-9 (P2): a moved manual amendment without its metadata names its parent from the amendment record's digest
    src = os.path.join(tdr, "r4_manual")
    p = pair(src, "r5_moved_manual")
    os.remove(os.path.join(p + ".amend1", "metadata.json"))
    _sh.move(src, src + ".gone")
    try:
        al = amendment_lines(p + ".amend1")
    finally:
        _sh.move(src + ".gone", src)
    ok(al and f"`{p}`" in al[0],
       f"T_R5_MOVED_NO_METADATA a moved manual amendment without metadata names its moved parent ({al[:1]})")
