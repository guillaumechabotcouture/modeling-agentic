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
  campaign_year   decimal year of the package's net campaign (in the decision window)
  smc_months      optional; default: rainfall-peak month -1 .. +2 (season is lagged one month)
  irs_month       optional; default: three months before the transmission peak
  irs_years       optional; decimal years of IRS rounds (default: every year of the window)
  irs_product     optional; pm_cs (default, pirimiphos-methyl CS) | short
  resistance may be one value or {species: value, default: value}
Package components: net none|pyr|pbo|dual; smc 0|1; irs 0|1; cm 0|1 (enhanced CM).
Outcomes: per-person totals over the decision window by age group (u5, 5_14, 15p) for
cases, severe, deaths, treated and DALYs; named-window metrics; cost drivers per person.
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
    from . import model as M
    from . import vector as V
else:  # pragma: no cover
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
    from toolkit.engines.contract import EngineCard, ParamSpec, UnitSpec, Package, Horizon, Outcomes, resolve_params, check_engine  # noqa: E402
    from toolkit.engines.malaria import model as M, vector as V  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FITTED = os.path.join(HERE, "benchmarks", "fitted.yaml")
RESULTS = os.path.join(HERE, "benchmarks", "results.yaml")
AGES = ["u5", "5_14", "15p"]
GRIFFIN = "Griffin et al. 2010 PLoS Med 10.1371/journal.pmed.1000324; 2014 Nat Commun; 2015 Lancet Infect Dis"
A8 = "E7 attempt 8 value (models/malaria_abm.py VC), kept"
VERSION = "0.3"
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
    ("n_agents", 4000, "fixed", "agents", None, None, "numerical setting", "unverified"),
]


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
                "expected severe episodes and deaths; DALYs from GBD 2019 weights and reference life table"],
            components={"net": ["none", "pyr", "pbo", "dual"], "smc": [0, 1], "irs": [0, 1], "cm": [0, 1]},
            unit_attributes={
                "m0": "emergence density per person, [0.2, 400]", "season": "12 monthly values, mean 1",
                "resistance": "bioassay mortality [0, 1]", "species": "species shares",
                "cm_ft": "effective treatment [0, 0.95]", "itn_use": "net use after a campaign [0, 1]",
                "itn_hist_years": "past campaign years", "campaign_year": "decimal year of the package's campaign",
                "smc_months": "optional SMC months", "irs_month": "optional IRS month",
                "irs_years": "optional IRS round years", "irs_product": "pm_cs | short",
                "itn_use_at": "years after distribution at which itn_use was measured (default 0)",
                "hist_net": "net type of past campaigns (default pyr)"},
            outputs={"per_capita": ["cases", "severe", "deaths", "treated", "dalys"],
                     "windows": ["pfpr_u5", "pfpr_6m14y", "pfpr_2_10", "inc_u5", "inc_all", "inc_u5_smc", "eir_annual",
                                 "net_use", "net_access"],
                     "cost_drivers": ["nets_given", "smc_doses", "irs_sprayed", "treated_all"]},
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

    def run(self, params, unit: UnitSpec, package: Package, horizon: Horizon, seed: int) -> Outcomes:
        t0 = time.time()
        sim, P = self.build_sim(params, unit, package, horizon, seed)
        sim.run()
        return self._outcomes(sim, P, horizon, seed, t0)

    def build_sim(self, params, unit: UnitSpec, package: Package, horizon: Horizon, seed: int):
        """The Starsim sim for one run (not yet run), and the resolved parameters."""
        import starsim as ss
        P = resolve_params(self, params)
        a = dict(unit.attributes)
        comp = {"net": "none", "smc": 0, "irs": 0, "cm": 0}
        comp.update(package.components)
        season = np.asarray(a.get("season", [1.0] * 12), float)
        smc_m = tuple(a.get("smc_months") or default_smc_months(season))
        w0, w1 = horizon.window
        dis = M.Malaria(M0=float(a.get("m0", 5.0)), season=season, resistance=_resistance(a.get("resistance", 0.5)),
                        ft=float(a.get("cm_ft", 0.3)), species=a.get("species"), P=P,
                        init_eir=min(0.6 * float(a.get("m0", 5.0)), 150.0), win_start=w0, win_stop=w1,
                        smc_months=smc_m, import_eir=P["import_eir"], irs_product=a.get("irs_product", "pm_cs"))
        use, self._access_capped = access_for_use(float(a.get("itn_use", 0.55)), float(a.get("itn_use_at", 0.0)), P)
        itv = [M.CaseManagement(base=float(a.get("cm_ft", 0.3)), add=P["cm_add"] if int(comp["cm"]) else 0.0,
                                start=w0, name="cm")]
        hist = [float(y) for y in a.get("itn_hist_years", [])]
        if hist:
            itv.append(M.ITNCampaign(years=hist, use=use, net_type=a.get("hist_net", "pyr"), name="itn_hist"))
        years = list(range(int(np.floor(w0)), int(np.ceil(w1))))
        if comp["net"] != "none":
            itv.append(M.ITNCampaign(years=[float(a.get("campaign_year", w0))], use=use, net_type=comp["net"],
                                     count=True, name="itn_new"))
        if int(comp["smc"]):
            itv.append(M.SMCDelivery(years=years, months=smc_m, pe=P["smc_pe"], hi_frac=P["smc_hi_frac"], name="smc"))
        if int(comp["irs"]):
            month = int(a.get("irs_month") or ((int(np.argmax(season)) - 3) % 12 + 1))
            irs_years = [int(np.floor(float(y))) for y in a.get("irs_years", years)]
            itv.append(M.IRSRound(years=irs_years, month=month, coverage=P["irs_coverage"], name="irs"))
        sim = ss.Sim(people=_people(int(P["n_agents"])), start=float(horizon.start), stop=float(horizon.stop),
                     dt=ss.days(1), diseases=dis, demographics=_demog(), interventions=itv,
                     rand_seed=int(seed), verbose=0)
        return sim, P

    def _outcomes(self, sim, P, horizon, seed, t0) -> Outcomes:
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
            wins[name] = {
                "pfpr_u5": float(d["pfpr_u5"][sel].mean()), "pfpr_6m14y": float(d["pfpr_6m14y"][sel].mean()),
                "pfpr_2_10": float(d["pfpr_2_10"][sel].mean()), "inc_u5": float(d["inc_u5"][sel].sum()),
                "inc_all": float(d["inc_all"][sel].sum()), "inc_u5_smc": float(d["inc_u5_smc"][sel].sum()),
                "eir_annual": float(d["eir"][sel].sum() / dur),
                "net_use": float(d["net_use"][sel].mean()), "net_access": float(d["net_access"][sel].mean())}
        tot_treated = float(dz.acc["treated"].sum() / max(dz.acc_cost["pdays_all"] / ndays, 1e-9))
        cd = {"nets_given": float(dz.acc_cost["nets_given"]), "smc_doses": float(dz.acc_cost["smc_doses"]),
              "irs_sprayed": float(dz.acc_cost["irs_sprayed"]), "treated_all": tot_treated}
        return Outcomes(per_capita=pc, windows=wins, cost_drivers=cd,
                        meta={"seconds": round(time.time() - t0, 2), "seed": int(seed),
                              "itn_access_capped": bool(getattr(self, "_access_capped", False)),
                              "age_shares": {g: float(dz.acc["pdays"][i] / max(dz.acc["pdays"].sum(), 1))
                                             for i, g in enumerate(AGES)}})


def to_jsonable(o: Outcomes) -> dict:
    return {"per_capita": o.per_capita, "windows": o.windows, "cost_drivers": o.cost_drivers, "meta": o.meta}


def _self_test() -> int:
    fails = []

    def ok(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    e = MalariaEngine()
    e_par = {"n_agents": 600}
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
        sim, _ = es.build_sim(dict(over, n_agents=2000), u1, Package("x", {"net": "pyr", "irs": 1}), h1, 5)
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
    sim, _ = es.build_sim({"n_agents": 600}, UnitSpec("t", dict(u1.attributes, irs_years=[2020.0])),
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
        sim2, _ = es.build_sim({"n_agents": 1500, "use_persist": persist}, u1, Package("n", {}), h1, 6)
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
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


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
