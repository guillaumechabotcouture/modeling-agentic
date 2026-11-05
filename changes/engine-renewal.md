---
phase: "v2 D"
commit: "renewal α-ε"
---

## Changelog

**Renewal engine family and the `estimates` deliverable (no allocation units), so the outbreak eval question runs on
v2.** Engine (`toolkit/engines/renewal/`, our own numpy code behind the contract): a discrete renewal equation
(Fraser 2007; Cori et al. 2013) on the declared generation interval's grid with susceptible depletion in the
exponential-survival form, piecewise transmissibility between declared knots (time-varying R_t), intervention
components as multipliers on R_t, window metrics `attack_rate`, `reff_end`, `reff_ref`, `growth_rate`, `run_batch()`,
the `schedule()` and `run_series()` hooks, and `cori_rt()`. The observation model is not in the engine: each source's
measurement model (reporting fraction as `under_ascertainment`, a declared reporting delay, negative-binomial
overdispersion with its own prior) is the harness's. Pack: 22 of 23 benchmarks in band (Wallinga-Lipsitch, final
size at weekly and daily steps with the linear-form negative control, reproduction-number identities, scale
invariance, Cori recovery and interval coverage of a known R_t, refit of a known R0 and reporting fraction, a second
loop-by-loop implementation); RW9 (published Cori et al. 2013 examples) not assessable until transcribed.
`ranges.yaml`: sourced ranges with their spread (unverified). Core, listed separately because it needs two-disease
acceptance: `deliverable.kind: estimates` (`toolkit/spec/estimates.py`: one population, `dynamics`, observation
sources, `posterior` calibration, outputs as engine window metrics, a threshold rule; allocation sections refused),
`toolkit/analysis/estimates.py` (joint posterior over the declared priors and each source's measurement parameters,
draws with provenance, outputs re-run at each draw, per-output "what the data determine" with automatic prior-range
tests whose data-rejected ends cannot change the decision, a posterior predictive check, figures, claims and
results.md), the shared `uncertainty.scalar_report()` writer of `uncertainty_report.yaml`, and small hooks in
`schema.py`, `run.py`, `api.py`, `contract.py`, `artifacts.py` and `thresholds.yaml`. Allocation specs are unchanged.
