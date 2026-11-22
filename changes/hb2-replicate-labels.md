---
phase: "v2"
commit: "hb2-replicate-labels"
---

## Changelog

**collect and status act on every replicate the launch recorded.** `cloud/run_cluster.py` `collect` and `status` looped over `cfg.labels`, which an eval config takes from `EVAL_REPS` at collect time; with it unset, R2 was skipped without a message (baseline hb1, all five R2s), and a plain `collect` could have shut down a cluster still running R2. A new `recorded_labels(cfg, assign)` takes the labels of assignments.json merged with `cfg.labels`, names a label recorded at launch that the config does not name, and names and skips a config label with no launch record (it was a KeyError). The default shutdown after `collect` is refused while any recorded label has no final result, naming it. Self-tests RL1-RL5 (RL1, RL3, RL4 and RL5 failed before the change).
