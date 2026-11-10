---
phase: "F6"
commit: "CI-1"
---

## Changelog

**A longer per-test limit for the slowest engine self-test (CI).** `toolkit/engines/malaria/selftest_late.py` took 866 s of the 900 s per-test limit on the #179 run and was killed at 900 s on #180 and #181, so shard 3 failed on every PR whatever it changed. The CI limit and `scripts/self_tests.py`'s default are now 1800 s (the job limit stays 60 minutes), and its shard-balancing weight is 870 s, so it no longer shares a shard with the other heavy self-tests. Splitting the module changes the malaria engine's code fingerprint, which invalidates its stamped evidence, so the split waits for the next engine version change. No change to what runs.
