#!/usr/bin/env python3
"""Malaria engine behind the `Engine` contract (toolkit/engines/contract.py).

    python3 -m toolkit.engines.malaria.engine --self-test     # M1-M21 and the v0.5-v0.7 sections
    python3 toolkit/engines/malaria/selftest_late.py --self-test  # the v0.8-v0.10 sections (a second process)
    python3 -m toolkit.engines.malaria.engine --card [path]    # writes card.yaml (after the pack is run)

Our own Starsim code (model.py, vector.py). Fitted engine-level parameters come from the
benchmark pack, fitted once (benchmarks/fitted.yaml); benchmark results come from
benchmarks/results.yaml. Neither is tuned per question.

Unit attributes (UnitSpec.attributes):
  m0              mosquito emergence density per person (the calibrated transmission scaler)
  season          12 monthly relative transmission values (mean 1)
  resistance      pyrethroid discriminating-dose bioassay mortality, 0-1 (1 = susceptible)
  species         {gambiae|arabiensis|funestus: share}
  cm_ft           probability a clinical episode is effectively treated (current level)
  itn_use         survey-reported nightly net use, measured itn_use_at years after the campaign
  itn_use_at      optional (default 0); access at distribution = itn_use / (retention(t) x use_rate)
  itn_hist_years  decimal years of past net campaigns (net type hist_net, or per campaign hist_net_by_year)
  hist_net        optional; net type of past and routine campaigns, pyr (default) | pbo | dual
  hist_net_by_year optional (v0.10); {decimal year: pyr|pbo|dual} or "year:type year:type"; years from itn_hist_years
  smc_hist_years  optional (v0.10); whole calendar years of past SMC (before the delivery period), every arm
  smc_hist_rounds optional (v0.10); rounds per past SMC year: one number or one per year (the first n SMC months)
  smc_hist_cov    optional (v0.13); mean per-round reach of past SMC: one share or one per year (default smc_cov)
  campaign_year   decimal year of the package's net campaign (in the delivery period)
  itn_routine_interval  optional (v0.6); years between routine net campaigns after campaign_year, in every arm
  smc_routine     optional (v0.7); 0 | 1: routine SMC every season after the delivery period, in every arm
  smc_months      optional; default: rainfall-peak month -1 .. +2 (season is lagged one month)
  irs_month       optional; default: three months before the transmission peak
  irs_years       optional; decimal years of IRS rounds (default: every year of the delivery period)
  irs_product     optional; pm_cs (default, pirimiphos-methyl CS) | short
  resistance may be one value or {species: value, default: value}
Package components: net none|pyr|pbo|dual; smc 0|1; irs 0|1; cm 0|1 (enhanced CM).
Outcomes: per-person totals over the decision window by age group (u5, 5_14, 15p) for
cases, severe, deaths, treated and DALYs; named-window metrics (microscopy prevalence in
model.PFPR_BANDS: pfpr_u5, pfpr_6m14y, pfpr_2_10 and, from v0.4, pfpr_lt5, pfpr_lt10,
pfpr_le15, pfpr_all); cost drivers per person.

v0.5: an equilibrated start. Each run begins with a no-intervention burn-in of `burnin_years`
(card parameter, default 50; source benchmarks/equilibration.yaml) before horizon.start, from
a closed-form immunity guess whose influence the burn-in removes. The population starts at
n_agents exp(-POP_GROWTH burnin_years) and grows to about n_agents at horizon.start.
run_batch() runs the burn-in and the shared history once per (unit, seed, burn-in parameters)
and forks an exact copy for each package at its first component event, so packages share an
identical history (common random numbers); run() is run_batch() with one member.
initial_state() returns the state at horizon.start. `batch_shares_state` tells the benchmark
runner and the analysis batch to send one unit's packages together.

v0.6: a delivery period distinct from the outcome window (`horizon.delivery`, a DeliveryHorizon from
toolkit/engines/horizon.py; default: the decision window). SMC rounds, default IRS rounds and the enhanced
case-management add-on act only in delivery years; with an explicit period, every component event must fall in it
(fail closed). Cost drivers count the delivery period (what the decision pays for), health outcomes the outcome window.
`itn_routine_interval` adds routine net campaigns at campaign_year + k x interval before the window's end, in every
package including the comparator, with net type hist_net and never charged. Without either, a run is byte-identical
to v0.5 (self-test D9; benchmarks/restamp_proof.py).

v0.7: `smc_routine` (0 | 1, default 0) runs routine SMC rounds in the unit's SMC months in every season from the
delivery period's end to the outcome window's end, in every package including the comparator, never charged. It
needs an explicit delivery period (fail closed). With package SMC as well, the package's delivery seasons and the
routine seasons must form one continuous season sequence (smc_schedule_problems). With the attribute absent or 0,
a run is byte-identical to v0.6 (self-test R7; benchmarks/restamp_proof.py).

v0.8: the two figure hooks of toolkit/engines/hooks.py. schedule() lists the unit's delivery events in one arm
(history, decision, routine) from delivery_plan(), the one function the sim's configuration also reads; it is pure.
run_series() returns monthly series of the card's outputs["series"] (under-5 prevalence, the calibration metric;
all-age and under-5 clinical incidence per person-year; two more prevalence bands), read from the model's daily
records through run_batch's member loop (shared burn-in and history). run and run_batch are unchanged (self-test S7;
benchmarks/restamp_proof.py compares the v0.7 and v0.8 code).

v0.9: net attrition follows the smooth-compact survival function of Bertozzi-Villa 2021 (Supplementary Note 1.4),
S(t) = exp(k - k / (1 - (t/L)^2)) for t < L and 0 after, k = 20 (new fixed parameter net_survival_k) and L from the
median retention net_retention_half (unchanged name, value and range). A net of age a days is lost each day with
probability 1 - S(a+1)/S(a) (model.net_loss_prob), and access_for_use divides by the same S(t). Not byte-identical
to v0.8, so the benchmark evidence is recomputed (validate_once.sh; ENGINE_NOTES.md, v0.9). Self-tests N0-N7.

v0.10: past SMC and a net type per past net campaign, as unit attributes. `smc_hist_years` (whole calendar years,
default []) runs SMC rounds in the unit's SMC months in each of those years, in every arm including the comparator,
never charged, through the same SMC intervention as the package's (model.SMCDelivery); `smc_hist_rounds` (one number,
or one per year) keeps the first n SMC months of a year. Every past round precedes the delivery period (fail closed).
`hist_net_by_year` gives a past campaign its own net type; campaigns it does not name, and routine campaigns, keep
hist_net. Past SMC puts the SMC parameters (smc_pe and smc_hi_frac to v0.10; the seven of v0.11), and a past dual-AI
campaign cfp_kill and cfp_half, in the burn-in key (they act before the fork). With the attributes absent or empty, a run is byte-identical to v0.9 (self-test H3;
benchmarks/restamp_proof.py). Self-tests H0-H7.

v0.11: the SMC mechanism from the literature (ENGINE_NOTES.md, v0.11; model.SMCDelivery). A child's per-round reach is
a persistent beta propensity (smc_cov, smc_rho; Malaria Consortium 2020 Table 21), a reached child completes the course
with probability smc_adh (Table 16) and is cleared of blood-stage infection, and new infections are blocked with
E0 exp(-(t/L)^k ln2) from the dose (smc_e0, smc_half, smc_k; Burgert 2022 Table 1), E0 x smc_partial after the day-1
dose only (Cairns 2022). smc_pe and smc_hi_frac are gone. Not byte-identical where SMC acts, so the benchmark
evidence is recomputed (validate_once.sh). Self-tests Q0-Q6 (selftest_late.py).

v0.12 (F6 S2-2): case management as an implementable package (ENGINE_NOTES.md, v0.12), with the fixed parameter
cm_target = 1. The add-on (`cm` component; cm_add, the gain inside the iCCM catchment, x cm_catchment_share) raises
effective treatment only in ages [cm_age_lo_m, cm_age_hi_m) months (default 2-59 months, the iCCM age band), ramped in
linearly over cm_ramp_years from the delivery start; severe-care access rises only by cm_severe_link x the add-on's
realised gain (default 0: no source gives a value). Two conditional cost drivers (card outputs.cost_drivers_when):
cm_child_years (child-years in the catchment, cm_catchment_share x the targeted ages, while the package's add-on runs,
per person; charged even at cm_add 0; priced by CM_COST_ITEMS, Collins 2014's recurrent total) and treated_cm_ages
(treated episodes in those ages, every arm). With cm_target 0 (the default) a run is byte-identical to v0.11
(self-test C6; benchmarks/restamp_proof.py). Self-tests C0-C11 (selftest_late.py).

v0.13 (F6 S2-3 part 2, critique C3): past SMC coverage apart from future coverage (ENGINE_NOTES.md, v0.13).
`smc_hist_cov` (one share in (0, 1], or one per smc_hist_years entry) is the mean per-round reach of the past SMC
rounds; the package's and routine SMC keep the parameter smc_cov. With it, smc_cov acts only after the fork and leaves
the burn-in key, so a calibration can fit the history without moving the coverage the allocation uses. smc_rho and the
child's persistent latent are shared by past and future rounds. Absent or empty, a run is byte-identical to v0.12
(self-test HC3; benchmarks/restamp_proof.py). Self-tests HC0-HC7 (selftest_late.py).
"""
from __future__ import annotations

import os
import sys
import time

import numpy as np
import pandas as pd
import yaml

if __package__:
    from ..contract import EngineCard, ParamSpec, UnitSpec, Package, Horizon, Outcomes, resolve_params, check_engine
    from ..horizon import delivery_period, delivery_problems, explicit_delivery
    from . import model as M
    from . import vector as V
else:  # pragma: no cover
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
    from toolkit.engines.contract import EngineCard, ParamSpec, UnitSpec, Package, Horizon, Outcomes, resolve_params, check_engine  # noqa: E402
    from toolkit.engines.horizon import delivery_period, delivery_problems, explicit_delivery  # noqa: E402
    from toolkit.engines.malaria import model as M, vector as V  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FITTED = os.path.join(HERE, "benchmarks", "fitted.yaml")
RESULTS = os.path.join(HERE, "benchmarks", "results.yaml")
AGES = ["u5", "5_14", "15p"]
GRIFFIN = "Griffin et al. 2010 PLoS Med 10.1371/journal.pmed.1000324; 2014 Nat Commun; 2015 Lancet Infect Dis"
A8 = "E7 attempt 8 value (models/malaria_abm.py VC), kept"
VERSION = "0.13"
# v0.12: case-management sources (ENGINE_NOTES.md, v0.12)
ORESANYA = "Oresanya et al. 2019 J Glob Health (PMC6594663), Niger State RAcE, Tables 5-6"
ISIGUZO = "Isiguzo et al. 2019 J Glob Health Rep (joghr.org article 12008), Abia State RAcE, Table 7"
OO = "Oo et al. 2019 Malar J (PMC6683427)"
# v0.12 (Codex r1 P2-3): the CM cost recipe. Collins et al. 2014 (Table 3) recurrent cost per child 2-59 months in the
# catchment per year (2010-2012 US$; 2.07-10.26 across seven countries; central 5) is a total that already includes
# medicines and supplies (3-26% of it), so it is charged on cm_child_years with no separate ACT / RDT item on the
# same component; its `includes` make toolkit/alloc/costs.py refuse one (self-test C10). Start-up ($202-352 per CHW)
# is a per_unit item the spec adds from its CHW count per unit.
CM_COST_ITEMS = [
    {"id": "cm_recurrent", "component": "cm", "value": 5.0, "price_year": 2012, "price_rule": "index",
     "basis": "per_outcome", "outcome": "cm_child_years",
     "includes": ["management", "supervision", "chw_recurrent", "medicines", "supplies"]},
]
# v0.11: SMC sources (ENGINE_NOTES.md, v0.11)
MC20 = ("Malaria Consortium 2021, Quantitative report on SMC supported by Malaria Consortium in 2020: coverage and "
        "quality in Burkina Faso, Chad, Nigeria, and Togo")
BURGERT = "Burgert et al. 2022 PLOS Glob Public Health 2:e0000211 (PMC10021282)"
BV = "Bertozzi-Villa et al. 2021 Nat Commun 10.1038/s41467-021-23707-7 (PMC8196080)"

# name, value, kind, units, lo, hi, source, verified
_PARAMS = [
    ("cfp_kill", 0.36, "fitted", "probability", 0.0, 0.85, "fitted: B10, B11", "unverified"),
    ("cfp_half", 2.5 * 365., "fixed", "days", None, None, A8, "unverified"),
    ("irs_kill", 0.5, "fitted", "probability", 0.0, 0.95, "fitted: B12, B12b, B12m", "unverified"),
    ("irs_rep", 0.10, "fixed", "probability", None, None, A8, "unverified"),
    ("irs_half", 0.4 * 365., "fixed", "days", None, None, A8 + " (decay of the generic short-lived product only)", "unverified"),
    ("irs_divert", 0.5, "fixed", "share", 0.0, 1.0, "assumption: share of IRS-deterred mosquitoes that bite outdoors instead of retrying", "unverified"),
    ("hh_size", 5.0, "fixed", "persons", None, None, "assumption (mean household size)", "unverified"),
    ("hh_net_corr", 0.5, "fixed", "correlation", 0.0, 0.9, "assumption: within-household correlation of the net-use latent", "unverified"),
    ("cov_corr", 0.3, "fixed", "correlation", 0.0, 0.9, "assumption: household correlation of net use and spraying", "unverified"),
    ("exp_corr", 0.2, "fixed", "correlation", 0.0, 0.6, "assumption: correlation of log biting rate with net non-use", "unverified"),
    ("irs_coverage", 0.8, "scenario", "share", None, None, "policy assumption (attempt 8 IRS_COVERAGE)", "unverified"),
    ("net_retention_half", 1.64 * 365., "fixed", "days", 1.33 * 365., 2.37 * 365., BV + ": LLIN median retention 1.64 y "
     "(IQR 1.33-2.37) across 40 countries (main text, Net retention time; Fig. 5; Supplementary Table 1.7); the median of "
     "the smooth-compact survival curve (v0.9; shape constant net_survival_k)", "matched"),
    ("net_survival_k", 20.0, "fixed", "shape", None, None, BV + ", Supplementary Note 1.4 (stock-and-flow model): "
     "smooth-compact loss exp(k - k / (1 - (t/tau)^2)) for t < tau, else 0, with k fixed at 20; the median retention is "
     "where the curve is 0.5", "matched"),
    ("use_rate", 0.871, "fixed", "share", 0.833, 0.901, BV + ": ITN use among those with access, all-country 87.1% (83.3-90.1)", "matched"),
    ("use_persist", 0.5, "fixed", "share", 0.0, 1.0, "assumption: share of the use gap that is persistent non-use (the rest is "
     "night-to-night variation)", "unverified"),
    ("net_holes", 1.0, "fixed", "switch", None, None, "holes in nets in use on (1) / off (0): " + V.HOLES_SOURCE, "matched"),
    # v0.11: SMC (ENGINE_NOTES.md, v0.11). smc_pe (a clinical effectiveness used as infection protection) and
    # smc_hi_frac (unsourced access groups) are gone
    ("smc_cov", 0.796, "fixed", "share", 0.521, 0.957, MC20 + ", Table 21 (Nigeria 2020, end-of-round, n = 7,898): "
     "cycles received none 6.0%, one 5.2%, two 13.3%, three 15.3%, four 60.2%; mean per-round reach of the beta-binomial "
     "fit (mean of the cycles 0.796). Range: ACCESS-SMC Partnership 2020 Lancet (PMC7718580) Table 1, Nigeria 2016 mean "
     "coverage per month 52.1%, to the Malaria Consortium 2022 report, Table 0, Nigeria cycle 2 day-1 coverage 95.7%",
     "matched"),
    ("smc_rho", 0.417, "fixed", "correlation", None, None, "intra-child correlation of the beta-binomial fitted by "
     "maximum likelihood to " + MC20 + ", Table 21 (predicts 5.5 / 7.4 / 10.3 / 16.6 / 60.1% for 0-4 cycles)", "matched"),
    ("smc_adh", 0.944, "fixed", "share", 0.842, 0.986, MC20 + ", Table 16: full three-day course among day-1 "
     "recipients, Nigeria end-of-round cycle 4, 94.4% (93.2-95.1). Range: ACCESS-SMC Nigeria 2015 key indicators "
     "84.2%; the Malaria Consortium 2021 report, Table 18, 98.6%", "matched"),
    ("smc_e0", 1.0, "fixed", "share", None, None, BURGERT + ", Table 1: SMC-SP+AQ initial efficacy 100% (pre-erythrocytic "
     "protection from infection, Eq 1)", "matched"),
    ("smc_half", 32.0, "fixed", "days", 24.0, 32.0, BURGERT + ", Table 1: SMC-SP+AQ half-life 32 days. Range: "
     "Braunack-Mayer et al. 2024 Lancet Glob Health (PMC10882206), Results: a liver-stage protection half-life of 24-30 "
     "days behaves like SP+AQ", "matched"),
    ("smc_k", 5.4, "fixed", "shape", None, None, BURGERT + ", Table 1: Weibull decay shape k = 5.4, E(t) = E0 "
     "exp(-(t/L)^k ln2) (Eq 1); Braunack-Mayer 2024 Table 1: 5.34", "matched"),
    ("smc_partial", 0.910, "fixed", "share", None, None, "Cairns et al. 2022 BMC Med 20:352 (PMC9540742), Results: 30-day "
     "protective efficacy 79.8% (33.7-93.8) with SP+AQ on day 1 only vs 87.7% with all three doses; 79.8 / 87.7. "
     "Assumption (Codex r1 P2-5): the ratio of two 30-day clinical PEs is applied as a multiplier on the initial "
     "infection-blocking efficacy, with the same decay and clearance", "unverified"),
    # v0.12: the point value stays 0.15 (v0.11 outputs unchanged); a run sets its own value and range in the spec
    ("cm_add", 0.15, "scenario", "probability", 0.0, 0.40, "enhanced CM, absolute gain in effective treatment. With "
     "cm_target 1 it is the gain inside the iCCM catchment, children 2-59 months (the unit-wide gain is cm_add x "
     "cm_catchment_share): ACT among confirmed cases +17 points, " + ORESANYA + "; appropriate treatment of the three "
     "iCCM illnesses +33 points, " + ISIGUZO + "; home management RR 1.89 (1.60-2.22) on appropriate treatment, " + OO
     + "; range about +0.15 to +0.40; 0 is the no-effect scenario. v0.11 and cm_target 0: a unit-wide gain for all "
     "ages, 0.15 (attempt 8 value, no source)", "unverified"),
    ("cm_catchment_share", 0.3, "fixed", "share", 0.2, 0.5, "share of the unit's children 2-59 months inside the iCCM "
     "catchment (cm_target 1). It scales the effect (unit-wide gain cm_add x share) and the children charged "
     "(cm_child_years) alike, because Collins et al. 2014 J Glob Health (iCCM costing tool, Table 3) price per child "
     "within catchments. Assumption: iCCM in Nigeria serves hard-to-reach communities 5 km or more from a facility "
     "(" + ORESANYA + "); no unit-level data; 0.2-0.5 assumed", "unverified"),
    ("cm_target", 0.0, "fixed", "switch", None, None, "v0.12 model switch: 1 = the case-management package (targeted "
     "ages, ramp, severe-care link, its cost drivers); 0 = the v0.11 add-on (all ages, from the delivery start, "
     "severe-care access raised one for one), kept so earlier results reproduce exactly", "unverified"),
    ("cm_age_lo_m", 2.0, "fixed", "months", None, None, "iCCM in Nigeria treats children 2-59 months (" + ORESANYA
     + "; " + ISIGUZO + ")", "unverified"),
    ("cm_age_hi_m", 60.0, "fixed", "months", None, None, "upper end (exclusive) of the iCCM age band, 2-59 months "
     "(as cm_age_lo_m); ages 5 and over get no gain", "unverified"),
    ("cm_ramp_years", 2.5, "fixed", "years", 2.0, 3.0, "assumption from programme timing: the RAcE endline gains "
     "were measured about three years after the start (2014 to February 2017; " + ORESANYA + "; " + ISIGUZO + "), so "
     "the gain ramps in linearly over 2-3 years from the delivery start", "unverified"),
    ("cm_severe_link", 0.0, "fixed", "share", 0.0, 1.0, "share of the add-on's gain in uncomplicated effective "
     "treatment that also raises severe-care access; no published estimate was found: " + OO + " pools no "
     "severe-malaria outcome, and iCCM refers severe cases rather than treating them. Default 0 (conservative): the "
     "add-on changes deaths only through transmission and post-treatment prophylaxis. 1 = the v0.11 one-for-one link",
     "unverified"),
    ("cfr_severe_treated", 0.10, "fixed", "probability", None, None, "AQUAMAT (attempt 8 C48)", "unverified"),
    ("cfr_severe_untreated", 0.30, "fixed", "probability", None, None, "assumption (attempt 8 C49)", "unverified"),
    ("severe_access_add", 0.25, "fixed", "probability", None, None, A8 + " (severe-care access above CM)", "unverified"),
    ("dw_moderate", 0.051, "fixed", "weight", None, None, "GBD 2019 disability weight, acute infectious episode, moderate", "unverified"),
    ("dw_severe", 0.133, "fixed", "weight", None, None, "GBD 2019 disability weight, acute infectious episode, severe", "unverified"),
    ("dur_moderate_d", 5.0, "fixed", "days", None, None, A8 + " (unit_model DUR_MOD)", "unverified"),
    ("dur_severe_d", 10.0, "fixed", "days", None, None, A8 + " (unit_model DUR_SEV)", "unverified"),
    ("le_u5", 87.0, "fixed", "years", None, None, "GBD 2019 reference life table e(x), mean age at death <5 (attempt 8)", "unverified"),
    ("le_5_14", 80.0, "fixed", "years", None, None, "GBD 2019 reference life table e(x), 5-14 (attempt 8)", "unverified"),
    ("le_15p", 54.0, "fixed", "years", None, None, "GBD 2019 reference life table e(x), 15+ (attempt 8)", "unverified"),
    ("import_eir", 0.1, "fixed", "infectious bites/person/year", None, None, A8, "unverified"),
    ("n_agents", 4000, "fixed", "agents", None, None, "numerical setting (population at horizon.start)", "unverified"),
    ("burnin_years", 50.0, "fixed", "years", None, None, "convergence evidence: benchmarks/equilibration.yaml (adult IC "
     "and ID by age within 5% when the burn-in is doubled, and independent of init_eir_per_m0, at four transmission "
     "levels); whole years, so the seasonal phase at horizon.start is unchanged", "unverified"),
    ("init_eir_per_m0", 0.6, "fixed", "infectious bites/person/year per unit m0", None, None,
     "assumption: the closed-form immunity guess at the start of the burn-in (EIR capped at 150); after the burn-in "
     "the starting immunity does not depend on it (benchmarks/equilibration.yaml)", "unverified"),
]
# v0.5: the burn-in starts with n_agents exp(-POP_GROWTH burnin_years) people and grows to about n_agents at
# horizon.start. Births 37 and deaths 11 per 1,000 per year (_demog); 0.0269 per year is the growth measured in the
# pre-v0.5 burn-in diagnostic (ENGINE_NOTES.md, v0.5) and checked by self-test V2.
POP_GROWTH = 0.0269
# Starsim 3.3.2's Births and Deaths add and remove no one in a sim that starts before 1900 (measured: 500 agents from
# 1890 or 1899 stay 500 for 5 years; from 1900, 73 births). A burn-in that would start earlier fails closed (V2c).
MIN_START_YEAR = 1900.0
# Parameters read only by package components (after the fork) or by the outcome accounting in the decision window:
# param sets that differ only in these share one burn-in (checked by V5). cfp_kill and cfp_half act through dual-AI
# nets, so they join the burn-in key when past campaigns used dual-AI nets (hist_net: dual).
POST_FORK = frozenset({"irs_kill", "irs_rep", "irs_half", "irs_coverage", *M.SMC_PARAM_NAMES, "cm_add",
                       *M.CM_PARAM_NAMES,
                       "cfr_severe_treated", "cfr_severe_untreated", "severe_access_add", "dw_moderate", "dw_severe",
                       "dur_moderate_d", "dur_severe_d", "le_u5", "le_5_14", "le_15p"})
DUAL_NET = frozenset({"cfp_kill", "cfp_half"})
# v0.10: SMC parameters act before the fork when a unit has past SMC (smc_hist_years), so they join its burn-in key
SMC_PARAMS = frozenset(M.SMC_PARAM_NAMES)      # v0.11: the seven SMC parameters
IMM_BANDS = (("0_5", 0.0, 5.0), ("5_15", 5.0, 15.0), ("15_30", 15.0, 30.0), ("30p", 30.0, 1e9), ("15p", 15.0, 1e9))
# v0.8: the monthly series run_series offers (toolkit/engines/hooks.py), each one of the model's daily records
# (model.DAILY) with the same definition as the window metric of the same name
SERIES = {
    "pfpr_u5": {"label": "microscopy prevalence, children 6-59 months (the calibration metric)",
                "units": "share", "aggregate": "mean", "denominator": "person aged 6-59 months"},
    "pfpr_2_10": {"label": "microscopy prevalence, ages 2-10", "units": "share", "aggregate": "mean",
                  "denominator": "person aged 2-10"},
    "pfpr_all": {"label": "microscopy prevalence, all ages", "units": "share", "aggregate": "mean",
                 "denominator": "person"},
    "inc_all": {"label": "clinical incidence, all ages", "units": "episodes per person-year", "aggregate": "rate",
                "denominator": "person-year"},
    "inc_u5": {"label": "clinical incidence, under 5", "units": "episodes per child-year (under 5)",
               "aggregate": "rate", "denominator": "person-year under 5"},
}


def _people(n):
    ages = np.arange(0, 86)
    import starsim as ss
    return ss.People(n_agents=n, age_data=pd.DataFrame({'age': ages, 'value': np.exp(-0.036 * ages)}))


def _demog():
    import starsim as ss
    return [ss.Births(birth_rate=ss.peryear(37)), ss.Deaths(death_rate=ss.peryear(11))]


def access_for_use(use: float, at_years: float, P: dict) -> tuple:
    """Net access at distribution that gives survey-reported nightly use `use` measured
    `at_years` after distribution: use = access x retention(t) x use_rate. Capped at 1;
    the flag is True when the reported use cannot be reached under the engine's retention
    and use rate."""
    ret = float(M.net_survival(at_years * 365.0, P["net_retention_half"], P["net_survival_k"]))   # v0.9: the sim's S(t)
    need = use / max(ret * P["use_rate"], 1e-9)
    return min(need, 1.0), bool(need > 1.0)


def _resistance(r):
    """Bioassay mortality: one value, or {species: value, default: value}."""
    if isinstance(r, dict):
        return {str(k): float(v) for k, v in r.items()}
    return float(r)


def default_smc_months(season):
    rain_peak = (int(np.argmax(season)) - 1) % 12 + 1      # season is rainfall lagged one month
    return tuple(((rain_peak - 2 + k) % 12) + 1 for k in range(4))


def smc_schedule_problems(pkg_rounds, routine_rounds, months, d0, d1, w1) -> list:
    """v0.7: problems with the SMC rounds of one arm, the package's (in the delivery period [d0, d1)) and the routine
    programme's (in [d1, w1)), as decimal times. Together they must be one continuous season sequence in `months`: no
    round given twice, no season missing between the first round and the window's end, no routine round inside the
    delivery period."""
    key = lambda t: round(float(t) * 12)         # noqa: E731 -- a round's month index (rounds are month starts)
    bad = []
    inside = [round(t, 3) for t in routine_rounds if d0 <= t < d1]
    if inside:
        bad.append(f"routine SMC round(s) at {inside[:4]} inside the delivery period [{d0:g}, {d1:g}): the decision "
                   "funds those seasons (smc_routine)")
    allr = [key(t) for t in list(pkg_rounds) + list(routine_rounds)]
    dup = sorted({k for k in allr if allr.count(k) > 1})
    if dup:
        bad.append(f"SMC round(s) at {[round(k / 12, 3) for k in dup[:4]]} given twice (package and routine SMC)")
    if allr:
        first = min(allr)
        y0 = first // 12
        want = {k for y in range(y0, int(np.ceil(w1)) + 1) for m in months
                for k in [y * 12 + (int(m) - 1)] if first <= k and k / 12 < w1}
        miss = sorted(want - set(allr))
        if miss:
            bad.append(f"SMC season(s) missing at {[round(k / 12, 3) for k in miss[:4]]} between the package's SMC and "
                       "routine SMC (smc_routine)")
    return bad


def _is_num(x) -> bool:
    return isinstance(x, (int, float, np.integer, np.floating)) and not isinstance(x, (bool, np.bool_))


def smc_history(a: dict, d0: float, months, later=()) -> dict | None:
    """v0.10: a unit's past SMC from smc_hist_years (whole calendar years) and smc_hist_rounds (optional: one number
    for every past year, or one per year; default every SMC month). A year's n rounds are at the month starts of the
    first n of the unit's SMC `months` in that year (model.smc_targets, as the package's SMC). Every round precedes
    the delivery start d0 and the first of the package's and routine SMC rounds `later` (Codex r1 P1-1). None when
    there is no past year (the v0.9 sim). Fails closed on any other form."""
    ys, rr, cv = a.get("smc_hist_years"), a.get("smc_hist_rounds"), a.get("smc_hist_cov")
    if ys is None:
        ys = []
    if not isinstance(ys, (list, tuple)):
        raise ValueError(f"smc_hist_years must be a list of whole calendar years (got {ys!r})")
    years = []
    for y in ys:
        if not _is_num(y) or not np.isfinite(float(y)) or float(y) != int(float(y)):
            raise ValueError(f"smc_hist_years: {y!r} is not a whole calendar year (the list is {list(ys)!r})")
        years.append(int(float(y)))
    if len(set(years)) != len(years):
        raise ValueError(f"smc_hist_years: a year is given twice ({years})")
    raw = a.get("smc_months")
    if years and raw is not None and not isinstance(raw, (list, tuple)):
        # Codex r2 P1: a falsy or non-list value must not fall back to the default months silently
        raise ValueError(f"smc_months {raw!r}: with past SMC (smc_hist_years) the SMC months must be a list of "
                         "distinct whole months 1-12 (or absent, for the default from the seasonality)")
    if years:     # Codex r1 P1-3: the months past SMC uses are whole, distinct calendar months (order kept)
        bad_m = [m for m in months if not _is_num(m) or not np.isfinite(float(m)) or float(m) != int(float(m))
                 or not 1 <= int(float(m)) <= 12]
        if bad_m or len({int(float(m)) for m in months}) != len(list(months)) or not list(months):
            raise ValueError(f"smc_months {list(months)!r}: with past SMC (smc_hist_years) the SMC months must be "
                             "distinct whole months 1-12")
    months = [int(float(m)) for m in months]

    def n_rounds(x):
        if not _is_num(x) or not np.isfinite(float(x)) or float(x) != int(float(x)) or not 1 <= int(x) <= len(months):
            raise ValueError(f"smc_hist_rounds: {x!r} must be a whole number of rounds from 1 to the unit's "
                             f"{len(months)} SMC months {months}")
        return int(x)
    if rr is None:
        ns = [len(months)] * len(years)
    elif isinstance(rr, (list, tuple)):
        if len(rr) != len(years):
            raise ValueError(f"smc_hist_rounds: {len(rr)} values for {len(years)} past SMC years (give one number, or "
                             "one per entry of smc_hist_years)")
        ns = [n_rounds(x) for x in rr]
    elif _is_num(rr):
        ns = [n_rounds(rr)] * len(years)
    else:
        raise ValueError(f"smc_hist_rounds must be a number or a list of numbers (got {rr!r})")

    def share(x):          # v0.13: a mean per-round reach, in smc_cov's domain (0, 1]
        if not _is_num(x) or not np.isfinite(float(x)) or not 0.0 < float(x) <= 1.0:
            raise ValueError(f"smc_hist_cov: {x!r} must be a share in (0, 1], the mean per-round reach of past SMC")
        return float(x)
    if cv is None or (isinstance(cv, (list, tuple)) and len(cv) == 0):
        cs = None                                  # every past round at smc_cov (v0.12)
    elif isinstance(cv, (list, tuple)):
        if len(cv) != len(years):
            raise ValueError(f"smc_hist_cov: {len(cv)} values for {len(years)} past SMC years (give one share, or "
                             "one per entry of smc_hist_years)")
        cs = [share(x) for x in cv]
    elif _is_num(cv):
        cs = [share(cv)] * len(years)
    else:
        raise ValueError(f"smc_hist_cov must be a share or a list of shares (got {cv!r})")
    if not years:
        return None
    pairs = sorted(zip(years, ns, cs or [None] * len(years)))
    rounds = [t for y, n, _ in pairs for t in M.smc_targets([y], months[:n])]
    covs = None if cs is None else [c for y, n, c in pairs for _ in M.smc_targets([y], months[:n])]
    first = min([float(d0)] + [float(t) for t in later])
    late = [round(t, 3) for t in rounds if t >= first]
    if late:
        raise ValueError(f"smc_hist_years: past SMC round(s) at {late[:4]} are at or after {first:g}, the "
                         f"delivery start {d0:g} or the first package or routine SMC round; past SMC precedes both")
    return {"years": [y for y, _, _ in pairs], "rounds_per_year": [n for _, n, _ in pairs], "months": months,
            "rounds": rounds, "covs": covs}


def hist_net_types(a: dict, hist_years: list) -> list | None:
    """v0.10: the net type of each past campaign (one per entry of itn_hist_years) from hist_net_by_year, a mapping
    {decimal year: pyr | pbo | dual} or the string "year:type year:type ..."; a campaign not named takes hist_net.
    None when the attribute is absent, empty or "none" (every past campaign gives hist_net: the v0.9 sim). Each year
    must be one of itn_hist_years (within 1e-6 y) and named once. Fails closed on any other form."""
    m = a.get("hist_net_by_year")
    if m is None or (isinstance(m, dict) and not m) or (isinstance(m, str) and m.strip().lower() in ("", "none")):
        return None
    if isinstance(m, str):
        pairs = []
        for tok in m.split():
            y, sep, t = tok.partition(":")
            if not sep or not y or not t:
                raise ValueError(f"hist_net_by_year: {tok!r} is not year:type (the string is {m!r})")
            pairs.append((y, t))
    elif isinstance(m, dict):
        pairs = list(m.items())
    else:
        raise ValueError(f"hist_net_by_year must be a mapping {{year: net type}} or 'year:type ...' (got {m!r})")
    default = a.get("hist_net", "pyr")
    out = [None] * len(hist_years)
    seen = []
    for y, t in pairs:
        try:
            yf = float("nan") if isinstance(y, bool) else float(y)
        except (TypeError, ValueError):
            yf = float("nan")
        if not np.isfinite(yf):
            raise ValueError(f"hist_net_by_year: {y!r} is not a decimal year")
        if t not in ("pyr", "pbo", "dual"):
            raise ValueError(f"hist_net_by_year: {y!r}: net type {t!r} is not pyr, pbo or dual")
        if any(abs(yf - z) <= 1e-6 for z in seen):
            raise ValueError(f"hist_net_by_year: year {yf:g} is given twice")
        seen.append(yf)
        hit = [i for i, h in enumerate(hist_years) if abs(float(h) - yf) <= 1e-6]
        if not hit:
            raise ValueError(f"hist_net_by_year: {yf:g} is not a past campaign year (itn_hist_years {list(hist_years)})")
        for i in hit:
            out[i] = t
    return [t if t is not None else default for t in out]


class MalariaEngine:
    def __init__(self, fitted_path: str = FITTED, results_path: str = RESULTS):
        self.fitted = (yaml.safe_load(open(fitted_path)) or {}).get("parameters", {}) if os.path.exists(fitted_path) else {}
        self.results_path = results_path
        self.fitted_path = fitted_path        # recorded as an input of the analyses that use the engine

    def parameters(self) -> list:
        out = []
        for name, val, kind, units, lo, hi, src, ver in _PARAMS:
            if kind == "fitted" and name in self.fitted:
                f = self.fitted[name]
                val, lo, hi = f["value"], f.get("lo", lo), f.get("hi", hi)
                src = f"fitted once to benchmarks {', '.join(f.get('benchmarks', []))} (benchmarks/fitted.yaml)"
            out.append(ParamSpec(name, float(val), kind, units, lo, hi, src, ver))
        return out

    def describe(self) -> EngineCard:
        bench = []
        if os.path.exists(self.results_path):
            bench = (yaml.safe_load(open(self.results_path)) or {}).get("benchmarks", [])
        return EngineCard(
            name="malaria-starsim", version=VERSION,
            mechanisms=[
                "Griffin-lite human natural history and immunity (fixed: " + GRIFFIN + ")",
                "species-specific vectors (Q0, phi_I, phi_B, mu0 from the D6 prior; endophily assumed)",
                "ITN: repeating, killing and feeding per attempt from published hut-trial relationships by bioassay "
                "mortality and species complex, pyrethroid and PBO, with resistance-dependent decay (" + V.CHURCHER_SOURCE
                + "); dual-AI adds a fitted chlorfenapyr kill",
                "IRS: deterrence on entry (a share diverted to outdoor biting) and killing of indoor-resting fed "
                "mosquitoes, product-specific decay (pirimiphos-methyl CS from the trial's wall cone bioassays)",
                "field use of nets: attrition by net age on the smooth-compact survival curve (median retention and "
                "shape k = 20, Bertozzi-Villa 2021; v0.9), use below access with nightly "
                "variation (use rate, same source), holes growing with net age (Briet 2020); survey use mapped to access",
                "households: correlated net use within households, correlated net use and spraying, "
                "biting heterogeneity correlated with net non-use",
                "ITN x IRS redundancy from shared indoor target, diversion and correlated coverage (no interaction factor)",
                "SMC (v0.11: beta-propensity reach, adherence, clearance at dosing, Weibull protection against infection); "
                "case management (effective treatment); v0.12 with cm_target 1: the add-on in declared ages (default "
                "2-59 months) with a linear ramp, severe-care access raised only through cm_severe_link (default 0)",
                "delivery period distinct from the outcome window: components act and cost drivers count only in "
                "delivery years; routine net campaigns after the package's campaign in every arm (v0.6)",
                "routine SMC in every arm after the delivery period, uncharged (v0.7)",
                "past SMC before the delivery period and a net type per past net campaign, in every arm, uncharged "
                "(v0.10); past SMC at its own per-round coverage, apart from the package's (v0.13)",
                "expected severe episodes and deaths; DALYs from GBD 2019 weights and reference life table",
                "microscopy prevalence by age band, including the ITN trials' own bands (under 5 from birth, under 10, "
                "15 and under, all ages; Pryce 2018 characteristics of included studies)"],
            components={"net": ["none", "pyr", "pbo", "dual"], "smc": [0, 1], "irs": [0, 1], "cm": [0, 1]},
            unit_attributes={
                "m0": "emergence density per person, [0.2, 400]", "season": "12 monthly values, mean 1",
                "resistance": "bioassay mortality [0, 1]", "species": "species shares",
                "cm_ft": "effective treatment [0, 0.95]", "itn_use": "net use after a campaign [0, 1]",
                "itn_hist_years": "past campaign years", "campaign_year": "decimal year of the package's campaign",
                "smc_months": "optional SMC months", "irs_month": "optional IRS month",
                "irs_years": "optional IRS round years", "irs_product": "pm_cs | short",
                "itn_use_at": "years after distribution at which itn_use was measured (default 0)",
                "hist_net": "net type of past campaigns (default pyr)",
                "itn_routine_interval": "years between routine net campaigns after campaign_year: every arm, net type "
                                        "hist_net, never charged (default: none)",
                "smc_routine": "0 | 1: routine SMC every season from the delivery period's end to the window's end in "
                               "the unit's SMC months: every arm, never charged; needs objective.delivery (default 0)",
                "smc_hist_years": "whole calendar years of past SMC before the delivery period, rounds in the unit's "
                                  "SMC months: every arm, never charged (default [])",
                "smc_hist_rounds": "optional rounds per past SMC year: one number, or one per smc_hist_years entry; "
                                   "the first n SMC months (default: every SMC month)",
                "smc_hist_cov": "optional mean per-round reach of past SMC, a share in (0, 1]: one number, or one per "
                                "smc_hist_years entry; the package's and routine SMC keep smc_cov (default: smc_cov; "
                                "v0.13)",
                "hist_net_by_year": "optional net type per past campaign, {year: pyr | pbo | dual} or 'year:type ...'; "
                                    "years from itn_hist_years; others hist_net (default none)"},
            outputs={"per_capita": ["cases", "severe", "deaths", "treated", "dalys"],
                     "windows": list(M.PFPR_BANDS) + ["inc_u5", "inc_all", "inc_u5_smc", "eir_annual",
                                                      "net_use", "net_access"],
                     "cost_drivers": ["nets_given", "smc_doses", "irs_sprayed", "treated_all"],
                     # v0.12: produced only with cm_target 1; the spec validator refuses a mapping to one otherwise
                     "cost_drivers_when": {"cm_child_years": {"cm_target": 1.0},
                                           "treated_cm_ages": {"cm_target": 1.0}},
                     "series": {k: dict(v) for k, v in SERIES.items()}},
            age_groups=AGES,
            valid_ranges={"m0": [0.2, 400], "resistance": [0.1, 1.0], "cm_ft": [0.0, 0.95], "eir_annual": [0.1, 300]},
            fitted={k: v for k, v in self.fitted.items()},
            benchmarks=bench,
            notes=["endophily per species is an unverified assumption (not in the D6 prior)",
                   "net effects use Churcher 2016 Table 6 coefficients (matched from the paper); the An. funestus PBO "
                   "row is read as a linear form (unverified reading)",
                   "household size and the three correlation parameters are unverified assumptions, fixed (not "
                   "fitted) and varied in the ENGINE_NOTES sensitivity scan",
                   "IRS product decay: pm_cs from Protopopoff 2018 cone bioassays (matched); short from attempt 8 (unverified)"])

    # ---------------------------------------------------------------- v0.5: equilibrated start, shared by packages
    # A run is a no-intervention burn-in of `burnin_years` before horizon.start (case management at the unit's
    # cm_ft, no vector control or chemoprevention), the unit's own history from horizon.start, and the package from
    # its first component event. run_batch runs the burn-in and the shared history once per (unit, seed, burn-in
    # parameters) and copies the sim for each package at the fork, so every package has an identical history
    # (common random numbers). run() is run_batch with one member.
    batch_shares_state = True
    # v0.6: honours horizon.delivery (toolkit/engines/horizon.py); the spec validator refuses the field otherwise
    supports_delivery = True
    # v0.8: the figure hooks' contract version (toolkit/engines/hooks.py HOOKS_CONTRACT)
    hooks_contract = "figures-hooks/1"

    def initialisation(self) -> dict:
        P = resolve_params(self, {})
        return {"method": f"no-intervention burn-in of {P['burnin_years']:g} years before horizon.start (case "
                          "management at the unit's cm_ft), from a closed-form immunity guess (init_eir_per_m0); the "
                          "population starts at n_agents exp(-0.0269 burnin_years) and grows to n_agents; packages "
                          "share the burn-in and the history up to their first component event",
                "evidence": "benchmarks/equilibration.yaml"}

    @staticmethod
    def _routine_years(a: dict, horizon: Horizon) -> list:
        """v0.6: routine (non-decision) net campaigns at campaign_year + k x itn_routine_interval, k >= 1, before the
        outcome window ends; [] without the attribute. They run in every arm, so they belong to the shared history."""
        iv = a.get("itn_routine_interval")
        if iv is None:
            return []
        iv = float(iv)
        if not iv > 0:
            raise ValueError(f"itn_routine_interval must be > 0 years (got {iv:g})")
        d0, d1 = delivery_period(horizon)
        cy, w1 = float(a.get("campaign_year", d0)), float(horizon.window[1])
        out = [cy + k * iv for k in range(1, int(np.ceil((w1 - cy) / iv)) + 1) if cy + k * iv < w1]
        inside = [y for y in out if d0 <= y < d1]
        if inside:
            raise ValueError(f"routine net campaign(s) at {inside} fall inside the delivery period [{d0:g}, {d1:g}): "
                             "the decision funds those years (itn_routine_interval)")
        return out

    @staticmethod
    def _smc_routine(a: dict, horizon: Horizon) -> dict | None:
        """v0.7: the routine SMC programme of a unit with smc_routine = 1: every season in the unit's SMC months from
        the delivery period's end to the outcome window's end, in every arm, never charged. None when the attribute
        is absent or 0 (the v0.6 sim). Needs an explicit delivery period (fail closed)."""
        v = a.get("smc_routine")
        if v is None:
            return None
        try:
            f = float(v)
        except (TypeError, ValueError):
            f = float("nan")
        if isinstance(v, bool) or f not in (0.0, 1.0):
            raise ValueError(f"smc_routine must be 0 or 1 (got {v!r})")
        if f == 0.0:
            return None
        if not explicit_delivery(horizon):
            raise ValueError("smc_routine = 1 needs a delivery period (objective.delivery): routine SMC runs after the "
                             "delivery period ends, and without one the package's SMC covers the whole window")
        d0, d1 = delivery_period(horizon)
        w1 = float(horizon.window[1])
        season = np.asarray(a.get("season", [1.0] * 12), float)
        months = tuple(a.get("smc_months") or default_smc_months(season))
        years = list(range(int(np.floor(d1)), int(np.ceil(w1)) + 1))
        return {"years": years, "months": months, "after": float(d1), "before": w1,
                "rounds": M.smc_targets(years, months, float(d1), w1)}

    @classmethod
    def delivery_plan(cls, a: dict, components: dict, horizon: Horizon) -> dict:
        """v0.8: the one source of a unit's delivery schedule in one arm, read by the sim (_plan for the package's
        components, _base_sim for the past and routine campaigns, routine SMC and the add-on period) and by
        schedule(). Pure: no sim, no random numbers. Fails closed as _plan did up to v0.7, in the same order.
        v0.6: the package's components act in the delivery period (the decision window unless horizon.delivery is
        given)."""
        bad = delivery_problems(horizon)
        if bad:
            raise ValueError("; ".join(bad))
        comp = {"net": "none", "smc": 0, "irs": 0, "cm": 0}
        comp.update(components)
        season = np.asarray(a.get("season", [1.0] * 12), float)
        d0, d1 = delivery_period(horizon)
        ex = explicit_delivery(horizon)
        years = list(range(int(np.floor(d0)), int(np.ceil(d1))))
        plan = {"net": None, "smc": None, "irs": None, "fork_events": [float(d0)],
                # the add-on acts in the delivery period; without one it starts at the window and has no end (v0.5)
                "cm": {"on": bool(int(comp["cm"])), "start": d0, "stop": d1 if ex else None}}
        # (intervention, component, label, kind, target) of every delivery: schedule() projects them onto the sim's
        # daily steps with model.event_steps, as the interventions do
        ev = []
        timed = []                                   # (component, decimal year) of every event the decision pays for
        if comp["net"] != "none":
            cy = float(a.get("campaign_year", d0))
            plan["net"] = {"years": [cy], "net_type": comp["net"]}
            plan["fork_events"].append(cy)
            timed.append(("net campaign (campaign_year)", cy))
            ev.append(("itn_new", "net", f"net campaign ({comp['net']})", "decision", cy))
        if int(comp["smc"]):
            months = tuple(a.get("smc_months") or default_smc_months(season))
            plan["smc"] = {"years": years, "months": months}
            ts = M.smc_targets(years, months)
            plan["fork_events"] += ts
            timed += [("SMC round", t) for t in ts]
            ev += [("smc", "smc", "SMC round", "decision", t) for t in ts]
        if int(comp["irs"]):
            month = int(a.get("irs_month") or ((int(np.argmax(season)) - 3) % 12 + 1))
            irs_years = [int(np.floor(float(y))) for y in a.get("irs_years", years)]
            plan["irs"] = {"years": irs_years, "month": month}
            ts = M.irs_targets(irs_years, month)
            plan["fork_events"] += ts
            timed += [("IRS round", t) for t in ts]
            ev += [("irs", "irs", "IRS round", "decision", t) for t in ts]
        if ex:
            out = [f"{c} at {t:.3f}" for c, t in timed if not d0 <= t < d1]
            if out:
                raise ValueError(f"outside the delivery period [{d0:g}, {d1:g}): {', '.join(out[:4])}")
        plan["hist_years"] = [float(y) for y in a.get("itn_hist_years", [])]
        plan["hist_net"] = a.get("hist_net", "pyr")
        plan["hist_nets"] = hist_net_types(a, plan["hist_years"])   # v0.10: None, or one net type per past campaign
        plan["routine_years"] = cls._routine_years(a, horizon)   # validated here, before any sim is built
        rs = cls._smc_routine(a, horizon)            # v0.7: and routine SMC with the package's own rounds
        if rs is not None:
            pkg = [t for c, t in timed if c == "SMC round"]
            bad = smc_schedule_problems(pkg, rs["rounds"], rs["months"], d0, d1, float(horizon.window[1]))
            if bad:
                raise ValueError("; ".join(bad))
        plan["smc_routine"] = rs
        # v0.10: past SMC in the unit's SMC months, before the delivery period (None without smc_hist_years)
        later = [t for c, t in timed if c == "SMC round"] + list((rs or {}).get("rounds", []))
        plan["smc_hist"] = smc_history(a, float(d0), a.get("smc_months") or default_smc_months(season), later)
        # one intervention (itn_hist) runs past and routine campaigns, in this order (as _base_sim builds it)
        hn = plan["hist_nets"] or [plan["hist_net"]] * len(plan["hist_years"])
        ev += [("itn_hist", "net", f"past net campaign ({n})", "history", t) for t, n in zip(plan["hist_years"], hn)]
        ev += [("itn_hist", "net", f"routine net campaign ({plan['hist_net']})", "routine", t)
               for t in plan["routine_years"]]
        ev += [("smc_routine", "smc", "routine SMC round", "routine", t) for t in (rs or {}).get("rounds", [])]
        ev += [("smc_hist", "smc", "past SMC round", "history", t) for t in (plan["smc_hist"] or {}).get("rounds", [])]
        plan["deliveries"] = ev
        # where cost drivers count (model.delivery_contains): the delivery period, or the window when none is given
        w0, w1 = (float(x) for x in horizon.window)
        plan["accounting"] = {"del_start": d0 if ex else None, "del_stop": d1 if ex else None,
                              "win_start": w0, "win_stop": w1}
        return plan

    def _plan(self, P: dict, a: dict, components: dict, horizon: Horizon) -> dict:
        """The package's component settings and the decimal years at which they first act (from delivery_plan)."""
        dp = self.delivery_plan(a, components, horizon)
        return {"cm_add": P["cm_add"] if dp["cm"]["on"] else 0.0, "cm_on": dp["cm"]["on"], "net": dp["net"],
                "smc": dp["smc"],
                "irs": dp["irs"], "events": dp["fork_events"]}

    def schedule(self, params, unit: UnitSpec, package: Package, horizon: Horizon) -> list:
        """v0.8 figure hook (toolkit/engines/hooks.py): the unit's delivery events in the arm `package` over
        [horizon.start, window end), from delivery_plan, the code that configures the sim. Pure. A past campaign
        before horizon.start (it acts in the burn-in) and a round after the window's end are not listed."""
        P = resolve_params(self, params)             # unknown parameters fail closed, as in run()
        dp = self.delivery_plan(dict(unit.attributes), package.components, horizon)
        return self.project_schedule(dp, self._yearvec(P, horizon), horizon)

    @staticmethod
    def _yearvec(P: dict, horizon: Horizon) -> np.ndarray:
        """The sim's daily time vector (decimal years), from a Starsim Timeline with the sim's start, stop and step:
        no sim is built and no random number drawn (self-test S1 checks the projection against the sim's steps)."""
        import starsim as ss
        B = int(round(float(P["burnin_years"])))
        tl = ss.Timeline(start=float(horizon.start) - B, stop=float(horizon.stop), dt=ss.days(1))
        tl.init()
        return np.asarray(tl.yearvec)

    @staticmethod
    def project_schedule(dp: dict, yv, horizon: Horizon) -> list:
        """The delivery plan's targets as the sim executes them: each target on its daily step (model.event_steps),
        targets of one intervention on one step as one delivery, t0 the step's year. Fails closed when a decision
        delivery falls on a step where the sim does not charge it (model.delivery_contains), so `charged` matches
        the cost drivers. Lists the steps in [horizon.start, window end)."""
        yv = np.asarray(yv)
        st, w1 = float(horizon.start), float(horizon.window[1])
        acc, seen, out, unc = dp["accounting"], set(), [], []
        dlo, dhi = delivery_period(horizon)          # hooks contract: history before it, routine outside it
        for grp, comp, label, kind, t in dp["deliveries"]:
            steps = M.event_steps(yv, [t])
            if not steps or (grp, next(iter(steps))) in seen:
                continue                             # outside the sim, or a delivery already on this step
            ti = next(iter(steps))
            seen.add((grp, ti))
            yr = float(yv[ti])
            # #127 r3: boundaries are compared exactly. A step that differs from a boundary by representation only
            # (<= 1e-9 y, far below a day) is reported at the boundary's own float, but only when every membership
            # test (charging by the sim's own delivery_contains, coverage, before / inside the delivery period) gives
            # the same answer for both, so the event is never moved across a boundary.
            sides = lambda x: (M.delivery_contains(x, **acc), st <= x < w1, x < dlo, dlo <= x < dhi)  # noqa: E731
            for bnd in (dlo, dhi, st, w1):
                if yr != bnd and abs(yr - bnd) <= 1e-9 and sides(bnd) == sides(yr):
                    yr = float(bnd)
                    break
            if kind == "decision" and not M.delivery_contains(yr, **acc):
                unc.append(f"{label} at {yr:.3f}")
            if kind == "history" and yr >= dlo:
                unc.append(f"{label} at {yr:.3f} (a past campaign at or after the delivery start, {dlo:g})")
            if kind == "routine" and dlo <= yr < dhi:
                unc.append(f"{label} at {yr:.3f} (a routine delivery inside the delivery period)")
            if st <= yr < w1:
                out.append({"component": comp, "label": label, "kind": kind, "t0": yr, "t1": None,
                            "charged": kind == "decision"})
        lo, hi = ((acc["del_start"], acc["del_stop"]) if acc["del_start"] is not None
                  else (acc["win_start"], acc["win_stop"]))
        if unc:
            raise ValueError(f"schedule: deliveries outside their kind's period (decision events must be charged: "
                             f"cost drivers count in [{lo:g}, {hi:g}); history events precede the delivery period; "
                             f"routine events fall outside it): {', '.join(unc[:4])}")
        if dp["cm"]["on"]:
            out.append({"component": "cm", "label": "enhanced case management (add-on)", "kind": "decision",
                        "t0": float(lo), "t1": float(hi), "charged": True})
        return sorted(out, key=lambda x: x["t0"])

    @staticmethod
    def _burnin_key(P: dict, a: dict) -> str:
        import json
        # v0.10: dual-AI nets in any past campaign (hist_net_by_year) and past SMC act before the fork, so their
        # parameters join the key (delivery_plan validated both attributes before this)
        hn = hist_net_types(a, [float(y) for y in a.get("itn_hist_years", [])]) or []
        dual = a.get("hist_net", "pyr") == "dual" or "dual" in hn
        skip = POST_FORK | (frozenset() if dual else DUAL_NET)
        if a.get("smc_hist_years"):
            skip = skip - SMC_PARAMS
            # v0.13: with their own coverage the past rounds do not read smc_cov, which then acts only after the fork
            season = np.asarray(a.get("season", [1.0] * 12), float)
            if (smc_history(a, float("inf"), a.get("smc_months") or default_smc_months(season)) or {}).get("covs"):
                skip = skip | {"smc_cov"}
        if isinstance(a.get("hist_net_by_year"), dict):   # Codex r1 P2-5: keys of mixed type cannot be sorted
            a = dict(a, hist_net_by_year=sorted([float(k), str(v)] for k, v in a["hist_net_by_year"].items()))
        return json.dumps({"P": {k: v for k, v in P.items() if k not in skip}, "unit": a}, sort_keys=True,
                          default=lambda x: x.tolist() if hasattr(x, "tolist") else repr(x))   # arrays in full (review P3)

    def _base_sim(self, P: dict, a: dict, horizon: Horizon, seed: int, cfg: dict | None = None):
        """The sim from horizon.start - burnin_years (not yet initialised), with every component slot present and
        inert unless `cfg` sets it; returns (sim, n0, access_capped)."""
        import starsim as ss
        B = float(P["burnin_years"])
        if B < 0 or abs(B - round(B)) > 1e-9:
            raise ValueError(f"burnin_years must be a whole number of years >= 0 (got {B:g})")
        B = int(round(B))
        if float(horizon.start) - B < MIN_START_YEAR:
            raise ValueError(f"the sim would start in {float(horizon.start) - B:g} (horizon.start {horizon.start:g} - "
                             f"burnin_years {B}); Starsim 3.3.2 births and deaths do nothing before {MIN_START_YEAR:g}, "
                             "so the population would not grow: shorten the burn-in or move the horizon")
        M.smc_check(P)                               # v0.11: SMC parameters outside their domain fail closed
        M.cm_check(P)                                # v0.12: and the case-management options
        n = int(P["n_agents"])
        n0 = n if B == 0 else max(int(round(n * np.exp(-POP_GROWTH * B))), 50)
        season = np.asarray(a.get("season", [1.0] * 12), float)
        w0, w1 = horizon.window
        dp = self.delivery_plan(a, {}, horizon)      # v0.8: the history, routine, add-on and accounting (one source)
        m0 = float(a.get("m0", 5.0))
        dis = M.Malaria(M0=m0, season=season, resistance=_resistance(a.get("resistance", 0.5)),
                        ft=float(a.get("cm_ft", 0.3)), species=a.get("species"), P=P,
                        init_eir=min(P["init_eir_per_m0"] * m0, 150.0), win_start=w0, win_stop=w1,
                        smc_months=tuple(a.get("smc_months") or default_smc_months(season)),
                        import_eir=P["import_eir"], irs_product=a.get("irs_product", "pm_cs"),
                        rec_year=float(horizon.start), del_start=dp["accounting"]["del_start"],
                        del_stop=dp["accounting"]["del_stop"])
        use, capped = access_for_use(float(a.get("itn_use", 0.55)), float(a.get("itn_use_at", 0.0)), P)
        # v0.6: the add-on acts in the delivery period; without one it starts at the window and has no end (v0.5)
        itv = [M.CaseManagement(base=float(a.get("cm_ft", 0.3)), add=0.0, start=dp["cm"]["start"],
                                stop=dp["cm"]["stop"], name="cm")]
        hist = dp["hist_years"] + dp["routine_years"]
        if hist:
            # v0.10: a net type per past campaign when hist_net_by_year is given (routine campaigns keep hist_net)
            nts = None if dp["hist_nets"] is None else dp["hist_nets"] + [dp["hist_net"]] * len(dp["routine_years"])
            itv.append(M.ITNCampaign(years=hist, use=use, net_type=dp["hist_net"], name="itn_hist", net_types=nts))
        itv += [M.ITNCampaign(years=[], use=use, net_type="pyr", count=True, name="itn_new"),
                M.SMCDelivery(years=[], P=P, name="smc"),
                M.IRSRound(years=[], coverage=P["irs_coverage"], name="irs")]
        rs = dp["smc_routine"]
        if rs is not None:     # v0.7: built only when set, last, so a unit without it gets the v0.6 sim exactly
            itv.append(M.SMCDelivery(years=rs["years"], months=rs["months"], P=P, count=False, after=rs["after"],
                                     before=rs["before"], name="smc_routine"))
        sh = dp["smc_hist"]
        if sh is not None:     # v0.10: built only when set, last, so a unit without it gets the v0.9 sim exactly
            # Codex r1 P1-2: every past round runs on a step before the delivery start (the steps the sim uses)
            yv = self._yearvec(P, horizon)
            d0 = float(delivery_period(horizon)[0])
            late = sorted(round(float(yv[i]), 6) for i in M.event_steps(yv, sh["rounds"]) if float(yv[i]) >= d0)
            if late:
                raise ValueError(f"smc_hist_years: past SMC round(s) run on step(s) {late[:4]}, at or after the "
                                 f"delivery start {d0:g}")
            kw = {} if sh["covs"] is None else {"covs": sh["covs"]}     # v0.13: past coverage (smc_hist_cov)
            itv.append(M.SMCDelivery(years=sh["years"], months=sh["months"], P=P, count=False, times=sh["rounds"],
                                     name="smc_hist", **kw))
        if cfg is not None:
            self._configure({i.name: i for i in itv}, None, P, cfg)
        sim = ss.Sim(people=_people(n0), start=float(horizon.start) - B, stop=float(horizon.stop),
                     dt=ss.days(1), diseases=dis, demographics=_demog(), interventions=itv,
                     rand_seed=int(seed), verbose=0)
        return sim, n0, capped

    @staticmethod
    def _configure(itv, dz, P: dict, cfg: dict) -> None:
        """Set the package's components (and, after a fork, the member's parameters) on the sim's interventions."""
        if dz is not None:
            dz.P = dict(P)
        itv["cm"].add = float(cfg["cm_add"])
        itv["cm"].pkg = bool(cfg.get("cm_on", False))    # v0.12: the package has the add-on (its cost drivers)
        net, smc, irs = itv["itn_new"], itv["smc"], itv["irs"]
        net.years = list(cfg["net"]["years"]) if cfg["net"] else []
        net.code = V.NET_CODE[cfg["net"]["net_type"]] if cfg["net"] else V.NET_CODE["pyr"]
        smc.years = list(cfg["smc"]["years"]) if cfg["smc"] else []
        if cfg["smc"]:
            smc.months = list(cfg["smc"]["months"])
        smc.set_smc(P)
        if "smc_routine" in itv:   # v0.7: routine rounds act after the fork, with the member's own SMC parameters
            itv["smc_routine"].set_smc(P)
        if "smc_hist" in itv:      # v0.10: the burn-in key holds the SMC parameters, so this is the shared value
            itv["smc_hist"].set_smc(P)
        irs.years = list(cfg["irs"]["years"]) if cfg["irs"] else []
        if cfg["irs"]:
            irs.month = int(cfg["irs"]["month"])
        irs.cov = P["irs_coverage"]
        for it in (net, smc, irs):
            it.__dict__.pop("_tis", None)          # event steps are recomputed from the new years

    def build_sim(self, params, unit: UnitSpec, package: Package, horizon: Horizon, seed: int):
        """The Starsim sim of one run (not yet run, burn-in included), and the resolved parameters."""
        P = resolve_params(self, params)
        a = dict(unit.attributes)
        sim, _, self._access_capped = self._base_sim(P, a, horizon, seed, self._plan(P, a, package.components, horizon))
        return sim, P

    def run(self, params, unit: UnitSpec, package: Package, horizon: Horizon, seed: int) -> Outcomes:
        return self.run_batch([params], unit, [package], horizon, seed)[0]

    def run_batch(self, param_sets, unit, packages, horizon: Horizon, seed: int) -> list:
        """Outcomes of many (parameter set, package) pairs at one seed; `unit` is one UnitSpec or one per pair,
        `packages` one Package or one per pair. Pairs whose burn-in inputs agree (the unit and every parameter outside
        POST_FORK) share one burn-in and one history up to the earliest component event of any of them; each pair is
        then continued from an exact copy. Each pair's outcomes equal run() of that pair (self-test V4)."""
        B = len(param_sets)
        units = list(unit) if isinstance(unit, (list, tuple)) else [unit] * B
        pkgs = list(packages) if isinstance(packages, (list, tuple)) else [packages] * B
        if B == 0 or len(units) != B or len(pkgs) != B:
            raise ValueError("run_batch: give one unit (or one per pair) and one package (or one per pair)")
        return self._run_members(param_sets, units, pkgs, horizon, seed)[0]

    def run_series(self, params, unit: UnitSpec, packages: list, horizon: Horizon, seed: int, metrics: list,
                   step: str = "month") -> list:
        """v0.8 figure hook (toolkit/engines/hooks.py): monthly series of `metrics` (keys of the card's
        outputs["series"]) for each package, in order, at one seed. The packages share the burn-in and the history
        (run_batch's member loop); the series are read from the model's daily records, so the sims are run_batch's."""
        if step != "month":
            raise ValueError(f"run_series: step {step!r} is not supported (month)")
        miss = [m for m in metrics if m not in SERIES]
        if miss or not metrics:
            raise ValueError(f"run_series: metrics {miss or metrics} are not in the card's series {sorted(SERIES)}")
        pkgs = list(packages)
        if not pkgs:
            raise ValueError("run_series: give at least one package")
        _, ser = self._run_members([params] * len(pkgs), [unit] * len(pkgs), pkgs, horizon, seed,
                                   series=list(metrics))
        return ser

    @staticmethod
    def series_steps(horizon: Horizon) -> tuple:
        """(starts, ends) of the monthly steps: the contract's grid (hooks.monthly_grid, figures-hooks/1), whose
        last step ends at the window's end (a partial step when the window ends inside a month)."""
        if __package__:
            from ..hooks import monthly_grid
        else:  # pragma: no cover
            from toolkit.engines.hooks import monthly_grid
        t = [float(x) for x in monthly_grid(horizon)]
        if not t:
            raise ValueError("run_series: the outcome window ends at or before horizon.start (no monthly step)")
        return t, t[1:] + [float(horizon.window[1])]

    @staticmethod
    def _series(sim, horizon: Horizon, metrics: list, pid: str) -> dict:
        """One package's monthly series, with each step's end (t_end) and its number of simulated days (days). A
        "mean" metric is the mean of the step's daily values; a "rate" metric the step's sum of daily episodes per
        person over its length in years. A step's days are those a named window over the same span selects
        ((yv >= a) & (yv < b), as _outcomes; found once for all steps with searchsorted), so window metrics are the
        day-weighted (mean) or length-weighted (rate) sums of the steps they contain (series_window)."""
        yv = np.asarray(sim.t.yearvec)
        d = sim.diseases.malaria.daily
        t, ends = MalariaEngine.series_steps(horizon)
        idx = np.searchsorted(yv, np.asarray(t + ends[-1:], float), side="left")
        vals, days = {m: [] for m in metrics}, []
        for k, (a, b) in enumerate(zip(t, ends)):
            lo, hi = int(idx[k]), int(idx[k + 1])
            if hi <= lo:
                raise ValueError(f"run_series: no simulated day in the step [{a:.4f}, {b:.4f})")
            days.append(hi - lo)
            for m in metrics:
                x = d[m][lo:hi]
                vals[m].append(float(x.mean()) if SERIES[m]["aggregate"] == "mean" else float(x.sum() / (b - a)))
        return {"package": pid, "t": t, "t_end": list(ends), "days": days, "values": vals}

    @staticmethod
    def series_window(entry: dict, metric: str, a: float, b: float) -> float:
        """The window metric over [a, b) from one run_series entry: the day-weighted mean of the steps' values
        ("mean") or the sum of rate x step length ("rate", episodes per person over the window, as the window
        metrics inc_u5 / inc_all). Exact when [a, b) is a union of steps. Fails closed (Codex r2) when [a, b) reaches
        outside the entry's coverage [t[0], t_end[-1]), when it cuts through a step (the step's days cannot be split),
        or when the steps inside do not cover it completely."""
        t, te = np.asarray(entry["t"], float), np.asarray(entry["t_end"], float)
        n, v = np.asarray(entry["days"], float), np.asarray(entry["values"][metric], float)
        tol = 1e-9
        if not (b > a and a >= t[0] - tol and b <= te[-1] + tol):
            raise ValueError(f"series_window: [{a:g}, {b:g}) is not inside the series' coverage "
                             f"[{t[0]:g}, {te[-1]:g})")
        inside = (t >= a - tol) & (te <= b + tol)
        cut = (t < b - tol) & (te > a + tol) & ~inside
        if cut.any():
            raise ValueError(f"series_window: [{a:g}, {b:g}) cuts through the monthly step(s) starting at "
                             f"{[round(x, 4) for x in t[cut][:3]]}")
        ts, es = t[inside], te[inside]
        if (not inside.any() or abs(ts[0] - a) > tol or abs(es[-1] - b) > tol
                or np.any(np.abs(ts[1:] - es[:-1]) > tol)):
            raise ValueError(f"series_window: the whole steps inside [{a:g}, {b:g}) do not cover it completely "
                             "(coverage)")
        if SERIES[metric]["aggregate"] == "mean":
            return float((v * n)[inside].sum() / n[inside].sum())
        return float((v * (te - t))[inside].sum())

    def _run_members(self, param_sets, units, pkgs, horizon: Horizon, seed: int, series: list | None = None):
        """run_batch's member loop: (outcomes, series) per (parameter set, unit, package); series is None per member
        unless `series` lists metrics (v0.8), which are read from the daily records after the run and change
        nothing in it (self-test S7)."""
        import gc
        import pickle
        B = len(param_sets)
        mem = []
        for p, u, k in zip(param_sets, units, pkgs):
            P = resolve_params(self, p)
            M.smc_check(P)          # v0.11: every member, not only the one whose parameters build the shared burn-in
            M.cm_check(P)           # v0.12: likewise
            a = dict(u.attributes)
            mem.append((P, a, self._plan(P, a, k.components, horizon)))
        groups = {}
        for i, (P, a, _) in enumerate(mem):
            groups.setdefault(self._burnin_key(P, a), []).append(i)
        out, ser = [None] * B, [None] * B
        for idx in groups.values():
            t0 = time.time()
            P0, a0, _ = mem[idx[0]]
            sim, n0, capped = self._base_sim(P0, a0, horizon, seed)
            sim.init()
            yv = np.asarray(sim.t.yearvec)
            tf = min(t for i in idx for t in mem[i][2]["events"])
            ti_f = int(np.argmin(np.abs(yv - tf)))          # the same nearest-step rule as the components' events
            if ti_f >= 1:
                sim.run(until=yv[ti_f - 1])
            # one snapshot, restored per member (faster than copy.deepcopy of a Starsim sim); the last member continues
            # the original
            snap = pickle.dumps(sim, protocol=pickle.HIGHEST_PROTOCOL) if len(idx) > 1 else None
            shared = (time.time() - t0) / len(idx)
            for n, i in enumerate(idx):
                t1 = time.time()
                s = sim if n == len(idx) - 1 else pickle.loads(snap)
                P, a, cfg = mem[i]
                self._configure(s.interventions, s.diseases.malaria, P, cfg)
                s.run()
                dz = s.diseases.malaria
                out[i] = self._outcomes(s, P, horizon, seed, t1 - shared, capped, extra={
                    "burnin_years": int(round(P["burnin_years"])), "n_burnin_start": n0, "n_start": dz.n_start,
                    "fork_year": round(float(yv[ti_f]), 6), "burnin_members": len(idx),
                    "start_state_sha256": dz.start_sha})
                if series:
                    ser[i] = self._series(s, horizon, series, pkgs[i].id)
                if n == len(idx) - 2:
                    snap = None                # the last member continues the original sim
                try:
                    dz.daily, dz.mos = {}, []
                    s.shrink(size_limit=None, die=False)   # Starsim keeps finished sims alive: drop their people and loop
                except Exception:  # noqa: BLE001 -- best effort; the outcomes are already computed
                    pass
                del s, dz
                gc.collect()                   # Starsim sims hold reference cycles: free each member before the next
            del sim
            gc.collect()
        return out, ser

    def initial_state(self, params, unit: UnitSpec, package: Package | None = None, horizon: Horizon | None = None,
                      seed: int = 1) -> dict:
        """The state at horizon.start after the burn-in (no package acts before it): population, mean immunity by
        age band (IB, IC, ID, IV) and microscopy prevalence. For the equilibration evidence and self-tests."""
        if horizon is None:
            raise ValueError("initial_state needs the horizon: the state is the one at horizon.start")
        P = resolve_params(self, params)
        a = dict(unit.attributes)
        sim, n0, _ = self._base_sim(P, a, horizon, seed)
        sim.init()
        yv = np.asarray(sim.t.yearvec)
        ti = int(np.argmin(np.abs(yv - float(horizon.start))))
        if ti >= 1:
            sim.run(until=yv[ti - 1])
        dz = sim.diseases.malaria
        u = sim.people.auids
        age = np.asarray(sim.people.age[u], float)
        imm = {nm: {} for nm in ("IB", "IC", "ID", "IV")}
        for nm in imm:
            v = np.asarray(getattr(dz, nm)[u], float)
            for b, lo, hi in IMM_BANDS:
                s = (age >= lo) & (age < hi)
                imm[nm][b] = float(v[s].mean()) if s.any() else float("nan")
        st = np.asarray(dz.st[u])
        det = ((st == M.D_) | (st == M.T_)).astype(float)
        q = dz._detect(age * 365)
        det[st == M.A_] = q[st == M.A_]
        return {"N": int(len(u)), "N0": int(n0), "year": float(yv[ti]), "burnin_years": int(round(P["burnin_years"])),
                "init_eir": min(P["init_eir_per_m0"] * float(a.get("m0", 5.0)), 150.0), "immunity": imm,
                "pfpr": M.prevalence_bands(det, age * 365),
                "hh_mean_size": float(len(u) / max(len(np.unique(np.asarray(dz.hh[u]))), 1))}

    def _outcomes(self, sim, P, horizon, seed, t0, capped=None, extra=None) -> Outcomes:
        w0, w1 = horizon.window
        dz = sim.diseases.malaria
        yv = np.asarray(sim.t.yearvec)
        ndays = max(((yv >= w0) & (yv < w1)).sum(), 1)
        pc = {k: {} for k in ("cases", "severe", "deaths", "treated", "dalys")}
        le = [P["le_u5"], P["le_5_14"], P["le_15p"]]
        for i, g in enumerate(AGES):
            avg = dz.acc["pdays"][i] / ndays
            for k in ("cases", "severe", "deaths", "treated"):
                pc[k][g] = float(dz.acc[k][i] / avg) if avg > 0 else 0.0
            yld = (max(pc["cases"][g] - pc["severe"][g], 0) * P["dw_moderate"] * P["dur_moderate_d"] / 365
                   + pc["severe"][g] * P["dw_severe"] * P["dur_severe_d"] / 365)
            pc["dalys"][g] = float(yld + pc["deaths"][g] * le[i])
        wins = {}
        for name, (t_a, t_b) in (horizon.windows or {}).items():
            sel = (yv >= t_a) & (yv < t_b)
            dur = max(t_b - t_a, 1e-9)
            d = dz.daily
            wins[name] = {k: float(d[k][sel].mean()) for k in M.PFPR_BANDS}
            wins[name].update({
                "inc_u5": float(d["inc_u5"][sel].sum()),
                "inc_all": float(d["inc_all"][sel].sum()), "inc_u5_smc": float(d["inc_u5_smc"][sel].sum()),
                "eir_annual": float(d["eir"][sel].sum() / dur),
                "net_use": float(d["net_use"][sel].mean()), "net_access": float(d["net_access"][sel].mean())})
        # v0.6: treatments in the delivery period per mean person in it (the add-on and its funding end there); the
        # delivery accumulators equal the window ones when the period is the window
        d0, d1 = delivery_period(horizon)
        ndel = max(((yv >= d0) & (yv < d1)).sum(), 1)
        tot_treated = float(dz.acc_del_treated.sum() / max(dz.acc_cost["pdays_del"] / ndel, 1e-9))
        cd = {"nets_given": float(dz.acc_cost["nets_given"]), "smc_doses": float(dz.acc_cost["smc_doses"]),
              "irs_sprayed": float(dz.acc_cost["irs_sprayed"]), "treated_all": tot_treated}
        if P.get("cm_target"):
            # v0.12: per mean person in the delivery period, as treated_all: child-years in the targeted ages while the
            # package's add-on runs (the per-child cost), and treated episodes in those ages (commodities)
            den = max(dz.acc_cost["pdays_del"] / ndel, 1e-9)
            cd["cm_child_years"] = float(dz.acc_cm[0] * P["cm_catchment_share"] / 365.0 / den)   # in the catchment
            cd["treated_cm_ages"] = float(dz.acc_cm[1] / den)
        if capped is None:
            capped = bool(getattr(self, "_access_capped", False))
        return Outcomes(per_capita=pc, windows=wins, cost_drivers=cd,
                        meta=dict({"seconds": round(time.time() - t0, 2), "seed": int(seed),
                                   "itn_access_capped": bool(capped),
                                   "age_shares": {g: float(dz.acc["pdays"][i] / max(dz.acc["pdays"].sum(), 1))
                                                  for i, g in enumerate(AGES)}}, **(extra or {})))


def to_jsonable(o: Outcomes) -> dict:
    return {"per_capita": o.per_capita, "windows": o.windows, "cost_drivers": o.cost_drivers, "meta": o.meta}


def _self_test_unit() -> UnitSpec:
    """The unit every self-test section starts from (here and in selftest_late.py)."""
    return UnitSpec("t", {"m0": 8.0, "season": [1.0] * 12, "resistance": 0.5, "species": {"gambiae": 0.7, "arabiensis": 0.3},
                          "cm_ft": 0.3, "itn_use": 0.6, "itn_hist_years": [2016.3], "campaign_year": 2018.1})


def _self_test() -> int:
    """Cases M1-M21 and the v0.5-v0.7 sections. The v0.8-v0.10 sections run in a second process,
    `python3 toolkit/engines/malaria/selftest_late.py --self-test`, so each module stays well inside CI's per-test
    time limit (scripts/self_tests.py discovers and runs both)."""
    fails = []

    def ok(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    e = MalariaEngine()
    e_par = {"n_agents": 600, "burnin_years": 0}      # mechanism tests: no burn-in (v0.5 cases below)
    unit = _self_test_unit()
    h = Horizon(2015.0, 2019.0, (2018.0, 2019.0), {"pre": [2017.0, 2018.0], "post": [2018.3, 2019.0]})

    class Small(MalariaEngine):
        def run(self, params, unit, package, horizon, seed):
            return super().run(dict(e_par, **params), unit, package, horizon, seed)
    es = Small()
    probs = check_engine(es, unit, Package("pbo_irs", {"net": "pbo", "irs": 1}), h)
    ok(probs == [], f"M1 the malaria engine passes the contract test {probs}")
    none = es.run({}, unit, Package("none", {}), h, 3)
    net = es.run({}, unit, Package("dual", {"net": "dual"}), h, 3)
    ok(abs(none.windows["pre"]["pfpr_u5"] - net.windows["pre"]["pfpr_u5"]) < 1e-12,
       "M2 common random numbers: arms are identical before the package starts")
    ok(net.windows["post"]["eir_annual"] < none.windows["post"]["eir_annual"], "M3 a dual-AI campaign lowers EIR")
    ok(net.cost_drivers["nets_given"] > 0.4 and none.cost_drivers["nets_given"] == 0, "M4 nets are counted only for the package's campaign")
    try:
        es.run({"no_such": 1}, unit, Package("none", {}), h, 1)
        ok(False, "M5 unknown parameter fails closed")
    except KeyError:
        ok(True, "M5 unknown parameter fails closed")
    try:
        es.run({}, UnitSpec("x", dict(unit.attributes, species={"stephensi": 1.0})), Package("none", {}), h, 1)
        ok(False, "M6 unknown species fails closed")
    except KeyError:
        ok(True, "M6 unknown species fails closed")
    names = [p.name for p in e.parameters() if p.kind == "fitted"]
    ok(set(names) == {"cfp_kill", "irs_kill"}, "M7 two engine-level parameters are fitted (nets use published hut-trial values)")
    # ---- mechanisms in isolation (households and correlated latents; IRS product)
    h1 = Horizon(2019.0, 2020.5, (2020.0, 2020.5), {"post": [2020.0, 2020.5]})
    u1 = UnitSpec("t", dict(unit.attributes, itn_hist_years=[], campaign_year=2020.0, irs_month=1))

    def latents(over):
        sim, _ = es.build_sim(dict(over, n_agents=2000, burnin_years=0), u1, Package("x", {"net": "pyr", "irs": 1}), h1, 5)
        sim.init()
        dz = sim.diseases.malaria
        uu = sim.people.auids
        return dz.hh[uu], dz.prop_net[uu] < 0.6, dz.prop_spray[uu] < 0.8, dz.zeta[uu]

    def within_hh_concordance(hh, use):
        df = pd.DataFrame({"hh": hh, "use": use.astype(float)})
        g = df.groupby("hh")["use"].agg(["mean", "size"])
        g = g[g["size"] > 1]
        return float((g["mean"] * (1 - g["mean"])).mean())   # 0 when every household is all-or-none
    hh0, use0, spr0, z0 = latents({"hh_net_corr": 0.0, "cov_corr": 0.0, "exp_corr": 0.0})
    hh9, use9, spr9, z9 = latents({"hh_net_corr": 0.9, "cov_corr": 0.9, "exp_corr": 0.6})
    ok(within_hh_concordance(hh9, use9) < 0.5 * within_hh_concordance(hh0, use0),
       "M8 household correlation clusters net use within households")
    ok(abs(use0.mean() - 0.6) < 0.04 and abs(use9.mean() - 0.6) < 0.04, "M9 the correlation leaves achieved net use unchanged")
    c0 = np.corrcoef(use0, spr0)[0, 1]
    c9 = np.corrcoef(use9, spr9)[0, 1]
    ok(abs(c0) < 0.06 and c9 > 0.2, f"M10 net use and spraying correlate only when cov_corr > 0 ({c0:.3f}, {c9:.3f})")
    r9, r0 = z9[~use9].mean() / z9[use9].mean(), z0[~use0].mean() / z0[use0].mean()
    ok(r9 > 2.0 and abs(r0 - 1) < 0.25 and abs(z9.mean() - 1) < 0.2,
       f"M11 (non-user / user biting {r0:.2f} -> {r9:.2f}; mean {z9.mean():.2f}) biting rates are higher among net non-users only when exp_corr > 0 (mean biting kept at 1)")
    irs_s = es.run({}, UnitSpec("t", dict(u1.attributes, irs_product="short")), Package("i", {"irs": 1}), h1, 7)
    irs_l = es.run({}, u1, Package("i", {"irs": 1}), h1, 7)
    ok(irs_l.windows["post"]["eir_annual"] < irs_s.windows["post"]["eir_annual"],
       "M12 the long-lasting IRS product cuts EIR more over six months than the short-lived one")
    sim, _ = es.build_sim({"n_agents": 600, "burnin_years": 0}, UnitSpec("t", dict(u1.attributes, irs_years=[2020.0])),
                          Package("i", {"irs": 1}), Horizon(2019.0, 2021.5, (2020.0, 2021.5), {}), 3)
    # ---- v0.3 field-use mechanisms in isolation: attrition, use below access, nightly variation
    P0 = resolve_params(e, {})
    acc, capped = access_for_use(0.6, 0.0, P0)
    acc1, _ = access_for_use(0.3, 1.64, P0)
    ok(abs(acc - 0.6 / 0.871) < 1e-9 and not capped and abs(acc1 - 0.3 / (0.5 * 0.871)) < 1e-6 and access_for_use(0.9, 1.0, P0)[1],
       "M14 access at distribution = reported use / (retention x use rate), capped and flagged when unreachable")
    hA = Horizon(2019.5, 2021.75, (2020.0, 2021.75), {"w0": [2020.02, 2020.10], "w1": [2021.60, 2021.68]})
    uA = UnitSpec("t", dict(u1.attributes, itn_use=0.6, irs_month=None))
    oA = es.run({"n_agents": 1500}, uA, Package("n", {"net": "pyr"}), hA, 4)
    a0, a1 = oA.windows["w0"]["net_access"], oA.windows["w1"]["net_access"]
    ok(abs(a1 / a0 - 0.5) < 0.08, f"M15 attrition: access halves over the median retention time ({a0:.3f} -> {a1:.3f})")
    ok(abs(oA.windows["w0"]["net_use"] / a0 - 0.871) < 0.03 and abs(oA.windows["w0"]["net_use"] - 0.6) < 0.04,
       f"M16 nightly use among those with access = use rate; reported use reproduced ({oA.windows['w0']['net_use']:.3f})")
    oB = es.run({"n_agents": 1500, "net_retention_half": 1e9, "use_rate": 1.0}, uA, Package("n", {"net": "pyr"}), hA, 4)
    ok(abs(oB.windows["w1"]["net_access"] / oB.windows["w0"]["net_access"] - 1) < 0.08
       and oB.windows["w0"]["net_use"] > oB.windows["w0"]["net_access"] * 0.99,
       "M17 with attrition and the use gap switched off, access stays and everyone with access uses it")

    def overlap(persist):
        sim2, _ = es.build_sim({"n_agents": 1500, "use_persist": persist, "burnin_years": 0}, u1, Package("n", {}), h1, 6)
        sim2.init()
        dz2 = sim2.diseases.malaria
        uu = sim2.people.auids
        dz2.has_net[uu] = True
        a, b = dz2.using_net(uu), dz2.using_net(uu)
        return float((a & b).mean()), float(a.mean())
    o1, m1 = overlap(1.0)
    o0, m0 = overlap(0.0)
    ok(abs(m1 - 0.871) < 0.03 and abs(m0 - 0.871) < 0.03 and o1 > m1 - 0.01 and abs(o0 - 0.871 ** 2) < 0.03,
       f"M18 nightly variation: same mean use; persistent non-use repeats the same people ({o1:.3f}), nightly does not ({o0:.3f})")
    sim.init()
    ok(sim.interventions["irs"].years == [2020], "M13 irs_years restricts IRS to the trial's rounds")
    # ---- v0.4: prevalence in the B7 trials' own age bands (microscopy, as before)
    yrs = np.array([0.3, 4.0, 8.0, 12.0, 15.5, 30.0])
    pb = M.prevalence_bands(np.array([1.0, 0.0, 1.0, 0.0, 1.0, 0.0]), yrs * 365)
    ok(abs(pb["pfpr_lt5"] - 0.5) < 1e-12 and abs(pb["pfpr_lt10"] - 2 / 3) < 1e-12 and abs(pb["pfpr_le15"] - 0.6) < 1e-12
       and abs(pb["pfpr_all"] - 0.5) < 1e-12 and abs(pb["pfpr_u5"] - 0.0) < 1e-12 and abs(pb["pfpr_6m14y"] - 1 / 3) < 1e-12,
       f"M19 age bands: under 5 from birth, under 10, 15 and under (< 16 y), all ages; the old bands unchanged {pb}")
    hP = Horizon(2016.0, 2021.1, (2020.0, 2021.1), {"m12": [2020.92, 2021.08]})
    uP = UnitSpec("t", {"m0": 6.0, "season": [1.0] * 12, "resistance": 0.95, "species": {"gambiae": 1.0},
                        "cm_ft": 0.3, "itn_use": 0.7, "campaign_year": 2020.0})
    o_none = es.run({"n_agents": 2500}, uP, Package("none", {}), hP, 9)
    o_net = es.run({"n_agents": 2500}, uP, Package("pyr", {"net": "pyr"}), hP, 9)
    keys = ("pfpr_lt5", "pfpr_lt10", "pfpr_le15", "pfpr_all")
    ok(all(k in o_net.windows["m12"] and 0.0 <= o_net.windows["m12"][k] <= 1.0 for k in keys),
       "M20 the new age-band prevalences are reported in every window")
    # v0.5: the ratio is the geometric mean over three seeds (one seed flipped it when the household fix changed the
    # random draws: 0.521 under 5 vs 0.487 all ages, within seed noise)
    lr = {"pfpr_lt5": [], "pfpr_all": []}
    for sd in (9, 10, 11):
        a0, a1 = e.run_batch([dict(e_par, n_agents=2500)] * 2, uP, [Package("none", {}), Package("pyr", {"net": "pyr"})],
                             hP, sd)
        for k in lr:
            lr[k].append(np.log(a1.windows["m12"][k] / max(a0.windows["m12"][k], 1e-9)))
    rr = {k: float(np.exp(np.mean(v))) for k, v in lr.items()}
    ok(rr["pfpr_all"] > rr["pfpr_lt5"],
       f"M21 forward behaviour (Sochantha 2006 age interaction): nets cut all-age prevalence less than under-5 prevalence {rr}")
    _self_test_v05(ok, e, unit)
    _self_test_v06(ok, e, unit)
    _self_test_v07(ok, e, unit)
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def _self_test_v05(ok, e, unit) -> None:
    """v0.5: the equilibrated start. A no-intervention burn-in of `burnin_years` before horizon.start, run once per
    (unit, seed) and shared by every package (run_batch); the population starts smaller and grows to n_agents."""
    def case(label, fn):
        try:
            fn()
        except Exception as ex:  # noqa: BLE001 -- a missing function is a failing case, not a crash of the test
            ok(False, f"{label} ({type(ex).__name__}: {ex})")

    hz = Horizon(2015.0, 2017.0, (2016.0, 2017.0), {"pre": [2015.0, 2016.0], "post": [2016.2, 2017.0]})
    u = UnitSpec("t", dict(unit.attributes, itn_hist_years=[2015.3], campaign_year=2016.1))
    pk = [Package("none", {}), Package("pyr", {"net": "pyr"}), Package("dual_smc", {"net": "dual", "smc": 1}),
          Package("cm", {"cm": 1})]
    base = {"n_agents": 500, "burnin_years": 3}

    def v1():
        ok(getattr(e, "batch_shares_state", False) is True and callable(getattr(e, "run_batch", None)),
           "V1 the engine declares that packages share one initial state, and runs them through run_batch")
        b = {p.name: p for p in e.parameters()}.get("burnin_years")
        ok(b is not None and b.value == 50 and b.kind == "fixed" and "equilibration.yaml" in b.source,
           "V1b the burn-in length is a declared card parameter (default 50 y) whose source is the convergence evidence")
    case("V1", v1)

    def v2():
        n, B = 1500, 10
        sts = [e.initial_state({"n_agents": n, "burnin_years": B}, u, horizon=hz, seed=s) for s in range(1, 7)]
        mean = float(np.mean([s["N"] for s in sts]))
        ok(all(s["N0"] < 0.8 * n for s in sts) and abs(mean / n - 1) < 0.03,
           f"V2 the burn-in population starts smaller ({sts[0]['N0']}) and grows to n_agents at horizon.start "
           f"(mean {mean:.0f} of {n} over 6 seeds)")
        hs = [s["hh_mean_size"] for s in sts]
        ok(all(abs(h / 5.0 - 1) < 0.1 for h in hs),
           f"V9 (review P2) households keep their declared mean size (hh_size 5) while the population grows: {hs}")
        s0 = e.initial_state({"n_agents": n, "burnin_years": 0}, u, horizon=hz, seed=1)
        ok(s0["N"] == n and s0["N0"] == n, "V2b without a burn-in the population at horizon.start is n_agents exactly")
        try:
            e.initial_state({"n_agents": n, "burnin_years": 140}, u, horizon=hz, seed=1)
            ok(False, "V2c a burn-in starting before 1900 (Starsim demography inactive) fails closed")
        except ValueError as ex:
            ok("1900" in str(ex), "V2c a burn-in starting before 1900 (Starsim demography inactive) fails closed")
    case("V2", v2)

    def v3():
        outs = e.run_batch([base] * len(pk), u, pk, hz, 5)
        hs = {o.meta["start_state_sha256"] for o in outs}
        pre = [o.windows["pre"] for o in outs]
        other = e.run_batch([base], u, [pk[0]], hz, 6)[0]
        ok(len(hs) == 1 and all(p == pre[0] for p in pre) and other.meta["start_state_sha256"] not in hs
           and all(o.meta["burnin_members"] == len(pk) for o in outs),
           "V3 packages run from one burn-in share an identical pre-start history (state hash at horizon.start and "
           "every pre-package window), and another seed does not")
        f = outs[0].meta["fork_year"]
        ok(hz.start <= f <= hz.window[0] and outs[0].meta["burnin_years"] == 3,
           f"V3b the packages fork at their first component event, within [horizon.start, decision start] ({f})")
        solo = e.run(base, u, pk[2], hz, 5)
        ok(solo.per_capita == outs[2].per_capita and solo.windows == outs[2].windows
           and solo.cost_drivers == outs[2].cost_drivers,
           "V4 a package run alone equals the same package in a shared batch (the fork copies the state exactly)")
        ue = UnitSpec("t", dict(u.attributes, irs_years=[2015.0], irs_month=6))
        pair = e.run_batch([base] * 2, ue, [pk[0], Package("irs", {"irs": 1})], hz, 5)
        alone = e.run(base, ue, pk[0], hz, 5)
        ok(pair[0].meta["fork_year"] < 2015.5 and alone.meta["fork_year"] == hz.window[0]
           and alone.per_capita == pair[0].per_capita and alone.windows == pair[0].windows,
           "V4b (review P2) a package forked early, beside a member whose component acts before the decision start, "
           "equals the same package run alone (forked at the decision start)")
    case("V3", v3)

    def v5():
        ps = [dict(base, cfp_kill=0.1, irs_kill=0.2), dict(base, cfp_kill=0.7, irs_kill=0.8)]
        a, b = e.run_batch(ps, u, [pk[2], pk[2]], hz, 5)
        ok(a.meta["start_state_sha256"] == b.meta["start_state_sha256"] and a.meta["burnin_members"] == 2
           and a.windows["post"] != b.windows["post"],
           "V5 parameters that act only through package components share one burn-in, and still change the outcomes")
        c, d = e.run_batch([dict(base, hh_net_corr=0.1), dict(base, hh_net_corr=0.8)], u, [pk[1], pk[1]], hz, 5)
        ok(c.meta["start_state_sha256"] != d.meta["start_state_sha256"] and c.meta["burnin_members"] == 1,
           "V5b a parameter that acts during the burn-in gets its own burn-in")
    case("V5", v5)

    def v6():
        lo = e.initial_state({"n_agents": 400, "burnin_years": 0, "init_eir_per_m0": 0.15}, u, horizon=hz, seed=2)
        hi = e.initial_state({"n_agents": 400, "burnin_years": 0, "init_eir_per_m0": 2.4}, u, horizon=hz, seed=2)
        ok(hi["immunity"]["IC"]["15p"] > 1.5 * lo["immunity"]["IC"]["15p"],
           "V6 without a burn-in the starting immunity follows the init_eir guess (the knob the evidence varies)")
        from toolkit.engines.malaria.benchmarks import equilibration as EQ
        doc = yaml.safe_load(open(EQ.OUT)) if os.path.exists(EQ.OUT) else None
        bv = {p.name: p.value for p in e.parameters()}["burnin_years"]
        probs = EQ.problems(doc, bv, VERSION)
        # v0.5 review: after 50 years adult IC (30+) still differs 6-9% between the x0.25 and x4 guesses at three
        # levels (residual memory of the burn-in's first cohort; within 2% after 70 years), so V7 judges what the
        # guess does to decision outputs (guess_sensitivity.yaml, 3%) and reports the immunity spread beside it.
        from toolkit.engines.malaria.benchmarks import guess_sensitivity as GS
        gdoc = yaml.safe_load(open(GS.OUT)) if os.path.exists(GS.OUT) else None
        gp = GS.problems(gdoc, bv, VERSION)
        imm = [p for p in probs if p.startswith("init_guess")]
        ok(not gp and not [p for p in probs if p.startswith("evidence")],
           f"V7 after the burn-in, the init_eir guess (x0.25 vs x4) moves PfPR 6-59 m, all-age cases, treated_all and "
           f"DALYs averted by at most 3% at >= 2 levels (guess_sensitivity.yaml): {gp[:3]}; adult immunity spread "
           f"above 5% (reported, not judged): {imm[:4]}")
        ok(not [p for p in probs if not p.startswith("init_guess")],
           f"V8 equilibration: adult IC and ID by age stay within 5% when the burn-in is doubled, at >= 2 "
           f"transmission levels (benchmarks/equilibration.yaml): {probs[:3]}")
    case("V6", v6)


def _self_test_v06(ok, e, unit) -> None:
    """v0.6: a delivery period (DeliveryHorizon.delivery) distinct from the outcome window, and routine net campaigns in every
    arm (itn_routine_interval). Without either, a run is byte-identical to v0.5 (D9; benchmarks/restamp_proof.py
    compares the v0.5 and v0.6 code on the pack's own settings)."""
    def case(label, fn):
        try:
            fn()
        except Exception as ex:  # noqa: BLE001 -- a missing feature is a failing case, not a crash of the test
            ok(False, f"{label} ({type(ex).__name__}: {ex})")

    base = {"n_agents": 500, "burnin_years": 2}
    season = [0.4, 0.4, 0.5, 0.7, 1.0, 1.4, 1.8, 2.0, 1.6, 1.0, 0.7, 0.5]
    u = UnitSpec("t", dict(unit.attributes, season=season, itn_hist_years=[2015.3], campaign_year=2016.1, irs_month=4))
    # outcome window 2016-2019, delivery 2016-2017; the same sim with the window cut to the delivery period
    try:
        from toolkit.engines.horizon import DeliveryHorizon
        hd = DeliveryHorizon(2015.0, 2019.0, (2016.0, 2019.0), {"y1": [2016.0, 2017.0], "y3": [2018.0, 2019.0]},
                     delivery=(2016.0, 2017.0))
    except (ImportError, TypeError) as ex:
        ok(False, f"D0-D9 a horizon carries a delivery period (toolkit/engines/horizon.py) ({ex})")
        return
    hw = Horizon(2015.0, 2019.0, (2016.0, 2017.0), {"y1": [2016.0, 2017.0], "y3": [2018.0, 2019.0]})
    full = Package("full", {"net": "pyr", "smc": 1, "irs": 1, "cm": 1})

    def d0():
        ok(tuple(int(x) for x in VERSION.split(".")) >= (0, 6) and getattr(e, "supports_delivery", False) is True
           and "itn_routine_interval" in e.describe().unit_attributes,
           "D0 engine v0.6 declares that it honours a delivery period and the routine-campaign attribute")
        b = yaml.safe_load(open(os.path.join(HERE, "attribute_binding.yaml")))["attribute_binding"]
        ok(isinstance(b.get("itn_routine_interval"), dict) and "default" in b["itn_routine_interval"],
           "D0b the routine-campaign attribute has a declared default in attribute_binding.yaml (no routine campaign)")
    case("D0", d0)

    def d1():
        sim, _ = e.build_sim(base, u, full, hd, 3)
        sim.init()
        itv = sim.interventions
        ok(itv["smc"].years == [2016] and itv["irs"].years == [2016] and itv["cm"].stop == 2017.0
           and itv["cm"].start == 2016.0,
           "D1 SMC rounds, IRS rounds and the case-management add-on act only in the delivery period")
        sim2, _ = e.build_sim(base, u, full, Horizon(2015.0, 2019.0, (2016.0, 2019.0), {}), 3)
        sim2.init()
        ok(sim2.interventions["smc"].years == [2016, 2017, 2018] and sim2.interventions["cm"].stop is None,
           "D1b without a delivery period they act in every year of the window, the add-on with no end (v0.5)")
        sim.run()
        ok(abs(sim.diseases.malaria.pars.ft - float(u.attributes["cm_ft"])) < 1e-12,
           "D1c after the delivery period effective treatment is back at the unit's cm_ft")
    case("D1", d1)

    def d2():
        a, = e.run_batch([base], u, [full], hd, 4)
        b, = e.run_batch([base], u, [full], hw, 4)
        ok(a.cost_drivers == b.cost_drivers and a.cost_drivers["smc_doses"] > 0 and a.cost_drivers["irs_sprayed"] > 0
           and a.cost_drivers["nets_given"] > 0,
           f"D2 cost drivers are counted over the delivery period only (equal to a run whose window is the delivery "
           f"period): {a.cost_drivers} vs {b.cost_drivers}")
        ok(a.per_capita["cases"]["u5"] > 2.0 * b.per_capita["cases"]["u5"] and a.windows["y1"] == b.windows["y1"],
           "D3 health outcomes accumulate over the full outcome window (three years, not the one delivery year)")
    case("D2", d2)

    def d4():
        ur = UnitSpec("t", dict(u.attributes, itn_routine_interval=1.0, hist_net="pbo"))
        sim, _ = e.build_sim(base, ur, Package("none", {}), hd, 3)
        sim.init()
        hist = sim.interventions["itn_hist"]
        ok(np.allclose(hist.years, [2015.3, 2017.1, 2018.1]) and hist.code == V.NET_CODE["pbo"],
           f"D4 routine campaigns at campaign_year + k x interval inside the window, net type hist_net: {hist.years}")
        pk = [Package("none", {}), Package("pyr", {"net": "pyr"})]
        r_none, r_pyr = e.run_batch([base] * 2, ur, pk, hd, 5)
        n_none, n_pyr = e.run_batch([base] * 2, UnitSpec("t", dict(u.attributes, hist_net="pbo")), pk, hd, 5)
        ok(r_none.windows["y3"]["net_access"] > 0.3 and r_pyr.windows["y3"]["net_access"] > 0.3
           and n_none.windows["y3"]["net_access"] < 0.25,
           f"D5 the routine campaign acts in the package and in the comparator (access in the last year "
           f"{r_none.windows['y3']['net_access']:.2f}, {r_pyr.windows['y3']['net_access']:.2f}; without it "
           f"{n_none.windows['y3']['net_access']:.2f})")
        ok(r_none.cost_drivers["nets_given"] == 0 and r_pyr.cost_drivers == n_pyr.cost_drivers,
           "D6 routine campaigns are never charged (nets_given counts the package's campaign only)")
    case("D4", d4)

    def d7():
        def refused(h, attrs, pkg, needle, label):
            try:
                e.run(base, UnitSpec("t", dict(u.attributes, **attrs)), pkg, h, 1)
                ok(False, label)
            except ValueError as ex:
                ok(needle in str(ex), f"{label} ({ex})")
        refused(DeliveryHorizon(2015.0, 2019.0, (2016.0, 2019.0), {}, delivery=(2016.0, 2019.5)), {}, Package("n", {}),
                "delivery", "D7 a delivery period outside the outcome window fails closed")
        refused(DeliveryHorizon(2015.0, 2019.0, (2016.0, 2019.0), {}, delivery=(2017.0, 2016.0)), {}, Package("n", {}),
                "delivery", "D7b an empty delivery period fails closed")
        refused(hd, {"campaign_year": 2017.5}, Package("p", {"net": "pyr"}), "campaign",
                "D7c the package's net campaign outside the delivery period fails closed")
        refused(hd, {"irs_years": [2018.0]}, Package("i", {"irs": 1}), "IRS",
                "D7d an IRS round (irs_years) outside the delivery period fails closed")
        refused(hd, {"itn_routine_interval": 0.5}, Package("n", {}), "routine",
                "D7e a routine campaign inside the delivery period fails closed (the plan funds those years)")
        refused(hd, {"itn_routine_interval": 0.0}, Package("n", {}), "itn_routine_interval",
                "D7f a non-positive routine interval fails closed")
    case("D7", d7)

    def d9():
        hz = Horizon(2015.0, 2017.0, (2016.0, 2017.0), {"pre": [2015.0, 2016.0], "post": [2016.2, 2017.0]})
        hz_eq = DeliveryHorizon(2015.0, 2017.0, (2016.0, 2017.0), dict(hz.windows), delivery=(2016.0, 2017.0))
        ub = UnitSpec("t", dict(unit.attributes, itn_hist_years=[2015.3], campaign_year=2016.1))
        pk = [Package("none", {}), Package("pyr", {"net": "pyr"}), Package("dual_smc", {"net": "dual", "smc": 1}),
              Package("cm", {"cm": 1}), Package("pbo_irs", {"net": "pbo", "irs": 1})]
        strip = lambda o: (o.per_capita, o.windows, o.cost_drivers,  # noqa: E731
                           {k: v for k, v in o.meta.items() if k != "seconds"})
        for sd in (5, 6):
            a = [strip(o) for o in e.run_batch([base] * len(pk), ub, pk, hz, sd)]
            b = [strip(o) for o in e.run_batch([base] * len(pk), ub, pk, hz_eq, sd)]
            ok(a == b, f"D9 (seed {sd}) an explicit delivery period equal to the window gives byte-identical outcomes, "
                       f"cost drivers and meta for {len(pk)} packages")
    case("D9", d9)


def _self_test_v07(ok, e, unit) -> None:
    """v0.7: routine SMC in every arm after the delivery period (smc_routine). With the attribute absent or 0, a run is
    byte-identical to v0.6 (R7; benchmarks/restamp_proof.py compares the v0.6 and v0.7 code on the pack's settings)."""
    def case(label, fn):
        try:
            fn()
        except Exception as ex:  # noqa: BLE001 -- a missing feature is a failing case, not a crash of the test
            ok(False, f"{label} ({type(ex).__name__}: {ex})")

    from toolkit.engines.horizon import DeliveryHorizon
    base = {"n_agents": 1000, "burnin_years": 2}
    season = [0.4, 0.4, 0.5, 0.7, 1.0, 1.4, 1.8, 2.0, 1.6, 1.0, 0.7, 0.5]
    wins = {"y1": [2016.0, 2017.0], "y3": [2018.0, 2019.0]}
    attrs = dict(unit.attributes, season=season, itn_hist_years=[2015.3], campaign_year=2016.1, irs_month=4)
    u = UnitSpec("t", attrs)
    ur = UnitSpec("t", dict(attrs, smc_routine=1))
    hd = DeliveryHorizon(2015.0, 2019.0, (2016.0, 2019.0), dict(wins), delivery=(2016.0, 2017.0))
    hw = Horizon(2015.0, 2019.0, (2016.0, 2019.0), dict(wins))     # no delivery period: SMC every window season
    months = default_smc_months(np.asarray(season))
    none, smc = Package("none", {}), Package("smc", {"smc": 1})
    full = Package("full", {"net": "pyr", "smc": 1, "irs": 1, "cm": 1})

    def r0():
        ok(tuple(int(x) for x in VERSION.split(".")) >= (0, 7) and "smc_routine" in e.describe().unit_attributes,
           "R0 engine v0.7 declares the routine-SMC unit attribute on its card")
        b = yaml.safe_load(open(os.path.join(HERE, "attribute_binding.yaml")))["attribute_binding"]
        ok(isinstance(b.get("smc_routine"), dict) and str(b["smc_routine"].get("default")) == "0",
           "R0b the routine-SMC attribute has the declared default 0 in attribute_binding.yaml (no routine SMC)")
    case("R0", r0)

    def r1():
        want = [y + (m - 1) / 12 for y in (2017, 2018) for m in months]
        for pkg, own in ((none, []), (full, [2016])):
            sim, _ = e.build_sim(base, ur, pkg, hd, 3)
            sim.init()
            itv = sim.interventions
            r = itv["smc_routine"]
            ok(np.allclose(sorted(r.targets()), want) and itv["smc"].years == own and r.count is False
               and list(r.months) == list(months),
               f"R1 ({pkg.id}) routine SMC rounds in every season after the delivery period, in the unit's SMC months, "
               f"uncounted; the package's own SMC only in delivery seasons: {sorted(r.targets())}, {itv['smc'].years}")
        sim, _ = e.build_sim(base, UnitSpec("t", dict(attrs, smc_routine=0)), full, hd, 3)
        sim.init()
        ok("smc_routine" not in list(sim.interventions.keys()),
           "R1b with smc_routine 0 no routine-SMC intervention is built (the v0.6 sim)")
    case("R1", r1)

    def r2():
        r_none, r_smc = e.run_batch([base] * 2, ur, [none, smc], hd, 5)
        n_none, n_smc = e.run_batch([base] * 2, u, [none, smc], hd, 5)
        ok(r_none.meta["burnin_members"] == 2,
           "R2 the comparator and the package share one burn-in and history with routine SMC")
        k = "inc_u5_smc"
        ok(r_none.windows["y3"][k] < 0.7 * n_none.windows["y3"][k] and r_smc.windows["y3"][k] < 0.7 * n_smc.windows["y3"][k],
           f"R2b routine SMC acts after delivery in the comparator ({r_none.windows['y3'][k]:.3f} vs "
           f"{n_none.windows['y3'][k]:.3f} without) and in the package ({r_smc.windows['y3'][k]:.3f} vs "
           f"{n_smc.windows['y3'][k]:.3f})")
        ok(r_none.windows["y1"] == n_none.windows["y1"] and r_none.per_capita["cases"]["u5"] < n_none.per_capita["cases"]["u5"],
           "R4 comparator burden after delivery is lower where smc_routine = 1 (and identical in the delivery year)")
        ok(r_none.cost_drivers["smc_doses"] == 0 and r_none.cost_drivers == n_none.cost_drivers
           and r_smc.cost_drivers == n_smc.cost_drivers and r_smc.cost_drivers["smc_doses"] > 0,
           f"R3 routine SMC is never charged: cost drivers are equal with and without it ({r_smc.cost_drivers} vs "
           f"{n_smc.cost_drivers})")
    case("R2", r2)

    def r5():
        a = e.run(base, ur, smc, hd, 7)          # package SMC in 2016, routine SMC in 2017 and 2018
        b = e.run(base, u, smc, hw, 7)           # package SMC in every window season (no delivery period)
        ok(a.per_capita == b.per_capita and a.windows == b.windows,
           "R5 no rebound gap: SMC in the delivery period followed by routine SMC gives the same health outcomes as "
           "SMC in every season of the window (no missing season, no double round)")
        ok(0 < a.cost_drivers["smc_doses"] < 0.5 * b.cost_drivers["smc_doses"],
           f"R5b only the delivery seasons are charged ({a.cost_drivers['smc_doses']:.3f} vs "
           f"{b.cost_drivers['smc_doses']:.3f} for SMC charged in all three)")
    case("R5", r5)

    def r6():
        def refused(h, at, pkg, needle, label):
            try:
                e.run(base, UnitSpec("t", dict(attrs, **at)), pkg, h, 1)
                ok(False, label)
            except ValueError as ex:
                ok(needle in str(ex), f"{label} ({ex})")
        refused(hw, {"smc_routine": 1}, none, "objective.delivery",
                "R6 smc_routine = 1 without a delivery period fails closed")
        refused(hd, {"smc_routine": 2}, none, "smc_routine", "R6b smc_routine other than 0 or 1 fails closed")
        refused(hd, {"smc_routine": "yes"}, none, "smc_routine", "R6c a non-numeric smc_routine fails closed")
        P = smc_schedule_problems
        mo = (7,)
        ok(P([2016.5], [2017.5, 2018.5], mo, 2016.0, 2017.0, 2019.0) == [],
           "R6d the schedule check passes a continuous season sequence")
        ok(any("missing" in p for p in P([2016.5], [2018.5], mo, 2016.0, 2017.0, 2019.0)),
           "R6e the schedule check fails a missing season between the package's SMC and routine SMC")
        ok(any("twice" in p for p in P([2016.5], [2016.5, 2017.5, 2018.5], mo, 2016.0, 2017.0, 2019.0)),
           "R6f the schedule check fails a round given twice")
        ok(any("inside the delivery period" in p for p in P([], [2016.5, 2017.5, 2018.5], mo, 2016.0, 2017.0, 2019.0)),
           "R6g the schedule check fails a routine round inside the delivery period")
    case("R6", r6)

    def r7():
        strip = lambda o: (o.per_capita, o.windows, o.cost_drivers,  # noqa: E731
                           {k: v for k, v in o.meta.items() if k != "seconds"})
        u0 = UnitSpec("t", dict(attrs, smc_routine=0))
        pk = [none, smc, full]
        small = {"n_agents": 400, "burnin_years": 2}
        for h, nm in ((hd, "delivery"), (hw, "no delivery")):
            a = [strip(o) for o in e.run_batch([small] * len(pk), u, pk, h, 5)]
            b = [strip(o) for o in e.run_batch([small] * len(pk), u0, pk, h, 5)]
            ok(a == b, f"R7 ({nm}) smc_routine = 0 gives byte-identical outcomes, cost drivers and meta to the "
                       f"attribute absent for {len(pk)} packages")
    case("R7", r7)



def _self_test_v08(ok, e, unit) -> None:
    """v0.8: the figure hooks of toolkit/engines/hooks.py (#127). schedule() is pure and comes from the code that
    configures the sim (delivery_plan); run_series() reads the model's daily records through run_batch's member loop,
    so run / run_batch are unchanged (S7; benchmarks/restamp_proof.py compares the v0.7 and v0.8 code)."""
    def case(label, fn):
        try:
            fn()
        except Exception as ex:  # noqa: BLE001 -- a missing feature is a failing case, not a crash of the test
            ok(False, f"{label} ({type(ex).__name__}: {ex})")

    import copy
    from toolkit.engines import hooks as HK
    from toolkit.engines.horizon import DeliveryHorizon
    base = {"n_agents": 500, "burnin_years": 2}
    season = [0.4, 0.4, 0.5, 0.7, 1.0, 1.4, 1.8, 2.0, 1.6, 1.0, 0.7, 0.5]
    wins = {"y1": [2016.0, 2017.0], "y3": [2018.0, 2019.0], "all": [2015.0, 2019.0]}
    # past campaigns before (burn-in) and inside the covered range; routine nets 2017.1, 2018.1; routine SMC 2017-18
    attrs = dict(unit.attributes, season=season, itn_hist_years=[2014.5, 2015.3], campaign_year=2016.1, irs_month=4,
                 itn_routine_interval=1.0, smc_routine=1)
    u = UnitSpec("t", attrs)
    hd = DeliveryHorizon(2015.0, 2019.0, (2016.0, 2019.0), dict(wins), delivery=(2016.0, 2017.0))
    none, full = Package("none", {}), Package("full", {"net": "pyr", "smc": 1, "irs": 1, "cm": 1})
    smc_only = Package("smc", {"smc": 1})
    metrics = ["pfpr_u5", "inc_all", "inc_u5"]

    def s0():
        ok(tuple(int(x) for x in VERSION.split(".")) >= (0, 8) and HK.supports(e, "schedule")
           and HK.supports(e, "series"), "S0 engine v0.8 offers both figure hooks (hooks.supports)")
    case("S0", s0)

    def s1():
        from toolkit.engines.malaria import model as MM
        w1, st = float(hd.window[1]), float(hd.start)
        for pkg in (none, full, smc_only):
            ev = e.schedule(base, u, pkg, hd)
            sim, _ = e.build_sim(base, u, pkg, hd, 3)
            sim.init()
            itv, yv = sim.interventions, np.asarray(sim.t.yearvec)
            ran = lambda ts: sorted(round(float(yv[i]), 9) for i in MM._event_tis(sim, ts) if st <= yv[i] < w1)  # noqa
            want = {"net:past+routine": ran(itv["itn_hist"].years), "net:decision": ran(itv["itn_new"].years),
                    "smc:decision": ran(itv["smc"].targets()),
                    "irs:decision": ran([y + (itv["irs"].month - 1) / 12 for y in itv["irs"].years]),
                    "smc:routine": ran(itv["smc_routine"].targets())}
            pick = lambda c, ks: sorted(round(x["t0"], 9) for x in ev if x["component"] == c and x["kind"] in ks)  # noqa
            have = {"net:past+routine": pick("net", ("history", "routine")), "net:decision": pick("net", ("decision",)),
                    "smc:decision": pick("smc", ("decision",)), "irs:decision": pick("irs", ("decision",)),
                    "smc:routine": pick("smc", ("routine",))}
            ok(have == want and len(pick("net", ("history",))) == 1 and len(pick("net", ("routine",))) == 2,
               f"S1 ({pkg.id}) every event is a step the sim executes, and every executed step is listed: "
               f"{ {k: (len(have[k]), len(want[k])) for k in want} }")
            cm = [x for x in ev if x["component"] == "cm"]
            c = itv["cm"]
            ok((cm == [] if c.add == 0 else len(cm) == 1 and abs(cm[0]["t0"] - c.start) < 1e-9
                and abs(cm[0]["t1"] - (c.stop if c.stop is not None else w1)) < 1e-9)
               and all(x["charged"] == (x["kind"] == "decision") for x in ev),
               f"S1b ({pkg.id}) the add-on period is the sim's; charged exactly for decision events")
        evn = e.schedule(base, u, none, hd)
        evf = e.schedule(base, u, full, hd)
        ok(not [x for x in evn if x["kind"] == "decision"]
           and {x["component"] for x in evf if x["kind"] == "decision"} == {"net", "smc", "irs", "cm"}
           and [x for x in evn if x["kind"] != "decision"] == [x for x in evf if x["kind"] != "decision"],
           "S1c the comparator has no decision event; every arm has the same history and routine events")
    case("S1", s1)

    def s2():
        import starsim as ss
        ua = UnitSpec("t", copy.deepcopy(attrs))
        before, st0 = copy.deepcopy(ua.attributes), np.random.get_state()
        real = ss.Sim

        def no_sim(*a, **k):
            raise AssertionError("schedule() built a sim")
        ss.Sim = no_sim
        try:
            a1 = e.schedule(base, ua, full, hd)
            a2 = e.schedule(base, ua, full, hd)
        finally:
            ss.Sim = real
        st1 = np.random.get_state()
        ok(a1 == a2 and ua.attributes == before and st0[0] == st1[0] and np.array_equal(st0[1], st1[1])
           and st0[2:] == st1[2:], "S2 schedule() is pure: no sim, no random draw, the unit unchanged, same answer")
    case("S2", s2)

    def s3():
        hw = Horizon(2015.0, 2019.0, (2016.0, 2019.0), dict(wins))
        uw = UnitSpec("t", {k: v for k, v in attrs.items() if k not in ("smc_routine", "itn_routine_interval")})
        for pkg in (none, full, smc_only):
            for h, uu, nm in ((hd, u, "delivery"), (hw, uw, "window")):
                s = HK.schedule(e, base, uu, pkg, h)
                ok(all(float(h.start) <= x["t0"] < float(h.window[1]) for x in s),
                   f"S3 ({pkg.id}, {nm}) #127's checker accepts the schedule ({len(s)} events in "
                   "[horizon.start, window end))")
        try:
            e.schedule(base, UnitSpec("t", dict(attrs, campaign_year=2017.5)), full, hd)
            ok(False, "S3b a campaign outside the delivery period fails closed in schedule() as in the sim")
        except ValueError as ex:
            ok("delivery period" in str(ex), f"S3b a campaign outside the delivery period fails closed in schedule() "
                                             f"as in the sim ({ex})")
    case("S3", s3)

    def s4():
        card = e.describe().outputs.get("series") or {}
        ok(card.get("pfpr_u5", {}).get("aggregate") == "mean" and card.get("inc_all", {}).get("aggregate") == "rate"
           and card.get("inc_u5", {}).get("aggregate") == "rate" and "year" in card.get("inc_all", {}).get("units", "")
           and "year" in card.get("inc_u5", {}).get("units", "")
           and all(str(v.get("label") or "").strip() for v in card.values()),
           f"S4 the card declares the series: under-5 prevalence (mean), all-age and under-5 incidence (rate per "
           f"person-year): {card}")
        r = HK.run_series(e, base, u, [full, none], hd, 5, metrics)
        t = r[0]["t"]
        ok([x["package"] for x in r] == ["full", "none"] and len(t) == 48 and abs(t[0] - 2015.0) < 1e-12
           and np.allclose(np.diff(t), 1 / 12) and t[-1] < 2019.0,
           f"S4b one entry per package in order, monthly steps over [horizon.start, window end): {len(t)} steps")
        v = r[1]["values"]
        ok(all(0.0 <= x <= 1.0 for x in v["pfpr_u5"]) and all(x >= 0 for x in v["inc_all"] + v["inc_u5"])
           and 0.05 < float(np.mean(v["inc_all"])) < 20,
           f"S4c prevalence is a share and incidence episodes per person-year (comparator mean all-age "
           f"{float(np.mean(v['inc_all'])):.2f})")
    case("S4", s4)

    def s5():
        outs, ser = e._run_members([base] * 2, [u] * 2, [none, full], hd, 5, series=metrics)
        pub = e.run_series(base, u, [none, full], hd, 5, metrics)
        ok([s["values"] for s in ser] == [p["values"] for p in pub] and [s["t"] for s in ser] == [p["t"] for p in pub],
           "S5 run_series returns the member loop's series")
        for o, s, pb in zip(outs, ser, pub):
            t, n = np.asarray(s["t"]), np.asarray(s["days"], float)
            ends = np.append(t[1:], float(hd.window[1]))
            for w, (a, b) in wins.items():
                m = (t >= a - 1e-9) & (t < b - 1e-9)
                p = float((np.asarray(s["values"]["pfpr_u5"])[m] * n[m]).sum() / n[m].sum())
                i5 = float((np.asarray(s["values"]["inc_u5"])[m] * (ends - t)[m]).sum())
                ia = float((np.asarray(s["values"]["inc_all"])[m] * (ends - t)[m]).sum())
                W = o.windows[w]
                ok(abs(p - W["pfpr_u5"]) < 1e-9 and abs(i5 - W["inc_u5"]) < 1e-9 * max(1, W["inc_u5"])
                   and abs(ia - W["inc_all"]) < 1e-9 * max(1, W["inc_all"]),
                   f"S5b ({s['package']}, {w}) the series over the window equal the window metrics: pfpr_u5 "
                   f"{p:.6f} vs {W['pfpr_u5']:.6f}, inc_u5 {i5:.6f} vs {W['inc_u5']:.6f}, inc_all {ia:.6f} vs "
                   f"{W['inc_all']:.6f}")
                pm = float(np.mean(np.asarray(pb["values"]["pfpr_u5"])[m]))
                ok(abs(pm - W["pfpr_u5"]) <= 0.02 * max(W["pfpr_u5"], 1e-3),
                   f"S5c ({s['package']}, {w}) the plain mean of the public monthly prevalence is within 2% of the "
                   f"window metric ({pm:.4f} vs {W['pfpr_u5']:.4f})")
    case("S5", s5)

    def s6():
        a, b = e.run_series(base, u, [none, full], hd, 6, metrics)
        t = np.asarray(a["t"])
        pre = t < 2016.0 - 1e-9
        ok(all(np.array_equal(np.asarray(a["values"][m])[pre], np.asarray(b["values"][m])[pre]) for m in metrics)
           and any(not np.array_equal(np.asarray(a["values"][m])[~pre], np.asarray(b["values"][m])[~pre])
                   for m in metrics),
           "S6 packages share their history: identical series before the first component event, different after")
    case("S6", s6)

    def s7():
        strip = lambda o: (o.per_capita, o.windows, o.cost_drivers,  # noqa: E731
                           {k: v for k, v in o.meta.items() if k != "seconds"})
        pk = [none, full, smc_only]
        a = [strip(o) for o in e.run_batch([base] * 3, u, pk, hd, 7)]
        outs, _ = e._run_members([base] * 3, [u] * 3, pk, hd, 7, series=metrics)
        ok(a == [strip(o) for o in outs], "S7 recording the series leaves the outcomes byte-identical to run_batch's")
    case("S7", s7)

    def s8():
        try:
            HK.run_series(e, base, u, [none], hd, 1, ["no_such_metric"])
            ok(False, "S8 a metric the card does not list is refused")
        except HK.HookInvalid as ex:
            ok("not in the card" in str(ex), f"S8 a metric the card does not list is refused ({ex})")
    case("S8", s8)

    plain = {k: v for k, v in attrs.items() if k not in ("smc_routine", "itn_routine_interval")}
    hw = Horizon(2015.0, 2019.0, (2016.0, 2019.0), {})

    def refused(fn):
        try:
            fn()
            return None
        except ValueError as ex:
            return str(ex)

    def s9():
        # Codex r1 P2-2: a decision event the sim delivers but does not charge (counting is by in_delivery())
        ux, net = UnitSpec("t", dict(plain, campaign_year=2015.5)), Package("pyr", {"net": "pyr"})
        o = e.run(base, ux, net, hw, 3)
        why = refused(lambda: e.schedule(base, ux, net, hw))
        ok(o.cost_drivers["nets_given"] == 0 and why is not None and "charged" in why,
           f"S9 a decision campaign the sim delivers uncharged (nets_given {o.cost_drivers['nets_given']}) makes "
           f"schedule() fail closed ({why})")
        hm = Horizon(2015.0, 2019.0, (2016.5, 2019.0), {})
        us, smc1 = UnitSpec("t", dict(plain, campaign_year=2016.5, smc_months=[5, 6, 7, 8])), Package("s", {"smc": 1})
        o2 = e.run(base, us, smc1, hm, 3)
        why2 = refused(lambda: e.schedule(base, us, smc1, hm))
        ok(o2.cost_drivers["smc_doses"] > 0 and why2 is not None and "charged" in why2,
           f"S9b SMC rounds before a mid-year window start run uncharged: schedule() fails closed ({why2})")
        for pkg, drv in ((Package("n", {"net": "pyr"}), "nets_given"), (smc1, "smc_doses"),
                         (Package("i", {"irs": 1}), "irs_sprayed")):
            ev = [x for x in e.schedule(base, u, pkg, hd) if x["charged"]]
            oo = e.run(base, u, pkg, hd, 3)
            ok(ev and oo.cost_drivers[drv] > 0,
               f"S9c ({pkg.id}) the charged events are charged by the sim ({len(ev)} events, {drv} "
               f"{oo.cost_drivers[drv]:.3f})")
    case("S9", s9)

    def s10():
        # Codex r1 P2-3: repeated or same-day targets are one delivery in the sim
        from toolkit.engines.malaria import model as MM
        ud = UnitSpec("t", dict(plain, itn_hist_years=[2015.25, 2015.25, 2015.25 + 0.2 / 365]))
        ev = [x for x in e.schedule(base, ud, none, hw) if x["kind"] == "history"]
        sim, _ = e.build_sim(base, ud, none, hw, 3)
        sim.init()
        ran = MM._event_tis(sim, sim.interventions["itn_hist"].years)
        ok(len(ev) == len(ran) == 1, f"S10 three past-campaign targets on one daily step are one event, as the sim "
                                     f"runs them ({len(ev)} listed, {len(ran)} executed)")
    case("S10", s10)

    def s11():
        # Codex r1 P2-4: the public series carry their day counts and step ends, and reconcile exactly
        w1 = 2018.0 + 1 / 365                         # off the monthly grid: a final step of one or two days
        wn = {"all": [2015.0, w1], "last": [2015.0 + 35 / 12, w1], "y1": [2016.0, 2017.0]}
        hp = DeliveryHorizon(2015.0, 2018.1, (2016.0, w1), dict(wn), delivery=(2016.0, 2017.0))
        r = HK.run_series(e, base, u, [none, full], hp, 5, metrics)
        outs = e.run_batch([base] * 2, u, [none, full], hp, 5)
        ok(all(len(x["days"]) == len(x["t"]) == len(x["t_end"]) and abs(x["t_end"][-1] - w1) < 1e-12 for x in r),
           "S11 each public entry carries the day count and the end of every step")
        for x, o in zip(r, outs):
            for w, (a0, b0) in wn.items():
                got = {m: e.series_window(x, m, a0, b0) for m in metrics}
                W = o.windows[w]
                ok(all(abs(got[m] - W[m]) <= 1e-9 * max(1.0, abs(W[m])) for m in metrics),
                   f"S11b ({x['package']}, {w}) the public series reconcile exactly with the window metrics: "
                   f"{ {m: (round(got[m], 6), round(W[m], 6)) for m in metrics} }")
        why = refused(lambda: e.series_window(r[0], "pfpr_u5", 2016.04, 2017.0))
        ok(why is not None and "step" in why, f"S11c a window that cuts through a monthly step fails closed ({why})")
    case("S11", s11)

    def s12():
        # #127 r1 (H9-H12): the hooks contract version, series metadata, the monthly grid and schedule semantics
        ok(getattr(e, "hooks_contract", None) == "figures-hooks/1", "S12 the engine declares hooks contract "
                                                                     "figures-hooks/1")
        card = e.describe().outputs.get("series") or {}
        ok(card and all(all(isinstance(v.get(k), str) and v.get(k).strip() for k in ("label", "units", "aggregate",
                                                                                     "denominator"))
                        and v["aggregate"] in ("mean", "rate") for v in card.values()),
           f"S12b every series metric has label, units, aggregate and denominator: "
           f"{ {k: v.get('denominator') for k, v in card.items()} }")
        w1 = 2018.0 + 1 / 365
        hp = DeliveryHorizon(2015.0, 2018.1, (2016.0, w1), {}, delivery=(2016.0, 2017.0))
        for h in (hd, hp):
            r = HK.run_series(e, base, u, [none, full], h, 4, ["pfpr_u5"])
            n = int(np.ceil((float(h.window[1]) - float(h.start)) * 12 - 1e-9))
            ok(all(len(x["t"]) == n and all(abs(x["t"][k] - (float(h.start) + k / 12)) < 1e-6 for k in range(n))
                   and x["t"] == HK.monthly_grid(h) and abs(x["t_end"][-1] - float(h.window[1])) < 1e-12
                   for x in r) and r[0]["t"] == r[1]["t"],
               f"S12c the grid is hooks.monthly_grid: every step start horizon.start + k/12 before the window end "
               f"({n}; a partial last step ending at the window end), the same for every arm")
        d0, d1 = delivery_period(hd)
        for pkg in (none, full, smc_only):
            ev = HK.schedule(e, base, u, pkg, hd)
            setc = {k for k, v in pkg.components.items() if v not in (0, None, "none")}
            bad = [x for x in ev if not (float(hd.start) <= x["t0"] <= float(hd.window[1]))
                   or (x["t1"] is not None and not float(hd.start) <= x["t1"] <= float(hd.window[1]))
                   or (x["kind"] == "decision" and (not d0 <= x["t0"] <= d1 or x["component"] not in setc))
                   or (x["kind"] == "history" and not x["t0"] < d0)
                   or (x["kind"] == "routine" and d0 <= x["t0"] < d1)]
            ok(not bad, f"S12d ({pkg.id}) every event keeps #127's schedule semantics: {bad[:2]}")
        why = refused(lambda: e.schedule(base, UnitSpec("t", dict(attrs, itn_hist_years=[2016.5])), none, hd))
        ok(why is not None and "history" in why, f"S12e a past campaign inside the delivery period fails closed in "
                                                 f"schedule() ({why})")
    case("S12", s12)

    def s13():
        # Codex r2 P2: a window outside the series' coverage is refused, not truncated
        t = [2015.0 + k / 12 for k in range(48)]
        ent = {"package": "x", "t": t, "t_end": t[1:] + [2019.0], "days": [30] * 48,
               "values": {"pfpr_u5": [0.1] * 48, "inc_u5": [1.0] * 48}}
        for a0, b0, nm in ((2018.0, 2020.0, "past the series end"), (2014.0, 2016.0, "before the series start"),
                           (2014.5, 2020.0, "both sides")):
            why = refused(lambda: e.series_window(ent, "inc_u5", a0, b0))
            ok(why is not None and "coverage" in why, f"S13 a window {nm} ([{a0:g}, {b0:g})) is refused, not truncated "
                                                      f"({why})")
        ok(abs(e.series_window(ent, "inc_u5", 2018.0, 2019.0) - 1.0) < 1e-12
           and abs(e.series_window(ent, "pfpr_u5", 2015.0, 2019.0) - 0.1) < 1e-12,
           "S13b windows covered by whole steps inside the coverage are aggregated")
    case("S13", s13)

    def s14():
        # #127 r2 (P2-11): half-open delivery semantics at each boundary, against what the sim does
        from toolkit.engines.malaria import model as MM
        d0, d1 = delivery_period(hd)
        net = Package("pyr", {"net": "pyr"})
        ev = e.schedule(base, UnitSpec("t", dict(attrs, campaign_year=d0)), net, hd)
        dec = [x for x in ev if x["kind"] == "decision"]
        o = e.run(base, UnitSpec("t", dict(attrs, campaign_year=d0)), net, hd, 3)
        ok(len(dec) == 1 and dec[0]["t0"] == d0 and dec[0]["charged"] and o.cost_drivers["nets_given"] > 0,
           f"S14 a decision campaign at the delivery start d0 is inside, charged in the schedule and by the sim "
           f"(nets_given {o.cost_drivers['nets_given']:.3f})")
        for at, pkg, label, needle in (
                ({"campaign_year": d1}, net, "S14b a decision campaign at the delivery end d1 is refused",
                 "delivery period"),
                ({"itn_hist_years": [d0]}, net, "S14c a past campaign at d0 is refused", "history"),
                ({"campaign_year": d0 - 0.5, "itn_routine_interval": 0.5}, none,   # routine nets run in every arm
                 "S14d a routine campaign at d0 is refused", "routine")):
            why = refused(lambda: e.schedule(base, UnitSpec("t", dict(attrs, **at)), pkg, hd))
            ok(why is not None and needle in why, f"{label} ({why})")
        ur = UnitSpec("t", dict(attrs, campaign_year=d0, itn_routine_interval=1.0, smc_months=[1, 2, 3, 4]))
        ev = e.schedule(base, ur, Package("ps", {"net": "pyr", "smc": 1}), hd)
        rn = sorted(x["t0"] for x in ev if x["kind"] == "routine" and x["component"] == "net")
        rs = sorted(x["t0"] for x in ev if x["kind"] == "routine" and x["component"] == "smc")
        sim, _ = e.build_sim(base, ur, Package("ps", {"net": "pyr", "smc": 1}), hd, 3)
        sim.init()
        yv = np.asarray(sim.t.yearvec)
        ran = sorted(float(yv[i]) for i in MM._event_tis(sim, sim.interventions["smc_routine"].targets()))
        ok(rn and rn[0] == d1 and rs and rs[0] == d1 and rs == [t for t in ran if t < float(hd.window[1])]
           and all(x["t0"] < float(hd.window[1]) for x in ev),
           f"S14e a routine campaign and the first routine SMC round at d1 are accepted, as the sim runs them "
           f"(nets {rn}, first SMC {rs[:1]})")
        g = DeliveryHorizon(2023.0, 2029.25, (2024.0, 2029.25), {}, delivery=(2024.0, 2027.0))
        ug = UnitSpec("t", dict(attrs, itn_hist_years=[2021.25], campaign_year=2024.25, itn_routine_interval=3.0))
        evg = HK.schedule(e, {"n_agents": 200}, ug, Package("ps", {"net": "pyr", "smc": 1}), g)
        rg = sorted(round(x["t0"], 3) for x in evg if x["kind"] == "routine" and x["component"] == "net")
        sg = [x["t0"] for x in evg if x["kind"] == "routine" and x["component"] == "smc"]
        ok(rg in ([2027.249], [2027.25]) and bool(sg) and min(sg) >= 2027.0,
           f"S14f GC7 dates: the routine campaign at campaign + 3 ({rg}) and routine SMC from d1 (first "
           f"{round(min(sg), 3) if sg else None}) lie outside the delivery period; #127's checker accepts them")
    case("S14", s14)

    def s15():
        # #127 r3: exact half-open boundaries; the hook reports and charges each event on the side the sim does,
        # checked on the real time vector (Starsim rounds it to 9 decimals: whole years from the sim start are exact)
        from toolkit.engines.malaria import model as MM
        net = Package("pyr", {"net": "pyr"})
        g = DeliveryHorizon(2023.0, 2029.25, (2024.0, 2029.25), {}, delivery=(2024.0, 2027.0))
        gp = {"n_agents": 200}                       # the card's 50-year burn-in: the real GC7 time vector
        ug = dict(attrs, itn_hist_years=[2023.0], campaign_year=2024.0, smc_months=[1, 2, 3, 4], itn_routine_interval=3.0)
        ev = HK.schedule(e, gp, UnitSpec("t", ug), Package("ps", {"net": "pyr", "smc": 1}), g)
        P = resolve_params(e, gp)
        yv = e._yearvec(P, g)
        acc = e.delivery_plan(ug, {"net": "pyr", "smc": 1}, g)["accounting"]
        dec = [x for x in ev if x["kind"] == "decision" and x["component"] == "net"]
        his = [x for x in ev if x["kind"] == "history"]
        rsm = sorted(x["t0"] for x in ev if x["kind"] == "routine" and x["component"] == "smc")
        rnt = [x["t0"] for x in ev if x["kind"] == "routine" and x["component"] == "net"]
        ok(dec and dec[0]["t0"] == 2024.0 and dec[0]["charged"] and MM.delivery_contains(dec[0]["t0"], **acc)
           and his and his[0]["t0"] == 2023.0 and rsm and rsm[0] == 2027.0
           and not MM.delivery_contains(rsm[0], **acc) and rnt == [float(yv[min(MM.event_steps(yv, [2027.0]))])],
           f"S15 on the GC7 time vector the decision at d0 is exactly 2024.0 and charged, the past campaign at "
           f"horizon.start is exactly 2023.0, the first routine SMC round is exactly d1 and uncharged, and the "
           f"routine net at campaign + 3 is on its executed step ({rnt})")
        ok(all(x["t0"] < 2029.25 for x in ev) and all(x["t1"] is None or x["t1"] <= 2029.25 for x in ev),
           f"S15b nothing is listed at or after the window end (last t0 {max(x['t0'] for x in ev):.6f})")
        # a start off the day grid: d0 and d1 fall between steps; the nearest steps lie after both
        g2 = DeliveryHorizon(2023.25, 2029.25, (2024.0, 2029.25), {}, delivery=(2024.0, 2027.0))
        ev2 = e.schedule({"n_agents": 200, "burnin_years": 20}, UnitSpec("t", ug), Package("ps", {"net": "pyr", "smc": 1}), g2)
        d2 = [x for x in ev2 if x["kind"] == "decision" and x["component"] == "net"]
        r2 = sorted(x["t0"] for x in ev2 if x["kind"] == "routine" and x["component"] == "smc")
        ok(d2 and 2024.0 < d2[0]["t0"] < 2024.002 and d2[0]["charged"] and r2 and 2027.0 < r2[0] < 2027.002,
           f"S15c with an off-grid start, the decision at d0 runs on the next step ({d2[0]['t0'] if d2 else None}) "
           f"and is charged; the routine round at d1 runs after d1 ({r2[:1]})")
        # a delivery start between steps whose nearest step lies before it: the sim does not charge the campaign
        g3 = DeliveryHorizon(2023.0, 2025.0, (2024.0, 2025.0), {}, delivery=(2024.25, 2025.0))
        u3 = UnitSpec("t", dict(plain, campaign_year=2024.25))
        o3 = e.run({"n_agents": 300, "burnin_years": 2}, u3, net, g3, 3)
        why = refused(lambda: e.schedule({"n_agents": 300, "burnin_years": 2}, u3, net, g3))
        ok(o3.cost_drivers["nets_given"] == 0 and why is not None and "charged" in why,
           f"S15d a campaign at a delivery start the nearest step precedes is not charged by the sim "
           f"(nets_given {o3.cost_drivers['nets_given']}), and schedule() fails closed ({why})")
        # representation: an executed step one ulp-scale off d0 is reported at d0 when it is on d0's side, and
        # never moved across it
        dp = e.delivery_plan(ug, {"net": "pyr"}, g)
        i0 = min(MM.event_steps(yv, [2024.0]))
        up, dn = yv.copy(), yv.copy()
        up[i0], dn[i0] = 2024.0 + 1e-12, 2024.0 - 1e-12
        a = [x for x in e.project_schedule(dp, up, g) if x["kind"] == "decision" and x["component"] == "net"]
        why2 = refused(lambda: e.project_schedule(dp, dn, g))
        ok(a and a[0]["t0"] == 2024.0 and a[0]["charged"] and why2 is not None and "charged" in why2,
           f"S15e a step 1e-12 after d0 is reported exactly at d0; one 1e-12 before it is not charged by the sim's "
           f"test and is refused, not moved ({why2})")
    case("S15", s15)


def _self_test_v09(ok, e, unit) -> None:
    """v0.9: net loss follows the smooth-compact survival function of Bertozzi-Villa 2021 (Supplementary Note 1.4),
    S(t) = exp(k - k / (1 - (t/L)^2)) for t < L and 0 after, with k fixed at 20 and L set by the median retention
    (net_retention_half). Each day a net of age a is lost with the conditional hazard 1 - S(a+1)/S(a)."""
    def case(label, fn):
        try:
            fn()
        except Exception as ex:  # noqa: BLE001 -- a missing feature is a failing case, not a crash of the test
            ok(False, f"{label} ({type(ex).__name__}: {ex})")

    P0 = resolve_params(e, {})

    def n0():
        specs = {p.name: p for p in e.parameters()}
        k, med = specs.get("net_survival_k"), specs["net_retention_half"]
        ok(tuple(int(x) for x in VERSION.split(".")) >= (0, 9) and k is not None and k.kind == "fixed" and k.value == 20.0 and k.lo is None
           and "Supplementary Note 1.4" in k.source and med.value == 1.64 * 365 and med.lo == 1.33 * 365
           and med.hi == 2.37 * 365 and "median" in med.source and "exponential" not in med.source,
           "N0 engine v0.9: k = 20 is a fixed, sourced parameter; net_retention_half stays the median (1.64 y, card "
           "range 1.33-2.37 y) and no longer reads as an exponential half-life")
    case("N0", n0)

    def n1():
        k = 20.0
        for med in (365.0, 1.64 * 365, 949.0):
            L = M.net_survival_L(med, k)
            ok(abs(L - med * np.sqrt((k + np.log(2)) / np.log(2))) < 1e-9 * L,
               f"N1a L = median x sqrt((k + ln2) / ln2) ({med:g} d -> L {L:.1f} d)")
            s = M.net_survival(np.array([0.0, med, L, L + 1.0, 2 * L]), med, k)
            ok(abs(s[0] - 1.0) < 1e-12 and abs(s[1] - 0.5) < 1e-9 and s[2] == 0.0 and s[3] == 0.0 and s[4] == 0.0,
               f"N1b S(0) = 1, S(median) = 0.5, S(t) = 0 for t >= L (median {med:g} d): {s.round(12).tolist()}")
            g = M.net_survival(np.linspace(0, L, 400), med, k)
            ok(bool(np.all(np.diff(g) <= 0)) and np.all(np.isfinite(g)), f"N1c S is finite and non-increasing ({med:g} d)")
        ok(abs(M.net_survival_L(1.64 * 365, 20.0) / 365 - 8.96) < 0.01,
           "N1d the continental median 1.64 y gives L = 8.96 y (all nets gone by then)")
        s_off = M.net_survival(np.array([0.0, 1000.0, 3650.0]), 1e9, 20.0)
        ok(np.all(np.isfinite(s_off)) and float(s_off.min()) > 1 - 1e-9,
           "N1e a very long median (1e9 d: attrition switched off) keeps every net, without overflow")
    case("N1", n1)

    def n2():
        med, k = 300.0, 20.0
        L = M.net_survival_L(med, k)
        a = np.arange(0, int(L) + 3, dtype=float)
        p = M.net_loss_prob(a, med, k)
        s = M.net_survival(a, med, k)
        want = np.where(s > 0, 1 - M.net_survival(a + 1, med, k) / np.where(s > 0, s, 1.0), 1.0)
        ok(np.allclose(p, want, atol=1e-12) and np.all((p >= 0) & (p <= 1)) and p[-1] == 1.0,
           "N2a the daily loss probability is the conditional hazard 1 - S(a+1)/S(a), and 1 at or past L")
        kept = np.concatenate([[1.0], np.cumprod(1 - p)])[: len(a)]
        ok(np.allclose(kept, s, atol=1e-9), "N2b compounding the daily hazard from age 0 reproduces S(t) at every age")
    case("N2", n2)

    def n3():
        # agent level: nets of four ages at the start; after T days the share of each group still holding its net is
        # S(a0 + T) / S(a0), among the people alive throughout (deaths remove people, not nets)
        med = 365.0
        hz = Horizon(2019.0, 2020.0, (2019.0, 2020.0), {})
        u = UnitSpec("t", dict(unit.attributes, itn_hist_years=[], campaign_year=2019.5, irs_month=None))
        sim, _ = e.build_sim({"n_agents": 4000, "burnin_years": 0, "net_retention_half": med}, u, Package("n", {}), hz, 5)
        sim.init()
        dz = sim.diseases.malaria
        uu = np.asarray(sim.people.auids)
        ages0 = np.array([0.0, 180.0, 365.0, 600.0])
        grp = np.arange(len(uu)) % len(ages0)
        dz.has_net[sim.people.auids] = True
        dz.net_t[sim.people.auids] = -ages0[grp]
        sim.run()
        T = float(len(sim.timevec))
        alive = np.isin(uu, np.asarray(sim.people.auids))
        kept = np.asarray(dz.has_net.raw)[uu]          # by uid, including people who died (masked out below)
        rows = []
        for i, a0 in enumerate(ages0):
            m = alive & (grp == i)
            want = float(M.net_survival(a0 + T, med, 20.0) / M.net_survival(a0, med, 20.0))
            got, nn = float(kept[m].mean()), int(m.sum())
            sd = np.sqrt(max(want * (1 - want), 1e-4) / nn)
            exp_ = float(0.5 ** (T / med))
            rows.append((a0, round(got, 3), round(want, 3), round(exp_, 3), abs(got - want) < 4 * sd + 0.005))
        ok(all(r[-1] for r in rows), f"N3 agent-level loss matches S(a0 + T) / S(a0) by net age (age, kept, "
                                     f"smooth-compact, exponential): {rows}")
    case("N3", n3)

    def n4():
        rows = []
        for use, t in ((0.6, 0.0), (0.5, 0.54), (0.3, 1.64), (0.2, 3.0)):
            acc, capped = access_for_use(use, t, P0)
            s = float(M.net_survival(t * 365.0, P0["net_retention_half"], P0["net_survival_k"]))
            need = use / (s * P0["use_rate"])
            rows.append(abs(acc - min(need, 1.0)) < 1e-12 and capped == (need > 1.0))
        a1, _ = access_for_use(0.4, 1.0, P0)
        s1 = float(M.net_survival(365.0, P0["net_retention_half"], P0["net_survival_k"]))
        ok(all(rows) and abs(a1 - 0.4 / (s1 * 0.871)) < 1e-12 and abs(s1 - 0.5 ** (1 / 1.64)) > 0.05,
           f"N4 access at distribution divides by the same S(t) the sim uses (S(1 y) = {s1:.3f}, not the "
           f"exponential {0.5 ** (1 / 1.64):.3f})")
    case("N4", n4)

    def n5():
        # the Muleba setting keeps its own trial-derived retention: reported use 77.3% in year 1 (surveys at 4 and 9
        # months, mean 0.54 y) and 59.2% in year 2 (16 and 21 months, mean 1.54 y; Protopopoff 2018). Under the
        # engine's survival curve its median must give that ratio (v0.3 derived 949 d for the exponential)
        pk = yaml.safe_load(open(os.path.join(HERE, "benchmarks", "pack.yaml")))
        med = float(pk["settings"]["muleba"]["params"]["net_retention_half"])
        r = float(M.net_survival(1.54 * 365, med, P0["net_survival_k"]) / M.net_survival(0.54 * 365, med, P0["net_survival_k"]))
        ok(abs(r - 0.592 / 0.773) < 0.002, f"N5 Muleba's median retention ({med:g} d) reproduces its trial's year-2 / "
                                           f"year-1 use ratio under S(t): {r:.4f} vs {0.592 / 0.773:.4f}")
    case("N5", n5)

    def n6():
        # review r1 (P2): a non-positive or non-finite shape or median fails closed (k < 0 gives S > 1)
        bad = []
        for med, k in ((598.6, 0.0), (598.6, -0.5), (598.6, float("nan")), (598.6, float("inf")), (0.0, 20.0),
                       (-1.0, 20.0), (float("nan"), 20.0), (float("inf"), 20.0)):
            for fn in (M.net_survival, M.net_loss_prob):
                try:
                    fn(np.array([100.0]), med, k)
                    bad.append((fn.__name__, med, k))
                except ValueError:
                    pass
        try:
            access_for_use(0.5, 1.0, dict(P0, net_survival_k=-0.5))
            bad.append(("access_for_use", "k", -0.5))
        except ValueError:
            pass
        ok(not bad, f"N6 the survival helpers and access_for_use refuse k or a median that is not finite and positive "
                    f"(accepted: {bad})")
    case("N6", n6)

    def n7():
        # review r1 (P2): the hazard's net age on campaign days. Two campaigns reach the same people (the second
        # replaces nets they still hold); the age passed to net_loss_prob is 0 on each campaign's step and 1 the day after
        hz = Horizon(2019.0, 2020.0, (2019.0, 2020.0), {})
        u = UnitSpec("t", dict(unit.attributes, itn_hist_years=[2019.2, 2019.6], itn_use=0.6, campaign_year=2019.9,
                               irs_month=None))
        sim, _ = e.build_sim({"n_agents": 800, "burnin_years": 0, "net_retention_half": 1e9}, u, Package("n", {}), hz, 3)
        rec, orig = [], M.net_loss_prob

        def spy(age, med, k):
            rec.append((np.asarray(sim.people.auids).copy(), np.asarray(age, float).copy()))
            return orig(age, med, k)
        M.net_loss_prob = spy
        try:
            sim.run()
        finally:
            M.net_loss_prob = orig
        s1, s2 = sorted(M.event_steps(sim.t.yearvec, [2019.2, 2019.6]))
        dz = sim.diseases.malaria
        held = set(np.asarray(sim.people.auids)[np.asarray(dz.has_net[sim.people.auids], bool)].tolist())
        at = {}
        for s in (s1, s1 + 1, s2, s2 + 1):
            ids, ages = rec[s]
            at[s] = {int(i): float(a) for i, a in zip(ids, ages) if int(i) in held}
        both = set(at[s1]) & set(at[s2 + 1])
        ok(len(rec) == len(sim.timevec) and len(both) > 100
           and all(at[s1][i] == 0 and at[s1 + 1][i] == 1 and at[s2][i] == 0 and at[s2 + 1][i] == 1 for i in both),
           f"N7 net age in the loss hazard is 0 on a campaign's step and 1 the next day, for first nets and for "
           f"replacements of nets still held ({len(both)} people, campaign steps {s1}, {s2})")
    case("N7", n7)


def _self_test_v010(ok, e, unit) -> None:
    """v0.10: past SMC (smc_hist_years, optional smc_hist_rounds) and a net type per past net campaign
    (hist_net_by_year). Past SMC runs through the same SMC intervention as the package's, uncharged, before the
    delivery period, in every arm. With the attributes absent or empty a run is byte-identical to v0.9 (H3;
    benchmarks/restamp_proof.py compares the v0.9 and v0.10 code)."""
    def case(label, fn):
        try:
            fn()
        except Exception as ex:  # noqa: BLE001 -- a missing feature is a failing case, not a crash of the test
            ok(False, f"{label} ({type(ex).__name__}: {ex})")

    from toolkit.engines import hooks as HK
    from toolkit.engines.horizon import DeliveryHorizon
    from toolkit.engines.malaria import model as MM
    base = {"n_agents": 800, "burnin_years": 2}
    season = [0.4, 0.4, 0.5, 0.7, 1.0, 1.4, 1.8, 2.0, 1.6, 1.0, 0.7, 0.5]
    months = default_smc_months(np.asarray(season))          # (6, 7, 8, 9) for this season
    wins = {"y14": [2014.0, 2015.0], "y15": [2015.0, 2016.0], "d": [2016.0, 2017.0]}
    attrs = dict(unit.attributes, season=season, itn_hist_years=[2014.3, 2015.2], campaign_year=2016.1, irs_month=4)
    hd = DeliveryHorizon(2014.0, 2018.0, (2016.0, 2018.0), dict(wins), delivery=(2016.0, 2017.0))
    # the runs below use a shorter horizon (CI time): 2015 for past SMC and one delivery year, one burn-in year
    hs = DeliveryHorizon(2015.0, 2017.0, (2016.0, 2017.0), {"y15": [2015.0, 2016.0], "d": [2016.0, 2017.0]},
                         delivery=(2016.0, 2017.0))
    small = {"n_agents": 300, "burnin_years": 1}
    none, smc = Package("none", {}), Package("smc", {"smc": 1})
    full = Package("full", {"net": "pyr", "smc": 1, "irs": 1, "cm": 1})
    strip = lambda o: (o.per_capita, o.windows, o.cost_drivers,  # noqa: E731
                       {k: v for k, v in o.meta.items() if k != "seconds"})

    def refused(at, needle, label, pkg=none):
        try:
            e.schedule(base, UnitSpec("t", dict(attrs, **at)), pkg, hd)
            ok(False, label)
        except ValueError as ex:
            ok(needle in str(ex), f"{label} ({ex})")

    def h0():
        ua = e.describe().unit_attributes
        ok(tuple(int(x) for x in VERSION.split(".")) >= (0, 10)
           and {"smc_hist_years", "smc_hist_rounds", "hist_net_by_year"} <= set(ua),
           "H0 engine v0.10 declares smc_hist_years, smc_hist_rounds and hist_net_by_year on its card")
        b = yaml.safe_load(open(os.path.join(HERE, "attribute_binding.yaml")))["attribute_binding"]
        ok(all(isinstance(b.get(k), dict) and "default" in b[k] and str(b[k].get("meaning") or "").strip()
               for k in ("smc_hist_years", "smc_hist_rounds", "hist_net_by_year"))
           and str(b["smc_hist_years"]["default"]) == "[]" and str(b["hist_net_by_year"]["default"]) == "none",
           "H0b attribute_binding.yaml gives each new attribute a default and its meaning (no past SMC; every past "
           "campaign's net type is hist_net)")
    case("H0", h0)

    def h1():
        uh = UnitSpec("t", dict(attrs, smc_hist_years=[2014, 2015]))
        want = sorted(y + (m - 1) / 12 for y in (2014, 2015) for m in months)
        for pkg in (none, full):
            ev = e.schedule(base, uh, pkg, hd)
            past = [x for x in ev if x["component"] == "smc" and x["kind"] == "history"]
            ok(len(past) == len(want) and all(abs(x["t0"] - t) < 1.0 / 365 for x, t in zip(past, want))
               and all(x["charged"] is False and x["label"] == "past SMC round" for x in past),
               f"H1 ({pkg.id}) the schedule lists the past SMC rounds at their dates, as history, uncharged: "
               f"{[round(x['t0'], 3) for x in past]}")
            sim, _ = e.build_sim(base, uh, pkg, hd, 3)
            sim.init()
            itv, yv = sim.interventions, np.asarray(sim.t.yearvec)
            ran = sorted(round(float(yv[i]), 9) for i in MM._event_tis(sim, itv["smc_hist"].targets()))
            ok(ran == sorted(round(x["t0"], 9) for x in past) and isinstance(itv["smc_hist"], MM.SMCDelivery)
               and itv["smc_hist"].count is False,
               f"H1b ({pkg.id}) the sim runs past SMC through the SMC intervention, uncounted, on the listed steps")
        HK.schedule(e, base, uh, full, hd)      # the hooks checker accepts history SMC before the delivery period
        ur = UnitSpec("t", dict(attrs, smc_hist_years=[2014, 2015], smc_hist_rounds=[2, 3]))
        near = lambda a, b: len(a) == len(b) and all(abs(x - y) < 1.0 / 365 for x, y in zip(a, b))  # noqa: E731
        got = sorted(x["t0"] for x in e.schedule(base, ur, none, hd) if x["component"] == "smc")
        w2 = sorted(y + (m - 1) / 12 for y, k in ((2014, 2), (2015, 3)) for m in months[:k])
        ok(near(got, w2), f"H1c smc_hist_rounds gives each past year its first n SMC months: {[round(x, 3) for x in got]}")
        us = UnitSpec("t", dict(attrs, smc_hist_years=[2015], smc_hist_rounds=2))
        got = sorted(x["t0"] for x in e.schedule(base, us, none, hd) if x["component"] == "smc")
        ok(near(got, sorted(2015 + (m - 1) / 12 for m in months[:2])),
           f"H1d one number in smc_hist_rounds applies to every past year: {[round(x, 3) for x in got]}")
    case("H1", h1)

    def h2():
        uh = UnitSpec("t", dict(attrs, smc_hist_years=[2015]))
        u0 = UnitSpec("t", dict(attrs))
        p = {"n_agents": 1000, "burnin_years": 1}
        # one run per unit gives the series and the outcomes (run_series reads run_batch's member loop, S7)
        (oh,), (sh,) = e._run_members([p], [uh], [none], hs, 5, series=["inc_u5"])
        (o0,), (s0,) = e._run_members([p], [u0], [none], hs, 5, series=["inc_u5"])
        t = np.asarray(sh["t"])
        first = 2015 + (months[0] - 1) / 12
        pre = t < first - 1e-9
        ok(np.array_equal(np.asarray(sh["values"]["inc_u5"])[pre], np.asarray(s0["values"]["inc_u5"])[pre]),
           "H2 the trajectory is identical before the first past SMC round")
        on = (t >= first - 1e-9) & (t < 2015 + months[-1] / 12 - 1e-9)
        a, b = float(np.sum(np.asarray(sh["values"]["inc_u5"])[on])), float(np.sum(np.asarray(s0["values"]["inc_u5"])[on]))
        ok(a < 0.7 * b, f"H2b under-5 clinical incidence is lower in the past SMC months of 2015 ({a:.3f} vs {b:.3f} "
                        "without past SMC)")
        paid = ("nets_given", "smc_doses", "irs_sprayed")           # treated_all follows the health outcomes
        ok(oh.windows["y15"]["inc_u5_smc"] < 0.7 * o0.windows["y15"]["inc_u5_smc"]
           and all(oh.cost_drivers[k] == o0.cost_drivers[k] for k in paid) and oh.cost_drivers["smc_doses"] == 0,
           f"H2c past SMC acts in 2015 ({oh.windows['y15']['inc_u5_smc']:.3f} vs {o0.windows['y15']['inc_u5_smc']:.3f}) "
           f"and is never charged ({oh.cost_drivers})")
    case("H2", h2)

    def h3():
        pk = [none, full]
        ref = [strip(o) for o in e.run_batch([small] * 2, UnitSpec("t", attrs), pk, hs, 5)]
        ref_names = None
        for extra, nm in (({"smc_hist_years": []}, "smc_hist_years []"),
                          ({"smc_hist_years": [], "smc_hist_rounds": 4}, "smc_hist_rounds without past years"),
                          ({"hist_net_by_year": {}}, "hist_net_by_year {}"),
                          ({"hist_net_by_year": ""}, "hist_net_by_year ''"),
                          ({"hist_net_by_year": "none"}, "hist_net_by_year 'none'"),
                          ({"hist_net_by_year": {2014.3: "pyr", 2015.2: "pyr"}}, "every campaign at hist_net")):
            ux = UnitSpec("t", dict(attrs, **extra))
            got = [strip(o) for o in e.run_batch([small] * 2, ux, pk, hs, 5)]
            ok(got == ref, f"H3 ({nm}) byte-identical outcomes, cost drivers and meta to the attributes absent")
            sim, _ = e.build_sim(small, ux, full, hs, 5)
            sim.init()
            names = list(sim.interventions.keys())
            if ref_names is None:
                s0, _ = e.build_sim(small, UnitSpec("t", attrs), full, hs, 5)
                s0.init()
                ref_names = list(s0.interventions.keys())
            ok(names == ref_names, f"H3b ({nm}) the same interventions as v0.9: {names}")
    case("H3", h3)

    def h4():
        for at, label in (({"smc_hist_years": "2014 2015"}, "a string, not a list"),
                          ({"smc_hist_years": 2015}, "a number, not a list"),
                          ({"smc_hist_years": [2014.5]}, "a fractional year"),
                          ({"smc_hist_years": [2015, 2015]}, "a repeated year"),
                          ({"smc_hist_years": [float("nan")]}, "NaN"),
                          ({"smc_hist_years": [True]}, "a boolean"),
                          ({"smc_hist_years": ["2015"]}, "a string year"),
                          ({"smc_hist_years": [2016]}, "a year in the delivery period"),
                          ({"smc_hist_years": [2015], "smc_hist_rounds": 0}, "0 rounds"),
                          ({"smc_hist_years": [2015], "smc_hist_rounds": 5}, "more rounds than SMC months"),
                          ({"smc_hist_years": [2015], "smc_hist_rounds": 2.5}, "fractional rounds"),
                          ({"smc_hist_years": [2014, 2015], "smc_hist_rounds": [4]}, "a rounds list of another length"),
                          ({"smc_hist_years": [2015], "smc_hist_rounds": "4"}, "rounds as a string")):
            key = "smc_hist_rounds" if "smc_hist_rounds" in at else "smc_hist_years"
            refused(at, key, f"H4 smc_hist_years / smc_hist_rounds: {label} fails closed")
        for at, label in (({"hist_net_by_year": {2015.2: "olyset"}}, "an unknown net type"),
                          ({"hist_net_by_year": {2015.25: "pbo"}}, "a year that is not a past campaign"),
                          ({"hist_net_by_year": "2015.2-pbo"}, "a malformed string"),
                          ({"hist_net_by_year": "2015.2:pbo 2015.2:dual"}, "a year given twice"),
                          ({"hist_net_by_year": ["pbo"]}, "a list"),
                          ({"hist_net_by_year": {"x": "pbo"}}, "a non-numeric year"),
                          ({"hist_net_by_year": {2015.2: "none"}}, "net type none")):
            refused(at, "hist_net_by_year", f"H4b hist_net_by_year: {label} fails closed")
        try:
            e.run(base, UnitSpec("t", dict(attrs, smc_hist_years=[2016.5])), none, hd, 1)
            ok(False, "H4c the sim path refuses a malformed past SMC year too")
        except ValueError as ex:
            ok("smc_hist_years" in str(ex), f"H4c the sim path refuses a malformed past SMC year too ({ex})")
    case("H4", h4)

    def h5():
        p = dict(base, n_agents=1000, burnin_years=0)
        yrs = [2014.3, 2015.2]
        for form, mp in (("mapping", {2015.2: "dual"}), ("string", "2015.2:dual"), ("string keys", {"2015.2": "dual"})):
            ux = UnitSpec("t", dict(attrs, hist_net="pbo", hist_net_by_year=mp, itn_routine_interval=1.0))
            ev = e.schedule(p, ux, none, hd)
            lab = [(round(x["t0"], 2), x["label"]) for x in ev if x["component"] == "net"]
            ok(lab[:2] == [(2014.3, "past net campaign (pbo)"), (2015.2, "past net campaign (dual)")]
               and all(lb == "routine net campaign (pbo)" for _, lb in lab[2:]) and len(lab) > 2,
               f"H5 ({form}) the schedule names each past campaign's own net type; routine campaigns keep hist_net: {lab}")
            sim, _ = e.build_sim(p, ux, none, hd, 4)
            sim.init()
            yv = np.asarray(sim.t.yearvec)
            s1, s2 = sorted(MM.event_steps(yv, yrs))
            dz = sim.diseases.malaria
            got = {}
            for s in (s1, s2):
                sim.run(until=yv[s])
                uu = sim.people.auids
                new = np.asarray(dz.net_t[uu]) == s
                got[s] = sorted(set(np.asarray(dz.net_type[uu])[new].tolist()))
            ok(got[s1] == [float(V.NET_CODE["pbo"])] and got[s2] == [float(V.NET_CODE["dual"])],
               f"H5b ({form}) the 2014.3 campaign gives hist_net (pbo) nets and the 2015.2 campaign dual-AI nets: {got}")
    case("H5", h5)

    def h6():
        uh = UnitSpec("t", dict(attrs, smc_hist_years=[2015]))
        ps = [dict(small, smc_cov=0.6), dict(small, smc_cov=0.9)]
        b = e.run_batch(ps, uh, none, hs, 3)
        solo = [e.run(q, uh, none, hs, 3) for q in ps]
        ok([o.meta["burnin_members"] for o in b] == [1, 1] and [strip(o) for o in b] == [strip(o) for o in solo],
           "H6 with past SMC, parameter sets that differ in smc_cov (v0.11; smc_pe up to v0.10) do not share a burn-in (it acts before the fork); "
           "each member equals its own run")
        b0 = e.run_batch(ps, UnitSpec("t", attrs), none, hs, 3)
        ok([o.meta["burnin_members"] for o in b0] == [2, 2], "H6b without past SMC they still share one (v0.9)")
        ud = UnitSpec("t", dict(attrs, hist_net_by_year={2015.2: "dual"}))
        pc = [dict(small, cfp_kill=0.2), dict(small, cfp_kill=0.6)]
        bd = e.run_batch(pc, ud, none, hs, 3)
        ok([o.meta["burnin_members"] for o in bd] == [1, 1]
           and [strip(o) for o in bd] == [strip(e.run(q, ud, none, hs, 3)) for q in pc],
           "H6c a past dual-AI campaign from hist_net_by_year puts cfp_kill in the burn-in key, as hist_net: dual does")
    case("H6", h6)

    def h7():
        # Codex r1 P1-1: without a delivery period the package's SMC may start in the window's first calendar year,
        # before the window; a past round must not coincide with (or follow) a package or routine SMC round
        hn = Horizon(2023.0, 2026.0, (2024.5, 2026.0), {})
        un = UnitSpec("t", dict(attrs, campaign_year=2024.5, smc_months=[5, 6, 7, 8], smc_hist_years=[2024],
                                smc_hist_rounds=2))
        for fn, nm in ((lambda: e.schedule(base, un, smc, hn), "schedule"), (lambda: e.run(base, un, smc, hn, 1), "run")):
            try:
                fn()
                ok(False, f"H7 ({nm}) a past SMC round on a package SMC round is refused")
            except ValueError as ex:
                ok("smc_hist_years" in str(ex), f"H7 ({nm}) a past SMC round on a package SMC round is refused ({ex})")
        # Codex r1 P1-2: a round dated before the delivery start that the sim runs on a step at or after it
        hb = DeliveryHorizon(2023.0, 2025.0, (2023.167, 2025.0), {}, delivery=(2023.167, 2024.0))
        ub = UnitSpec("t", dict(attrs, campaign_year=2023.5, smc_months=[3], smc_hist_years=[2023]))
        try:
            e.run({"n_agents": 200, "burnin_years": 0}, ub, none, hb, 1)
            ok(False, "H7b a past round executed at or after the delivery start is refused in the sim path")
        except ValueError as ex:
            ok("smc_hist_years" in str(ex), f"H7b a past round executed at or after the delivery start is refused in "
                                            f"the sim path ({ex})")
        # Codex r1 P1-3: SMC months the past rounds use must be whole, distinct months 1-12
        for mo in ([7.9], [0], [13], [True], [7, 7], False, 0, "6 7", 7):     # Codex r2 P1: falsy and non-list values too
            refused({"smc_months": mo, "smc_hist_years": [2015]}, "smc_months",
                    f"H7c with past SMC, smc_months {mo} fails closed")
        # Codex r1 P2-5: year keys of mixed type are accepted by the run as by the schedule
        um = UnitSpec("t", dict(attrs, hist_net_by_year={2014.3: "pbo", "2015.2": "dual"}))
        o = e.run({"n_agents": 200, "burnin_years": 0}, um, none, hs, 1)
        ok(o.per_capita is not None, "H7d hist_net_by_year with float and string year keys runs (burn-in key)")
    case("H7", h7)


def _self_test_v011(ok, e, unit) -> None:
    """v0.11: the SMC mechanism from the literature (ENGINE_NOTES.md, v0.11). Reach from a persistent beta propensity
    (mean smc_cov, intra-child correlation smc_rho; Malaria Consortium 2020 Table 21), a full course with probability
    smc_adh, clearance at dosing, and Weibull-decaying protection against new infection E0 exp(-(t/L)^k ln2)
    (Burgert 2022 Table 1), E0 x smc_partial after the day-1 dose only (Cairns 2022)."""
    def case(label, fn):
        try:
            fn()
        except Exception as ex:  # noqa: BLE001 -- a missing feature is a failing case, not a crash of the test
            ok(False, f"{label} ({type(ex).__name__}: {ex})")

    from math import comb
    from scipy.special import betaln
    want_p = {"smc_cov": (0.796, "Table 21"), "smc_rho": (0.417, "Table 21"), "smc_adh": (0.944, "Table 16"),
              "smc_e0": (1.0, "Burgert"), "smc_half": (32.0, "Burgert"), "smc_k": (5.4, "Burgert"),
              "smc_partial": (0.910, "Cairns")}

    def q0():
        specs = {p.name: p for p in e.parameters()}
        good = [k for k, (v, src) in want_p.items() if k in specs and abs(specs[k].value - v) < 1e-9
                and specs[k].kind == "fixed" and src in specs[k].source]
        ok(tuple(int(x) for x in VERSION.split(".")) >= (0, 11) and sorted(good) == sorted(want_p)
           and "smc_pe" not in specs and "smc_hi_frac" not in specs,
           f"Q0 engine v0.11: the seven SMC parameters are fixed and sourced; smc_pe and smc_hi_frac are gone "
           f"(sourced: {sorted(good)})")
        from toolkit.spec.priors import load_card
        mc = load_card(e)
        ok(mc["problems"] == [] and {"smc_cov", "smc_adh", "smc_half"} <= set(mc["priors"])
           and "smc_pe" not in mc["priors"], f"Q0b priors.yaml covers the new parameters: {mc['problems'][:3]}")
    case("Q0", q0)

    def q1():
        t = np.array([-1.0, 0.0, 14.0, 21.0, 28.0, 32.0, 38.0, 45.0, 90.0])
        E = M.smc_protection(t, 1.0, 32.0, 5.4)
        ref = np.exp(-(np.maximum(t, 0) / 32.0) ** 5.4 * np.log(2))
        ok(E[0] == 0.0 and abs(E[1] - 1.0) < 1e-12 and abs(E[5] - 0.5) < 1e-12 and np.allclose(E[1:], ref[1:])
           and bool(np.all(np.diff(E[1:]) <= 0)) and abs(E[3] - 0.931) < 0.002 and abs(E[4] - 0.714) < 0.002
           and E[7] < 0.02, f"Q1 protection E0 exp(-(t/L)^k ln2): 0 before a dose, E0 at the dose, E0/2 at L, "
                            f"decreasing: {E.round(3).tolist()}")
        E2 = M.smc_protection(np.array([0.0, 32.0]), 0.91, 32.0, 5.4)
        ok(np.allclose(E2, [0.91, 0.455]), f"Q1b E0 scales the curve ({E2.tolist()})")
    case("Q1", q1)

    def q2():
        N = 200000
        u = (np.arange(N) + 0.5) / N
        pi = M.smc_propensity(u, 0.796, 0.417)
        rng = np.random.default_rng(1)
        n = (rng.random((4, N)) < pi).sum(axis=0)
        got = np.bincount(n, minlength=5) / N
        s = 1 / 0.417 - 1
        a, b = 0.796 * s, 0.204 * s
        bb = np.array([comb(4, k) * np.exp(betaln(k + a, 4 - k + b) - betaln(a, b)) for k in range(5)])
        ok(abs(pi.mean() - 0.796) < 0.002 and np.allclose(got, bb, atol=0.006)
           and np.allclose(bb, [0.055, 0.074, 0.103, 0.166, 0.601], atol=0.002),
           f"Q2 rounds reached over 4 rounds are beta-binomial (Table 21 fit): {got.round(3).tolist()} vs "
           f"{bb.round(3).tolist()} (observed 0.060 0.052 0.133 0.153 0.602)")
        p0 = M.smc_propensity(u, 0.796, 0.0)
        ok(np.allclose(p0, 0.796), "Q2b smc_rho = 0: every child has the mean propensity (independent rounds)")
        ok(np.all(np.diff(pi) >= 0), "Q2c the propensity is monotone in the child's latent (persistent ranking)")
    case("Q2", q2)

    hz = Horizon(2016.0, 2017.0, (2016.0, 2017.0), {"in": [2016 + 5 / 12, 2016 + 9 / 12],
                                                   "post": [2016 + 8 / 12 + 29 / 365, 2016 + 8 / 12 + 42 / 365]})
    # Codex r1 P2-2: the last round is at the start of September (2016 + 8/12), so "post" is 29-42 days after it
    usmc = UnitSpec("t", dict(unit.attributes, m0=20.0, itn_hist_years=[], campaign_year=2016.5, irs_month=None,
                              smc_months=[6, 7, 8, 9]))

    def q3():
        sim, _ = e.build_sim({"n_agents": 4000, "burnin_years": 0}, usmc, Package("s", {"smc": 1}), hz, 7)
        sim.init()
        yv = np.asarray(sim.t.yearvec)
        s1 = sorted(M.event_steps(yv, sim.interventions["smc"].targets()))[0]
        sim.run(until=yv[s1 - 1])
        dz = sim.diseases.malaria
        uu = sim.people.auids
        a = np.asarray(sim.people.age[uu])
        el = uu[(a >= 0.25) & (a < 5)]
        inf_before = np.asarray(dz.st[el]) != M.S_
        sim.run(until=yv[s1])
        got = np.asarray(dz.smc_t[el]) == s1
        eff = np.asarray(dz.smc_eff[el])[got]
        full = float(np.mean(np.abs(eff - 1.0) < 1e-12))
        part = bool(np.all((np.abs(eff - 1.0) < 1e-12) | (np.abs(eff - 0.910) < 1e-12)))
        ok(abs(got.mean() - 0.796) < 0.04, f"Q3 a round reaches the share smc_cov of children 3-59 m ({got.mean():.3f})")
        ok(abs(full - 0.944) < 0.03 and part, f"Q3b reached children: full course share {full:.3f} (smc_adh 0.944) "
                                             "with E0, the others E0 x smc_partial")
        st_after = np.asarray(dz.st[el])
        ok(float(np.mean(st_after[got] == M.S_)) > 0.99 and (inf_before & got).sum() > 20 and (st_after[~got] != M.S_).any(),
           "Q3c clearance at dosing: every reached child is uninfected after the round; children not reached keep "
           "their infections")
        older = uu[a >= 5]
        ok(not np.any(np.asarray(dz.smc_t[older]) == s1), "Q3d children 5 and over are never dosed")
        # Codex r1 P2-3: reach persists across the intervention's own rounds (the beta propensity, not the test's draws)
        s2 = sorted(M.event_steps(yv, sim.interventions["smc"].targets()))[1]
        sim.run(until=yv[s2])
        both = np.isin(el, np.asarray(sim.people.auids))
        el2 = el[both]
        a2 = np.asarray(sim.people.age[el2])
        el2 = el2[a2 < 5]
        r1 = np.isin(el2, el[got])
        r2 = np.asarray(dz.smc_t[el2]) == s2
        phi = float(np.corrcoef(r1, r2)[0, 1])
        ok(0.2 < phi < 0.7, f"Q3e reach in round 2 is correlated with reach in round 1 (phi {phi:.3f}; independent "
                            "rounds give 0)")
    case("Q3", q3)

    def q4():
        lr_in, lr_post = [], []
        for sd in (3, 4):
            o0, o1 = e.run_batch([{"n_agents": 3000, "burnin_years": 2}] * 2, usmc,
                                 [Package("none", {}), Package("s", {"smc": 1})], hz, sd)
            lr_in.append(o1.windows["in"]["inc_u5"] / max(o0.windows["in"]["inc_u5"], 1e-9))
            lr_post.append(o1.windows["post"]["inc_u5"] / max(o0.windows["post"]["inc_u5"], 1e-9))
        pe_in, pe_post = 1 - float(np.mean(lr_in)), 1 - float(np.mean(lr_post))
        ok(pe_in > 0.7 and pe_post < pe_in - 0.1,
           f"Q4 population-level diagnostic, all children under 5 (reached or not): incidence reduction "
           f"{pe_in:.3f} over the rounds, {pe_post:.3f} at 29-42 days after the last round. Not the recipients' "
           f"effectiveness that ACCESS-SMC estimates (0.882 within 28 days, 0.614 at 29-42 days); a check, not an "
           f"input")
    case("Q4", q4)

    def q5():
        bad = []
        for k, v in (("smc_cov", 0.0), ("smc_cov", 1.2), ("smc_rho", 1.0), ("smc_rho", -0.1), ("smc_adh", 1.5),
                     ("smc_e0", -0.1), ("smc_partial", 1.1), ("smc_half", 0.0), ("smc_k", float("nan")),
                     ("smc_half", float("inf"))):
            try:
                e.run({"n_agents": 200, "burnin_years": 0, k: v}, usmc, Package("s", {"smc": 1}), hz, 1)
                bad.append((k, v))
            except ValueError as ex:
                if k not in str(ex):
                    bad.append((k, v, str(ex)[:60]))
        ok(not bad, f"Q5 out-of-range SMC parameters fail closed, naming the parameter (accepted: {bad})")
        # a member forked from another member's burn-in (SMC parameters are post-fork without past SMC) is checked too
        try:
            e.run_batch([{"n_agents": 200, "burnin_years": 0}, {"n_agents": 200, "burnin_years": 0, "smc_cov": 1.5}],
                        usmc, Package("s", {"smc": 1}), hz, 1)
            ok(False, "Q5b a forked member's out-of-range SMC parameter fails closed")
        except ValueError as ex:
            ok("smc_cov" in str(ex), f"Q5b a forked member's out-of-range SMC parameter fails closed ({ex})")
    case("Q5", q5)

    def q6():
        from toolkit.engines.horizon import DeliveryHorizon
        season = [0.4, 0.4, 0.5, 0.7, 1.0, 1.4, 1.8, 2.0, 1.6, 1.0, 0.7, 0.5]
        attrs = dict(unit.attributes, season=season, itn_hist_years=[2015.2], campaign_year=2016.1, irs_month=4)
        hs = DeliveryHorizon(2015.0, 2017.0, (2016.0, 2017.0), {"y15": [2015.0, 2016.0]}, delivery=(2016.0, 2017.0))
        small = {"n_agents": 300, "burnin_years": 1}
        for k, vals in (("smc_cov", (0.6, 0.9)), ("smc_half", (24.0, 32.0))):
            ps = [dict(small, **{k: vals[0]}), dict(small, **{k: vals[1]})]
            b = e.run_batch(ps, UnitSpec("t", dict(attrs, smc_hist_years=[2015])), Package("none", {}), hs, 3)
            b0 = e.run_batch(ps, UnitSpec("t", attrs), Package("none", {}), hs, 3)
            ok([o.meta["burnin_members"] for o in b] == [1, 1] and [o.meta["burnin_members"] for o in b0] == [2, 2],
               f"Q6 ({k}) with past SMC the SMC parameters are in the burn-in key; without it they act after the fork")
        # Codex r1 P2-3: members that share one burn-in each act with their own SMC parameters (dz.P and the
        # interventions are set per member), including the decay read from dz.P at each step
        hq = DeliveryHorizon(2015.0, 2017.0, (2016.0, 2017.0), {"d": [2016.0, 2017.0]}, delivery=(2016.0, 2017.0))
        uq = UnitSpec("t", dict(attrs, m0=20.0))
        ps = [dict(small), dict(small, smc_half=24.0), dict(small, smc_k=2.0), dict(small, smc_cov=0.5, smc_rho=0.0)]
        b = e.run_batch(ps, uq, Package("s", {"smc": 1}), hq, 4)
        solo = [e.run(q, uq, Package("s", {"smc": 1}), hq, 4) for q in ps]
        strip = lambda o: (o.per_capita, o.windows, o.cost_drivers)  # noqa: E731
        ok([o.meta["burnin_members"] for o in b] == [4] * 4 and [strip(o) for o in b] == [strip(o) for o in solo]
           and len({repr(o.windows["d"]) for o in b}) == 4,
           "Q6b members sharing one burn-in each equal their own run and differ from one another (smc_half, smc_k, "
           "smc_cov / smc_rho act per member)")
    case("Q6", q6)


def _self_test_v012(ok, e, unit) -> None:
    """v0.12 (F6 S2-2): case management as an implementable package (ENGINE_NOTES.md, v0.12). With cm_target = 1 the
    add-on raises effective treatment only in the declared ages (default 2-59 months), ramps in linearly over
    cm_ramp_years from the delivery start, raises severe-care access only through cm_severe_link (default 0), and the
    engine reports the children covered and the treated episodes in those ages. cm_target = 0 is the v0.11 add-on."""
    def case(label, fn):
        try:
            fn()
        except Exception as ex:  # noqa: BLE001 -- a missing feature is a failing case, not a crash of the test
            ok(False, f"{label} ({type(ex).__name__}: {ex})")

    new = ("cm_target", "cm_age_lo_m", "cm_age_hi_m", "cm_ramp_years", "cm_severe_link", "cm_catchment_share")
    v11 = {"nets_given", "smc_doses", "irs_sprayed", "treated_all"}
    drv = {"cm_child_years", "treated_cm_ages"}

    def c0():
        sp = {p.name: p for p in e.parameters()}
        want = {"cm_target": 0.0, "cm_age_lo_m": 2.0, "cm_age_hi_m": 60.0, "cm_ramp_years": 2.5, "cm_severe_link": 0.0,
                "cm_catchment_share": 0.3}
        got = {k: sp[k].value for k in want if k in sp and sp[k].kind == "fixed"}
        ok(tuple(int(x) for x in VERSION.split(".")) >= (0, 12) and got == want and set(new) <= POST_FORK,
           f"C0 engine v0.12: the case-management options are fixed card parameters acting after the fork ({got})")
        ok("no published" in sp["cm_severe_link"].source and "Oresanya" in sp["cm_add"].source
           and sp["cm_add"].kind == "scenario" and (sp["cm_add"].lo, sp["cm_add"].hi) == (0.0, 0.40)
           and "Collins" in sp["cm_catchment_share"].source,
           "C0b cm_add stays a scenario parameter with a sourced range; cm_severe_link says no estimate was found")
        from toolkit.spec.priors import load_card
        mc = load_card(e)
        outs = e.describe().outputs
        ok(mc["problems"] == [] and set(new) <= set(mc["fixed"]) and not drv & set(outs["cost_drivers"])
           and outs.get("cost_drivers_when") == {d: {"cm_target": 1.0} for d in sorted(drv)},
           f"C0c priors.yaml gives each new parameter a reason; the card lists the new cost drivers as produced only "
           f"with cm_target 1 (cost_drivers_when; the contract test checks cost_drivers in every run) {mc['problems'][:2]}")
    case("C0", c0)

    def c1():
        r = [M.cm_ramp(y, 2020.0, 2.0) for y in (2019.0, 2020.0, 2021.0, 2022.0, 2023.0)]
        ok(r == [0.0, 0.0, 0.5, 1.0, 1.0] and M.cm_ramp(2020.0, 2020.0, 0.0) == 1.0,
           f"C1 the ramp is linear from the delivery start over cm_ramp_years, 1 after it, a step at 0 years ({r})")
    case("C1", c1)

    hz = Horizon(2016.0, 2018.0, (2017.0, 2018.0), {"w": [2017.0, 2018.0]})
    uu = UnitSpec("t", dict(unit.attributes, m0=20.0, itn_hist_years=[], campaign_year=2017.0, cm_ft=0.3))
    base = {"n_agents": 3000, "burnin_years": 1, "cm_add": 0.3}
    pk = [Package("none", {}), Package("cm", {"cm": 1})]

    def ratio(o, g, num="treated", den="cases"):
        return o.per_capita[num][g] / max(o.per_capita[den][g], 1e-12)

    def cfr(P, pt):
        return (1 - pt) * P["cfr_severe_untreated"] + pt * P["cfr_severe_treated"]

    def c2():
        P = resolve_params(e, base)
        o0, o1 = e.run_batch([base] * 2, uu, pk, hz, 5)
        ok(set(o1.cost_drivers) == v11 and ratio(o1, "15p") > ratio(o0, "15p") + 0.2,
           f"C2 cm_target absent: the v0.11 add-on (all ages: treated share 15+ {ratio(o0, '15p'):.3f} -> "
           f"{ratio(o1, '15p'):.3f}; cost drivers {sorted(o1.cost_drivers)})")
        want = cfr(P, min(1.0, P["severe_access_add"] + 0.6))
        ok(all(abs(ratio(o1, g, "deaths", "severe") - want) < 1e-6 for g in AGES),
           "C2b cm_target absent: severe-care access rises one for one with the add-on (v0.11)")
    case("C2", c2)

    tp = dict(base, cm_target=1, cm_ramp_years=0, cm_catchment_share=1.0)

    def c3():
        P = resolve_params(e, tp)
        o0, o1 = e.run_batch([tp] * 2, uu, pk, hz, 5)
        gu5 = ratio(o1, "u5") - ratio(o0, "u5")
        ok(0.2 < gu5 < 0.32 and all(abs(ratio(o1, g) - ratio(o0, g)) < 0.06 for g in ("5_14", "15p")),
           f"C3 cm_target 1: the add-on raises effective treatment under 5 only (gain u5 {gu5:.3f}; 5-14 "
           f"{ratio(o0, '5_14'):.3f} -> {ratio(o1, '5_14'):.3f}; 15+ {ratio(o0, '15p'):.3f} -> {ratio(o1, '15p'):.3f})")
        want = cfr(P, min(1.0, P["severe_access_add"] + 0.3))
        ok(all(abs(ratio(o, g, "deaths", "severe") - want) < 1e-6 for o in (o0, o1) for g in AGES),
           "C3b cm_severe_link 0 (default): severe-care access does not move with the add-on")
        q = dict(tp, cm_severe_link=1.0)
        l0, l1 = e.run_batch([q] * 2, uu, pk, hz, 5)
        ok(ratio(l1, "u5", "deaths", "severe") < want - 0.02 and all(
            abs(ratio(l1, g, "deaths", "severe") - want) < 1e-6 for g in ("5_14", "15p")),
           f"C3c cm_severe_link 1: severe-care access rises only in the targeted ages "
           f"(u5 CFR {ratio(l1, 'u5', 'deaths', 'severe'):.4f} vs {want:.4f})")
        q2 = dict(tp, cm_age_hi_m=180.0)
        _, w1 = e.run_batch([q2] * 2, uu, pk, hz, 5)
        ok(ratio(w1, "5_14") > ratio(o0, "5_14") + 0.15, "C3d the targeted ages come from cm_age_lo_m / cm_age_hi_m")
    case("C3", c3)

    def c4():
        o0, full = e.run_batch([tp] * 2, uu, pk, hz, 6)
        ramp = e.run_batch([dict(tp, cm_ramp_years=1.0)], uu, pk[1], hz, 6)[0]
        f = (ratio(ramp, "u5") - ratio(o0, "u5")) / max(ratio(full, "u5") - ratio(o0, "u5"), 1e-9)
        ok(0.3 < f < 0.7, f"C4 a ramp over the whole window gives about half the gain ({f:.3f})")
    case("C4", c4)

    def c5():
        o0, o1 = e.run_batch([tp] * 2, uu, pk, hz, 5)
        z = e.run_batch([dict(tp, cm_add=0.0)], uu, pk[1], hz, 5)[0]
        sh = o1.meta["age_shares"]["u5"]
        ok(drv <= set(o0.cost_drivers) and o0.cost_drivers["cm_child_years"] == 0.0
           and 0.85 * sh < o1.cost_drivers["cm_child_years"] <= sh + 1e-9
           and o1.cost_drivers["treated_cm_ages"] > o0.cost_drivers["treated_cm_ages"] > 0,
           f"C5 cost drivers: children 2-59 months covered per person-year {o1.cost_drivers['cm_child_years']:.4f} "
           f"(u5 share {sh:.4f}; 0 without the add-on) and treated episodes in the targeted ages "
           f"({o0.cost_drivers.get('treated_cm_ages')} -> {o1.cost_drivers.get('treated_cm_ages')})")
        ok(z.cost_drivers["cm_child_years"] == o1.cost_drivers["cm_child_years"] and z.per_capita == o0.per_capita
           and z.windows == o0.windows and {k: z.cost_drivers[k] for k in v11 | {"treated_cm_ages"}}
           == {k: o0.cost_drivers[k] for k in v11 | {"treated_cm_ages"}},
           "C5b an add-on with cm_add 0 is still charged for the children it covers and changes no health outcome, "
           "window metric or other cost driver")
        # Codex r1 P2-4: the catchment share scales the effect and the children charged alike
        h = e.run_batch([dict(tp, cm_catchment_share=0.4)], uu, pk[1], hz, 5)[0]
        ok(abs(h.cost_drivers["cm_child_years"] - 0.4 * o1.cost_drivers["cm_child_years"]) < 1e-12
           and ratio(o0, "u5") < ratio(h, "u5") < ratio(o1, "u5") - 0.1,
           f"C5c cm_catchment_share 0.4: child-years charged are 0.4 x the targeted ages' "
           f"({h.cost_drivers['cm_child_years']:.4f} vs {o1.cost_drivers['cm_child_years']:.4f}) and the gain is "
           f"diluted (treated share u5 {ratio(o0, 'u5'):.3f} / {ratio(h, 'u5'):.3f} / {ratio(o1, 'u5'):.3f})")
    case("C5", c5)

    def c6():
        a = e.run_batch([base, base], uu, pk, hz, 5)
        b = e.run_batch([dict(base, cm_target=1)] * 2, uu, pk, hz, 5)
        ok(a[0].per_capita == b[0].per_capita and a[0].windows == b[0].windows
           and {k: b[0].cost_drivers[k] for k in v11} == a[0].cost_drivers,
           "C6 without the add-on, cm_target 1 gives the v0.11 outputs exactly (plus the two new cost drivers)")
        mix = [base, tp, dict(tp, cm_severe_link=0.5)]
        bm = e.run_batch(mix, uu, pk[1], hz, 5)
        solo = [e.run(q, uu, pk[1], hz, 5) for q in mix]
        strip = lambda o: (o.per_capita, o.windows, o.cost_drivers)  # noqa: E731
        ok([o.meta["burnin_members"] for o in bm] == [3] * 3 and [strip(o) for o in bm] == [strip(o) for o in solo],
           "C6b members sharing one burn-in each act with their own case-management options")
    case("C6", c6)

    def c7():
        bad = []
        for over in ({"cm_target": 0.5}, {"cm_age_lo_m": 60.0}, {"cm_age_hi_m": float("nan")}, {"cm_ramp_years": -1.0},
                     {"cm_severe_link": 1.5}, {"cm_add": 1.2}, {"cm_add": -0.1}, {"cm_catchment_share": 0.0},
                     {"cm_catchment_share": 1.5}):
            k = next(iter(over))
            q = dict(tp, n_agents=200, **over) if k != "cm_target" else dict(base, n_agents=200, **over)
            try:
                e.run(q, uu, pk[1], hz, 1)
                bad.append(over)
            except ValueError as ex:
                if k not in str(ex):
                    bad.append((over, str(ex)[:60]))
        ok(not bad, f"C7 out-of-domain case-management options fail closed, naming the parameter (accepted: {bad})")
    case("C7", c7)

    # Codex r1 P3: deterministic, hand-counted boundary cases of the pure functions Malaria.step and
    # CaseManagement.step use
    def c8():
        # Codex r2 P2-1: signatures (age, ft, within-catchment gain, catchment share, ages); share 1 here
        a = np.array([0.0, 60.8, 60.84, 1000.0, 1824.9, 1825.0, 4000.0])       # 2 months = 60.833 d, 60 = 1825 d
        pft, in_t = M.cm_treat_prob(a, 0.3, 0.2, 1.0, 2.0, 60.0)
        ok(in_t.tolist() == [False, False, True, True, True, False, False]
           and np.allclose(pft, [0.3, 0.3, 0.5, 0.5, 0.5, 0.3, 0.3], atol=1e-15),
           f"C8 the gain starts at exactly 2 months and stops at exactly 60 months ({in_t.tolist()})")
        days = np.arange(0.0, 2000.0)                                          # one child ageing day by day
        ok(int(M.cm_treat_prob(days, 0.3, 0.2, 1.0, 2.0, 60.0)[1].sum()) == 1825 - 61,
           "C8b a child ageing from birth gets the gain on days 61-1824 of life only (1,764 days, hand count)")
        ok(float(M.cm_treat_prob(np.array([100.0]), 0.9, 0.2, 1.0, 2.0, 60.0)[0][0]) == 0.95,
           "C8c effective treatment is capped at 0.95")
        sv = [float(M.cm_severe_access(0.25, 0.3, 1.0, g, 1.0)) for g in (0.0, 0.2)]
        sat = [float(M.cm_severe_access(0.25, 0.7, k, 0.2, 1.0)) for k in (1.0, 0.5, 0.2)]
        ok(np.allclose(sv, [0.55, 0.75], atol=1e-15) and sat[:2] == [1.0, 1.0] and abs(sat[2] - 0.99) < 1e-12,
           f"C8d severe-care access = access + cm_ft + link x gain, saturating at 1 ({sv}, {sat})")
        g = [M.cm_gain_at(y, 0.3, 2020.0, 2021.0, 2.5, True) for y in (2019.9, 2020.0, 2020.5, 2020.999, 2021.0)]
        ok(np.allclose(g, [0.0, 0.0, 0.06, 0.3 * 0.999 / 2.5, 0.0], atol=1e-12)
           and M.cm_gain_at(2020.5, 0.3, 2020.0, 2021.0, 2.5, False) == 0.0
           and abs(M.cm_gain_at(2023.0, 0.3, 2020.0, None, 2.5, True) - 0.3) < 1e-15,
           f"C8e delivery stopping before the ramp completes ends the gain at the stop; no package, no gain ({g})")
        # Codex r2 P2-1: partial coverage caps inside the catchment first, then weights by the share
        t = float(M.cm_treat_prob(np.array([100.0]), 0.8, 0.4, 0.3, 2.0, 60.0)[0][0])
        sc = float(M.cm_severe_access(0.25, 0.7, 1.0, 0.2, 0.3))
        ok(abs(t - 0.845) < 1e-12 and abs(sc - (0.7 * 0.95 + 0.3 * 1.0)) < 1e-12,
           f"C8f partial coverage: treatment 0.7 x 0.8 + 0.3 x min(0.95, 0.8 + 0.4) = 0.845 ({t:.4f}); severe-care "
           f"access 0.7 x 0.95 + 0.3 x min(1, 1.15) = 0.965 ({sc:.4f}); capping after averaging gives 0.92 and 1")
        ok(float(M.cm_treat_prob(np.array([100.0]), 0.3, 0.0, 0.3, 2.0, 60.0)[0][0]) == 0.3,
           "C8g a zero gain leaves treatment at cm_ft exactly")
    case("C8", c8)

    def c9():
        from toolkit.engines.horizon import DeliveryHorizon
        q = dict(tp, n_agents=1500, cm_catchment_share=0.5)
        short = DeliveryHorizon(2016.0, 2018.0, (2017.0, 2018.0), {"d": [2017.0, 2018.0]}, delivery=(2017.0, 2018.0))
        long_ = DeliveryHorizon(2016.0, 2019.0, (2017.0, 2019.0), {"d": [2017.0, 2018.0], "after": [2018.0, 2019.0]},
                                delivery=(2017.0, 2018.0))
        s0, s1 = e.run_batch([q] * 2, uu, pk, short, 8)
        l0, l1 = e.run_batch([q] * 2, uu, pk, long_, 8)
        ok(s1.cost_drivers == l1.cost_drivers and s0.cost_drivers == l0.cost_drivers and s1.windows["d"] == l1.windows["d"]
           and l1.cost_drivers["cm_child_years"] > 0,
           "C9 a longer outcome window: the cost drivers count only the delivery period, and the delivery year's "
           "outcomes are unchanged")
    case("C9", c9)

    def c10():
        from toolkit.alloc import costs as C
        spec = {"units": {"id": "id", "populations": {"total": "pop"}}, "components": {"cm": {}},
                "packages": {"none": [], "cm": ["cm"]},
                "prices": {"base_year": 2024, "index": {"2012": 229.6, "2024": 313.7}},
                "cost_items": [dict(it) for it in CM_COST_ITEMS]}
        extra = {"id": "cm_act_rdt", "component": "cm", "value": 0.9, "price_year": 2024, "price_rule": "index",
                 "basis": "per_outcome", "outcome": "treated_cm", "incremental": True,
                 "includes": ["medicines", "supplies"]}
        bad = C.validate(dict(spec, cost_items=spec["cost_items"] + [extra]))
        ok(C.validate(spec) == [] and any("double count" in b and "cm_act_rdt" in b for b in bad)
           and all(it["outcome"] == "cm_child_years" for it in CM_COST_ITEMS if it["basis"] == "per_outcome"),
           f"C10 the documented CM cost recipe (Collins 2014 recurrent total, medicines and supplies included) "
           f"validates, and a separate ACT/RDT charge on the same component is refused as a double count ({bad[:1]})")
    case("C10", c10)

    def c11():
        from toolkit.engines.conditional_drivers import check

        class Small(MalariaEngine):
            def run(self, params, unit, package, horizon, seed):
                return super().run(dict({"n_agents": 400, "burnin_years": 0}, **params), unit, package, horizon, seed)
        probs = check(Small(), uu, pk[1], Horizon(2016.0, 2017.0, (2016.5, 2017.0), {"w": [2016.5, 2017.0]}), 2)
        ok(probs == [], f"C11 the conditional cost drivers pass the conditional-driver contract test {probs}")
    case("C11", c11)


def _self_test_v013(ok, e, unit) -> None:
    """v0.13 (F6 S2-3 part 2, critique C3): past SMC coverage apart from future coverage. `smc_hist_cov` (one share, or
    one per smc_hist_years entry) is the mean per-round reach of the past SMC rounds; the package's and routine SMC keep
    smc_cov. A calibration can then fit the history without moving the coverage the allocation uses. Absent or empty,
    a run is byte-identical to v0.12 (HC3; benchmarks/restamp_proof.py)."""
    def case(label, fn):
        try:
            fn()
        except Exception as ex:  # noqa: BLE001 -- a missing feature is a failing case, not a crash of the test
            ok(False, f"{label} ({type(ex).__name__}: {ex})")

    from toolkit.engines.horizon import DeliveryHorizon
    from toolkit.engines.malaria import model as MM
    season = [0.4, 0.4, 0.5, 0.7, 1.0, 1.4, 1.8, 2.0, 1.6, 1.0, 0.7, 0.5]
    months = default_smc_months(np.asarray(season))          # (6, 7, 8, 9) for this season
    attrs = dict(unit.attributes, season=season, itn_hist_years=[2014.3], campaign_year=2016.1)
    hs = DeliveryHorizon(2014.0, 2017.0, (2016.0, 2017.0), {"d": [2016.0, 2017.0]}, delivery=(2016.0, 2017.0))
    h1 = DeliveryHorizon(2015.0, 2017.0, (2016.0, 2017.0), {"d": [2016.0, 2017.0]}, delivery=(2016.0, 2017.0))
    small = {"n_agents": 300, "burnin_years": 1}
    none, smc = Package("none", {}), Package("smc", {"smc": 1})
    strip = lambda o: (o.per_capita, o.windows, o.cost_drivers,  # noqa: E731
                       {k: v for k, v in o.meta.items() if k != "seconds"})
    cov0 = {p.name: p.value for p in e.parameters()}["smc_cov"]

    def hc0():
        ua = e.describe().unit_attributes
        ok(tuple(int(x) for x in VERSION.split(".")) >= (0, 13) and "smc_hist_cov" in ua,
           "HC0 engine v0.13 declares smc_hist_cov on its card")
        from toolkit.spec.schema import load_binding
        b, probs = load_binding(e)
        m = b.get("smc_hist_cov")
        ok(not probs and isinstance(m, dict) and "default" in m and "smc_cov" in str(m.get("meaning") or ""),
           f"HC0b attribute_binding.yaml gives smc_hist_cov a default and its meaning (past rounds at smc_cov) {probs}")
    case("HC0", hc0)

    def hc1():
        a = dict(attrs, smc_hist_years=[2015, 2014], smc_hist_cov=[0.3, 0.9])
        sh = smc_history(a, 2016.0, months)
        want = [0.9] * 4 + [0.3] * 4                          # rounds are in year order; each year keeps its share
        ok(sh is not None and sh.get("covs") == want and len(sh["rounds"]) == 8,
           f"HC1 one share per past year follows its year: {sh and sh.get('covs')}")
        sh = smc_history(dict(attrs, smc_hist_years=[2014, 2015], smc_hist_rounds=[2, 3], smc_hist_cov=0.5),
                         2016.0, months)
        ok(sh is not None and sh.get("covs") == [0.5] * 5, f"HC1b one share applies to every past round: {sh}")
        for v in (None, []):
            sh = smc_history(dict(attrs, smc_hist_years=[2015], smc_hist_cov=v), 2016.0, months)
            ok(sh is not None and sh.get("covs") is None, f"HC1c smc_hist_cov {v!r}: past rounds at smc_cov (v0.12)")
        u0 = UnitSpec("t", dict(attrs, smc_hist_years=[2014, 2015]))
        uc = UnitSpec("t", dict(attrs, smc_hist_years=[2014, 2015], smc_hist_cov=[0.3, 0.9]))
        ok(e.schedule(small, u0, smc, hs) == e.schedule(small, uc, smc, hs),
           "HC1d the past coverage changes no delivery date (the schedule is v0.12's)")
    case("HC1", hc1)

    def hc2():
        for at, label in (({"smc_hist_cov": 0}, "0"), ({"smc_hist_cov": 1.2}, "above 1"),
                          ({"smc_hist_cov": -0.1}, "negative"), ({"smc_hist_cov": float("nan")}, "NaN"),
                          ({"smc_hist_cov": True}, "a boolean"), ({"smc_hist_cov": "0.5"}, "a string"),
                          ({"smc_hist_cov": [0.5]}, "a list of another length"),
                          ({"smc_hist_cov": [0.5, None]}, "a list with a missing value"),
                          ({"smc_hist_cov": {2014: 0.5, 2015: 0.6}}, "a mapping"),
                          ({"smc_hist_years": [], "smc_hist_cov": [0.5]}, "a list without past years"),
                          ({"smc_hist_years": [], "smc_hist_cov": 1.5}, "out of range without past years")):
            u = UnitSpec("t", dict(attrs, **dict({"smc_hist_years": [2014, 2015]}, **at)))
            try:
                e.schedule(small, u, none, hs)
                ok(False, f"HC2 smc_hist_cov {label} fails closed")
            except ValueError as ex:
                ok("smc_hist_cov" in str(ex), f"HC2 smc_hist_cov {label} fails closed ({ex})")
        try:
            e.run(small, UnitSpec("t", dict(attrs, smc_hist_years=[2015], smc_hist_cov=0)), none, h1, 1)
            ok(False, "HC2b the sim path refuses it too")
        except ValueError as ex:
            ok("smc_hist_cov" in str(ex), f"HC2b the sim path refuses it too ({ex})")
    case("HC2", hc2)

    def hc3():
        pk = [none, smc]
        uh = dict(attrs, smc_hist_years=[2015])
        ref = [strip(o) for o in e.run_batch([small] * 2, UnitSpec("t", uh), pk, h1, 5)]
        s0, _ = e.build_sim(small, UnitSpec("t", uh), smc, h1, 5)
        s0.init()
        names = list(s0.interventions.keys())
        for extra, nm in (({"smc_hist_cov": None}, "None"), ({"smc_hist_cov": []}, "[]"),
                          ({"smc_hist_cov": cov0}, "equal to smc_cov"), ({"smc_hist_cov": [cov0]}, "[smc_cov]")):
            ux = UnitSpec("t", dict(uh, **extra))
            got = [strip(o) for o in e.run_batch([small] * 2, ux, pk, h1, 5)]
            ok(got == ref, f"HC3 (smc_hist_cov {nm}) byte-identical outcomes, cost drivers and meta to v0.12's past SMC")
            sx, _ = e.build_sim(small, ux, smc, h1, 5)
            sx.init()
            ok(list(sx.interventions.keys()) == names, f"HC3b ({nm}) the same interventions as v0.12")
        r0 = [strip(o) for o in e.run_batch([small] * 2, UnitSpec("t", attrs), pk, h1, 5)]
        r1 = [strip(o) for o in e.run_batch([small] * 2, UnitSpec("t", dict(attrs, smc_hist_cov=0.4)), pk, h1, 5)]
        ok(r1 == r0, "HC3c smc_hist_cov without past SMC years changes nothing")
    case("HC3", hc3)

    def hc4():
        # changing the future coverage (smc_cov) leaves the history years exactly as they were
        p = {"n_agents": 800, "burnin_years": 1}
        u = UnitSpec("t", dict(attrs, smc_hist_years=[2014, 2015], smc_hist_cov=0.6))
        ps = [dict(p, smc_cov=0.5), dict(p, smc_cov=0.95)]
        out, ser = e._run_members(ps, [u, u], [smc, smc], hs, 5, series=["pfpr_u5", "inc_u5"])
        t = np.asarray(ser[0]["t"])
        pre, post = t < 2016.0 - 1e-9, t >= 2016.0 - 1e-9
        same = all(np.array_equal(np.asarray(ser[0]["values"][k])[pre], np.asarray(ser[1]["values"][k])[pre])
                   for k in ("pfpr_u5", "inc_u5"))
        moved = not np.array_equal(np.asarray(ser[0]["values"]["inc_u5"])[post],
                                   np.asarray(ser[1]["values"]["inc_u5"])[post])
        ok(same and moved and [o.meta["burnin_members"] for o in out] == [2, 2],
           "HC4 with smc_hist_cov, smc_cov 0.5 and 0.95 share one burn-in and history: the monthly series are "
           f"identical before the delivery start and differ after it (burn-in members "
           f"{[o.meta['burnin_members'] for o in out]})")
        res = lambda o: (o.per_capita, o.windows, o.cost_drivers)  # noqa: E731 -- meta names the burn-in members
        solo = [res(e.run(q, u, smc, hs, 5)) for q in ps]
        ok([res(o) for o in out] == solo, "HC4b each member of the shared burn-in equals its own run")
        u12 = UnitSpec("t", dict(attrs, smc_hist_years=[2014, 2015]))
        o12 = e.run_batch(ps, u12, smc, hs, 5)
        ok([o.meta["burnin_members"] for o in o12] == [1, 1],
           "HC4c without smc_hist_cov (v0.12) the past rounds run at smc_cov, so the two do not share a burn-in")
    case("HC4", hc4)

    def reach(hist_cov, smc_cov):
        """Per-year mean share of children 3-59 months reached per round, history and package rounds."""
        u = UnitSpec("t", dict(attrs, smc_hist_years=[2014, 2015], smc_hist_cov=hist_cov))
        sim, _ = e.build_sim({"n_agents": 3000, "burnin_years": 0, "smc_cov": smc_cov}, u, smc, hs, 7)
        sim.init()
        yv = np.asarray(sim.t.yearvec)
        itv, dz = sim.interventions, sim.diseases.malaria
        steps = sorted(MM.event_steps(yv, itv["smc_hist"].targets()) | MM.event_steps(yv, itv["smc"].targets()))
        by = {}
        for s in steps:
            sim.run(until=yv[s - 1])
            uu = sim.people.auids
            a = np.asarray(sim.people.age[uu])
            el = uu[(a >= 0.25) & (a < 5)]
            sim.run(until=yv[s])
            by.setdefault(int(np.floor(yv[s] + 1e-9)), []).append(float(np.mean(np.asarray(dz.smc_t[el]) == s)))
        return {y: float(np.mean(v)) for y, v in by.items()}

    def hc5():
        # changing the past coverage (smc_hist_cov) leaves the package's SMC at smc_cov in the delivery years
        r1, r2 = reach([0.35, 0.9], 0.7), reach(0.6, 0.7)
        ok(abs(r1.get(2014, 9) - 0.35) < 0.06 and abs(r1.get(2015, 9) - 0.9) < 0.06 and abs(r2.get(2014, 9) - 0.6) < 0.06
           and abs(r2.get(2015, 9) - 0.6) < 0.06,
           f"HC5 past rounds reach smc_hist_cov, year by year: {r1} and {r2}")
        ok(abs(r1.get(2016, 9) - 0.7) < 0.06 and abs(r2.get(2016, 9) - 0.7) < 0.06,
           f"HC5b the package's rounds reach smc_cov (0.7) whatever the past coverage: {r1.get(2016)} and {r2.get(2016)}")
    case("HC5", hc5)

    def hc6():
        P = resolve_params(e, small)
        a = dict(attrs, smc_hist_years=[2015], smc_hist_cov=0.6)
        k = lambda q, x: e._burnin_key(dict(P, **q), x)  # noqa: E731
        ok(k({"smc_cov": 0.5}, a) == k({"smc_cov": 0.9}, a) and k({"smc_rho": 0.42}, a) != k({"smc_rho": 0.5}, a)
           and k({}, a) != k({}, dict(a, smc_hist_cov=0.7)),
           "HC6 with smc_hist_cov, smc_cov leaves the burn-in key; smc_rho and the past coverage stay in it")
        a0 = dict(attrs, smc_hist_years=[2015])
        ok(k({"smc_cov": 0.5}, a0) != k({"smc_cov": 0.9}, a0), "HC6b without it smc_cov stays in the key (v0.12)")
    case("HC6", hc6)

    def hc7():
        pmf = MM.smc_rounds_pmf(0.796, 0.417, 4)
        want = [0.055, 0.074, 0.103, 0.166, 0.601]          # ENGINE_NOTES.md v0.11: the fit to Table 21
        ok(len(pmf) == 5 and abs(sum(pmf) - 1) < 1e-9 and all(abs(x - w) < 0.003 for x, w in zip(pmf, want)),
           f"HC7 smc_rounds_pmf gives the beta-binomial rounds-received shares (all four rounds {pmf[-1]:.3f})")
        from math import comb
        b = MM.smc_rounds_pmf(0.6, 0.0, 3)
        ok(all(abs(x - comb(3, i) * 0.6 ** i * 0.4 ** (3 - i)) < 1e-12 for i, x in enumerate(b)),
           "HC7b smc_rho 0 gives the binomial")
        # Codex r1 P2: outside the propensity's domain the shares fail closed (no negative or partial distribution)
        for args, label in (((1.2, 0.4, 4), "mean above 1"), ((0.0, 0.4, 4), "mean 0"), ((0.6, -0.1, 4), "rho < 0"),
                            ((0.6, 1.0, 4), "rho 1"), ((0.6, 0.4, 2.9), "fractional rounds"), ((0.6, 0.4, -1), "n < 0"),
                            ((float("nan"), 0.4, 4), "NaN mean"), ((0.6, 0.4, True), "boolean rounds")):
            try:
                MM.smc_rounds_pmf(*args)
                ok(False, f"HC7c smc_rounds_pmf: {label} fails closed")
            except ValueError:
                ok(True, f"HC7c smc_rounds_pmf: {label} fails closed")
        # Codex r1 P3: no cancellation just above the independent limit
        s = sum(MM.smc_rounds_pmf(0.6, 1e-9, 4))
        ok(abs(s - 1.0) < 1e-12 and abs(MM.smc_rounds_pmf(1.0, 0.4, 3)[-1] - 1.0) < 1e-12,
           f"HC7d the shares sum to 1 near smc_rho 0 ({s!r}) and mean 1 reaches every child in every round")
        # Codex r2 P3: no overflow near smc_rho 1 with many rounds
        from math import nextafter
        hi = MM.smc_rounds_pmf(0.6, nextafter(1.0, 0.0), 20)
        ok(all(np.isfinite(hi)) and abs(sum(hi) - 1.0) < 1e-9 and abs(hi[-1] - 0.6) < 1e-6 and abs(hi[0] - 0.4) < 1e-6,
           f"HC7e near smc_rho 1 the shares stay finite and approach all-or-none (0.4 none, 0.6 all: "
           f"{hi[0]:.6f}, {hi[-1]:.6f})")
    case("HC7", hc7)


def _self_test_v014(ok, e, unit) -> None:
    """v0.14 (F6 S2-5b): severe cases and malaria deaths as window metrics, per person of each age group and of all
    ages over any named window (severe_<g>, deaths_<g>, g in AGES or all), counted as the decision window's per-capita
    outcomes are: expected severe episodes and deaths of the window's new clinical infections over the group's mean
    population in the window. A past window then gives the deaths those years would give as the decision window."""
    def case(label, fn):
        try:
            fn()
        except Exception as ex:  # noqa: BLE001 -- a missing feature is a failing case, not a crash of the test
            ok(False, f"{label} ({type(ex).__name__}: {ex})")

    names = [f"{k}_{g}" for k in ("severe", "deaths") for g in AGES + ["all"]]
    p = {"n_agents": 1000, "burnin_years": 1}
    none = Package("none", {})
    close = lambda a, b: abs(a - b) <= 1e-12 * max(abs(a), abs(b), 1e-300)  # noqa: E731

    def w0():
        wn = e.describe().outputs["windows"]
        ok(tuple(int(x) for x in VERSION.split(".")) >= (0, 14) and all(n in wn for n in names),
           f"W0 engine v0.14 lists {names} among its window metrics")
    case("W0", w0)

    def w1():
        # A counts 2015 as its decision window; B counts 2017 and names 2015 as a past window. The sims are the same
        # (no package acts), so B's past-window metrics are A's per-capita outcomes
        ha = Horizon(2015.0, 2018.0, (2015.0, 2016.0), {"past": [2015.0, 2016.0]})
        hb = Horizon(2015.0, 2018.0, (2017.0, 2018.0), {"past": [2015.0, 2016.0], "dec": [2017.0, 2018.0]})
        a, b = e.run(p, unit, none, ha, 4), e.run(p, unit, none, hb, 4)
        wb, pa = b.windows["past"], a.per_capita
        ok(all(close(wb[f"{k}_{g}"], pa[k][g]) for k in ("severe", "deaths") for g in AGES),
           f"W1 a past window's deaths and severe cases per person of each age group equal those years counted as "
           f"the decision window (deaths_u5 {wb.get('deaths_u5')} vs {pa['deaths']['u5']})")
        sh = a.meta["age_shares"]
        ok(all(close(wb[f"{k}_all"], sum(pa[k][g] * sh[g] for g in AGES)) for k in ("severe", "deaths")),
           "W1b the all-ages metrics are deaths and severe cases per person of the whole population")
        ok(all(close(b.windows["dec"][f"{k}_{g}"], b.per_capita[k][g]) for k in ("severe", "deaths") for g in AGES)
           and pa["deaths"]["u5"] > 0, "W1c a named window equal to the decision window gives the per-capita outcomes")
    case("W1", w1)

    def w2():
        # the case-management severe-care link (per-person case fatality) is counted the same way
        hz = Horizon(2015.0, 2017.0, (2016.0, 2017.0), {"dec": [2016.0, 2017.0], "half": [2016.0, 2016.5]})
        q = dict(p, cm_target=1.0, cm_severe_link=0.5, cm_add=0.3, cm_ramp_years=0.0)
        o = e.run(q, UnitSpec("t", dict(unit.attributes, m0=20.0)), Package("cm", {"cm": 1}), hz, 2)
        ok(all(close(o.windows["dec"][f"deaths_{g}"], o.per_capita["deaths"][g]) for g in AGES),
           "W2 with the severe-care link the window deaths equal the per-capita deaths")
        ok(all(0 < o.windows["half"][f"deaths_{g}"] < o.windows["dec"][f"deaths_{g}"] for g in AGES),
           "W2b a half window holds part of the deaths")
    case("W2", w2)


def write_card(path: str) -> None:
    """Engine card (describe() plus every parameter with its kind, range and source) as YAML."""
    e = MalariaEngine()
    card = e.describe().to_dict()
    card["parameters"] = [vars(p) for p in e.parameters()]
    card["species_bionomics"] = V.SPECIES
    card["hut_trial_coefficients"] = {"values": V.CHURCHER, "source": V.CHURCHER_SOURCE, "verified": "matched"}
    card["irs_decay"] = {k: {"cone_points_days_mortality": [list(x) for x in v], "logit_intercept_slope_per_day":
                             list(V.IRS_DECAY[k]), "source": V.IRS_SOURCE[k], "verified": "matched"}
                         for k, v in V.IRS_CONE.items()}
    card["irs_decay"]["short"] = {"source": V.IRS_SOURCE["short"], "verified": "unverified"}
    card["field_use"] = {
        "attrition": {"net_retention_half_days": 1.64 * 365, "net_survival_k": 20.0,
                      "function": "S(t) = exp(k - k / (1 - (t/L)^2)) for t < L, else 0; L = median sqrt((k + ln2) / ln2) "
                                  "(8.96 y at the 1.64 y median); daily loss 1 - S(a+1)/S(a) at net age a (v0.9)",
                      "source": BV + " (median LLIN retention 1.64 y, IQR 1.33-2.37, main text and Supplementary Table "
                                "1.7; smooth-compact form and k = 20, Supplementary Note 1.4)",
                      "verified": "matched",
                      "country_values": {"Nigeria": {"median_years": 2.22, "ci95": [2.00, 2.47], "surveys": 9,
                                                     "source": BV + " Supplementary Table 1.7"}},
                      "range_note": "Briet 2020 Table 3 cohorts: 24-month survival 0.69-0.98 (longer retention)"},
        "use_given_access": {"use_rate": 0.871, "source": BV + " (all-country use rate 87.1%, 83.3-90.1)", "verified": "matched"},
        "nightly_variation": {"use_persist": 0.5, "source": "assumption", "verified": "unverified"},
        "holes": {"values": V.HOLES, "coefficients_table_S9": V.BRIET, "source": V.HOLES_SOURCE, "verified": "matched",
                  "equations": "logit P = b0 + b1 ln(H+1) + b2 gamma ln(X+1) + b3 gamma ln(X+1) ln(H+1) for P_att, P_Bmu, "
                               "P_Cmu (Briet 2020 main text); repel = 1 - P_att, kill = P_att(P_Bmu + (1-P_Bmu)P_Cmu), "
                               "fed = P_att(1-P_Bmu)(1-P_Cmu)",
                  "mapping": "Briet's absolute change (holed minus intact at the remaining content X = 55 x Churcher decay) "
                             "added to Churcher's (repeat, die, feed), clipped and renormalised",
                  "unverified_parts": ["mixing the two strains linearly in bioassay mortality",
                                       "linear growth of ln(area+1) with net age, cap at 3 y", "X = 55 x Churcher decay",
                                       "the absolute-change mapping itself (our choice; a ratio mapping compounds the decay models)"]},
        "insecticide_decay_in_use": {"source": V.CHURCHER_SOURCE + " (Equations 15-19, resistance-dependent half-life)",
                                     "verified": "matched"},
        "transcription_check": "every Churcher 2016 equation used (2, 4, 8, 11-19) and every Table 6 coefficient re-checked "
                               "against the PMC MathML / table XML on 2026-10-31; Equation 4 was miscopied in v0.2 and fixed"}
    fp = FITTED
    if os.path.exists(fp):
        f = yaml.safe_load(open(fp)) or {}
        card["fit_rule"] = f.get("fit_rule")
    card["cm_cost_recipe"] = {"items": CM_COST_ITEMS, "range_per_child_year": [2.0, 5.0, 10.0],
                              "source": "Collins et al. 2014 J Glob Health, iCCM costing tool, Table 3 (recurrent, "
                                        "including medicines and supplies; per child 2-59 months within catchments)",
                              "not_charged_separately": "ACT and RDT per treated episode (inside the recurrent total)"}
    yaml.safe_dump(card, open(path, "w"), sort_keys=False, width=110)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    if sys.argv[1:2] == ["--card"]:
        write_card(sys.argv[2] if len(sys.argv) > 2 else os.path.join(HERE, "card.yaml"))
        sys.exit(0)
    print(__doc__)
