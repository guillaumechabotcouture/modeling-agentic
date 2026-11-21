---
phase: "F6"
commit: "AUTO-1C-b α-β"
---

## Changelog

**Model guards for unattended analysis runs (v2 autonomy brief, PR 1C item 2).** Four places where one value stopped or silently corrupted an analysis. (2a) `EmulatorModel.predict` judges the reduction clip on the emulator mean; a drawn emulator error that pushes a reduction past its bounds is clipped and counted in `error_clips` (published as `emulator_error_clips` in the uncertainty report), as the cost-output floor already was, and is no longer a model correction that can block the plan. (2b) `batch.design()` calls `cover_features()`: when the design points do not span an emulator feature's range over the units, the unit at that minimum or maximum takes one of the last design points that is not held out; a design that already spans every feature is unchanged, so its job hashes and stored rows are too. (2c) `stability.run` clamps a held-out ridge prediction to `calibration.range` and records `predictions_clamped` per fold and in total; a fold whose refit raises `CalibrationRefused` is recorded in `folds_not_run` with the refusal, and the other folds run. (2d) Fail-closed finite checks: `_group` refuses a non-finite per-capita outcome or group share, `DirectModel.predict` refuses a declared diagnostic the engine does not report (was NaN), `core.q` refuses a sample with a non-finite value (was all-NaN quantiles). Self-tests EM8-EM9, MD2b-MD2c (`toolkit/analysis/model.py`), BA-XU1-BA-XU3 (`toolkit/analysis/batch.py`), JR9c-JR9e and RP9b (`toolkit/analysis/run.py`).
