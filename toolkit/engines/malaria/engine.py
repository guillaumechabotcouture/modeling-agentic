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
  itn_use         achieved net use after a campaign
  itn_hist_years  decimal years of past pyrethroid-net campaigns
  campaign_year   decimal year of the package's net campaign (in the decision window)
  smc_months      optional; default: rainfall-peak month -1 .. +2 (season is lagged one month)
  irs_month       optional; default: three months before the transmission peak
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

# name, value, kind, units, lo, hi, source, verified
_PARAMS = [
    ("itn_kill_max", 0.36, "fitted", "probability", 0.10, 0.90, "fitted: B7, B8", "unverified"),
    ("itn_kexp", 3.0, "fixed", "exponent", None, None, A8 + "; form after Churcher 2016 eLife 10.7554/eLife.16090", "unverified"),
    ("itn_rep_max", 0.05, "fixed", "probability", None, None, A8, "unverified"),
    ("itn_block", 0.15, "fixed", "probability", None, None, A8 + " (barrier)", "unverified"),
    ("block_half", 3 * 365., "fixed", "days", None, None, "assumption (net physical integrity half-life 3 y)", "unverified"),
    ("ins_half", 2 * 365., "fixed", "days", None, None, A8, "unverified"),
    ("pbo_restore", 1.0, "fitted", "share", 0.0, 1.0, "fitted: B9", "unverified"),
    ("pbo_half", 1 * 365., "fixed", "days", None, None, A8, "unverified"),
    ("cfp_kill", 0.36, "fitted", "probability", 0.0, 0.85, "fitted: B10, B11", "unverified"),
    ("cfp_half", 2.5 * 365., "fixed", "days", None, None, A8, "unverified"),
    ("irs_kill", 0.18, "fitted", "probability", 0.0, 0.95, "fitted: B12, B12b", "unverified"),
    ("irs_rep", 0.10, "fixed", "probability", None, None, A8, "unverified"),
    ("irs_half", 0.4 * 365., "fixed", "days", None, None, A8, "unverified"),
    ("irs_coverage", 0.8, "scenario", "share", None, None, "policy assumption (attempt 8 IRS_COVERAGE)", "unverified"),
    ("net_retention_half", 2 * 365., "fixed", "days", None, None, A8 + " (net retention half-life ~2 y)", "unverified"),
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
            name="malaria-starsim", version="0.1-spike",
            mechanisms=[
                "Griffin-lite human natural history and immunity (fixed: " + GRIFFIN + ")",
                "species-specific vectors (Q0, phi_I, phi_B, mu0 from the D6 prior; endophily assumed)",
                "ITN: deflection (repellence + blocking), killing, feeding; pyrethroid resistance; PBO; dual-AI; decay",
                "IRS: deterrence on entry and killing of indoor-resting fed mosquitoes; decay",
                "ITN x IRS redundancy from shared indoor target (no interaction factor)",
                "SMC (28-day protection, clearance); case management (effective treatment)",
                "expected severe episodes and deaths; DALYs from GBD 2019 weights and reference life table"],
            components={"net": ["none", "pyr", "pbo", "dual"], "smc": [0, 1], "irs": [0, 1], "cm": [0, 1]},
            unit_attributes={
                "m0": "emergence density per person, [0.2, 400]", "season": "12 monthly values, mean 1",
                "resistance": "bioassay mortality [0, 1]", "species": "species shares",
                "cm_ft": "effective treatment [0, 0.95]", "itn_use": "net use after a campaign [0, 1]",
                "itn_hist_years": "past campaign years", "campaign_year": "decimal year of the package's campaign",
                "smc_months": "optional SMC months", "irs_month": "optional IRS month"},
            outputs={"per_capita": ["cases", "severe", "deaths", "treated", "dalys"],
                     "windows": ["pfpr_u5", "pfpr_6m14y", "pfpr_2_10", "inc_u5", "inc_all", "inc_u5_smc", "eir_annual"],
                     "cost_drivers": ["nets_given", "smc_doses", "irs_sprayed", "treated_all"]},
            age_groups=AGES,
            valid_ranges={"m0": [0.2, 400], "resistance": [0.1, 1.0], "cm_ft": [0.0, 0.95], "eir_annual": [0.1, 300]},
            fitted={k: v for k, v in self.fitted.items()},
            benchmarks=bench,
            notes=["endophily per species is an unverified assumption (not in the D6 prior)",
                   "resistance-to-killing uses attempt 8's power form; its maximum is fitted, not Churcher 2016's coefficients"])

    def run(self, params, unit: UnitSpec, package: Package, horizon: Horizon, seed: int) -> Outcomes:
        import starsim as ss
        t0 = time.time()
        P = resolve_params(self, params)
        a = dict(unit.attributes)
        comp = {"net": "none", "smc": 0, "irs": 0, "cm": 0}
        comp.update(package.components)
        season = np.asarray(a.get("season", [1.0] * 12), float)
        smc_m = tuple(a.get("smc_months") or default_smc_months(season))
        w0, w1 = horizon.window
        dis = M.Malaria(M0=float(a.get("m0", 5.0)), season=season, resistance=float(a.get("resistance", 0.5)),
                        ft=float(a.get("cm_ft", 0.3)), species=a.get("species"), P=P,
                        init_eir=min(0.6 * float(a.get("m0", 5.0)), 150.0), win_start=w0, win_stop=w1,
                        smc_months=smc_m, import_eir=P["import_eir"])
        use = float(a.get("itn_use", 0.55))
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
            itv.append(M.IRSRound(years=years, month=month, coverage=P["irs_coverage"], name="irs"))
        sim = ss.Sim(people=_people(int(P["n_agents"])), start=float(horizon.start), stop=float(horizon.stop),
                     dt=ss.days(1), diseases=dis, demographics=_demog(), interventions=itv,
                     rand_seed=int(seed), verbose=0)
        sim.run()
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
                "eir_annual": float(d["eir"][sel].sum() / dur)}
        tot_treated = float(dz.acc["treated"].sum() / max(dz.acc_cost["pdays_all"] / ndays, 1e-9))
        cd = {"nets_given": float(dz.acc_cost["nets_given"]), "smc_doses": float(dz.acc_cost["smc_doses"]),
              "irs_sprayed": float(dz.acc_cost["irs_sprayed"]), "treated_all": tot_treated}
        return Outcomes(per_capita=pc, windows=wins, cost_drivers=cd,
                        meta={"seconds": round(time.time() - t0, 2), "seed": int(seed),
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
    ok(set(names) == {"itn_kill_max", "pbo_restore", "cfp_kill", "irs_kill"}, "M7 four engine-level parameters are fitted")
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
    yaml.safe_dump(card, open(path, "w"), sort_keys=False, width=110)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    if sys.argv[1:2] == ["--card"]:
        write_card(sys.argv[2] if len(sys.argv) > 2 else os.path.join(HERE, "card.yaml"))
        sys.exit(0)
    print(__doc__)
