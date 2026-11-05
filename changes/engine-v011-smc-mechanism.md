---
phase: "v2a"
commit: "v0.11 α–δ"
---

## Changelog

**Malaria engine v0.11: SMC mechanism from the literature (owner approval 2026-11-05).**

Up to v0.10, every child reached was cleared of infection and then protected against infection at a flat `smc_pe` = 0.882 for 28 days. That value is the ACCESS-SMC protective effectiveness against *clinical malaria*. The per-round reach came from two unsourced access groups (`smc_hi_frac` 0.85).

v0.11 changes the mechanism in four places (`model.SMCDelivery`):
- **Reach.** A child's per-round reach is a persistent beta propensity with mean `smc_cov` = 0.796 and intra-child correlation `smc_rho` = 0.417. These come from a beta-binomial fit to the Nigeria 2020 distribution of SMC cycles received (Malaria Consortium coverage report, Table 21). The fit reproduces 6% of children with no cycle and 60% with all four. Independent rounds would give 0.2% and 40%.
- **Adherence.** A reached child completes the course with probability `smc_adh` = 0.944 (Table 16).
- **Clearance at dosing.** Kept from v0.10.
- **Protection against new infection.** It decays as E0 exp(−(t/L)^k ln2) (`model.smc_protection`), with `smc_e0` 1, `smc_half` 32 d and `smc_k` 5.4 (Burgert et al. 2022, Table 1). After the day-1 dose only, it is E0 × `smc_partial` 0.910 (Cairns et al. 2022, 79.8/87.7). Applying that ratio to infection protection is a named assumption.

There is no separate clinical factor. During the season, the trials measured a similar effect on infection and on clinical malaria (Dicko 2011 and Konaté 2011). ACCESS-SMC's 88.2% is a check of the effect that emerges, not an input. `smc_pe` and `smc_hi_frac` are removed.

On owner guidance (2026-11-05), every SMC parameter except `smc_k` is carried in `priors.yaml` as a range that covers the literature spread across trials, years and surveys. ENGINE_NOTES says that v0.11 fixes the mechanism's form. Which values inside the ranges agree with data is a question for a joint calibration, which is the next step.

**Tests.** The tests were written first. Q0–Q6 cover:
- the card and priors;
- the Weibull curve;
- the beta-binomial reach, checked against Table 21 and by persistence across rounds;
- the in-sim reach, adherence and clearance;
- a population-level clinical-effect diagnostic;
- fail-closed checks on every batch member;
- the burn-in key, and per-member parameters in a shared burn-in.

**Benchmarks.** `validate_once.sh 16 12` recomputed every evidence file on Coiled (cluster 2331751, about 100 credits). The same 14 of 17 benchmarks are in band, with 9 of 12 held out. Only the held-out SMC benchmarks moved, and both stay in band: B13 went from 0.360 to 0.286 (target 0.27) and B17 from 19.0 to 14.7. All other numbers are unchanged to the last digit.

**Codex review.** Round 1, before the benchmark run, found no P1. Its P2s were fixed test-first.
