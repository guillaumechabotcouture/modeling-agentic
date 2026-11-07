---
phase: "v2"
commit: "JC3 α"
status_group: "In review (v2 calibration)"
---

## Changelog

**The cited-input scan no longer reads a joint calibration group target's `key.csv` as a file (test first: T50c, T50d).** In `calibration.joint.targets[].key: {csv: <column>, units: <column>}`, `csv` names a column. `scripts/pipeline.py` `cited_paths` treated it as a cited file. Both pre-sign checks failed on it: `record_cited_inputs` ("the spec cites unit, which does not exist") and `derivation_problems` (`data_ref_not_derived`). The F5 amendment 7 pre-sign job hit this in its derive step. Only that mapping is skipped. The target's own `csv` and every other cited path are still checked (T50d).

## Status

- **JC3:** the cited-input scan reads a joint target's key as columns.
