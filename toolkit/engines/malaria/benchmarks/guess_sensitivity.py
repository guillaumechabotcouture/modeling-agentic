#!/usr/bin/env python3
"""How much the burn-in's starting guess moves decision outputs (v0.5 review question).

    python3 toolkit/engines/malaria/benchmarks/guess_sensitivity.py [--workers N] [--burnin B] [--out PATH]

The equilibration evidence found adult IC (30+) differing by 6-9% between starting guesses x0.25 and x4 after a
50-year burn-in. This script measures what that does to decision outputs, at m0 3.5, 9 and 28 (the levels that
missed), 6 seeds, guesses x0.25, x1 (the card's) and x4: microscopy PfPR 6-59 months in the calibration window
(2021.67-2022.0), all-age clinical cases and treated_all over 2024-2027 (package none), and all-age DALYs averted
by PBO nets + enhanced case management vs none. Same synthetic unit as equilibration.py. Writes
guess_sensitivity.yaml beside this file.
"""
from __future__ import annotations

import argparse
import os
import sys
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(HERE)))))
from toolkit.engines.malaria.benchmarks.equilibration import UNIT  # noqa: E402

LEVELS = [3.5, 9.0, 28.0]
SEEDS = [1, 2, 3, 4, 5, 6]
GUESS = {"x0.25": 0.25, "x1": 1.0, "x4": 4.0}
PACK = {"net": "pbo", "cm": 1}


def _allage(o, k):
    return sum(o.per_capita[k][g] * w for g, w in o.meta["age_shares"].items())


def _job(args):
    m0, g, seed, burnin = args
    from toolkit.engines.malaria.engine import MalariaEngine
    from toolkit.engines.contract import UnitSpec, Package, Horizon
    e = MalariaEngine()
    p = {"burnin_years": burnin, "init_eir_per_m0": 0.6 * GUESS[g]}
    u = UnitSpec("gs", dict(UNIT, m0=m0, itn_hist_years=[2017.5, 2020.5], campaign_year=2024.0))
    h = Horizon(2016.0, 2027.0, (2024.0, 2027.0), {"cal": [2021.67, 2022.0]})
    none, pk = e.run_batch([p, p], u, [Package("none", {}), Package("pbo_cm", PACK)], h, seed)
    return {"m0": m0, "guess": g, "seed": seed, "pfpr_u5_cal": none.windows["cal"]["pfpr_u5"],
            "cases_all": _allage(none, "cases"), "treated_all": none.cost_drivers["treated_all"],
            "dalys_averted": _allage(none, "dalys") - _allage(pk, "dalys")}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 1)
    ap.add_argument("--burnin", type=int, default=50)
    ap.add_argument("--out", default=os.path.join(HERE, "guess_sensitivity.yaml"))
    a = ap.parse_args(argv)
    jobs = [(m0, g, s, a.burnin) for m0 in LEVELS for g in GUESS for s in SEEDS]
    with ProcessPoolExecutor(max(1, min(a.workers, len(jobs)))) as ex:
        rows = list(ex.map(_job, jobs, chunksize=1))
    keys = ("pfpr_u5_cal", "cases_all", "treated_all", "dalys_averted")
    levels = []
    for m0 in LEVELS:
        lv = {"m0": m0, "mean": {}, "seed_sd": {}, "rel_x4_vs_x025": {}, "rel_x025_vs_x1": {}, "rel_x4_vs_x1": {},
              "paired_se_x4_vs_x025": {}}
        for k in keys:
            v = {g: np.array([r[k] for r in rows if r["m0"] == m0 and r["guess"] == g]) for g in GUESS}
            lv["mean"][k] = {g: round(float(x.mean()), 5) for g, x in v.items()}
            lv["seed_sd"][k] = round(float(v["x1"].std(ddof=1)), 5)
            lv["rel_x4_vs_x025"][k] = round(float(v["x4"].mean() / v["x0.25"].mean() - 1), 4)
            lv["rel_x025_vs_x1"][k] = round(float(v["x0.25"].mean() / v["x1"].mean() - 1), 4)
            lv["rel_x4_vs_x1"][k] = round(float(v["x4"].mean() / v["x1"].mean() - 1), 4)
            d = v["x4"] / v["x0.25"] - 1                                   # same seed: paired
            lv["paired_se_x4_vs_x025"][k] = round(float(d.std(ddof=1) / np.sqrt(len(d))), 4)
        levels.append(lv)
    doc = {"producer": "toolkit/engines/malaria/benchmarks/guess_sensitivity.py", "burnin_years": a.burnin,
           "seeds": SEEDS, "package": PACK, "levels": levels, "rows": rows}
    yaml.safe_dump(doc, open(a.out, "w"), sort_keys=False)
    for lv in levels:
        print(lv["m0"], lv["rel_x4_vs_x025"], lv["paired_se_x4_vs_x025"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
