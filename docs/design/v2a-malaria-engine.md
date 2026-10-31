# v2 workstream A: malaria engine mechanics and benchmark pack (design note)

Status: design note, written before the code (2026-10-31). Branch `v2a-engine`, PR into `v2`.
Context: `docs/design/architecture-v2.md` §3.3; spike findings in `spike/RESULTS.md`.

## Goal

The engine reproduces the trial evidence from mechanisms, fitted once, not per question.
The spike left four problems: B15 (ITN×IRS interaction OR about 1.0 against 2.43), B9 (PBO
capped by the ITN-vs-none killing fit), B19 (IRS $/DALY out of reach at the decision's
price) and two narrow misses (B16c, B20).

## Evidence this design starts from

Read from the source papers (PMC full text) before writing code:

1. **Protopopoff 2018 (Lancet, PMC5910376), Table 2.** At 9 months: standard LLIN 55%, PBO
   LLIN 31%, standard LLIN + IRS 29%, PBO LLIN + IRS 26%. ORs against standard LLIN: 0.37
   (0.21–0.65), 0.33 (0.19–0.55), 0.29 (0.17–0.49); interaction 2.43 (1.19–4.97). The
   factorial model includes the interaction term, so the "main effect" ORs equal the
   single-arm comparisons. **The pack's B9 (and B9b) pool PBO over the IRS arms; the trial's
   0.37 (and 0.40 at 21 months) is PBO alone against standard LLIN.** That is a basis error
   in the pack, fixed here. Site facts the pack had as assumptions: permethrin mortality
   8.8% in *An. gambiae* s.l. (pack: 25%); vectors 95.7% *An. gambiae* s.l. (95.6% of them
   s.s.), 3.7% *An. funestus*; net use 77% in year 1; 94% of households sprayed; one IRS
   round only (February 2015, Actellic 300CS); wall cone-bioassay mortality 99% at
   spraying, 82% at 9 months, 59% at 12 months.
2. **Churcher 2016 (eLife, PMC5025277), Table 6.** Hut-trial relationships from bioassay
   mortality x: hut mortality logit(l) = 0.63 + 4.00(x − 0.5); PBO bioassay mortality
   logit(f) = (3.41 + 5.88(x − 0.5)) / (1 + 0.78(x − 0.5)) for the *An. gambiae* complex,
   logit(f) = 2.53 + 0.89(x − 0.5) for *An. funestus*; entry relative to an untreated net
   m = 1 − [0.071 + 1.26(l − 0.5) + 1.52(l − 0.5)²] (capped at 1); fed and survived
   k = 0.02 exp(3.32(1 − l − 0.5)); k0 = 0.70; per-wash decay logit(γ) = −2.36 − 3.05(l − 0.5);
   half-life scaled to 2.64 years at full susceptibility; residual repeating r_M = 0.24;
   repeat, die and feed per attempt from Equations 12–14 and 17–19.
3. **Spike diagnostics.** With the old mechanics the interaction stays at 1.0–1.06 even
   when both single effects reach the trial's size (`spike/out/b15_diagnostic.yaml`).

## Benchmark-pack changes (step 1: feasibility before fitting)

- **Price floor for cost-per-outcome bands.** For each cost benchmark the runner computes
  the cost of the intervention arm and the outcome in the comparator arm. If even a
  perfectly effective intervention (outcome averted = all of the comparator's outcome)
  costs more per unit than the band's upper edge, the benchmark is `band_mis_based`. It is
  reported with the floor and never fitted toward.
- **Gating on main effects.** A benchmark may declare `requires: {id: [lo, hi]}`. If any
  required benchmark is outside the stated interval, the benchmark is `not_assessable`
  and the required values are reported beside it. B15 requires the trial's single-arm
  effects (PBO alone, IRS alone) inside their 95% CIs. B15's band becomes the trial's CI
  (1.19–4.97); the old ×/÷2.5 band (floor 0.97) could not tell "no interaction" from 2.43.
- **Structural feasibility from the fit design.** The joint fit (below) evaluates every
  fit target over a space-filling design of the fitted parameters' box. Before choosing
  values, the runner reports, from that design and a surrogate, each target that cannot be
  in band anywhere in the box (`infeasible_in_box`) and each pair of targets that cannot be
  in band together. Infeasible targets are dropped from the objective and reported, not
  fitted toward.
- Every benchmark row gets a `status`: `in_band`, `out_of_band`, `not_assessable` or
  `band_mis_based`, plus per-seed values and the seed spread.

## Engine mechanisms (step 2; each with an isolation test, then together)

1. **Published hut-trial coefficients for nets.** Replace the fitted maximum-kill power law
   and the PBO restoration factor by Churcher 2016's relationships (above), per species
   complex for PBO. Pyrethroid and PBO nets then have no fitted parameter. Dual-AI keeps
   one fitted chlorfenapyr kill on top of the pyrethroid base (no published coefficient in
   the priors; flagged).
2. **IRS early, strong effect with product-specific decay.** Killing of indoor-resting fed
   mosquitoes and deterrence follow a decay profile per product: for pirimiphos-methyl CS a
   logistic-in-time curve through the trial's cone-bioassay points (99%, 82%, 59%); the
   generic short-lived product keeps attempt 8's exponential (half-life 146 days,
   unverified). A share of IRS-deterred mosquitoes is diverted to outdoor biting instead
   of retrying indoors (assumption, varied in sensitivity).
3. **Household-correlated coverage.** Agents live in households (mean size 5). Net use and
   spraying are drawn from household-level latent variables with correlation
   `cov_corr` between net use and IRS, and within-household clustering of net use
   `hh_net_corr`. Net use is per person and persistent (the extreme of adherence
   heterogeneity: a non-user stays a non-user).
4. **Exposure heterogeneity correlated with net non-use.** The biting-rate multiplier keeps
   Griffin's lognormal (σ² = 1.67); its log is correlated with the net-use latent
   (`exp_corr`), so high-exposure people are more often non-users.

Correlation parameters 3–4 are assumptions with no trial target that identifies them;
they are fixed, flagged unverified, and varied in a sensitivity scan reported beside B15.
They are not fitted to B15.

## Joint fit (step 3)

- Fitted parameters: those the mechanisms leave without a published value (IRS maximum
  kill, chlorfenapyr kill; possibly IRS deterrence). One fit, all jointly.
- Design: Latin hypercube plus centre over the box, every point run on the pack's 8 seeds
  (the same seeds as the final run), with site calibration fixed at the box centre (the
  calibration arms do not contain IRS or dual-AI nets, so they do not depend on these
  parameters; the final run re-calibrates and reports any drift).
- Surrogate: quadratic in the parameters for each target's log value, held-out error
  reported. Objective: fewest feasible targets out of band, then the band-scaled squared
  log error. Range: the parameter box region where all feasible targets are in band.
- Verification: the full pack at the chosen values, 8 seeds, per-seed spread reported.
  The fit rule is versioned (`joint-v1`) in `fitted.yaml` and the engine card.
- Trial settings still may not override fitted parameters (the runner refuses).

## Reporting (step 4)

`benchmarks/results.yaml` and `ENGINE_NOTES.md`: every benchmark with status and seed
spread, the spike's values beside the new ones, B15's single-arm effects beside the
interaction, and the sensitivity scan. No interaction factor is added. If the mechanisms
cannot reach 2.43, the notes say how close they get and why.

## Out of scope

Faster prevalence response (detection and duration of patent infection), age-incidence
curves, and the decision re-run on the spike's analysis engine.
