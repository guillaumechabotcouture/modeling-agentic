---
phase: "F6"
commit: "α"
---

## Changelog

**Fsv2 VM prices.** `cloud/vm_prices.yaml` gains the Standard_FNs_v2 family (Azure list $0.0423 per vCPU-hour) and `budget.OBSERVED_VM_TYPES` names Standard_F8s_v2. Coiled chose that type for a `--cpu 8` request without `--memory` (cluster 2376181, f6-prep-idsweep); the price table did not list it, the job's cost cap stopped it (fail closed, as designed), and every later f6 budget check refused because a counted cluster could not be priced.
