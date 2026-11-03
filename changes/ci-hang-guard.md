---
phase: "v2 debt"
commit: "CI hang guard"
---

## Changelog

**CI's job limit is a guard against a hung job, not a duration budget, and the self-tests run two at a time.** The `checks` job's `timeout-minutes` goes from 40 to 120: on loaded runners a healthy run took 38–41 minutes, and the 40-minute limit cancelled green runs of #131, #133 and #135 with no failing test (2026-11-03). `scripts/self_tests.py` runs with `--jobs 2` and `OPENBLAS_NUM_THREADS`/`OMP_NUM_THREADS` set to 2, the settings every agent uses locally, which should roughly halve the self-test step. No test or code path changes.
