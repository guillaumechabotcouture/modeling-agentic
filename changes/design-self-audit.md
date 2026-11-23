---
phase: "v2"
commit: "SA"
---

## Changelog

**Self-audit checks design note (docs only).** `docs/design/self-audit-checks.md` designs six deterministic, harness-computed audits for the silent flaws an outside benchmark found in bare agents' models: the modelled effect integrated over each trial's estimand at the run's own values (SA1); fit diagnostics beyond average error, namely decision-relevant parameters at a bound, the sign and correlation of temporal changes by group against a predictive reference, and holdout skill against naive baselines (SA2); holdout by construction with shock periods, and a provenance class for calibration targets (SA3); nominal settings against their realised values at rollout (SA4); denominator provenance from a population source class (SA5); and effect scoping across arms for registered engines (SA6). Each gives what it computes, its artifacts, tolerance and source, finding and route, limits, tests to write first and smoke markers; with a table of the mechanisms each extends, an 11-PR priority order with sizes and 14 open questions with recommended defaults. No code changes.
