"""Self-tests of agents/stage_machine.py (lead_amend_r4), run by `python3 agents/stage_machine.py --self-test`.

Moved verbatim from agents/stage_machine.py by scripts/refactor/split_stage_machine.py.
The module under test is imported as _stage_machine_under_test.
"""
from __future__ import annotations

import hashlib
import json
import os
import analysis_backend as AB
import _stage_machine_under_test as _SM
from _stage_machine_under_test import (
    AMENDED_PROVISIONAL, AMENDMENT_RECORD, SPEC_RETHINK_REQUEST, amendment_lines, handoff_chain, handoff_child,
)
from sm_selftest_common import SM_GLOBALS
from sm_selftest_lead_amend_r5 import _self_test_lead_amend_r5


def _self_test_lead_amend_r4(ok, tdr: str, parent, good: dict, interrupted, pair, machine) -> None:
    """Codex round 1 of #349 on the lead amendment's recovery, lineage and storage."""
    import shutil as _sh
    from toolkit.spec import amend as AM
    # R1-1: an interrupted hand-off (the parent still at SPEC) stores its verified signed child
    d = interrupted("r4_store")
    ok(_SM.load_state(d).status is None and handoff_chain(d, kept=True) == [d + ".amend1"],
       f"T_R4_STORE_INTERRUPTED an interrupted hand-off's signed child is stored with the parent "
       f"({handoff_chain(d, kept=True)})")
    # R1-2: recovery never adopts a same-named child made from another spec or another amendment
    for name, field, value in (("r4_foreign_spec", "parent_spec_sha256", "f" * 64), ("r4_foreign_id", "amendment", "B1")):
        d = interrupted(name)
        mc = os.path.join(d + ".amend1", "metadata.json")
        json.dump(dict(json.load(open(mc)), **{field: value}), open(mc, "w"))
        before = open(mc, "rb").read()
        s = machine(d).run()
        la = json.load(open(os.path.join(d, SPEC_RETHINK_REQUEST))).get("lead_amendment") or {}
        ok(s == "paused:awaiting_spec_amendment" and la.get("status") == "refused" and open(mc, "rb").read() == before
           and handoff_chain(d, kept=True) == [],
           f"T_R4_FOREIGN_CHILD {name}: a child this request did not make is neither adopted, relinked nor stored ({la})")
    # R1-3: a restored parent whose sibling is a link to the original child never writes through it
    d = interrupted("r4_link")
    snap = {f: open(os.path.join(d + ".amend1", f), "rb").read() for f in ("metadata.json", AMENDMENT_RECORD)}
    os.makedirs(os.path.join(tdr, "r4_linked"))
    r = os.path.join(tdr, "r4_linked", "r4_link")
    _sh.copytree(d, r)
    os.symlink(d + ".amend1", r + ".amend1")
    s = machine(r).run()
    ok(s == "paused:awaiting_spec_amendment" and handoff_child(r) is None and handoff_chain(r, kept=True) == []
       and all(open(os.path.join(d + ".amend1", f), "rb").read() == b for f, b in snap.items()),
       f"T_R4_LINKED_CHILD a linked sibling is not a child: refused, the original untouched ({s})")
    # R1-4: a lineage write that fails leaves the parent at SPEC, recoverable; the next resume finishes the hand-off
    # (Codex r2 of #349: the error injected, so it holds for any user, and the retry on the same machine)
    d = interrupted("r4_readonly")
    real_wj, failed = SM_GLOBALS["_write_json"], []

    def fail_once(rd, rel, doc):
        if os.path.realpath(rd) == d + ".amend1" and not failed:
            failed.append(rel)
            raise PermissionError("injected")
        return real_wj(rd, rel, doc)
    m4 = machine(d)
    SM_GLOBALS["_write_json"] = fail_once
    try:
        m4.run()
        raised = None
    except OSError as e:
        raised = type(e).__name__
    finally:
        SM_GLOBALS["_write_json"] = real_wj
    st1, meta1 = _SM.load_state(d), json.load(open(os.path.join(d, "metadata.json")))
    s = m4.run()
    ok(raised and st1.status is None and st1.state == "SPEC" and meta1.get("status") != AMENDED_PROVISIONAL
       and m4.st.state == "FINAL" and s == AMENDED_PROVISIONAL and handoff_child(d) == d + ".amend1",
       f"T_R4_RELINK_FAILS a failed lineage write is raised before the parent is FINAL (state and metadata); the same "
       f"machine's retry finishes it ({raised}, {st1.state}, {meta1.get('status')}, {s})")
    # R1-6: the kept child is verified by the signed digest the request names, not the working spec's bytes
    d = interrupted("r4_kept")
    sp, keep = os.path.join(d, AB.SPEC_FILE), None
    _SM._write_json(d, "decision_brief.signoff.json", {"signed_by": "owner", "sha256": {AB.SPEC_FILE: "0" * 64}})
    machine(d).run()                                  # refused (the legacy brief), the child kept
    keep = open(sp, "rb").read()
    os.remove(sp)
    gone = handoff_chain(d, kept=True)
    open(sp, "wb").write(b"# another spec\n")
    mc = os.path.join(d + ".amend1", "metadata.json")
    meta0 = open(mc, "rb").read()
    json.dump(dict(json.loads(meta0), parent_spec_sha256=hashlib.sha256(b"# another spec\n").hexdigest()), open(mc, "w"))
    other = handoff_chain(d, kept=True)
    open(sp, "wb").write(keep)
    open(mc, "wb").write(meta0)
    ok(gone == [d + ".amend1"] and other == [],
       f"T_R4_KEPT_SIGNED_DIGEST a kept child is stored by the request's signed digest: also without the working spec, "
       f"never by replacement bytes ({gone}, {other})")
    # R1-7 (P2): a manual amendment moved with its parent names the moved parent (verified by the parent-state digest)
    d = parent("r4_manual", "lead", good)[0]
    st = _SM.load_state(d)
    st.state, st.status = "FINAL", "completed"
    _SM.save_state(d, st)
    import yaml
    pf = os.path.join(tdr, "r4_manual_patch.yaml")
    yaml.safe_dump(good, open(pf, "w"))
    AM.amend(d, pf, signoff="lead", log=lambda *a: None)
    p = pair(d, "r4_moved_manual")
    _sh.move(d, d + ".gone")
    try:
        al = amendment_lines(p + ".amend1")
    finally:
        _sh.move(d + ".gone", d)
    ok(al and f"`{p}`" in al[0] and f"`{d}`" not in al[0],
       f"T_R4_MOVED_MANUAL a moved manual amendment names its moved parent ({al[:1]})")
    _self_test_lead_amend_r5(ok, tdr, interrupted, pair, machine)
