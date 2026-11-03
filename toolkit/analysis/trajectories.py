#!/usr/bin/env python3
"""The trajectory step: modelled monthly series for a stratified sample of units (docs/design/v2-figures.md §5).

    python -m toolkit.analysis.trajectories run <run_dir> [--per-region 3] [--seeds 3] [--metrics a,b] [--workers N]
    python3 toolkit/analysis/trajectories.py --self-test

For a finished run: per region, the units at the lowest, median and highest comparator burden
(analysis/figure_data.yaml `burden`; fewer when the region has fewer units) are sampled and recorded. For each
sampled unit and seed, one compute task runs the comparator and the unit's plan package through the engine's
`run_series` hook (toolkit/engines/hooks.py) at the point parameters and the unit's calibrated attribute, through
scripts/compute.py `run_map` (the same path as the batch, so a real run sends it to Coiled; rows are kept per
input, so a stopped step resumes). It writes analysis/trajectories.yaml (the analysis envelope; the manifest is
not touched): the sample, the series per unit (mean over seeds), and population-weighted series per region and in
total, with the calibration target per region where the metric is the calibration metric. An engine without the
hook gives `status: not_offered` with the reason, and the figure is recorded as not produced.

Cost: tasks = sampled units x seeds, each one shared-history run with two arms; at 3 units per region and 3 seeds
that is 9 tasks per region. Nothing here names a disease, component or unit.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys

import numpy as np
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

PRODUCER = "toolkit/analysis/trajectories.py"
OUT = os.path.join("analysis", "trajectories.yaml")
ROWS = os.path.join("analysis", "trajectories", "series.jsonl")


def _data(run_dir, kind):
    p = os.path.join(run_dir, "analysis", f"{kind}.yaml")
    if not os.path.exists(p):
        raise FileNotFoundError(f"needs analysis/{kind}.yaml")
    return yaml.safe_load(open(p))["data"]


def sample_units(units: dict, burden: dict, per_region: int) -> dict:
    """{region: [unit ids]}: per region, `per_region` units spread over the comparator-burden order (lowest, median,
    highest for 3), deterministic. `units`: {id: region}."""
    out = {}
    for r in sorted(set(units.values())):
        ids = sorted((i for i, rr in units.items() if rr == r), key=lambda i: (burden.get(i, 0.0), i))
        if len(ids) <= per_region:
            out[r] = ids
            continue
        pos = sorted({int(round(k * (len(ids) - 1) / max(per_region - 1, 1))) for k in range(per_region)})
        out[r] = [ids[p] for p in pos]
    return out


def tasks(run_dir: str, per_region: int = 3, seeds: int = 3, metrics=None) -> tuple[list[dict], dict]:
    """(compute inputs, plan of the step) for a finished run. Raises hooks.HookUnsupported without the hook."""
    from toolkit.analysis.batch import calibrated_value
    from toolkit.analysis.core import Analysis
    from toolkit.engines import hooks
    from toolkit.spec import schema
    spec = schema.load(os.path.join(run_dir, "decision_spec.yaml"))
    eng = spec.engine()
    if not hooks.supports(eng, "series"):
        raise hooks.HookUnsupported("run_series", eng)
    known = hooks.series_metrics(eng)
    metrics = list(metrics or known)
    point, cal, fd = _data(run_dir, "point"), _data(run_dir, "calibration"), _data(run_dir, "figure_data")
    plan = {str(k): v for k, v in point["plan"].items()}
    A = Analysis(spec, None, log=lambda *a, **k: None)
    theta = dict(A.theta0, **(cal.get("theta") or {}))
    reg = spec["decision_units"].get("region")
    units = {i: str(A.by_id[i].get(reg, "")) if reg else "" for i in A.ids}
    burden = {i: (v or {}).get("per_person") or 0.0 for i, v in (fd.get("burden") or {}).items()}
    smp = sample_units(units, burden, per_region)
    h = spec.horizon({})
    hz = {"start": float(h.start), "stop": float(h.stop), "window": [float(x) for x in h.window]}
    if getattr(h, "delivery", None) is not None:
        hz["delivery"] = [float(x) for x in h.delivery]
    cal_attr = spec["calibration"].get("attribute")
    seed0 = int((spec.get("uncertainty") or {}).get("seed", 42))
    out = []
    for r, ids in smp.items():
        for i in ids:
            a = dict(spec.unit_attributes(A.by_id[i]))
            z = ((cal.get("units") or {}).get(i) or {}).get("z")
            if cal_attr and z is not None:
                a[cal_attr] = calibrated_value(spec, z)
            arms = [[A.comp, spec.engine_package(A.pk[A.comp])], [plan[i], spec.engine_package(A.pk[plan[i]])]]
            for s in range(seeds):
                out.append({"unit": i, "region": r, "attributes": a, "arms": arms, "params": theta,
                            "engine": spec["engine"],     # engine: auto is resolved to the chosen engine on load
                            "horizon": hz, "seed": seed0 + 1000 * s + 7, "metrics": metrics})
    step = {"sample": smp, "per_region": per_region, "seeds": seeds, "metrics": metrics,
            "metric_info": {m: known[m] for m in metrics}, "plan": plan, "units": units}
    return out, step


def run_task(task: dict) -> dict:
    """The compute.py function: one unit and seed, both arms, through the engine's run_series hook."""
    from toolkit.analysis.batch import _engine
    from toolkit.engines import hooks
    from toolkit.engines.contract import Package, UnitSpec
    from toolkit.engines.horizon import make_horizon
    eng = _engine(task["engine"])
    hz = task["horizon"]
    h = make_horizon(hz["start"], hz["stop"], hz["window"], {}, hz.get("delivery"))
    pk = [Package(a, c) for a, c in task["arms"]]
    res = hooks.run_series(eng, task["params"], UnitSpec(task["unit"], task["attributes"]), pk, h, task["seed"],
                           task["metrics"])
    return {"unit": task["unit"], "seed": task["seed"], "series": res}


def aggregate(run_dir: str, step: dict, results: list[dict]) -> dict:
    """Seed means per unit and arm, then population-weighted means per region and in total."""
    from toolkit.spec import schema
    spec = schema.load(os.path.join(run_dir, "decision_spec.yaml"))
    pops = spec["decision_units"]["populations"]
    by_id = {str(u[spec.uid]): u for u in spec.units()}
    w = {i: sum(float(by_id[i][pm["column"]]) for pm in pops.values()) for i in by_id}
    metrics = step["metrics"]
    acc = {}
    t = None
    for r in results:
        for k, arm in enumerate(("comparator", "plan")):
            s = r["series"][k]
            t = t or s["t"]
            e = acc.setdefault(r["unit"], {}).setdefault(arm, {m: [] for m in metrics})
            for m in metrics:
                e[m].append(s["values"][m])
    per_unit = {i: {arm: {m: list(np.mean(np.asarray(v[m], float), axis=0)) for m in metrics} for arm, v in a.items()}
                for i, a in acc.items()}

    def wmean(ids):
        tw = sum(w[i] for i in ids)
        return {arm: {m: list(np.sum([np.asarray(per_unit[i][arm][m]) * w[i] for i in ids], axis=0) / tw)
                      for m in metrics} for arm in ("comparator", "plan")}
    cal = spec.get("calibration") or {}
    tgt_col, cal_metric = cal.get("target_column"), cal.get("metric")
    sd = float((cal.get("target_uncertainty") or {}).get("region_sd_logit") or 0.0)

    def target(ids):
        if not tgt_col or cal_metric not in metrics:
            return {}
        v = np.array([float(by_id[i][tgt_col]) for i in ids])
        ws = np.array([w[i] for i in ids])
        c = float(np.sum(v * ws) / np.sum(ws))
        lg = np.log(c / (1 - c))
        il = lambda x: float(1 / (1 + np.exp(-x)))  # noqa: E731
        win = cal.get("window") or [None, None]
        return {"value": c, "lo": il(lg - 1.96 * sd), "hi": il(lg + 1.96 * sd),
                "t": (float(win[0]) + float(win[1])) / 2 if win[0] is not None else None}
    regions = {r: {"t": t, "arms": wmean(ids), "target": target(ids), "units": ids}
               for r, ids in step["sample"].items() if ids}
    allids = [i for ids in step["sample"].values() for i in ids]
    return {"total": {"t": t, "arms": wmean(allids), "target": target(allids), "units": allids}, "regions": regions,
            "per_unit": per_unit}


def write(run_dir: str, data: dict, inputs: list[str]) -> str:
    from toolkit.analysis.artifacts import plain, sha256
    doc = {"schema": "analysis.trajectories/1", "produced_by": "harness", "producer": PRODUCER,
           "inputs": {p: sha256(os.path.join(run_dir, p)) for p in inputs},
           "written_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"), "data": plain(data)}
    p = os.path.join(run_dir, OUT)
    with open(p, "w") as f:
        yaml.safe_dump(doc, f, sort_keys=False, width=120)
    return OUT


def run(run_dir: str, per_region: int = 3, seeds: int = 3, metrics=None, log=print, **compute_kw) -> dict:
    from toolkit.engines import hooks
    run_dir = os.path.abspath(run_dir)
    inputs = ["decision_spec.yaml", "analysis/point.yaml", "analysis/calibration.yaml", "analysis/figure_data.yaml"]
    try:
        tk, step = tasks(run_dir, per_region, seeds, metrics)
    except hooks.HookUnsupported as e:
        data = {"status": "not_offered", "reason": str(e), "sample": {}, "series": {}}
        write(run_dir, data, [p for p in inputs if os.path.exists(os.path.join(run_dir, p))])
        log(f"trajectories: {e}")
        return data
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import compute
    out = os.path.join(run_dir, ROWS)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    log(f"trajectories: {len(tk)} tasks ({sum(len(v) for v in step['sample'].values())} units x {seeds} seeds)")
    rows, summ = compute.run_map(run_dir, os.path.join(ROOT, "toolkit", "analysis", "trajectories.py") + "::run_task",
                                 tk, out, purpose="v2 analysis: trajectory step (figures)", **compute_kw)
    bad = [r for r in rows if not r.get("ok")]
    if bad:
        raise RuntimeError(f"trajectories: {len(bad)} of {len(rows)} tasks failed (e.g. {str(bad[0])[:300]})")
    series = aggregate(run_dir, step, [r["result"] for r in rows])
    from toolkit.spec import schema
    spec = schema.load(os.path.join(run_dir, "decision_spec.yaml"))
    h = spec.horizon({})
    d = getattr(h, "delivery", None) or h.window
    data = {"status": "ok", "sample": step["sample"], "per_region": per_region, "seeds": seeds,
            "metrics": step["metrics"], "metric_info": step["metric_info"],
            "calibration_metric": (spec.get("calibration") or {}).get("metric"),
            "delivery": [float(x) for x in d], "window": [float(x) for x in h.window], "series": series,
            "compute": {k: summ.get(k) for k in ("backend", "workers", "n", "n_failed", "wall_s")}}
    write(run_dir, data, inputs)
    return data


# --------------------------------------------------------------------------- self-test
def _self_test() -> int:
    import tempfile
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)

    smp = sample_units({f"u{i}": f"r{i % 2}" for i in range(10)}, {f"u{i}": float(i) for i in range(10)}, 3)
    ok(smp == {"r0": ["u0", "u4", "u8"], "r1": ["u1", "u5", "u9"]},
       f"TJ1 per region: the lowest, median and highest burden units ({smp})")
    ok(sample_units({"a": "r", "b": "r"}, {}, 3) == {"r": ["a", "b"]}, "TJ2 a small region gives all its units")
    from toolkit.analysis import run as RUN
    from toolkit.spec import schema, signoff
    q = lambda *a, **k: None  # noqa: E731
    keep = os.environ.get("TRAJ_SELFTEST_KEEP")          # a directory to keep the rendered figures in (optional)
    with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as figs:
        figs = keep or figs
        bench, v_ok, v_bad = RUN._toy_bench(d)
        schema.toy_spec(d, n=12, bench=bench, dt_values=(v_ok, v_bad))
        signoff.sign(d, "lead", note="self-test")
        RUN.run(d, log=q, compute_kw={"max_workers": 2})
        res = run(d, log=q)
        ok(res["status"] == "not_offered" and "run_series()" in res["reason"]
           and os.path.exists(os.path.join(d, OUT)),
           f"TJ3 an engine without the series hook records not_offered with the reason: {res.get('reason')}")
        from toolkit.analysis import figures as FG
        r = FG.render(d, os.path.join(figs, "a"))
        why = {x["figure"]: x["reason"] for x in r["not_produced"]}.get("trajectories", "")
        ok("needs trajectory runs" in why and "run_series()" in why,
           f"TJ4 the figure is not produced and says why: {why}")
        # the same run with an engine that offers the hook (the toy engine plus hooks)
        sp = os.path.join(d, "decision_spec.yaml")
        raw = yaml.safe_load(open(sp))
        raw["engine"]["ref"] = "toolkit.engines.hooks:ToyScheduleEngine"
        yaml.safe_dump(raw, open(sp, "w"), sort_keys=False)
        res = run(d, per_region=2, seeds=2, log=q, max_workers=2)
        tot = res["series"]["total"]
        n_units = sum(len(v) for v in res["sample"].values())
        ok(res["status"] == "ok" and n_units == 6 and len(tot["t"]) == 36
           and set(tot["arms"]) == {"comparator", "plan"},
           f"TJ5 with the hook: {n_units} sampled units, monthly series for both arms, per region and in total")
        pre = [k for k, tt in enumerate(tot["t"]) if tt < res["delivery"][0]]
        same_before = all(abs(tot["arms"]["comparator"]["prev"][k] - tot["arms"]["plan"]["prev"][k]) < 1e-12
                          for k in pre)
        after = [k for k, tt in enumerate(tot["t"]) if tt >= res["delivery"][0]]
        lower_after = all(tot["arms"]["plan"]["prev"][k] <= tot["arms"]["comparator"]["prev"][k] + 1e-12 for k in after)
        ok(same_before and lower_after, "TJ6 the arms agree before delivery and the plan is not above the comparator after")
        ok(res["series"]["total"]["target"].get("value") is not None, "TJ7 the calibration target is carried per panel")
        r2 = FG.render(d, os.path.join(figs, "b"))
        ok(any(f["figure"] == "trajectories" for f in r2["figures"]), "TJ8 the trajectories figure is produced")
    print("OK: all self-test cases passed." if not fails else f"FAILED: {len(fails)} case(s)")
    return 1 if fails else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m toolkit.analysis.trajectories")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("run_dir")
    r.add_argument("--per-region", type=int, default=3)
    r.add_argument("--seeds", type=int, default=3)
    r.add_argument("--metrics", help="comma-separated series metrics (default: every metric the card lists)")
    r.add_argument("--workers", type=int, default=2)
    a = ap.parse_args(argv)
    res = run(a.run_dir, a.per_region, a.seeds, a.metrics.split(",") if a.metrics else None, max_workers=a.workers)
    print(json.dumps({"status": res["status"], "reason": res.get("reason"),
                      "units": sum(len(v) for v in res.get("sample", {}).values())}))
    return 0 if res["status"] == "ok" else 2


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    sys.exit(main())
