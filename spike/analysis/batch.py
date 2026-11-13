#!/usr/bin/env python3
"""Emulator design and engine batch (analysis engine, architecture-v2 §3.4). Engine-agnostic.

    python3 spike/analysis/batch.py design <decision_spec.yaml> <out_dir>
    python3 spike/analysis/batch.py run <out_dir> [--workers N] [--shard k/n]   # resumable; writes as it goes
                                    # (results go to $JOB_OUT when set, e.g. under cloud/vm_job.py)

Design: `emulator.points` design points. Each point takes the non-calibrated attributes of
one real unit (units drawn without replacement, then with replacement), a Sobol value of
the calibrated attribute over `calibration.log_range`, and Sobol values of every fitted
engine parameter over its range from the engine card. Every package in the spec is run at
every point with the same seed (common random numbers), so package contrasts carry little
simulation noise. A share `emulator.holdout_share` of points is held out to measure
emulator accuracy.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

import numpy as np
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from spike.analysis import spec as S  # noqa: E402
from toolkit.alloc import costs as acosts  # noqa: E402
from toolkit.engines.contract import UnitSpec, Package, Horizon  # noqa: E402


def unit_attributes(spec: dict, row: dict) -> dict:
    """Engine unit attributes for one units-table row (spec decision_units.engine_attributes)."""
    out = {}
    for attr, m in spec["decision_units"]["engine_attributes"].items():
        if "value" in m:
            out[attr] = m["value"]
        elif "column" in m:
            v = row[m["column"]]
            out[attr] = [float(x) for x in str(v).split(m["split"])] if "split" in m else _num(v)
        elif isinstance(m["columns"], dict):
            out[attr] = {k: float(row[c]) for k, c in m["columns"].items()}
        else:
            out[attr] = [float(row[c]) for c in m["columns"]]
    return out


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return v


def calibrated_value(spec: dict, z: float) -> float:
    return float(math.exp(z)) if spec["calibration"].get("scale", "log") == "log" else float(z)


def fitted_ranges(engine) -> dict:
    return {p.name: (p.lo, p.hi, p.value) for p in engine.parameters() if p.kind == "fitted"}


def horizon(spec: dict) -> Horizon:
    h = spec["horizon"]
    return Horizon(float(h["start"]), float(h["stop"]), tuple(h["window"]), {"cal": list(spec["calibration"]["window"])})


def design(spec: dict, out_dir: str) -> dict:
    from scipy.stats import qmc
    eng = S.load_engine(spec["engine"])
    units = S.read_units(spec)
    em = spec.get("emulator") or {}
    n = int(em.get("points", 64))
    rng = np.random.default_rng(int(em.get("seed", 7)))
    fr = fitted_ranges(eng)
    names = sorted(fr)
    sob = qmc.Sobol(d=1 + len(names), scramble=True, seed=int(em.get("seed", 7))).random(n)
    lo, hi = spec["calibration"]["log_range"]
    order = list(rng.permutation(len(units)))
    while len(order) < n:
        order += list(rng.permutation(len(units)))
    hold = set(rng.choice(n, size=int(round(n * float(em.get("holdout_share", 0.1)))), replace=False).tolist())
    pk = acosts.packages(spec["options"])
    uid = spec["decision_units"]["id"]
    points, jobs = [], []
    for i in range(n):
        row = units[order[i]]
        z = lo + (hi - lo) * sob[i, 0]
        theta = {nm: fr[nm][0] + (fr[nm][1] - fr[nm][0]) * sob[i, 1 + k] for k, nm in enumerate(names)}
        attrs = unit_attributes(spec, row)
        attrs[spec["calibration"]["attribute"]] = calibrated_value(spec, z)
        seed = 1000 + i
        points.append({"point": i, "unit_id": row[uid], "z": z, "theta": theta, "seed": seed, "holdout": i in hold})
        for name, cs in pk.items():
            jobs.append({"id": f"{i}|{name}", "point": i, "package": name, "components": S.engine_package(spec, cs),
                         "unit": attrs, "params": theta, "seed": seed})
    os.makedirs(out_dir, exist_ok=True)
    json.dump({"points": points, "fitted_names": names, "packages": list(pk), "spec_engine": spec["engine"],
               "horizon": spec["horizon"], "cal_window": spec["calibration"]["window"]},
              open(os.path.join(out_dir, "design.json"), "w"), indent=1)
    with open(os.path.join(out_dir, "jobs.jsonl"), "w") as f:
        for j in jobs:
            f.write(json.dumps(j) + "\n")
    return {"points": n, "jobs": len(jobs), "packages": len(pk)}


_E = {}


def _run_one(args):
    ref, hz, job = args
    if ref not in _E:
        _E[ref] = S.load_engine(ref)
    eng = _E[ref]
    h = Horizon(float(hz["start"]), float(hz["stop"]), tuple(hz["window"]), {"cal": list(hz["cal"])})
    try:
        o = eng.run(job["params"], UnitSpec(str(job["point"]), job["unit"]), Package(job["package"], job["components"]),
                    h, int(job["seed"]))
        return {"id": job["id"], "ok": True, "result": {"per_capita": o.per_capita, "windows": o.windows,
                                                         "cost_drivers": o.cost_drivers, "meta": o.meta}}
    except Exception as e:  # noqa: BLE001 -- recorded, the analysis fails closed on missing results
        return {"id": job["id"], "ok": False, "error": f"{type(e).__name__}: {e}"}


def run(out_dir: str, workers: int, results_dir: str | None = None, shard: str | None = None) -> dict:
    d = json.load(open(os.path.join(out_dir, "design.json")))
    hz = dict(d["horizon"], cal=d["cal_window"])
    rdir = results_dir or out_dir
    os.makedirs(rdir, exist_ok=True)
    rp = os.path.join(rdir, "results.jsonl")
    done = set()
    for src in (rp, os.path.join(out_dir, "results_prev.jsonl")):   # resume, also from results shipped with the design
        if not os.path.exists(src):
            continue
        for line in open(src):
            try:
                r = json.loads(line)
                if r.get("ok"):
                    done.add(r["id"])
            except json.JSONDecodeError:
                pass
    jobs = [json.loads(x) for x in open(os.path.join(out_dir, "jobs.jsonl"))]
    if shard:   # "k/n": this process runs the design points with point % n == k
        k, nsh = (int(x) for x in shard.split("/"))
        jobs = [j for j in jobs if j["point"] % nsh == k]
    todo = [j for j in jobs if j["id"] not in done]
    t0 = time.time()
    n_ok = n_bad = 0
    # A fresh process pool per chunk: attempt 1 lost every shard to a BrokenProcessPool at ~750 runs
    # (worker memory growth); max_tasks_per_child then deadlocked once every child had been recycled.
    chunk = max(workers * 15, 1)
    k = 0
    with open(rp, "a") as f:
        for c0 in range(0, len(todo), chunk):
            with ProcessPoolExecutor(workers) as ex:
                futs = [ex.submit(_run_one, (d["spec_engine"], hz, j)) for j in todo[c0:c0 + chunk]]
                for fu in as_completed(futs):
                    r = fu.result()
                    f.write(json.dumps(r) + "\n")
                    f.flush()
                    n_ok += r["ok"]
                    n_bad += not r["ok"]
                    k += 1
                    if k % 200 == 0:
                        print(f"{k}/{len(todo)} done, {time.time() - t0:.0f}s", flush=True)
    summ = {"jobs": len(jobs), "already_done": len(done), "ran": len(todo), "ok": n_ok, "failed": n_bad,
            "wall_s": round(time.time() - t0, 1), "workers": workers}
    json.dump(summ, open(os.path.join(rdir, "batch_summary.json"), "w"), indent=1)
    print(summ)
    return summ


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["design", "run"])
    ap.add_argument("a")
    ap.add_argument("b", nargs="?")
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 1)
    ap.add_argument("--results-dir", default=None)
    ap.add_argument("--shard", default=None, help="k/n: run only design points with point %% n == k")
    a = ap.parse_args(argv)
    if a.action == "design":
        spec = yaml.safe_load(open(a.a))
        errs = S.validate(spec)
        if errs:
            print("spec invalid:\n  " + "\n  ".join(errs))
            return 2
        print(design(spec, a.b))
    else:
        run(a.a, a.workers, a.results_dir or os.environ.get("JOB_OUT"), a.shard)
    return 0


if __name__ == "__main__":
    sys.exit(main())
