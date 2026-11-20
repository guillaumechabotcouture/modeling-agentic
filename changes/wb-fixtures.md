---
phase: "v2"
commit: "WB β"
---

## Changelog

**Review workbench: fixture builder and domain word list.** `tools/workbench/fixtures.py` `build(dir, upto, defect, method)` writes a small synthetic run shaped like a stored run's files for the `history_matching` and `posterior` calibration methods, clean or with exactly one planted defect from `DEFECTS` (each entry names its scorecard case, the check it must move, the expected status and the one edit). `upto` leaves out the files of later stages so a defect can be shown at its earliest stage. Names are neutral (units U01…, groups G1…, parameters p1…). `tools/workbench/domain_words.txt` is the list a self-test uses to keep disease, country, intervention and metric words out of the workbench. Self-test: 78 cases (clean builds for both methods, each defect differs from the clean run only in the files its edit names, recorded input hashes match). Stacked on the specification.
