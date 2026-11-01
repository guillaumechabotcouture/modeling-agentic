# Malaria engine v0.4: benchmark notes

Engine `malaria-starsim` v0.4 (workstream A of architecture v2). Design note:
`docs/design/v2a-malaria-engine.md`. Every number in the tables block is rendered from
`benchmarks/results.yaml`, `benchmarks/fitted.yaml` and `benchmarks/b15_mechanisms.yaml`,
with the spike (`history/results_v0.1-spike.yaml`) and the earlier rounds (`history/results_v0.2.yaml`,
`results_v0.3-r2.yaml`, `results_v0.3.yaml`) beside them, by `benchmarks/render_notes.py`. Reproduce with
`bash toolkit/engines/malaria/benchmarks/validate_once.sh` on a VM.

## v0.4 scope (written and pushed before any v0.4 pack run)

The open gaps after v0.3 are B7 (0.595 against 0.68–0.98), B16c (4 of 5 rules), B8's marginal
pass (0.462 against a 0.46 floor) and the Muleba calibration grid edge (m0 127.6 against a
grid bound of 128). The owner's rule: explain an overshoot with a mechanism from published
data, never a fitted correction. B7, B8 and B16c stay held out. Nothing below was chosen by
its B7 result. Each candidate was weighed on the trials' own design and on published data
before the pack was run, and the pack is run once on the committed set.

### What the B7 trials measured

Pryce 2018 (Cochrane CD000363.pub3, PMC6418392) pools 6 trials for ITN vs no nets,
*P. falciparum* prevalence: RR 0.83 (0.71–0.98), 18,809 participants. Five of the six could
be identified from the review's Characteristics of included studies table:

| Trial | Setting | Ages in the prevalence survey | Unit randomised | Transmission | Net use |
|---|---|---|---|---|---|
| Halbluetzel 1996 | Burkina Faso | 0–59 months (curtains, not nets) | groups of villages | EIR 300–500 | not stated |
| Moyou-Somo 1995 | Cameroon | 15 and under | household (20:20) | EIR 10–20 | not described |
| Rowland 1996 | Pakistan | all ages | household (97:95) | low | 66% every night, 82% 3–6 nights a week |
| Smithuis 2013 | Myanmar | under 10 | village (10:10); 6.8% of the weight | not stated | not stated |
| Sochantha 2006 | Cambodia | all ages (10,726 people, 34 villages) | village | EIR 6 | 87%; 14% of controls used an ITN |

The sixth trial is not named in the accessible text. Every identified trial measured
prevalence by microscopy (blood smears), as the engine does. Sochantha 2006 (Trop Med Int
Health 11:1166) reports the age dependence directly. The all-ages prevalence ratio was 0.91
(0.65–1.28), and the under-5 OR 0.63 (0.26–1.53), with a significant age interaction
(p = 0.042). The same nets reduce child prevalence more than all-age prevalence. The pack's
B7 measures prevalence in 6 months to 14 years. Two of the five identified trials measured
all ages, and the other three used three different child age bands.

### Candidate mechanisms

| # | Candidate | Published evidence | Expected direction on B7 | Decision |
|---|---|---|---|---|
| 1 | **Age band of the prevalence outcome** | Table above; Sochantha's age interaction | up (toward 1): all-age and wider child bands respond less than 6 m–14 y | **implement** |
| 2 | Microscopy vs RDT | all identified trials used microscopy; so does the engine | none | no change needed |
| 3 | Control-arm net use | 14% in Sochantha only | up, small | not implemented: one trial, would need a setting per trial |
| 4 | Household randomisation (the community effect reaches control households) | Moyou-Somo, Rowland | up | not implemented: two small trials; needs nets allocated within one simulated population |
| 5 | Outdoor and early biting of the Asian trials' vectors (*An. dirus*, *An. minimus*, *An. culicifacies*) | trial settings (Sochantha: forest malaria) | up | not implemented: the D6 prior has no Asian bionomics, and they do not apply to the Nigeria decision. Recorded as a basis mismatch of B7 |
| 6 | Transmission intensity per trial (EIR 6 to 500) | table above | mixed | not implemented: needs a setting per trial |
| 7 | Trial nets maintained or re-treated (less attrition than LLIN campaigns) | trial descriptions (e.g. 6-monthly re-treatment in the ITN trials of that era) | down (a stronger effect) | not implemented: no trial-specific retention data. Recorded as a known risk in the opposite direction |
| 8 | S-shaped net loss curve (Bertozzi-Villa 2021 smooth-compact function) | the main text says the curve is S-shaped; its steepness is in Supplementary Note 1.4, which is not accessible | down at 12 m | not implemented: a parameter would be guessed; the exponential stays flagged |

**Committed (1): B7 is measured in each identified trial's own ages.**
- **Estimand:** B7 becomes the geometric mean of the RR in each identified trial's age band:
  - 0–59 months (Halbluetzel);
  - 15 years and under (Moyou-Somo);
  - all ages (Rowland, Sochantha: two entries);
  - under 10 years (Smithuis).
- **Engine change:** new window metrics for these bands (`pfpr_lt5`, `pfpr_le15`, `pfpr_all`, `pfpr_lt10`), all by microscopy as before.
- **Weights:** equal. The Cochrane weights are not in the accessible text (only Smithuis's 6.8% is), so equal weighting is an unverified assumption, stated in the pack.
- **Old estimand kept as a diagnostic:** the run also reports the old 6 m–14 y RR, which is not used for the status.

**B8:** unchanged (under-5 clinical incidence, 0–24 months).
- The incidence trials' ages are not all identifiable. Those that are: Henry 2005 (Côte d'Ivoire, kdr frequency about 90%) and Sochantha 2006 (an all-ages consultation rate, adjusted rate ratio 0.72).
- No mechanism is committed for B8, and its marginal pass stays marginal.

**B16c: no mechanism committed; expected to stay at 4 of 5 rules.**
- Beier 1999's full text was not accessible (HTTP 403), so the age group of its prevalence surveys and its EIR methods cannot be checked.
- One candidate was considered and set aside: reporting an adult's EIR, as a human landing catch would, instead of the population mean, which is about 0.8 times an adult's.
  - It moves every point to higher EIR, so it cannot satisfy the rule that fails.
  - Beier pooled landing and spray-catch EIRs, so the adult basis is not established.

**Muleba grid edge (fit rule).**
- **Grid:** Muleba's calibration grid extends to 256.
- **Runner flag:** the runner flags a calibrated value within 5% (in log terms) of either outer grid bound as `near_grid_edge`. Until now it was flagged only when the target lay outside the grid.

**Expected outcome:**
- B7 rises. No magnitude is predicted, and it may stay out of band.
- No other benchmark's estimand changes.
- Muleba's interpolated m0 should not move: the added grid point lies above the bracketing interval.
- The joint fit is re-run because the engine code changed.

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
- **Holes (re-derived after Codex round 2):** Briët 2020's published equations. They give
  the probability of attacking (P_att), of dying before feeding (P_Bμ) and of dying after
  feeding (P_Cμ), as logistic functions of ln(holed area + 1), insecticide content and
  their interaction (main-text equations; Table S9 coefficients).
  - Briët's outcomes per entered mosquito: repel = 1 − P_att;
    kill = P_att[P_Bμ + (1 − P_Bμ)P_Cμ]; fed = P_att(1 − P_Bμ)(1 − P_Cμ).
  - The engine adds Briët's absolute change between a holed and an intact net, at the
    insecticide the net has left (55 mg/m² × the Churcher decay), to Churcher's (repeat,
    die, feed) outcomes.
  - Not a ratio: Briët's intact-net feeding share is near zero, so a ratio multiplied
    feeding at 2 years to 96%. That compounds the two decay models and contradicts the
    paper's conclusion that holes act mainly through attrition and non-use.
  - Holed area grows to the seven-cohort median of ln(area) 3.67 at 24 months (Table 3).
  - Round 2 had applied the attack slope (0.379/0.541) directly to Churcher's feeding
    share, without the interaction term. That was wrong: the slope is for attacks, which
    include mosquitoes that die. Round 2's B7/B8 values are superseded.
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
### Benchmark pack: spike v0.1, earlier rounds and v0.4 (this run)

| ID | Benchmark | Target (band) | v0.1 spike | v0.2 (round 1) | v0.3 round 2 | v0.3 | v0.4 | v0.4 seed range | v0.4 status | Role |
|---|---|---|---|---|---|---|---|---|---|---|
| B7 | ITN (pyrethroid) vs none, prevalence RR at 12 m (trials' own age bands) | 0.83 (0.68–0.98) | 0.735 (in band) | 0.578 (**out of band**) | 0.642 (**out of band**) | 0.595 (**out of band**) | 0.589 | 0.57–0.609 | **out of band** | held_out |
| B8 | ITN vs none, clinical incidence RR 0-24 m (under 5) | 0.55 (0.46–0.64) | 0.578 (in band) | 0.45 (**out of band**) | 0.54 (in band) | 0.462 (in band) | 0.462 | 0.436–0.505 | in band | held_out |
| B9 | PBO vs pyrethroid nets (no IRS), prevalence OR at 9 m | 0.37 (0.09–0.65) | 0.798 (**out of band**) | 0.506 (in band) | 0.377 (in band) | 0.36 (in band) | 0.36 | 0.342–0.406 | in band | held_out |
| B9b | PBO vs pyrethroid nets (no IRS), prevalence OR at 21 m | 0.4 (0.2–0.81) | 0.953 (**out of band**) | 0.691 (in band) | 0.797 (in band) | 0.662 (in band) | 0.662 | 0.638–0.694 | in band | held_out |
| B9c | PBO vs pyrethroid nets, prevalence aOR at 24 m (Misungwi) | 0.99 (0.67–1.45) | 0.993 (in band) | 0.75 (in band) | 0.865 (in band) | 0.705 (in band) | 0.705 | 0.666–0.732 | in band | held_out |
| B10 | Dual-AI vs pyrethroid nets, prevalence OR at 24 m | 0.45 (0.23–0.67) | 0.688 (**out of band**) | 0.642 (in band) | 0.667 (in band) | 0.665 (in band) | 0.665 | 0.604–0.739 | in band | fit |
| B11 | Dual-AI vs pyrethroid nets, incidence RR 0-24 m | 0.54 (0.38–0.7) | 0.402 (in band) | 0.501 (in band) | 0.504 (in band) | 0.511 (in band) | 0.511 | 0.481–0.537 | in band | fit |
| B12 | Non-pyrethroid IRS added to ITN, prevalence RR at 12 m | 0.61 (0.34–0.88) | 0.878 (in band) | 0.786 (in band) | 0.807 (in band) | 0.8 (in band) | 0.8 | 0.759–0.85 | in band | fit |
| B12b | Non-pyrethroid IRS added to ITN, incidence RR 0-24 m | 0.86 (0.49–1.23) | 0.746 (in band) | 0.576 (in band) | 0.59 (in band) | 0.574 (in band) | 0.574 | 0.534–0.63 | in band | fit |
| B12m | Pirimiphos-methyl IRS added to pyrethroid nets (Muleba), prevalence OR at 9 m | 0.33 (0.19–0.55) | n/a | 0.427 (in band) | 0.445 (in band) | 0.451 (in band) | 0.451 | 0.429–0.48 | in band | fit |
| B13 | SMC, in-season clinical incidence RR (3-59 m) | 0.27 (0.14–0.4) | 0.346 (in band) | 0.353 (in band) | 0.361 (in band) | 0.355 (in band) | 0.355 | 0.344–0.366 | in band | held_out |
| B15 | ITN x IRS interaction OR (PBO x IRS, prevalence at 9 m, 2x2 factorial) | 2.43 (1.19–4.97) | 0.982 (in band) | 1.4 (in band) | 1.44 (in band) | 1.48 (in band) | 1.48 | 1.36–1.53 | in band | held_out |
| B16c | EIR-prevalence curve (Beier 1999 rules: EIR > 15 -> PfPR >= 50%; EIR >= 200 -> PfPR > 80%) | rules | 4/5 rules (**out of band**) | 4/5 rules (**out of band**) | 4/5 rules (**out of band**) | 4/5 rules (**out of band**) | 4/5 rules | per seed: 4/5, 4/5, 4/5, 4/5, 4/5, 4/5, 4/5, 4/5 | **out of band** | held_out |
| B17 | SMC $ per DALY averted (reference setting) | 38.4 (7.67–192) | 18.3 (in band) | 18.8 (in band) | 18.1 (in band) | 18.1 (in band) | 18.1 | 16.4–20.2 | in band | held_out |
| B18 | ITN vs none $ per DALY averted (reference setting) | 44.5 (8.9–223) | 57.3 (in band) | 14.2 (in band) | 23.2 (in band) | 19.1 (in band) | 19.1 | 17.3–22.3 | in band | held_out |
| B19 | IRS vs none $ per DALY averted (reference setting) | 25.2 (5.03–126) | 370 (**out of band**) | 115 (in band) | 122 (in band) | 114 (in band) | 114 | 107–118 | in band | held_out |
| B20 | Dual-AI vs pyrethroid nets, incremental $ per DALY averted (high resistance) | 19 (3.8–95) | 3.63 (**out of band**) | 4.16 (in band) | 4.33 (in band) | 4.89 (in band) | 4.89 | 4.25–5.92 | in band | held_out |

v0.4: 15 in band, 2 out of band, 0 not assessable, 0 band mis-based, of 17 (10 of 12 held-out in band). 298 engine runs, 8 seeds per arm (common random numbers). v0.1: 10 of 16 in band.
- B7 by outcome definition (geometric mean of these): Halbluetzel 1996 (0-59 months) 0.5585; Moyou-Somo 1995 (15 and under) 0.5979; Rowland 1996 (all ages) 0.6025; Smithuis 2013 (under 10) 0.5836; Sochantha 2006 (all ages) 0.6025. Diagnostic, not judged: windows.m12.pfpr_6m14y 0.5953.
- B15 requires {'B9': [0.21, 0.65], 'B12m': [0.19, 0.55]}; computed B9 0.3596, B12m 0.4513.
- B15 arms (OR vs standard LLIN): PBO alone 0.3596, IRS alone 0.4513, both 0.2396; prevalence {'a00': 0.564, 'a10': 0.3175, 'a01': 0.3686, 'a11': 0.2366}.
- B19: the target 25.2 is below the price floor 64.1 per DALY (cost per person divided by ALL the comparator's DALYs per person): no intervention can reach the target value at the pack's price; only the band's upper part is reachable. The band's basis (price or setting) differs from the decision's.
- B19: price floor 64.1 per DALY (cost 24.5 per person over the comparator's 0.383 DALYs per person), below the band's upper edge 126; meeting the band would need 50.9% of all DALYs averted; the engine averts 56.3%.

Calibrated transmission per setting (value, grid; a value at an outer grid bound is flagged):

cochrane_itn m0 13.3 on [4, 8, 16, 32, 64]; muleba m0 127.6 on [8, 16, 32, 64, 128, 256]; misungwi m0 11.94 on [8, 16, 32, 64, 128]; benin m0 13.33 on [4, 8, 16, 32, 64]; irs_added m0 28.66 on [8, 16, 32, 64, 128]; sahel_smc m0 24.55 on [4, 8, 16, 32, 64]; ce_ref m0 17.37 on [4, 8, 16, 32, 64]; ce_res m0 17.37 on [4, 8, 16, 32, 64].

Fitted engine-level parameters (fit rule `joint-v1`, seeds [11, 12, 13, 14, 15, 16, 17, 18], 25 design points):

| Parameter | Value | Range (all feasible targets in band) | Box | Fitted to |
|---|---|---|---|---|
| cfp_kill | 0.425 | 0.415–0.575 | [0.0, 0.8] | B10, B11, B12, B12b, B12m |
| irs_kill | 0.421563 | 0.3028–0.5403 | [0.0, 0.95] | B10, B11, B12, B12b, B12m |

Feasibility before fitting: infeasible in the box: none; conflicting pairs: none; all feasible targets in band at the value: True. Surrogate leave-one-out error (log scale): B10 0.0129, B11 0.0167, B12 0.0118, B12b 0.0128, B12m 0.0236.
Verification at the value (re-run, re-calibrated): B10 0.6646 (surrogate 0.6626, in), B11 0.5113 (surrogate 0.4975, in), B12 0.7997 (surrogate 0.8005, in), B12b 0.5742 (surrogate 0.5756, in), B12m 0.4513 (surrogate 0.4435, in).

### B15 by mechanism (Muleba 2×2, 9 months; re-calibrated per row; fitted parameters fixed)

| Configuration | PBO alone OR | IRS alone OR | Both OR | Interaction OR | Seed range | Single arms in trial CI |
|---|---|---|---|---|---|---|
| Trial (Protopopoff 2018) | 0.37 | 0.33 | 0.29 | 2.43 | CI 1.19–4.97 | |
| hut coefficients only (no household, no diversion, short IRS) | 0.3584 | 0.6795 | 0.2848 | 1.17 | 1.0025–1.3714 | no |
| + IRS long-lasting product decay (alone) | 0.3584 | 0.4049 | 0.2199 | 1.515 | 1.4021–1.6347 | yes |
| + IRS diversion to outdoor biting 0.5 (alone) | 0.3584 | 0.7057 | 0.3015 | 1.192 | 1.0591–1.3247 | no |
| + household net-use clustering 0.5 and net x IRS correlation 0.3 (alone) | 0.3584 | 0.6929 | 0.2832 | 1.141 | 1.0531–1.268 | no |
| + exposure correlated with net non-use 0.2 (alone) | 0.353 | 0.6653 | 0.279 | 1.188 | 1.0392–1.3688 | no |
| all together (engine defaults) | 0.3596 | 0.4513 | 0.2396 | 1.476 | 1.3581–1.5301 | yes |
| all together, high correlations (0.9, 0.9, 0.6) | 0.3623 | 0.5105 | 0.2571 | 1.39 | 1.2079–1.5718 | yes |
| all together, full diversion (1.0) | 0.3596 | 0.4997 | 0.264 | 1.469 | 1.12–1.6264 | yes |
| all together, high correlations and full diversion | 0.3623 | 0.5654 | 0.2795 | 1.364 | 1.1883–1.5794 | no |

### Field-use mechanisms, each alone and together (nets vs none, B7/B8; Muleba B9/B15)

| Configuration | B7 (0.68–0.98) | B8 (0.46–0.64) | B9 (0.09–0.65) | B15 (1.19–4.97) | B15 arms: PBO / IRS / both |
|---|---|---|---|---|---|
| hut-like nets (no attrition, full use given access, no holes) | 0.4624 [0.4193–0.4781] **out** | 0.2736 [0.2613–0.2884] **out** | 0.4092 [0.3542–0.4616] | 1.3969 [1.1976–1.5867] | 0.4092 / 0.438 / 0.2504 |
| + attrition alone (median retention 1.64 y) | 0.6034 [0.5867–0.6296] **out** | 0.4871 [0.476–0.512] | 0.3916 [0.3506–0.4214] | 1.4014 [1.3226–1.566] | 0.3916 / 0.4553 / 0.2499 |
| + use below access alone (use rate 0.871, half persistent) | 0.4391 [0.4235–0.4664] **out** | 0.2524 [0.2318–0.2729] **out** | 0.3775 [0.3413–0.4105] | 1.519 [1.3418–1.7325] | 0.3775 / 0.4373 / 0.2508 |
| + holes alone (Briet 2020) | 0.456 [0.4073–0.4779] **out** | 0.264 [0.2355–0.2882] **out** | 0.405 [0.3834–0.4381] | 1.4404 [1.3338–1.5521] | 0.405 / 0.4405 / 0.257 |
| all field mechanisms (engine defaults) | 0.5888 [0.5701–0.6088] **out** | 0.4623 [0.4359–0.5047] | 0.3596 [0.3417–0.406] | 1.4765 [1.3581–1.5301] | 0.3596 / 0.4513 / 0.2396 |
<!-- tables:end -->

## Reading the results (v0.4: one run of the committed scope)

This run passes 15 of 17 benchmarks, the same count as v0.3. B7 and B16c stay out of band.
The fitted values are unchanged (`cfp_kill` 0.425, `irs_kill` 0.4216), because no fit
target's estimand or the engine's dynamics changed. The v0.3 results are in
`history/results_v0.3.yaml`.

**B7 (held out): 0.589, seed range 0.57–0.61, against 0.68–0.98. Out of band, and slightly
lower than v0.3's 0.595.**
- **What changed:** the trials' own age bands made almost no difference.
  - The RR by band is 0.56 (0–59 months), 0.58 (under 10), 0.60 (15 and under) and 0.60
    (all ages).
  - The v0.3 definition (6 months to 14 years) gives 0.595.
  - The equal-weight mean is lower than the old value because the under-5 band (one trial)
    responds more than 6 months to 14 years does.
- **What this shows:** the engine has an age gradient in the net effect, but a far weaker
  one than the trials.
  - Engine: 0.56 in under-5s against 0.60 in all ages.
  - Sochantha 2006: 0.63 in under-5s against 0.91 in all ages.
  - So the engine cuts adult prevalence almost as much as child prevalence. The trials do
    not.
- **Where the gap now points:** the human module's prevalence response in older children
  and adults, for example how long infections last and how detectable they are at high
  immunity. It no longer points to the outcome definition.
  - This is a candidate for the next round. It is not a correction: nothing was tuned to
    B7.
  - B7's basis also differs from the engine's setting in ways the engine cannot represent
    (candidates 3–6 in the scope): Asian forest vectors, household randomisation,
    curtains at EIR 300–500.

**B8 (held out): 0.462, seed range 0.44–0.50. In band, unchanged and still marginal.**

**B16c (held out): 4 of 5 rules, unchanged.** The failing point is EIR 17.1 → PfPR₂₋₁₀
0.474, against ≥ 0.50. No mechanism was committed for it (see the scope).

**Muleba grid edge: resolved.**
- The grid now runs to 256.
- Muleba calibrates to m0 127.6. That value is interpolated between 64 and 128 and does not
  move.
- No setting is within 5% of an outer grid bound. The runner now flags any that is, as
  `near_grid_edge` in `settings_calibrated`.
- The joint fit's box: neither fitted value is at a bound.
  - `cfp_kill` 0.425 in [0, 0.8].
  - `irs_kill` 0.42 in [0, 0.95].

## Reading the results (v0.3, round 3: holes re-derived from Briët's equations)

This run passes 15 of 17 benchmarks. Round 2 also passed 15, but its holes model was wrong
and its B7/B8 values are superseded. v0.2 passed 14 with a miscopied PBO equation, and the
spike passed 10 of 16.

**B7, B8 (nets vs none, held out): B8 is in band, but only just; B7 is out.**
- **B8:** 0.46 (target 0.55, band floor 0.46). Its seed range, 0.44–0.50, straddles the
  floor, so this pass is marginal and not robust.
- **B7:** 0.60 against the 0.68 floor.
- In round 2, B8 was 0.54 and B7 0.64. Most of that improvement came from the wrong holes
  mapping, which overstated how much holes raise feeding.
- **Hut-like nets** (no attrition, full use, no holes) give B7 0.47 and B8 0.27.
- **Attrition alone** gives 0.60 and 0.49. It is still the main factor.
- **Holes alone,** with Briët's equations, now barely matter: 0.46 and 0.26. That matches
  the paper's conclusion that physical decay acts mainly through attrition and non-use.
- **Use below access alone** leaves the mean unchanged, because reported use is mapped back
  to access.
- Nothing is fitted to B7/B8. The remaining gap, mostly in prevalence (B7), is not
  explained by these mechanisms at the published values. The open candidates are the
  Cochrane setting's assumed 70% use, and the human module's prevalence response.

**B15 (ITN×IRS interaction, held out): 1.48, seed range 1.36–1.53, against the trial's
2.43 (1.19–4.97).** It is in band and assessable.
- **Arms** (OR against standard nets): PBO alone 0.36 against 0.37; IRS alone 0.45
  against 0.33; both 0.24 against 0.29.
- **The shortfall:** IRS alone is weaker than the trial, and the combination is more
  effective than in the trial, so there is less redundancy than observed. With the trial's
  single arms and the engine's combined arm, the interaction would be about 2.0.
- **By mechanism,** the long-lasting IRS decay is the main step (1.17 to 1.52).
- No interaction factor was added.

**B9 / B9b / B9c (PBO, held out):** 0.36 against the trial's 0.37 at 9 months; 0.66
against 0.40 at 21 months, so the PBO advantage still fades faster than in the trial;
0.70 against 0.99 at Misungwi.

**B19 (IRS $/DALY): 114, in band at the edge (126).** The $25 target is below the $64
price floor at the decision's IRS price.

**B16c** is unchanged at 4 of 5 rules. It sits in the human module.

**Fit:** `cfp_kill` 0.425, `irs_kill` 0.42. The verification re-run confirms every fit target
in band, with no conflicting pairs and no calibration drift.

**Open item on calibration.** Muleba's transmission now calibrates to m0 = 127.6, at the top
of its calibration grid (8–128): the standard-net arm gives 0.5505 at 128 against the 0.55
target. The value is interpolated inside the grid, not clamped. The nets are stronger now
that holes matter less, so the site needs more transmission to reach its baseline. Widen the
grid before the next pack run.

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
