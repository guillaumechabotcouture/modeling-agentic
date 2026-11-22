---
phase: "v2 autonomy"
commit: "W0-J ζ"
---

## Changelog

**The supervisor gets its own budget scope, `sv`.** `cloud/supervisor.py`'s default cluster name is now `sv-supervisor-gcc` (was `ev-supervisor-gcc`, which counted the supervisor against the eval benchmark's `ev` budget), and `cloud/budget_scopes.yaml` adds the scope `sv` (prefix `sv-`); the owner signs it with `budget.py set --scope sv`. Self-tests SV16c (supervisor) and BR15c (budget) failed before the change.
