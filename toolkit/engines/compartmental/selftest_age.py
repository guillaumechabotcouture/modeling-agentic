#!/usr/bin/env python3
"""Self-tests of the compartmental engine's v0.2 additions (age-structured questions), split from engine.py.

    python3 toolkit/engines/compartmental/selftest_age.py --self-test

engine.py --self-test covers v0.1 (contract, batching, stop rule, pack subset, stamps); this file covers what v0.2
adds, each with v0.1 behaviour as the default: time_unit year, per-group calibrated parameters (s0:<g>,
ratio:<o>:<g>), coverage_scale, the SEIR generation interval, components acting during the epidemic (day), and the
figure hooks schedule() and run_series() (figures-hooks/1), checked through toolkit/engines/hooks.py. Nothing here
names a disease.
"""
from __future__ import annotations

import math
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from toolkit.engines.compartmental.engine import (CompartmentalEngine, seir_generation_interval,  # noqa: E402
                                                  YEAR_DAYS)
from toolkit.engines.contract import Horizon, Package, UnitSpec, check_engine  # noqa: E402

AGES = ["a0", "a1", "a2", "a3", "a4"]
C5 = [[2.2, 1.6, 3.0, 0.7, 0.5], [0.8, 9.0, 3.8, 1.1, 0.4], [0.6, 1.4, 5.6, 1.5, 0.7],
      [0.3, 1.0, 3.5, 2.2, 1.1], [0.3, 0.5, 2.1, 1.3, 1.6]]
N5 = [3.7e5, 7.4e5, 2.0e6, 8.3e5, 6.9e5]


def _age(**kw):
    cfg = dict(groups=AGES, structure="age_groups_contact_matrix", mode="to_extinction", initialiser="given",
               dt="day", generation_interval={"seir": {"latent_days": 1.5, "infectious_days": 3.0}},
               components={f"v{g}": {"kind": "doses", "group": g, "block_size": 7.0e4, "max_blocks": 6} for g in AGES},
               outcomes=["hosp"], tol=1e-10, step_cap=20000)
    cfg.update(kw)
    return CompartmentalEngine(**cfg)


def _unit(**kw):
    a = {"N": list(N5), "contact": C5, "seed": 1e-7, "intro": "a1", "s0": [0.9, 0.8, 0.65, 0.6, 0.55],
         "ratio:hosp": [0.004, 0.001, 0.002, 0.006, 0.02]}
    a.update(kw)
    return UnitSpec("u", a)


def _self_test() -> int:
    fails = []

    def ok(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    def safe(fn):
        try:
            return fn(), None
        except Exception as e:  # noqa: BLE001 -- a test that raises is a failing test
            return None, f"{type(e).__name__}: {e}"

    hd = Horizon(0, 20000, (0, 20000), {"all": [0, 20000]})
    hy = Horizon(2026.0, 2046.0, (2026.0, 2046.0), {"all": [2026.0, 2046.0]})
    P = {"r0": 1.6}

    # A1 time_unit year: the same epidemic as in days, read in years
    ed, ey = _age(), _age(time_unit="year")
    od, oy = ed.run(P, _unit(), Package("0", {}), hd, 1), ey.run(P, _unit(), Package("0", {}), hy, 1)
    ok(all(abs(od.per_capita["infections"][g] - oy.per_capita["infections"][g]) < 1e-15 for g in AGES)
       and abs(od.windows["all"]["attack_rate"] - oy.windows["all"]["attack_rate"]) < 1e-15,
       "A1 time_unit year gives the day-unit epidemic, horizon read in decimal years from horizon.start")
    short = Horizon(2026.0, 2026.05, (2026.0, 2026.05), {"all": [2026.0, 2026.05]})
    _, err = safe(lambda: ey.run(P, _unit(), Package("0", {}), short, 1))
    ok(err is not None and "outlasts the decision window" in err,
       f"A2 an epidemic still running at the decision window's end fails closed (year unit): {err}")
    ok(ey.days(2026.5, hy) == 0.5 * YEAR_DAYS and ed.days(12.0, hd) == 12.0, "A3 horizon times convert to days")

    # A4 per-group parameters equal the unit attributes they replace
    ep = _age(per_group_parameters=["s0", "ratio:hosp"])
    u = _unit()
    pp = dict(P, **{f"s0:{g}": v for g, v in zip(AGES, u.attributes["s0"])},
              **{f"ratio:hosp:{g}": v for g, v in zip(AGES, u.attributes["ratio:hosp"])})
    up = UnitSpec("u", {k: v for k, v in u.attributes.items() if k not in ("s0", "ratio:hosp")})
    o1, o2 = ed.run(P, u, Package("b", {"va2": 3}), hd, 1), ep.run(pp, up, Package("b", {"va2": 3}), hd, 1)
    ok(o1.per_capita == o2.per_capita and o1.windows == o2.windows and o1.cost_drivers == o2.cost_drivers,
       "A4 per_group_parameters s0 and ratio:<o>: calibrated engine parameters give the attribute outcomes")
    names = {p.name: p for p in ep.parameters()}
    ok(all(names[f"s0:{g}"].kind == "calibrated" and names[f"s0:{g}"].lo == 0.0 and names[f"s0:{g}"].hi == 1.0
           for g in AGES) and "s0" not in ep.describe().unit_attributes
       and "ratio:hosp" not in ep.describe().unit_attributes and f"ratio:hosp:{AGES[0]}" in names,
       "A5 per-group parameters are on the card (calibrated, range [0, 1]); the attributes they replace are not")
    ok(check_engine(ep, up, Package("b", {"va2": 3}), hd) == [], "A6 the per-group configuration passes the contract")
    for bad in (["s1"], ["ratio:nothing"], ["s0", "s0"]):
        _, err = safe(lambda: _age(per_group_parameters=bad))
        ok(err is not None and "ValueError" in err, f"A7 per_group_parameters {bad} is refused: {err}")
    _, err = safe(lambda: ep.run(dict(pp, **{"ratio:hosp:a0": -0.1}), up, Package("0", {}), hd, 1))
    ok(err is not None and ">= 0" in err, f"A7b a negative ratio is refused: {err}")

    # A8 coverage_scale on cohort_history
    coh = dict(groups=["old", "c1", "c2"], structure="birth_cohorts", mode="to_extinction", initialiser="cohort_history",
               doses=["d1", "d2"], generation_interval=[0.5, 0.5], tol=1e-12)
    ec = CompartmentalEngine(**coh)
    ua = {"N": [5.0e5, 1.0e5, 1.0e5], "birth_year": [2000, 2015, 2016], "decision_year": 2026,
          "always_immune": ["old"], "contact_rate": 12.0, "coverage:d1": [0.0, 0.8, 0.9], "coverage:d2": [0.0, 0.5, 0.6]}
    sp = {"seroconversion:d1": 0.85, "seroconversion:d2": 0.9, "r0": 15.0}
    s1 = ec.initial_state(sp, UnitSpec("c", ua))["s_initialiser"]
    s_one = ec.initial_state(sp, UnitSpec("c", dict(ua, coverage_scale=1.0)))["s_initialiser"]
    s_hi = ec.initial_state(sp, UnitSpec("c", dict(ua, coverage_scale=1.2)))["s_initialiser"]
    by_hand = (1 - min(1.0, 1.2 * 0.9) * 0.85) * (1 - min(1.0, 1.2 * 0.6) * 0.9)
    ok(s1 == s_one and abs(s_hi[2] - by_hand) < 1e-12 and "coverage_scale" in ec.describe().unit_attributes,
       f"A8 coverage_scale: latent coverage min(1, scale x declared), scale 1 = absent ({s_hi[2]:.6f} vs {by_hand:.6f})")
    _, err = safe(lambda: ec.initial_state(sp, UnitSpec("c", dict(ua, coverage_scale=0.0))))
    ok(err is not None and "positive" in err, f"A8b a non-positive coverage_scale is refused: {err}")

    # A9 SEIR generation interval
    w = np.asarray(seir_generation_interval(1.5, 3.0, 1.0))
    s = np.arange(1, len(w) + 1)
    w_eq = np.asarray(seir_generation_interval(2.0, 2.0, 1.0))
    ok(abs(w.sum() - 1) < 1e-12 and np.all(w >= 0) and abs(float(np.dot(w, s)) - 4.5) < 0.05
       and abs(float(np.dot(w_eq, np.arange(1, len(w_eq) + 1))) - 4.0) < 0.05,
       f"A9 SEIR generation interval: sums to 1, mean latent + infectious ({float(np.dot(w, s)):.4f}; L = D case too)")
    ok(list(ed.w) == list(w) and ed.config()["generation_interval_declared"] == {"seir": {"latent_days": 1.5,
                                                                                         "infectious_days": 3.0}},
       "A9b the engine discretises a declared {seir: ...} interval and records the declaration in its configuration")
    for bad in ({"seir": {"latent_days": 1.0}}, {"seir": {"latent_days": -1, "infectious_days": 2}},
                {"seir": {"latent_days": 1, "infectious_days": 2}, "w": [1.0]}):
        _, err = safe(lambda: _age(generation_interval=bad))
        ok(err is not None, f"A9c a malformed SEIR interval {bad} is refused: {err}")

    # A10 components acting during the epidemic
    camp = dict(groups=["all"], mode="to_extinction", generation_interval={"seir": {"latent_days": 2, "infectious_days": 3}},
                tol=1e-12, components={"vax": {"kind": "campaign", "targets": ["all"], "reach": 0.6}})
    e0 = CompartmentalEngine(**camp)
    u1 = UnitSpec("x", {"N": [1.0e6], "s0": [1.0], "seed": 1e-8})
    pv = {"r0": 2.0, "efficacy:vax": 0.9}
    none = e0.run(pv, u1, Package("0", {}), hd, 1)
    early = e0.run(pv, u1, Package("v", {"vax": 1}), hd, 1)
    e_mid = CompartmentalEngine(**dict(camp, components={"vax": {"kind": "campaign", "targets": ["all"], "reach": 0.6,
                                                                 "day": 40}}))
    mid = e_mid.run(pv, u1, Package("v", {"vax": 1}), hd, 1)
    e_late = CompartmentalEngine(**dict(camp, components={"vax": {"kind": "campaign", "targets": ["all"], "reach": 0.6,
                                                                  "day": 3000}}))
    late = e_late.run(pv, u1, Package("v", {"vax": 1}), hd, 1)
    ar = lambda o: o.windows["all"]["attack_rate"]  # noqa: E731
    ok(ar(early) < ar(mid) < ar(none), f"A10 a campaign during the epidemic averts less than one before it "
       f"({ar(early):.4f} < {ar(mid):.4f} < {ar(none):.4f})")
    ok(abs(ar(late) - ar(none)) < 1e-12 and abs(late.cost_drivers["doses:vax"] - 0.6) < 1e-12,
       "A11 a campaign after extinction averts nothing; its doses are still delivered (the run continues to it)")
    s_end = mid.windows["all"]["s_end"]
    pre = e_mid.run(pv, u1, Package("v", {"vax": 1}), Horizon(0, 20000, (0, 20000), {"pre": [0, 40]}), 1)
    protected = 0.6 * 0.9 * pre.windows["pre"]["s_end"]          # S just before day 40, times reach x efficacy
    ok(abs(s_end - (1.0 - ar(mid) - protected)) < 1e-12,
       f"A12 the susceptible trajectory subtracts the susceptibles a mid-epidemic component protects "
       f"({s_end:.6f} vs {1.0 - ar(mid) - protected:.6f})")
    ok(e_mid.initial_state(pv, u1, Package("v", {"vax": 1}))["s_start"] == [1.0], "A13 initial_state(): a component "
       "acting during the epidemic is not in the initial state")
    _, err = safe(lambda: CompartmentalEngine(**dict(camp, dt="week", generation_interval=[1.0],
                                                     components={"vax": {"kind": "campaign", "targets": ["all"],
                                                                         "day": 10}})))
    ok(err is not None and "whole number" in err, f"A14 a day that is not a whole number of steps is refused: {err}")
    win = CompartmentalEngine(**dict(camp, mode="windowed", components={"vax": {"kind": "campaign", "targets": ["all"],
                                                                                "day": 500}}))
    _, err = safe(lambda: win.run(pv, u1, Package("v", {"vax": 1}), Horizon(0, 100, (0, 100), {"all": [0, 100]}), 1))
    ok(err is not None and "after the windowed horizon" in err, f"A15 a component after a windowed horizon fails: {err}")

    # A16 hooks: offered only with time_unit year; checked by toolkit/engines/hooks.py
    from toolkit.engines import hooks as H
    ok(not H.supports(ed, "schedule") and not H.supports(ed, "series"), "A16 a day-unit engine offers no figure hooks")
    eyh = _age(time_unit="year", components={**{f"v{g}": {"kind": "doses", "group": g, "block_size": 7.0e4,
                                                           "max_blocks": 6} for g in AGES}})
    ok(H.supports(eyh, "schedule") and H.supports(eyh, "series"), "A17 a year-unit engine offers both hooks")
    pk = Package("p", {"va1": 2, "va4": 1})
    ev, err = safe(lambda: H.schedule(eyh, P, _unit(), pk, hy))
    ok(err is None and sorted(e["component"] for e in ev) == ["va1", "va4"]
       and all(e["kind"] == "decision" and e["charged"] and e["t0"] == 2026.0 for e in ev),
       f"A18 schedule(): one charged decision event per component in the package, at horizon.start: {err or ev}")
    pks = [Package("0", {}), pk]
    ser, err = safe(lambda: H.run_series(eyh, P, _unit(), pks, hy, 1, ["incidence", "susceptible"]))
    ok(err is None, f"A19 run_series() passes the hooks contract check (monthly grid, both metrics): {err}")
    if ser:
        outs = eyh.run_batch([P, P], _unit(), pks, hy, 1)
        tot = [sum(v / 12.0 for v in r["values"]["incidence"]) for r in ser]
        ar_ = [o.windows["all"]["attack_rate"] for o in outs]
        ok(all(abs(a - b) < 1e-12 for a, b in zip(tot, ar_)),
           f"A20 run_series incidence integrates to the run's attack rate ({tot} vs {ar_})")
        sus = ser[0]["values"]["susceptible"]
        ok(abs(sus[-1] - outs[0].windows["all"]["s_end"]) < 1e-9 and sus[0] <= outs[0].windows["all"]["s_start"],
           "A21 run_series susceptible share ends at the run's s_end")
        o_again = eyh.run_batch([P, P], _unit(), pks, hy, 1)
        ok(all(a.per_capita == b.per_capita for a, b in zip(outs, o_again)), "A22 run_series leaves run_batch unchanged")
    hdel = __import__("toolkit.engines.horizon", fromlist=["make_horizon"]).make_horizon(
        2026.0, 2046.0, [2026.0, 2046.0], {"all": [2026.0, 2046.0]}, delivery=[2026.5, 2027.0])
    _, err = safe(lambda: eyh.run(P, _unit(), pk, hdel, 1))
    ok(err is not None and "outside the delivery period" in err,
       f"A23 time_unit year: a component acting outside the delivery period fails closed: {err}")

    # A25-A27 final_size: the whole epidemic counts, whatever the window end
    efs = _age(time_unit="year", final_size=True)
    o_fs = efs.run(P, _unit(), Package("0", {}), short, 1)
    ok(abs(o_fs.windows["all"]["attack_rate"] - od.windows["all"]["attack_rate"]) < 1e-9
       and abs(o_fs.per_capita["infections"]["a2"] - od.per_capita["infections"]["a2"]) < 1e-9
       and o_fs.meta["time_axis"] == "generations",
       f"A25 final_size: a window shorter than the epidemic still counts its final size, on the generation grid "
       f"({o_fs.windows['all']['attack_rate']:.10f} vs {od.windows['all']['attack_rate']:.10f})")
    # A33: near criticality the generation grid converges in few steps, to the declared interval's final size
    near = dict(P, r0=1.0 / 0.6935)
    o_d = ed.run(near, _unit(), Package("0", {}), hd, 1)
    o_g = efs.run(near, _unit(), Package("0", {}), short, 1)
    ok(abs(o_g.windows["all"]["attack_rate"] - o_d.windows["all"]["attack_rate"]) < 1e-8
       and o_g.meta["steps"] < o_d.meta["steps"] / 3,
       f"A33 a near-critical final size on the generation grid equals the daily one in fewer steps "
       f"({o_g.windows['all']['attack_rate']:.3e} vs {o_d.windows['all']['attack_rate']:.3e}; "
       f"{o_g.meta['steps']} vs {o_d.meta['steps']} steps)")
    _, err = safe(lambda: efs.run(P, _unit(), Package("0", {}),
                                  Horizon(2026.0, 2046.0, (2026.0, 2046.0), {"early": [2026.0, 2026.5]}), 1))
    ok(err is not None and "generations" in err, f"A34 a named window inside a generation-grid run is refused: {err}")
    _, err = safe(lambda: _age(mode="windowed", final_size=True))
    ok(err is not None and "to_extinction" in err, f"A26 final_size needs to_extinction: {err}")
    late_w = Horizon(2026.0, 2046.0, (2026.5, 2046.0), {"all": [2026.5, 2046.0]})
    _, err = safe(lambda: efs.run(P, _unit(), Package("0", {}), late_w, 1))
    ok(err is not None and "must start at horizon.start" in err, f"A27 final_size: a window after the start fails: {err}")

    # Codex r1 (tests first): A28-A32
    efs_f = _age(time_unit="year", final_size=True, forcing="cosine", forcing_period=365.0)
    o28 = efs_f.run(dict(P, forcing_amplitude=0.2, forcing_phase=30.0), _unit(), Package("0", {}), short, 1)
    ok(all(math.isfinite(v) for v in o28.windows["all"].values()),
       f"A28 (r1 P1-5) final_size with seasonal forcing: every window metric is finite ({o28.windows['all']['reff_end']})")
    e29 = CompartmentalEngine(groups=["all"], mode="to_extinction", generation_interval=[1.0], time_unit="year",
                              components={"v": {"kind": "campaign", "targets": ["all"], "reach": 1.0, "day": 10}})
    h29 = Horizon(2026.0, 2027.0, (2026.0, 2027.0), {"all": [2026.0, 2027.0]})
    u29 = UnitSpec("z", {"N": [1.0e6], "s0": [1.0], "seed": 0.0})
    s29 = H.run_series(e29, {"r0": 2.0, "efficacy:v": 1.0}, u29, [Package("v", {"v": 1})], h29, 1, ["susceptible"])
    want = 10.0 / (YEAR_DAYS / 12.0)
    ok(abs(s29[0]["values"]["susceptible"][0] - want) < 1e-9 and s29[0]["values"]["susceptible"][1] == 0.0,
       f"A29 (r1 P1-6) the monthly susceptible share is a time mean ({s29[0]['values']['susceptible'][0]:.4f} vs "
       f"{want:.4f})")
    e30 = _age(time_unit="year")
    cut = 2026.0 + 10.0 / YEAR_DAYS
    h30 = Horizon(2026.0, 2046.0, (2026.0, 2046.0), {"early": [2026.0, cut], "late": [cut, 2046.0],
                                                     "all": [2026.0, 2046.0]})
    o30 = e30.run(P, _unit(), Package("0", {}), h30, 1)
    by_step = 1e-7 + sum(o30.meta["incidence"][:9])          # seeds at step 0, then steps 1..9 (days 1-9)
    ok(abs(o30.windows["early"]["attack_rate"] - by_step) < 1e-15
       and abs(o30.windows["early"]["attack_rate"] + o30.windows["late"]["attack_rate"]
               - o30.windows["all"]["attack_rate"]) < 1e-14,
       f"A30 (r1 P1-7) a window ending at an event time excludes that step ({o30.windows['early']['attack_rate']:.3e} "
       f"vs {by_step:.3e})")
    e31 = CompartmentalEngine(**dict(camp, components={"vax": {"kind": "campaign", "targets": ["all"], "reach": 0.6,
                                                               "day": 3000}}))
    o31 = e31.run(pv, u1, Package("0", {}), hd, 1)
    ok(o31.windows["all"]["steps"] < 3000, f"A31 (r1 P2-14) a late component the package does not set does not hold "
       f"the run open ({o31.windows['all']['steps']:.0f} steps)")
    for cap in (0, -3, float("nan")):
        _, err = safe(lambda: seir_generation_interval(1.5, 3.0, 1.0, max_days=cap))
        ok(err is not None, f"A32 (r1 P2-15) max_days {cap} is refused: {err}")

    # A24 a contract check of each new configuration
    ok(check_engine(eyh, _unit(), pk, hy) == [] and check_engine(e_mid, u1, Package("v", {"vax": 1}), hd) == [],
       "A24 the year-unit and mid-epidemic configurations pass the contract test")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    print(__doc__)
