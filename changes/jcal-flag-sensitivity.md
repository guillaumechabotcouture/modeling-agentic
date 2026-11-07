---
phase: "v2"
commit: "JC2 α–γ"
status_group: "In review (v2 calibration)"
---

## Changelog

**Joint calibration: unmatched units may proceed flagged, one primary error setting with reported sensitivities, and the emulator-check rule change stated as one (owner decisions 2026-11-06).**
- **Flag option.** `calibration.joint.unmatched` can now be set to `{mode: block | flag, max_units, default_reason, reasons: [{unit | region, reason}]}`. Absent, it blocks as before. A malformed value is refused.
  - With `mode: flag`, units that no compatible value matches, or that fail the engine verification, are carried into the outcome batch and the allocation. Each carries its recorded reason, taken from its own unit entry, else its region's, else `default_reason`.
  - Every other block stays a block: an emulator check, a target-level verification failure, conflicting units, or more misses than `max_units`. So does a blocked report whose block details are missing or do not name exactly the units that missed (JA11g).
  - A flagged unit is marked `calibration_flag` in the allocation file and `calibration_flag` / `calibration_status` in the point artifact. It is listed under `low_confidence_units`, with a note, in the uncertainty report, and in `unmatched_decision` in the joint calibration artifact.
  - Flagged units inform neither the stability folds' prediction of a held-out unit's value nor the covariate scaling behind it (JR9a). A fold with too few unflagged training units is recorded `not_run`, and the metrics are computed over the folds that ran. When no fold runs, both verdicts are `NOT_RUN` (JR9b).
- **Sensitivities.** `calibration.joint.sensitivity: [{id, label, targets: {target id: {sd | bias_sd | discrepancy_sd}}}]` lists error settings that are reported beside the primary, never as a block.
  - Each is calibrated on every run of the primary setting, in the same process (no new wave; the engine verification runs).
  - The report names the primary setting's target errors (`primary`).
- **Emulator-check rule (HM20, first in PR #150; tightened here).** This rule change was made after seeing the w6 result: the four benchmark emulators failed their wave-0 check on 32 points and passed a later cross-validation over all 64 runs. Before, any failed check blocked for good.
  - In #150, a failed check cleared when a later check covering every run so far passed. Review found that a pooled pass can dilute the same failures (two bad runs of 32 fail; the same two of 64 pass).
  - Now a failed check clears only when a later check covering every run passes overall **and** on the failed check's own runs, judged out of fold. Every one of those runs must be present in the later check (HM20, HM22, HM24). A check of the new wave's points alone still cannot clear a failure (HM14).
  - A failed check that has not cleared is saved with the runs (`failed_checks` on each run, `unresolved_checks` with each check's size). A continuation from those runs, a sensitivity or a second setting starts with it still failed. If any of its runs are not reused, it cannot clear (HM25). Carried failures are renamed with their observables when window names change; one whose window is gone never clears (JA14).
  - Recomputed with w6's own folds, all five benchmark emulators pass both parts (share outside 3 sd: 0.000 to 0.031 on the wave-0 runs), so w6's setting A still clears. With other fold draws, two (B10, B12b) fail: these emulators are marginal.
- **Best shared point (HM23).** Units that no value matches no longer steer the choice of the best shared point.
- **Declarations the code would not apply are refused (JA13).** A reason entry must name exactly one unit or region. A sensitivity cannot override `sd` on a target whose error comes from its data (`sd_column`, `ci_columns`, `n_column`, `ci`). On a benchmark it can change only `discrepancy_sd`. A sensitivity id is a plain name (JA13c).
- **A failing sensitivity is reported with its error and never stops the primary (JA12d).** This covers every step: writing its spec, building its problem and calibrating. A top-up's minimum run count applies to the unit's total.
- **Top-up waves (HM21).** A new wave can simulate only the units with fewer earlier runs than a count, for example units whose inputs changed.
- **Tests, written first:** JA11 (flag option shape and decision), JA12 (sensitivity), JA13, HM20, HM22, HM23, HM24, HM25, JA14, JR8, JR9 (flagged units through ANALYZE: allocation file, point, uncertainty report), HM21.

## Status

- **JC2:** unmatched units proceed flagged under an owner option; primary and sensitivity error settings; the emulator-check rule change stated.
