#!/usr/bin/env python3
"""Per-run cost of the v0.5 start (burn-in shared by packages) against the v0.4 start (no burn-in).

    python3 toolkit/engines/malaria/benchmarks/runtime.py [--workers N] [--out PATH]

A decision-like point: the synthetic Sahel/savanna unit of equilibration.py, 4,000 agents, horizon 2016-2027,
decision window 2024-2027, past campaigns 2017.5 and 2020.5, four packages (none, pyrethroid nets, dual-AI nets
+ SMC, enhanced case management). Per point (m0 x seed), each in one process on one core:
  v04           four run() calls with burnin_years 0 (the v0.4 computation: no burn-in, one run per package)
  v05_shared    one run_batch() call (one burn-in, packages forked at their first component event)
  v05_unshared  four run() calls at the card's burn-in (what a caller that does not batch pays)
Writes runtime.yaml beside this file.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(HERE)))))
from toolkit.engines.malaria.benchmarks.equilibration import UNIT  # noqa: E402

PACKAGES = {"none": {}, "pyr": {"net": "pyr"}, "dual_smc": {"net": "dual", "smc": 1}, "cm": {"cm": 1}}
POINTS = [(m0, s) for m0 in (9.0, 28.0) for s in (1, 2, 3)]
HORIZON = {"start": 2016.0, "stop": 2027.0, "window": [2024.0, 2027.0], "windows": {"cal": [2021.67, 2022.0]}}


def _point(args):
    m0, seed = args
    from toolkit.engines.malaria.engine import MalariaEngine
    from toolkit.engines.contract import UnitSpec, Package, Horizon
    e = MalariaEngine()
    u = UnitSpec("rt", dict(UNIT, m0=m0, itn_hist_years=[2017.5, 2020.5], campaign_year=2024.0))
    h = Horizon(HORIZON["start"], HORIZON["stop"], tuple(HORIZON["window"]), dict(HORIZON["windows"]))
    pk = [Package(k, v) for k, v in PACKAGES.items()]
    out = {"m0": m0, "seed": seed}
    t = time.process_time()
    for k in pk:
        e.run({"burnin_years": 0}, u, k, h, seed)
    out["v04"] = time.process_time() - t
    t = time.process_time()
    shared = e.run_batch([{}] * len(pk), u, pk, h, seed)
    out["v05_shared"] = time.process_time() - t
    t = time.process_time()
    solo = [e.run({}, u, k, h, seed) for k in pk]
    out["v05_unshared"] = time.process_time() - t
    out["same_outcomes"] = all(a.per_capita == b.per_capita and a.windows == b.windows for a, b in zip(shared, solo))
    out["fork_year"] = shared[0].meta["fork_year"]
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 1)
    ap.add_argument("--out", default=os.path.join(HERE, "runtime.yaml"))
    a = ap.parse_args(argv)
    from toolkit.engines.malaria.engine import VERSION
    with ProcessPoolExecutor(max(1, min(a.workers, len(POINTS)))) as ex:
        rows = list(ex.map(_point, POINTS, chunksize=1))
    mean = {k: round(sum(r[k] for r in rows) / len(rows), 2) for k in ("v04", "v05_shared", "v05_unshared")}
    doc = {"producer": "toolkit/engines/malaria/benchmarks/runtime.py", "engine_version": VERSION,
           "clock": "process CPU seconds", "n_agents": 4000, "horizon": HORIZON, "packages": list(PACKAGES),
           "repeats": len(rows), "seconds_per_point": mean,
           "ratio_to_v04": {k: round(v / mean["v04"], 3) for k, v in mean.items()},
           "shared_equals_unshared": all(r["same_outcomes"] for r in rows),
           "points": [{k: (round(v, 2) if isinstance(v, float) else v) for k, v in r.items()} for r in rows]}
    yaml.safe_dump(doc, open(a.out, "w"), sort_keys=False)
    print(f"wrote {a.out}: {mean}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
