#!/usr/bin/env python3
"""Direct evaluation: the uncertainty draws' engine runs fanned out over the run's compute workers.

    python3 toolkit/analysis/draw_fanout.py --self-test

On `analysis.evaluation: direct` every draw runs the engine for every (unit, package) pair. The hb1 eval run
flu_vaccine R2 ran 200 draws x 3,374 pairs serially in the analysis process for 14.6 h while its compute workers
sat idle. When the run has compute workers (MA_COMPUTE_SCHEDULER, set by the launcher on Coiled) or the caller asks
for it (compute_kw `draw_fanout: true`), uncertainty._draw_rows samples a wave of draws in order, sends their engine
runs through scripts/compute.py (`map_fn`), then evaluates each draw in order on its prefetched runs
(DirectModel.use_runs). Without workers the draws run in-process as before.

Deterministic whatever the number of workers: a task is one draw's pairs cut exactly as DirectModel._runs cuts them
in-process (run_batch calls of DirectModel.BATCH members for an engine that declares batch_equals_run, else one
run() per pair), so every engine call has the same members as the in-process evaluation. Each task's result carries
the sha256 of its inputs and the engine identity, checked on return (fail closed). Nothing here names a disease.

Which engines fan out: only one that declares `batch_equals_run` (its run_batch equals run pair by pair, a contract
self-test of that engine: its results do not depend on how pairs are grouped), and whose input files (benchmark
results, fitted values, priors, file paths in its config) lie where compute.py ships them to workers (the repo's
toolkit/ and scripts/, the run's models/ and data/). Any other engine keeps its draws in-process, and the reason is
logged (`A.draw_fanout_off`). Codex review of #347: the package-rate engine multiplies rate ratios in frozenset order,
which depends on each process's hash seed, so it is not fanned out. Inputs that a JSON round trip would change
(mapping keys that are not text) also stay in-process, since a task may cross to a worker as JSON.

Limits, as they are. A wave holds at most WAVE_PAIRS engine outcomes, but never less than one whole draw: a draw with
more pairs is held whole, as the in-process evaluation holds it. `max_workers` (from compute_kw or
`analysis.workers`) reaches compute.py as for the batch stage; compute.py's tagged Dask pool does not apply it
(its own MA_COMPUTE_* settings govern that pool, for every stage).
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

PRODUCER = "toolkit/analysis/draw_fanout.py"
REL = "analysis/draw_fanout.json"      # written by run.py when the draws fanned out (backend, tasks, waves)
WAVE = 50            # draws per compute call: bounds the engine outcomes held in memory at once
PAIRS_PER_TASK = 64  # an engine without batch_equals_run: run() pairs per task
WAVE_PAIRS = 200000   # engine runs per wave at most (a wave holds its draws' outcomes until they are evaluated)
MAP_KEYS = ("backend", "max_workers", "timeout_s", "ledger", "max_core_hours")
META_KEYS = ("age_shares",)     # the only Outcomes.meta field DirectModel.predict reads (model._group, group_sizes)


def _engine_files(A) -> list:
    """The files the engine reads: its benchmark results, fitted values and priors, and file paths in its config."""
    from toolkit.spec import priors as PRI
    eng = A.eng
    out = [getattr(eng, k, None) for k in ("results_path", "fitted_path")] + [PRI.priors_path(eng)]
    cfg = (A.spec["engine"] or {}).get("config") if isinstance(A.spec["engine"], dict) else None

    def walk(x):
        if isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, (list, tuple)):
            for v in x:
                walk(v)
        elif isinstance(x, str) and os.path.isfile(A.spec.resolve(x) if not os.path.isabs(x) else x):
            out.append(A.spec.resolve(x) if not os.path.isabs(x) else x)
    walk(cfg or {})
    return [f for f in out if f and os.path.exists(f)]


def _shipped(path: str, run_dir: str) -> bool:
    """Whether compute.make_bundle ships this file to its workers at the same path, under its own rules: inside the
    run's models/ or data/ or the repo's code dirs, in no __pycache__, .git or _truth* directory, not a file
    `_bundle_skips` leaves out (logs, batch records), and within the size limit (Codex r2 P1-3)."""
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import compute
    try:
        rd = os.path.realpath(run_dir)
        # Codex r3 P1-1: the bundle walks real directories but filters the file's own name (a symlink is judged by
        # its name, not its target's)
        dp, fn = os.path.realpath(os.path.dirname(os.path.abspath(path))), os.path.basename(path)
        rp = os.path.join(dp, fn)
        if not os.path.isfile(rp):
            return False
        if rp in {os.path.join(compute.REPO, r) for r in compute._data_set_files(compute.FP_DATA_SET, skip=rd)}:
            return True          # Codex r3 P2: the declared data set ships whatever its size or name
        roots = [os.path.join(rd, "models"), os.path.join(rd, "data")] + [os.path.join(compute.REPO, x)
                                                                         for x in compute.REPO_CODE_DIRS]
        root = next((r for r in roots if rp.startswith(r + os.sep)), None)
        if root is None:
            return False
        parts = os.path.relpath(dp, root).split(os.sep)
        if any(x in ("__pycache__", ".git") or x.startswith("_truth") for x in parts):
            return False
        # Codex r3 P1-2: only the directory's files count as sidecars (os.walk's `files`, as make_bundle reads them)
        names = {e.name for e in os.scandir(dp) if not e.is_dir()}
        if compute._bundle_skips(fn, names, root == os.path.join(rd, "data"), dp == os.path.join(rd, "models")):
            return False
        return os.path.getsize(rp) <= compute.DEFAULT_MAX_FILE_MB * 2 ** 20
    except OSError:          # Codex r3 P2: what cannot be verified is not shipped: the draws stay in-process
        return False


def _wire_safe(x) -> bool:
    """Whether a JSON round trip gives the same value back (tasks may cross to workers as JSON)."""
    try:
        return json.loads(json.dumps(x)) == x
    except (TypeError, ValueError):
        return False


def config(A) -> dict | None:
    """The compute settings for a fan-out, or None (in-process): direct evaluation only, with compute workers
    (MA_COMPUTE_SCHEDULER) or `draw_fanout: true` in the run's compute_kw, a run directory to ledger in, and an engine
    a worker reproduces (module docstring). A refused fan-out records its reason in A.draw_fanout_off and logs it once."""
    A.__dict__["draw_fanout_off"] = None
    if getattr(A.model, "kind", None) != "direct":
        return None
    kw = dict(getattr(A, "compute", None) or {})
    if not kw.get("run_dir"):
        return None
    if not (os.environ.get("MA_COMPUTE_SCHEDULER") or kw.get("draw_fanout") is True):
        return None
    off = None
    if getattr(A.eng, "batch_equals_run", False) is not True:
        off = "the engine does not declare batch_equals_run, so its results may depend on the worker process"
    else:
        out = [f for f in _engine_files(A) if not _shipped(f, kw["run_dir"])]
        if out:
            off = f"engine input files lie outside what compute.py ships to workers ({', '.join(out[:3])})"
        elif not _wire_safe([A.spec["engine"], A.model.attrs, dict(A.theta0)]):
            off = "the engine config, unit attributes or parameters change in a JSON round trip (non-text keys)"
    if off is not None:
        A.__dict__["draw_fanout_off"] = off
        if not A.__dict__.get("_draw_fanout_logged"):
            A.__dict__["_draw_fanout_logged"] = True
            (getattr(A, "log", None) or (lambda *a: None))(f"draw fan-out off, draws run in-process: {off}")
        return None
    return kw


def _canon(x):
    """A JSON-ready form both sides rebuild identically: mappings as [key text, key type, value] rows sorted by key
    text and type (unit attributes may mix key types, e.g. year maps), tuples as lists."""
    if isinstance(x, dict):
        return {"__map__": sorted(([str(k), type(k).__name__, _canon(v)] for k, v in x.items()),
                                  key=lambda r: (r[0], r[1]))}
    if isinstance(x, (list, tuple)):
        return [_canon(v) for v in x]
    return x


def _sha(x) -> str:
    return hashlib.sha256(json.dumps(_canon(x), default=str).encode()).hexdigest()


def _horizon(model) -> dict:
    """DirectModel.horizon as make_horizon's arguments (the worker rebuilds the same horizon)."""
    H = model.horizon
    d = getattr(H, "delivery", None)
    return {"start": H.start, "stop": H.stop, "window": list(H.window), "windows": dict(H.windows or {}),
            "delivery": None if d is None else list(d)}


def tasks(A, theta, zs) -> list[dict]:
    """One draw's engine runs as compute tasks, cut as DirectModel._runs cuts them."""
    m, sp = A.model, A.spec
    pairs = [(i, k) for i in A.ids for k in m.packages]
    batch = bool(getattr(m.eng, "batch_equals_run", False) and callable(getattr(m.eng, "run_batch", None)))
    size = m.BATCH if batch else PAIRS_PER_TASK
    hz = _horizon(m)
    out = []
    for a in range(0, len(pairs), size):
        mem = []
        for i, k in pairs[a:a + size]:
            u = m.unit_spec(i, zs.get(i) if zs else None)
            mem.append({"unit_id": u.id, "unit": dict(u.attributes), "package": k,
                        "components": sp.engine_package(m.pk[k])})
        out.append({"engine": sp["engine"], "horizon": hz, "seed": int(m.seed), "params": dict(theta),
                    "batch": batch, "members": mem})
    return out


def run_task(task: dict) -> dict:
    """The compute.py function: one task's engine runs, with the sha256 of its inputs and the engine identity."""
    from toolkit.analysis import batch as Bt
    from toolkit.engines.contract import Package, UnitSpec
    from toolkit.engines.identity import engine_identity
    from toolkit.engines.horizon import make_horizon
    eng = Bt._engine(task["engine"])
    hz = task["horizon"]
    h = make_horizon(hz["start"], hz["stop"], tuple(hz["window"]), hz.get("windows"), hz.get("delivery"))
    units = [UnitSpec(x["unit_id"], dict(x["unit"])) for x in task["members"]]
    pks = [Package(x["package"], dict(x["components"])) for x in task["members"]]
    th, sd = dict(task["params"]), int(task["seed"])
    if task["batch"]:
        outs = eng.run_batch([dict(th)] * len(units), units, pks, h, sd)
    else:
        outs = [eng.run(dict(th), u, p, h, sd) for u, p in zip(units, pks)]
    return {"rows": [{"per_capita": o.per_capita, "windows": o.windows, "cost_drivers": o.cost_drivers,
                      "meta": {k: o.meta[k] for k in META_KEYS if k in (o.meta or {})}} for o in outs],
            "task_sha": _sha(task), "engine": engine_identity(eng)}


def _row_ok(task, row, ident) -> bool:
    res = row.get("result") or {}
    return bool(row.get("ok")) and res.get("task_sha") == _sha(task) and res.get("engine") == ident \
        and len(res.get("rows") or []) == len(task["members"])


def wave_size(A) -> int:
    """Draws per wave: WAVE, fewer when a draw has many engine runs (WAVE_PAIRS bounds the outcomes held)."""
    n = max(1, len(A.ids) * len(A.model.packages))
    return max(1, min(WAVE, WAVE_PAIRS // n))


def prefetch(A, drawn, kw) -> list[dict]:
    """[{(unit, package): Outcomes}] for each (theta, zs) in `drawn`, from one compute call. Fails closed on a failed
    task, a task whose inputs or engine identity do not match, or a missing member."""
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import compute
    from toolkit.engines.contract import Outcomes
    from toolkit.engines.identity import engine_identity
    per = [tasks(A, th, zs) for th, zs in drawn]
    flat = [t for ts in per for t in ts]
    t0 = time.time()
    lim = int(kw["max_tasks"]) if kw.get("max_tasks") is not None else compute.DEFAULT_MAX_TASKS
    if lim < 1:          # Codex r2 P1-5: a non-positive limit is refused, never read as "no limit"
        raise ValueError(f"draw fan-out: max_tasks must be positive (got {kw.get('max_tasks')})")
    rows, summ, ncalls, nworkers = [], {}, 0, 0
    ident = engine_identity(A.eng)
    for a in range(0, len(flat), lim):       # Codex r1 P2-5: calls within the task limit, in draw order
        part = flat[a:a + lim]
        r_, summ = compute.map_fn(kw["run_dir"], os.path.join(ROOT, "toolkit", "analysis", "draw_fanout.py")
                                  + "::run_task", part, max_tasks=lim,
                                  purpose="v2 analysis: uncertainty draws (direct evaluation)",
                                  **{k: kw[k] for k in MAP_KEYS if k in kw})
        r_ = list(r_)
        ncalls += 1
        nworkers = max(nworkers, int(summ.get("workers") or 0))
        # Codex r2 P2, r3 P1-3: a slice is checked whole before anything is kept or the next one is sent
        bad = [n for n, (t, r) in enumerate(zip(part, r_)) if not _row_ok(t, r, ident)]
        if len(r_) != len(part) or bad:
            n = a + (bad[0] if bad else min(len(r_), len(part)))
            why = ((r_[bad[0]].get("error") or "inputs or engine identity differ") if bad
                   else f"{len(r_)} rows for {len(part)} tasks")
            raise ValueError(f"draw fan-out: task {n} of {len(flat)} failed or does not match its inputs ({why}); the "
                             "analysis fails closed")
        rows += r_
    if len(rows) != len(flat):
        raise ValueError(f"draw fan-out: {len(rows)} rows for {len(flat)} tasks; the analysis fails closed")
    out, n = [], 0
    for ts in per:
        runs = {}
        for t in ts:
            for x, o in zip(t["members"], rows[n]["result"]["rows"]):
                runs[(x["unit_id"], x["package"])] = Outcomes(**o)
            n += 1
        out.append(runs)
    st = A.__dict__.setdefault("draw_fanout", {"producer": PRODUCER, "backend": None, "calls": 0, "draws": 0,
                                               "tasks": 0, "wall_s": 0.0, "workers": 0})
    st.update(backend=summ.get("backend"), calls=st["calls"] + ncalls, draws=st["draws"] + len(drawn),
              tasks=st["tasks"] + len(flat), wall_s=round(st["wall_s"] + time.time() - t0, 3),
              workers=max(int(st["workers"] or 0), nworkers))
    return out


# --------------------------------------------------------------------------- self-test
def _toy(d):
    """The direct-path toy run (run._direct_toy, the package-rate engine)."""
    from toolkit.analysis import run as RUN
    RUN._direct_toy(d)


def _declared(flag: bool):
    """Self-test only: the package-rate engine declares batch_equals_run in this (coordinator) process. Its workers
    run each pair with run() (it has no run_batch), as the in-process path does."""
    from toolkit.engines.empirical.engine import PackageRateEngine
    if flag:
        PackageRateEngine.batch_equals_run = True
    else:
        PackageRateEngine.__dict__.get("batch_equals_run") is not None and delattr(PackageRateEngine, "batch_equals_run")


def _self_test() -> int:
    import tempfile
    import yaml
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)

    from toolkit.analysis import run as RUN
    from toolkit.spec import signoff
    q = lambda *a, **k: None  # noqa: E731
    sched = os.environ.pop("MA_COMPUTE_SCHEDULER", None)     # Codex r1 P3-9: the baseline really is in-process
    _declared(True)
    try:
        return _self_test_cases(ok, fails, RUN, signoff, q, tempfile, yaml)
    finally:
        _declared(False)
        if sched is not None:
            os.environ["MA_COMPUTE_SCHEDULER"] = sched


def _self_test_cases(ok, fails, RUN, signoff, q, tempfile, yaml) -> int:
    from toolkit.analysis.model import DirectModel
    from toolkit.analysis import draw_fanout as M        # the module the analysis imports (not __main__)
    keep = (DirectModel.BATCH, M.PAIRS_PER_TASK)
    DirectModel.BATCH = M.PAIRS_PER_TASK = 5      # several tasks per draw, so 3 workers really split a draw
    res = {}
    for label, kw in (("in-process", None), ("fan-out x1", {"draw_fanout": True, "backend": "local", "max_workers": 1}),
                      ("fan-out x3", {"draw_fanout": True, "backend": "local", "max_workers": 3})):
        with tempfile.TemporaryDirectory() as d:
            _toy(d)
            signoff.sign(d, "lead")
            R = RUN.run(d, log=q, compute_kw=kw)
            fo = os.path.join(d, REL)
            res[label] = {"unc": yaml.safe_load(open(os.path.join(d, "analysis", "uncertainty.yaml")))["data"],
                          "fd": yaml.safe_load(open(os.path.join(d, "analysis", "figure_data.yaml")))["data"]["replay"],
                          "fanout": json.load(open(fo)) if os.path.exists(fo) else None,
                          "draws": int(R["uncertainty"]["draws"])}
    DirectModel.BATCH, M.PAIRS_PER_TASK = keep
    a, b, c = res["in-process"], res["fan-out x1"], res["fan-out x3"]
    ok(a["fanout"] is None and b["fanout"] and b["fanout"]["backend"] == "local" and b["fanout"]["draws"] >= b["draws"]
       and b["fanout"]["tasks"] >= 2 * b["draws"] and c["fanout"]["workers"] >= 2,
       f"DF1 with draw_fanout the draws' engine runs go through compute.py ({b['fanout']}); without it, none do")
    ok(a["unc"] == b["unc"] == c["unc"],
       "DF2 the uncertainty artifact is identical in-process and fanned out over 1 or 3 workers")
    ok(a["fd"]["reproduced"] is True and b["fd"]["reproduced"] is True and c["fd"]["reproduced"] is True
       and b["fd"].get("method") == "stored_draws",
       "DF3 the figure step's check of the stored draws reproduces on the fan-out path too")
    # DF4: a task whose result does not match its inputs fails closed
    import types
    from toolkit.spec import schema
    from toolkit.analysis.core import Analysis
    with tempfile.TemporaryDirectory() as d:
        _toy(d)
        spec = schema.load(os.path.join(d, "decision_spec.yaml"))
        A = Analysis(spec, DirectModel(spec, log=q), log=q)
        A.compute = {"run_dir": d, "draw_fanout": True, "backend": "local", "max_workers": 1}
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import compute
        orig = compute.map_fn
        compute.map_fn = lambda rd, fn, inputs, **k: ([{"ok": True, "result": dict(run_task(t), task_sha="0")}
                                                       for t in inputs], {"backend": "local"})
        try:
            prefetch(A, [(dict(A.theta0), None)], A.compute)
            ok(False, "DF4 a task result whose input hash differs fails closed")
        except ValueError as e:
            ok("fails closed" in str(e), f"DF4 a task result whose input hash differs fails closed ({e})")
        finally:
            compute.map_fn = orig
        ok(config(types.SimpleNamespace(model=A.model, compute={"run_dir": d})) is None
           or bool(os.environ.get("MA_COMPUTE_SCHEDULER")),
           "DF5 without compute workers or draw_fanout the draws stay in-process")
        # DF6: prefetched runs are taken only for the evaluation they were computed for
        runs = prefetch(A, [(dict(A.theta0), None)], A.compute)[0]
        zs = {i: 0.25 for i in A.ids}
        A.model.use_runs(dict(A.theta0), None, runs)
        try:
            A.model.predict(zs, dict(A.theta0), A.ids)
            ok(False, "DF6 prefetched runs for other unit values are refused")
        except ValueError:
            ok(True, "DF6 prefetched runs for other unit values are refused")
        A.model.use_runs(dict(A.theta0), None, runs)
        got = A.model.predict(None, dict(A.theta0), A.ids)
        ok(A.model._pre is None and got == A.model.predict(None, dict(A.theta0), A.ids),
           "DF7 prefetched runs are taken once and give the in-process evaluation")
        A.model._pre = None
        # Codex r1 P1-1: unit attributes with mixed key types (the malaria engine's year maps) hash canonically
        try:
            h1, h2 = _sha({"u": {2014.3: "pbo", "2015.2": "dual"}}), _sha({"u": {"2015.2": "dual", 2014.3: "pbo"}})
            ok(h1 == h2, "DF8 a task whose unit attributes mix key types hashes, the same whatever the key order")
        except TypeError as e:
            ok(False, f"DF8 a task whose unit attributes mix key types hashes ({e})")
        # Codex r1 P1-2, P1-3: engines the fan-out cannot reproduce on a worker stay in-process, with the reason
        with tempfile.TemporaryDirectory() as elsewhere:      # Codex r2 P1-1: a private directory outside the run
            out = os.path.join(elsewhere, "fitted_outside.yaml")
            A.eng.fitted_path = out
            open(out, "w").write("{}")
            try:
                ok(config(A) is None and "outside" in (A.__dict__.get("draw_fanout_off") or ""),
                   f"DF9 an engine input file outside what compute.py ships keeps the draws in-process "
                   f"({A.__dict__.get('draw_fanout_off')})")
            finally:
                del A.eng.fitted_path
        # Codex r2 P1-3: a file under the run's models/ that compute.py's bundle leaves out (a .log) is not shipped
        os.makedirs(os.path.join(d, "models"), exist_ok=True)
        lg = os.path.join(d, "models", "bench.log")
        open(lg, "w").write("{}")
        ok(not _shipped(lg, d) and _shipped(os.path.join(ROOT, "toolkit", "analysis", "draw_fanout.py"), d),
           "DF16 a file is shipped only under compute.py's own bundle rules (a .log under models/ is not)")
        # Codex r3 P1-1: the name the engine opens is what the bundle filters, not a symlink's target
        yml = os.path.join(d, "models", "bench.yaml")
        open(yml, "w").write("{}")
        ln = os.path.join(d, "models", "bench2.log")
        os.symlink(yml, ln)
        ok(_shipped(yml, d) and not _shipped(ln, d), "DF20 a .log symlink to a shipped file is judged by its own name")
        # Codex r3 P1-1b: a directory named like a log's summary sidecar does not exempt the log
        os.makedirs(os.path.join(d, "models", "bench.log.summary.json"), exist_ok=True)
        ok(not _shipped(lg, d), "DF21 a directory named like a sidecar does not make a .log shipped")
        # Codex r2 P1-2: inputs a JSON round trip would change (non-text mapping keys) keep the draws in-process
        i0 = A.ids[0]
        keep_attrs = dict(A.model.attrs[i0])
        A.model.attrs[i0] = dict(keep_attrs, year_map={2014: "x"})
        try:
            ok(config(A) is None and "JSON" in (A.__dict__.get("draw_fanout_off") or ""),
               f"DF17 unit attributes a JSON round trip changes keep the draws in-process "
               f"({A.__dict__.get('draw_fanout_off')})")
        finally:
            A.model.attrs[i0] = keep_attrs
        # Codex r2 P1-5: a non-positive task limit is refused before anything is dispatched
        try:
            prefetch(A, [(dict(A.theta0), None)], dict(A.compute, max_tasks=0))
            ok(False, "DF18 max_tasks 0 is refused before dispatch")
        except ValueError as e:
            ok("max_tasks" in str(e), f"DF18 max_tasks 0 is refused before dispatch ({e})")
        # Codex r3 P1-3: a slice that returns more or fewer rows than it was given fails closed at once
        orig3 = compute.map_fn
        compute.map_fn = lambda rd, fn, inputs, **k: ([{"ok": True, "result": run_task(inputs[0])}] * 2,
                                                      {"backend": "local"})
        try:
            prefetch(A, [(dict(A.theta0), None)] * 2, dict(A.compute, max_tasks=1))
            ok(False, "DF22 a slice with the wrong number of rows fails closed")
        except ValueError as e:
            ok("fails closed" in str(e), f"DF22 a slice with the wrong number of rows fails closed ({e})")
        finally:
            compute.map_fn = orig3
        # Codex r2 P2: a failed slice stops the wave before the next compute call
        calls = []
        orig2 = compute.map_fn

        def failing(rd, fn, inputs, **k):
            calls.append(len(inputs))
            return [{"ok": False, "error": "model: boom"} for _ in inputs], {"backend": "local"}
        compute.map_fn = failing
        try:
            prefetch(A, [(dict(A.theta0), None)] * 3, dict(A.compute, max_tasks=1))
            ok(False, "DF19 a failed slice stops the wave")
        except ValueError:
            ok(len(calls) == 1, f"DF19 a failed slice stops the wave before the next compute call ({len(calls)} calls)")
        finally:
            compute.map_fn = orig2
        _declared(False)
        try:
            ok(config(A) is None and "batch_equals_run" in (A.__dict__.get("draw_fanout_off") or ""),
               f"DF10 an engine that does not declare batch_equals_run stays in-process "
               f"({A.__dict__.get('draw_fanout_off')})")
        finally:
            _declared(True)
        # Codex r1 P2-4: a task returns only the metadata direct prediction reads
        t0 = tasks(A, dict(A.theta0), None)[0]
        ok(all(set(r["meta"]) <= set(META_KEYS) for r in run_task(t0)["rows"]),
           f"DF11 task rows carry only the metadata the model reads ({META_KEYS})")
        # Codex r1 P2-5: a compute call never exceeds its task limit; dispatches are split in draw order
        try:
            two = prefetch(A, [(dict(A.theta0), None), (dict(A.theta0), None)], dict(A.compute, max_tasks=1))
            ok(len(two) == 2 and all(set(x) == set(runs) for x in two),
               "DF12 dispatches are split to respect max_tasks, in draw order")
        except ValueError as e:
            ok(False, f"DF12 dispatches are split to respect max_tasks ({e})")
        # Codex r1 P3-8: the stored-draws check logs to its caller's logger
        from toolkit.analysis import figure_data as FD
        msgs = []
        A.fit_calibration()
        from toolkit.analysis import uncertainty as UQ
        rows = list(UQ._draw_rows(A, _drawer(A, 2), 2, label="t", log=msgs.append))
        ok(len(rows) == 2 and any(m.startswith("t: 2/2") for m in msgs),
           f"DF13 per-draw progress goes to the logger the caller passes ({msgs})")
        del FD
    # Codex r1 P2-6, P2-7: analysis.workers caps the fan-out; a serial rerun removes the earlier fan-out record
    with tempfile.TemporaryDirectory() as d:
        _toy(d)
        p = os.path.join(d, "decision_spec.yaml")
        r = yaml.safe_load(open(p))
        r["analysis"]["workers"] = 1
        yaml.safe_dump(r, open(p, "w"), sort_keys=False)
        signoff.sign(d, "lead")
        R1 = RUN.run(d, log=q, compute_kw={"draw_fanout": True, "backend": "local"})
        had = os.path.exists(os.path.join(d, REL))
        ok(R1["_A"].compute.get("max_workers") == 1 and had,
           f"DF14 analysis.workers reaches the fan-out's compute calls as max_workers, as for the batch stage "
           f"({R1['_A'].compute.get('max_workers')})")
        RUN.run(d, log=q)
        ok(not os.path.exists(os.path.join(d, REL)),
           "DF15 a serial rerun in the same directory leaves no fan-out record from the earlier run")
    print("OK: all self-test cases passed." if not fails else f"FAILED: {len(fails)} case(s)")
    return 1 if fails else 0


def _drawer(A, nd):
    from toolkit.analysis import uncertainty as UQ
    D = UQ.Drawer(A, int(A.spec["uncertainty"].get("seed", 42)))
    D.prepare(nd)
    return D


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    print(__doc__)
