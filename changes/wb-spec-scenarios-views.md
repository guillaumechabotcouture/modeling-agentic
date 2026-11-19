---
phase: "v2"
commit: "WB ι"
---

## Changelog

**Review workbench: Spec-and-evidence and Scenarios views.** **Spec and evidence** (`views/spec.py`, `spec_read.py`, `spec_checks.py`), read from the verified signed spec: the decision in one table; every value the spec sets that scales an effect, where it is set, its source and whether the signed spec itself marks the source verified; the dossier, entries with no data first; the sign-off history with what each signature changed and its note; the pre-sign results from the sheet. Seven checks, all for the reader to judge: an outcome adjustment with no recorded follow-up (no run records one as data: gap H1), spec-set values with no verified mark, dossier entries with no data and no decision test, an empty note on the current signature with the count of sections it changed, sheet sections the sign-off code always writes that the sheet lacks, a HIGH row on the sheet, and the decision date by the producer's own rule. **Scenarios** (`views/scenarios.py`): whether the dynamics were looked at (a run that reached the outcome batch with no trajectory record is a problem), the harness's own dynamics checks, the stored schedule as a timeline per group against the delivery period and the outcome window, and the simulated series when the run has them. The page gains a generic timeline chart and panels per group for line charts. `--accept` passes on five new acceptance files (three runs for Spec, two for Scenarios) and the earlier seventeen are unchanged. Stacked on the fixtures.
