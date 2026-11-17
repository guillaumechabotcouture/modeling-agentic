---
phase: "F6"
commit: "DT-BE α"
---

## Changelog

**A `boundary_engine` decision test (F6 run 4, owner decision R3 on RIG-002).** The emulator's near-switch ranking check (`toolkit/analysis/boundary.py`) failed its 0.8 rule in F6 runs 2 and 3: 15 of 20 units, also after 64 more uniform design points. The owner chose to declare the shortfall with a test of whether it changes the decision. The new type `boundary_engine` takes no parameters. The boundary check now keeps its engine-side tables on the analysis (`A.boundary_engine`: benefits, costs with their component split, and the packages run on the engine for each boundary unit). The test puts them in place of the emulator's tables at those units and re-solves the plan: it reads stored engine rows only, with no new job or evaluation. Packages not run on the engine keep the emulator's values, as the check's own fail-closed scale test requires. The record names the units, the packages run there and the substituted benefits. The test is not run when the boundary check produced no tables. Sign-off refuses parameters and a spec on the direct path. Self-tests SP-BE1-3 (schema) and BE-R1-3 (`run.py`, after BD-R1) were written first and failed (the type was unknown).
