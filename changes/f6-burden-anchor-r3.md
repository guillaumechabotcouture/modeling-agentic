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

Review round 1 of #220 adds five fixes. The boundary check now anchors the engine side per checked unit to that unit's anchored point burden, so an engine whose absolute level differs from the emulator's cannot hide a reversed ranking. The grouped YLD check uses each engine age group's own years of life lost per death (`daly_parts.yll_per_death_by_age`); with only an all-age value it is skipped, and the artifact records why. The emulator path predicts no grouped outcomes, which is recorded too, and the boundary hybrid keeps the engine's `y_group`. Directly anchored DALYs in the model window need deaths there for the same population groups. Anchor groups with several rows get a hashed name (`group_<sha256[:10]>`), and row ids may not start with `group_`. Review round 2 of #220 adds three changes. When `yll_per_death_by_age` is declared, every YLL computation uses it, weighted by the deaths of each age group where those are known: the aggregate YLD check, the grouped checks (`y_group` and `burden_group`, including singleton age bands) and DALY composition. Otherwise the aggregate check uses the smallest age-specific value. With only an all-age value, the aggregate check is skipped with a recorded reason, and composed grouped DALYs follow the total's ratio. The boundary check runs the anchor arm on the engine. Packages not run on the engine keep the emulator's anchoring and are left out of the checks. Self-tests BA20-BA32 (burden.py, run.py) and SP9b/SP11c (schema.py) were written first and failed before the change. No cloud run.
