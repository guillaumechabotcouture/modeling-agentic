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
### Benchmark pack, before (spike v0.1) and after (v0.2)

| ID | Benchmark | Target (band) | v0.1 spike | v0.1 status | v0.2 | v0.2 seed range | v0.2 status | Role |
|---|---|---|---|---|---|---|---|---|
| B7 | ITN (pyrethroid) vs none, prevalence RR at 12 m | 0.83 (0.68–0.98) | 0.735 | in band | 0.578 | 0.551–0.597 | **out of band** | held_out |
| B8 | ITN vs none, clinical incidence RR 0-24 m (under 5) | 0.55 (0.46–0.64) | 0.578 | in band | 0.45 | 0.435–0.481 | **out of band** | held_out |
| B9 | PBO vs pyrethroid nets (no IRS), prevalence OR at 9 m | 0.37 (0.09–0.65) | 0.798 | **out of band** | 0.506 | 0.469–0.566 | in band | held_out |
| B9b | PBO vs pyrethroid nets (no IRS), prevalence OR at 21 m | 0.4 (0.2–0.81) | 0.953 | **out of band** | 0.691 | 0.663–0.716 | in band | held_out |
| B9c | PBO vs pyrethroid nets, prevalence aOR at 24 m (Misungwi) | 0.99 (0.67–1.45) | 0.993 | in band | 0.75 | 0.702–0.796 | in band | held_out |
| B10 | Dual-AI vs pyrethroid nets, prevalence OR at 24 m | 0.45 (0.23–0.67) | 0.688 | **out of band** | 0.642 | 0.574–0.68 | in band | fit |
| B11 | Dual-AI vs pyrethroid nets, incidence RR 0-24 m | 0.54 (0.38–0.7) | 0.402 | in band | 0.501 | 0.447–0.543 | in band | fit |
| B12 | Non-pyrethroid IRS added to ITN, prevalence RR at 12 m | 0.61 (0.34–0.88) | 0.878 | in band | 0.786 | 0.714–0.829 | in band | fit |
| B12b | Non-pyrethroid IRS added to ITN, incidence RR 0-24 m | 0.86 (0.49–1.23) | 0.746 | in band | 0.576 | 0.539–0.602 | in band | fit |
| B12m | Pirimiphos-methyl IRS added to pyrethroid nets (Muleba), prevalence OR at 9 m | 0.33 (0.19–0.55) | n/a | n/a | 0.427 | 0.403–0.463 | in band | fit |
| B13 | SMC, in-season clinical incidence RR (3-59 m) | 0.27 (0.14–0.4) | 0.346 | in band | 0.353 | 0.327–0.386 | in band | held_out |
| B15 | ITN x IRS interaction OR (PBO x IRS, prevalence at 9 m, 2x2 factorial) | 2.43 (1.19–4.97) | 0.982 | in band | 1.4 | 1.21–1.52 | in band | held_out |
| B16c | EIR-prevalence curve (Beier 1999 rules: EIR > 15 -> PfPR >= 50%; EIR >= 200 -> PfPR > 80%) | rules | 4/5 rules | **out of band** | 4/5 rules | per seed: 4/5, 4/5, 4/5, 4/5, 4/5, 4/5, 4/5, 4/5 | **out of band** | held_out |
| B17 | SMC $ per DALY averted (reference setting) | 38.4 (7.67–192) | 18.3 | in band | 18.8 | 17.6–20.4 | in band | held_out |
| B18 | ITN vs none $ per DALY averted (reference setting) | 44.5 (8.9–223) | 57.3 | in band | 14.2 | 12.5–17.4 | in band | held_out |
| B19 | IRS vs none $ per DALY averted (reference setting) | 25.2 (5.03–126) | 370 | **out of band** | 115 | 110–124 | in band | held_out |
| B20 | Dual-AI vs pyrethroid nets, incremental $ per DALY averted (high resistance) | 19 (3.8–95) | 3.63 | **out of band** | 4.16 | 3.23–5.65 | in band | held_out |

v0.2: 14 in band, 3 out of band, 0 not assessable, 0 band mis-based, of 17 (9 of 12 held-out in band). 296 engine runs, 8 seeds per arm (common random numbers). v0.1: 10 of 16 in band.
- B15 requires {'B9': [0.21, 0.65], 'B12m': [0.19, 0.55]}; computed B9 0.5058, B12m 0.4274.
- B15 arms (OR vs standard LLIN): PBO alone 0.5058, IRS alone 0.4274, both 0.3024; prevalence {'a00': 0.5555, 'a10': 0.3874, 'a01': 0.3482, 'a11': 0.2744}.
- B19: the target 25.2 is below the price floor 64.2 per DALY (cost per person divided by ALL the comparator's DALYs per person): no intervention can reach the target value at the pack's price; only the band's upper part is reachable. The band's basis (price or setting) differs from the decision's.
- B19: price floor 64.2 per DALY (cost 24.6 per person over the comparator's 0.383 DALYs per person), below the band's upper edge 126; meeting the band would need 51% of all DALYs averted; the engine averts 55.6%.

Fitted engine-level parameters (fit rule `joint-v1`, seeds [11, 12, 13, 14, 15, 16, 17, 18], 25 design points):

| Parameter | Value | Range (all feasible targets in band) | Box | Fitted to |
|---|---|---|---|---|
| cfp_kill | 0.39 | 0.36–0.535 | [0.0, 0.8] | B10, B11, B12, B12b, B12m |
| irs_kill | 0.409687 | 0.285–0.5344 | [0.0, 0.95] | B10, B11, B12, B12b, B12m |

Feasibility before fitting: infeasible in the box: none; conflicting pairs: none; all feasible targets in band at the value: True. Surrogate leave-one-out error (log scale): B10 0.0165, B11 0.0186, B12 0.0097, B12b 0.0143, B12m 0.0152.
Verification at the value (re-run, re-calibrated): B10 0.6425 (surrogate 0.6422, in), B11 0.5008 (surrogate 0.5, in), B12 0.7857 (surrogate 0.7913, in), B12b 0.5764 (surrogate 0.5791, in), B12m 0.4274 (surrogate 0.4313, in).

### B15 by mechanism (Muleba 2×2, 9 months; re-calibrated per row; fitted parameters fixed)

| Configuration | PBO alone OR | IRS alone OR | Both OR | Interaction OR | Seed range | Single arms in trial CI |
|---|---|---|---|---|---|---|
| Trial (Protopopoff 2018) | 0.37 | 0.33 | 0.29 | 2.43 | CI 1.19–4.97 | |
| hut coefficients only (no household, no diversion, short IRS) | 0.4624 | 0.6743 | 0.3512 | 1.127 | 1.0014–1.3857 | no |
| + IRS long-lasting product decay (alone) | 0.4624 | 0.4078 | 0.2504 | 1.328 | 1.141–1.4282 | yes |
| + IRS diversion to outdoor biting 0.5 (alone) | 0.4624 | 0.7123 | 0.3667 | 1.113 | 0.9504–1.3315 | no |
| + household net-use clustering 0.5 and net x IRS correlation 0.3 (alone) | 0.4555 | 0.6686 | 0.3447 | 1.132 | 1.0393–1.2449 | no |
| + exposure correlated with net non-use 0.2 (alone) | 0.5063 | 0.6587 | 0.3568 | 1.07 | 0.9962–1.2151 | no |
| all together (engine defaults) | 0.5058 | 0.4274 | 0.3024 | 1.399 | 1.2095–1.5153 | yes |
| all together, high correlations (0.9, 0.9, 0.6) | 0.6186 | 0.4876 | 0.3702 | 1.227 | 1.1579–1.3063 | yes |
| all together, full diversion (1.0) | 0.5058 | 0.4893 | 0.3241 | 1.309 | 1.1481–1.4876 | yes |
| all together, high correlations and full diversion | 0.6186 | 0.5355 | 0.4086 | 1.234 | 1.1758–1.2974 | yes |
<!-- tables:end -->

## Reading the results

**B15 (ITN×IRS interaction): 1.40, seed range 1.21–1.52, against the trial's 2.43
(1.19–4.97).** It is in band and assessable, because both single arms fall inside the
trial's CIs. The spike had 0.98, and its single arms were outside them. The mechanism
table shows where the gain comes from:
- the published hut coefficients alone give 1.13;
- the long-lasting IRS decay is the largest single step (1.33), and it is the only
  mechanism that brings the IRS-alone arm inside the trial's CI;
- diversion, household clustering and exposure correlation change little on their own;
- stronger correlations lower the interaction (1.23), because they weaken both single
  arms more than the combined arm.

**Why it stops short of 2.43.** The engine's combined arm matches the trial: OR 0.30
against 0.29 (prevalence 27% against 26%). The shortfall is in the single arms, which are
weaker than the trial's: PBO alone 0.51 against 0.37, IRS alone 0.43 against 0.33. With the
trial's single arms and the engine's combined arm, the interaction would be
0.30 / (0.37 × 0.33), about 2.5. So the engine reproduces the trial's redundancy (the
combination adds little), but not the size of each intervention alone at 9 months. Two
things probably limit the single arms:
- prevalence falls slowly because patent infections clear slowly in the human module
  (the spike saw the same pattern);
- at 8.8% bioassay mortality, Churcher 2016's PBO curve predicts a smaller PBO gain than
  Muleba observed.

No interaction factor was added, and B15 was not a fit target.

**B9, B9b (PBO): now in band, held out.** The spike's structural cap is gone: no fitted
net parameter limits PBO any more. Correcting the basis also matters, since the trial's
0.37 is PBO alone and not pooled over IRS arms.

**B7, B8 (nets vs none): now out of band, held out.** Nets with the published hut
coefficients at 95% susceptibility are stronger than the Cochrane pooled trials: prevalence
RR 0.58 against 0.68–0.98, incidence RR 0.45 against 0.46–0.64. The spike met these two only
because it fitted net killing to them, and that fitted value is what capped PBO (B9). The
engine now follows the hut data and reports the miss. Possible reasons: the Cochrane trials'
nets and use differ from the setting's assumed 70% use; and hut-trial efficacy may overstate
field efficacy. Adding a field-efficacy factor for nets would be a new fitted parameter.
That needs an owner decision, because it would bring back the B7/B8-versus-B9 trade-off.
B18 (ITN $/DALY, 14 against 57 in the spike) moves for the same reason and stays in band.

**B19 (IRS $/DALY): 115, in band, but only at the band's upper edge (126).** The target
($25) is below the price floor ($64 per DALY even if IRS averted every DALY), so the target
value cannot be met at the decision's IRS price. The band's basis (older, cheaper IRS
programmes) differs from the decision's cost spec. The band is now met only because the
reference setting uses the long-lasting product with annual rounds, which averts 56% of
DALYs against the 51% needed. With the short-lived product it would be out of band. The
runner now reports the price floor and the share needed for every cost benchmark, and it
marks a band `band_mis_based` when no fitted value can reach it.

**B20 (dual-AI incremental $/DALY): 4.16, in band, narrowly.** Some seeds fall below the
band's floor (seed range 3.23–5.65). **B16c** is unchanged at 4 of 5 rules: EIR 17 gives
PfPR 0.47, against at least 0.50. This sits in the human module, not in the mechanisms
changed here.

**Fit.** The joint fit (rule `joint-v1`, 25 design points × 8 seeds) found every target
feasible and no conflicting pairs. All five fit targets are in band at the chosen values,
the surrogate agrees with the verification re-run to within 1% (log scale), and
calibration did not drift between the box centre and the fitted value.

## Assumptions still unverified

- Endophily per species (not in the D6 prior).
- Household size (5) and the three correlation values (0.5, 0.3, 0.2). Nothing in the pack
  identifies them, so they are fixed, not fitted; the B15 scan above varies them.
- Share of IRS-deterred mosquitoes diverted outdoors (0.5).
- The *An. funestus* PBO row of Churcher 2016 Table 6, read as a linear form.
- The product mix of the Cochrane IRS-added-to-ITN trials (set to the short-lived product).
- Trial-site seasonality and past net campaigns.
