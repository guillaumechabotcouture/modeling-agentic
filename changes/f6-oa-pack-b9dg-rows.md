---
phase: "F6"
commit: "OA pack rows α–β"
---

## Changelog

**Malaria pack: stored rows for B9d-B9g (engine v0.18, pre-fit values).** `toolkit/engines/malaria/benchmarks/results.yaml` gains the rows of the Coiled run `f6-prep-oa2-pack-b9dg` (`benchmark.py run --only B9d,B9e,B9f,B9g`; stored and verified under `outputs/_engine/pack/v018_b9dg/`), merged with `benchmark.py merge` (no engine run), so `verified_results` accepts the malaria engine's record again. The rows are B9d 0.835 (in band), B9e 0.490, B9f 0.332 and B9g 0.789 (all three out of band). `benchmark.merge_results` now keeps every input's calibrated settings, refusing a setting calibrated differently in two inputs; before, a setting only the new run calibrated (here llineup) was dropped. It also keeps the initialisation record and each input's provenance (`merged_from`: its rows and any re-stamp record). Self-test BP50b.
