"""Engine batch for the emulator path (design §2), run through scripts/compute.py.

Design: `analysis.emulator.points` design points. Each takes the engine attributes of one
real unit (units drawn without replacement, then with replacement), a Sobol value of the
calibrated attribute over `calibration.range`, and Sobol values of every fitted engine
parameter over its engine-card range. Every package runs at every point with the same
seed (common random numbers). A share `holdout_share` of points is held out to measure
emulator accuracy. The design is the spike's (`spike/analysis/batch.py`), unchanged, so
the stored spike batch is reusable.

Runs go to `scripts/compute.py` (`run_map`): dask on Coiled when the launcher provides a
scheduler, else a local process pool that is replaced every chunk (spike lesson 7);
results are kept per input, so a rerun completes a stopped batch.
"""
from __future__ import annotations

import json
import math
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from toolkit.engines.contract import UnitSpec, Package, Horizon  # noqa: E402

PRODUCER = "toolkit/analysis/batch.py"
CAL_WINDOW = "cal"


def fitted_ranges(engine) -> dict:
    """{name: (lo, hi, value)} of the engine's fitted parameters, in engine order."""
    return {p.name: (p.lo, p.hi, p.value) for p in engine.parameters() if p.kind == "fitted"}


def calibrated_value(spec, z: float) -> float:
    return float(math.exp(z)) if spec["calibration"].get("scale", "log") == "log" else float(z)


def windows(spec) -> dict:
    cal = spec["calibration"]
    return {CAL_WINDOW: list(cal["window"])} if cal.get("method") == "emulator_inversion" else {}


def design(spec) -> tuple[dict, list[dict]]:
    """(design, jobs): the spike's design, from the spec."""
    from scipy.stats import qmc
    eng = spec.engine()
    units = spec.units()
    em = spec["analysis"].get("emulator") or {}
    n = int(em.get("points", 64))
    rng = np.random.default_rng(int(em.get("seed", 7)))
    fr = fitted_ranges(eng)
    names = sorted(fr)
    sob = qmc.Sobol(d=1 + len(names), scramble=True, seed=int(em.get("seed", 7))).random(n)
    lo, hi = spec["calibration"]["range"]
    order = list(rng.permutation(len(units)))
    while len(order) < n:
        order += list(rng.permutation(len(units)))
    hold = set(rng.choice(n, size=int(round(n * float(em.get("holdout_share", 0.1)))), replace=False).tolist())
    pk = spec.packages()
    uid = spec.uid
    attr = spec["calibration"]["attribute"]
    points, jobs = [], []
    for i in range(n):
        row = units[order[i]]
        z = lo + (hi - lo) * sob[i, 0]
        theta = {nm: fr[nm][0] + (fr[nm][1] - fr[nm][0]) * sob[i, 1 + k] for k, nm in enumerate(names)}
        attrs = spec.unit_attributes(row)
        attrs[attr] = calibrated_value(spec, z)
        seed = 1000 + i
        points.append({"point": i, "unit_id": row[uid], "z": z, "theta": theta, "seed": seed, "holdout": i in hold})
        for name, cs in pk.items():
            jobs.append({"id": f"{i}|{name}", "point": i, "package": name, "components": spec.engine_package(cs),
                         "unit": attrs, "params": theta, "seed": seed})
    h = spec["horizon"]
    d = {"points": points, "fitted_names": names, "packages": list(pk), "spec_engine": spec["engine"],
         "horizon": {"start": h["start"], "stop": h["stop"], "window": list(spec["objective"]["horizon"])},
         "cal_window": list(spec["calibration"]["window"])}
    return d, [dict(j, engine=spec["engine"], horizon=d["horizon"], windows=windows(spec)) for j in jobs]


_E = {}


def run_job(job: dict) -> dict:
    """One engine run (the compute.py function). Engine instances are cached per process."""
    from toolkit.spec.schema import load_engine
    key = json.dumps(job["engine"], sort_keys=True)
    if key not in _E:
        _E[key] = load_engine(job["engine"])
    hz = job["horizon"]
    h = Horizon(float(hz["start"]), float(hz["stop"]), tuple(hz["window"]), dict(job.get("windows") or {}))
    o = _E[key].run(job["params"], UnitSpec(str(job["point"]), job["unit"]), Package(job["package"], job["components"]),
                    h, int(job["seed"]))
    return {"per_capita": o.per_capita, "windows": o.windows, "cost_drivers": o.cost_drivers, "meta": o.meta}


def run(run_dir: str, jobs: list[dict], out_rel: str = "analysis/batch/results.jsonl", **kw) -> dict:
    """Run every job through scripts/compute.py; returns {job id: result}. Missing results fail closed later."""
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import compute
    out = os.path.join(run_dir, out_rel)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    inputs = [{k: v for k, v in j.items() if k != "id"} for j in jobs]
    rows, summ = compute.run_map(run_dir, os.path.join(ROOT, "toolkit", "analysis", "batch.py") + "::run_job",
                                 inputs, out, purpose="v2 analysis: engine batch", **kw)
    res = {j["id"]: r["result"] for j, r in zip(jobs, rows) if r.get("ok")}
    return {"results": res, "summary": {k: summ.get(k) for k in ("backend", "workers", "n", "n_failed", "wall_s")},
            "path": out}


def load_results(path: str, jobs: list[dict] | None = None) -> dict:
    """{job id: result} from a spike-format file (`id` per row) or a compute.py file (`i` per row, in job order)."""
    out = {}
    with open(path) as f:
        for line in f:
            if not line.strip():
                continue
            r = json.loads(line)
            if not r.get("ok"):
                continue
            if "id" in r:
                out[r["id"]] = r["result"]
            elif jobs is not None and "i" in r:
                out[jobs[r["i"]]["id"]] = r["result"]
    return out
