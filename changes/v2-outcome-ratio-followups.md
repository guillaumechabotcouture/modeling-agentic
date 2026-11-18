---
phase: "F6"
commit: "#248 α–β"
---

## Changelog

**`outcome_ratio` follow-ups from the Codex review of #245, and #246 P2-5 (issue #248).** P2-2 (point corrections guard the re-solved plans) and P2-3 (ratio 1, allowed for a component with a base outcome adjustment) were done in #251; JR11d checks P2-2 here. `toolkit/analysis/decision_tests.py` `_outcome_ratio`: a package holding the component with no counterpart is held and disclosed (`unpaired`, the record's `missing_subtraction`), and the test no longer says it covers every package (P2-4). `toolkit/spec/schema.py`: a test whose component the comparator holds is refused, since it would move the baseline every benefit is measured from (P2-1). `toolkit/spec/preconditions.py` `decision_test_runs` refuses an `outcome_ratio` or `burden_scale` test as an engine-departure test, saying why: both hold the engine (P2-5). results.md and the claims catalog state each tested ratio and the package pairs (P3), and a `burden_scale` test's shares, each reference's factor and its transfer assumption (#246 P2-5). The SP-OR7 test and the P2-1 and P2-4 fixes come from the closed PR #247, without its `group_outcome_scale` type. P3 item 7 (emulator-only execution) needed no change: the test's contract is that nothing is evaluated anew, which JR11 checks. Self-tests SP-OR7, JR11c, JR11d, JR11f, JR11g and BS-R8 failed first (on the α commit, before #251 merged).
