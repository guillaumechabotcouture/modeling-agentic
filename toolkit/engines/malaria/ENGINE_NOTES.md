# Malaria engine v0.7: benchmark notes

Engine `malaria-starsim` v0.7 (workstream A of architecture v2). Design note:
`docs/design/v2a-malaria-engine.md`. Every number in the tables block is rendered from
`benchmarks/results.yaml`, `benchmarks/fitted.yaml`, `benchmarks/b15_mechanisms.yaml`,
`benchmarks/equilibration.yaml` and `benchmarks/runtime.yaml`, with the spike
(`history/results_v0.1-spike.yaml`) and the earlier rounds (`history/results_v0.2.yaml`,
`results_v0.3-r2.yaml`, `results_v0.3.yaml`, `results_v0.4.yaml`) beside them, by
`benchmarks/render_notes.py`. Reproduce with `bash toolkit/engines/malaria/benchmarks/validate_once.sh` on a VM.

**v0.7 and v0.6 numbers are v0.5's.** The v0.6 changes act only when a horizon gives a delivery period or a unit sets
`itn_routine_interval`. The v0.7 change acts only when a unit sets `smc_routine = 1`. The pack and the evidence use
none of them, so the pack's results, fit, equilibration and guess-sensitivity files keep the numbers v0.5 computed.
Their identity fields were re-stamped twice by `benchmarks/restamp_proof.py`:
- for v0.6, after the proof found v0.5 and v0.6 outputs identical;
- for v0.7, after it found v0.6 and v0.7 outputs identical.

Both proofs ran on the pack's settings and the evidence cases (`benchmarks/restamp_proof.yaml` holds the latest).
Each file's `restamped:` record holds the v0.7 re-stamp, with the v0.6 record under `previous`. A Coiled run of
`validate_once.sh` is open until it confirms them.

## v0.8 scope: the figure hooks, schedule() and run_series() (written before the v0.8 code)

**The problem.** The v2 figure set (`docs/design/v2-figures.md` §5, PR #127) has two figures the
harness cannot draw from stored results: a per-region timeline of what is delivered when, and
monthly trajectories of the calibration metric and incidence for the comparator and the plan.
PR #127 defines two optional engine hooks for them in `toolkit/engines/hooks.py`; `contract.py` is
unchanged. v0.8 implements both hooks in the malaria engine, exactly to that interface, and
changes nothing that `run` and `run_batch` return.

**Hook 1: `schedule(params, unit, package, horizon) -> list[dict]`.**
- One dict per delivery event, `{component, label, kind, t0, t1, charged}`, over
  `[horizon.start, window end)`. `charged` is true exactly when `kind` is `decision`.
- `history`: the unit's past net campaigns (`itn_hist_years`, net type `hist_net`).
- `decision`: the package's events in the delivery period: the net campaign
  (`campaign_year`), each SMC round, each IRS round, and the case-management add-on as a period
  (`t0` the delivery start, `t1` its end; the window end when there is no delivery period, where
  the add-on has no end).
- `routine`: routine net campaigns (`itn_routine_interval`) and routine SMC rounds
  (`smc_routine`), in every arm.
- Point events (campaigns and rounds) have `t1 = None`. Events the sim would run outside
  `[horizon.start, window end)` are not listed: a past campaign before `horizon.start` acts in the
  burn-in, and a round after the window's end has no outcome counted.
- **One source.** The schedule logic now lives in one pure function, `delivery_plan(a,
  components, horizon)`. It returns the package's component settings, the history, routine and
  add-on timings, and the event list. `_plan` (the package's settings for the sim) and
  `_base_sim` (the history and routine interventions and the add-on period) both read it, and
  `schedule()` returns its event list. There is no second copy of the schedule rules.
  `delivery_plan` makes every check `_plan` made before (fail closed), in the same order.
- **Pure.** No sim is built and no random number is drawn. A self-test runs `schedule()` with
  `starsim.Sim` replaced by a function that raises, and checks that numpy's global random state
  and the unit's attributes are unchanged.

**Hook 2: `outputs["series"]` and `run_series(params, unit, packages, horizon, seed, metrics, step="month")`.**
- The card declares `outputs["series"]`: `pfpr_u5` (under-5 microscopy prevalence, 6-59
  months: the calibration metric, the same definition as the window metric `pfpr_u5`; aggregate
  `mean`, units share), `pfpr_2_10` and `pfpr_all` (`mean`, share), `inc_all` (all-age clinical
  incidence; aggregate `rate`, episodes per person-year) and `inc_u5` (under-5 clinical
  incidence, `rate`, episodes per child-year).
- It returns one dict per package, in order: `{"package": id, "t": [...], "values": {metric:
  [...]}}`. Steps start at `horizon.start + k/12` and run up to the window's end; the last step
  ends at the window's end.
- **How it is recorded.** The model already records each of these quantities every day
  (`Malaria.daily`, the records the named windows read). `run_series` reads those records after
  each member's run, before they are freed. No analyzer is added and the sim is not changed. A
  `mean` value is the mean of the step's daily values. A `rate` value is the step's sum of daily
  episodes per person, divided by the step's length in years.
- **Consistency with the window metrics.** A window whose bounds lie on the step grid selects the
  same days as its steps. So its `pfpr_u5` equals the day-weighted mean of the steps' values, and
  its `inc_u5` / `inc_all` (episodes per person over the window) equal the sum of rate x step
  length. A self-test checks both to 1e-9.
- **Shared history.** `run_batch`'s member loop moves into one internal function. `run_batch`
  and `run_series` both call it, so `run_series` gets the same shared burn-in, history and fork
  (common random numbers) as `run_batch`. Its packages are therefore identical before the first
  component event.

**Byte identity.** `run` and `run_batch` build, configure and run the same sims as v0.7, and
return the same outcomes. The new card output and the code change the engine identity
(`config_sha256`, `code_sha256`, version 0.8). As for v0.6 (#123) and v0.7 (#126):
- `benchmarks/restamp_proof.py prove --old-ref origin/v2` compares v0.7 and v0.8 on the pack's
  settings and the evidence cases. It runs on a Coiled VM in the IDM default workspace, not on
  the laptop.
- The stored evidence is re-stamped with `restamp` only when all 19 cases are identical.
- Self-test S7 checks, on a toy unit with history, decision and routine events, that recording
  the series does not change the outcomes: the member loop run by `run_series` returns outcomes
  byte-identical to `run_batch`'s. The v0.7-to-v0.8 comparison itself is the proof above, since
  a self-test cannot hold the v0.7 code.

**Tests written first (α).** Engine S0–S8:
- S0: version 0.8, and `hooks.supports` is true for both hooks.
- S1: `schedule()` for a toy unit with past, decision and routine nets, package and routine SMC,
  IRS and the add-on equals the event list of the sim the engine builds (its interventions'
  years, rounds and add-on period). The comparator arm has no decision event.
- S2: `schedule()` is pure.
- S3: #127's checker (`hooks.schedule`) accepts every arm's schedule.
- S4: `run_series` shape and units: one entry per package in order, monthly steps over
  `[horizon.start, window end)`, the card's labels, units and aggregates.
- S5: the series averaged over a window equal the window metrics.
- S6: the packages' series are identical before the fork, and differ after it.
- S7: `run_batch` is unchanged by series recording.
- S8: `hooks.run_series` refuses a metric the card does not list.

## v0.7 scope: routine SMC in every arm after the delivery period (written before the v0.7 code)

**The problem.** v0.6 split the delivery period (`objective.delivery`) from the outcome window
and added routine net campaigns in every arm (`itn_routine_interval`). SMC had no routine
counterpart. A run with outcomes over 2024.0–2029.25 and delivery over 2024.0–2027.0 then stopped
SMC at the end of delivery in the plan arm only. The engine simulated what follows: children who
had been protected for three seasons lost that protection while their immunity was lower than
in the comparator, so their burden rose after 2027. That rebound was booked as a cost of SMC, and
the plan's SMC fell from 62 units to 1. In reality SMC is a routine annual programme. It goes on
after the grant whether or not the grant funded it.

**The owner's decision (2026-11-02).** After the delivery period, both arms continue the same
routine programme. Nets already do this through `itn_routine_interval`, and SMC now does the
same. Case management after delivery stays at the unit's current level (`cm_ft`, without the
GC7 add-on) in both arms, as in v0.6.

**The change (a mechanism; no correction factor).**
- **New unit attribute `smc_routine`** (0 or 1 per unit; a column or a value; default 0). Where
  it is 1, SMC rounds run every season from the end of the delivery period to the end of the
  outcome window, in every arm including the comparator. Each season uses the unit's SMC months
  (`smc_months`, or the default from the seasonality), the same months the package's SMC uses.
  A round belongs to the routine programme when its decimal time t satisfies
  `delivery end <= t < window end`.
- **Never charged.** Routine rounds fall after the delivery period, and cost drivers count the
  delivery period only (v0.6), so `smc_doses` never includes them. The routine intervention is
  also built with counting switched off, so no later change to the delivery accumulators can
  charge it.
- **Shared history.** Like routine nets, routine SMC is a unit attribute. It is part of the
  burn-in key, it is built into every arm's sim before the fork, and the fork time is still the
  first event the decision pays for.
- **Where both apply.** In a unit with `smc_routine = 1` whose package also has SMC, the
  package's rounds cover the delivery seasons (e.g. 2024–2026) and the routine rounds cover every
  later season (2027 onwards). The engine checks that the two schedules together form one
  continuous season sequence: no missing season and no round given twice. The check fails
  closed. The plan arm with SMC in delivery plus routine SMC then gives the same health
  outcomes as SMC delivered every season of the window. Only the cost drivers differ, because
  the routine seasons are not charged.
- **Fail closed.**
  - `smc_routine = 1` without `objective.delivery` is refused. Without a delivery period
    there is no "after the grant", and the package's SMC already covers the whole window.
  - A value other than 0 or 1 is refused.
  - A routine round inside the delivery period is refused, because the plan funds those
    years.

**Back-compatibility.** With `smc_routine` absent or 0, the sim is built exactly as in v0.6: the
same interventions, in the same order, with the same random draws. A self-test compares the
outcomes of runs with the attribute absent and set to 0, with and without a delivery period.
`benchmarks/restamp_proof.py prove --old-ref origin/v2` then runs the pack's settings and the
equilibration and guess-sensitivity cases under the v0.6 code and the v0.7 code, and compares
every output exactly. The identity fields of the stored evidence are re-stamped only when that
proof passes. Each re-stamped file's `restamped:` record then names the v0.6 identity it
replaced, and it keeps the v0.6 record (whose numbers came from v0.5) under `previous`.

**Tests written first (α).** Engine R0–R7:
- R0: the attribute and its default are declared.
- R1: routine rounds are scheduled in every season after delivery and in no arm's delivery
  seasons.
- R2: routine SMC acts in the comparator and in the package.
- R3: it is uncharged. Cost drivers are equal with and without it.
- R4: comparator burden after delivery is lower where `smc_routine = 1`, and identical before.
- R5: there is no rebound gap. The plan arm with SMC in delivery plus routine SMC equals SMC in
  every season of the window.
- R6: validation errors, including the schedule check against a synthetic gap, double round
  and in-delivery round.
- R7: absent and 0 give byte-identical outputs.

Proof script RP8 covers re-stamping a file that already holds a `restamped:` record.

## v0.6 scope: a delivery period distinct from the outcome window (written before the v0.6 code)

**The problem.** Up to v0.5 one window, `objective.horizon`, did two jobs: it bounded the
years in which package components are delivered (SMC rounds, IRS rounds, the enhanced
case-management add-on) and the years over which health outcomes are counted. A net
campaign pays for a net that lasts about 3 years, but its benefit was counted only until
the window ended. In the Nigeria v2 run (13 Global Fund states, 309 units, window
2024.0–2027.0), the 67 units whose campaign is at 2026.25 got credit for about 0.75 y of
a net's life against its full cost (184 units at 2025.25 got about 1.75 y; 58 at 2024.25
about 2.75 y). The optimiser's net choices follow that truncation, not the nets' effect.

**The owner's decision (2026-11-02).**
- Extend the outcome window to 2029.25, so every campaign's net is counted for its whole life.
- Deliver the funded annual components only in the funded years.
- In both the package and the comparator, model the next routine net campaign 3 years
  after each GC7 campaign when it falls inside the outcome window. Each campaign is then
  credited with about its own 3 years: the 2024.25 and 2025.25 campaigns are followed by
  routine campaigns at 2027.25 and 2028.25, and the 2026.25 campaign runs to the window
  end at 2029.25.

**The change (a mechanism; no correction factor).**
- **Delivery period.** The spec gains `objective.delivery: [t0, t1]`. It is optional and
  must lie inside `objective.horizon`. It reaches engines as the `delivery` field of a
  `DeliveryHorizon`, a subclass of the contract's `Horizon` in `toolkit/engines/horizon.py`.
  The harness builds a `DeliveryHorizon` only when the spec gives the field, and an engine
  reads it as `getattr(horizon, "delivery", None)` (`None` means the decision window).
  `contract.py` is unchanged: it is part of every engine's code fingerprint, and editing it
  would make the compartmental engine's benchmark stamps stale as well. The analysis batch writes `delivery` into a job's horizon only when the spec
  gives it, so job hashes of existing specs do not change. An engine that honours the
  field declares `supports_delivery`; the spec validator refuses `objective.delivery` for
  an engine that does not (fail closed: another engine would ignore it silently).
- **What the engine delivers.** SMC rounds and default IRS rounds run in each year of the
  delivery period. The case-management add-on applies from the delivery start and stops at
  its end. The package's net campaign defaults to the delivery start. With an explicit
  delivery period, the run fails closed when a component event (the net campaign, an SMC
  round or an IRS round, including rounds set by `irs_years`) falls outside it, or when the
  period is not inside the outcome window.
- **What is paid and what is counted.** Cost drivers (`nets_given`, `smc_doses`,
  `irs_sprayed`, `treated_all`) are counted over the delivery period. Health outcomes and
  their per-capita denominators are counted over the outcome window.
- **`treated_all` (the case-management cost driver).** It is counted over the delivery
  period: treatments given in delivery years per mean person in those years. Treatments
  after the delivery period ends are not GC7-funded, and the add-on itself has stopped
  then, so charging them would bill the plan for care it does not buy. Counting over the
  outcome window would also leak each package's later epidemiology into the incremental
  treatment cost (fewer cases after a net campaign means fewer treatments in 2027–29),
  which is a health effect, not a cost of case management. The per-capita `treated`
  outcome is still counted over the outcome window: it is a health output.
- **Routine campaigns in every arm.** A new unit attribute `itn_routine_interval` (years;
  default: none) schedules routine net campaigns at `campaign_year + k x interval` (k = 1,
  2, ...) while they fall before the outcome window's end. They run in every package,
  including the comparator, since `campaign_year` is the unit's campaign date whether or not
  a package buys nets. Their net type is the unit's `hist_net`, the type the routine
  programme distributed before (default pyr), and their nets are never charged: they are
  not part of the decision. A routine campaign inside the delivery period fails closed,
  because the plan funds those years. Like `itn_hist_years`, it acts through the shared
  history, so it is part of the burn-in key, and every arm of a unit sees the same schedule.
  `itn_hist_years` already accepts years after horizon.start: past campaigns run in all
  arms and are uncharged. The interval attribute is used instead because the spec can then
  set one value (`{value: 3}`) rather than a new derived column per unit.

**Back-compatibility.** Without `objective.delivery` and `itn_routine_interval`, a run is
byte-identical to v0.5: the delivery period is the window, the add-on has no end, and the
delivery accumulators add the same terms in the same order as the window ones. The
benchmark pack, the equilibration and guess-sensitivity evidence and the runtime script
build horizons without the new field, and their units do not set the new attribute. Two
checks prove it. Self-test D9 runs packages with and without an explicit delivery period
equal to the window and compares the outcomes exactly. `benchmarks/restamp_proof.py` runs
the pack's settings and arms under the v0.5 code (origin/v2) and the v0.6 code, and
compares every output exactly. Only after that proof are the identity fields of the stored
evidence re-stamped, and each file records that it was re-stamped by the proof, not
recomputed. A Coiled validation run confirms it afterwards; that run is open until it
reports.

**Tests written first.** Engine D1–D9: SMC, IRS and the add-on stop at the delivery end;
cost drivers are counted over delivery only; outcomes accumulate over the full window; a
routine campaign acts in both the package and the comparator and is uncharged; delivery
outside the window, a campaign outside delivery and a routine campaign inside delivery
fail closed; equal delivery is byte-identical. Spec SP-D1–SP-D3; batch
BA-D1.

## v0.5 scope: an equilibrated starting state (written before the v0.5 pack run)

**The problem.** Up to v0.4 every run started at horizon.start with immunity set by a
closed-form approximation from a guessed EIR (`init_eir = min(0.6 m0, 150)`). Immunity
decays over 30 years (IC, IV) and 10 years (IB, ID), so that start is far from the
engine's own equilibrium for adults. A diagnostic against a 60-year no-intervention
burn-in, at under-5 PfPR 5–45%, found:
- adult IC and ID at 2021 were 1.2–2 times too low, and still 0.65–0.92 of equilibrium
  inside a 2024–27 decision window;
- adult cases were overstated by 30–100%, and adult severe cases and deaths by up to 2 times;
- under-5 burden, the calibration to under-5 PfPR and DALYs averted changed by less than
  10% (no package ranking that matters changed);
- but the `treated_all` cost driver counts all ages, and it feeds a per-outcome cost item,
  so treatment costs and cases averted at all ages were biased.

**The change (no fitted factor; B7, B8 and B16c stay held out).**
- **Burn-in.** Each run starts `burnin_years` (card parameter, default 50; source: the
  equilibration evidence below) before horizon.start, with no vector control or
  chemoprevention and case management at the unit's `cm_ft`. The closed-form guess
  (`init_eir_per_m0`, 0.6) now sets only the state at the start of the burn-in.
- **Population.** The burn-in starts with n_agents exp(−0.0269 × burnin_years) people
  (births 37, deaths 11 per 1,000 per year), which grows to n_agents at horizon.start,
  in expectation, as in the diagnostic. Transmission is per person, so the smaller early
  population changes noise, not intensity (the diagnostic found the same adult immunity
  with 4,000 people from the first year).
- **Shared by packages.** `run_batch()` runs the burn-in and the unit's own history once per
  (unit, seed, burn-in parameters) and copies the sim exactly for each package at the
  first step any package's component acts (the decision start at the latest). Every
  package therefore has an identical history (common random numbers), and a package run
  alone gives the same outcomes as in a batch. Parameter sets that differ only in
  parameters that act after the fork (IRS, SMC, dual-AI chlorfenapyr kill, enhanced case
  management, severe-case and DALY accounting) share one burn-in, so the joint fit's design
  points share it too.
- **Callers batch.** The benchmark runner sends one setting's arms per seed to
  `run_batch()`, and the analysis batch sends one design point's packages per task, for an
  engine that declares `batch_shares_state`.
- **Declaration.** Every benchmark pack now declares its engine's initialisation method
  and its equilibration evidence (`initialisation: {method, evidence}`, checked by the
  runner; disease-agnostic). The malaria pack's evidence is `equilibration.yaml`; the
  compartmental pack's is its initialiser benchmarks.

**Tests written first.** Engine V1–V8 (burn-in parameter; population grows to n_agents;
identical pre-start history across packages; a package alone equals the same package in a
batch; post-fork parameters share a burn-in; independence from the starting guess; the
doubling check), runner BP53–BP57, batch BA10–BA12, equilibration Q1–Q6, renderer R2.

**Expected outcome.** Adult immunity at horizon.start rises toward equilibrium, with
fewer adult cases. Under-5 calibrations move little. Effect ratios on child outcomes should
move little, and so should B7's under-5 components. B7's all-age components (Rowland,
Sochantha) may move: with more adult immunity, adult prevalence responds less to nets, and
v0.4's gap was in exactly that age pattern. No magnitude is predicted. The cost-per-DALY
benchmarks (B17–B20) may move a little through adult DALYs; their costs do not include
treatment.

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
### Benchmark pack: spike v0.1, earlier rounds and v0.5 (this run)

| ID | Benchmark | Target (band) | v0.1 spike | v0.2 (round 1) | v0.3 round 2 | v0.3 | v0.4 | v0.5 | v0.5 seed range | v0.5 status | Role |
|---|---|---|---|---|---|---|---|---|---|---|---|
| B7 | ITN (pyrethroid) vs none, prevalence RR at 12 m (trials' own age bands) | 0.83 (0.68–0.98) | 0.735 (in band) | 0.578 (**out of band**) | 0.642 (**out of band**) | 0.595 (**out of band**) | 0.589 (**out of band**) | 0.578 | 0.538–0.6 | **out of band** | held_out |
| B8 | ITN vs none, clinical incidence RR 0-24 m (under 5) | 0.55 (0.46–0.64) | 0.578 (in band) | 0.45 (**out of band**) | 0.54 (in band) | 0.462 (in band) | 0.462 (in band) | 0.449 | 0.405–0.474 | **out of band** | held_out |
| B9 | PBO vs pyrethroid nets (no IRS), prevalence OR at 9 m | 0.37 (0.09–0.65) | 0.798 (**out of band**) | 0.506 (in band) | 0.377 (in band) | 0.36 (in band) | 0.36 (in band) | 0.359 | 0.31–0.396 | in band | held_out |
| B9b | PBO vs pyrethroid nets (no IRS), prevalence OR at 21 m | 0.4 (0.2–0.81) | 0.953 (**out of band**) | 0.691 (in band) | 0.797 (in band) | 0.662 (in band) | 0.662 (in band) | 0.67 | 0.627–0.697 | in band | held_out |
| B9c | PBO vs pyrethroid nets, prevalence aOR at 24 m (Misungwi) | 0.99 (0.67–1.45) | 0.993 (in band) | 0.75 (in band) | 0.865 (in band) | 0.705 (in band) | 0.705 (in band) | 0.7 | 0.664–0.775 | in band | held_out |
| B10 | Dual-AI vs pyrethroid nets, prevalence OR at 24 m | 0.45 (0.23–0.67) | 0.688 (**out of band**) | 0.642 (in band) | 0.667 (in band) | 0.665 (in band) | 0.665 (in band) | 0.649 | 0.587–0.694 | in band | fit |
| B11 | Dual-AI vs pyrethroid nets, incidence RR 0-24 m | 0.54 (0.38–0.7) | 0.402 (in band) | 0.501 (in band) | 0.504 (in band) | 0.511 (in band) | 0.511 (in band) | 0.501 | 0.467–0.535 | in band | fit |
| B12 | Non-pyrethroid IRS added to ITN, prevalence RR at 12 m | 0.61 (0.34–0.88) | 0.878 (in band) | 0.786 (in band) | 0.807 (in band) | 0.8 (in band) | 0.8 (in band) | 0.802 | 0.723–0.849 | in band | fit |
| B12b | Non-pyrethroid IRS added to ITN, incidence RR 0-24 m | 0.86 (0.49–1.23) | 0.746 (in band) | 0.576 (in band) | 0.59 (in band) | 0.574 (in band) | 0.574 (in band) | 0.572 | 0.553–0.598 | in band | fit |
| B12m | Pirimiphos-methyl IRS added to pyrethroid nets (Muleba), prevalence OR at 9 m | 0.33 (0.19–0.55) | n/a | 0.427 (in band) | 0.445 (in band) | 0.451 (in band) | 0.451 (in band) | 0.449 | 0.405–0.482 | in band | fit |
| B13 | SMC, in-season clinical incidence RR (3-59 m) | 0.27 (0.14–0.4) | 0.346 (in band) | 0.353 (in band) | 0.361 (in band) | 0.355 (in band) | 0.355 (in band) | 0.358 | 0.348–0.374 | in band | held_out |
| B15 | ITN x IRS interaction OR (PBO x IRS, prevalence at 9 m, 2x2 factorial) | 2.43 (1.19–4.97) | 0.982 (in band) | 1.4 (in band) | 1.44 (in band) | 1.48 (in band) | 1.48 (in band) | 1.49 | 1.29–1.72 | in band | held_out |
| B16c | EIR-prevalence curve (Beier 1999 rules: EIR > 15 -> PfPR >= 50%; EIR >= 200 -> PfPR > 80%) | rules | 4/5 rules (**out of band**) | 4/5 rules (**out of band**) | 4/5 rules (**out of band**) | 4/5 rules (**out of band**) | 4/5 rules (**out of band**) | 4/5 rules | per seed: 4/5, 4/5, 4/5, 4/5, 4/5, 4/5, 4/5, 4/5 | **out of band** | held_out |
| B17 | SMC $ per DALY averted (reference setting) | 38.4 (7.67–192) | 18.3 (in band) | 18.8 (in band) | 18.1 (in band) | 18.1 (in band) | 18.1 (in band) | 19 | 17.2–21.1 | in band | held_out |
| B18 | ITN vs none $ per DALY averted (reference setting) | 44.5 (8.9–223) | 57.3 (in band) | 14.2 (in band) | 23.2 (in band) | 19.1 (in band) | 19.1 (in band) | 19.5 | 16.9–24.6 | in band | held_out |
| B19 | IRS vs none $ per DALY averted (reference setting) | 25.2 (5.03–126) | 370 (**out of band**) | 115 (in band) | 122 (in band) | 114 (in band) | 114 (in band) | 119 | 114–132 | in band | held_out |
| B20 | Dual-AI vs pyrethroid nets, incremental $ per DALY averted (high resistance) | 19 (3.8–95) | 3.63 (**out of band**) | 4.16 (in band) | 4.33 (in band) | 4.89 (in band) | 4.89 (in band) | 4.58 | 3.87–8.27 | in band | held_out |

v0.5: 14 in band, 3 out of band, 0 not assessable, 0 band mis-based, of 17 (9 of 12 held-out in band). 298 engine runs, 8 seeds per arm (common random numbers). v0.1: 10 of 16 in band.
- B7 by outcome definition (geometric mean of these): Halbluetzel 1996 (0-59 months) 0.5589; Moyou-Somo 1995 (15 and under) 0.5828; Rowland 1996 (all ages) 0.5849; Smithuis 2013 (under 10) 0.581; Sochantha 2006 (all ages) 0.5849. Diagnostic, not judged: windows.m12.pfpr_6m14y 0.5827.
- B15 requires {'B9': [0.21, 0.65], 'B12m': [0.19, 0.55]}; computed B9 0.3594, B12m 0.4492.
- B15 arms (OR vs standard LLIN): PBO alone 0.3595, IRS alone 0.4492, both 0.2406; prevalence {'a00': 0.5674, 'a10': 0.3205, 'a01': 0.3708, 'a11': 0.24}.
- B19: the target 25.2 is below the price floor 67.7 per DALY (cost per person divided by ALL the comparator's DALYs per person): no intervention can reach the target value at the pack's price; only the band's upper part is reachable. The band's basis (price or setting) differs from the decision's.
- B19: price floor 67.7 per DALY (cost 24.8 per person over the comparator's 0.367 DALYs per person), below the band's upper edge 126; meeting the band would need 53.8% of all DALYs averted; the engine averts 56.9%.

Calibrated transmission per setting (value, grid; a value at an outer grid bound is flagged):

cochrane_itn m0 13.47 on [4, 8, 16, 32, 64]; muleba m0 127.4 on [8, 16, 32, 64, 128, 256]; misungwi m0 12.28 on [8, 16, 32, 64, 128]; benin m0 13.45 on [4, 8, 16, 32, 64]; irs_added m0 29.06 on [8, 16, 32, 64, 128]; sahel_smc m0 23.55 on [4, 8, 16, 32, 64]; ce_ref m0 17.31 on [4, 8, 16, 32, 64]; ce_res m0 17.31 on [4, 8, 16, 32, 64].

Fitted engine-level parameters (fit rule `joint-v1`, seeds [11, 12, 13, 14, 15, 16, 17, 18], 25 design points):

| Parameter | Value | Range (all feasible targets in band) | Box | Fitted to |
|---|---|---|---|---|
| cfp_kill | 0.43 | 0.41–0.58 | [0.0, 0.8] | B10, B11, B12, B12b, B12m |
| irs_kill | 0.415625 | 0.3087–0.5284 | [0.0, 0.95] | B10, B11, B12, B12b, B12m |

Feasibility before fitting: infeasible in the box: none; conflicting pairs: none; all feasible targets in band at the value: True. Surrogate leave-one-out error (log scale): B10 0.0146, B11 0.024, B12 0.0121, B12b 0.0187, B12m 0.0164.
Verification at the value (re-run, re-calibrated): B10 0.6491 (surrogate 0.6537, in), B11 0.5013 (surrogate 0.4983, in), B12 0.8017 (surrogate 0.7997, in), B12b 0.5716 (surrogate 0.5726, in), B12m 0.4492 (surrogate 0.4477, in).

### Equilibration of the starting state (burn-in 50 y; 6 seeds, 4000 agents, state at 2050.0)

Seed means at horizon.start. Doubled: twice the burn-in. Guess x0.25 / x4: the card's burn-in from a closed-form immunity guess 4x lower or higher. None: no burn-in (the v0.4 start). Criterion: within 5% for IC, ID in bands 15_30, 30p, 15p.

| m0 | PfPR 6-59 m | Quantity | Band | Burn-in | Doubled | Change | Guess x0.25 | Guess x4 | Difference | None (v0.4 start) | None / burn-in |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1.5 | 0.123 | IC | 15_30 | 10.3 | 10.6 | +2.6% | 10.6 | 10.3 | +2.8% | 4.52 | 0.439 |
| 1.5 | 0.123 | IC | 30p | 18.1 | 18.7 | +3.7% | 17.9 | 18.7 | -4.5% | 7.59 | 0.42 |
| 1.5 | 0.123 | IC | 15p | 14.7 | 15.3 | +4.0% | 14.7 | 15.1 | -2.3% | 6.2 | 0.423 |
| 1.5 | 0.123 | ID | 15_30 | 6.16 | 6.33 | +2.8% | 6.39 | 6.25 | +2.1% | 2.57 | 0.417 |
| 1.5 | 0.123 | ID | 30p | 7.69 | 7.81 | +1.5% | 7.78 | 7.71 | +0.9% | 3.17 | 0.412 |
| 1.5 | 0.123 | ID | 15p | 7.02 | 7.18 | +2.2% | 7.17 | 7.07 | +1.4% | 2.9 | 0.413 |
| 3.5 | 0.242 | IC | 15_30 | 24.4 | 24.8 | +1.4% | 24.3 | 24 | +1.5% | 10.5 | 0.429 |
| 3.5 | 0.242 | IC | 30p | 41.1 | 42.2 | +2.7% | 40.5 | 43.3 | -6.5% | 17.7 | 0.431 |
| 3.5 | 0.242 | IC | 15p | 33.8 | 34.7 | +2.9% | 33.4 | 34.8 | -4.1% | 14.4 | 0.427 |
| 3.5 | 0.242 | ID | 15_30 | 14.1 | 14.4 | +1.7% | 14.1 | 14 | +1.0% | 5.9 | 0.418 |
| 3.5 | 0.242 | ID | 30p | 17 | 16.9 | -0.4% | 17 | 17.3 | -1.9% | 7.36 | 0.434 |
| 3.5 | 0.242 | ID | 15p | 15.7 | 15.8 | +0.7% | 15.7 | 15.8 | -0.8% | 6.7 | 0.426 |
| 9.0 | 0.404 | IC | 15_30 | 51.9 | 50.9 | -1.9% | 52.2 | 51.7 | +1.0% | 26.5 | 0.51 |
| 9.0 | 0.404 | IC | 30p | 87.5 | 89.1 | +1.8% | 85 | 91.4 | -7.0% | 45.1 | 0.515 |
| 9.0 | 0.404 | IC | 15p | 72 | 72.8 | +1.2% | 70.6 | 74 | -4.6% | 36.7 | 0.51 |
| 9.0 | 0.404 | ID | 15_30 | 29 | 28.4 | -2.0% | 29 | 28.9 | +0.4% | 14.9 | 0.515 |
| 9.0 | 0.404 | ID | 30p | 34.9 | 34.7 | -0.6% | 34.1 | 34.5 | -1.0% | 18.6 | 0.534 |
| 9.0 | 0.404 | ID | 15p | 32.3 | 32 | -1.0% | 31.9 | 32 | -0.5% | 17 | 0.525 |
| 28.0 | 0.534 | IC | 15_30 | 108 | 106 | -2.1% | 109 | 110 | -0.9% | 78.9 | 0.73 |
| 28.0 | 0.534 | IC | 30p | 183 | 187 | +2.0% | 179 | 196 | -8.6% | 135 | 0.735 |
| 28.0 | 0.534 | IC | 15p | 150 | 152 | +1.3% | 149 | 159 | -6.3% | 109 | 0.727 |
| 28.0 | 0.534 | ID | 15_30 | 57.3 | 56.3 | -1.8% | 57.8 | 58.3 | -1.0% | 43.1 | 0.751 |
| 28.0 | 0.534 | ID | 30p | 69 | 69.2 | +0.3% | 68.9 | 69 | -0.1% | 53.9 | 0.781 |
| 28.0 | 0.534 | ID | 15p | 63.9 | 63.7 | -0.3% | 64 | 64.3 | -0.5% | 49 | 0.767 |

Criterion: NOT met: init_guess: m0 3.5: IC 30p differs -6.5% between the x0.25 and x4 guesses; init_guess: m0 9.0: IC 30p differs -7.0% between the x0.25 and x4 guesses; init_guess: m0 28.0: IC 30p differs -8.6% between the x0.25 and x4 guesses; init_guess: m0 28.0: IC 15p differs -6.3% between the x0.25 and x4 guesses

### Per-run cost (runtime.yaml: 4000 agents, horizon 2016.0-2027.0, 4 packages per point, 6 points, one core each)

| Start | CPU s per point | CPU s per package run | Relative to v0.4 | Wall s per point | Peak memory (MB) |
|---|---|---|---|---|---|
| v0.4 start (no burn-in, one run per package) | 150.5 | 37.6 | 1.00x | 150 | 441 |
| v0.5: one burn-in per point, packages forked (run_batch) | 209.8 | 52.4 | 1.39x | 210 | 1,832 |
| v0.5 without sharing (one burn-in per package run) | 577.7 | 144.4 | 3.84x | 578 | 1,005 |
<!-- tables:end -->

## Reading the results (v0.5: one fit and pack run at a 50-year burn-in)

**Benchmarks: 14 of 17 in band, against 15 of 17 for v0.4.** Fitted values barely moved
(cfp_kill 0.43 against 0.425; irs_kill 0.416 against 0.422). Every effect benchmark moved by
3% or less.
- **B8 left the band.** 0.449 against a floor of 0.46 (v0.4: 0.462, a marginal pass). The move
  is −2.9%, about one standard error of the log ratio (0.018 to 0.019 per round). With more
  adult immunity, fewer of the infections that nets prevent turn clinical. B8 stays held out,
  and nothing was tuned toward it.
- **B7 stays out of band:** 0.578 (v0.4: 0.589). The all-age components moved slightly away
  from Sochantha's age pattern (all ages 0.585, under 5 0.559). The gap was predicted to point
  at the human module, and an equilibrated start does not close it.
- **B16c:** 4 of 5 rules, unchanged.
- **Cost per DALY (B17–B20):** in band, within 5% of v0.4.
- **Calibrated transmission:** m0 shifted by 4.1% or less at every site. Muleba is at 127.4 (grid
  edge 256, not flagged).

**Equilibration (equilibration.yaml), m0 1.5 to 28 (under-5 PfPR about 12% to 53%).**
- **Doubling check:** after the 50-year burn-in, adult IC and ID by age band change by 4.0% or
  less when the burn-in is doubled. This meets the 5% criterion at all four levels.
- **The v0.4 start:** adult immunity was at 0.41–0.78 of equilibrium (the "None / burn-in"
  column), the same deficit the diagnostic found.
- **Starting guess, immunity:** adult IC 30+ still differs 6.5–8.6% between the x0.25 and x4
  guesses at m0 3.5, 9 and 28 (IC 15+ 6.3% at m0 28). This misses the 5% criterion as first
  written.
  - It is residual memory of the burn-in's first cohort, not a second fixed point. A 70-year
    burn-in brings the same spread to 1.8% or less, and both guesses approach the same value.
  - The card's own guess (x1) is within 4% of the doubled burn-in.
- **Starting guess, decision outputs (guess_sensitivity.yaml):** the x4 and x0.25 guesses move
  under-5 PfPR in the calibration window, all-age cases and treated_all (2024–27, no package)
  by 2.4% or less. DALYs averted by PBO nets plus enhanced case management move by 0.6% or
  less, at m0 3.5, 9 and 28 over 6 seeds.
  - These differences are within noise: the paired standard errors are 1–6%. The check
    rules out large effects but cannot resolve effects of 1–2%.
- **V7's criterion changed after the result was seen.** It was first written as adult immunity
  within 5% between the two guesses. After the coordinator's review it now asks for decision
  outputs within 3%, and the immunity spread is reported beside it but not judged. The
  burn-in stays at 50 years (owner's default, coordinator's decision). The 70-year option
  costs about 40% more burn-in time per point.

**Two defects found while validating, both fixed.**
- **Households (code review).** In v0.4, newborns never founded new households, so the mean
  household size grew with the population: about 4-fold over a 50-year burn-in. Newborns now
  found households as needed to keep the mean at hh_size (V9).
- **Starsim before 1900.** Starsim 3.3.2's births and deaths do nothing in a sim that starts
  before 1900, so a long burn-in kept its starting population. The 70-year run's 140-year arm
  showed this. Such a start now fails closed (V2c), and the evidence runs are dated 2050.

**Per-run cost and memory (runtime.yaml; one decision point; 4,000 agents; 2016–2027; four
packages; one core per task).**

| Start | CPU s per point | Relative to v0.4 | Peak memory per process |
|---|---|---|---|
| v0.4 start | 150 | 1.00x | 0.44 GB |
| v0.5 shared (`run_batch`) | 210 | 1.39x | 1.8 GB |
| v0.5 without sharing | 578 | 3.84x | 1.0 GB |

- **Wall time** equals CPU time: one core, single-threaded.
- **Where the time goes:** the burn-in, and the shared history to the decision start in
  2024, cost about 135–160 s once per point. Each package then costs about 10–19 s: 3 years
  plus restoring the snapshot.
- **Batches with more packages:** the cost per package falls as packages are added. A
  32-package design point, as in the 13-state Nigeria batch (256 points × 32 packages =
  8,192 runs), is estimated (not measured) at about 0.4–0.6 times v0.4's 32 × 37.6 s.
- **Memory:** a shared-burn-in process holds the burn-in sim, its snapshot and one member,
  about 1.8 GB at 4,000 agents. After the shrink fix, each further member adds tens of MB.
  Before it, fit tasks with 50 members reached about 8 GB and broke the process pool twice.
  - On E8-class workers (8 vCPU, 64 GiB), 8 processes per worker need about 15–25 GB.
  - The benchmark fit and run used 24 processes on 128 GiB, peaking at about 41 GB.

**The burn-in is not cached across runs.** `toolkit/analysis/batch.py` sends each design point's
packages as one task (`run_point`), so the 32 packages of a point share one burn-in. The 256
points each compute their own: each point has its own unit, m0, parameters and seed.

**B15 mechanism scan: not re-run in full for v0.5.** The compute cap ended the job after 3 of
its 14 configurations. v0.4's scan is kept in `history/b15_mechanisms_v0.4.yaml`. B15 itself
was run in the pack: 1.49, in band.

**Evidence provenance.** equilibration.yaml and guess_sensitivity.yaml were computed at commit
d5652f8. The engine code since then differs only in releasing memory between forked members,
which does not change any value: `run_batch` and `initial_state` give identical numbers, and
the preliminary and final guess-sensitivity runs agreed to every digit. results.yaml and
fitted.yaml were computed on the final code and verify against it.

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
