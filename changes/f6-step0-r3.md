---
phase: "F6"
commit: "step 0 r3"
---

## Changelog

**Step 0 follow-up (PR #217 review round 3): treated-risk bounds, precise constant-hazard conversion, unknown keys and empty names refused.** In the package-rate engine's `trial_priors` mode, a risk-ratio conversion checks the treated risk p0 x RR, including odds-derived ratios, at the top of the declared range and again for every resolved value (overrides and draws): below 1 for constant hazard, at most 0.10 for the rare-outcome approximation. The constant-hazard rate ratio is `log1p(-p0 RR) / log1p(-p0)`, exact at tiny risks. Every configuration key must be one the mode reads (a misspelt `interactions`, `overlap`, `range` or `baseline_source` no longer falls back to its default silently), and an empty or blank component or outcome name is refused.
