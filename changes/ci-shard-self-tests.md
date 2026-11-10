---
phase: "v2 debt"
commit: "CI self-test shards"
---

## Changelog

**CI runs the self-tests in 4 parallel shards.** `scripts/self_tests.py --shard K/N` runs only shard K of N: the slowest self-tests are dealt first, each to the shard with the least measured CI time so far (`WEIGHTS_S`, from run 42734450748; others count 5 s), so every self-test runs in exactly one shard and the split depends only on the file list. Discovery problems fail every shard. The workflow's `self-tests` job is a 4-shard matrix (`fail-fast: false`, 60-minute guard) and the other steps move to a `checks` job. The self-test step took 25 minutes of a 27-minute run (about 50 minutes of self-test time two at a time); each shard now carries about 15 minutes of it, so a run should take about 10 minutes, bounded by `agents/stage_machine.py` (8.5 minutes). ST14 covers the partition, the order-independence, the `--shard` refusals, the live 4-shard balance and stale weight entries.
