---
phase: "v2"
commit: "N2"
---

## Changelog

**A parameter_value decision test outside a parameter's declared range runs inside the emulator design.** Before, on the emulator path, a `parameter_value` test at a value outside the parameter's design box made the emulator extrapolate, and the plan guard then blocked the whole analysis. `toolkit/analysis/batch.py` now widens a design parameter's box to span the values the spec's `parameter_value` tests set for it (`design_ranges` in design.json; a spec without such tests gets the same design as before), `toolkit/analysis/model.py` takes that box as the emulator's domain, and `toolkit/analysis/decision_tests.py` refuses (not_run, with the box) a value still outside it instead of extrapolating. The uncertainty draws keep the declared prior ranges. Self-tests RP10-RP12 in `toolkit/analysis/run.py`. Used by the harness-only re-run of the Nigeria 13-state allocation, for a sensitivity test that weakens the ITN effect.
