---
phase: "F6"
commit: "A5"
status_group: "F6 Stage 2"
---

## Changelog

**A finding the critique leaves out stays open (F6 A5; project review finding A5).** The gate built its open and closed findings only from the current critiques, so a HIGH or MEDIUM finding with a frozen `done_when` test that a critic simply stopped listing was neither open, resolved, regressed nor unmet: a round-2 critique with `blockers: []` could reach ACCEPT while round 1's findings were unfixed. `done_when.run` now reports `dropped`: the registry tests whose finding no current critique lists at any severity, that are not withdrawn, were not tied to no brief item when last listed (`outside_brief_round` in the registry), and whose test has never passed. `gate_findings` turns each into a HIGH `done_when_dropped`, as a regression is, naming the finding's own severity, with its own key (one v2 ledger row per finding) and its frozen test; the kind is critic-derived, so it is never declared away early. The test's status counts it as open, so a dropped finding whose test is not judged also blocks ACCEPT. A left-out finding whose test passes is not reopened, and neither is one with a judgement test (which no harness check can pass) that the critic resolved in an earlier round: the registry records the round of the critic's latest resolution (`critic_resolved_round`). A resolved finding whose runnable test fails still does not leave by being left out. Both pipelines get it: v1's gate and v2's stage machine take `done_when.gate_findings` as they are. The critique-blockers-schema skill says that leaving a blocker out does not close it. A registry written before this change has no `critic_resolved_round`, so a judgement finding in it that the critic no longer lists is not reopened (its resolution cannot be known). Self-tests DW27–DW34 (`done_when.py`) and DG4b (`validate_critique_yaml.py`: such a critique does not reach ACCEPT). Codex round 1 found the brief, LOW-severity, persistence, legacy-registry and ledger-key cases.

## Status

- **A5:** a HIGH or MEDIUM finding a critic stops listing stays open until its test passes, or the critic lists it again as resolved or withdraws the test with a reason.
