---
phase: "F6"
commit: "S2-5d δ"
status_group: "F6 Stage 2"
---

## Changelog

**Burden anchor, review round 3: state-level anchors.** Six fixes from the third review of #210:
- A stability fold that holds out one region no longer fails on the anchors of other regions: anchors that no evaluated unit belongs to are skipped.
- The emulator boundary check builds its hybrid from the emulator's un-anchored outputs, so window deaths and DALYs are anchored once, not twice.
- The tornado row and the automatic range test for an anchor group (`anchor_range_<ids>`, one per group) move the group's coupled anchors together, each to its 2.5% or 97.5% end. This matches the draws' shared normal, so an analysis whose draws are coherent never aborts in sensitivity.
- The YLD consistency check for directly anchored DALYs also covers the grouped outputs (`y_group`, `burden_group`).
- The schema refuses `burden_anchor` declared both as a source and as excluded.
- The schema reserves the `anchor_range_` prefix, and `run_all` refuses any test id used twice before a test runs.

Review round 1 of #220 adds five fixes. The boundary check now anchors the engine side per checked unit to that unit's anchored point burden, so an engine whose absolute level differs from the emulator's cannot hide a reversed ranking. The grouped YLD check uses each engine age group's own years of life lost per death (`daly_parts.yll_per_death_by_age`); with only an all-age value it is skipped, and the artifact records why. The emulator path predicts no grouped outcomes, which is recorded too, and the boundary hybrid keeps the engine's `y_group`. Directly anchored DALYs in the model window need deaths there for the same population groups. Anchor groups with several rows get a hashed name (`group_<sha256[:10]>`), and row ids may not start with `group_`. Review round 3 of #220 replaces the YLL-per-death constant with the model's own split. The outcome models (direct and emulator) derive YLL per engine age group from the engine's own deaths times `daly_parts.yll_per_death_by_age` (numbers or engine parameters, e.g. the malaria engine's le_u5, le_5_14 and le_15p), before any aggregation over population bands. The emulator emulates YLL beside the other outcomes. Composed DALYs are DALYs + (f_deaths - 1) x YLL + (f_cases - 1) x YLD, with YLD = DALYs - YLL taken from the model for that unit, package and window, so factors of 1 reproduce the model's DALYs exactly. The direct-DALY check compares anchored DALYs with the anchored YLL. The published share of DALYs from deaths is the model's YLL over its DALYs. `daly_parts.yll_per_death` is refused with a pointer to the new key, and composed DALYs cannot be in the model window, which carries no YLL. Where the model's DALYs fall below its YLL (emulator error), YLD counts as zero, and `burden_anchor.yaml` (`yld_floors`, at the point) and the uncertainty record (`anchor_yld_floors`, over the draws) record it, with the unit x package count and the largest shortfall. No engine code changes, so no engine fingerprint changes and no batch job hash changes. Self-tests BA20-BA38 and MD4 (burden.py, run.py) and SP9b/SP11c (schema.py) were written first and failed before the change. No cloud run.
