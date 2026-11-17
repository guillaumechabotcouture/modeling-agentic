---
phase: "v2 pre-sign checks"
commit: "red flag"
---

## Changelog

**When the most expensive option of a component group wins in nearly every unit, the gate holds the run before WRITE until it records why.** New `toolkit/analysis/red_flag.py` runs from the analysis contract gate (`toolkit/analysis/contract.py`) on the point plan. For each group of two or more components (`options.components.<c>.group`), and in each unit whose plan holds one of them, the dearest is the component with the highest fixed cost there, from cost items other than per_outcome through `toolkit/alloc/costs.py`. The flag is raised when the dearest is chosen in at least 90% of those units and in at least 5 of them. It is a HIGH, non-declarable finding (`red_flag_mechanism`) until `mechanism_checks.yaml` in the run directory holds `{group, component, why_it_wins, evidence: [...]}` for it: which of burden, relative effect or price makes it win, and the trial or benchmark evidence the mechanism rests on. The fix task may write that file (`agents/v2_sdk_tasks.py` OWNS). A check that cannot run fails closed.
