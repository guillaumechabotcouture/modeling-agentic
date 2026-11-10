---
phase: "F6"
commit: "A5"
status_group: "F6 Stage 2"
---

## Changelog

**A finding the critique leaves out stays open (F6 A5; project review finding A5).** The gate built its open and closed findings only from the current critiques, so a HIGH or MEDIUM finding with a frozen `done_when` test that a critic simply stopped listing was neither open, resolved, regressed nor unmet: a round-2 critique with `blockers: []` could reach ACCEPT while round 1's findings were unfixed. `done_when.run` now reports `dropped`, the registry tests that no current critique lists, that are not withdrawn or outside the brief, and whose test has never passed; `gate_findings` turns each into `done_when_dropped` at the finding's own severity (critic-derived, so never declared away early), and the test's status counts it as open, so a dropped HIGH whose test is not judged also blocks ACCEPT. A left-out finding whose test passes is not reopened. Both pipelines get it: v1's gate and v2's stage machine take `done_when.gate_findings` as they are. The critique-blockers-schema skill says that leaving a blocker out does not close it. Self-tests DW27–DW30 (`done_when.py`).

## Status

- **A5:** a HIGH or MEDIUM finding a critic stops listing stays open until its test passes, or the critic lists it again as resolved or withdraws the test with a reason.
