#!/usr/bin/env python3
"""Per-run cost of the v0.5 start (burn-in shared by packages) against the v0.4 start (no burn-in).

    python3 toolkit/engines/malaria/benchmarks/runtime.py [--workers N] [--out PATH]

A decision-like point: the synthetic Sahel/savanna unit of equilibration.py, 4,000 agents, horizon 2016-2027,
decision window 2024-2027, past campaigns 2017.5 and 2020.5, four packages (none, pyrethroid nets, dual-AI nets
+ SMC, enhanced case management). Per point (m0 x seed) and mode, each in a fresh process on one core
(max_tasks_per_child=1, so the peak resident memory is that mode's own):
  v04           four run() calls with burnin_years 0 (the v0.4 computation: no burn-in, one run per package)
  v05_shared    one run_batch() call (one burn-in, packages forked at their first component event): what
                toolkit/analysis/batch.py runs per design point (run_point)
  v05_unshared  four run() calls at the card's burn-in (what a caller that does not batch pays)
CPU seconds, wall seconds and peak RSS per (point, mode). Writes runtime.yaml beside this file.
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
MODES = ("v04", "v05_shared", "v05_unshared")
HORIZON = {"start": 2016.0, "stop": 2027.0, "window": [2024.0, 2027.0], "windows": {"cal": [2021.67, 2022.0]}}


def _task(args):
    m0, seed, mode = args
    import resource
    from toolkit.engines.malaria.engine import MalariaEngine
    from toolkit.engines.contract import UnitSpec, Package, Horizon
    e = MalariaEngine()
    u = UnitSpec("rt", dict(UNIT, m0=m0, itn_hist_years=[2017.5, 2020.5], campaign_year=2024.0))
    h = Horizon(HORIZON["start"], HORIZON["stop"], tuple(HORIZON["window"]), dict(HORIZON["windows"]))
    pk = [Package(k, v) for k, v in PACKAGES.items()]
    t, w = time.process_time(), time.time()
    if mode == "v04":
        outs = [e.run({"burnin_years": 0}, u, k, h, seed) for k in pk]
    elif mode == "v05_shared":
        outs = e.run_batch([{}] * len(pk), u, pk, h, seed)
    else:
        outs = [e.run({}, u, k, h, seed) for k in pk]
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    rss_mb = rss / 1024 if sys.platform != "darwin" else rss / 2**20          # Linux: KiB; macOS: bytes
    return {"m0": m0, "seed": seed, "mode": mode, "cpu_s": round(time.process_time() - t, 2),
            "wall_s": round(time.time() - w, 2), "peak_rss_mb": round(rss_mb, 1),
            "fork_year": outs[0].meta.get("fork_year"),
            "outcomes": [[o.per_capita, o.windows] for o in outs]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 1)
    ap.add_argument("--out", default=os.path.join(HERE, "runtime.yaml"))
    a = ap.parse_args(argv)
    from toolkit.engines.malaria.engine import VERSION
    tasks = [(m0, s, m) for m0, s in POINTS for m in MODES]
    with ProcessPoolExecutor(max(1, min(a.workers, len(tasks))), max_tasks_per_child=1) as ex:
        rows = list(ex.map(_task, tasks, chunksize=1))
    by = {m: [r for r in rows if r["mode"] == m] for m in MODES}
    mean = lambda m, k: round(sum(r[k] for r in by[m]) / len(by[m]), 2)  # noqa: E731
    same = all(a_["outcomes"] == b_["outcomes"] for a_, b_ in zip(by["v05_shared"], by["v05_unshared"]))
    doc = {"producer": "toolkit/engines/malaria/benchmarks/runtime.py", "engine_version": VERSION,
           "clock": "process CPU seconds (cpu_s) and wall seconds (wall_s), one core per task",
           "n_agents": 4000, "horizon": HORIZON, "packages": list(PACKAGES), "repeats": len(POINTS),
           "seconds_per_point": {m: mean(m, "cpu_s") for m in MODES},
           "wall_seconds_per_point": {m: mean(m, "wall_s") for m in MODES},
           "peak_rss_mb": {m: max(r["peak_rss_mb"] for r in by[m]) for m in MODES},
           "ratio_to_v04": {m: round(mean(m, "cpu_s") / mean("v04", "cpu_s"), 3) for m in MODES},
           "shared_equals_unshared": same,
           "points": [{k: v for k, v in r.items() if k != "outcomes"} for r in rows]}
    yaml.safe_dump(doc, open(a.out, "w"), sort_keys=False)
    print(f"wrote {a.out}: {doc['seconds_per_point']} peak MB {doc['peak_rss_mb']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
