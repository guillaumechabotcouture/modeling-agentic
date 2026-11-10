---
phase: "F6"
commit: "S2-5b engine"
status_group: "F6 Stage 2"
---

## Changelog

**Severe cases and malaria deaths as window metrics (F6 S2-5b; malaria engine v0.14).** `validation.burden` can compare a past window through engine window metrics per person of each population group (#173), but the malaria engine reported only clinical cases per named window (`inc_all`, `inc_u5`), and F6's burden reference is under-5 malaria deaths. For every named window the engine now reports `severe_u5`, `severe_5_14`, `severe_15p`, `severe_all`, `deaths_u5`, `deaths_5_14`, `deaths_15p` and `deaths_all`: the window's total expected severe episodes or deaths of new infections in the age group (Griffin 2010/2014 severe-disease probability; case fatality from `cfr_severe_treated`, `cfr_severe_untreated` and `severe_access_add`, or the v0.12 severe-care link) over the group's mean population in the window, counted as the decision window's `per_capita` outcomes are. The model records each day's values apart from the hashed daily records (`Malaria.wdaily`); the decision window's accumulators add the same stored values. Members that share a burn-in recount the shared days with their own case fatality (Codex round 1 P1), and a window the simulated days do not cover gives NaN (P2). Every v0.13 output is byte-identical: `benchmarks/restamp_proof.py`, which now leaves only the eight added window metrics out of the new side (`ADDED_WINDOW_METRICS`, RP3b), found the outputs identical (see the PR for the run), and the four evidence files were re-stamped (`validate_once.sh` confirmation pending). Tests, written first: engine W0-W4 (`selftest_late.py`): a past window equals the same years counted as the decision window, also with the severe-care link; shared burn-ins in either batch order; uncovered windows. Open item recorded in ENGINE_NOTES.md (not changed, to keep v0.13 outputs): `per_capita` deaths over decision-window days before a later fork use the first member's case fatality.

## Status

- **S2-5b (engine):** the malaria engine reports severe cases and deaths per named window and age group, so the burden check can compare under-5 deaths over a past window.
