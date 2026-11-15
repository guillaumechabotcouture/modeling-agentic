---
phase: "F6"
commit: "unit-attribute tests ε"
---

## Changelog

**Unit-attribute decision tests: three fixes from the #228 re-review.** (1) A changed cell is written in its column's own format: in a column whose every cell is an integer literal (a binary eligibility flag, a count) an integral value stays an integer literal, so the eligibility reader accepts it (`_cell`). A case whose modified table the units readers still reject is refused with their reason in `rejected_values`; the run no longer aborts. (2) Identity compares everything the arm's analysis reads from the table (`arm_reads`): the built inputs (encoded emulator features on the emulator path, engine attributes otherwise), the population, calibration target and region cells, the cost of every package, the feasible packages, locks and cap weights per unit, and the cap maxima and group links. A population change the emulator features do not see is no longer refused as an identity. (3) A case is "provably unchanged" only when its feasible packages, locks, caps, group links and planning budget equal the headline's (`constraints_of`), so a change that unlocks or locks a package through eligibility is re-solved and reported as able to move the plan. Self-tests UA-R7, UA-R7b, UA-R8, UA-R9 (`toolkit/analysis/run.py`; the reviewer's probes).
