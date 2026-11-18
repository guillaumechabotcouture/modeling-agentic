---
phase: "v2 pre-sign checks"
commit: "red flag"
---

## Changelog

**The critics see a heuristic note when the most expensive option of a component group wins in nearly every unit.** New `toolkit/analysis/red_flag.py` reads the point plan (`analysis/point.yaml`) and the spec. For each group of two or more components (`options.components.<c>.group`), and in each unit whose plan holds one of them, the dearest is the component with the highest fixed cost there, from cost items other than per_outcome through `toolkit/alloc/costs.py`. When the dearest is chosen in at least 90% of those units, and in at least 5 of them, the v2 critique task adds a note to the reviewer's message. The note asks which of burden, relative effect or price makes it win, and whether its effect rests on trial evidence. It is a note, never a gate or a finding (owner direction 2026-11-18): the reviewer decides whether it needs a blocker. A note that cannot be computed says so and never stops the review.
