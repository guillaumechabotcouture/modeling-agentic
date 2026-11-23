"""The CONFIGURE state handler, registered by name.

Moved verbatim from agents/stage_machine.py by scripts/refactor/split_stage_machine.py.
"""
from __future__ import annotations

import os
import analysis_backend as AB

from ..failures import _rep_error
from . import handler


@handler("CONFIGURE")
class ConfigureHandler:
    def h_CONFIGURE(self):
        import pipeline as P
        errs = self.backend.validate(os.path.join(self.run_dir, AB.SPEC_FILE))
        if errs:
            return self._go("FINAL", f"spec invalid for the engine: {'; '.join(errs)[:200]}", "run_failed")
        err = self._write_pipeline()
        if err:
            return self._go("FINAL", f"configure failed: {err}"[:300], "run_failed")
        steps, _errs = P.load(self.run_dir)
        if not any(s_.get("id") == "configure" for s_ in steps):    # workstream B: one analyze step
            return self._go("ANALYZE", "pipeline written (no separate configure step)")
        rep = P.run(self.run_dir, only="configure")
        if not rep.get("ok"):
            return self._go("FINAL", f"configure failed: {_rep_error(rep)}", "run_failed")
        return self._go("ANALYZE", "engine batch built")
