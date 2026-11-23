"""The PRESENTATION state handler, registered by name.

Moved verbatim from agents/stage_machine.py by scripts/refactor/split_stage_machine.py.
"""
from __future__ import annotations

from . import handler


@handler("PRESENTATION")
class PresentationHandler:
    def h_PRESENTATION(self):
        # The review of the draft is part of the write-up: like WRITE, it may use the write-up reserve (an accepted
        # run whose WRITE spent into the reserve ended budget_exhausted here, with most of the reserve unspent).
        res = self._task(f"PRESENTATION#{self._visit()}:r{self.st.round}", "critique", self.run_dir,
                         "critique-presentation", self.st.round, writeup=True)
        if res is None:
            return self._budget_out("PRESENTATION")
        if self._rereview_due():
            return self._go("CRITIQUE", f"re-review of the revised draft, round {self.st.round}")
        return self._go("GATE", "presentation review of the draft")
