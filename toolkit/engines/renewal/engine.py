#!/usr/bin/env python3
"""Renewal engine behind the Engine contract: a discrete generation-interval transmission model (Fraser 2007;
Cori et al. 2013) with susceptible depletion, for one closed population.

    python3 toolkit/engines/renewal/engine.py --self-test
    python3 toolkit/engines/renewal/engine.py --card [path]   # writes card.yaml (after the pack is run)

Our own Python code (numpy); no external simulator. One step has the length of the generation interval's grid
(`dt`: one week or one day). With infections I_t as shares of the population N, susceptibles S_t, the declared
generation interval w_1..w_L (lags of 1..L steps, summing to 1) and a transmissibility profile c(t):

    lambda_t = R0 c(t) sum_s w_s I_{t-s},     I_t = S_{t-1} [1 - exp(-lambda_t)],     S_t = S_{t-1} - I_t

This is the bounded exponential-survival form of the renewal equation: it never infects more than the remaining
susceptibles, and for constant c it satisfies the final-size relation log(S_inf / S_0) = -R0 (seed + sum I) exactly
at any step length. In the growth phase it is the linear renewal equation I_t = R sum_s w_s I_{t-s}, so the growth
rate r and R obey R = 1 / sum_s w_s exp(-r s dt) (Wallinga & Lipsitch 2007). The instantaneous reproduction number
is R_t = R0 c(t) S_{t-1}.

The transmissibility profile is c(t) = m_k(t) x prod_c (1 - effect_c [component c active at t]):
  transmissibility  constant (c = 1) | {knots: [t_1, ...]}: pieces [start, t_1), [t_1, t_2), ...; piece 0 is the
                    reference (m_0 = 1, so R0 is the reproduction number of the first piece) and each later piece has
                    a calibrated multiplier `transmissibility:<k>` (time-varying Rt)
  components        {name: {start, stop}}: an intervention multiplying Rt by (1 - effect:<name>) over [start, stop)
                    (decimal years) when the package has it; `effect:<name>` is a scenario parameter

Configuration (constructor keywords; anything else is refused): generation_interval ([w_1..w_L] or {dt, w}),
dt (week | day), transmissibility, components, results_path.

Unit attributes: N (population, > 0), s0 (susceptible share at the start, before the seed, in [0, 1]; default 1),
seed (infections in the step before the horizon starts, as a share of N; default 1e-6). The seed's infections
come out of s0.

Outputs. Per capita: infections over the decision window. Window metrics, for each named window [a, b) (the steps
whose start lies in it): attack_rate (infections in the window / N: new infections only), s_start, s_end,
reff_start (R0 c S at the window's first step), reff_end (R0 c(t_end) s_end, c of the window's last step),
reff_ref (R0 m_0 s_end: the window's end with the reference transmissibility, i.e. "the same transmissibility as at
the start", no component), growth_rate (least-squares slope of log incidence per year), peak_time, r0. Meta: the
incidence and R_t series. Hooks (toolkit/engines/hooks.py): schedule() for the components, run_series() for monthly
infections, R_t and the susceptible share. `cori_rt()` is the Cori et al. (2013) estimator of R_t from a case
series: the time-varying estimate the pack checks against known R_t, and a reporting-invariant diagnostic.

The observation model (reporting fraction, reporting delay, overdispersion) is not part of the engine: each data
source has its own measurement model in the harness (toolkit/analysis/observation.py, toolkit/analysis/estimates.py),
and no source is taken as the truth. Nothing in this file names a disease.
"""
from __future__ import annotations

import json
import math
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from toolkit.engines.contract import (EngineCard, Horizon, Outcomes, Package, ParamSpec, UnitSpec,  # noqa: E402
                                      check_engine, resolve_params)
from toolkit.engines.hooks import HOOKS_CONTRACT, monthly_grid  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PACK = os.path.join(HERE, "benchmarks", "pack.yaml")
RESULTS = os.path.join(HERE, "benchmarks", "results.yaml")
NAME, VERSION = "renewal", "0.1"
DT_YEARS = {"week": 7.0 / 365.25, "day": 1.0 / 365.25}
DEFAULTS = {"s0": 1.0, "seed": 1.0e-6}
R0_RANGE = (0.05, 40.0)
MULT_RANGE = (0.02, 20.0)
PUBLISHED_LABEL = ("validated against analytic identities (final size, Wallinga-Lipsitch, reproduction-number "
                   "identities, scale invariance) and known-R_t recovery; published benchmark RW9 (Cori et al. 2013 "
                   "worked examples) not yet assessed")
WINDOW_METRICS = ("attack_rate", "s_start", "s_end", "reff_start", "reff_end", "reff_ref", "growth_rate",
                  "peak_time", "r0")
SERIES = {"infections": {"label": "new infections", "units": "infections per person-year", "aggregate": "rate",
                         "denominator": "person-year"},
          "reff": {"label": "instantaneous reproduction number R_t", "units": "infections per infection",
                   "aggregate": "mean", "denominator": "person"},
          "susceptible": {"label": "susceptible share", "units": "share", "aggregate": "mean",
                          "denominator": "person"}}


def check_generation_interval(gi, dt):
    raise NotImplementedError("renewal engine: tests first (alpha); implementation in beta")


def simulate(r0, cmat, s0, seed, w):
    raise NotImplementedError("renewal engine: tests first (alpha); implementation in beta")


def cori_rt(cases, w, tau=3, a=1.0, b=5.0, level=0.95, start=None):
    raise NotImplementedError("renewal engine: tests first (alpha); implementation in beta")


class RenewalEngine:
    hooks_contract = HOOKS_CONTRACT

    def __init__(self, **kw):
        raise NotImplementedError("renewal engine: tests first (alpha); implementation in beta")


FAST_PACK_IDS = ("RW1a", "RW2a", "RW3a", "RW5a")


def _self_test() -> int:
    from toolkit.engines.hooks import run_series as hook_series, schedule as hook_schedule
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

    w = [0.2, 0.4, 0.25, 0.15]
    e, err = safe(lambda: RenewalEngine(generation_interval=w))
    ok(e is not None, f"K0 the engine builds from a weekly generation interval: {err}")
    if e is None:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    for label, gi in (("sums to 0.9", [0.5, 0.4]), ("negative", [1.2, -0.2]), ("gap", [0.5, None, 0.5]),
                      ("another grid", {"dt": "day", "w": [1.0]})):
        _, err = safe(lambda gi=gi: RenewalEngine(generation_interval=gi))
        ok(err is not None, f"K0b a generation interval that {label} fails closed: {err}")
    _, err = safe(lambda: RenewalEngine(generation_interval=w, colour="blue"))
    ok(err is not None, "K0c an unknown configuration keyword is refused")
    h = Horizon(2025.0, 2026.0, (2025.0, 2026.0), {"y": [2025.0, 2026.0], "early": [2025.0, 2025.5],
                                                     "late": [2025.5, 2026.0]})
    u = UnitSpec("city", {"N": 1.5e6, "s0": 1.0, "seed": 2e-4})
    ok(check_engine(e, u, Package("none", {}), h) == [], f"K1 contract: {check_engine(e, u, Package('none', {}), h)}")
    ok(e.n_steps(h) == 52, f"K1b a one-year horizon on the weekly grid has 52 steps ({e.n_steps(h)})")
    # K2 run_batch equals run pair by pair; K3 identical across seeds
    ps = [{"r0": 1.4}, {"r0": 2.2}, {"r0": 0.8}]
    us = [u, UnitSpec("b", {"N": 1e5, "s0": 0.6, "seed": 1e-3}), u]
    b = e.run_batch(ps, us, [Package("none", {})] * 3, h, 1)
    one = [e.run(p, x, Package("none", {}), h, 1) for p, x in zip(ps, us)]
    ok(all(bb.windows == oo.windows and bb.per_capita == oo.per_capita for bb, oo in zip(b, one)),
       "K2 run_batch() equals run() member by member")
    ok(e.run(ps[0], u, Package("none", {}), h, 1).windows == e.run(ps[0], u, Package("none", {}), h, 99).windows,
       "K3 outputs are identical across seeds (deterministic)")
    # K4 final size (exponential-survival identity) at any step length, with s0 < 1
    from scipy.optimize import brentq
    hl = Horizon(0.0, 30.0, (0.0, 30.0), {"all": [0.0, 30.0]})
    worst = 0.0
    for r0, s0 in ((1.5, 1.0), (2.0, 1.0), (4.0, 1.0), (2.5, 0.7)):
        for dt, gi in (("week", w), ("day", {"dt": "day", "w": [0.1, 0.3, 0.3, 0.2, 0.1]})):
            ee = RenewalEngine(generation_interval=gi, dt=dt)
            o = ee.run({"r0": r0}, UnitSpec("x", {"N": 1e6, "s0": s0, "seed": 1e-9}), Package("none", {}), hl, 1)
            sst = s0 - 1e-9
            sinf = brentq(lambda x: x - sst * math.exp(-r0 * (1e-9 + sst - x)), 0.0, sst)
            worst = max(worst, abs(o.windows["all"]["s_end"] - sinf))
    ok(worst < 1e-8, f"K4 the final-size relation holds at weekly and daily steps, s0 = 1 and 0.7 (worst {worst:.2e})")
    # K5 Wallinga-Lipsitch
    hg = Horizon(0.0, 2.0, (0.0, 2.0), {"g": [0.3, 0.6]})
    worst = 0.0
    for r0 in (0.8, 1.3, 2.5):
        o = e.run({"r0": r0}, UnitSpec("x", {"N": 1.0, "s0": 1.0, "seed": 1e-12}), Package("none", {}), hg, 1)
        r = o.windows["g"]["growth_rate"] * e.dt_years                 # per step
        rw = 1.0 / sum(wk * math.exp(-r * (k + 1)) for k, wk in enumerate(w))
        worst = max(worst, abs(rw / r0 - 1))
    ok(worst < 1e-3, f"K5 R0 = 1 / sum_s w_s exp(-r s) in the growth phase, R0 0.8, 1.3, 2.5 (worst rel {worst:.1e})")
    # K6 reproduction-number identities
    o = e.run({"r0": 1.7}, u, Package("none", {}), h, 1).windows["y"]
    ok(abs(o["reff_end"] - 1.7 * o["s_end"]) < 1e-12 and o["reff_ref"] == o["reff_end"] and o["reff_end"] < 1,
       f"K6 reff_end = R0 S_end/N (unforced), reff_ref equals it, below 1 after a major epidemic ({o['reff_end']:.3f})")
    # K7 scale invariance: (R0, s0, N) and (R0 s0, 1, s0 N) give the same counts
    a1 = e.run({"r0": 3.0}, UnitSpec("x", {"N": 1e6, "s0": 0.5, "seed": 1e-4}), Package("none", {}), h, 1)
    a2 = e.run({"r0": 1.5}, UnitSpec("x", {"N": 5e5, "s0": 1.0, "seed": 2e-4}), Package("none", {}), h, 1)
    c1 = np.asarray(a1.meta["incidence"]) * 1e6
    c2 = np.asarray(a2.meta["incidence"]) * 5e5
    ok(np.max(np.abs(c1 / c2 - 1)) < 1e-10 and abs(a1.windows["y"]["reff_end"] - a2.windows["y"]["reff_end"]) < 1e-12
       and abs(a1.windows["y"]["attack_rate"] / a2.windows["y"]["attack_rate"] - 0.5) < 1e-10,
       "K7 scale invariance: with s0 unknown the counts fix R0 s0 and reff_end, not the attack rate")
    # K8 piecewise transmissibility and a component
    ek = RenewalEngine(generation_interval=w, transmissibility={"knots": [2025.5]},
                       components={"ctl": {"start": 2025.25, "stop": 2025.5}})
    tt = ek.step_starts(h)
    o = ek.run({"r0": 1.5, "transmissibility:1": 0.5, "effect:ctl": 0.4}, u, Package("c", {"ctl": 1}), h, 1)
    c = np.asarray(o.meta["rt"]) / (1.5 * np.concatenate([[1.0 - 2e-4], np.asarray(o.meta["s"])[:-1]]))
    want = np.where(tt >= 2025.5, 0.5, 1.0) * np.where((tt >= 2025.25) & (tt < 2025.5), 0.6, 1.0)
    ok(np.max(np.abs(c - want)) < 1e-12, "K8 R_t follows the piecewise multiplier and the component's (1 - effect)")
    ok(abs(o.windows["y"]["reff_ref"] - 1.5 * o.windows["y"]["s_end"]) < 1e-12,
       "K8b reff_ref uses the reference transmissibility (piece 0, no component)")
    # K9 attack_rate counts window infections only
    oy = e.run({"r0": 1.7}, u, Package("none", {}), h, 1).windows
    ok(abs(oy["late"]["attack_rate"] - (oy["y"]["attack_rate"] - oy["early"]["attack_rate"])) < 1e-12
       and abs(oy["late"]["s_start"] - oy["early"]["s_end"]) < 1e-15,
       "K9 a window's attack rate counts only infections in it")
    # K10 Cori recovers a known R_t; reporting fraction cancels
    he = Horizon(0.0, 40 * e.dt_years, (0.0, 40 * e.dt_years), {})
    eh = RenewalEngine(generation_interval=w, transmissibility={"knots": [20 * e.dt_years - 1e-9]})
    oo = eh.run({"r0": 1.6, "transmissibility:1": 0.5}, UnitSpec("x", {"N": 1e9, "s0": 1.0, "seed": 1e-6}),
                Package("none", {}), he, 1)
    inf = np.asarray(oo.meta["incidence"]) * 1e9
    rt = np.asarray(oo.meta["rt"])
    cr = cori_rt(inf, w, tau=3)
    cr10 = cori_rt(0.1 * inf, w, tau=3)
    errs = [abs(m / rt[t] - 1) for t, m in zip(cr["t"], cr["mean"]) if t >= 8 and not 19 <= t <= 22]
    inv = max(abs(x / y - 1) for x, y in zip(cr["mean"][4:], cr10["mean"][4:]))
    ok(max(errs) < 0.02 and inv < 2e-3,
       f"K10 Cori recovers the known R_t of a simulated series (worst rel {max(errs):.3f}) and, up to its gamma prior, "
       f"does not depend on a constant reporting fraction (worst rel {inv:.1e} at 1/10 reporting)")
    # K11 hooks
    hd = Horizon(2025.0, 2026.0, (2025.0, 2026.0), {})
    sc = hook_schedule(ek, {"effect:ctl": 0.4}, u, Package("c", {"ctl": 1}), hd)
    ok(len(sc) == 1 and sc[0]["kind"] == "decision", f"K11a schedule(): one decision event per component ({sc})")
    sr = hook_series(e, {"r0": 1.7}, u, [Package("none", {})], hd, 1, ["infections", "reff", "susceptible"])
    tot = sum(v / 12 for v in sr[0]["values"]["infections"])
    ok(len(sr[0]["t"]) == 12 and abs(tot - oy["y"]["attack_rate"]) < 1e-9,
       f"K11b run_series(): 12 monthly steps; monthly infections integrate to the attack rate ({tot:.6f})")
    # K12 fail closed
    for label, fn in (("an unknown parameter", lambda: e.run({"zz": 1}, u, Package("none", {}), h, 1)),
                      ("s0 above 1", lambda: e.run({}, UnitSpec("x", {"N": 1e5, "s0": 1.2}), Package("none", {}), h, 1)),
                      ("no N", lambda: e.run({}, UnitSpec("x", {"s0": 1.0}), Package("none", {}), h, 1)),
                      ("r0 outside its range", lambda: e.run({"r0": 99.0}, u, Package("none", {}), h, 1))):
        _, err = safe(fn)
        ok(err is not None, f"K12 {label} fails closed: {err}")
    # K13 the committed benchmark pack: a fast subset re-run, and results stamped for this code
    import yaml
    from toolkit.engines import benchmark as B
    if os.path.exists(PACK):
        pack = yaml.safe_load(open(PACK))
        eng0 = B.load_engine(pack["engine"], pack["configs"][pack["default_config"]])
        ok(B.validate(pack, eng0) == [], f"K13a the pack validates: {B.validate(pack, eng0)[:3]}")
        sub = dict(pack, benchmarks=[x for x in pack["benchmarks"] if x["id"] in FAST_PACK_IDS])
        rows = {r["id"]: r for r in B.run_pack(sub, eng0, B.Runner(sub, eng0, 1, seeds=[1]), {})["benchmarks"]}
        ok(all(rows.get(i, {}).get("status") == "in_band" for i in FAST_PACK_IDS),
           "K13b fast pack subset in band: " + ", ".join(f"{i} {rows.get(i, {}).get('status')}" for i in FAST_PACK_IDS))
        eng = B.load_engine(pack["engine"], dict(pack["configs"][pack["default_config"]], results_path=RESULTS))
        got, why = B.verified_benchmarks(eng)
        ok(why is None and len(got) == len(pack["benchmarks"]), f"K13c results.yaml is stamped for this code: {why}")
        bad = [r["id"] for r in got if r["status"] != "in_band" and r["id"] != "RW9"]
        ok(got and not bad, f"K13d every assessable benchmark in band ({bad}); RW9 not_assessable until transcribed")
    else:
        ok(False, "K13 the benchmark pack exists")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    if sys.argv[1:2] == ["--card"]:
        write_card(sys.argv[2] if len(sys.argv) > 2 else os.path.join(HERE, "card.yaml"))
        sys.exit(0)
    print(__doc__)
