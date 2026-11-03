#!/usr/bin/env python3
"""Malaria engine behind the `Engine` contract (toolkit/engines/contract.py).

    python3 -m toolkit.engines.malaria.engine --self-test
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
  itn_hist_years  decimal years of past pyrethroid-net campaigns
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
VERSION = "0.8"
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
     "(IQR 1.33-2.37) across 40 countries; used as an exponential half-life (the paper's loss curve is S-shaped: shape unverified)", "matched"),
    ("use_rate", 0.871, "fixed", "share", 0.833, 0.901, BV + ": ITN use among those with access, all-country 87.1% (83.3-90.1)", "matched"),
    ("use_persist", 0.5, "fixed", "share", 0.0, 1.0, "assumption: share of the use gap that is persistent non-use (the rest is "
     "night-to-night variation)", "unverified"),
    ("net_holes", 1.0, "fixed", "switch", None, None, "holes in nets in use on (1) / off (0): " + V.HOLES_SOURCE, "matched"),
    ("smc_pe", 0.882, "fixed", "share", 0.787, 0.934, "ACCESS-SMC Partnership 2020 Lancet 10.1016/S0140-6736(20)32227-3", "matched"),
    ("smc_hi_frac", 0.85, "fixed", "share", None, None, A8 + " (SMC access groups)", "unverified"),
    ("cm_add", 0.15, "scenario", "probability", None, None, A8 + " (enhanced CM: +0.15 effective treatment)", "unverified"),
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
POST_FORK = frozenset({"irs_kill", "irs_rep", "irs_half", "irs_coverage", "smc_pe", "smc_hi_frac", "cm_add",
                       "cfr_severe_treated", "cfr_severe_untreated", "severe_access_add", "dw_moderate", "dw_severe",
                       "dur_moderate_d", "dur_severe_d", "le_u5", "le_5_14", "le_15p"})
DUAL_NET = frozenset({"cfp_kill", "cfp_half"})
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
    ret = float(np.exp(-np.log(2) * at_years * 365.0 / P["net_retention_half"]))
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
                "field use of nets: attrition (median retention, Bertozzi-Villa 2021), use below access with nightly "
                "variation (use rate, same source), holes growing with net age (Briet 2020); survey use mapped to access",
                "households: correlated net use within households, correlated net use and spraying, "
                "biting heterogeneity correlated with net non-use",
                "ITN x IRS redundancy from shared indoor target, diversion and correlated coverage (no interaction factor)",
                "SMC (28-day protection, clearance); case management (effective treatment)",
                "delivery period distinct from the outcome window: components act and cost drivers count only in "
                "delivery years; routine net campaigns after the package's campaign in every arm (v0.6)",
                "routine SMC in every arm after the delivery period, uncharged (v0.7)",
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
                               "the unit's SMC months: every arm, never charged; needs objective.delivery (default 0)"},
            outputs={"per_capita": ["cases", "severe", "deaths", "treated", "dalys"],
                     "windows": list(M.PFPR_BANDS) + ["inc_u5", "inc_all", "inc_u5_smc", "eir_annual",
                                                      "net_use", "net_access"],
                     "cost_drivers": ["nets_given", "smc_doses", "irs_sprayed", "treated_all"],
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
        plan["routine_years"] = cls._routine_years(a, horizon)   # validated here, before any sim is built
        rs = cls._smc_routine(a, horizon)            # v0.7: and routine SMC with the package's own rounds
        if rs is not None:
            pkg = [t for c, t in timed if c == "SMC round"]
            bad = smc_schedule_problems(pkg, rs["rounds"], rs["months"], d0, d1, float(horizon.window[1]))
            if bad:
                raise ValueError("; ".join(bad))
        plan["smc_routine"] = rs
        # one intervention (itn_hist) runs past and routine campaigns, in this order (as _base_sim builds it)
        ev += [("itn_hist", "net", f"past net campaign ({plan['hist_net']})", "history", t) for t in plan["hist_years"]]
        ev += [("itn_hist", "net", f"routine net campaign ({plan['hist_net']})", "routine", t)
               for t in plan["routine_years"]]
        ev += [("smc_routine", "smc", "routine SMC round", "routine", t) for t in (rs or {}).get("rounds", [])]
        plan["deliveries"] = ev
        # where cost drivers count (model.delivery_contains): the delivery period, or the window when none is given
        w0, w1 = (float(x) for x in horizon.window)
        plan["accounting"] = {"del_start": d0 if ex else None, "del_stop": d1 if ex else None,
                              "win_start": w0, "win_stop": w1}
        return plan

    def _plan(self, P: dict, a: dict, components: dict, horizon: Horizon) -> dict:
        """The package's component settings and the decimal years at which they first act (from delivery_plan)."""
        dp = self.delivery_plan(a, components, horizon)
        return {"cm_add": P["cm_add"] if dp["cm"]["on"] else 0.0, "net": dp["net"], "smc": dp["smc"],
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
        skip = POST_FORK | (frozenset() if a.get("hist_net", "pyr") == "dual" else DUAL_NET)
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
            itv.append(M.ITNCampaign(years=hist, use=use, net_type=dp["hist_net"], name="itn_hist"))
        itv += [M.ITNCampaign(years=[], use=use, net_type="pyr", count=True, name="itn_new"),
                M.SMCDelivery(years=[], pe=P["smc_pe"], hi_frac=P["smc_hi_frac"], name="smc"),
                M.IRSRound(years=[], coverage=P["irs_coverage"], name="irs")]
        rs = dp["smc_routine"]
        if rs is not None:     # v0.7: built only when set, last, so a unit without it gets the v0.6 sim exactly
            itv.append(M.SMCDelivery(years=rs["years"], months=rs["months"], pe=P["smc_pe"], hi_frac=P["smc_hi_frac"],
                                     count=False, after=rs["after"], before=rs["before"], name="smc_routine"))
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
        net, smc, irs = itv["itn_new"], itv["smc"], itv["irs"]
        net.years = list(cfg["net"]["years"]) if cfg["net"] else []
        net.code = V.NET_CODE[cfg["net"]["net_type"]] if cfg["net"] else V.NET_CODE["pyr"]
        smc.years = list(cfg["smc"]["years"]) if cfg["smc"] else []
        if cfg["smc"]:
            smc.months = list(cfg["smc"]["months"])
        smc.pe, smc.hi_frac = P["smc_pe"], P["smc_hi_frac"]
        if "smc_routine" in itv:   # v0.7: routine rounds act after the fork, with the member's own SMC parameters
            itv["smc_routine"].pe, itv["smc_routine"].hi_frac = P["smc_pe"], P["smc_hi_frac"]
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
        """(starts, ends) of the monthly steps (hooks contract figures-hooks/1): step k starts at horizon.start +
        k/12 for k < round((window end - start) x 12); the last ends at the window's end (a little longer or shorter
        than a month when the window end is off the monthly grid)."""
        st, w1 = float(horizon.start), float(horizon.window[1])
        n = max(int(round((w1 - st) * 12)), 1)
        t = [st + k / 12 for k in range(n)]
        return t, t[1:] + [w1]

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
        if capped is None:
            capped = bool(getattr(self, "_access_capped", False))
        return Outcomes(per_capita=pc, windows=wins, cost_drivers=cd,
                        meta=dict({"seconds": round(time.time() - t0, 2), "seed": int(seed),
                                   "itn_access_capped": bool(capped),
                                   "age_shares": {g: float(dz.acc["pdays"][i] / max(dz.acc["pdays"].sum(), 1))
                                                  for i, g in enumerate(AGES)}}, **(extra or {})))


def to_jsonable(o: Outcomes) -> dict:
    return {"per_capita": o.per_capita, "windows": o.windows, "cost_drivers": o.cost_drivers, "meta": o.meta}


def _self_test() -> int:
    fails = []

    def ok(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    e = MalariaEngine()
    e_par = {"n_agents": 600, "burnin_years": 0}      # mechanism tests: no burn-in (v0.5 cases below)
    unit = UnitSpec("t", {"m0": 8.0, "season": [1.0] * 12, "resistance": 0.5, "species": {"gambiae": 0.7, "arabiensis": 0.3},
                          "cm_ft": 0.3, "itn_use": 0.6, "itn_hist_years": [2016.3], "campaign_year": 2018.1})
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
    _self_test_v08(ok, e, unit)
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
            n = int(round((float(h.window[1]) - float(h.start)) * 12))
            ok(all(len(x["t"]) == n and all(abs(x["t"][k] - (float(h.start) + k / 12)) < 1e-6 for k in range(n))
                   and abs(x["t_end"][-1] - float(h.window[1])) < 1e-12 for x in r) and r[0]["t"] == r[1]["t"],
               f"S12c the grid is horizon.start + k/12 for k < round((window end - start) x 12) = {n}, ends at the "
               "window end, and is the same for every arm")
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
        "attrition": {"net_retention_half_days": 1.64 * 365, "source": BV + " (median LLIN retention 1.64 y, IQR 1.33-2.37)",
                      "verified": "matched", "unverified_parts": ["exponential shape (the paper's loss curve is S-shaped)"],
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
    yaml.safe_dump(card, open(path, "w"), sort_keys=False, width=110)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    if sys.argv[1:2] == ["--card"]:
        write_card(sys.argv[2] if len(sys.argv) > 2 else os.path.join(HERE, "card.yaml"))
        sys.exit(0)
    print(__doc__)
