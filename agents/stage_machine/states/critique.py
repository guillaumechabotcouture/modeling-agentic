"""The CRITIQUE state handler, registered by name.

Moved verbatim from agents/stage_machine.py by scripts/refactor/split_stage_machine.py.
"""
from __future__ import annotations

from ..common import CRITICS
from . import handler


@handler("CRITIQUE")
class CritiqueHandler:
    def h_CRITIQUE(self):
        self._world_model("refresh")
        if self.st.round_pending:
            self.st.round += 1
            self.st.round_pending = False
        if self.st.round > self.st.max_rounds:
            return self._go("FINAL", "round cap passed (gate did not stop the run)", "completed_gate_error")
        v = self._visit()
        fb = self.st.tries.get("schema_feedback") or {}
        critics = list(CRITICS) if (not fb or "*" in fb) else [r for r in fb if r in CRITICS or r == "critique-presentation"]
        rereview = self._rereview_due()          # hb1 cause G: the domain and red-team critics, on the revised draft
        for reviewer in critics:
            args = (self.run_dir, reviewer, self.st.round) + ((fb.get(reviewer) or fb.get("*"),) if fb else ())
            res = self._task(f"CRITIQUE#{v}:r{self.st.round}:{reviewer}", "critique", *args)
            if res is None:
                return self._budget_out("CRITIQUE")
        self.st.tries.pop("schema_feedback", None)
        if rereview:
            self.st.tries.pop("rereview", None)
            return self._go("GATE", f"critique round {self.st.round} (re-review of the revised draft)")
        return self._go("GATE", f"critique round {self.st.round}")
