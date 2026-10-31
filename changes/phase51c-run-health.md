---
phase: "51"
commit: "γ"
status_group: "Merged (Phase 51, development process)"
---

## Changelog

**A live run-health monitor.** Live runs found harness defects only after hours: on attempt 8 compute queued on one VM while 23 sat idle for most of a day (A16), the round-5 gate went from 19 to 83 findings, session 2 was killed at 07:00 UTC, and a dead caller was charged 400 core-hours. Each was in data the run already writes. `cloud/run_health.py` holds six pure rules over a snapshot (trace, gate history, compute ledger, budget, status.json, scheduler view): `idle_compute` (compute chunks queued while a compute-tagged VM has had nothing to run for 15 min), `gate_spike` (a round's first gate more than 3x the previous round's last gate, at least 20 findings), `session_error`, `stall` (no heartbeat for 10 min or no tool use for 45), `spend_early` (over 50% of the cap before round 4) and `compute_no_rows` (a call charged at least 1 core-hour with no completed row); thresholds in `cloud/run_health.yaml`, where unknown rules or settings are errors. `cloud/run_cluster.py <config> health [--loop]` collects the snapshot attach-only (the scheduler view and the run-file view are sent by value, so runs launched earlier have them) and appends alerts to `<out_dir>/health.log`; unreadable inputs are alerts too. Self-tests replay attempt 8's recorded trace, gate history and compute ledger (reduced to the fields read, `cloud/fixtures/e7r8_health/`) and synthetic scheduler snapshots; no test attaches to a cluster. Retro on attempt 8: session_error at 2026-10-29 07:00:25 UTC, gate spike at 2026-10-30 01:44 UTC, compute with no rows 4 times (first 2026-10-28 21:48, the 400 core-hour one at 22:58), and, on snapshots reconstructed from the ledger's `workers: 1` batches, idle compute from 2026-10-28 21:53 UTC (25 episodes); no stall or early spend.

## Status

- **γ:** `cloud/run_cluster.py <config> health --loop` flags idle compute VMs with work queued, gate spikes, session errors, stalls, early spend and compute charged with no rows while a run is live; replayed on attempt 8 it fires from its second hour.
