#!/usr/bin/env python3
"""Acceptance of the joint calibration on a second disease area and a canary with no allocation units, on a real v2
engine (the renewal engine, toolkit/engines/renewal), with planted values: the calibration must recover them.

    python3 toolkit/calibration/acceptance.py --self-test

  AC1  units: ten populations, each with its own susceptible share (the per-unit parameter) and a shared R0 and
       intervention effect; targets per population: a serosurvey attack rate (sampling sd from its sample size) and
       the cumulative incidence before the intervention (declared sd); a shared trial target (reproduction-number
       ratio with and without the component in a reference population). Every population matched, the planted values inside
       the region, the effect contracted. Without the trial the effect is not identified (depletion trades the
       susceptible share against it), and the region says so (AC1d).
  AC2  the same with one population whose observed attack rate no susceptible share reaches: unmatched, with the
       direction, and blocked.
  AC3  canary, no units: one population, shared R0 and seed only, shared targets (attack rate, cumulative
       incidence before the intervention window).
  AC4  the same canary with the peak week instead: a step-valued output the emulator check refuses.
The production engine's acceptance is the real-data smoke (PR description), which these cases complement.
Nothing here names a disease; the generation interval and ranges are synthetic.
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from toolkit.calibration import history_matching as HM  # noqa: E402

GI = [0.2, 0.4, 0.3, 0.1]


def _engine():
    from toolkit.engines.renewal.engine import RenewalEngine
    return RenewalEngine(generation_interval=GI, components={"vc": {"start": 0.3, "stop": 0.7}}, results_path=None)


def _horizon():
    from toolkit.engines.contract import Horizon
    return Horizon(0.0, 1.0, (0.0, 1.0), {"all": [0.0, 1.0], "pre": [0.0, 0.3], "trial": [0.3, 0.35]})


def _sim_factory(eng, attrs):
    from toolkit.engines.contract import Package, UnitSpec
    h = _horizon()

    def run_one(theta, a, comps):
        o = eng.run(theta, UnitSpec("u", a), Package("p", comps), h, 0)
        w = o.windows["all"]
        return {"attack": float(w["attack_rate"]), "peak": float(w["peak_time"]),
                "pre": float(o.windows["pre"]["attack_rate"]), "reff": float(o.windows["trial"]["reff_start"])}

    def sim(pts):
        out = []
        for p in pts:
            th = dict(p["theta"])
            if p["kind"] == "unit":
                a = {k: v for k, v in attrs[p["unit"]].items() if not k.startswith("_")}
                a["s0"] = float(p["z"])
                o = run_one(th, a, {"vc": 1} if attrs[p["unit"]].get("_vc") else {})
            elif "log10_seed" in th:          # the canary: one population, shared values only
                a = {"N": 1e6, "seed": float(10 ** th.pop("log10_seed")), "s0": 1.0}
                o = run_one(th, a, {})
            else:                             # a trial: R_t with / without the component as it starts (reference population)
                a = {"N": 1e6, "seed": 1e-6, "s0": 1.0}
                o = {"rr": run_one(th, a, {"vc": 1})["reff"] / run_one(th, a, {})["reff"]}
            out.append(o)
        return out
    return sim


def _units_problem(unreachable=None):
    eng = _engine()
    truth = {"r0": 2.2, "effect:vc": 0.3}
    units = [f"p{i}" for i in range(10)]
    attrs = {u: {"N": 2e5 + 5e4 * i, "seed": 1e-3, "_vc": True} for i, u in enumerate(units)}
    s_true = {u: 0.6 + 0.035 * i for i, u in enumerate(units)}
    sim = _sim_factory(eng, attrs)
    obs = sim([{"kind": "unit", "unit": u, "z": s_true[u], "theta": truth, "seed": 0} for u in units])
    tg = []
    for u, o in zip(units, obs):
        v = 0.9995 if u == unreachable else o["attack"]
        sd = 0.15 if u == unreachable else HM.sd_from_n(v, 300, "logit")
        tg.append(HM.Target(f"{u}:sero", "unit", "attack", v, "logit", sd_obs=sd, sd_md=0.05, unit=u,
                            source="synthetic serosurvey, n = 300"))
        tg.append(HM.Target(f"{u}:pre", "unit", "pre", o["pre"], "log", sd_obs=0.1, sd_md=0.05, unit=u,
                            source="synthetic case series: cumulative incidence before the intervention (declared sd)"))
    rr = sim([{"kind": "shared", "theta": truth, "seed": 0}])[0]["rr"]
    tg.append(HM.Target("trial_rr", "shared", "rr", rr, "log", sd_obs=0.04, sd_md=0.02,
                        source="synthetic trial: reproduction-number ratio with / without the component"))
    P = HM.Problem([HM.Param("r0", 1.3, 3.5, "uniform", None, "synthetic range"),
                    HM.Param("effect:vc", 0.0, 0.6, "triangular", 0.25, "synthetic range")],
                   {"name": "s0", "lo": 0.3, "hi": 1.0}, units, tg)
    return P, sim, truth, s_true


def _canary(second="pre"):
    """One population, shared R0 and seed. `second`: the second target, "pre" (cumulative incidence before the
    intervention window: smooth in both parameters) or "peak" (the peak week: a step function on the weekly grid,
    which a smooth emulator cannot follow within the stated error)."""
    eng = _engine()
    truth = {"r0": 1.8, "log10_seed": -4.5}
    sim = _sim_factory(eng, {})
    o = sim([{"kind": "shared", "theta": truth, "seed": 0}])[0]
    P = HM.Problem([HM.Param("r0", 1.2, 3.0, "uniform", None, "synthetic range"),
                    HM.Param("log10_seed", -6.0, -3.0, "uniform", None, "synthetic range")], None, [],
                   [HM.Target("attack", "shared", "attack", o["attack"], "logit",
                              sd_obs=HM.sd_from_n(o["attack"], 500, "logit"), sd_md=0.03, source="synthetic survey"),
                    (HM.Target("pre", "shared", "pre", o["pre"], "log", sd_obs=0.05, source="synthetic series")
                     if second == "pre" else
                     HM.Target("peak", "shared", "peak", o["peak"], "log", sd_obs=0.04, source="synthetic series"))])
    return P, sim, truth


def _self_test() -> int:
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)
    q = lambda *a, **k: None  # noqa: E731
    P, sim, truth, s_true = _units_problem()
    r = HM.calibrate(P, sim, seed=1, waves=2, log=q, candidates_log2=10, points_per_unit=16, shared_points=24)
    rg = r["region"]["ranges"]
    ok(r["status"] == "matched" and r["n_matched"] == 10, f"AC1a renewal engine: every population matched ({r['blocks']})")
    ok(all(rg[k][0] <= v <= rg[k][1] for k, v in truth.items()) and r["region"]["contraction"]["effect:vc"] > 0.3,
       f"AC1b the planted shared values are inside the region and the effect contracts ({rg}, {r['region']['contraction']})")
    ok(all(r["units"][u]["z_interval"][0] - 0.03 <= s <= r["units"][u]["z_interval"][1] + 0.03
           for u, s in s_true.items()), "AC1c each population's planted susceptible share is inside its interval")
    P3, sim3, _, _ = _units_problem()
    P3.targets = [t for t in P3.targets if t.kind != "shared"]
    r3 = HM.calibrate(P3, sim3, seed=1, waves=1, log=q, candidates_log2=10, points_per_unit=10)
    ok(r3["region"]["contraction"]["effect:vc"] < r["region"]["contraction"]["effect:vc"],
       f"AC1d without the trial target the effect stays wide (contraction {r3['region']['contraction']['effect:vc']} "
       f"vs {r['region']['contraction']['effect:vc']} with it)")
    P2, sim2, _, _ = _units_problem(unreachable="p3")
    r2 = HM.calibrate(P2, sim2, seed=1, waves=1, log=q, candidates_log2=10, points_per_unit=10)
    ok(r2["blocked"] and r2["unmatched"] == ["p3"] and r2["units"]["p3"]["direction"] in ("target_above_reach", "no_value_fits_every_target") and r2["units"]["p3"]["misses"],
       f"AC2 an attack rate no susceptible share reaches: unmatched, with the direction, blocked "
       f"({r2['unmatched']}, {r2['units']['p3']['direction']})")
    Pc, simc, tc = _canary()
    rc = HM.calibrate(Pc, simc, seed=2, waves=2, log=q, candidates_log2=11, shared_points=24)
    rgc = rc["region"]["ranges"]
    ok(rc["status"] == "matched" and all(rgc[k][0] <= v <= rgc[k][1] for k, v in tc.items())
       and rc["region"]["contraction"]["r0"] > 0.5,
       f"AC3 canary with no units (shared targets only): planted values inside a contracted region ({rgc}; "
       f"{rc['status']}, {rc['blocks']}, {[(w['wave'], {k: v['status'] for k, v in w['validation'].items()}) for w in rc['waves']]})")
    # CI 2026-11-06: with the peak week as the second target the emulator check fails (the peak week is a step
    # function of the parameters on the engine's weekly grid). That is the check working: it fails closed.
    Pp, simp, _ = _canary("peak")
    rp = HM.calibrate(Pp, simp, seed=2, waves=2, log=q, candidates_log2=11, shared_points=24)
    ok(rp["blocked"] and any(b["code"] == "emulator_inadequate" for b in rp["blocks"]),
       f"AC4 a step-valued target (the peak week on a weekly grid) fails the emulator check and blocks "
       f"({[b['code'] for b in rp['blocks']]})")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    print(__doc__)
