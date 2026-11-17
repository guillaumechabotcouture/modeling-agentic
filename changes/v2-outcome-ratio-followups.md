---
phase: "F6"
commit: "#248 α–β"
---

## Changelog

**`outcome_ratio` follow-ups from the Codex review of #245, and #246 P2-5 (issue #248).** `toolkit/analysis/decision_tests.py` `_outcome_ratio`: every re-solved plan is guarded with the point evaluation's model corrections (`Analysis.point_events`), and the test is not run when they are not recorded (P2-2); a package holding the component with no counterpart is held and disclosed (`unpaired`, the record's `missing_subtraction`), and the test no longer says it covers every package (P2-4). `toolkit/spec/schema.py`: a ratio of 1 is allowed, since it sets a package's burden to its counterpart's (P2-3), and a test whose component the comparator holds is refused, since it would move the baseline every benefit is measured from (P2-1). `toolkit/spec/preconditions.py` `decision_test_runs` refuses an `outcome_ratio` or `burden_scale` test as an engine-departure test, saying why: both hold the engine (P2-5). results.md and the claims catalog state each tested ratio and the package pairs (P3), and a `burden_scale` test's shares, each reference's factor and its transfer assumption (#246 P2-5). The SP-OR5 and SP-OR7 tests and the P2-1, P2-3 and P2-4 fixes come from the closed PR #247, without its `group_outcome_scale` type. P3 item 7 (emulator-only execution) needed no change: the test's contract is that nothing is evaluated anew, which JR11 checks. Self-tests SP-OR5, SP-OR7, JR11c-g and BS-R8 failed first.
