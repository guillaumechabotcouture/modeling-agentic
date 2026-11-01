#!/usr/bin/env python3
"""Compartmental engine (Starsim) behind the Engine contract: design note v2d §1.1.

    python3 toolkit/engines/compartmental/engine.py --self-test

(Test-first commit: the self-test below is written before the module.)
"""
from __future__ import annotations

import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from toolkit.engines.contract import Horizon, Package, UnitSpec, check_engine  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PACK = os.path.join(HERE, "benchmarks", "pack.yaml")
RESULTS = os.path.join(HERE, "benchmarks", "results.yaml")


class CompartmentalEngine:  # replaced by the implementation
    def __init__(self, **config):
        raise NotImplementedError("compartmental engine: not yet implemented")


FAST_PACK_IDS = ("FS1b", "FS4a", "BR1a", "CI1a", "CI4a", "RN2a", "RN2c", "OB1a")


def _self_test() -> int:
    import numpy as np
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

    single = dict(groups=["all"], structure="single_group", mode="to_extinction", initialiser="given", dt="day",
                  generation_interval=[0.2, 0.3, 0.3, 0.2], components={"vax": {"kind": "campaign", "targets": ["all"],
                                                                              "reach": 0.5}})
    hx = Horizon(0, 200000, (0, 200000), {"all": [0, 200000]})
    u1 = UnitSpec("u", {"N": [1e6], "s0": [1.0], "seed": 1e-9})
    e, err = safe(lambda: CompartmentalEngine(**single))
    ok(e is not None, f"K0 the engine builds from a configuration: {err}")
    if e is None:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    ok(check_engine(e, u1, Package("v", {"vax": 1}), hx) == [], f"K1a contract (to_extinction, given, campaign): "
       f"{check_engine(e, u1, Package('v', {'vax': 1}), hx)}")
    win = dict(single, mode="windowed", forcing="cosine", forcing_period=365.0)
    ew = CompartmentalEngine(**win)
    hw = Horizon(0, 300, (0, 300), {"all": [0, 300], "late": [100, 300]})
    ok(check_engine(ew, u1, Package("0", {}), hw) == [], "K1b contract (windowed, forced)")
    coh = dict(groups=["b2000", "b2010", "b2011"], structure="birth_cohorts", mode="to_extinction",
               initialiser="cohort_history", doses=["d1", "d2"], dose_dependence="independent", dt="day",
               generation_interval=[0.5, 0.5], components={"cu": {"kind": "campaign", "targets": ["b2011"], "reach": 0.9}})
    ec = CompartmentalEngine(**coh)
    uc = UnitSpec("c", {"N": [3e5, 1e5, 1e5], "birth_year": [2000, 2010, 2011], "decision_year": 2025,
                        "always_immune": ["b2000"], "contact_rate": 8.0, "seed": 1e-9, "intro": "b2011",
                        "coverage:d1": [0.0, 0.9, 0.8], "coverage:d2": [0.0, 0.6, 0.0]})
    ok(check_engine(ec, uc, Package("c", {"cu": 1}), hx) == [], "K1c contract (cohort_history, birth cohorts)")
    mg = dict(groups=["a", "b", "c"], structure="age_groups_contact_matrix", mode="to_extinction", initialiser="given",
              dt="day", transmission="q", generation_interval=[0.5, 0.3, 0.2],
              components={"blk": {"kind": "doses", "group": "b", "block_size": 1e4, "max_blocks": 5}})
    em = CompartmentalEngine(**mg)
    um = UnitSpec("m", {"N": [3e5, 2e5, 1e5], "s0": [1.0, 0.9, 0.8], "seed": 1e-9, "intro": "a",
                        "contact": [[2.0, 0.5, 0.2], [0.6, 1.5, 0.4], [0.3, 0.5, 1.0]]})
    ok(check_engine(em, um, Package("b", {"blk": 3}), hx) == [], "K1d contract (contact matrix, dose blocks)")

    # K2: the module draws no random numbers, so outputs are identical across seeds
    outs = [e.run({"r0": 2.5}, u1, Package("v", {"vax": 1}), hx, s) for s in (1, 7, 99991)]
    ok(all(o.per_capita == outs[0].per_capita and o.windows == outs[0].windows and o.cost_drivers == outs[0].cost_drivers
           for o in outs) and [o.meta["seed"] for o in outs] == [1, 7, 99991],
       "K2 outputs are identical across seeds; the seed is recorded")
    ow = [ew.run({"r0": 1.6, "forcing_amplitude": 0.2}, u1, Package("0", {}), hw, s) for s in (1, 5)]
    ok(ow[0].windows == ow[1].windows, "K2b identical across seeds in windowed mode too")

    # K3: run_batch() equals run() pair by pair (rel 1e-12)
    rng = np.random.default_rng(3)
    ps = [{"r0": float(r), "efficacy:vax": float(ef)} for r, ef in zip(rng.uniform(1.2, 6, 9), rng.uniform(0.5, 1, 9))]
    pk = [Package(f"p{i}", {"vax": int(i % 2)}) for i in range(9)]
    ob = e.run_batch(ps, u1, pk, hx, 1)

    def rel(a, b):
        return abs(a - b) / max(abs(b), 1e-300)
    worst = 0.0
    for p, k, o in zip(ps, pk, ob):
        r = e.run(p, u1, k, hx, 1)
        worst = max([worst, rel(o.windows["all"]["attack_rate"], r.windows["all"]["attack_rate"]),
                     rel(o.per_capita["infections"]["all"], r.per_capita["infections"]["all"]),
                     rel(o.cost_drivers["doses:vax"] + 1, r.cost_drivers["doses:vax"] + 1)])
    ok(len(ob) == 9 and worst < 1e-12, f"K3a run_batch() equals run() pair by pair, to_extinction (worst rel {worst:.1e})")
    psw = [{"r0": float(r), "forcing_amplitude": float(a), "forcing_phase": float(ph)}
           for r, a, ph in zip(rng.uniform(1.1, 3, 6), rng.uniform(0, 0.4, 6), rng.uniform(0, 365, 6))]
    obw = ew.run_batch(psw, u1, Package("0", {}), hw, 1)
    worst = max(rel(o.windows[w][m], ew.run(p, u1, Package("0", {}), hw, 1).windows[w][m])
                for p, o in zip(psw, obw) for w in ("all", "late") for m in ("attack_rate", "reff_end", "growth_rate"))
    ok(worst < 1e-12, f"K3b run_batch() equals run(), windowed with per-member forcing (worst rel {worst:.1e})")
    obm = em.run_batch([{"q": 0.9}, {"q": 1.3}], [um, UnitSpec("m2", dict(um.attributes, s0=[0.7, 0.7, 0.7]))],
                       [Package("0", {}), Package("b", {"blk": 2})], hx, 1)
    rm = [em.run({"q": 0.9}, um, Package("0", {}), hx, 1),
          em.run({"q": 1.3}, UnitSpec("m2", dict(um.attributes, s0=[0.7, 0.7, 0.7])), Package("b", {"blk": 2}), hx, 1)]
    ok(all(rel(a.per_capita["infections"][g], b.per_capita["infections"][g]) < 1e-12 for a, b in zip(obm, rm)
           for g in ("a", "b", "c")), "K3c run_batch() with one unit per pair (latent unit attributes vary)")

    # K4: the generation interval fails closed
    for bad, why in (([0.5, 0.4], "sum"), ([0.6, -0.1, 0.5], "negative"), ([0.5, None, 0.5], "gap"),
                     ([0.5, float("nan"), 0.5], "gap"), ({"dt": "week", "w": [1.0]}, "grid")):
        _, err = safe(lambda: CompartmentalEngine(**dict(single, generation_interval=bad)))
        ok(err is not None and "ValueError" in err, f"K4 a generation interval with a {why} fails closed: {err}")
    _, err = safe(lambda: CompartmentalEngine(**dict(single, generation_interval={"dt": "day", "w": [0.5, 0.5]})))
    ok(err is None, "K4b a generation interval on the engine's own grid is accepted")

    # K5: the step cap fails closed
    capped = CompartmentalEngine(**dict(single, step_cap=30, segment=16))
    _, err = safe(lambda: capped.run({"r0": 2.0}, u1, Package("0", {}), hx, 1))
    ok(err is not None and "converged: false" in err, f"K5 an epidemic still running at the step cap fails closed: {err}")

    # K6: unknown configuration fails closed
    for bad in ({"mode": "both"}, {"structure": "network"}, {"initialiser": "survey"}, {"no_such_field": 1},
                {"importation": "maybe"}, {"offspring": "negbin"}, {"dt": "month"}):
        _, err = safe(lambda: CompartmentalEngine(**dict(single, **bad)))
        ok(err is not None, f"K6 configuration {bad} is refused: {err}")
    _, err = safe(lambda: e.run({}, UnitSpec("x", {"N": [1e6], "s0": [1.2], "seed": 1e-9}), Package("0", {}), hx, 1))
    ok(err is not None, "K6b a susceptible share outside [0, 1] is refused")

    # K7: homogeneous mixing across unequal cohorts is population-weighted, not a matrix of ones
    C = np.asarray(ec.contact_matrix(uc))
    Nc = np.asarray(uc.attributes["N"])
    ok(np.allclose(C, 8.0 * np.tile(Nc / Nc.sum(), (3, 1)), rtol=0, atol=1e-15)
       and abs(ec.run({"r0": 3.0}, uc, Package("0", {}), hx, 1).windows["all"]["r0"] - 3.0) < 1e-12,
       "K7 birth cohorts mix with C_ij = c N_j / N; R0 = q rho(C)")

    # K8: Starsim structure
    import starsim as ss
    sim = e.build_sim({"r0": 2.0}, u1, Package("v", {"vax": 1}), hx, 3)[0]
    mods = [m for m in sim.pars.diseases] if isinstance(sim.pars.diseases, list) else [sim.pars.diseases]
    itvs = sim.pars.interventions if isinstance(sim.pars.interventions, list) else [sim.pars.interventions]
    ok(isinstance(sim, ss.Sim) and any(isinstance(m, ss.Module) and type(m).__name__ == "GroupEpidemic" for m in mods)
       and all(isinstance(i, ss.Intervention) for i in itvs) and len(itvs) == 1,
       "K8 run() builds an ss.Sim with the GroupEpidemic module and start-of-sim ss.Intervention components")
    o = e.run({"r0": 2.0}, u1, Package("v", {"vax": 1}), hx, 1)
    ok(o.meta.get("p_major_derived") is True and any("p_major" in str(x) and "derived" in str(x)
                                                     for x in e.describe().notes + e.describe().mechanisms),
       "K9 p_major is labelled derived (an equation, not the sim) on the card and in the outcomes")

    # K10: dose blocks and targets_susceptible
    st = em.initial_state({"q": 1.0, "efficacy:blk": 1.0}, um, Package("b", {"blk": 3}))
    ok(abs(st["s_start"][1] - 0.9 * (1 - 3e4 / 2e5)) < 1e-12 and abs(st["doses"]["blk"] - 3e4) < 1e-9,
       "K10a dose blocks: coverage = blocks x block / N_g; doses given to immune people are wasted")
    emt = CompartmentalEngine(**dict(mg, targets_susceptible=True))
    st2 = emt.initial_state({"q": 1.0, "efficacy:blk": 1.0}, um, Package("b", {"blk": 3}))
    ok(abs(st2["s_start"][1] - (0.9 - 3e4 / 2e5)) < 1e-12, "K10b targets_susceptible: every dose reaches a susceptible")

    # K11: fast analytic subset of the pack, through the runner
    import yaml
    from toolkit.engines import benchmark as B
    pack = yaml.safe_load(open(PACK))
    sub = dict(pack, benchmarks=[b for b in pack["benchmarks"] if b["id"] in FAST_PACK_IDS])
    eng0 = B.load_engine(pack["engine"], pack["configs"][pack.get("default_config") or "single_day"])
    ok(B.validate(pack, eng0) == [], f"K11a the pack validates: {B.validate(pack, eng0)[:3]}")
    rows = {r["id"]: r for r in B.run_pack(sub, eng0, B.Runner(sub, eng0, 1, seeds=[1]), {})["benchmarks"]}
    ok(all(rows[i]["status"] == "in_band" for i in FAST_PACK_IDS),
       "K11b fast pack subset in band: " + ", ".join(f"{i} {rows[i]['status']}" for i in FAST_PACK_IDS))

    # K12: the committed results were computed by the runner for this code, and every assessable benchmark is in band
    eng = B.load_engine(pack["engine"], dict(pack["configs"][pack["default_config"]], results_path=RESULTS))
    got, why = B.verified_benchmarks(eng)
    ok(why is None and len(got) == len(pack["benchmarks"]), f"K12a results.yaml is stamped for this engine code: {why}")
    bad = [r["id"] for r in got if r["status"] != "in_band" and r["id"] not in ("FS7", "RN5")]
    na = {r["id"]: r["status"] for r in got if r["id"] in ("FS7", "RN5")}
    ok(got and not bad and na == {"FS7": "not_assessable", "RN5": "not_assessable"},
       f"K12b every assessable benchmark in band ({bad}); FS7 and RN5 not_assessable until transcribed ({na})")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    print(__doc__)
