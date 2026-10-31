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
CORE = ("point", "package", "components", "unit", "params", "seed")     # a job's own inputs (beside engine, horizon)
# A re-run on another platform (the spike's batch ran on Linux VMs) differs in the last digits of floating point
# (observed: about 1e-9 relative); 1e-6 separates that from a model change. Wall-clock timings are not outputs.
ADOPT_RTOL = 1e-6
TIMING_KEYS = frozenset({"seconds"})


def job_sha(job: dict) -> str:
    """sha256 of a job's full inputs (engine reference and config, horizon, windows, unit, package, params, seed)."""
    import hashlib
    return hashlib.sha256(json.dumps({k: v for k, v in job.items() if k != "id"}, sort_keys=True,
                                     default=str).encode()).hexdigest()


def _engine(ref):
    from toolkit.spec.schema import load_engine
    key = json.dumps(ref, sort_keys=True)
    if key not in _E:
        _E[key] = load_engine(ref)
    return _E[key]


def run_job(job: dict) -> dict:
    """One engine run (the compute.py function), with its provenance: the job's input hash and the engine's identity
    and code fingerprint (Codex P1-3). Engine instances are cached per process."""
    from toolkit.engines.contract import engine_identity
    eng = _engine(job["engine"])
    hz = job["horizon"]
    h = Horizon(float(hz["start"]), float(hz["stop"]), tuple(hz["window"]), dict(job.get("windows") or {}))
    o = eng.run(job["params"], UnitSpec(str(job["point"]), job["unit"]), Package(job["package"], job["components"]),
                h, int(job["seed"]))
    return {"result": {"per_capita": o.per_capita, "windows": o.windows, "cost_drivers": o.cost_drivers, "meta": o.meta},
            "provenance": {"job_sha": job_sha(job), "engine": engine_identity(eng), "producer": PRODUCER}}


def run(run_dir: str, jobs: list[dict], engine, out_rel: str = "analysis/batch/results.jsonl", **kw) -> dict:
    """Run every job through scripts/compute.py; returns {job id: result} for rows whose provenance matches."""
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import compute
    out = os.path.join(run_dir, out_rel)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    inputs = [{k: v for k, v in j.items() if k != "id"} for j in jobs]
    rows, summ = compute.run_map(run_dir, os.path.join(ROOT, "toolkit", "analysis", "batch.py") + "::run_job",
                                 inputs, out, purpose="v2 analysis: engine batch", **kw)
    res, problems = verify_rows([dict(r, id=j["id"]) for j, r in zip(jobs, rows) if r.get("ok")], jobs, engine)
    return {"results": res, "problems": problems,
            "summary": {k: summ.get(k) for k in ("backend", "workers", "n", "n_failed", "wall_s")}, "path": out}


def verify_rows(rows: list[dict], jobs: list[dict], engine) -> tuple[dict, list[str]]:
    """Keep a row only when it carries harness provenance matching its job's full inputs and the current engine
    identity and code (Codex P1-3). Rows: {"id", "result": {"result", "provenance"}} (compute.py format)."""
    from toolkit.engines.contract import engine_identity
    ident = engine_identity(engine)
    want = {j["id"]: job_sha(j) for j in jobs}
    res, bad = {}, []
    for r in rows:
        jid, body = r.get("id"), r.get("result")
        pv = body.get("provenance") if isinstance(body, dict) else None
        if jid not in want:
            continue
        if not isinstance(pv, dict):
            bad.append(f"{jid}: no provenance")
        elif pv.get("job_sha") != want[jid]:
            bad.append(f"{jid}: inputs differ from the current job")
        elif pv.get("engine") != ident:
            bad.append(f"{jid}: computed by another engine identity or code")
        else:
            res[jid] = body["result"]
    return res, bad


def load_results(path: str, jobs: list[dict], engine) -> tuple[dict, list[str]]:
    """Stored results (compute.py rows, in job order) accepted only with matching provenance per row."""
    rows = []
    with open(path) as f:
        for line in f:
            if line.strip():
                r = json.loads(line)
                if r.get("ok") and isinstance(r.get("i"), int) and r["i"] < len(jobs):
                    rows.append(dict(r, id=jobs[r["i"]]["id"]))
                elif r.get("ok") and "id" in r:
                    rows.append(r)
    return verify_rows(rows, jobs, engine)


def adopt(run_dir: str, src_results: str, src_jobs: str, src_design: str, jobs: list[dict], design: dict, engine,
          sample: int = 4, workers: int = 2, rtol: float = ADOPT_RTOL) -> dict:
    """Adopt a batch computed before results carried provenance (the spike's). Every stored job must equal the
    current job on its full inputs; the stored design must name the same engine, horizon and windows; and a
    deterministic sample of jobs is re-run with the current engine and must reproduce the stored results. The adopted
    rows are then written with harness provenance (`adopted_from`); anything short of that fails closed."""
    from toolkit.analysis.artifacts import sha256
    from toolkit.engines.contract import engine_identity
    sd = json.load(open(src_design))
    want = {j["id"]: j for j in jobs}
    probs = []
    ref = design["spec_engine"]
    if sd.get("spec_engine") not in (ref, ref.get("ref") if isinstance(ref, dict) and not ref.get("config") else None):
        probs.append(f"stored design engine {sd.get('spec_engine')!r} differs from {ref!r}")
    for k in ("start", "stop", "window"):
        if (sd.get("horizon") or {}).get(k) != design["horizon"].get(k):
            probs.append(f"stored design horizon.{k} differs")
    if sd.get("cal_window") != design["cal_window"]:
        probs.append("stored design calibration window differs")
    seen = set()
    with open(src_jobs) as f:
        for line in f:
            if not line.strip():
                continue
            sj = json.loads(line)
            j = want.get(sj.get("id"))
            if j is None:
                probs.append(f"stored job {sj.get('id')} is not a current job")
                continue
            diff = [k for k in CORE if json.dumps(sj.get(k), sort_keys=True) != json.dumps(j.get(k), sort_keys=True)]
            if diff:
                probs.append(f"stored job {sj['id']}: inputs differ ({diff})")
            seen.add(sj["id"])
    probs += [f"current job {k} is not in the stored batch" for k in sorted(set(want) - seen)[:5]]
    stored = {}
    with open(src_results) as f:
        for line in f:
            if line.strip():
                r = json.loads(line)
                if r.get("ok"):
                    stored[r["id"]] = r["result"]
    probs += [f"no stored result for {k}" for k in sorted(set(want) - set(stored))[:5]]
    if probs:
        raise ValueError("stored batch not adopted: " + "; ".join(probs[:8]))
    ids = sorted(want)
    pick = sorted({ids[int(k * (len(ids) - 1) / max(sample - 1, 1))] for k in range(sample)}) if sample > 0 else []
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import compute
    rows, _ = compute.map_fn(run_dir, os.path.join(ROOT, "toolkit", "analysis", "batch.py") + "::run_job",
                             [{k: v for k, v in want[i].items() if k != "id"} for i in pick], backend="local",
                             max_workers=workers, ledger=False)
    mism, worst = [], [0.0]
    for i, r in zip(pick, rows):
        if not r.get("ok"):
            mism.append(f"{i}: rerun failed ({r.get('error')})")
        elif not _same(r["result"]["result"], stored[i], rtol, worst):
            mism.append(f"{i}: rerun differs from the stored result beyond rtol {rtol:g}")
    if mism:
        raise ValueError("stored batch not adopted: the current engine does not reproduce it: " + "; ".join(mism))
    ident = engine_identity(engine)
    out = os.path.join(run_dir, "analysis", "batch", "results.jsonl")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    src = {"path": os.path.abspath(src_results), "sha256": sha256(src_results), "jobs_sha256": sha256(src_jobs)}
    with open(out, "w") as f:
        for n, j in enumerate(jobs):
            f.write(json.dumps({"i": n, "ok": True, "result": {"result": stored[j["id"]], "provenance": {
                "job_sha": job_sha(j), "engine": ident, "producer": PRODUCER, "adopted_from": src}}}) + "\n")
    return {"path": out, "adopted_from": src, "jobs_matched": len(seen), "rerun_sample": pick,
            "rerun_reproduced": len(pick), "rtol": rtol, "rerun_max_rel_diff": worst[0],
            "ignored": sorted(TIMING_KEYS)}


def _same(a, b, rtol, worst=None) -> bool:
    if isinstance(a, dict) and isinstance(b, dict):
        ka, kb = set(a) - TIMING_KEYS, set(b) - TIMING_KEYS
        return ka == kb and all([_same(a[k], b[k], rtol, worst) for k in ka])
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all([_same(x, y, rtol, worst) for x, y in zip(a, b)])
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
        rel = 0.0 if a == b else abs(a - b) / max(abs(a), abs(b))
        if worst is not None:
            worst[0] = max(worst[0], rel)
        return rel <= rtol
    return a == b
