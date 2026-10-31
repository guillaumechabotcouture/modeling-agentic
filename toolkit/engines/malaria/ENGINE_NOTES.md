# Malaria engine v0.3: benchmark notes

Engine `malaria-starsim` v0.3 (workstream A of architecture v2). Design note:
`docs/design/v2a-malaria-engine.md`. Every number in the tables block is rendered from
`benchmarks/results.yaml`, `benchmarks/fitted.yaml` and `benchmarks/b15_mechanisms.yaml`,
with the spike (`history/results_v0.1-spike.yaml`) and PR round 1 (`history/results_v0.2.yaml`)
beside them, by `benchmarks/render_notes.py`. Reproduce with
`bash toolkit/engines/malaria/benchmarks/validate_once.sh` on a VM.

## v0.3: review corrections and field use of nets

**Corrections from review (Codex round 1).** These change the v0.2 numbers, and v0.2's
results are superseded:
- **Churcher 2016 Equation 4 was miscopied in v0.2.** The PBO bioassay relationship is
  logit(f) = b1 + b2(x − τ)/(1 + b3(x − τ)); v0.2 divided b1 as well. At Muleba (8.8%
  permethrin mortality) PBO bioassay mortality is 0.46, not 0.81, so v0.2 overstated the
  PBO effect there. The tests now reproduce the paper's own statements: in the
  *An. gambiae* complex the PBO gain peaks at intermediate resistance, and in
  *An. funestus* PBO restores near-100% mortality. The other equations used (2, 8, 11–19)
  and every Table 6 coefficient were re-checked against the paper's MathML and table XML;
  they match.
- **A not-assessable or band-mis-based benchmark no longer counts as a pass.** Every pass
  total now follows the status.
- **The fit's feasibility claim uses the verification re-run, not the surrogate.** If the
  re-run disproves the surrogate, the claim is withdrawn and the targets are listed.

**Field use of nets (owner decision: model why field effects are lower than hut effects,
with no fitted field-efficacy factor).** Each mechanism is tested on its own, then together:
- **Attrition:** nets are lost with the LLIN median retention of 1.64 years (Bertozzi-Villa
  2021, 40 countries, IQR 1.33–2.37), used as an exponential half-life. Muleba uses its own
  trial-reported decline in use instead (77.3% in year 1, 59.2% in year 2, a 2.60-year
  half-life).
- **Use below access:** people with a net use it on 87.1% of nights on average
  (Bertozzi-Villa 2021, use among those with access). Half of the use gap is persistent
  non-use and half is night-to-night variation; that split is an assumption. Survey-reported
  use is mapped back to access at distribution, and the mapping is capped at 100% access
  and flagged when it hits the cap.
- **Holes:** the log odds of feeding through a net rise by 0.379–0.541 per unit of
  ln(holed area + 1) (Briët 2020, Table S9, susceptible and resistant strains). Holed area
  grows to the seven-cohort median of ln(area) 3.67 at 24 months (Table 3).
- **Insecticide decay in use** was already in the Churcher relationships.

None of these values is fitted to B7/B8, and B7/B8 stay held out.

## v0.2: what changed from the spike (v0.1)

Mechanisms (no interaction factor anywhere):
- **Nets from published hut-trial coefficients.** Pyrethroid and PBO nets use Churcher
  et al. 2016 (eLife, Table 6) by bioassay mortality and species complex, including the
  faster decay of insecticidal activity against resistant mosquitoes. The fitted
  maximum-kill and PBO-restoration parameters are gone. B7, B8, B9, B9b and B9c are now
  held out: no fitted parameter acts on them.
- **IRS by product.** Pirimiphos-methyl CS keeps its effect for most of a year (a decay
  curve through the Muleba trial's own wall cone bioassays: 99%, 82%, 59% at 0, 9 and 12
  months). The generic short-lived product keeps attempt 8's 146-day half-life. Half of
  the IRS-deterred mosquitoes are diverted to outdoor biting, where nets do not reach them.
- **Households.** Net use clusters within households; net use and spraying are correlated
  by household; biting rates are higher among people who do not use nets.

Benchmark pack:
- **B9 and B9b basis corrected.** The trial's 0.37 (9 months) and 0.40 (21 months) are PBO
  nets alone against standard nets (Protopopoff 2018, Table 2). The spike pooled the IRS
  arms, which mixes in the redundancy B15 measures.
- **B12m added** (fit target): IRS alone at Muleba, OR 0.33 (0.19–0.55).
- **B15** now uses the trial's 95% CI (1.19–4.97) as its band, and is only assessable when
  both single arms (PBO alone, IRS alone) fall inside the trial's CIs. The spike's ×/÷2.5
  band started at 0.97, so it could not tell "no interaction" from 2.43.
- **Muleba site facts from the paper:** permethrin mortality 8.8% (*An. gambiae* s.l.) and
  54.5% (*An. funestus*), species shares, 77% net use, 94% of households sprayed, one IRS
  round only.
- **Cost bands** are checked against a price floor, and an out-of-band cost band is
  re-checked over the whole fitted parameter range. A band that no parameter value
  consistent with the effect trials can reach at the pack's prices is `band_mis_based`.
- **One joint fit** (`joint-v1`) of the two parameters no published source sets
  (chlorfenapyr kill, IRS kill), on the same 8 seeds as the final run.

<!-- tables:start -->
### Benchmark pack: spike v0.1, v0.2 (PR round 1) and v0.3

| ID | Benchmark | Target (band) | v0.1 spike | v0.2 | v0.3 | v0.3 seed range | v0.3 status | Role |
|---|---|---|---|---|---|---|---|---|
| B7 | ITN (pyrethroid) vs none, prevalence RR at 12 m | 0.83 (0.68–0.98) | 0.735 (in band) | 0.578 (**out of band**) | 0.642 | 0.619–0.681 | **out of band** | held_out |
| B8 | ITN vs none, clinical incidence RR 0-24 m (under 5) | 0.55 (0.46–0.64) | 0.578 (in band) | 0.45 (**out of band**) | 0.54 | 0.523–0.554 | in band | held_out |
| B9 | PBO vs pyrethroid nets (no IRS), prevalence OR at 9 m | 0.37 (0.09–0.65) | 0.798 (**out of band**) | 0.506 (in band) | 0.377 | 0.36–0.403 | in band | held_out |
| B9b | PBO vs pyrethroid nets (no IRS), prevalence OR at 21 m | 0.4 (0.2–0.81) | 0.953 (**out of band**) | 0.691 (in band) | 0.797 | 0.769–0.827 | in band | held_out |
| B9c | PBO vs pyrethroid nets, prevalence aOR at 24 m (Misungwi) | 0.99 (0.67–1.45) | 0.993 (in band) | 0.75 (in band) | 0.865 | 0.841–0.887 | in band | held_out |
| B10 | Dual-AI vs pyrethroid nets, prevalence OR at 24 m | 0.45 (0.23–0.67) | 0.688 (**out of band**) | 0.642 (in band) | 0.667 | 0.614–0.696 | in band | fit |
| B11 | Dual-AI vs pyrethroid nets, incidence RR 0-24 m | 0.54 (0.38–0.7) | 0.402 (in band) | 0.501 (in band) | 0.504 | 0.465–0.544 | in band | fit |
| B12 | Non-pyrethroid IRS added to ITN, prevalence RR at 12 m | 0.61 (0.34–0.88) | 0.878 (in band) | 0.786 (in band) | 0.807 | 0.747–0.86 | in band | fit |
| B12b | Non-pyrethroid IRS added to ITN, incidence RR 0-24 m | 0.86 (0.49–1.23) | 0.746 (in band) | 0.576 (in band) | 0.59 | 0.564–0.613 | in band | fit |
| B12m | Pirimiphos-methyl IRS added to pyrethroid nets (Muleba), prevalence OR at 9 m | 0.33 (0.19–0.55) | n/a | 0.427 (in band) | 0.445 | 0.421–0.479 | in band | fit |
| B13 | SMC, in-season clinical incidence RR (3-59 m) | 0.27 (0.14–0.4) | 0.346 (in band) | 0.353 (in band) | 0.361 | 0.339–0.382 | in band | held_out |
| B15 | ITN x IRS interaction OR (PBO x IRS, prevalence at 9 m, 2x2 factorial) | 2.43 (1.19–4.97) | 0.982 (in band) | 1.4 (in band) | 1.44 | 1.29–1.61 | in band | held_out |
| B16c | EIR-prevalence curve (Beier 1999 rules: EIR > 15 -> PfPR >= 50%; EIR >= 200 -> PfPR > 80%) | rules | 4/5 rules (**out of band**) | 4/5 rules (**out of band**) | 4/5 rules | per seed: 4/5, 4/5, 4/5, 4/5, 4/5, 4/5, 4/5, 4/5 | **out of band** | held_out |
| B17 | SMC $ per DALY averted (reference setting) | 38.4 (7.67–192) | 18.3 (in band) | 18.8 (in band) | 18.1 | 16.4–20.2 | in band | held_out |
| B18 | ITN vs none $ per DALY averted (reference setting) | 44.5 (8.9–223) | 57.3 (in band) | 14.2 (in band) | 23.2 | 20.1–28.2 | in band | held_out |
| B19 | IRS vs none $ per DALY averted (reference setting) | 25.2 (5.03–126) | 370 (**out of band**) | 115 (in band) | 122 | 114–128 | in band | held_out |
| B20 | Dual-AI vs pyrethroid nets, incremental $ per DALY averted (high resistance) | 19 (3.8–95) | 3.63 (**out of band**) | 4.16 (in band) | 4.33 | 3.71–5.03 | in band | held_out |

v0.3: 15 in band, 2 out of band, 0 not assessable, 0 band mis-based, of 17 (10 of 12 held-out in band). 296 engine runs, 8 seeds per arm (common random numbers). v0.1: 10 of 16 in band.
- B15 requires {'B9': [0.21, 0.65], 'B12m': [0.19, 0.55]}; computed B9 0.3768, B12m 0.4447.
- B15 arms (OR vs standard LLIN): PBO alone 0.3768, IRS alone 0.4447, both 0.2409; prevalence {'a00': 0.5609, 'a10': 0.325, 'a01': 0.3623, 'a11': 0.2354}.
- B19: the target 25.2 is below the price floor 64.1 per DALY (cost per person divided by ALL the comparator's DALYs per person): no intervention can reach the target value at the pack's price; only the band's upper part is reachable. The band's basis (price or setting) differs from the decision's.
- B19: price floor 64.1 per DALY (cost 24.5 per person over the comparator's 0.383 DALYs per person), below the band's upper edge 126; meeting the band would need 50.9% of all DALYs averted; the engine averts 52.5%.

Fitted engine-level parameters (fit rule `joint-v1`, seeds [11, 12, 13, 14, 15, 16, 17, 18], 25 design points):

| Parameter | Value | Range (all feasible targets in band) | Box | Fitted to |
|---|---|---|---|---|
| cfp_kill | 0.395 | 0.395–0.54 | [0.0, 0.8] | B10, B11, B12, B12b, B12m |
| irs_kill | 0.38 | 0.2672–0.5047 | [0.0, 0.95] | B10, B11, B12, B12b, B12m |

Feasibility before fitting: infeasible in the box: none; conflicting pairs: none; all feasible targets in band at the value: True. Surrogate leave-one-out error (log scale): B10 0.0175, B11 0.0126, B12 0.0079, B12b 0.0114, B12m 0.0167.
Verification at the value (re-run, re-calibrated): B10 0.6667 (surrogate 0.6675, in), B11 0.5037 (surrogate 0.5007, in), B12 0.8074 (surrogate 0.7958, in), B12b 0.5904 (surrogate 0.5843, in), B12m 0.4447 (surrogate 0.4365, in).

### B15 by mechanism (Muleba 2×2, 9 months; re-calibrated per row; fitted parameters fixed)

| Configuration | PBO alone OR | IRS alone OR | Both OR | Interaction OR | Seed range | Single arms in trial CI |
|---|---|---|---|---|---|---|
| Trial (Protopopoff 2018) | 0.37 | 0.33 | 0.29 | 2.43 | CI 1.19–4.97 | |
| hut coefficients only (no household, no diversion, short IRS) | 0.3855 | 0.6868 | 0.2952 | 1.115 | 1.0093–1.1685 | no |
| + IRS long-lasting product decay (alone) | 0.3855 | 0.4045 | 0.2228 | 1.429 | 1.2815–1.582 | yes |
| + IRS diversion to outdoor biting 0.5 (alone) | 0.3855 | 0.699 | 0.3105 | 1.152 | 0.9929–1.2871 | no |
| + household net-use clustering 0.5 and net x IRS correlation 0.3 (alone) | 0.3855 | 0.6869 | 0.2946 | 1.112 | 1.0448–1.1946 | no |
| + exposure correlated with net non-use 0.2 (alone) | 0.3738 | 0.671 | 0.2922 | 1.165 | 1.045–1.2897 | no |
| all together (engine defaults) | 0.3768 | 0.4447 | 0.2409 | 1.438 | 1.2855–1.607 | yes |
| all together, high correlations (0.9, 0.9, 0.6) | 0.3792 | 0.4988 | 0.2561 | 1.354 | 1.2349–1.5112 | yes |
| all together, full diversion (1.0) | 0.3768 | 0.4981 | 0.2668 | 1.422 | 1.1619–1.5719 | yes |
| all together, high correlations and full diversion | 0.3792 | 0.5444 | 0.279 | 1.352 | 1.2694–1.4756 | yes |

### Field-use mechanisms, each alone and together (nets vs none, B7/B8; Muleba B9/B15)

| Configuration | B7 (0.68–0.98) | B8 (0.46–0.64) | B9 (0.09–0.65) | B15 (1.19–4.97) | B15 arms: PBO / IRS / both |
|---|---|---|---|---|---|
| hut-like nets (no attrition, full use given access, no holes) | 0.4654 [0.4308–0.4815] **out** | 0.2736 [0.2613–0.2884] **out** | 0.4092 [0.3542–0.4616] | 1.3644 [1.2189–1.5841] | 0.4092 / 0.4797 / 0.2679 |
| + attrition alone (median retention 1.64 y) | 0.6049 [0.5843–0.637] **out** | 0.4871 [0.476–0.512] | 0.3916 [0.3506–0.4214] | 1.3731 [1.2487–1.5942] | 0.3916 / 0.4891 / 0.263 |
| + use below access alone (use rate 0.871, half persistent) | 0.4431 [0.4292–0.4697] **out** | 0.2524 [0.2318–0.2729] **out** | 0.3775 [0.3413–0.4105] | 1.4335 [1.317–1.5368] | 0.3775 / 0.4804 / 0.2599 |
| + holes alone (Briet 2020) | 0.5103 [0.4663–0.5418] **out** | 0.3778 [0.3512–0.4082] **out** | 0.4316 [0.4116–0.4552] | 1.337 [1.2697–1.486] | 0.4316 / 0.4631 / 0.2672 |
| all field mechanisms (engine defaults) | 0.6423 [0.619–0.681] **out** | 0.5403 [0.523–0.5541] | 0.3768 [0.3602–0.4032] | 1.4379 [1.2855–1.607] | 0.3768 / 0.4447 / 0.2409 |
<!-- tables:end -->

## Reading the results (v0.3)

v0.3 passes 15 of 17 benchmarks. v0.2 passed 14, but its PBO numbers rested on the
miscopied Equation 4 and are superseded; the spike passed 10 of 16.

**B7, B8 (nets vs none, held out): B8 is now in band; B7 is still out, narrowly.**
- With all field mechanisms, B8 is 0.54 (target 0.55) and B7 is 0.64. B7's band floor is
  0.68, and its highest seed reaches it (0.681).
- Hut-like nets (no attrition, full use, no holes) give B7 0.47 and B8 0.27, far stronger
  than the trials. That is the hut-to-field gap the owner asked us to explain.
- Attrition is the largest single factor: alone it brings B8 to 0.49 and B7 to 0.60.
- Holes come second: alone, 0.38 and 0.51.
- Use below access barely changes the mean on its own. Survey-reported use is mapped back
  to access, so the average use stays the same and only who is covered on a given night
  changes.
- Nothing was fitted to B7/B8.
- The rest of B7's gap is in prevalence (B7) rather than incidence (B8). The Cochrane
  setting's 70% use at distribution is still an assumption.

**B15 (ITN×IRS interaction, held out): 1.44, seed range 1.29–1.61, against the trial's
2.43 (1.19–4.97).** It is in band and assessable.
- After the Equation 4 fix, the PBO-alone arm matches the trial: OR 0.38 against 0.37.
  This is B9, which moves from 0.51 to 0.38.
- IRS alone is weaker than the trial (0.44 against 0.33).
- The combined arm is stronger than the trial (0.24 against 0.29; prevalence 24% against
  26%).
- With the trial's single arms and the engine's combined arm, the interaction would be
  about 2.0. So the shortfall now has two parts: the IRS-alone arm is too weak, and the
  combination is more effective than in the trial, so there is less redundancy than
  observed.
- By mechanism, the long-lasting IRS decay is still the main step (1.12 to 1.43). The
  household correlations and diversion change little on their own, and stronger
  correlations lower the interaction slightly (1.35).
- No interaction factor was added, and B15 was not a fit target.

**B9b (PBO at 21 months): 0.80, in band only at its edge (0.81).** The PBO advantage fades
by 21 months in the engine, while the trial kept OR 0.40. Under resistance the published
relationships give a short insecticide half-life, and attrition adds to it. This is the
next PBO mismatch to examine.

**B19 (IRS $/DALY): 122, in band at the edge (126).** As in v0.2, the $25 target is below
the $64 price floor, so the target value cannot be met at the decision's IRS price.

**B16c** is unchanged at 4 of 5 rules. It sits in the human module.

**Fit.** The fit (rule `joint-v1`, 25 design points × 8 seeds) found every target feasible
and no conflicting pairs. The verification re-run confirms every fit target in band. The
surrogate agrees with the re-run to within 1.5% (log scale), and calibration did not drift.

## Assumptions still unverified

- Endophily per species (not in the D6 prior).
- Household size (5) and the three correlation values (0.5, 0.3, 0.2). Nothing in the pack
  identifies them, so they are fixed, not fitted; the B15 scan above varies them.
- Share of IRS-deterred mosquitoes diverted outdoors (0.5).
- The *An. funestus* PBO row of Churcher 2016 Table 6, read as a linear form.
- The product mix of the Cochrane IRS-added-to-ITN trials (set to the short-lived product).
- Trial-site seasonality and past net campaigns.
- Field use (v0.3):
  - the exponential shape of net loss (the paper's loss curve is S-shaped);
  - the split of the use gap between persistent and night-to-night non-use (0.5);
  - interpolating the holes slope by bioassay mortality;
  - growth of holed area as linear in log area, with a cap at 3 years;
  - Cochrane setting use at distribution (0.7);
  - using a continent-wide retention (1.64 years) in trial settings without their own data.
    Briët 2020's cohorts kept more of their nets: 69–98% survival at 24 months.
