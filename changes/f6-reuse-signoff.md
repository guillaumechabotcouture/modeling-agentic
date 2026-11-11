---
phase: "F6"
commit: "S1-8b"
status_group: "F6 Stage 2"
---

## Changelog

**Engine-batch reuse ignores sign-off records (F6 S1-8b).** The S1-8 reuse fingerprint (`scripts/compute.py code_fingerprint`) hashed every file under the run's `data/`, including `data/geometry_signoff.json`, the geometry sign-off record that `toolkit/spec/geometry.py freeze()` writes at sign-off (who signed, when, the owner's approvals, and a copy of the declaration with the input files' hashes). A lead-signed preview and the owner-signed final run of the same spec differ in that record only, so the final run would have recomputed every stored engine batch. `FP_SIGNOFF_RECORDS` now names that one path, with the reason it cannot change an engine output: the boundary, crosswalk and units files it hashes stay hashed under `data/`, and only the gate reads the record. Like `FP_HARNESS_OUTPUTS`, it is skipped only in a v2 run (`decision_spec.yaml` at the run root). A v1 run's own model code may read any file, so a v1 run still hashes the record. Every other file under `data/` is still hashed (derived CSVs, inputs, `data/derivations_record.json`). The self-tests were written first and failed before the change: RS0-RS5 (`compute.py`). They cover an exact list with no wildcard, lead and owner records giving the same fingerprint (confined mode included), other `data/` files still counting, a v1 run, and a scan built from the same list. The scan fails if any engine-batch job module (`toolkit/engines/**`, `toolkit/analysis/batch.py`, `trajectories.py`) names an excluded record or the module that owns it, and a planted read shows the scan is not vacuous. Codex round 1: `data/input_checks.json` is skipped too (the geometry check copies the signer and the owner's approvals into it, so it differs between the two sign-offs), and a run counts as v2 only when `metadata.json` records `pipeline: v2` as well as having a spec at its root (a v1 run can hold a spec); RS0, RS1 and RS3b, and RU3's fixture records its pipeline.

## Status

- **S1-8b:** engine-batch reuse ignores the geometry sign-off record in a v2 run (`FP_SIGNOFF_RECORDS`), so an owner-signed final run reuses a lead-signed preview's engine batches of the same spec.
