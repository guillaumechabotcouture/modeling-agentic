---
phase: "F6"
commit: "fix"
status_group: "F6 Stage 1"
---

## Changelog

**Benchmark results restamped after the decision-test thresholds changed.** The evidence snapshot (#158) binds each coverage row to `toolkit/analysis/thresholds.yaml`, and #162 then added the regret and dollars thresholds to that file, so the stored compartmental and renewal results read "rerun required" on `v2` (engine self-tests K12a/b, K13c/d and the estimates acceptance failed). Both packs were rerun locally with their own `run` command (2 workers): renewal 10 sims in 5 s, compartmental 66 sims in 116 s. Every row's computed values, verdicts and seeds are identical to the stored results; only the declaration digests and wall time changed. Follow-up: bind coverage rows to the thresholds they read, not the whole file, so an unrelated threshold change does not force a rerun.

## Status

- **fix:** compartmental and renewal results restamped; no value changed.
