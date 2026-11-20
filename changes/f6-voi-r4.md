---
phase: "F6"
commit: "VOI η"
---

## Changelog

**Value of information: review round 4 of #212.** Perfect information tells draws apart by their full benefit and cost tables (and feasibility), not by summaries: draws whose candidates and recorded inputs agree but whose tables differ (as with per-unit calibration attributes or emulator error, which have no scalar input) are no longer merged into one class for the overspend allowance. The decide-now solve within the budget in every draw, and each plan decided within an input's bins, leave out packages a model correction bears on in those draws before solving, so the exact zero-allowance optimum is kept instead of being discarded after the solve. The contract check requires, for every computed input, a finite share over budget for the policy chosen after learning, within the signed allowance; each candidate's `draws_over_budget` an integer in [0, draws]; and candidate ids that are unique strings (a malformed id is a finding, not a crash). Self-tests G1-G3 (the reviews' reproductions), written first, failed before the change (G1 EVPI 0 against 2.5; G2 EVPI 9 against an exact 5; G3 five gate mutations passed or raised).
