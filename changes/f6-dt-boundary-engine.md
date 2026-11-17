---
phase: "F6"
commit: "DT-BE α"
---

## Changelog

**A `boundary_engine` decision test (F6 run 4, owner decision R3 on RIG-002).** The emulator's near-switch ranking check (`toolkit/analysis/boundary.py`) failed its 0.8 rule in F6 runs 2 and 3: 15 of 20 units, also after 64 more uniform design points. The owner chose to declare the shortfall with a test of whether it changes the decision. The new type `boundary_engine` takes no parameters. The boundary check now keeps its engine-side tables on the analysis (`A.boundary_engine`: benefits, costs with their component split, and the packages run on the engine for each boundary unit). The test puts them in place of the emulator's tables at those units and re-solves the plan: it reads stored engine rows only, with no new job or evaluation. Packages not run on the engine keep the emulator's values, as the check's own fail-closed scale test requires. The record names the units, the packages run there and the substituted benefits. The test is not run when the boundary check produced no tables. Sign-off refuses parameters and a spec on the direct path. Self-tests SP-BE1-3 (schema) and BE-R1-3 (`run.py`, after BD-R1) were written first and failed (the type was unknown). Codex round 1 raised three P1s and two P2s. All are fixed, each with a test written first that failed (β).
- Only the two compared cells (the chosen package and its runner-up: benefit, cost and component split) are substituted at each boundary unit, because the check's scale test proves only those are engine-derived. Every other cell keeps the point tables (P1-1, P1-2; BE-R4).
- The re-solved plan is guarded with the point evaluation's corrections (P1-3; BE-R5).
- A boundary check clears its stash on entry and publishes it only when it completes (P2-4; BE-R6).
- An infeasible re-solve is recorded as not run, with why (P2-5; BE-R7). Codex round 2 raised one P1 and one P2, both fixed with their tests written first (γ).
- A point correction on a cell the engine now supplies no longer blocks the re-solved plan; every other correction still does (BE-R8).
- Without the point evaluation's recorded corrections the test is not run (BE-R9).
