"""The WRITE state handler, registered by name.

Moved verbatim from agents/stage_machine.py by scripts/refactor/split_stage_machine.py.
"""
from __future__ import annotations

import findings_ledger as FL
import fix_loop as FX

from ..report import render_report
from . import handler


@handler("WRITE")
class WriteHandler:
    def h_WRITE(self):
        mode = self.st.write_mode or "not_accepted"
        self._world_model("refresh")
        fb = self.st.tries.get("language_feedback")             # a language repair (F6 S1-7)
        # F6 run 3: the write-up findings the fix loop left to this stage, with their remedies
        wf = [{"id": f.id, "claim": f.claim, "fix_requires": f.fix_requires}
              for f in FL.open_findings(FL.load(self.run_dir)) if FX.is_write_up(f)]
        res = self._task(f"WRITE#{self._visit()}:{mode}", "write", self.run_dir, mode, *([fb] if fb else []),
                         writeup=True, **({"findings": wf} if wf else {}))
        self.st.tries.pop("language_feedback", None)
        if res is None:
            return self._go("FINAL", "no budget left for the write-up", "completed_no_report")
        if not res.ok or not self._has_draft():
            return self._go("FINAL", "the writer produced no draft", "completed_no_report")
        render_report(self.run_dir, self.backend)
        self.st.post_write = True
        if mode == "not_accepted" and not self._rereview_due():
            # (a revised not-accepted draft gets its presentation review at the new round too: Codex r1 of #344, P2-5)
            return self._go("GATE", "not-accepted report written; final gate")
        return self._go("PRESENTATION", f"draft written ({mode})")
