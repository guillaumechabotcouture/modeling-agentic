# Renewal engine: notes

Version 0.1 (2026-11-05). Our own numpy code behind `toolkit/engines/contract.py`; no external simulator, no R.

## Model

One closed population. Time runs on the generation interval's grid (one week or one day). With infections `I_t`
and susceptibles `S_t` as shares of `N`, the declared generation interval `w_1..w_L` and a transmissibility profile
`c(t)`:

    lambda_t = R0 c(t) sum_s w_s I_{t-s},   I_t = S_{t-1} (1 - exp(-lambda_t)),   R_t = R0 c(t) S_{t-1}

- **Exponential survival, not the linear form.** The update never infects more than the remaining susceptibles,
  and with constant `c` the final-size relation `log(S_inf/S_0) = -R0 (seed + sum I)` holds exactly at any step
  (RW2a-d, RW2f). The linear form `I_t = S R0 sum w I` misses it (RW2e: 0.8687 against 0.7968). In the growth phase
  both reduce to the Cori/Fraser renewal equation, so `R = 1/sum_s w_s e^{-r s}` (RW1a-c).
- **Time-varying R_t.** `c(t)` is piecewise constant between declared knots (piece 0 is the reference, so `R0` is
  the first piece's reproduction number), times `(1 - effect)` for each intervention component while it is active.
  A question that needs no intervention declares none.
- **What is reported.** `attack_rate` (new infections in a window over `N`), `reff_end` (`R0 c S_end` with the last
  step's `c`), `reff_ref` (`R0 c_0 S_end`: the same transmissibility as at the start, no component), `growth_rate`,
  the `R_t` and incidence series. `cori_rt()` is the Cori et al. (2013) estimator from a case series; it recovers a
  known `R_t` (RW5a), and its 95% intervals cover the truth in 0.93 of windows on Poisson-reported series (RW5b).
- **Observation is not in the engine.** Reporting fraction, reporting delay and overdispersion belong to each data
  source's measurement model in the harness (`toolkit/analysis/observation.py`, `toolkit/analysis/estimates.py`). No
  source is treated as the truth.

## What a case series does and does not determine (RW4)

The recursion depends on `S/N` only, so `(R0, s0, N)` and `(R0 s0, 1, s0 N)` give identical counts. With a case
series and no serosurvey:
- `R0 s0` (the effective reproduction number at the start) and `R0 S_end` (`reff_end`) are determined by the shape of
  the curve, whatever the reporting fraction or `s0`;
- the attack rate scales with `s0`, and the reporting fraction with `1/s0`, so they are determined only up to `s0`.
  The data bound the attack rate from above, at its value when `s0 = 1`.

With `s0` known and an epidemic that turns over by depletion, the reporting fraction is identified (RW6b). An
unknown seasonal change in transmissibility (`transmissibility: {knots}`) would weaken that again.

## Validation

`benchmarks/pack.yaml`, 23 benchmarks, all held out (no engine-level fitted parameters). 22 in band; RW9 (published
Cori et al. 2013 worked examples) is `not_assessable` until its values are transcribed with a quoted span. The card
carries the label "validated against analytic identities ... published benchmark RW9 not yet assessed".

    python3 -m toolkit.engines.benchmark toolkit/engines/renewal/benchmarks/pack.yaml run --workers 2   # ~2 s
    python3 toolkit/engines/renewal/engine.py --card
    python3 toolkit/engines/renewal/engine.py --self-test                                             # ~5 s

`ranges.yaml` holds the published ranges a planner declares priors from, each with its spread and source
(`verified: unverified` until the evidence service matches a quoted span).

## Out of range

Births, deaths, waning, migration, vector dynamics (absorbed into `R0` and `c(t)`), stochastic fade-out, spatial
structure, age structure.
