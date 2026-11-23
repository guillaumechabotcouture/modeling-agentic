"""The ENGINE_CHOICE state handler, registered by name.

Moved verbatim from agents/stage_machine.py by scripts/refactor/split_stage_machine.py.
"""
from __future__ import annotations

import os
import analysis_backend as AB

from ..common import _now, _write_json
from ..signoff import evidence_gate
from . import handler


@handler("ENGINE_CHOICE")
class EngineChoiceHandler:
    def h_ENGINE_CHOICE(self):
        try:
            spec = self.backend.choice_spec(self.run_dir)
        except Exception as e:  # noqa: BLE001 -- fail closed
            return self._go("FINAL", f"the spec cannot be read for the engine choice: {e}", "run_failed")
        rows = self.backend.eligible_engines(spec)
        eligible = [r for r in rows if r.get("eligible")]
        rec = {"at": _now(), "candidates": rows}
        if not eligible:
            _write_json(self.run_dir, "engine_choice.json", rec)
            return self._go("FINAL", "no registered engine fits the spec", "run_failed")
        rt = self.st.tries.get("rethink_engine")         # set by a RETHINK back-edge (_rethink): the engine left, and why
        res = self._task(f"ENGINE_CHOICE#{self._visit()}:pick_engine", "pick_engine", spec, rows,
                         **({"rethink": rt} if rt else {}))
        if res is None:
            return self._budget_out("ENGINE_CHOICE")
        if rt:
            rec["rethink"] = rt
            self.st.tries.pop("rethink_engine", None)
        pick = (res.output or {}).get("engine") if res.ok else None
        if pick not in {r["ref"] for r in eligible}:
            pick, why = eligible[0]["ref"], f"harness: the pick {pick!r} is not eligible; first eligible engine"
        else:
            why = (res.output or {}).get("why", "")
        cfg = next((r.get("config") for r in eligible if r["ref"] == pick), None) or {}
        miss = evidence_gate(self.run_dir, engine=(pick, cfg), signoff=self.st.signoff)
        if miss:            # F6: the signed dossier must cover the engine actually chosen, before any compute
            _write_json(self.run_dir, AB.ENGINE_CHOICE, dict(rec, evidence_refused={"engine": pick, "why": miss}))
            return self._go("FINAL", f"the signed evidence dossier does not cover {pick}'s decision inputs: "
                                     + "; ".join(miss[:5]), "run_failed")
        rec.update(AB.engine_choice_record(pick, why, rows, config=cfg))
        if self.cross and len(eligible) > 1:
            try:
                rec["cross_check"] = self.backend.cross_check(
                    spec, [pick] + [r["ref"] for r in eligible if r["ref"] != pick][:1],
                    os.path.join(self.run_dir, "analysis", "cross_check"), run_dir=self.run_dir)
            except Exception as e:  # noqa: BLE001 -- recorded; the check is optional
                rec["cross_check"] = {"error": f"{type(e).__name__}: {e}"}
        _write_json(self.run_dir, AB.ENGINE_CHOICE, rec)     # the spec stays as signed
        self.st.engine = pick
        return self._go("PREREGISTER" if self.st.world_model_stages else "CONFIGURE", f"engine {pick}")
