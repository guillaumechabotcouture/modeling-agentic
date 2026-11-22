---
phase: "v2 debt"
commit: "flaky self-tests δ"
---

## Changelog

**`scripts/resume_smoke.py` RS8 `cap_stop` made deterministic.** The compute cap counts wall-clock seconds times pool slots from before the pool starts, so process start and model load are charged against it (`compute._map_local`, by design: those seconds use the cores the allowance pays for). The scenario gave the batch a 1.5 core-second allowance with 0.6 s rows, so it kept a row only if the pool started in under 0.9 s. On a busy CI runner it did not, and CI showed "the interrupted attempt persisted 0 of 6 rows". It reproduced locally every time with 0.5 s or 1 s of CPU burned at model load. Row 0 now finishes and rows from 1 on hold until the allowance stops the batch, so the stop never depends on how many rows fit. The allowance is 12 core-seconds and only has to outlast the pool's start and row 0. The toy model now takes 1.5 s to load (the old allowance) in this scenario, so every run checks a start that would have failed before. The assertion is stronger: exactly row 0 must be kept, where before any non-empty proper subset passed. `scripts/write_audit.py` counts the scenario's new pid file, which is in the smoke's own work dir (4 reviewed open-write sites, up from 3). No product code changed.
