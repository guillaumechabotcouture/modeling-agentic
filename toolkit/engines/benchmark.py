#!/usr/bin/env python3
"""Benchmark-pack runner for any engine behind the contract (architecture-v2 §3.3).

    python3 -m toolkit.engines.benchmark <pack.yaml> fit   [--workers N] [--seeds K]
    python3 -m toolkit.engines.benchmark <pack.yaml> run   [--workers N] [--seeds K]
    python3 -m toolkit.engines.benchmark --self-test

A pack is validated once per engine version, never per question:
- `fit` runs the pack's `fit` steps in order (one engine parameter each, a grid, the
  benchmarks it is fitted to) and writes `fitted.yaml` beside the pack;
- `run` runs every benchmark with the fitted values and writes `results.yaml` (every
  benchmark, in band or not, with its role: `fit` if a fit step used it, else `held_out`).

Pack schema:
    engine: <module>:<class>            # e.g. toolkit.engines.malaria.engine:MalariaEngine
    seeds: [1, 2, 3, 4]                 # common random numbers: every arm uses the same seeds
    horizon: {start, stop, window: [t0, t1], windows: {name: [t0, t1]}}
    settings:                           # one simulated trial site (or reference setting)
      <name>:
        unit: {...}                     # engine unit attributes
        horizon: {...}                  # optional, replaces the pack horizon
        params: {...}                   # optional overrides of NON-fitted engine parameters
        calibrate: {attribute, arm_package, value_path, target, grid}   # optional: set one
                                        # unit attribute so the baseline matches the trial site
    arms: {<name>: {setting: <name>, package: {...}}}
    benchmarks:
      - id, metric, source, target, band: [lo, hi]
        estimand:
          {type: ratio | odds_ratio, num: arm | [arms], den: arm | [arms], value: <path>}
          {type: interaction_or, a11, a10, a01, a00, value: <path>}
          {type: cost_per_outcome, num, den, value: <path>, costs: {<driver>: <unit cost>}, costs_num: {<driver>: <incremental price>}}
          {type: curve_rules, arms: [...], x: <path>, y: <path>, rules: [{x_min, y_min} | {x_max, y_max}]}
    fit: [{param, grid: [...], targets: [ids]}]

Value paths: windows.<window>.<metric>, per_capita.<outcome>.<age group>,
per_capita_all.<outcome> (age groups weighted by the model population), cost.<driver>.
Ratios and odds ratios are geometric means over seeds of the per-seed CRN ratio; costs per
outcome use seed means. A setting may not override a fitted parameter (fail closed).
"""
from __future__ import annotations

import argparse
import importlib
import math
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import yaml

if __package__:
    from .contract import UnitSpec, Package, Horizon
else:  # pragma: no cover
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    from toolkit.engines.contract import UnitSpec, Package, Horizon  # noqa: E402

TYPES = ("ratio", "odds_ratio", "interaction_or", "cost_per_outcome", "curve_rules")


def load_engine(ref: str):
    mod, cls = ref.split(":")
    return getattr(importlib.import_module(mod), cls)()


def _h(d: dict) -> Horizon:
    return Horizon(float(d["start"]), float(d["stop"]), tuple(d["window"]), dict(d.get("windows") or {}))


def validate(pack: dict, engine) -> list[str]:
    bad = []
    specs = {p.name: p for p in engine.parameters()}
    fitted = {k for k, p in specs.items() if p.kind == "fitted"}
    for s, cfg in (pack.get("settings") or {}).items():
        for k in (cfg.get("params") or {}):
            if k not in specs:
                bad.append(f"setting {s}: unknown parameter {k}")
            elif k in fitted:
                bad.append(f"setting {s}: overrides fitted parameter {k} (no per-setting tuning)")
    arms = pack.get("arms") or {}
    for a, cfg in arms.items():
        if cfg.get("setting") not in (pack.get("settings") or {}):
            bad.append(f"arm {a}: unknown setting {cfg.get('setting')}")
    ids = set()
    for b in pack.get("benchmarks") or []:
        ids.add(b.get("id"))
        e = b.get("estimand") or {}
        if e.get("type") not in TYPES:
            bad.append(f"{b.get('id')}: estimand type {e.get('type')!r} not in {TYPES}")
            continue
        for a in _arms_of(e):
            if a not in arms:
                bad.append(f"{b.get('id')}: unknown arm {a}")
        if e["type"] != "curve_rules":
            band = b.get("band")
            if not (isinstance(band, list) and len(band) == 2 and band[0] <= b.get("target", float("nan")) <= band[1]):
                bad.append(f"{b.get('id')}: band must be [lo, hi] containing the target")
    for f in pack.get("fit") or []:
        if f.get("param") not in fitted:
            bad.append(f"fit step {f.get('param')}: not a fitted engine parameter")
        for t in f.get("targets") or []:
            if t not in ids:
                bad.append(f"fit step {f.get('param')}: unknown benchmark {t}")
    return bad


def _arms_of(e: dict) -> list:
    out = []
    for k in ("num", "den", "a11", "a10", "a01", "a00"):
        v = e.get(k)
        out += v if isinstance(v, list) else ([v] if v else [])
    return out + list(e.get("arms") or [])


def get(res: dict, path: str) -> float:
    kind, *rest = path.split(".")
    if kind == "windows":
        return float(res["windows"][rest[0]][rest[1]])
    if kind == "per_capita":
        return float(res["per_capita"][rest[0]][rest[1]])
    if kind == "per_capita_all":
        sh = res["meta"]["age_shares"]
        return float(sum(res["per_capita"][rest[0]][g] * w for g, w in sh.items()))
    if kind == "cost":
        return float(res["cost_drivers"][rest[0]])
    raise KeyError(f"bad value path {path}")


# ----------------------------------------------------------------------------- running
_ENGINE = {}


def _job(args):
    ref, params, unit, package, horizon, seed = args
    if ref not in _ENGINE:
        _ENGINE[ref] = load_engine(ref)
    eng = _ENGINE[ref]
    o = eng.run(params, UnitSpec("bench", unit), Package("p", package), _h(horizon), seed)
    pdays = o.meta.get("age_shares")
    if pdays is None:
        pdays = {g: 1.0 / len(o.per_capita[next(iter(o.per_capita))]) for g in o.per_capita[next(iter(o.per_capita))]}
    return {"per_capita": o.per_capita, "windows": o.windows, "cost_drivers": o.cost_drivers,
            "meta": dict(o.meta, age_shares=pdays)}


class Runner:
    def __init__(self, pack: dict, engine, workers: int = 1, seeds=None):
        self.pack, self.engine, self.workers = pack, engine, workers
        self.seeds = list(seeds or pack.get("seeds") or [1])
        self.ref = pack["engine"]
        self.cache = {}
        self.calibrated = {}
        self.nsims = 0

    def _map(self, jobs):
        if self.workers <= 1:
            return [_job(j) for j in jobs]
        with ProcessPoolExecutor(self.workers) as ex:
            return list(ex.map(_job, jobs, chunksize=1))

    def _setting(self, name):
        s = self.pack["settings"][name]
        return dict(s["unit"]), dict(s.get("horizon") or self.pack["horizon"]), dict(s.get("params") or {})

    def calibrate_settings(self, fitted: dict, names=None):
        """Settings with `calibrate`: pick the unit attribute value (log-linear interpolation on
        a grid) so the baseline arm reproduces the trial site's baseline value."""
        todo = [n for n, s in self.pack["settings"].items() if s.get("calibrate") and (names is None or n in names)]
        jobs, idx = [], []
        for n in todo:
            c = self.pack["settings"][n]["calibrate"]
            unit, hor, par = self._setting(n)
            for g in c["grid"]:
                for sd in self.seeds[:2]:
                    jobs.append((self.ref, dict(par, **fitted), dict(unit, **{c["attribute"]: g}), c.get("arm_package") or {}, hor, sd))
                    idx.append((n, g))
        res = self._map(jobs)
        self.nsims += len(jobs)
        for n in todo:
            c = self.pack["settings"][n]["calibrate"]
            ys = [np.mean([get(r, c["value_path"]) for (nn, gg), r in zip(idx, res) if nn == n and gg == g]) for g in c["grid"]]
            xs = np.log(np.asarray(c["grid"], float))
            ys = np.asarray(ys)
            order = np.argsort(ys)
            val = float(np.exp(np.interp(c["target"], ys[order], xs[order])))
            self.calibrated[n] = {"attribute": c["attribute"], "value": val, "grid": c["grid"],
                                  "baseline_values": [round(float(y), 4) for y in ys], "target": c["target"]}

    def arm_results(self, arms, fitted: dict):
        key = tuple(sorted(fitted.items()))
        jobs, need = [], []
        for a in arms:
            if (a, key) in self.cache:
                continue
            cfg = self.pack["arms"][a]
            unit, hor, par = self._setting(cfg["setting"])
            if cfg["setting"] in self.calibrated:
                c = self.calibrated[cfg["setting"]]
                unit[c["attribute"]] = c["value"]
            for sd in self.seeds:
                jobs.append((self.ref, dict(par, **fitted), unit, cfg.get("package") or {}, hor, sd))
            need.append(a)
        if jobs:
            res = self._map(jobs)
            self.nsims += len(jobs)
            k = len(self.seeds)
            for i, a in enumerate(need):
                self.cache[(a, key)] = res[i * k:(i + 1) * k]
        return {a: self.cache[(a, key)] for a in arms}

    def evaluate(self, b: dict, fitted: dict) -> dict:
        e = b["estimand"]
        R = self.arm_results(_arms_of(e), fitted)
        nseed = len(self.seeds)
        val = lambda a, i: get(R[a][i], e.get("value") or "")  # noqa: E731

        def pooled(arms, i):
            arms = arms if isinstance(arms, list) else [arms]
            return float(np.mean([val(a, i) for a in arms]))

        def odds(p):
            p = min(max(p, 1e-4), 1 - 1e-4)
            return p / (1 - p)
        out = {}
        if e["type"] == "ratio":
            per = [pooled(e["num"], i) / pooled(e["den"], i) for i in range(nseed)]
        elif e["type"] == "odds_ratio":
            per = [odds(pooled(e["num"], i)) / odds(pooled(e["den"], i)) for i in range(nseed)]
        elif e["type"] == "interaction_or":
            per = []
            for i in range(nseed):
                o = {k: odds(pooled(e[k], i)) for k in ("a11", "a10", "a01", "a00")}
                per.append((o["a11"] / o["a00"]) / ((o["a10"] / o["a00"]) * (o["a01"] / o["a00"])))
                out.setdefault("arm_or", []).append({k: round(o[k] / o["a00"], 4) for k in ("a11", "a10", "a01")})
        elif e["type"] == "cost_per_outcome":
            dv = np.mean([pooled(e["den"], i) - pooled(e["num"], i) for i in range(nseed)])
            dc = 0.0
            for drv, uc in (e.get("costs") or {}).items():       # unit cost x (driver in num - driver in den)
                dc += float(uc) * np.mean([get(R[e["num"]][i], f"cost.{drv}") - get(R[e["den"]][i], f"cost.{drv}")
                                           for i in range(nseed)])
            for drv, uc in (e.get("costs_num") or {}).items():   # incremental unit price x driver in num
                dc += float(uc) * np.mean([get(R[e["num"]][i], f"cost.{drv}") for i in range(nseed)])
            comp = float(dc / dv) if dv > 0 else float("inf")
            out.update(computed=comp, outcome_averted_per_capita=float(dv), cost_per_capita=float(dc))
            return self._verdict(b, out)
        else:  # curve_rules
            pts = []
            for a in e["arms"]:
                xs = [get(R[a][i], e["x"]) for i in range(nseed)]
                ys = [get(R[a][i], e["y"]) for i in range(nseed)]
                pts.append([round(float(np.mean(xs)), 3), round(float(np.mean(ys)), 4)])
            checks = []
            for x, y in pts:
                for r in e["rules"]:
                    if "x_min" in r and x >= r["x_min"]:
                        checks.append(y >= r["y_min"])
                    if "x_max" in r and x <= r["x_max"]:
                        checks.append(y <= r["y_max"])
            out.update(points=pts, rules_checked=len(checks), rules_met=int(sum(checks)),
                       computed=float(np.mean(checks)) if checks else float("nan"))
            out["in_band"] = bool(checks) and all(checks)
            return out
        per = np.asarray(per, float)
        out.update(computed=float(np.exp(np.mean(np.log(per)))), per_seed=[round(float(x), 4) for x in per],
                   se_log=float(np.std(np.log(per), ddof=1) / math.sqrt(len(per))) if len(per) > 1 else None)
        return self._verdict(b, out)

    @staticmethod
    def _verdict(b, out):
        lo, hi = b["band"]
        out["in_band"] = bool(lo <= out["computed"] <= hi)
        return out


def _loss(b, comp):
    lo, hi = b["band"]
    if not (comp > 0 and math.isfinite(comp)):
        return 1e6
    hw = (math.log(hi) - math.log(lo)) / 2
    return ((math.log(comp) - math.log(b["target"])) / hw) ** 2


def fit(pack: dict, engine, runner: Runner) -> dict:
    byid = {b["id"]: b for b in pack["benchmarks"]}
    fitted = {}
    out = {}
    for step in pack.get("fit") or []:
        prm, grid = step["param"], [float(g) for g in step["grid"]]
        settings = {pack["arms"][a]["setting"] for t in step["targets"] for a in _arms_of(byid[t]["estimand"])}
        runner.calibrate_settings(fitted, [s for s in settings if s not in runner.calibrated])
        losses, comps = [], []
        for g in grid:
            f = dict(fitted, **{prm: g})
            c = {t: runner.evaluate(byid[t], f)["computed"] for t in step["targets"]}
            comps.append(c)
            losses.append(sum(_loss(byid[t], c[t]) for t in step["targets"]))
        L = np.asarray(losses)
        # Refine on a fine grid: interpolate each benchmark's log value between grid points, then take
        # the value with the fewest out-of-band benchmarks, then the lowest loss. The range is where
        # every target is in band (else where the loss is within one band half-width per target).
        xs = np.linspace(grid[0], grid[-1], 801)
        n_out, Lf = np.zeros(len(xs)), np.zeros(len(xs))
        for t in step["targets"]:
            v = np.exp(np.interp(xs, grid, np.log(np.maximum([c[t] for c in comps], 1e-12))))
            lo_b, hi_b = byid[t]["band"]
            n_out += (v < lo_b) | (v > hi_b)
            Lf += np.array([_loss(byid[t], x) for x in v])
        j = int(np.argmin(n_out * 1e6 + Lf))
        best = float(xs[j])
        okx = xs[n_out == 0] if (n_out == 0).any() else xs[Lf <= float(len(step["targets"]))]
        lo, hi = (float(okx.min()), float(okx.max())) if len(okx) else (best, best)
        fitted[prm] = round(best, 4)
        out[prm] = {"value": round(best, 4), "lo": round(min(lo, best), 4), "hi": round(max(hi, best), 4),
                    "all_targets_in_band_at_value": bool(n_out[j] == 0),
                    "benchmarks": list(step["targets"]), "grid": grid, "loss": [round(float(x), 3) for x in L],
                    "computed_on_grid": [{k: round(v, 4) for k, v in c.items()} for c in comps]}
        print(f"fit {prm}: {best:.4f} [{lo:.3f}, {hi:.3f}] all in band: {bool(n_out[j] == 0)}; "
              f"grid loss {np.round(L, 2).tolist()}", flush=True)
    return out


def run_pack(pack: dict, engine, runner: Runner, fitted: dict) -> dict:
    fit_ids = {t for s in pack.get("fit") or [] for t in s["targets"]}
    runner.calibrate_settings(fitted)
    rows = []
    for b in pack["benchmarks"]:
        r = runner.evaluate(b, fitted)
        row = {"id": b["id"], "metric": b["metric"], "role": "fit" if b["id"] in fit_ids else "held_out",
               "target": b.get("target"), "band": b.get("band"), "source": b.get("source", "")}
        row.update({k: (round(v, 4) if isinstance(v, float) else v) for k, v in r.items()})
        rows.append(row)
        print(f"{b['id']:6s} {row['role']:8s} computed {r['computed']:.4g} target {b.get('target')} band {b.get('band')} "
              f"{'IN' if r['in_band'] else 'OUT'}", flush=True)
    return {"benchmarks": rows, "settings_calibrated": runner.calibrated, "fitted_used": fitted,
            "seeds": runner.seeds, "n_sims": runner.nsims,
            "summary": {"in_band": sum(r["in_band"] for r in rows), "total": len(rows),
                        "held_out_in_band": sum(r["in_band"] for r in rows if r["role"] == "held_out"),
                        "held_out_total": sum(r["role"] == "held_out" for r in rows)}}


PRODUCER = "toolkit/engines/benchmark.py"


def stamp(out: dict, engine) -> dict:
    """Provenance on a results or fitted file: who wrote it, for which engine and code (Codex P1-13)."""
    from toolkit.engines.contract import engine_identity
    return {"producer": PRODUCER, "engine_identity": engine_identity(engine), **out}


def verified_benchmarks(engine) -> tuple[list, str | None]:
    """(benchmark rows, None) when the engine's results file was written by this runner for this engine's current
    code; else ([], why). Decision tests read benchmark arm ratios only from here."""
    from toolkit.engines.contract import engine_identity
    path = getattr(engine, "results_path", None)
    if not path or not os.path.exists(path):
        return [], "the engine has no benchmark results file"
    try:
        doc = yaml.safe_load(open(path)) or {}
    except (OSError, yaml.YAMLError) as e:
        return [], f"benchmark results unreadable: {e}"
    if doc.get("producer") != PRODUCER:
        return [], f"{os.path.basename(path)} was not written by {PRODUCER} (no provenance): re-run the pack"
    if doc.get("engine_identity") != engine_identity(engine):
        return [], f"{os.path.basename(path)} was computed for another engine identity or code: re-run the pack"
    return list(doc.get("benchmarks") or []), None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pack")
    ap.add_argument("action", choices=["fit", "run", "validate"])
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 1)
    ap.add_argument("--seeds", type=int, default=0, help="use the first K seeds of the pack (0 = all)")
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    pack = yaml.safe_load(open(a.pack))
    eng = load_engine(pack["engine"])
    bad = validate(pack, eng)
    if bad:
        print("pack invalid:\n  " + "\n  ".join(bad))
        return 2
    if a.action == "validate":
        print("pack valid")
        return 0
    seeds = pack["seeds"][:a.seeds] if a.seeds else pack["seeds"]
    runner = Runner(pack, eng, a.workers, seeds)
    d = os.path.dirname(os.path.abspath(a.pack))
    t0 = time.time()
    if a.action == "fit":
        out = fit(pack, eng, runner)
        path = a.out or os.path.join(d, "fitted.yaml")
        yaml.safe_dump(stamp({"engine": pack["engine"], "fitted_at": time.strftime("%F %T"), "seeds": seeds,
                              "n_sims": runner.nsims, "wall_s": round(time.time() - t0, 1), "parameters": out}, eng),
                       open(path, "w"), sort_keys=False)
    else:
        f = yaml.safe_load(open(os.path.join(d, "fitted.yaml"))) if os.path.exists(os.path.join(d, "fitted.yaml")) else {}
        fitted = {k: v["value"] for k, v in (f.get("parameters") or {}).items()}
        out = run_pack(pack, eng, runner, fitted)
        out["wall_s"] = round(time.time() - t0, 1)
        path = a.out or os.path.join(d, "results.yaml")
        yaml.safe_dump(stamp(out, eng), open(path, "w"), sort_keys=False)
    print(f"wrote {path} ({runner.nsims} sims, {time.time() - t0:.0f}s)")
    return 0


def _self_test() -> int:
    fails = []

    def ok(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    pack = {
        "engine": "toolkit.engines.contract:ToyEngine", "seeds": [1, 2],
        "horizon": {"start": 0, "stop": 3, "window": [0, 3], "windows": {"w": [1, 2]}},
        "settings": {"s": {"unit": {"x": 0.0}},
                     "c": {"unit": {"x": 0.0}, "calibrate": {"attribute": "x", "value_path": "windows.w.prev",
                                                             "target": 0.6, "grid": [0.5, 1.0, 2.0]}}},
        "arms": {"none": {"setting": "s", "package": {}}, "a": {"setting": "s", "package": {"a": 1}},
                 "b": {"setting": "s", "package": {"b": 1}}, "ab": {"setting": "s", "package": {"a": 1, "b": 1}},
                 "c0": {"setting": "c", "package": {}}},
        "benchmarks": [
            {"id": "T1", "metric": "a vs none", "target": 0.4, "band": [0.3, 0.5],
             "estimand": {"type": "ratio", "num": "a", "den": "none", "value": "per_capita.cases.all"}},
            {"id": "T2", "metric": "interaction", "target": 1.0, "band": [0.8, 1.25],
             "estimand": {"type": "interaction_or", "a11": "ab", "a10": "a", "a01": "b", "a00": "none", "value": "windows.w.prev"}},
            {"id": "T3", "metric": "cost per case", "target": 2.0, "band": [0.1, 100],
             "estimand": {"type": "cost_per_outcome", "num": "a", "den": "none", "value": "per_capita.cases.all",
                          "costs": {"a_doses": 1.0}}},
            {"id": "T4", "metric": "curve", "estimand": {"type": "curve_rules", "arms": ["none", "c0"], "x": "windows.w.prev",
                                                        "y": "windows.w.prev", "rules": [{"x_min": 0.3, "y_min": 0.3}]}}],
        "fit": [{"param": "eff_a", "grid": [0.3, 0.45, 0.6, 0.7], "targets": ["T1"]}]}
    eng = load_engine(pack["engine"])
    ok(validate(pack, eng) == [], "BP1 a well-formed pack validates")
    bad = dict(pack, settings=dict(pack["settings"], s={"unit": {"x": 0.0}, "params": {"eff_a": 0.9}}))
    ok(any("fitted parameter" in s for s in validate(bad, eng)), "BP2 a setting that overrides a fitted parameter is refused")
    bad2 = dict(pack, benchmarks=pack["benchmarks"] + [{"id": "T9", "metric": "m", "target": 3.0, "band": [0.1, 1.0],
                                                         "estimand": {"type": "ratio", "num": "a", "den": "zz", "value": "x"}}])
    probs = validate(bad2, eng)
    ok(any("unknown arm" in s for s in probs) and any("containing the target" in s for s in probs),
       "BP3 unknown arms and a band that excludes its target are refused")
    r = Runner(pack, eng, 1)
    f = fit(pack, eng, r)
    ok(abs(f["eff_a"]["value"] - 0.6) < 0.02 and f["eff_a"]["lo"] <= 0.6 <= f["eff_a"]["hi"],
       f"BP4 the fit recovers the parameter that reproduces the target (eff_a 0.6): {f['eff_a']['value']}")
    res = run_pack(pack, eng, r, {"eff_a": f["eff_a"]["value"]})
    rows = {x["id"]: x for x in res["benchmarks"]}
    ok(rows["T1"]["role"] == "fit" and rows["T2"]["role"] == "held_out", "BP5 roles: fitted vs held-out benchmarks")
    ok(abs(rows["T2"]["computed"] - 1.0) > 0.01, "BP6 interaction OR of a multiplicative-in-burden engine is not forced to 1 on the odds scale")
    ok(abs(rows["T3"]["computed"] - 3 / (1.5 * f["eff_a"]["value"])) < 1e-3, f"BP7 cost per outcome = cost difference / outcome averted ({rows['T3']['computed']})")
    ok(abs(res["settings_calibrated"]["c"]["value"] - 0.758) < 0.08, "BP8 a setting is calibrated to its baseline target")
    ok(rows["T4"]["rules_checked"] >= 1, "BP9 curve rules are checked")
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        eng.results_path = os.path.join(d, "results.yaml")
        yaml.safe_dump(res, open(eng.results_path, "w"))
        ok(verified_benchmarks(eng)[1] and "provenance" in verified_benchmarks(eng)[1],
           "BP10 results without provenance are not verified (Codex P1-13)")
        yaml.safe_dump(stamp(res, eng), open(eng.results_path, "w"))
        rows2, why = verified_benchmarks(eng)
        ok(why is None and len(rows2) == len(res["benchmarks"]), f"BP11 stamped results for this engine verify: {why}")
        doc = yaml.safe_load(open(eng.results_path))
        doc["engine_identity"]["code_sha256"] = "0" * 64
        yaml.safe_dump(doc, open(eng.results_path, "w"))
        ok("another engine" in (verified_benchmarks(eng)[1] or ""), "BP12 results for other engine code are refused")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    sys.exit(main())
