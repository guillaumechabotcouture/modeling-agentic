"""The EVIDENCE state handler, registered by name.

Moved verbatim from agents/stage_machine.py by scripts/refactor/split_stage_machine.py.
"""
from __future__ import annotations

from ..common import _put
from . import handler


@handler("EVIDENCE")
class EvidenceHandler:
    def h_EVIDENCE(self):
        """v2 autonomy 1B-ii (D1): the evidence dossier, before SIGN. No decision inputs needed: a pass with no task.
        Otherwise the harness scaffolds each missing entry with what execution uses (toolkit/spec/evidence_draft.py)
        and the bounded draft_evidence task fills the evidence, at most EDR.MAX_TRIES times in the run, each time with
        the signer's check's problems; then the record (evidence/dossier_draft.json) and the sign-off sheet, which
        binds the dossier's sha256. A spec signed under the earlier order (SIGN before EVIDENCE) is left as signed."""
        from toolkit.spec import evidence_draft as EDR
        rd = self.run_dir
        if self._signed_before():       # Codex r1 P1-2/3: SIGN checks the signature, its signer and the approvals
            return self._go("SIGN", "evidence: the spec was signed before this stage (earlier order); the dossier "
                                    "stays as signed")
        # the evidence service's registry (workstream V5), still a stub: the run console reads it
        _put(rd, "evidence/registry.yaml", "# evidence service stub (v2 C): no parameter has been fetched or "
             "matched yet\nstatus: stub\nentries: []\n")

        def spec_back(probs):
            self.st.tries["spec_feedback"] = probs
            return self._go("SPEC", "evidence: " + "; ".join(probs)[:200])
        try:
            raw, a = self._evidence_assess(rd)
            if a["spec_problems"]:
                return spec_back(a["spec_problems"])
            sc = EDR.scaffold(rd, a, raw)["scaffold"] if a["needed"] else {}
        except Exception as e:  # noqa: BLE001 -- fail closed: back to the draft, whose tries are bounded
            return spec_back([f"the evidence stage could not read the spec or the dossier: {type(e).__name__}: {e}"])
        notes = []
        fb = list(self.st.tries.get("evidence_feedback") or [])

        def drafts():       # Codex r1 P2-7, r2 P2-2: the drafts the journal charged (completed or interrupted)
            return sum(1 for k, j in self.st.journal.items() if k.startswith("EVIDENCE#")
                       and j.get("status") in ("completed", "interrupted"))
        if a["needed"]:
            a = EDR.assess(rd, raw)
            if a["problems"] and not a.get("draftable"):
                notes.append(f"the spec names {a['rel']}: only {EDR.ED.DOSSIER_FILE} is drafted in the run")
            while a["problems"] and a.get("draftable") and drafts() < EDR.MAX_TRIES:
                n = drafts()
                res = self._task(f"EVIDENCE#{self._visit()}:try{n + 1}", "draft_evidence", rd, a["rel"],
                                 EDR.packet(a), fb + a["problems"])
                self.st.tries["evidence"] = drafts()
                if res is None:
                    return self._budget_out("EVIDENCE")
                notes.append(f"try {n + 1}: " + (res.note if res.ok else f"failed: {res.note}")[:200])
                fb = []
                try:
                    EDR.scaffold(rd, a, raw)            # an entry the task dropped is scaffolded again
                    a = EDR.assess(rd, raw)
                except Exception as e:  # noqa: BLE001 -- the task left a dossier the harness cannot read
                    a = dict(a, problems=[f"the dossier cannot be read: {type(e).__name__}: {e}"])
        self.st.tries.pop("evidence_feedback", None)
        rec = EDR.finish(rd, a, sc, drafts(), raw, notes)
        if a["needed"]:
            try:
                if getattr(self.backend, "candidates", None) is None and hasattr(self.backend, "pre_sign_problems"):
                    # Codex r1 P2-12: a resumed process has no candidate rows from SPEC; the sheet would lose them
                    # (r2 P2-7: with h_SPEC's own arguments)
                    (self.backend.pre_sign_problems(rd, derived=True) if self.st.derived_inputs
                     else self.backend.pre_sign_problems(rd))
                self._write_sheet(rd)        # the sheet records the dossier's sha256: written after the dossier
            except Exception as e:  # noqa: BLE001 -- SIGN refuses without a current sheet, and says so
                self.log(f"[machine] the sign-off sheet could not be written: {type(e).__name__}: {e}")
        if rec["status"] == "incomplete":
            self.log(f"[machine] the evidence dossier still has {len(a['problems'])} problem(s) after "
                     f"{rec['tries']} draft(s): see {EDR.RECORD}")
        return self._go("SIGN", f"evidence: {rec['status']} ({len(rec['required'])} input(s))")
