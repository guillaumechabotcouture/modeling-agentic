# Architecture-v2 spike: results

Question: does "a validated, reusable engine plus analysis computed by harness code" beat
attempt 8's "write everything per run" on the same question (Nigeria GC7 malaria
allocation)? Design note: `spike/DESIGN.md`. Every number in the tables below is rendered
from artifacts by `spike/render_results.py`; none is typed by hand.

## Summary

**Short answer: the architecture works and is far cheaper; the engine is not yet better than
attempt 8's on the two benchmarks the spike was meant to fix.**

- **Architecture, cost and code (clear gain).** The whole chain (spec, engine, benchmark pack,
  batch, emulator, calibration, optimisation, uncertainty, leave-one-out, decision test) ran
  from harness code with no agent-written analysis. VM spend was about $7 (109 Coiled
  credits) and pipeline model-call spend was $0, against attempt 8's $442 plus about 5,074
  credits. Question-specific code is 78 lines plus a 108-line spec, against about 7,700
  lines written for attempt 8. Re-running the analysis takes about a minute.
- **B15 (ITN×IRS interaction) is in band in name only.** The engine gives 0.98 against a
  band whose floor is 0.97; attempt 8 gave 0.94. The two are not distinguishable at the
  seed noise (per-seed values 0.90–1.03). A diagnostic (`spike/out/b15_diagnostic.yaml`)
  shows the interaction stays at 1.0–1.06 even when the single-intervention effects are
  as strong as the trial's. The mechanics as built (shared indoor target, IRS deterrence,
  net-killed mosquitoes never resting) do not produce the trial's sub-multiplicative 2.43.
- **B19 (IRS $/DALY) is out of band: about 370, as attempt 8 (about 330).** It cannot come
  into band at the decision's own IRS cost unless IRS averts at least half of all malaria
  DALYs in the reference setting. At $10.32 per person-year, a perfectly effective IRS
  would still cost about $63/DALY; the band's upper edge is about $126.
- **Other benchmarks.** 10 of 16 in band (5 of 9 held out). Out: B9 (PBO at 9 months;
  structurally capped, see below), B9b, B10 (narrowly), B16c (narrowly: EIR 16.8 gives
  PfPR 0.47 against at least 0.50), B19, and B20 (narrowly, dual-AI looks slightly too
  cost-effective).
- **Decision.** 9.92M DALYs averted at point; median 10.05M (95% UI 9.76M–10.23M) over 64
  draws. Attempt 8's point value for the same 13 states is 10.85M, so the spike is about
  9% lower and within attempt 8's national interval. The mix differs: dual-AI nets
  everywhere a net is bought (no PBO, because the engine's PBO effect is weak), enhanced
  case management in 169 LGAs (12 in attempt 8), SMC in 85 (119), IRS in none (none), and
  30 LGAs (all of Osun) get no GF package. Osun's last campaign was in 2023, so a 2026
  campaign adds little within the window. Attempt 8 imposed a state-wide campaign rule;
  the spike did not.
- **Leave-one-archetype-out.** The worst-fold rank r is 0.39, about the same as attempt 8's
  0.35 (UNSTABLE on the skill's thresholds). Package identity moves, but the fold plans
  lose at most 0.7% of DALYs when judged with the full calibration. The instability is
  among near-equivalent packages, not a loss of value.
- **DT1** (interaction set to 2.43, or to its CI ends 1.19 and 4.97): no LGA changes package.
  IRS is never chosen, and the trial value makes IRS on top of nets worth even less. The
  test cannot move this plan.
- **Wall time and budget.** About 2 h of compute end to end: about 15 min for the pack on the
  laptop, 94 min for the batch on six VMs (about 35 min of it lost to two process-pool
  failures, now fixed), and about 1 min for the analysis. Total spend is far below the $80
  stop.

The uncertainty interval is much narrower than attempt 8's because it covers less: only
the fitted engine parameters within their benchmark ranges and a state-level calibration
error. Costs, effect-size priors beyond the pack, the DALY chain and emulator error are not
included. That is a scope difference, not a precision gain.

## What was run

1. **Engine** (`toolkit/engines/malaria/`): attempt 8's Starsim model promoted behind the
   `Engine` contract, with species-specific bionomics (D6 prior values; endophily assumed),
   ITN deflection / killing / feeding with resistance, PBO, dual-AI and decay, and IRS
   deterrence and killing of indoor-resting mosquitoes. No interaction factor.
2. **Benchmark pack** (`toolkit/engines/malaria/benchmarks/`): four engine-level parameters
   fitted once (sequential one-parameter grid fits, 4 seeds), then every benchmark run with
   8 seeds and common random numbers. B13, B15, B16c, B17–B20 and two added PBO durability
   checks (B9b, B9c) were held out of fitting. 634 engine runs for fit plus pack, on the
   laptop (12 cores).
3. **Decision spec** (`spike/decision_spec.yaml`): the 13 Global Fund states (309 LGAs,
   90.8M people), packages net type × SMC × IRS × enhanced CM (32), the attempt 8 cost
   spec in toolkit/alloc form, envelope $315.6M with the $59.3M CM floor fixed, undiscounted
   in-window DALYs vs CF1.
4. **Analysis** (`spike/analysis/`): 256 design points × 32 packages = 8,192 engine runs on
   six 8-vCPU Coiled VMs (`cloud/vm_job.py`), Gaussian-process emulators with held-out
   accuracy, calibration of each LGA's transmission to its 2021 PfPR target by emulator
   inversion, toolkit/alloc ILP, 64 uncertainty draws, leave-one-archetype-out, and decision
   test DT1.

## Process notes, stated plainly

- **The fit procedure was changed once, after its first pass.** The first fit refined each
  parameter with a parabola through the three best grid points. For the chlorfenapyr
  killing parameter this landed at 0.24, where B10 is out of band, although 0.30 puts both
  of its targets in band. The runner now interpolates each benchmark on a fine grid and
  takes the value with the fewest out-of-band targets, then the lowest loss. The pack was
  refitted once with the new rule; nothing else changed. The first fit is kept in the
  scratch log for comparison.
- **B20 was first reported as $0/DALY** because the runner charged the incremental net
  price on the difference in nets distributed (zero between two net arms). The runner
  gained a `costs_num` option (incremental price × quantity in the intervention arm) and
  the pack was re-run with the same seeds.
- Trial-site settings (species mix, resistance, seasonality, net use) are assumptions
  written from the trial descriptions. Each site's transmission is calibrated to its
  control-arm or baseline level; settings cannot override fitted parameters (the runner
  refuses).

<!-- tables:start -->
### Benchmark pack (engine fitted once; held-out rows were not used in fitting)

| ID | Benchmark | Target (band) | Spike engine | In band | Role | Attempt 8 |
|---|---|---|---|---|---|---|
| B7 | ITN (pyrethroid) vs none, prevalence RR at 12 m | 0.83 (0.68–0.98) | 0.735 | yes | fit | 0.682 (in) |
| B8 | ITN vs none, clinical incidence RR 0-24 m (under 5) | 0.55 (0.46–0.64) | 0.578 | yes | fit | 0.495 (in) |
| B9 | PBO vs pyrethroid nets, prevalence OR at 9 m (pooled over IRS arms) | 0.37 (0.09–0.65) | 0.798 | **no** | fit | 0.379 (in) |
| B9b | PBO vs pyrethroid nets, prevalence OR at 21 m (pooled over IRS arms) | 0.4 (0.2–0.81) | 0.953 | **no** | held_out | n/a |
| B9c | PBO vs pyrethroid nets, prevalence aOR at 24 m (Misungwi) | 0.99 (0.67–1.45) | 0.993 | yes | held_out | n/a |
| B10 | Dual-AI vs pyrethroid nets, prevalence OR at 24 m | 0.45 (0.23–0.67) | 0.688 | **no** | fit | 0.597 (in) |
| B11 | Dual-AI vs pyrethroid nets, incidence RR 0-24 m | 0.54 (0.38–0.7) | 0.402 | yes | fit | 0.448 (in) |
| B12 | Non-pyrethroid IRS added to ITN, prevalence RR at 12 m | 0.61 (0.34–0.88) | 0.878 | yes | fit | 0.861 (in) |
| B12b | Non-pyrethroid IRS added to ITN, incidence RR 0-24 m | 0.86 (0.49–1.23) | 0.746 | yes | fit | 0.613 (in) |
| B13 | SMC, in-season clinical incidence RR (3-59 m) | 0.27 (0.14–0.4) | 0.346 | yes | held_out | 0.349 (in) |
| B15 | ITN x IRS interaction OR (PBO x IRS, prevalence at 9 m, 2x2 factorial) | 2.43 (0.972–6.08) | 0.982 | yes | held_out | 0.945 (out) |
| B16c | EIR-prevalence curve (Beier 1999 rules: EIR > 15 -> PfPR >= 50%; EIR >= 200 -> PfPR > 80%) | rules | 4/5 rules met; points (EIR, PfPR2-10): (1.036, 0.10), (4.333, 0.25), (16.8, 0.47), (52.602, 0.64), (175.755, 0.78), (524.759, 0.85) | **no** | held_out | n/a |
| B17 | SMC $ per DALY averted (reference setting) | 38.4 (7.67–192) | 18.3 | yes | held_out | 38.6 (in) |
| B18 | ITN vs none $ per DALY averted (reference setting) | 44.5 (8.9–223) | 57.3 | yes | held_out | 46.7 (in) |
| B19 | IRS vs none $ per DALY averted (reference setting) | 25.2 (5.03–126) | 370 | **no** | held_out | 327 (out) |
| B20 | Dual-AI vs pyrethroid nets, incremental $ per DALY averted (high resistance) | 19 (3.8–95) | 3.63 | **no** | held_out | 6.87 (in) |

In band: 10 of 16 (held out: 5 of 9). 296 engine runs, 8 seeds per arm (common random numbers).

Fitted engine-level parameters (toolkit/engines/malaria/benchmarks/fitted.yaml):

| Parameter | Value | Range used for uncertainty | Fitted to | All its targets in band |
|---|---|---|---|---|
| itn_kill_max | 0.2228 | 0.1762–0.2835 | B7, B8 | yes |
| pbo_restore | 1.0 | 0.2863–1.0 | B9 | no |
| cfp_kill | 0.29 | 0.29–0.3162 | B10, B11 | yes |
| irs_kill | 0.14 | 0.127–0.404 | B12, B12b | yes |

### Decision results (13 GF states, 309 LGAs)

| Quantity | Spike (v2) | Attempt 8 |
|---|---|---|
| DALYs averted 2024–2026 vs CF1, point | 9.92M | 10.85M (same 13 states) |
| DALYs averted, median (95% UI), fixed plan over 64 draws | 10.05M (9.76M–10.23M) | national, all 774 LGAs: 10.66M (6.77M–15.45M); GF states are 98.6% of its point value |
| Deaths averted, point | 0.12M | n/a for the subset |
| Cost allocated (excl. CM floor) | $256.25M of $256.25M | $240.9M + $15M reserve |
| PBO nets (LGAs) | 0 | 174 |
| Dual-AI nets | 239 | 135 |
| Pyrethroid nets | 0 | 0 |
| SMC | 85 | 119 |
| IRS | 0 | 0 |
| Enhanced CM | 169 | 12 |
| No GF package | 30 | 0 (state-wide campaign rule imposed) |
| Leave-one-archetype-out worst-fold rank r | 0.389 (15 folds with ≥5 LGAs; verdict UNSTABLE) | 0.346 (UNSTABLE) |
| LOO package concordance, mean / worst fold | 71.5% / 0% | 86.7% / 30.8% |
| LOO package concordance, worst fold with ≥5 LGAs | 18.8% | n/a |
| LOO DALY gap in the held-out LGAs, mean / worst fold (skill metric) | -36.3% / 28.9% | 4.0% / 35.9% (cases) |
| LOO DALYs lost over all LGAs (fold plan judged with full calibration), mean / worst | -0.022% / 0.726% | n/a |
| Max calibrated annual EIR (B16: 120, band 40–200) | 91.6 | 106–168 |
| Units clamped at the calibration range | 0 of 309 | n/a |
| Emulator held-out accuracy | reduction R² median 0.886, 10th pct 0.517; calibration logit RMSE 0.0919 | hand-built emulators, validated per round |
| Plan stability over draws | 153 of 309 LGAs keep their package in ≥90% of re-optimised draws | n/a |

Decision test DT1: Does the plan change when the ITN x IRS interaction equals the trial value (and its CI ends)?

Engine interaction OR in the trial setting: 0.982.

| Interaction set to | IRS-on-net increment scale | LGAs changing package | LGAs with IRS (point plan) |
|---|---|---|---|
| 2.43 | 0 | 0 | 0 (0) |
| 1.19 | 0 | 0 | 0 (0) |
| 4.97 | 0 | 0 | 0 (0) |

### Code

| Part | Non-blank lines |
|---|---|
| Reusable engine (toolkit/engines: contract, benchmark runner, malaria engine) | 1,284 |
| Engine benchmark pack (YAML) | 169 |
| Reusable analysis engine (spike/analysis) | 920 |
| Question-specific code (spike/units.py) | 78 |
| Question-specific spec (spike/decision_spec.yaml) | 108 |
| Attempt 8: per-run code, models/*.py (42 files; 5,850 lines with blanks) | 5,316 |
| Attempt 8: per-run driver scripts at the run root (12 files) | 2,426 |

### Spend and time

| Item | Spike | Attempt 8 |
|---|---|---|
| Coiled credits (clusters spike-v2-*) | 109.25 (12 VMs) | 5,074 |
| Estimated VM cost at 0.061 $/credit | $6.66 | about $310 at the same rate |
| Pipeline model-call spend (critics, writer) | $0.00 | $442 |
| Batch wall time | 94.0 min | 44 h total run |
<!-- tables:end -->

## B15 and B19, in detail

**B15.** The trial (Protopopoff 2018, Muleba) found PBO OR 0.37 and IRS OR 0.33 at 9
months, with interaction OR 2.43. In the engine's Muleba setting, PBO vs pyrethroid is OR
0.81 and IRS added is OR 0.87, and the interaction is 0.98. Raising IRS killing to 0.7 and
ITN killing to 0.6 (outside their fitted ranges) brings the single effects to the trial's
neighbourhood (ORs 0.53 and 0.51), but the interaction moves only to 1.06. On the
prevalence-odds scale the engine combines the two interventions nearly multiplicatively.
The trial's redundancy probably needs mechanisms the engine lacks:
- household-level correlation of net use and spraying;
- a stronger IRS effect in the first months, which the trial's 9-month timing would pick up;
- heterogeneity in exposure that both interventions remove from the same people.
Attempt 8's B15 was computed differently (a policy-grid cell, incidence-based), so the two
values are not like for like either.

**B9 (PBO), the main structural miss.** B7/B8 (ITN vs none) set the maximum killing of a
pyrethroid net against susceptible mosquitoes at about 0.22. PBO can at most restore that
killing, which caps the PBO-vs-pyrethroid OR near 0.79 even at full restoration. So the
pack's targets are mutually inconsistent under this structure. Attempt 8 passed B9 only by
switching in a separate "trial-consistent PBO variant". The engine also responds less on
prevalence than the trials while matching or exceeding them on incidence (B7 vs B8, B10 vs
B11, B12 vs B12b). Its prevalence dynamics look too slow over 9–24 months.

**B19.** The reference setting has 0.39 malaria DALYs per person over 3 years without
interventions. Charged at the decision's $10.32 per person-year (80% coverage), IRS costs
$24.65 per person, so the floor is about $63/DALY even at 100% efficacy. The engine's IRS
(fitted to Cochrane IRS-added-to-ITN, where its effect is modest) averts 17% and lands at
about $370. The band's $25 central value (Conteh 2021) reflects older, cheaper IRS
programmes. Either the B19 band should be re-based on the cost spec the decision uses, or
it should become an effect benchmark (DALYs averted per person protected) independent of
price.

## What to change in the v2 design

1. **Check a benchmark pack for feasibility before fitting.** Under the engine's structure
   some targets cannot be met together (B7/B8 with B9), and B19 cannot be met at the
   decision's price. A cheap pre-check should flag both before any per-question work:
   the pack's own bounds and the price floor. Each miss then goes to the owner as a known
   limit instead of being tuned around.
2. **Gate interaction benchmarks on their main effects.** B15 is only informative when the
   single arms match the trial. The pack should report a10 and a01 beside the interaction,
   and mark the interaction "not assessable" when they miss. The band (×/÷2.5) is too wide
   on the low side to separate "no interaction" from the trial value.
3. **Engine work, in order of decision value:**
   - PBO and resistance mapping with published hut-trial coefficients (Churcher 2016 /
     Sherrard-Smith 2022) in place of the fitted maximum;
   - faster prevalence response (detection and duration of patent infection);
   - correlated household coverage, as the mechanism for ITN×IRS redundancy.
   Each change re-runs the pack, about 15 minutes on a laptop.
4. **Fit jointly, with seed noise in the objective.** Sequential one-parameter fits on 4
   seeds left B10 narrowly out at 8 seeds. The fit should use the same seeds as the final
   pack run. Every change to the fit rule should be a new engine version with its diff
   recorded, as here.
5. **Measure allocation stability as value lost, not package identity.** Rank r and
   package concordance flag near-ties as instability. Report "DALYs lost when one
   archetype's data is withheld" (≤0.7% here) beside rank r. The gate's threshold belongs
   on value lost.
6. **Declare the uncertainty scope in the spec and render it in the report.** The spike's
   interval is narrow because it covers two sources. The spec should list the sources
   (engine parameters, calibration, costs, effect-size priors, emulator error), and the
   analysis engine should refuse to label an interval "95% UI" without that list.
7. **Make the compute service robust to long batches.** The process pool broke at about 750
   runs per VM (worker memory), and `max_tasks_per_child` then deadlocked. A fresh pool
   per chunk fixed both. That belongs in `scripts/compute.py`. Sharding over `vm_job.py`
   worked, and the results survived three restarts.
8. **Decision tests need a relevance check.** DT1 could not move a plan with no IRS in it.
   The analysis should say so up front ("component absent from the plan and from every
   draw's plan"), not report "no change".
9. **Evidence.** The trial-site settings (species mix, resistance, net use) and the
   endophily values are unverified assumptions. They are the first entries the evidence
   service (§3.2) should check, because B9, B10 and B15 depend on them.
10. **Data-mapping and decision-layer choices still drive the plan:** campaign timing
    classes, the state-wide campaign rule, and the severe-case cost of enhanced case
    management. The spike leaves out attempt 8's severe-case cost item ($90 per severe
    case newly reaching care), and that favours enhanced case management. These choices
    belong in the signed spec as explicit owner decisions.

Open items:
- an LLM critic and writer pass on the computed facts (not run; $0 spent);
- a second engine version with items 3a–3b, re-running the pack;
- the PR is a draft for review, not merged.
