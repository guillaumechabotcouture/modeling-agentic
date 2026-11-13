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

Self-tests BA20-BA24 (burden.py, run.py) and SP9b/SP11c (schema.py) were written first and failed before the change. No cloud run.
