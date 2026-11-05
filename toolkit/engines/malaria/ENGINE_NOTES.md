# Malaria engine v0.10: benchmark notes

Engine `malaria-starsim` v0.10 (workstream A of architecture v2). Design note:
`docs/design/v2a-malaria-engine.md`. Every number in the tables block is rendered from
`benchmarks/results.yaml`, `benchmarks/fitted.yaml`, `benchmarks/b15_mechanisms.yaml`,
`benchmarks/equilibration.yaml` and `benchmarks/runtime.yaml`, with the spike
(`history/results_v0.1-spike.yaml`) and the earlier rounds (`history/results_v0.2.yaml`,
`results_v0.3-r2.yaml`, `results_v0.3.yaml`, `results_v0.4.yaml`, `results_v0.8.yaml`) beside them, by
`benchmarks/render_notes.py`. Reproduce with `bash toolkit/engines/malaria/benchmarks/validate_once.sh` on a VM.

**v0.10 numbers are v0.9's.** v0.10 adds past SMC and a net type per past net campaign as unit attributes (section below); they act
only when a unit sets them, and the pack and the evidence set none. So the results, fit, equilibration and guess-sensitivity files keep the numbers
v0.9 computed, and their identity fields were re-stamped by
`benchmarks/restamp_proof.py` after it found v0.9 (d24d28d) and v0.10 (2460326) outputs identical in 23 of 23 cases: the
14 pack settings with all arms at seed 11, the 5 equilibration and guess-sensitivity cases, and 4 pinned scenarios,
including `empty_history_v010`, which sets the new attributes at their empty values. The proof ran locally (Python
3.14.3, NumPy 2.4.4, Starsim 3.3.2, macOS arm64, run `local-v010-3`). Each file's `restamped:` record says
`confirmation: {validate_once: pending}` until a `validate_once.sh` run reports. The v0.9 paragraphs below describe
the v0.9 recompute, whose numbers these are.

**v0.9 numbers are recomputed.** v0.9 changes net attrition to the smooth-compact survival curve (section below),
so every evidence file was recomputed by `bash toolkit/engines/malaria/benchmarks/validate_once.sh 16 12`. The run
used one Coiled VM (16 vCPU, 64 GiB; cluster 2324123, run `run1`, workspace `idm-coiled-idmad-r2`) at commit 811c807.
It recomputed the equilibration, guess sensitivity, joint fit (1,748 sims), pack run (298 sims), B15 mechanism scan
(14 configurations) and per-run cost. The same 14 of 17 benchmarks are in band, and the same 9 of 12 held-out ones.
B7 moved from 0.578 to 0.501, B8 from 0.449 to 0.410, and B16c stayed at 4 of 5 rules, all out of band as before.
The joint fit moved to cfp_kill 0.405 and irs_kill 0.433. The equilibration values are unchanged: the burn-in has no
nets. The v0.5–v0.8 column below is `history/results_v0.8.yaml`.

**Engine self-test V7 failed on the 6-seed guess-sensitivity evidence, and passes at 24 seeds.** V7 asks that four decision
outputs move by at most 3% between the x0.25 and x4 burn-in starting guesses, at every level. Two outputs miss at
v0.9: treated_all at m0 3.5 (−3.2%, paired seed SE 1.0%) and DALYs averted at m0 9.0 (+7.1%, paired seed SE 4.9%).
At v0.8 every output was within 3%, with paired SEs up to 6.3%. Six seeds cannot resolve a 3% tolerance for these
outputs, so the result does not show whether the net change matters for burn-in convergence. Nothing was re-run or
re-tuned to make V7 pass.

*Extension declared before running it (review round 2):* `guess_sensitivity.py` goes from 6 to 24 seeds. The 3%
tolerance, the levels, the guesses and the outputs stay the same. 24 seeds about halve the paired SE. The result is
reported whether it passes or not, and nothing else is changed to make it pass.

*Result (Coiled cluster 2325371, run `gs24`, 19.3 credits):* every output is within 3% at every level, and V7
passes. The largest moves between the x0.25 and x4 guesses are:
- DALYs averted: +2.3% at m0 3.5 (paired SE 1.5%), +2.2% at m0 9.0 (SE 2.3%), −1.4% at m0 28 (SE 1.7%);
- PfPR 6–59 m: +1.8% at m0 9.0 (SE 2.1%);
- treated_all: −0.8% at m0 3.5 (SE 1.2%).

The 24-seed point estimates meet the unchanged V7 criterion. Monte Carlo uncertainty remains, and some intervals still reach past 3%, so a stronger convergence claim would need a criterion that accounts for uncertainty. `guess_sensitivity.yaml` now holds the 24-seed run. The paragraph that follows describes the v0.6–v0.8
re-stamps; those numbers are now history.

**v0.8, v0.7 and v0.6 numbers are v0.5's.** The v0.6 changes act only when a horizon gives a delivery period or a unit
sets `itn_routine_interval`. The v0.7 change acts only when a unit sets `smc_routine = 1`. The v0.8 change adds two
figure hooks (`schedule`, `run_series`) and leaves `run` and `run_batch` unchanged. The pack and the evidence use none
of these, so the pack's results, fit, equilibration and guess-sensitivity files keep the numbers v0.5 computed.
Their identity fields were re-stamped three times by `benchmarks/restamp_proof.py`:
- for v0.6, after the proof found v0.5 and v0.6 outputs identical;
- for v0.7, after it found v0.6 and v0.7 outputs identical;
- for v0.8, after it found v0.7 (bf24d1b) and v0.8 outputs identical in 22 of 22 cases (run on a Coiled VM in
  the IDM default workspace).

All three proofs ran on the pack's settings and the evidence cases; the v0.8 proof also ran three pinned scenarios
with a delivery period, routine nets and SMC, boundary dates and heterogeneous batches
(`benchmarks/restamp_proof.yaml` holds the latest).
Each file's `restamped:` record holds the v0.8 re-stamp, with the v0.7 and v0.6 records under `previous`. The
confirming Coiled run of `validate_once.sh` has not run yet: each v0.8 record says `confirmation: {validate_once:
pending}` until it reports.

## v0.11 scope: SMC mechanism from the literature (owner approval 2026-11-05; written before the v0.11 code)

**Why.** In the F5 Nigeria run, all 57 local government areas (LGAs) that calibrated to the top of the transmission
range (log m0 = 4.6) had past SMC. Their 2021 survey window (Sep–Dec 2021) overlaps the SMC rounds. In v0.10:
- every child reached was cleared and then protected against infection at a flat `smc_pe` = 0.882 for exactly 28
  days. That number is the ACCESS-SMC protective effectiveness against *clinical malaria* (0–28 days, case-control);
- the per-round reach came from two access groups. `smc_hi_frac` = 0.85 of children were reached with probability
  0.95 per round and the rest with 0.5, for a mean of 0.88 per round. The 0.85 had no source.

v0.11 replaces both with values that each have a published source and location. No value below was chosen by
fitting to the Nigeria calibration targets or to any benchmark. B13 and B17 (the pack's two SMC benchmarks) stay
held out.

### What the literature says

| Quantity | Source | Location | Value (quote or number) |
|---|---|---|---|
| Clinical malaria, SMC vs control | Meremikwu et al. 2012, Cochrane CD003756.pub4 (PMC6532713) | Abstract, Main results (full text not accessible) | rate ratio 0.26 (95% CI 0.17–0.38), six trials, 9,321 participants |
| Clinical malaria, 0–28 and 29–42 days after a dose | ACCESS-SMC Partnership 2020, Lancet 396:1829 (PMC7718580) | Results; Fig. 2A | "88·2% (95% CI 78·7–93·4)" within 28 days; "61·4% (47·4–71·8)" at 29–42 days |
| Same, Nigeria 2016 | Cairns et al. 2021, PLoS Med 18:e1003727 (PMC8457484) | Table 2 | 83.1% (69.1–90.8) at ≤28 days; 63.7% (12.7–84.9) at 29–42 days |
| Infection during the season, and about 6 weeks after the last dose (Mali, SP+AQ vs placebo, all children with LLINs) | Dicko et al. 2011, PLoS Med 8:e1000407 (PMC3032550) | Results, malaria infection | weekly surveys 1.9% vs 13.2% ("protective efficacy of 85%", 73–92); end of season 7.2% vs 13.2% (46%); clinical malaria PE 82% (78–85), Table 2 |
| Same (Burkina Faso) | Konaté et al. 2011, PLoS Med 8:e1000408 (PMC3032552) | Table 4; Table 2 | weekly surveys 18.6% vs 45.8% (RR 0.40); 6 weeks after the last course 11.4% vs 41.5% (RR 0.27); clinical malaria PE 70% (66–74) |
| Mechanism | Konaté et al. 2011 | Discussion | "IPTc clears existing infections and has a prophylactic effect preventing new blood stage infections" |
| Protection by days since a dose | Cairns et al. 2022, BMC Med 20:352 (PMC9540742) | Abstract; Results (Fig. 6) | "exceeded 90% in the first 2–3 weeks … but was not 100%, even immediately post-administration … declines more sharply after day 28"; "completely lost … at around 45 days … (at about 38 days) using Smoothed Cox Residuals" |
| Protection with the day-1 dose only | Cairns et al. 2022 | Results (paragraph before Fig. 4) | 30-day PE 79.8% (33.7–93.8) with SP+AQ on day 1 only, against 87.7% with all three daily doses |
| Decay shape against infection | Burgert et al. 2022, PLOS Glob Public Health 2:e0000211 (PMC10021282) | Table 1; Methods, Eq 1 | SMC-SP+AQ "Initial efficacy: 100% / Half-life: 32 days / Decay shape: Weibull, k= 5.4", E(t) = E0 exp(−(t/L)^k ln2), applied as "pre-erythrocytic protection from infection"; prevalent blood-stage infections cleared at dosing ("The same initial blood-stage clearance was applied for SMC-SP+AQ") |
| Same, independent refit | Braunack-Mayer et al. 2024, Lancet Glob Health 12:e478 (PMC10882206) | Table 1; Results | Weibull shape k = 5·34; a liver-stage protection half-life of "24–30 days" behaves like SP+AQ |
| Per-round reach and its heterogeneity, Nigeria 2020 (Bauchi, Jigawa, Kano, Katsina, Kebbi, Sokoto, Yobe) | Malaria Consortium 2021, "Quantitative report on SMC supported by Malaria Consortium in 2020: coverage and quality in Burkina Faso, Chad, Nigeria, and Togo" | Table 21 (end-of-round survey, n = 7,898, weighted) | cycles received with day-1 SPAQ: none 6.0%, one 5.2%, two 13.3%, three 15.3%, four 60.2% |
| Full three-day course among day-1 recipients, Nigeria 2020 | same report | Table 16 | 94.4% (93.2–95.1), end-of-round, cycle 4 |
| Coverage range, other years | ACCESS-SMC 2020 (Table 1); Malaria Consortium 2022 and 2023 reports (Table 0) | as named | mean coverage per month, Nigeria: 83.0% (2015), 52.1% (2016); day-1 coverage per cycle 93–96% (2022, 2023, end-of-cycle) |
| Full course, range | ACCESS-SMC Nigeria 2015 key indicators; Malaria Consortium 2021 report, Table 18 | as named | 84.2%; 98.6% |

What this says for the engine:
- **During the season, the trials measured almost the same effect on infection as on clinical malaria.** Dicko
  reports 85% against infection and 82% against clinical malaria; Konaté reports RR 0.40 and 0.30. So a strong
  in-season effect on infection is supported, and v0.11 does **not** weaken it with a separate factor. This
  supports a modelling choice; it does not prove that no separate clinical mechanism exists.
- **Protection is not a 28-day step.** It stays above 90% for about three weeks, falls after day 28 and is gone by
  about day 38–45. The Weibull curve of Burgert 2022 gives 0.93 at day 21, 0.71 at day 28, 0.61 at day 30, 0.17 at
  day 38 and 0.01 at day 45.
- **Coverage is heterogeneous.** In 2020, 6.0% of Nigerian children got no cycle and 60.2% got all four. If each
  round were independent at the same mean (0.796 per round), only 0.2% would get none and 40% all four.

### Mechanism (v0.11)

1. **Who is reached (replaces the access groups).** Each child aged 3–59 months has a persistent propensity
   π ~ Beta with mean `smc_cov` and intra-child correlation `smc_rho`. It is drawn once from the child's uniform
   latent `prop_smc`, as before. Each round reaches the child with probability π. The number of rounds reached in
   a season is then beta-binomial. Fitted to Table 21 by maximum likelihood, it gives mean 0.796 and correlation
   0.417, and predicts none 5.5%, one 7.4%, two 10.3%, three 16.6%, four 60.1% against the observed 6.0 / 5.2 / 13.3 /
   15.3 / 60.2. The fit is to the coverage survey only, not to any prevalence or benchmark. `smc_rho` = 0 gives
   independent rounds.
2. **Adherence.** A reached child completes the three-day course with probability `smc_adh` = 0.944 (Table 16).
3. **Treatment at dosing (kept).** Every reached child's blood-stage infection is cleared, as in v0.10 and as in
   Burgert 2022's model. No source in hand gives a clearance probability below 1 for SP+AQ. This is named as an
   assumption.
4. **Prophylaxis against new infection (replaces the 28-day step).** From the day of a dose t0, a new infection is
   blocked with probability E(t) = E0 exp(−((t − t0)/L)^k ln2). The values are E0 = `smc_e0` = 1, L = `smc_half` =
   32 days and k = `smc_k` = 5.4 (Burgert 2022, Table 1). A child who took only the day-1 dose gets
   E0 × `smc_partial`, where `smc_partial` = 0.910 = 79.8 / 87.7 is the ratio of Cairns 2022's two 30-day PEs
   against clinical malaria. Applying that ratio to the initial infection-blocking efficacy, with the same decay and
   clearance, is an **assumption** (Codex review round 1), so the parameter is marked unverified; its CI is wide
   (33.7–93.8%) because few trial children skipped days 2–3. This
   protection is held apart from the 25-day post-treatment prophylaxis of case management, and the two combine as
   1 − (1 − a)(1 − b). A new dose restarts the curve.
5. **Clinical effect: no separate factor (dropped).** The trials' near-equal in-season effect on infection and on
   disease means the clinical effect should follow from the infections prevented, as it does in both
   Weibull-curve sources. ACCESS-SMC's 88.2% (0–28 days) and 61.4% (29–42 days) are used as a **check** of the
   effect that emerges, not as an input. The self-test asks only that the 0–28-day effect be high and that it
   fall after day 28.

**Dropped:**
- `smc_pe`: a clinical effectiveness used as infection protection, which becomes a check.
- `smc_hi_frac`, and the hard-coded 0.95 and 0.5 reach of the two groups: unsourced.
- the 28-day module constant: replaced by the Weibull curve.

**Kept:**
- clearance at dosing;
- ages 3–59 months;
- month-start round dates;
- the per-child persistent latent;
- uncharged past and routine SMC;
- the smc_doses cost driver, which counts children given the day-1 dose.

**Assumptions, named:**
- a child's propensity persists across seasons. The surveys measure one season;
- clearance at dosing has probability 1 (step 3);
- the partial-course multiplier (step 4).

**New engine parameters.** All are kind `fixed`, and each has its source on the card.

| Parameter | Value | Range for priors | Source |
|---|---|---|---|
| `smc_cov` | 0.796 | 0.521–0.957 | Table 21 above; range: ACCESS-SMC Nigeria 2016 mean per month (52.1%) to Malaria Consortium 2022 end-of-cycle C2 (95.7%) |
| `smc_rho` | 0.417 | none (fixed) | beta-binomial fit to Table 21 |
| `smc_adh` | 0.944 | 0.842–0.986 | Table 16; range as in the literature table |
| `smc_e0` | 1.0 | none (fixed) | Burgert 2022 Table 1 |
| `smc_half` | 32 days | 24–32 days | Burgert 2022 Table 1; Braunack-Mayer 2024 (24–30 days) |
| `smc_k` | 5.4 | none (fixed) | Burgert 2022 Table 1 (Braunack-Mayer 2024: 5.34) |
| `smc_partial` | 0.910 | none (fixed) | Cairns 2022 (79.8% / 87.7%) |

A unit's own coverage, such as a state's survey value, can be set through a run spec's `engine_parameters`, as for
`net_retention_half`. Out-of-range values fail closed: `smc_cov` outside (0, 1], `smc_rho` outside [0, 1),
`smc_adh`, `smc_e0` or `smc_partial` outside [0, 1], and `smc_half` or `smc_k` that is not finite and positive.

**Burn-in key.** With past SMC, the seven new parameters join the key, as `smc_pe` and `smc_hi_frac` did. Without
past SMC they act after the fork.

**Not byte-identical.** Every run with SMC changes, so `validate_once.sh` recomputes all the evidence. B13 and B17
stay held out. A Nigeria reachability check on the 57 clamped LGAs is a diagnostic only, and it is reported in the
PR, not here, because the run data are licensed.

**Tests (written first; `selftest_late.py`, section `_self_test_v011`).**
- Q0: the card, parameters and priors; `smc_pe` and `smc_hi_frac` are gone.
- Q1: the protection curve: E0 at t0, E0/2 at L, decreasing, 0 before any dose.
- Q2: the propensity model: rounds received follow the beta-binomial (Table 21 reproduced), the mean per-round
  reach is `smc_cov`, and `smc_rho` = 0 gives independent rounds.
- Q3: in the sim, a round reaches the share `smc_cov`, and the reached children's states and doses are correct
  (clearance, the share with a full course, E0 or E0 × `smc_partial`); Q3e: reach in the intervention's second round
  is correlated with reach in its first.
- Q4: a population-level diagnostic over all children under 5, reached or not: the reduction in clinical incidence
  is high over the rounds and lower at 29–42 days after the last round. It is not the recipients' effectiveness that
  ACCESS-SMC estimates, so the two are not compared numerically.
- Q5: out-of-range values fail closed; Q5b: also for a member forked from another member's burn-in.
- Q6: the burn-in key; Q6b: members sharing one burn-in each equal their own run and differ from one another.

**Codex review round 1 (before the benchmark run).** No P1 findings. These P2s were fixed, each test-first:
- validation of every batch member (Q5b);
- Q4's post-dose window, which started a month late, and its wording;
- per-member SMC parameters in a shared burn-in (Q6b) and reach persistence across the intervention's own rounds
  (Q3e);
- `prop_smc` added to the start-state hash;
- the partial-course multiplier named as an assumption.

The tiny-`smc_rho` overflow (P3) is handled too. One item is open: `resolve_params` coerces `True` and numeric
strings to floats before `smc_check` sees them. It is shared by every parameter, so it is left to a contract-level
change.

## v0.10 scope: past SMC and a net type per past net campaign (owner approval 2026-11-04)

**Why.** Up to v0.9 a unit had no attribute for past SMC, so a calibration to a survey taken after SMC began (for
example a 2021 survey) treated SMC-treated units as never having had SMC: their fitted transmission can come out too
low. Past net campaigns all used one net type (`hist_net`), so a unit that moved from pyrethroid to PBO or dual-AI
nets could not be described.

**New unit attributes.** Each is optional, with a default declared in `attribute_binding.yaml`.

| Attribute | Format | Default | Meaning |
|---|---|---|---|
| `smc_hist_years` | list of whole calendar years, e.g. `[2019, 2020, 2021]`; units-table column: `{column, split: " ", empty: "none"}` with cells `2019 2020 2021` or `none` | `[]` | SMC rounds in each of those years in the unit's SMC months (`smc_months`, or the default from the seasonality) |
| `smc_hist_rounds` | one whole number for every past year, or a list with one per entry of `smc_hist_years` | the number of SMC months | a year with n rounds gets the first n of the unit's SMC months |
| `hist_net_by_year` | mapping `{decimal year: pyr \| pbo \| dual}`, or the string `"2017.5:pyr 2020.5:pbo"`; a units-table column holds the string, or `none` | none | each named past campaign gives that net type; campaigns not named, and routine campaigns, keep `hist_net` |

**Mechanism.** No new mechanism. Past SMC rounds run through `model.SMCDelivery`, the intervention of the package's
SMC (and of routine SMC), with the same efficacy `smc_pe`, the same access groups `smc_hi_frac` and the round dates
of `model.smc_targets` (month starts). They run in every arm, the comparator included, and are never charged
(`count=False`). A per-campaign net type sets the code that `ITNCampaign` gives to that campaign's nets; nothing else
about the campaign changes. Both come from `delivery_plan`, so `schedule()` lists past SMC rounds as `history`
events (label `past SMC round`) and names each past campaign's own net type (`past net campaign (dual)`).

**Fail closed.** The engine refuses (ValueError, before any sim is built):
- `smc_hist_years` that is not a list (a string or a bare number), or holds a fractional year, NaN, a boolean, a
  string, or a repeated year;
- a past SMC round at or after the delivery start or the first package or routine SMC round (past SMC precedes
  both; without a delivery period the package's SMC can start before the window, in its first calendar year), or
  one that the sim would run on a daily step at or after the delivery start (checked on the sim's own steps);
- with past SMC, SMC months that are not distinct whole months 1–12;
- `smc_hist_rounds` that is not a whole number from 1 to the number of SMC months, or a list of another length;
- `hist_net_by_year` with a net type other than pyr, pbo or dual, a year that is not one of `itn_hist_years`
  (within 1e-6 y), a year given twice, a token that is not `year:type`, or a value that is neither a mapping nor a
  string.

**Burn-in sharing.** Past SMC acts before the fork, so for a unit with `smc_hist_years` the parameters `smc_pe` and
`smc_hi_frac` join its burn-in key; a past dual-AI campaign named in `hist_net_by_year` puts `cfp_kill` and
`cfp_half` in the key, as `hist_net: dual` already did. Without these attributes the key is v0.9's.

**Spec binding (disease-agnostic).** A bound column may not be blank (`attribute_column_empty`), so an empty list
needs an explicit token. `toolkit/spec/schema.py` gains an optional `empty` field for split columns:
`{column: c, split: " ", empty: "none"}` reads a cell equal to `none` as `[]`. `empty` needs `split` and a non-blank,
non-numeric string, so no reader can take the token for data (self-tests SP8b–g).

**Back-compatibility.** With the attributes absent, empty (`[]`, `{}`, `""`, `"none"`), `smc_hist_rounds` given
without past years, or `hist_net_by_year` naming every campaign at `hist_net`, the sim has the v0.9 interventions and
gives byte-identical outcomes, cost drivers and meta (self-test H3). `benchmarks/restamp_proof.py` compares v0.9 and
v0.10 on the pack, the evidence cases, the pinned scenarios, and a new scenario `empty_history_v010` that sets the
new attributes at their empty values.

**Tests (written first).** H0 card and binding; H1 past SMC rounds in the schedule at their dates, uncharged, on the
steps the sim runs, and the rounds-per-year override; H2 the trajectory is identical before the first past round and
under-5 incidence is lower in the past SMC months (sum of the monthly rates over June–September 2015,
0.081 vs 0.646 per child-year, 1,000 agents, horizon 2015–2017), never charged; H3 byte-identity with empty values; H4 malformed formats fail closed, in `schedule()`
and in the sim path; H5 each past campaign gives its own net type and routine campaigns keep `hist_net`; H6 burn-in
key; H7 (Codex review round 1) overlap with the package's SMC, the sim-step check, SMC months and year keys of
mixed type. The v0.8–v0.10 sections run in a second process (`selftest_late.py --self-test`; L0 checks every
section runs in one of the two), so each module stays well inside CI's per-test time limit.

## v0.9 scope: smooth-compact net survival (written before the v0.9 pack run)

**The change.** From v0.3 to v0.8 nets were lost at the constant daily rate ln2 / `net_retention_half`. That is
an exponential survival curve whose half-life equals the published median retention. v0.9 uses the survival
function that the source of that median fits, the smooth-compact curve:

    S(t) = exp(k - k / (1 - (t/L)^2))   for t < L,   S(t) = 0 for t >= L.

- **k = 20, fixed.** New engine parameter `net_survival_k` (kind `fixed`). Bertozzi-Villa et al. 2021, Supplementary
  Note 1.4 (stock-and-flow model, p. 19): "κ was fixed at a value of 20 and τ was given a wide prior τ ∼ Unif(5.5,
  20.7). After model fitting, net retention half-life was calculated analytically as the time t at which
  Loss(t, κ, τ) = 0.5." The paper gives no range for k. Briët, Hardy and Smith 2012 (Malar J 11:20, PMC3273435,
  appendix on `attritionOfNets`) used the same function with k = 18.
- **L from the median.** S(median) = 1/2 gives L = median × sqrt((k + ln2) / ln2), which is 5.46 × the median at
  k = 20. At the continental median of 1.64 y, L = 8.96 y. The prior bounds agree: τ = 5.5 y and 20.7 y give medians
  of 1.01 y and 3.79 y. The paper's Fig. 5 notes the 1-year floor, and 3.79 y is the highest upper 95% bound in
  its Supplementary Table 1.7.
- **`net_retention_half` is still the median.** Its name, value (1.64 y = 598.6 d) and card range (IQR 1.33–2.37 y,
  main text "Net retention time" and Supplementary Table 1.7) are unchanged. It is no longer used as a half-life.
- **Per-net daily loss.** The sim already tracks each person's net age (`net_t`) for holes. A net of age a days is
  now lost each day with the conditional hazard 1 − S(a+1)/S(a) (`model.net_loss_prob`). Compounded from age 0, this
  gives S(t) exactly (self-test N2b), and the sim reproduces S(a0 + T)/S(a0) by net age (N3). On a campaign's step
  the age is 0, for a first net and for a replacement of a net still held (N7). A shape or median that is not
  finite and positive fails closed (N6).
- **Access at distribution** (`access_for_use`) divides reported use by the same S(t) (N4).
- **Insecticide decay (Churcher 2016) and holes (Briët 2020)** are unchanged.

| Net age | Exponential (v0.8) | Smooth-compact (v0.9) |
|---|---|---|
| 6 months | 0.81 | 0.94 |
| 1 year | 0.66 | 0.78 |
| 1.64 years (median) | 0.50 | 0.50 |
| 2 years | 0.43 | 0.35 |
| 3 years | 0.28 | 0.08 |
| 4 years | 0.18 | 0.007 |

At the same median, nets last longer in the first year and are almost all gone by year 4.

**Muleba keeps its own retention, re-derived for the new shape.** v0.3 derived Muleba's 949 days from the trial's
reported use: 77.3% in year 1 (surveys at 4 and 9 months, mean 0.54 y) and 59.2% in year 2 (16 and 21 months, mean
1.54 y). Under the exponential, those two values give a half-life of ln2 / ln(0.773 / 0.592) = 949 d. Under the
smooth-compact curve, the same two values give a median of 841.3 d (2.30 y): S(1.54 y) / S(0.54 y) = 0.592 / 0.773
(self-test N5). Keeping 949 d would mean a ratio of 0.81, which the trial did not report. The value comes from the
trial's use data only. Two assumptions carry over from v0.3: each year's use is evaluated at the mean time of its two
surveys, and the decline in use is all attrition (use among those with access is constant). Averaging S over the
two surveys of each year instead gives 838 d. The spec-value precedence test (EP36) now reads the setting's own value from the pack.

**Nigeria.** Supplementary Table 1.7 gives Nigeria (NGA) a median LLIN retention of 2.22 y (95% CI 2.00–2.47) from
9 surveys. It is recorded in `priors.yaml` (`country_values`) for a run spec to cite through `engine_parameters`.
The engine default stays the 40-country median, because the paper reports the country value as one country's fit.
The upper bound (2.47 y) is above the card range (2.37 y), so such a spec needs `outside_card_range: true`.

**What did not choose anything.** No net-survival value was chosen using any benchmark result. Only the two existing fitted parameters (cfp_kill, irs_kill) were refitted, to their usual targets B10, B11, B12, B12b and B12m:
- k and the median come from Bertozzi-Villa 2021;
- Muleba's median comes from its trial's reported use;
- B7, B8 and B16c stay held out, as do B9, B9b, B9c, B13, B15 and B17–B20.

The change is not byte-identical, so `validate_once.sh` recomputes the equilibration, guess sensitivity, joint fit,
pack run, B15 mechanism scan and per-run cost. The v0.8 files move to `history/` (`*_v0.8.yaml`) and the tables
below show both versions.

**Earlier candidate 8 (v0.4 table).** The v0.4 notes listed the S-shaped loss curve as candidate 8, "not implemented:
a parameter would be guessed", because Supplementary Note 1.4 was not accessible then. It has now been read, and k
is the paper's own value.

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

**Review round 1 (Codex) and #127's hook contract `figures-hooks/1`.** Each change has a test that
failed first (α2, α3):
- **Executed events (S1, S10).** `schedule()` now lists deliveries as the sim executes them.
  `delivery_plan` gives each delivery's raw target and the intervention that runs it. `schedule()`
  maps every target to its daily step with `model.event_steps`, the function the interventions
  use (`_event_tis`). Targets of one intervention on one step are one delivery (repeated
  `itn_hist_years`, or two dates on the same day), and `t0` is the step's year. The sim's time
  vector comes from a Starsim `Timeline` with the sim's start, stop and step, so no sim is built.
  SMC and IRS targets come from `model.smc_targets` and `model.irs_targets`, which the
  interventions also use.
- **Charging (S9).** A decision delivery is charged only when its step lies where the sim
  counts cost drivers (`model.delivery_contains`, shared with `Malaria.in_delivery`). Two
  examples: a campaign before the window start when no delivery period is given, and SMC rounds
  before a mid-year window start. The sim keeps running these as before, uncharged, but
  `schedule()` now fails closed rather than reporting a charged event.
- **Kinds (S12d, S12e).** Under the contract, `history` events precede the delivery period,
  `routine` events lie outside it, and `decision` events lie inside it and belong to a component
  the package sets. A past campaign at or after the delivery start makes `schedule()` fail
  closed.
- **Series metadata (S11, S12b, S12c).** Each `run_series` entry also returns `t_end` and `days`
  for every step. `series_window(entry, metric, a, b)` aggregates them exactly into the window
  metric over any union of steps, and fails closed on a window that cuts through a step.
  - The grid is the contract's, `hooks.monthly_grid(horizon)`, the same for every arm (round 2
    below).
  - Each card metric declares a `denominator`, and the engine declares
    `hooks_contract = "figures-hooks/1"`.
  - Step slices are found once with `searchsorted`, with the same day selection as the windows.
- **Proof coverage (RP10).** The pack builds plain horizons, and none of its units sets routine
  nets or routine SMC. So `restamp_proof.py` now also runs three pinned scenarios in both trees:
  - GC7-like delivery with routine nets and routine SMC;
  - boundary dates (the campaign and an IRS round at the delivery start, routine SMC and a
    routine campaign at the delivery end, the add-on stopping there, windows cutting through
    months);
  - a heterogeneous batch (two units, parameters inside and outside the burn-in key, no delivery
    period).

**Review round 2 (Codex) and #127's round 2.** Again, each change has a test that failed first
(α4, α5):
- **Coverage (S13).** `series_window` refuses a window that reaches outside the entry's
  coverage `[t[0], t_end[-1])`; before, it silently aggregated only the part it covered. It also
  needs the window to be covered completely by whole steps.
- **Grid (S12c, S11).** `run_series` uses `hooks.monthly_grid(horizon)`: a step starts at every
  `start + k/12` before the window end. When the window ends inside a month, the last step is
  partial and ends at the window end. Its `mean` is that step's mean, and its `rate` is episodes
  per person-year over the partial span. `t_end` and `days` follow it, and `series_window` stays
  exact over it.
- **Half-open delivery semantics (S14).** Tests at each boundary check the schedule against the
  sim:
  - A decision campaign at the delivery start d0 is listed, charged and charged by the sim; one
    at the delivery end d1 is refused.
  - A past campaign at d0 is refused.
  - A routine campaign at d0 is refused; at d1, a routine campaign and the first routine SMC
    round are accepted, on the steps the sim runs them.
  - With GC7 dates, routine nets at campaign + 3 and routine SMC from d1 lie outside the delivery
    period.

  `model.delivery_contains` (`d0 <= t < d1`) already matched these rules, and so did the routine
  placement.
- **Proof record (RP11).** Each side of the proof records its Python, NumPy, Starsim and
  platform versions (fail closed without them). The record's `execution` block names the Coiled
  cluster id, the run id and the workspace.
- **Confirmation (RP12).** A re-stamp record no longer says that `validate_once.sh` confirms it:
  `confirmation: {validate_once: pending}` until that run reports. Earlier records under
  `previous` keep the wording they were written with.

**#127's round 3: exact half-open boundaries (S15).** The hook contract now compares
boundaries exactly. `schedule()` reports each event at its executed step's year and charges it
by the sim's own test (`model.delivery_contains`), so both agree exactly. Tests on the real time
vector (the card's 50-year burn-in, GC7 dates) show:
- A GC7 decision campaign at the delivery start is reported at exactly 2024.0 and charged.
- A past campaign at horizon.start is reported at exactly 2023.0.
- The first routine SMC round is reported at exactly d1 = 2027.0 and is uncharged.
- The routine net at campaign + 3 is on its executed step.
- Nothing is listed at or after the window end.

Starsim rounds its time vector to 9 decimals, so whole years from the sim start are exact.
Representation-only differences (at most 1e-9 y) are reported at the boundary's own float, but
only when every membership test agrees, so no event moves across a boundary (S15e).

Sub-day offsets are real, not representation. With an off-grid start (2023.25), the steps
nearest d0 and d1 lie just after them: the decision is charged and the routine round counts as
routine. With a delivery start between steps whose nearest step lies before it (delivery
2024.25, campaign 2024.25, sim start on a whole year), the sim delivers the campaign uncharged.
`schedule()` then fails closed (S15d). This is unchanged v0.6 behaviour, listed as an open item.

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
| 8 | S-shaped net loss curve (Bertozzi-Villa 2021 smooth-compact function) | the main text says the curve is S-shaped; its steepness is in Supplementary Note 1.4, which is not accessible | down at 12 m | not implemented: a parameter would be guessed; the exponential stays flagged. **v0.9: implemented from Supplementary Note 1.4 (k = 20); see the v0.9 section** |

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
  2021, 40 countries, IQR 1.33–2.37), used as an exponential half-life (v0.9: the median of the
  smooth-compact curve, k = 20). Muleba uses its own
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
### Benchmark pack: spike v0.1, earlier rounds and v0.9 (this run)

| ID | Benchmark | Target (band) | v0.1 spike | v0.2 (round 1) | v0.3 round 2 | v0.3 | v0.4 | v0.5–v0.8 | v0.9 | v0.9 seed range | v0.9 status | Role |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| B7 | ITN (pyrethroid) vs none, prevalence RR at 12 m (trials' own age bands) | 0.83 (0.68–0.98) | 0.735 (in band) | 0.578 (**out of band**) | 0.642 (**out of band**) | 0.595 (**out of band**) | 0.589 (**out of band**) | 0.578 (**out of band**) | 0.501 | 0.466–0.539 | **out of band** | held_out |
| B8 | ITN vs none, clinical incidence RR 0-24 m (under 5) | 0.55 (0.46–0.64) | 0.578 (in band) | 0.45 (**out of band**) | 0.54 (in band) | 0.462 (in band) | 0.462 (in band) | 0.449 (**out of band**) | 0.41 | 0.385–0.451 | **out of band** | held_out |
| B9 | PBO vs pyrethroid nets (no IRS), prevalence OR at 9 m | 0.37 (0.09–0.65) | 0.798 (**out of band**) | 0.506 (in band) | 0.377 (in band) | 0.36 (in band) | 0.36 (in band) | 0.359 (in band) | 0.377 | 0.349–0.405 | in band | held_out |
| B9b | PBO vs pyrethroid nets (no IRS), prevalence OR at 21 m | 0.4 (0.2–0.81) | 0.953 (**out of band**) | 0.691 (in band) | 0.797 (in band) | 0.662 (in band) | 0.662 (in band) | 0.67 (in band) | 0.666 | 0.635–0.703 | in band | held_out |
| B9c | PBO vs pyrethroid nets, prevalence aOR at 24 m (Misungwi) | 0.99 (0.67–1.45) | 0.993 (in band) | 0.75 (in band) | 0.865 (in band) | 0.705 (in band) | 0.705 (in band) | 0.7 (in band) | 0.731 | 0.713–0.757 | in band | held_out |
| B10 | Dual-AI vs pyrethroid nets, prevalence OR at 24 m | 0.45 (0.23–0.67) | 0.688 (**out of band**) | 0.642 (in band) | 0.667 (in band) | 0.665 (in band) | 0.665 (in band) | 0.649 (in band) | 0.667 | 0.621–0.723 | in band | fit |
| B11 | Dual-AI vs pyrethroid nets, incidence RR 0-24 m | 0.54 (0.38–0.7) | 0.402 (in band) | 0.501 (in band) | 0.504 (in band) | 0.511 (in band) | 0.511 (in band) | 0.501 (in band) | 0.498 | 0.453–0.553 | in band | fit |
| B12 | Non-pyrethroid IRS added to ITN, prevalence RR at 12 m | 0.61 (0.34–0.88) | 0.878 (in band) | 0.786 (in band) | 0.807 (in band) | 0.8 (in band) | 0.8 (in band) | 0.802 (in band) | 0.804 | 0.77–0.83 | in band | fit |
| B12b | Non-pyrethroid IRS added to ITN, incidence RR 0-24 m | 0.86 (0.49–1.23) | 0.746 (in band) | 0.576 (in band) | 0.59 (in band) | 0.574 (in band) | 0.574 (in band) | 0.572 (in band) | 0.579 | 0.555–0.627 | in band | fit |
| B12m | Pirimiphos-methyl IRS added to pyrethroid nets (Muleba), prevalence OR at 9 m | 0.33 (0.19–0.55) | n/a | 0.427 (in band) | 0.445 (in band) | 0.451 (in band) | 0.451 (in band) | 0.449 (in band) | 0.451 | 0.411–0.472 | in band | fit |
| B13 | SMC, in-season clinical incidence RR (3-59 m) | 0.27 (0.14–0.4) | 0.346 (in band) | 0.353 (in band) | 0.361 (in band) | 0.355 (in band) | 0.355 (in band) | 0.358 (in band) | 0.36 | 0.346–0.371 | in band | held_out |
| B15 | ITN x IRS interaction OR (PBO x IRS, prevalence at 9 m, 2x2 factorial) | 2.43 (1.19–4.97) | 0.982 (in band) | 1.4 (in band) | 1.44 (in band) | 1.48 (in band) | 1.48 (in band) | 1.49 (in band) | 1.51 | 1.26–1.69 | in band | held_out |
| B16c | EIR-prevalence curve (Beier 1999 rules: EIR > 15 -> PfPR >= 50%; EIR >= 200 -> PfPR > 80%) | rules | 4/5 rules (**out of band**) | 4/5 rules (**out of band**) | 4/5 rules (**out of band**) | 4/5 rules (**out of band**) | 4/5 rules (**out of band**) | 4/5 rules (**out of band**) | 4/5 rules | per seed: 4/5, 4/5, 4/5, 4/5, 4/5, 4/5, 4/5, 4/5 | **out of band** | held_out |
| B17 | SMC $ per DALY averted (reference setting) | 38.4 (7.67–192) | 18.3 (in band) | 18.8 (in band) | 18.1 (in band) | 18.1 (in band) | 18.1 (in band) | 19 (in band) | 19 | 17.2–21.1 | in band | held_out |
| B18 | ITN vs none $ per DALY averted (reference setting) | 44.5 (8.9–223) | 57.3 (in band) | 14.2 (in band) | 23.2 (in band) | 19.1 (in band) | 19.1 (in band) | 19.5 (in band) | 21.9 | 16.5–34.2 | in band | held_out |
| B19 | IRS vs none $ per DALY averted (reference setting) | 25.2 (5.03–126) | 370 (**out of band**) | 115 (in band) | 122 (in band) | 114 (in band) | 114 (in band) | 119 (in band) | 116 | 108–126 | in band | held_out |
| B20 | Dual-AI vs pyrethroid nets, incremental $ per DALY averted (high resistance) | 19 (3.8–95) | 3.63 (**out of band**) | 4.16 (in band) | 4.33 (in band) | 4.89 (in band) | 4.89 (in band) | 4.58 (in band) | 6.41 | 4.87–9.22 | in band | held_out |

v0.9: 14 in band, 3 out of band, 0 not assessable, 0 band mis-based, of 17 (9 of 12 held-out in band). 298 engine runs, 8 seeds per arm (common random numbers). v0.1: 10 of 16 in band.
- B7 by outcome definition (geometric mean of these): Halbluetzel 1996 (0-59 months) 0.4837; Moyou-Somo 1995 (15 and under) 0.5067; Rowland 1996 (all ages) 0.5075; Smithuis 2013 (under 10) 0.5011; Sochantha 2006 (all ages) 0.5075. Diagnostic, not judged: windows.m12.pfpr_6m14y 0.5074.
- B15 requires {'B9': [0.21, 0.65], 'B12m': [0.19, 0.55]}; computed B9 0.3768, B12m 0.451.
- B15 arms (OR vs standard LLIN): PBO alone 0.3768, IRS alone 0.451, both 0.2571; prevalence {'a00': 0.5491, 'a10': 0.3146, 'a01': 0.3545, 'a11': 0.2385}.
- B19: the target 25.2 is below the price floor 67.7 per DALY (cost per person divided by ALL the comparator's DALYs per person): no intervention can reach the target value at the pack's price; only the band's upper part is reachable. The band's basis (price or setting) differs from the decision's.
- B19: price floor 67.7 per DALY (cost 24.8 per person over the comparator's 0.367 DALYs per person), below the band's upper edge 126; meeting the band would need 53.8% of all DALYs averted; the engine averts 58.5%.

Calibrated transmission per setting (value, grid; a value at an outer grid bound is flagged):

cochrane_itn m0 13.47 on [4, 8, 16, 32, 64]; muleba m0 115.9 on [8, 16, 32, 64, 128, 256]; misungwi m0 11.9 on [8, 16, 32, 64, 128]; benin m0 15.4 on [4, 8, 16, 32, 64]; irs_added m0 37.59 on [8, 16, 32, 64, 128]; sahel_smc m0 22.34 on [4, 8, 16, 32, 64]; ce_ref m0 17.31 on [4, 8, 16, 32, 64]; ce_res m0 17.31 on [4, 8, 16, 32, 64].

Fitted engine-level parameters (fit rule `joint-v1`, seeds [11, 12, 13, 14, 15, 16, 17, 18], 25 design points):

| Parameter | Value | Range (all feasible targets in band) | Box | Fitted to |
|---|---|---|---|---|
| cfp_kill | 0.405 | 0.405–0.545 | [0.0, 0.8] | B10, B11, B12, B12b, B12m |
| irs_kill | 0.433438 | 0.3087–0.5641 | [0.0, 0.95] | B10, B11, B12, B12b, B12m |

Feasibility before fitting: infeasible in the box: none; conflicting pairs: none; all feasible targets in band at the value: True. Surrogate leave-one-out error (log scale): B10 0.0174, B11 0.0188, B12 0.0098, B12b 0.0089, B12m 0.0139.
Verification at the value (re-run, re-calibrated): B10 0.6671 (surrogate 0.6691, in), B11 0.4979 (surrogate 0.4944, in), B12 0.8037 (surrogate 0.8086, in), B12b 0.5794 (surrogate 0.5803, in), B12m 0.451 (surrogate 0.4405, in).

### B15 by mechanism (Muleba 2×2, 9 months; re-calibrated per row; fitted parameters fixed)

| Configuration | PBO alone OR | IRS alone OR | Both OR | Interaction OR | Seed range | Single arms in trial CI |
|---|---|---|---|---|---|---|
| Trial (Protopopoff 2018) | 0.37 | 0.33 | 0.29 | 2.43 | CI 1.19–4.97 | |
| hut coefficients only (no household, no diversion, short IRS) | 0.3563 | 0.6716 | 0.2822 | 1.18 | 1.0439–1.3045 | no |
| + IRS long-lasting product decay (alone) | 0.3563 | 0.3886 | 0.2146 | 1.55 | 1.2934–1.8381 | yes |
| + IRS diversion to outdoor biting 0.5 (alone) | 0.3563 | 0.7189 | 0.2977 | 1.162 | 0.9413–1.3002 | no |
| + household net-use clustering 0.5 and net x IRS correlation 0.3 (alone) | 0.3536 | 0.6593 | 0.2754 | 1.181 | 1.0741–1.2709 | no |
| + exposure correlated with net non-use 0.2 (alone) | 0.3673 | 0.6501 | 0.2912 | 1.22 | 1.078–1.3559 | no |
| all together (engine defaults) | 0.3768 | 0.451 | 0.2571 | 1.513 | 1.258–1.6885 | yes |
| all together, high correlations (0.9, 0.9, 0.6) | 0.4611 | 0.5274 | 0.3141 | 1.292 | 1.1347–1.4569 | yes |
| all together, full diversion (1.0) | 0.3768 | 0.5024 | 0.281 | 1.485 | 1.3297–1.5597 | yes |
| all together, high correlations and full diversion | 0.4611 | 0.5764 | 0.3461 | 1.302 | 1.1756–1.4199 | no |

### Field-use mechanisms, each alone and together (nets vs none, B7/B8; Muleba B9/B15)

| Configuration | B7 (0.68–0.98) | B8 (0.46–0.64) | B9 (0.09–0.65) | B15 (1.19–4.97) | B15 arms: PBO / IRS / both |
|---|---|---|---|---|---|
| hut-like nets (no attrition, full use given access, no holes) | 0.4519 [0.4296–0.4911] **out** | 0.2619 [0.2411–0.3133] **out** | 0.4148 [0.3816–0.4718] | 1.3832 [1.1354–1.54] | 0.4148 / 0.4379 / 0.2513 |
| + attrition alone (median retention 1.64 y) | 0.5068 [0.4804–0.5336] **out** | 0.4312 [0.3996–0.47] **out** | 0.3992 [0.367–0.4159] | 1.4215 [1.3443–1.5381] | 0.3992 / 0.4431 / 0.2514 |
| + use below access alone (use rate 0.871, half persistent) | 0.4242 [0.398–0.4682] **out** | 0.2415 [0.2254–0.2843] **out** | 0.3792 [0.3633–0.4043] | 1.4634 [1.3068–1.6114] | 0.3792 / 0.4467 / 0.2479 |
| + holes alone (Briet 2020) | 0.4375 [0.4005–0.4723] **out** | 0.251 [0.2289–0.274] **out** | 0.4085 [0.3711–0.4485] | 1.3812 [1.2554–1.6539] | 0.4085 / 0.4516 / 0.2548 |
| all field mechanisms (engine defaults) | 0.5012 [0.4661–0.5389] **out** | 0.4104 [0.3846–0.4508] **out** | 0.3768 [0.3488–0.405] | 1.5127 [1.258–1.6885] | 0.3768 / 0.451 / 0.2571 |

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
| v0.4 start (no burn-in, one run per package) | 155.3 | 38.8 | 1.00x | 155 | 441 |
| v0.5: one burn-in per point, packages forked (run_batch) | 207.4 | 51.9 | 1.34x | 207 | 1,832 |
| v0.5 without sharing (one burn-in per package run) | 533.6 | 133.4 | 3.44x | 534 | 1,006 |
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
  - the exponential shape of net loss (the paper's loss curve is S-shaped); resolved in v0.9
    (smooth-compact, k = 20, Bertozzi-Villa 2021 Supplementary Note 1.4);
  - the split of the use gap between persistent and night-to-night non-use (0.5);
  - interpolating the holes slope by bioassay mortality;
  - growth of holed area as linear in log area, with a cap at 3 years;
  - Cochrane setting use at distribution (0.7);
  - using a continent-wide retention (1.64 years) in trial settings without their own data.
    Briët 2020's cohorts kept more of their nets: 69–98% survival at 24 months.
