"""Self-tests of agents/stage_machine.py (calibration_refused), run by `python3 agents/stage_machine.py --self-test`.

Moved verbatim from agents/stage_machine.py by scripts/refactor/split_stage_machine.py.
The module under test is imported as _stage_machine_under_test.
"""
from __future__ import annotations

import os
import sys
import findings_ledger as FL
import _stage_machine_under_test as _SM
from _stage_machine_under_test import (
    CALIBRATION_REFUSED, MAX_CALIBRATION_REFUSALS, calibration_refusal, rethink_request, run_v2,
)
from sm_selftest_common import SM_GLOBALS, _TOY_REFUSAL


def _self_test_calibration_refused(ok, td: str, toy_run, Batch, quiet) -> None:
    """CRF1-4 (eval baseline: a refused calibration ended the run at ANALYZE): it goes to the spec revision with the
    refusal and the parameters at a bound as a finding, at most MAX_CALIBRATION_REFUSALS times, then ends run_failed
    as before; conflicts.policy: stop keeps the old stop; another failure is not one."""
    drv = os.path.join(td, "toy_refusal.py")
    open(drv, "w").write(_TOY_REFUSAL)

    class Refuse(Batch):
        """The toy batch step replaced by one that refuses the calibration as the analysis does."""
        def pipeline_steps(self, spec_path, run_dir):
            steps = super().pipeline_steps(spec_path, run_dir)
            steps[0] = dict(steps[0], run=f"{sys.executable} {drv} {run_dir}")
            return steps
    refuse = Refuse(60)
    d, T = toy_run("calibration_refused")
    s1 = run_v2("toy question", d, T, signoff="none", log=quiet, backend=refuse, cross_engine_check=False)
    st = _SM.load_state(d)
    back = [h for h in st.history if h.get("from") == "ANALYZE"]
    led = FL.load(d)
    f = led.get(CALIBRATION_REFUSED)
    ok(s1 == "run_failed" and [h.get("to") for h in back] == ["SPEC"] * MAX_CALIBRATION_REFUSALS + ["FINAL"]
       and all("calibration refused (calibration_ridge" in h.get("why", "") for h in back[:-1])
       and "cap 2" in back[-1].get("why", "") and st.tries.get(CALIBRATION_REFUSED) == MAX_CALIBRATION_REFUSALS,
       f"CRF1 a refused calibration goes back to SPEC {MAX_CALIBRATION_REFUSALS} times, then ends run_failed "
       f"({s1}, {[(h.get('to'), h.get('why', '')[:60]) for h in back]})")
    ok(f is not None and f.owner_state == "SPEC" and f.severity == "HIGH" and not f.scope_declarable
       and "z:u0, z:u1, z:u2" in f.claim and "calibration.scale" in (f.fix_requires or "")
       and "calibration_ridge): calibration_ridge" not in f.claim,
       f"CRF2 the finding names the refusal, the parameters at a bound and the remedy ({f and f.claim[:160]})")
    req = rethink_request(d, st.round + 99)
    ok(any(x.get("id") == CALIBRATION_REFUSED for x in req.get("findings") or []),
       "CRF3 a signed spec's amendment request carries the refusal (the spec owns it)")
    # CRF4: conflicts.policy stop keeps today's behaviour: the first refusal ends the run
    d2, T2 = toy_run("calibration_refused_stop")
    g = SM_GLOBALS
    orig = g["conflicts_policy_stop"]
    g["conflicts_policy_stop"] = lambda run_dir: True
    try:
        s2 = run_v2("toy question", d2, T2, signoff="none", log=quiet, backend=refuse, cross_engine_check=False)
    finally:
        g["conflicts_policy_stop"] = orig
    back2 = [h.get("to") for h in _SM.load_state(d2).history if h.get("from") == "ANALYZE"]
    rep_other = {"ok": False, "errors": [], "steps": [{"id": "analyze", "status": "failed", "returncode": 1,
                                                       "error": "ValueError: boom"}]}
    ok(s2 == "run_failed" and back2 == ["FINAL"] and calibration_refusal(rep_other, d2) is None,
       f"CRF4 under conflicts.policy stop the first refusal ends the run; another failure is no refusal ({back2})")
