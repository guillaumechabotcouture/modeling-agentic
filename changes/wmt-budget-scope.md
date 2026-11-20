---
phase: "F6"
commit: "α"
---

## Changelog

**Budget scope for the world-model trials.** `cloud/budget_scopes.yaml` gains scope `wmt` with prefix `wmt-`, so world-model trial sweeps run on Coiled (`cloud/vm_job.py run wmt-… --budget-scope wmt --cap-usd N`) instead of the owner's laptop. It takes effect only when the owner signs a `wmt` budget with `cloud/budget.py set --scope wmt`.
