---
phase: "F6"
commit: "step 0 r3"
---

## Changelog

**Step 0 follow-up (PR #217 review round 3): treated-risk bounds, precise constant-hazard conversion, unknown keys and empty names refused.** In the package-rate engine's `trial_priors` mode, a risk-ratio conversion checks the treated risk p0 x RR, including odds-derived ratios, at the top of the declared range and again for every resolved value (overrides and draws): below 1 for constant hazard, at most 0.10 for the rare-outcome approximation. The constant-hazard rate ratio is `log1p(-p0 RR) / log1p(-p0)`, exact at tiny risks. Every configuration key must be one the mode reads (a misspelt `interactions`, `overlap`, `range` or `baseline_source` no longer falls back to its default silently), and an empty or blank component or outcome name is refused. Review round 1 of #221: the inclusive rare-outcome bound has a 1e-12 relative tolerance and construction and evaluation check it with the same function; every resolved conversion is checked before the exposure states, whatever the package; a control risk below 1e-300 is refused (and the small-risk limit used at evaluation); a malformed scalar section gives the contextual error.
