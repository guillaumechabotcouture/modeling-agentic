"""The PREREGISTER state handler, registered by name.

Moved verbatim from agents/stage_machine.py by scripts/refactor/split_stage_machine.py.
"""
from __future__ import annotations

from . import handler


@handler("PREREGISTER")
class PreregisterHandler:
    def h_PREREGISTER(self):
        return self._go("CONFIGURE", self._world_model("preregister"))
