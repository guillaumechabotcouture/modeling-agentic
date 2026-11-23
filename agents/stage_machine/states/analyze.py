"""The ANALYZE state handler, registered by name.

Moved verbatim from agents/stage_machine.py by scripts/refactor/split_stage_machine.py.
"""
from __future__ import annotations

import json
import os
import analysis_backend as AB
import findings_ledger as FL

from ..common import _sha256
from ..failures import _rep_error, analysis_failure_cause
from . import handler


MAX_CALIBRATION_REFUSALS = 2  # a refused calibration goes back to SPEC at most this often, then the run ends run_failed
CALIBRATION_REFUSED = "calibration_refused"      # its findings-ledger kind and id


def calibration_refusal(rep: dict, run_dir: str) -> dict | None:
    """{code, reason, at_bound} when the pipeline's failure is a refused calibration
    (toolkit/analysis/calibration.py CalibrationRefused), else None. A failed step's output names the exception; the
    analysis's own record (analysis/blocked.yaml, listed in this run's blocked manifest) adds its reason and the
    parameters at a bound when it is there. Anything else (another error, a stopped or skipped step) is not one."""
    import re
    if not isinstance(rep, dict) or rep.get("errors"):
        return None
    failed = [s for s in rep.get("steps") or [] if isinstance(s, dict) and s.get("status") == "failed"]
    hit = next((s for s in failed if "CalibrationRefused" in str(s.get("error") or "")), None)
    if hit is None:
        return None
    err = str(hit.get("error") or "")
    m = re.search(r"CalibrationRefused:\s*([a-z_]+):\s*(.*)", err)
    out = {"code": m.group(1) if m else "calibration_refused", "reason": " ".join((m.group(2) if m else err).split()),
           "at_bound": []}
    try:
        from toolkit.analysis import artifacts as AR
        man = json.load(open(os.path.join(run_dir, AR.MANIFEST)))
        rel = os.path.join("analysis", "blocked.yaml")
        ent = (man.get("artifacts") or {}).get(rel) or {}
        if man.get("status") == "blocked" and ent.get("sha256") == _sha256(os.path.join(run_dir, rel)):
            data = (AR.read(run_dir, rel) or {}).get("data") or {}
            if data.get("where") == "calibration" and data.get("exception") == "CalibrationRefused":
                out["code"] = str(data.get("code") or out["code"])
                out["reason"] = " ".join(str(data.get("reason") or out["reason"]).split())
                out["at_bound"] = [str(x) for x in data.get("at_bound") or []]
    except (OSError, ValueError, AttributeError, TypeError):
        pass                       # the step's own output still says what was refused
    if out["reason"].startswith(f"{out['code']}:"):       # the exception's text repeats its code
        out["reason"] = out["reason"][len(out["code"]) + 1:].strip()
    return out


def conflicts_policy_stop(run_dir: str) -> bool:
    """The spec's conflicts.policy is stop (toolkit/analysis/conflicts.py; its default when undeclared): a refused
    calibration then ends the run as before. An unreadable spec is read as stop (fail closed: the old behaviour)."""
    import yaml
    from toolkit.analysis import conflicts as CF
    try:
        raw = yaml.safe_load(open(os.path.join(run_dir, AB.SPEC_FILE))) or {}
        return CF.policy(raw)["policy"] == "stop"
    except Exception:  # noqa: BLE001
        return True


def record_calibration_refusal(run_dir: str, ref: dict, round_n) -> None:
    """The refusal as an open HIGH finding the spec owns (findings ledger), so the spec revision reads it: the
    redraft's feedback (Machine._rethink) or the owner's amendment request (rethink_request). It names the parameters
    at a bound and the remedy."""
    ab = list(ref.get("at_bound") or [])
    claim = (f"the calibration was refused ({ref.get('code')}): {ref.get('reason')}"
             + (f". Parameters at a bound: {', '.join(ab[:20])}" + (f" and {len(ab) - 20} more" if len(ab) > 20 else "")
                if ab else ""))
    led = FL.load(run_dir)
    f = led.get(CALIBRATION_REFUSED) or FL.Finding(id=CALIBRATION_REFUSED, kind=CALIBRATION_REFUSED, severity="HIGH",
                                                    source="harness-calibration", first_seen_round=round_n)
    f.severity, f.owner_state, f.target_stage, f.status, f.blocking = "HIGH", "SPEC", "PLAN", "open", True
    f.scope_declarable, f.last_seen_round, f.claim = False, round_n, claim[:2000]
    f.fix_requires = ("revise the spec's calibration: check calibration.scale and calibration.range against the "
                      "attribute's values (a log scale cannot reach negative values), then the parameterisation (a "
                      "reference package) or declared priors with method posterior")
    FL._event(f, round_n, "open", "harness-calibration", "calibration refused at ANALYZE")
    led[CALIBRATION_REFUSED] = f
    FL.save(run_dir, led)


@handler("ANALYZE")
class AnalyzeHandler:
    def h_ANALYZE(self):
        rep = self.rebuild(self.run_dir)
        if not rep.get("ok"):
            cause = analysis_failure_cause(rep, self.run_dir)
            why = f"analysis failed: {_rep_error(rep)}" + ("; resumable with --resume" if cause else "")
            ref = None if cause else calibration_refusal(rep, self.run_dir)
            if ref is not None and not conflicts_policy_stop(self.run_dir):
                # eval baseline: a refused calibration (a ridge, a fit pinned at its bounds) is a finding about the
                # spec's calibration, not the end of the run: it goes to the spec revision with the refusal reason and
                # the parameters at a bound, at most MAX_CALIBRATION_REFUSALS times (an unsigned spec is redrafted
                # with it; a signed one is the owner's to amend: h_SPEC). conflicts.policy: stop keeps the old stop.
                n = int(self.st.tries.get(CALIBRATION_REFUSED, 0) or 0)
                if n < MAX_CALIBRATION_REFUSALS:
                    self.st.tries[CALIBRATION_REFUSED] = n + 1
                    record_calibration_refusal(self.run_dir, ref, self.st.round)
                    return self._rethink("SPEC", f"calibration refused ({ref['code']}; try {n + 1} of "
                                                 f"{MAX_CALIBRATION_REFUSALS}): {ref['reason']}"[:300])
                why += f"; the calibration was refused {n} time(s) before (cap {MAX_CALIBRATION_REFUSALS})"
            return self._go("FINAL", why, "run_failed", extra={"resumable": cause} if cause else None)
        return self._go("SCORE" if self.st.world_model_stages else "CRITIQUE", "analysis built")
