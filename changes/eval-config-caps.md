---
phase: "harness benchmark"
commit: "eval-config-caps α-β"
---

## Changelog

**The five eval configs can pass the launch budget guard (owner decision, 2026-11-21: an `ev` budget scope of
$450).** `cloud/configs/_eval.py` set no `cap_usd`, and `run_cluster.budget_guard` refuses a config without one;
no budget scope covered the eval cluster names either. The shared config now sets `cap_usd` to US$35 per run times
the replicates on the cluster and `budget_scope="ev"`, keeps the model cap (`--max-budget-usd 35`), 3 rounds,
`--signoff lead` and the explicit `pipeline`, and refuses an `EVAL_TAG` that would put the cluster name outside
a-z, 0-9 and -. `cloud/budget_scopes.yaml` gains scope `ev: [ev-]`, which covers every eval cluster
(`ev-<question>-<tag>-gcc` and its -keeper / -overflow) and no other launcher's. The scope takes effect only when
the owner signs it (`cloud/budget.py set --scope ev ...`); nothing here signs a budget. Self-tests: EV0-EV4 in
`cloud/run_cluster.py` (each of the five configs, at 1 and 2 replicates: cap, scope, every cluster name under `ev`,
pipeline, rounds, model cap, sign-off mode; a bad tag refused at load), BR15b in `cloud/budget.py` (the tracked
scopes file signs `ev` at $450 with prefix `ev-`, and its launch check admits an eval cluster and refuses another
scope's name).
