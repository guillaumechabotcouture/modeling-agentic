---
phase: "50"
commit: "γ"
---

## Changelog

**Keeper start-up reads the scheduler's worker table only once it is populated (backlog fix).** v2blind1's launch (2026-10-31) ended with `KeyError: 'workers'` in `start_keeper`, after the keeper cluster's client had logged "Failed to reconnect to scheduler after 30.00 seconds, closing client": distributed's `Client.scheduler_info()` returns `{}`, with no `workers` key, whenever the client is not connected (before its first identity reply, while it reconnects, and after a failed reconnect has closed it). The run was already fired, but `clusters.json` never recorded the keeper. `worker_side.scheduler_workers` now reads the table with bounded doubling backoff (a closed client is replaced from its cluster by `live_client`) and, past the bound, raises `SchedulerInfoUnavailable` naming the last state instead of reading an empty table as "no workers". `launch` records the keeper in `clusters.json` before reading anything back, and `confirm_keeper` fails the launch with a message saying what to check if the keeper cannot be confirmed. The keeper loop waits out an unpopulated identity within the tick instead of dropping into its reconnect path, which re-attached and called `adapt()` again; past the bound it still logs and reconnects, so it never stops holding the pool. `status`, `keeper-log` and the overflow step use the same reader. Self-tests RC52a-g (fake clients and a fake `coiled`, no network) replay v2blind1's sequence and failed before the fix.
