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
MAP_KEYS = ("backend", "max_workers", "max_tasks", "timeout_s", "ledger", "max_core_hours")


def config(A) -> dict | None:
    """The compute settings for a fan-out, or None (in-process): direct evaluation only, with compute workers
    (MA_COMPUTE_SCHEDULER) or `draw_fanout: true` in the run's compute_kw, and a run directory to ledger in."""
    if getattr(A.model, "kind", None) != "direct":
        return None
    kw = dict(getattr(A, "compute", None) or {})
    if not kw.get("run_dir"):
        return None
    if not (os.environ.get("MA_COMPUTE_SCHEDULER") or kw.get("draw_fanout") is True):
        return None
    return kw


def _sha(x) -> str:
    return hashlib.sha256(json.dumps(x, sort_keys=True, default=str).encode()).hexdigest()


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
    from toolkit.engines.contract import Package, UnitSpec, engine_identity
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
                      "meta": o.meta} for o in outs], "task_sha": _sha(task), "engine": engine_identity(eng)}


def prefetch(A, drawn, kw) -> list[dict]:
    """[{(unit, package): Outcomes}] for each (theta, zs) in `drawn`, from one compute call. Fails closed on a failed
    task, a task whose inputs or engine identity do not match, or a missing member."""
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import compute
    from toolkit.engines.contract import Outcomes, engine_identity
    per = [tasks(A, th, zs) for th, zs in drawn]
    flat = [t for ts in per for t in ts]
    t0 = time.time()
    rows, summ = compute.map_fn(kw["run_dir"], os.path.join(ROOT, "toolkit", "analysis", "draw_fanout.py") + "::run_task",
                                flat, purpose="v2 analysis: uncertainty draws (direct evaluation)",
                                **{k: kw[k] for k in MAP_KEYS if k in kw})
    ident = engine_identity(A.eng)
    bad = [n for n, (t, r) in enumerate(zip(flat, rows)) if not r.get("ok") or (r.get("result") or {}).get("task_sha")
           != _sha(t) or (r.get("result") or {}).get("engine") != ident
           or len((r.get("result") or {}).get("rows") or []) != len(t["members"])]
    if len(rows) != len(flat) or bad:
        n = bad[0] if bad else len(rows)
        why = (rows[n].get("error") if n < len(rows) else "missing row") or "inputs or engine identity differ"
        raise ValueError(f"draw fan-out: {len(bad) or len(flat) - len(rows)} of {len(flat)} tasks failed or do not "
                         f"match their inputs (e.g. task {n}: {why}); the analysis fails closed")
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
    st.update(backend=summ.get("backend"), calls=st["calls"] + 1, draws=st["draws"] + len(drawn),
              tasks=st["tasks"] + len(flat), wall_s=round(st["wall_s"] + time.time() - t0, 3),
              workers=max(int(st["workers"] or 0), int(summ.get("workers") or 0)))
    return out


# --------------------------------------------------------------------------- self-test
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
    from toolkit.analysis.model import DirectModel
    from toolkit.analysis import draw_fanout as M        # the module the analysis imports (not __main__)
    keep = (DirectModel.BATCH, M.PAIRS_PER_TASK)
    DirectModel.BATCH = M.PAIRS_PER_TASK = 5      # several tasks per draw, so 3 workers really split a draw
    res = {}
    for label, kw in (("in-process", None), ("fan-out x1", {"draw_fanout": True, "backend": "local", "max_workers": 1}),
                      ("fan-out x3", {"draw_fanout": True, "backend": "local", "max_workers": 3})):
        with tempfile.TemporaryDirectory() as d:
            RUN._direct_toy(d)
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
        RUN._direct_toy(d)
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
    print("OK: all self-test cases passed." if not fails else f"FAILED: {len(fails)} case(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    print(__doc__)
