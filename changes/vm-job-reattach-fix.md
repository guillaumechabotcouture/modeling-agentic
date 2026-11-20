---
phase: "F6"
commit: "α–δ"
---

## Changelog

**`vm_job wait --cap-usd`: one cost snapshot, a conservative reservation for older clusters, and a stop after any later refusal.** A Codex re-review of #232 found three holes. `budget.reattach_budget_ok` read the windowed and the lifetime cost in two requests, so billing that advanced between them shrank the replacement reservation; both now come from the one details record the windowed spend read. For a cluster created before the budget's window, the window counts only its estimate while the cap is on max(billed, lifetime estimate), so the window could pass the budget under the cap; while it has a live instance, its reservation is now the cap less the lifetime estimate, so window spend stays within budget + reservation until the meter stops it (one with no live instance reserves the cap less its bill, and a reservation that would be zero or negative is not written, so the ledger stays valid). In `vm_job`, once the gate has accepted the cap, it is never re-run: only the meter is retried, and any refusal or failure after acceptance stops the cluster by id. Only a refusal by the gate itself leaves the cluster as it was. Self-tests BR22e-h, RN32i-j.
