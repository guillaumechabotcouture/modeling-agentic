---
phase: "v2"
commit: "WB ζ"
---

## Changelog

**Review workbench: fixtures for the Outcomes view.** `tools/workbench/fixtures.py` now writes every file the Outcomes view reads, for both deliverable kinds (`package_per_unit` and `estimates`), and a second run of the same question for the comparison between runs (`build(..., other=...)`). 36 variants, each one edit: the dearest option chosen nearly everywhere, a headline above half the comparator, a point value outside its interval, plan-changing and near-miss tests linked and not linked to a register row, burden references outside their band with and without a test, value of information on too few draws, a component with no boundary row, stability verdicts that disagree, a variant over budget, a shift between runs with and without a spec key that differs, and contradictions and missing pieces (an unparsable tests file, an interval with its ends reversed, a plan naming an undefined package, an unknown deliverable kind). The clean run no longer holds a plan-changing test or a test that did not run; both are variants. The specification records the Outcomes model and the rules settled from its acceptance passes on stored runs, and harness gap H17. Self-tests pass in every workbench module; no stored run's value is committed.
