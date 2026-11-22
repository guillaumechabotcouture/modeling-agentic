"""Self-tests of agents/stage_machine.py (evidence), run by `python3 agents/stage_machine.py --self-test`.

Moved verbatim from agents/stage_machine.py by scripts/refactor/split_stage_machine.py.
The module under test is imported as _stage_machine_under_test.
"""
from __future__ import annotations

import os
import analysis_backend as AB
from _stage_machine_under_test import (
    MAX_SIGN_REPAIRS, Machine, RunState, SIGNOFF_FILE, SIGNOFF_SHEET, _signoff, deliverable_problems, init_run,
)
from sm_selftest_common import _t_owner_disposes


def _self_test_evidence(ok, td: str) -> None:
    """v2 autonomy 1B-ii (D1): the EVIDENCE stage before SIGN, on the toy engine whose component declares decision
    inputs (toolkit/spec/toy_engines.py ToyDossierEngine)."""
    import yaml
    from v2_tasks import StubTasks
    from toolkit.spec import evidence_dossier as ED, evidence_draft as EDR, toy_engines as TE
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
    # T_SIGN_REPAIR: the sheet goes stale between EVIDENCE and SIGN; the refusal sends the run back, then it signs
    class Stale(StubTasks):
        def sign_spec(self, run_dir):
            if self.calls.count("sign_spec") == 0:
                os.remove(os.path.join(run_dir, SIGNOFF_SHEET))
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
    ok(hist[:5] == ["EVIDENCE", "SIGN", "EVIDENCE", "SIGN", "ENGINE_CHOICE"] and m.st.tries.get("sign_repair") == 1
       and T.calls.count("draft_evidence") == 1 and (_signoff(d) or {}).get("signed_by") == "lead",
       f"T_SIGN_REPAIR a refused lead sign-off goes back to EVIDENCE once, which rewrites the sheet, then signs ({hist}; "
       f"{T.calls})")
    # T_EVIDENCE_EXHAUSTED: a task that never supplies the evidence: three drafts, two repairs, then the owner
    m, T = setup("ev_none", "lead", {})
    hist = until(m)
    rec = EDR.read_record(m.run_dir) or {}
    ok(m.st.state == "SIGN" and m.st.paused == "awaiting_signoff" and T.calls.count("draft_evidence") == EDR.MAX_TRIES
       and m.st.tries.get("sign_repair") == MAX_SIGN_REPAIRS and rec.get("status") == "incomplete"
       and not os.path.exists(os.path.join(m.run_dir, SIGNOFF_FILE)),
       f"T_EVIDENCE_EXHAUSTED the check is never loosened: after {EDR.MAX_TRIES} drafts and {MAX_SIGN_REPAIRS} repairs "
       f"the run waits for the owner, unsigned ({hist}; {T.calls})")
    # T_EVIDENCE_OWNER: under --signoff owner the drafted dossier is on the sheet the owner signs
    m, T = setup("ev_owner", "owner", {"evidence": fill})
    until(m)
    sheet = open(os.path.join(m.run_dir, SIGNOFF_SHEET)).read() if os.path.exists(
        os.path.join(m.run_dir, SIGNOFF_SHEET)) else ""
    ok(m.st.state == "SIGN" and m.st.paused == "awaiting_signoff" and ED.SECTION in sheet, f"T_EVIDENCE_OWNER the "
       f"owner is shown the drafted dossier on the sheet ({m.st.state}, {m.st.paused})")
    _t_owner_disposes(m.run_dir)        # W0-G (v2 merge): the owner disposes of the review conditions before signing
    toolkit.sign(m.run_dir, "owner")
    m.step()
    ok(m.st.state == "ENGINE_CHOICE", f"T_EVIDENCE_OWNER_SIGNED once the owner signs, the run goes on ({m.st.state})")
    # T_EVIDENCE_NONE: --signoff none: the drafted dossier covers the engine at ENGINE_CHOICE (it failed there before)
    m, T = setup("ev_unsigned", "none", {"evidence": fill})
    hist = until(m, stop=("PREREGISTER", "CONFIGURE"))
    ok(hist[:4] == ["EVIDENCE", "SIGN", "ENGINE_CHOICE", hist[3] if len(hist) > 3 else None]
       and m.st.state in ("PREREGISTER", "CONFIGURE"),
       f"T_EVIDENCE_NONE --signoff none passes ENGINE_CHOICE's coverage check with the drafted dossier ({hist})")
    # T_EVIDENCE_NAME: the options need inputs and the spec names no dossier: back to the draft, nothing written
    d = os.path.join(td, "ev_name")
    os.makedirs(d)
    raw = yaml.safe_load(open(ED.toy_dossier_spec(d, dossier=False)))
    os.remove(os.path.join(d, AB.SPEC_FILE))
    m = Machine(d, StubTasks({"spec": raw}), init_run(d, "toy question", 3, "lead", "v2", None, derived_inputs=False),
                backend=toolkit, log=lambda *a: None, cross_engine_check=False)
    until(m, n=4)
    ok(m.st.state == "SPEC" and any("name the evidence dossier" in x for x in m.st.tries.get("spec_feedback") or [])
       and not os.path.exists(os.path.join(d, ED.DOSSIER_FILE)),
       f"T_EVIDENCE_NAME a spec that needs a dossier and names none is redrafted with the reason "
       f"({m.st.state}; {m.st.tries.get('spec_feedback')})")
    # T_EVIDENCE_RESUME: a run signed under the earlier order (SIGN, then EVIDENCE) resumes past EVIDENCE untouched
    d = os.path.join(td, "ev_resume")
    os.makedirs(d)
    ED.toy_dossier_spec(d)
    from toolkit.spec import signoff as SO
    SO.write_sheet(d)
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
    SO.write_sheet(d)
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
    SO.write_sheet(d)
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
    # T_DELIVERABLE_DOSSIER (Codex r1 P1-5): the deliverable may not be the dossier or the sheet
    raw = yaml.safe_load(open(os.path.join(d, AB.SPEC_FILE)))
    for name in (ED.DOSSIER_FILE, SIGNOFF_SHEET, "src[1].yaml"):     # r2 P1-1: a name that is not its own glob
        raw["evidence"] = {"dossier": "src[1].yaml"}
        raw["deliverable_file"] = {"path": name, "columns": {"unit": "id"}}
        yaml.safe_dump(raw, open(os.path.join(d, AB.SPEC_FILE), "w"))
        ok(any("owns" in p or "evidence dossier" in p for p in deliverable_problems(d)),
           f"T_DELIVERABLE_DOSSIER a deliverable named {name} is refused ({deliverable_problems(d)})")
