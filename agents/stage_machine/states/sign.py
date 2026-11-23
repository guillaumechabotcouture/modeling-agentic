"""The SIGN state handler, registered by name.

Moved verbatim from agents/stage_machine.py by scripts/refactor/split_stage_machine.py.
"""
from __future__ import annotations

import os
import analysis_backend as AB

from ..common import MAX_SIGN_REPAIRS, MAX_SPEC_TRIES, SIGNOFF_FILE, save_state
from ..signoff import _signoff, sign
from . import handler


@handler("SIGN")
class SignHandler:
    def _geometry_owner_pending(self) -> list[str]:
        """HIGH geometry findings the spec proposes as exceptions and the owner has not approved."""
        if not os.path.exists(os.path.join(self.run_dir, AB.SPEC_FILE)):
            return []
        from toolkit.spec import geometry as GEO
        return GEO.owner_pending(GEO.run(self.run_dir))

    def _sign(self, by: str, note: str = "") -> None:
        if getattr(self.backend, "sign", None):
            self.backend.sign(self.run_dir, by, note)
        else:
            sign(self.run_dir, by, note)

    def h_SIGN(self):
        mode = self.st.signoff
        from toolkit.spec import evidence_draft as EDR
        if not self._signed_before() and EDR.read_record(self.run_dir) is None:
            # Codex r1 P2-6: an unsigned run saved at SIGN under the earlier order has had no evidence stage
            return self._go("EVIDENCE", "earlier order: the evidence stage runs before sign-off")
        if mode == "none":
            return self._go("ENGINE_CHOICE", "sign-off not required (--signoff none)")
        by = self._signed_by()
        if not by and not self._review_conditions(mode):
            self.st.paused, self.st.status = "awaiting_signoff", None
            save_state(self.run_dir, self.st)
            return "SIGN"
        # A HIGH geometry finding the spec proposes as an exception is the owner's to approve (review of #131, P1):
        # a lead sign-off approves nothing, so the run waits for the owner here, as for a spec sign-off.
        pend = self._geometry_owner_pending()
        if by and (mode == "lead" or by == "owner") and not pend:
            return self._go("ENGINE_CHOICE", f"spec signed by {by}")
        if pend:
            how = ("python3 toolkit/spec/geometry.py approve" if by else "python3 agents/stage_machine.py sign")
            self.log(f"[machine] {len(pend)} HIGH geometry exception(s) wait for the owner ({', '.join(pend[:4])}): "
                     f"{how} {self.run_dir} --by owner")
        elif mode == "lead" and self._signed_before():
            # Codex r1 P1-1: a signature that no longer holds (the dossier or an input changed since) is never replaced
            # by a lead's: the record stays, and the owner re-signs
            self.st.paused, self.st.status = "awaiting_signoff", None
            save_state(self.run_dir, self.st)
            self.log("[machine] the spec's sign-off no longer holds (an input changed since, or the record is lost); "
                     "the record stays: restore the signed inputs, or amend the spec and relaunch (the owner signs)")
            return "SIGN"
        elif mode == "lead":
            res = self._task(f"SIGN#{self._visit()}:sign_spec", "sign_spec", self.run_dir)
            note = (_signoff(self.run_dir) or {}).get("note", "")
            if res is None:                 # Codex r2 P2-3: no allowance is the budget path, not an owner pause
                return self._budget_out("SIGN")
            if res.ok:
                if os.path.exists(os.path.join(self.run_dir, SIGNOFF_FILE)) and getattr(self.backend, "sign", None):
                    from lib.safe_io import remove_nofollow
                    remove_nofollow(self.run_dir, SIGNOFF_FILE)   # the lead's note; B writes its own record
                try:
                    self._sign("lead", (note + " " + self._evidence_note()).strip())   # the harness records the hash
                    return self._go("ENGINE_CHOICE", "spec signed by the lead")
                except ValueError as e:
                    self.log(f"[machine] the spec could not be signed: {e}")
                    back = self._sign_repair(str(e))
                    if back:
                        return back
        self.st.paused = "awaiting_signoff"
        self.st.status = None
        save_state(self.run_dir, self.st)
        self.log(f"[machine] waiting for the owner: python3 agents/stage_machine.py sign {self.run_dir} --by owner")
        return "SIGN"

    def _sign_repair(self, why: str) -> str | None:
        """v2 autonomy 1B-ii: a refused lead sign-off goes back with the refusal as feedback, to EVIDENCE when the
        evidence dossier is what was refused, else to SPEC (one more draft allowed), at most MAX_SIGN_REPAIRS times;
        then the run waits for the owner. The check is not loosened: the next SIGN runs it again on the files."""
        n = self.st.tries.get("sign_repair", 0)
        if n >= MAX_SIGN_REPAIRS:
            return None
        self.st.tries["sign_repair"] = n + 1
        if "evidence dossier" in why:
            self.st.tries["evidence_feedback"] = [why[:2000]]
            return self._go("EVIDENCE", f"sign-off refused (evidence dossier): repair {n + 1} of {MAX_SIGN_REPAIRS}")
        self.st.tries["spec_feedback"] = [why[:2000]]
        self.st.tries["spec"] = min(self.st.tries.get("spec", 0), MAX_SPEC_TRIES - 1)
        return self._go("SPEC", f"sign-off refused: repair {n + 1} of {MAX_SIGN_REPAIRS}")

    def _evidence_note(self) -> str:
        """The evidence stage's declared gaps, for the lead's sign-off note (counts; the record holds the inputs)."""
        from toolkit.spec import evidence_draft as EDR
        rec = EDR.read_record(self.run_dir) or {}
        if rec.get("status") in (None, "not_required"):
            return ""
        if rec.get("status") == "unreadable" or not EDR.record_current(self.run_dir, rec):
            return (f"Evidence dossier: provisional, not reviewed by the owner; {EDR.RECORD} could not be read or no "
                    "longer describes the dossier.")
        g = rec.get("gaps") if isinstance(rec.get("gaps"), dict) else {}
        parts = ", ".join(f"{len(g[k])} {k}" for k, _ in EDR.GAP_TEXT if g.get(k))
        return (f"Evidence dossier {'drafted by a run task' if rec.get('drafted_by_task') else 'supplied'}: "
                f"{len(rec.get('required') or [])} input(s); provisional, not reviewed by the owner; gaps declared in "
                f"{EDR.RECORD}" + (f" ({parts})." if parts else "."))
