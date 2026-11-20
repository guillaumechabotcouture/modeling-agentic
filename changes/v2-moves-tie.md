---
phase: "v2 determinism"
commit: "MT"
---

## Changelog

**A decision test reports the same worst value on every machine when regrets are tied.** `toolkit/analysis/moves.py` `aggregate` chose the tested value reported as worst by regret with `max()` over the raw regret. For a test whose values all had zero regret, noise below 1e-9 chose the value, and two CI runners reported different ones, with different top-level `reoptimised_objective` and `objective_gap` (found by #262's golden records, 2026-11-20). Now tested values whose regret agrees within `TIE_RTOL` (1e-6 of the re-optimised objective; the noise seen was below 1e-9 on objectives of 37 to 353) are tied, the first tested value in declared order is reported, and its `worst_by_criterion` entry records `tie: true` and `tied: <how many>`. Verdicts, the criteria that fire, plans and each tested value's own row do not change. Self-tests MV24-MV29 were written first; MV25 shuffles noise below the tolerance 200 times and gets one witness.
