---
phase: "v2"
commit: "calibration-refused-route"
---

## Changelog

**A refused calibration goes to the spec revision instead of ending the run.** At ANALYZE a `CalibrationRefused` (a ridge, a fit pinned at its bounds) ended the run `run_failed`. `agents/stage_machine.py` now recognises it (`calibration_refusal`: the failed step's output names the exception; this run's `analysis/blocked.yaml`, listed in its blocked manifest, adds the reason and the parameters at a bound), records an open HIGH `calibration_refused` finding the spec owns (not declarable; claim names the refusal and the bound parameters; remedy: check scale and range, then the parameterisation or priors), and takes the existing SPEC back-edge (`_rethink`): an unsigned spec is redrafted with the finding as feedback, a signed one gets the owner's amendment request (`spec_rethink_request.json`). Tries are counted in `run_state.json` (`MAX_CALIBRATION_REFUSALS` = 2); past the cap, and for a spec whose `conflicts.policy` is `stop` (its default when undeclared), the run ends `run_failed` as before. New edge ANALYZE -> SPEC. Self-tests CRF1-CRF4 (two returns to SPEC then run_failed; the finding's text; the amendment request carries it; policy stop and other failures unchanged) failed before the fix.
