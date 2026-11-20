---
phase: "v2"
commit: "WB δ"
---

## Changelog

**Review workbench: fixtures for the Model and Figures views; the clean fixture holds nothing declared.** `tools/workbench/fixtures.py` now writes every file the Model and Figures views read (an engine record with benchmarks, an engine choice, spec-set values, a figure set in three tiers with images, tables and provenance records, and the estimates deliverable's figure shape), with 24 Model and 12 Figures variants, each one edit, including contradictions and missing pieces (an unknown status word, a benchmark with no band, a malformed provenance record, an unknown producer). The clean run now holds no departure, no declared miss and no scope declaration, and no two of its files disagree; a variant `declared` holds one of each, and the register's cases that need a declared item use it. `checks.py` gains a shared `verified_spec` loader, and the decision-test loader returns each test's share and threshold. The specification records the Model and Figures rules settled from their acceptance passes on stored runs, and harness gaps H13 to H16. Self-tests pass in every workbench module; no stored run's value is committed.
