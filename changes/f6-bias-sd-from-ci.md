---
phase: "F6"
commit: "S2-3c"
status_group: "F6 Stage 2"
---

## Changelog

**A shared survey offset's sd can come from each group's own interval (owner, 2026-11-12).** The P6 preview matched all 309 units, but its joint calibration blocked on `offset_sampling_low_acceptance`: 833 of 1000 region-sample rows never met the 2018 state targets. Cause: one shared offset per (survey, state) had one sd for every state (`bias_sd` 0.15 on the logit scale). The 2021 MIS's own 95% interval for Yobe (microscopy 20.5%, 3.9-37.1%, 371 children tested) is about 0.68 on that scale, and the fit needed a Yobe offset of +0.40, 2.7 sd under the uniform value: 52 of 4096 candidate offsets were feasible. A unit target with `bias_by` may now declare `bias_sd_by_group: {csv, filter, key, ci: [lo, hi], ci_level, multiplier}` instead of `bias_sd`: each group's offset sd is its own interval's sd on the target's scale (`HM.sd_from_ci`, as the group targets' sampling sd already is). The same rule for every group, no group singled out. The history-matching core already took one sd per offset block, so only the adapter (`toolkit/analysis/joint_calibration.py`) changes. Fail closed: a calibrated group with no row or two rows, or an interval that is not increasing on the scale, is an input error; `bias_sd` and `bias_sd_by_group` together, or `bias_sd_by_group` without `bias_by` or on a group target, are spec problems. The survey-consistency check compares the declaration as a whole. Self-tests JA25a-c, written first: they failed before the change (the spec was refused). No cloud run.
