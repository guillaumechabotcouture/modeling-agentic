---
phase: "F6"
commit: "fix"
status_group: "F6 Stage 2"
---

## Changelog

**Benchmark evidence binds only the calibrator thresholds.** Oracle and coverage rows were bound to the whole `toolkit/analysis/thresholds.yaml`, so any new decision-test or burden threshold (#162, #165) invalidated every stored compartmental and renewal result. The declaration and code digests now cover only the sections the calibrators read (`calibration`, `estimates`; `benchmark_sources.CALIBRATOR_THRESHOLD_SECTIONS`); a file that does not parse to a mapping is hashed whole (fail closed). Test BS77. The compartmental and renewal packs were rerun locally (66 sims in 122 s, 10 sims in 3 s): every value and verdict is identical; only digests and wall time changed.

## Status

- **fix:** threshold digest scoped to the calibrator sections; packs restamped.
