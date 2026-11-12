---
phase: "F6"
commit: "S2-BG3"
status_group: "F6 Stage 2"
---

## Changelog

**Finished clusters count what they billed, and their reservations are released (F6 S2-BG3).** On 2026-11-11 at 18:20, `budget.py status --scope f6` showed $306 used against $191.68 billed, and $67 still reserved for a launch whose run, keeper and overflow clusters (`v2-nigeria-gf13-p6`, ids 2364928, 2364962, 2364947) were all stopped. That blocked further launches. Two causes, both in `_cluster_cost`. First, every cluster counted max(billed, estimate), and the estimate (VM-type upper bounds with margin) is about 1.6 x what Coiled bills, so finished clusters were over-counted. Second, a liveness check that read instance states. A stopped cluster's record can keep a VM in a non-final state (an overflow VM refused for quota stays `queued`), which read as live. That VM was projected to the present, and it held the launch's reservation at max(0, cap - spend) instead of releasing it. The fix: a cluster whose every process is stopped or errored, with its stop time, is finished. It counts its billed USD (`total_cost` x 0.061 + `cloud_total_cost`), and its VM records end with their processes. If a cost is missing, it counts the conservative estimate, and its partial bill is left out of the measured rate. A cluster with a live instance still counts max(billed, estimate), and still refuses a missing cost. A reservation whose clusters are all finished is released. Self-tests were written first and failed before the change. BR19 reproduces the P6 case: run, keeper and overflow stopped, one overflow VM left `queued`; the reservation is released and spend equals billed. BR8 checks that a finished cluster counts its bill, BR8e covers a missing cloud cost, and BR9 that a missing cost refuses only while an instance is live.

## Status

- **S2-BG3:** finished clusters count their bill and release their launch's reservation; `status --scope f6` to be re-read after merge.
