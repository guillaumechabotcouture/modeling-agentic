---
phase: "v2"
commit: "JC2 α–γ"
status_group: "In review (v2 calibration)"
---

## Changelog

**Joint calibration: unmatched units may proceed flagged, one primary error setting with reported sensitivities, and the emulator-check rule change stated as one (owner decisions 2026-11-06).**
- **Flag option.** `calibration.joint.unmatched` can now be set to `{mode: block | flag, max_units, default_reason, reasons: [{unit | region, reason}]}`. Absent, it blocks as before. A malformed value is refused.
  - With `mode: flag`, units that no compatible value matches, or that fail the engine verification, are carried into the outcome batch and the allocation. Each carries its recorded reason, taken from its own unit entry, else its region's, else `default_reason`.
  - Every other block stays a block: an emulator check, a target-level verification failure, conflicting units, or more misses than `max_units`.
  - A flagged unit is marked `calibration_flag` in the allocation file and `calibration_flag` / `calibration_status` in the point artifact. It is listed under `low_confidence_units`, with a note, in the uncertainty report, and in `unmatched_decision` in the joint calibration artifact.
  - Flagged units never inform the stability folds' prediction of a held-out unit's value.
- **Sensitivities.** `calibration.joint.sensitivity: [{id, label, targets: {target id: {sd | bias_sd | discrepancy_sd}}}]` lists error settings that are reported beside the primary, never as a block.
  - Each is calibrated on every run of the primary setting, in the same process (no new wave; the engine verification runs).
  - The report names the primary setting's target errors (`primary`).
- **Emulator-check rule (HM20, in PR #150).** This rule change was made after seeing the w6 result: the four benchmark emulators failed their wave-0 check on 32 points and passed a later cross-validation over all 64 runs. Before, any failed check blocked for good. Now a failed check clears only when a later check that covers every run so far, a cross-validation over all of them, passes.
  - The rationale: that check is a stronger test than a single wave's holdout, because it includes the points the failed emulator was judged on.
  - A later check of the new wave's points alone still cannot clear a failure (HM14).
- **Top-up waves (HM21).** A new wave can simulate only the units with fewer earlier runs than a count, for example units whose inputs changed.
- **Tests, written first:** JA11 (flag option shape and decision), JA12 (sensitivity), JR8 (flagged units through ANALYZE: allocation file, point, uncertainty report), HM21.

## Status

- **JC2:** unmatched units proceed flagged under an owner option; primary and sensitivity error settings; the emulator-check rule change stated.
