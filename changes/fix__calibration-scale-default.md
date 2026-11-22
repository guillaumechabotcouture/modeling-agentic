---
phase: "v2"
commit: "calibration-scale-default"
---

## Changelog

**One calibration scale default, taken from the engine card.** `toolkit/analysis/batch.py` `calibrated_value` read an undeclared `calibration.scale` as `log` while `toolkit/spec/schema.py` documented `linear`, so a spec calibrating `log_rate` on [-8, 2] with no scale gave the engine `exp(z) > 0` as a log rate: an impossible fit that ran into the bound and was refused as a ridge. `schema.calibration_scale` is now the one resolution (the declared scale; else the engine card's valid range for the attribute: reaching 0 or below means linear, strictly positive means log; else linear), read through `Spec.calibration_scale()` by the batch, the joint calibration (problem and signature) and the figure-data rebuild check, so a positive attribute such as a transmission density keeps its log scale and its job hashes. Before sign-off, `scale_problems` refuses a declared log scale for an attribute whose card range includes values <= 0, with the reason. `ridge_refusal` now names the parameters sitting at a bound and, when most do, says to check the attribute's scale or range first; `CalibrationRefused` carries them (`at_bound`) and `analysis/blocked.yaml` records them. Self-tests CR11 (the toy panel with its scale removed now calibrates), CR12 (the refusal names bound parameters; the scale hint only when most are bound) and SP4s-u (the resolver and the pre-sign refusal) failed before the fix.
