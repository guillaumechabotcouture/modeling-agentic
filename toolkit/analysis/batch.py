"""Engine batch for the emulator path (design §2), run through scripts/compute.py.

Design: `analysis.emulator.points` design points. Each takes the engine attributes of one
real unit (units drawn without replacement, then with replacement), a Sobol value of the
calibrated attribute over `calibration.range`, Sobol values of every fitted engine
parameter over its engine-card range, and Sobol values of every prior parameter the
uncertainty draws vary (toolkit/spec/priors.py) over its declared range (v2 priors; a spec
whose engine and spec declare no prior gets the spike's design unchanged). A parameter that a
`parameter_value` decision test sets outside that range has its design box widened to span the
test's values (`design_ranges` in design.json); the draws keep the declared range. Every package runs at every point with the same
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


def prior_ranges(spec) -> dict:
    """{name: (lo, hi, value)} of the prior parameters the uncertainty draws vary (toolkit/spec/priors.py), in name
    order. They are emulator inputs: the design spans their declared ranges, so every draw is inside the design box."""
    from toolkit.spec import priors as PRI
    return {k: (p.lo, p.hi, p.value) for k, p in PRI.drawn(spec).items()}


def test_value_ranges(spec, names) -> dict:
    """{name: (lo, hi)} of the values the spec's parameter_value decision tests set for design parameters `names`.
    The design box spans them too, so each test is evaluated inside the design (the draws keep the declared ranges)."""
    out = {}
    for dt in (spec["analysis"].get("decision_tests") or []):
        if dt.get("type") != "parameter_value" or dt.get("parameter") not in names:
            continue
        vs = [float(v) for v in dt.get("values") or []]
        if vs:
            lo, hi = out.get(dt["parameter"], (min(vs), max(vs)))
            out[dt["parameter"]] = (min(lo, *vs), max(hi, *vs))
    return out


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
    pr = prior_ranges(spec)                 # v2 priors: drawn parameters enter the design as inputs
    pnames = list(pr)
    fr = dict(fr, **pr)
    tv = test_value_ranges(spec, set(fr))
    ext = {}
    for nm, (tlo, thi) in tv.items():
        lo_, hi_ = min(fr[nm][0], tlo), max(fr[nm][1], thi)
        if (lo_, hi_) != (fr[nm][0], fr[nm][1]):
            ext[nm] = [lo_, hi_]
            fr[nm] = (lo_, hi_, fr[nm][2])
    sob =qmc.Sobol(d=1 + len(names) + len(pnames), scramble=True, seed=int(em.get("seed", 7))).random(n)
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
        theta = {nm: fr[nm][0] + (fr[nm][1] - fr[nm][0]) * sob[i, 1 + k] for k, nm in enumerate(names + pnames)}
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
    if pnames:
        d["prior_names"] = pnames
    if ext:
        d["design_ranges"] = ext          # parameters whose design box was widened to span decision-test values
    return d,[dict(j, engine=spec["engine"], horizon=d["horizon"], windows=windows(spec)) for j in jobs]


_E = {}
CORE = ("point", "package", "components", "unit", "params", "seed")     # a job's own inputs (beside engine, horizon)
# A re-run on another platform (the spike's batch ran on Linux VMs) differs in the last digits of floating point
# (observed: about 1e-9 relative); 1e-6 separates that from a model change. Wall-clock timings are not outputs.
ADOPT_RTOL = 1e-6
TIMING_KEYS = frozenset({"seconds"})
# Codex r2 P1-2: an adoption re-runs at least this many stored jobs before it stamps any of them.
ADOPT_MIN_SAMPLE = 4
ADOPT_MIN_SHARE = 0.01


def min_adopt_sample(n_jobs: int) -> int:
    import math
    return min(n_jobs, max(ADOPT_MIN_SAMPLE, math.ceil(ADOPT_MIN_SHARE * n_jobs)))


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
    need = min_adopt_sample(len(ids))
    if not isinstance(sample, int) or isinstance(sample, bool) or sample < need:
        raise ValueError(f"stored batch not adopted: the re-run sample must be at least {need} jobs "
                         f"(max({ADOPT_MIN_SAMPLE}, {ADOPT_MIN_SHARE:.0%} of {len(ids)})); got {sample!r} (Codex r2 P1-2)")
    n = min(sample, len(ids))
    pick = sorted({ids[int(round(k * (len(ids) - 1) / max(n - 1, 1)))] for k in range(n)})
    pick += [i for i in ids if i not in set(pick)][:n - len(pick)]     # spacing collisions: fill to n distinct jobs
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import compute
    rows, _ = compute.map_fn(run_dir, os.path.join(ROOT, "toolkit", "analysis", "batch.py") + "::run_job",
                             [{k: v for k, v in want[i].items() if k != "id"} for i in pick], backend="local",
                             max_workers=workers, ledger=False)
    mism, worst = [], [0.0]
    if len(rows) != len(pick):
        mism.append(f"{len(rows)} re-run rows for {len(pick)} sampled jobs")
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


# --------------------------------------------------------------------------- self-test
def _self_test() -> int:
    """python3 toolkit/analysis/batch.py --self-test: shipping a batch with its exact inputs, and adoption across
    platforms, on the toy engine. The cases replay the Nigeria re-run: shards computed on Linux VMs and merged on
    macOS, where one design point's inputs (m0 = exp(z)) regenerated with last-bit differences (32 rows' input
    hashes mismatched, one row per package), and one adoption re-run reproduced only to about 1.4%."""
    import copy
    import shutil
    import tempfile
    fails = []

    def ok(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    def case(label, fn):
        try:
            fn()
        except Exception as e:      # noqa: BLE001 -- a missing function is a failing case, not a crash of the test
            ok(False, f"{label} ({type(e).__name__}: {e})")

    def refused(fn, code):
        try:
            fn()
        except ValueError as e:
            return getattr(e, "code", None) == code, f"{getattr(e, 'code', None)}: {e}"
        return False, "not refused"

    from toolkit.spec import schema
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    g = globals()
    linux = {"system": "Linux", "machine": "x86_64", "python": "3.12.3", "numpy": "2.1.3", "scipy": "1.14.1"}
    with tempfile.TemporaryDirectory() as d:
        spec = schema.load(schema.toy_spec(d, n=12))
        spec["analysis"]["emulator"]["points"] = 8
        eng = spec.engine()
        design0, jobs0 = design(spec)
        attr = spec["calibration"]["attribute"]
        # the VM's inputs: design point 3 regenerated with a last-bit difference in the calibrated attribute
        vm_jobs = copy.deepcopy(jobs0)
        for j in vm_jobs:
            if j["point"] == 3:
                j["unit"][attr] = float(np.nextafter(j["unit"][attr], np.inf))
        vm_design = copy.deepcopy(design0)
        vm_design["points"][3]["z"] = float(np.nextafter(vm_design["points"][3]["z"], np.inf))
        n3 = sum(j["point"] == 3 for j in vm_jobs)
        rows = [{"id": j["id"], "ok": True, "result": run_job(j)} for j in vm_jobs]
        made = [0]

        def b0():
            # the observed failure, kept as evidence: regenerating the design at merge time loses the shipped rows
            res, probs = verify_rows(rows, jobs0, eng)
            ok(len(probs) == n3 and all("inputs differ" in p for p in probs),
               f"BA0 regenerating the design at merge time rejects the {n3} rows of the point with last-bit "
               f"differences (the re-run's 32 rows, one per package): {len(probs)}")
        case("BA0", b0)

        def shard_dirs(plat_a, plat_b=None, jobs=None, rws=None):
            jobs, rws, out = jobs or vm_jobs, rws or rows, []
            made[0] += 1
            for k in (0, 1):
                sd = os.path.join(d, f"run{made[0]}_shard{k}")
                mine = [j for j in jobs if j["point"] % 2 == k]
                ids = {j["id"] for j in mine}
                g["ship"](sd, spec, vm_design, mine, [r for r in rws if r["id"] in ids],
                          platform=plat_a if k == 0 or plat_b is None else plat_b)
                out.append(sd)
            return out

        def merged(sds):
            m = os.path.join(d, f"merged{made[0]}")
            g["merge_shipped"](m, sds)
            return m

        def b1():
            m = merged(shard_dirs(linux))
            man = json.load(open(os.path.join(m, SHIP_MANIFEST)))
            ok(man.get("platform") == linux and man.get("jobs_sha256") and man.get("results_sha256")
               and man.get("design_sha256"),
               "BA1 a shipped batch carries its exact jobs file, design and results, each hashed, and the platform "
               "that computed them")
            des, jbs, res, info = g["load_shipped"](m, spec, design0, jobs0, eng)
            ok(len(res) == len(jobs0) and jbs == vm_jobs and des["points"][3]["z"] == vm_design["points"][3]["z"]
               and info.get("inputs") == "shipped" and info.get("cross_platform") is True
               and 0 < info.get("input_max_rel_diff", 0) <= INPUT_RTOL,
               f"BA1 merged on another platform, every row is accepted against the shipped inputs, not a design "
               f"regenerated at merge time: {len(res)}/{len(jobs0)}, {info}")
        case("BA1", b1)

        def b2():
            m = merged(shard_dirs(platform_info()))
            r, why = refused(lambda: g["load_shipped"](m, spec, design0, jobs0, eng), "shipped_inputs_differ")
            ok(r, f"BA2 on the computing platform the design must regenerate exactly; a difference fails closed: {why}")
        case("BA2", b2)

        def b3():
            big = copy.deepcopy(vm_jobs)
            for j in big:
                if j["point"] == 3:
                    j["unit"][attr] *= 1 + 1e-6
            m = merged(shard_dirs(linux, jobs=big, rws=[{"id": j["id"], "ok": True, "result": run_job(j)}
                                                         for j in big]))
            r, why = refused(lambda: g["load_shipped"](m, spec, design0, jobs0, eng), "shipped_inputs_differ")
            ok(r, f"BA3 across platforms only last-bit input differences are accepted (1e-6 is a changed input): {why}")
        case("BA3", b3)

        def b4():
            m = merged(shard_dirs(linux))
            p = os.path.join(m, "results.jsonl")
            lines = open(p).read().splitlines()
            r0 = json.loads(lines[0])
            r0["result"]["result"]["per_capita"] = {}
            lines[0] = json.dumps(r0)
            open(p, "w").write("\n".join(lines) + "\n")
            r, why = refused(lambda: g["load_shipped"](m, spec, design0, jobs0, eng), "shipped_hash_mismatch")
            ok(r, f"BA4 a shipped file edited after hashing fails closed: {why}")
        case("BA4", b4)

        def b5():
            sds = shard_dirs(linux, dict(linux, system="Darwin", machine="arm64"))
            r, why = refused(lambda: merged(sds), "shipped_platform_mixed")
            ok(r, f"BA5 shards computed on different platforms are not merged into one batch: {why}")
        case("BA5", b5)

        def b6():
            sds = shard_dirs(linux)
            r, why = refused(lambda: g["load_shipped"](sds[0], spec, design0, jobs0, eng), "shipped_incomplete")
            ok(r, f"BA6 a batch missing jobs of the current design fails closed: {why}")
        case("BA6", b6)

        def b7():
            m = merged(shard_dirs(linux))
            d2 = os.path.join(d, "other_spec")
            os.makedirs(d2)
            shutil.copy(spec.path, d2)
            shutil.copy(os.path.join(d, "units.csv"), d2)
            with open(os.path.join(d2, os.path.basename(spec.path)), "a") as f:
                f.write("# edited\n")
            spec2 = schema.load(os.path.join(d2, os.path.basename(spec.path)))
            r, why = refused(lambda: g["load_shipped"](m, spec2, design0, jobs0, eng), "shipped_spec_differs")
            ok(r, f"BA7 a batch shipped for another decision spec fails closed: {why}")
        case("BA7", b7)

        def b8():
            sj, sr, sdz = (os.path.join(d, x) for x in ("a_jobs.jsonl", "a_res.jsonl", "a_design.json"))
            with open(sj, "w") as f:
                f.write("".join(json.dumps({k: j[k] for k in ("id",) + CORE}) + "\n" for j in jobs0))
            with open(sr, "w") as f:
                f.write("".join(json.dumps({"id": j["id"], "ok": True, "result": run_job(j)["result"]}) + "\n"
                                for j in jobs0))
            json.dump({"spec_engine": design0["spec_engine"], "horizon": design0["horizon"],
                       "cal_window": design0["cal_window"], "platform": linux}, open(sdz, "w"))
            import compute
            called = []
            orig = compute.map_fn
            compute.map_fn = lambda *a, **k: called.append(1) or orig(*a, **k)
            try:
                r, why = refused(lambda: adopt(d, sr, sj, sdz, jobs0, design0, eng, sample=len(jobs0)),
                                 "adopt_platform_differs")
            finally:
                compute.map_fn = orig
            ok(r and not called, f"BA8 an adoption recorded on another platform is refused before its replay "
                                 f"recomputes anything (the re-run's 1.4% sample): {why}; replays run {len(called)}")
            ok(ADOPT_RTOL == 1e-6, "BA9 the adoption tolerance is unchanged (1e-6)")
        case("BA8", b8)
    if fails:
        print(f"FAILED: {len(fails)} self-test case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    print(__doc__)
    sys.exit(2)
