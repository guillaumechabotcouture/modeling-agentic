---
phase: "v2"
commit: "JC3 α"
status_group: "In review (v2 calibration)"
---

## Changelog

**The cited-input scan no longer reads a joint calibration group target's `key.csv` as a file (test first: T50c, T50d).** In `calibration.joint.targets[].key: {csv: <column>, units: <column>}`, `csv` names a column. `scripts/pipeline.py` `cited_paths` treated it as a cited file. Both pre-sign checks failed on it: `record_cited_inputs` ("the spec cites unit, which does not exist") and `derivation_problems` (`data_ref_not_derived`). The F5 amendment 7 pre-sign job hit this in its derive step. The exemption covers only `key.csv` on a group target, and only when it is a string (a column name). The target's own `csv` is still checked (T50d). A non-string `key.csv`, or a `key.csv` on another target kind, is still scanned (T50e).

## Status

- **JC3:** the cited-input scan reads a joint target's key as columns.
