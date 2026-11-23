"""The SCORE state handler, registered by name.

Moved verbatim from agents/stage_machine.py by scripts/refactor/split_stage_machine.py.
"""
from __future__ import annotations

from . import handler


@handler("SCORE")
class ScoreHandler:
    def h_SCORE(self):
        return self._go("CRITIQUE", self._world_model("score_run"))
