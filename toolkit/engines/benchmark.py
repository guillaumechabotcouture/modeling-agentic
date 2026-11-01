#!/usr/bin/env python3
"""Benchmark-pack runner for any engine behind the contract (architecture-v2 §3.3).

    python3 -m toolkit.engines.benchmark <pack.yaml> fit   [--workers N] [--seeds K]
    python3 -m toolkit.engines.benchmark <pack.yaml> run   [--workers N] [--seeds K]
    python3 -m toolkit.engines.benchmark <pack.yaml> validate
    python3 -m toolkit.engines.benchmark --self-test

A pack is validated once per engine version, never per question:
- `fit` fits the engine-level parameters once and writes `fitted.yaml` beside the pack:
  with `fit_joint` (preferred), all parameters jointly over a design of their box, with a
  feasibility report before any value is chosen; with the legacy `fit` list, one parameter
  at a time in order;
- `run` runs every benchmark with the fitted values and writes `results.yaml`: every
  benchmark with its role (`fit` if the fit used it, else `held_out`), its status and its
  per-seed values.

Statuses (never fitted toward unless `in_band`/`out_of_band`):
    in_band          computed value inside the band
    out_of_band      computed value outside the band
    not_assessable   a benchmark it `requires` is outside the stated interval (e.g. an
                     interaction whose single-arm effects miss the trial's)
    band_mis_based   a cost-per-outcome band whose upper edge is below the price floor:
                     even an intervention that averted ALL of the comparator's outcome would
                     cost more per unit averted than the band allows at the pack's prices

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
        requires: {<id>: [lo, hi]}      # optional: assessable only if each is inside [lo, hi]
        estimand:
          {type: ratio | odds_ratio, num: arm | [arms], den: arm | [arms], value: <path>}
          {type: interaction_or, a11, a10, a01, a00, value: <path>}
          {type: cost_per_outcome, num, den, value: <path>, costs: {<driver>: <unit cost>}, costs_num: {<driver>: <incremental price>}}
          {type: curve_rules, arms: [...], x: <path>, y: <path>, rules: [{x_min, y_min} | {x_max, y_max}]}
          {type: value | identity, a: <side>, b: <side>, tol: {abs: x} | {rel: x}, compare: eq | le | differ}
          {type: value, not_transcribed: <why>}          # held out, values not yet transcribed: not_assessable
          {type: coverage, generator: {fn, args}, calibrator: {fn: <module>:<fn>, args}, replicates, seed,
           quantities: [...], band: [lo, hi], classes: {<param>: [allowed class, ...]}, class_share_min}
    fit_joint: {rule: <version>, params: {<name>: [lo, hi]}, targets: [ids], design: {n, seed}}
    fit: [{param, grid: [...], targets: [ids]}]          # legacy sequential fit
    configs: {<name>: {<constructor keywords>}}          # configured engines (v2d §5); a setting names its
    default_config: <name>                               # `config`; results are stamped with default_config's engine
    oracles: <module>                                    # oracle_<name> / probe_<name> / generate_<name> functions

Sides (v2d δ): a number; {arm, value: <path>}; {oracle: <name>, args}; {probe: <name>, args}. Oracle and
probe args may themselves be sides (resolved first). A probe gets the pack's engine factory first
(make_engine(config_name)). `value` compares an engine computation with a reference (a number or an
oracle); `identity` compares two computations of one quantity. Vector values compare element-wise (the
worst element decides). `le`: a <= b + tol; `differ`: |a - b| > tol (a negative control). A coverage
benchmark runs simulation-based calibration: `replicates` synthetic data sets from the generator (seeded
per replicate), each calibrated by the calibrator, which returns {intervals: {q: [lo, hi]}, classes};
a calibrator that cannot be imported makes the benchmark not_assessable, never a pass.

Value paths: windows.<window>.<metric>, per_capita.<outcome>.<age group>,
per_capita_all.<outcome> (age groups weighted by the model population), cost.<driver>.
Ratios and odds ratios are geometric means over seeds of the per-seed CRN ratio; costs per
outcome use seed means (per-seed values are reported too). A setting may not override a
fitted parameter (fail closed).

Joint fit, rule `joint-v1`: settings are calibrated once at the box centre; a Latin
hypercube of `n` points plus the centre is run on the pack's seeds (the same seeds as the
final run); each target's log value gets a quadratic surrogate in the scaled parameters
(leave-one-out error reported); a target that the surrogate puts out of band everywhere in
the box is `infeasible_in_box` and leaves the objective, and pairs of targets that cannot
be in band together are listed; the chosen value minimises (number of feasible targets out
of band, then the band-scaled squared log error) on a dense grid; the range is the box
region where every feasible target is in band; the targets are then re-run at the chosen
value, with settings re-calibrated there, as a verification.
"""
from __future__ import annotations

import argparse
import importlib
import itertools
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

TYPES = ("ratio", "odds_ratio", "interaction_or", "cost_per_outcome", "curve_rules", "value", "identity", "coverage")
COMPARE_TYPES = ("value", "identity")
COMPARES = ("eq", "le", "differ")
STATUSES = ("in_band", "out_of_band", "not_assessable", "band_mis_based")
JOINT_RULES = {
    "joint-v1": "LHS(n)+centre on the pack seeds; calibration at the box centre; quadratic surrogate of each "
                "target's log value; infeasible targets dropped; argmin (n out of band, band-scaled squared log "
                "error) on a dense grid; range = region with all feasible targets in band; verification re-run "
                "with re-calibration at the chosen value",
}


def load_engine(ref: str, config: dict | None = None):
    """The engine class `module:Class`, built with the pack configuration's keywords (none: no arguments)."""
    mod, cls = ref.split(":")
    return getattr(importlib.import_module(mod), cls)(**(config or {}))


def _engine_key(ref: str, config: dict | None) -> tuple:
    """Worker cache key: the engine reference and its canonical configuration."""
    import json
    return (ref, None if config is None else json.dumps(config, sort_keys=True, default=str))


def _side_refs(x, kind: str) -> list:
    """Every `kind` (arm | oracle | probe) named in a side, recursively through oracle/probe args."""
    out = []
    if isinstance(x, dict):
        if kind in x:
            out.append(x[kind])
        for v in (x.get("args") or {}).values() if ("oracle" in x or "probe" in x) else []:
            out += _side_refs(v, kind)
    elif isinstance(x, list):
        for v in x:
            out += _side_refs(v, kind)
    return out


def _oracles(pack: dict):
    return importlib.import_module(pack["oracles"]) if pack.get("oracles") else None


def _config_name(pack: dict, setting: dict):
    """A setting's configuration name: its own `config`, else the pack's `default_config` (None: unconfigured)."""
    return setting.get("config") if setting.get("config") is not None else pack.get("default_config")


def _config_of(pack: dict, setting: dict):
    name = _config_name(pack, setting)
    return None if name is None else (pack.get("configs") or {}).get(name)


def _h(d: dict) -> Horizon:
    return Horizon(float(d["start"]), float(d["stop"]), tuple(d["window"]), dict(d.get("windows") or {}))


def fit_targets(pack: dict) -> set:
    ids = {t for s in pack.get("fit") or [] for t in s["targets"]}
    return ids | set((pack.get("fit_joint") or {}).get("targets") or [])


def validate(pack: dict, engine) -> list[str]:
    bad = []
    specs = {p.name: p for p in engine.parameters()}
    fitted = {k for k, p in specs.items() if p.kind == "fitted"}
    configs = pack.get("configs") or {}
    if not isinstance(configs, dict):
        return ["configs must be a mapping {name: {constructor keywords}}"]
    if pack.get("default_config") is not None and pack["default_config"] not in configs:
        bad.append(f"default_config {pack['default_config']!r} is not a declared config")
    cfg_specs = {}
    for name, cfg in configs.items():
        try:
            e = load_engine(pack["engine"], cfg)
            cfg_specs[name] = {p.name: p for p in e.parameters()}
        except Exception as ex:  # noqa: BLE001 -- recorded: a config the engine refuses is a pack error
            bad.append(f"config {name}: the engine refuses it ({type(ex).__name__}: {ex})")
    for s, cfg in (pack.get("settings") or {}).items():
        sp = specs
        cname = _config_name(pack, cfg)
        if cname is not None:
            if cname not in configs:
                bad.append(f"setting {s}: unknown config {cname!r}")
                continue
            sp = cfg_specs.get(cname, {})
        fit_s = {k for k, p in sp.items() if p.kind == "fitted"}
        for k in (cfg.get("params") or {}):
            if k not in sp:
                bad.append(f"setting {s}: unknown parameter {k}")
            elif k in fit_s:
                bad.append(f"setting {s}: overrides fitted parameter {k} (no per-setting tuning)")
    arms = pack.get("arms") or {}
    for a, cfg in arms.items():
        if cfg.get("setting") not in (pack.get("settings") or {}):
            bad.append(f"arm {a}: unknown setting {cfg.get('setting')}")
    ids = {b.get("id") for b in pack.get("benchmarks") or []}
    try:
        om = _oracles(pack)
    except ImportError as ex:
        return bad + [f"oracles module {pack.get('oracles')!r} cannot be imported: {ex}"]
    for b in pack.get("benchmarks") or []:
        e = b.get("estimand") or {}
        if e.get("type") not in TYPES:
            bad.append(f"{b.get('id')}: estimand type {e.get('type')!r} not in {TYPES}")
            continue
        for a in _arms_of(e):
            if a not in arms:
                bad.append(f"{b.get('id')}: unknown arm {a}")
        if e["type"] in COMPARE_TYPES or e["type"] == "coverage":
            bad += _validate_new(b, e, om)
            continue
        if e["type"] != "curve_rules":
            band = b.get("band")
            if not (isinstance(band, list) and len(band) == 2 and band[0] <= b.get("target", float("nan")) <= band[1]):
                bad.append(f"{b.get('id')}: band must be [lo, hi] containing the target")
        for r, iv in (b.get("requires") or {}).items():
            if r not in ids or r == b.get("id"):
                bad.append(f"{b.get('id')}: requires unknown benchmark {r}")
            if not (isinstance(iv, list) and len(iv) == 2 and iv[0] < iv[1]):
                bad.append(f"{b.get('id')}: requires {r} needs an interval [lo, hi]")
    for f in pack.get("fit") or []:
        if f.get("param") not in fitted:
            bad.append(f"fit step {f.get('param')}: not a fitted engine parameter")
        for t in f.get("targets") or []:
            if t not in ids:
                bad.append(f"fit step {f.get('param')}: unknown benchmark {t}")
    fj = pack.get("fit_joint")
    if fj:
        if pack.get("fit"):
            bad.append("pack has both fit and fit_joint (one fit rule per engine version)")
        if fj.get("rule") not in JOINT_RULES:
            bad.append(f"fit_joint rule {fj.get('rule')!r} not in {sorted(JOINT_RULES)}")
        for k, r in (fj.get("params") or {}).items():
            if k not in fitted:
                bad.append(f"fit_joint: {k} is not a fitted engine parameter")
            if not (isinstance(r, list) and len(r) == 2 and r[0] < r[1]):
                bad.append(f"fit_joint: {k} needs a range [lo, hi]")
        missing = fitted - set(fj.get("params") or {})
        if missing:
            bad.append(f"fit_joint: fitted engine parameters not in the joint fit: {sorted(missing)}")
        for t in fj.get("targets") or []:
            if t not in ids:
                bad.append(f"fit_joint: unknown benchmark {t}")
        if not fj.get("targets"):
            bad.append("fit_joint: no targets")
    return bad


def _validate_new(b: dict, e: dict, om) -> list:
    bid = b.get("id")
    bad = []
    if e["type"] == "coverage":
        g = (e.get("generator") or {}).get("fn")
        if om is None or not callable(getattr(om, f"generate_{g}", None)):
            bad.append(f"{bid}: generator {g!r} not found (generate_{g} in the oracles module)")
        c = (e.get("calibrator") or {}).get("fn")
        if not isinstance(c, str) or ":" not in c:
            bad.append(f"{bid}: calibrator must be '<module>:<function>'")
        r = e.get("replicates")
        if isinstance(r, bool) or not isinstance(r, int) or r < 1:
            bad.append(f"{bid}: replicates must be a positive integer")
        if not e.get("quantities"):
            bad.append(f"{bid}: no quantities to cover")
        band = e.get("band")
        if not (isinstance(band, list) and len(band) == 2 and 0 <= band[0] <= band[1] <= 1):
            bad.append(f"{bid}: coverage band must be [lo, hi] within [0, 1]")
        return bad
    if e.get("not_transcribed"):
        return bad
    for side in ("a", "b"):
        if side not in e:
            bad.append(f"{bid}: {e['type']} needs sides a and b")
    for kind in ("oracle", "probe"):
        for name in _side_refs([e.get("a"), e.get("b")], kind):
            if om is None or not callable(getattr(om, f"{kind}_{name}", None)):
                bad.append(f"{bid}: {kind} {name!r} not found ({kind}_{name} in the oracles module)")
    tol = e.get("tol")
    if not (isinstance(tol, dict) and len(tol) == 1 and next(iter(tol)) in ("abs", "rel")
            and isinstance(next(iter(tol.values())), (int, float)) and next(iter(tol.values())) >= 0):
        bad.append(f"{bid}: tol must be {{abs: x}} or {{rel: x}} with x >= 0")
    if e.get("compare", "eq") not in COMPARES:
        bad.append(f"{bid}: compare {e.get('compare')!r} not in {COMPARES}")
    if e["type"] == "identity" and not (_side_refs(e.get("a"), "arm") or _side_refs(e.get("a"), "probe")):
        bad.append(f"{bid}: an identity compares two engine computations (side a names no arm or probe)")
    return bad


def _arms_of(e: dict) -> list:
    if e.get("type") in COMPARE_TYPES:
        return list(dict.fromkeys(_side_refs([e.get("a"), e.get("b")], "arm")))
    out = []
    for k in ("num", "den", "a11", "a10", "a01", "a00"):
        v = e.get(k)
        out += v if isinstance(v, list) else ([v] if v else [])
    return out + list(e.get("arms") or [])


def get(res: dict, path: str):
    kind, *rest = path.split(".")
    if kind == "per_capita_vec":            # one outcome per age group, in the engine's order
        return [float(v) for v in res["per_capita"][rest[0]].values()]
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
    ref, config, params, unit, package, horizon, seed = args
    key = _engine_key(ref, config)
    if key not in _ENGINE:
        _ENGINE[key] = load_engine(ref, config)
    eng = _ENGINE[key]
    o = eng.run(params, UnitSpec("bench", unit), Package("p", package), _h(horizon), seed)
    pdays = o.meta.get("age_shares")
    if pdays is None:
        pdays = {g: 1.0 / len(o.per_capita[next(iter(o.per_capita))]) for g in o.per_capita[next(iter(o.per_capita))]}
    return {"per_capita": o.per_capita, "windows": o.windows, "cost_drivers": o.cost_drivers,
            "meta": dict(o.meta, age_shares=pdays)}


def _spread(per) -> dict:
    per = [float(x) for x in per if math.isfinite(float(x))]
    if not per:
        return {"per_seed": [], "seed_min": None, "seed_max": None}
    return {"per_seed": [round(x, 4) for x in per], "seed_min": round(min(per), 4), "seed_max": round(max(per), 4)}


class Runner:
    def __init__(self, pack: dict, engine, workers: int = 1, seeds=None):
        self.pack, self.engine, self.workers = pack, engine, workers
        self.seeds = list(seeds or pack.get("seeds") or [1])
        self.ref = pack["engine"]
        self.cache = {}
        self.calibrated = {}
        self.nsims = 0
        self._engines = {}
        self.exercised = set()           # configurations a benchmark ran (arms or probes)

    def _map(self, jobs, chunk: int = 300):
        """Run jobs; a fresh process pool per chunk (one long-lived pool broke on worker memory at
        about 750 runs per VM in the spike)."""
        if self.workers <= 1 or len(jobs) <= 1:
            return [_job(j) for j in jobs]
        out = []
        for i in range(0, len(jobs), chunk):
            part = jobs[i:i + chunk]
            with ProcessPoolExecutor(min(self.workers, len(part))) as ex:
                out += list(ex.map(_job, part, chunksize=1))
            if len(jobs) > chunk:
                print(f"  ran {min(i + chunk, len(jobs))}/{len(jobs)} engine runs", flush=True)
        return out

    def _setting(self, name):
        s = self.pack["settings"][name]
        return dict(s["unit"]), dict(s.get("horizon") or self.pack["horizon"]), dict(s.get("params") or {})

    def _config(self, setting_name):
        return _config_of(self.pack, self.pack["settings"][setting_name])

    def make_engine(self, config_name=None):
        """The pack's engine with a named configuration (None: the runner's engine). Used by probes."""
        if config_name is None:
            return self.engine
        self.exercised.add(config_name)
        if config_name not in self._engines:
            self._engines[config_name] = load_engine(self.ref, (self.pack.get("configs") or {})[config_name])
        return self._engines[config_name]

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
                    jobs.append((self.ref, self._config(n), dict(par, **fitted), dict(unit, **{c["attribute"]: g}),
                                 c.get("arm_package") or {}, hor, sd))
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
                                  "baseline_values": [round(float(y), 4) for y in ys], "target": c["target"],
                                  "at_grid_edge": bool(c["target"] < ys.min() or c["target"] > ys.max())}

    def _arm_jobs(self, a, fitted):
        cfg = self.pack["arms"][a]
        unit, hor, par = self._setting(cfg["setting"])
        if cfg["setting"] in self.calibrated:
            c = self.calibrated[cfg["setting"]]
            unit[c["attribute"]] = c["value"]
        conf = self._config(cfg["setting"])
        name = _config_name(self.pack, self.pack["settings"][cfg["setting"]])
        if name is not None:
            self.exercised.add(name)
        return [(self.ref, conf, dict(par, **fitted), unit, cfg.get("package") or {}, hor, sd) for sd in self.seeds]

    def prefetch(self, arms, fitted_list):
        """Run every (arm, fitted) pair not yet cached in one parallel batch."""
        jobs, need = [], []
        for f in fitted_list:
            key = tuple(sorted(f.items()))
            for a in arms:
                if (a, key) in self.cache or (a, key) in need:
                    continue
                jobs += self._arm_jobs(a, f)
                need.append((a, key))
        if jobs:
            res = self._map(jobs)
            self.nsims += len(jobs)
            k = len(self.seeds)
            for i, ak in enumerate(need):
                self.cache[ak] = res[i * k:(i + 1) * k]

    def arm_results(self, arms, fitted: dict):
        self.prefetch(arms, [fitted])
        key = tuple(sorted(fitted.items()))
        return {a: self.cache[(a, key)] for a in arms}

    def evaluate(self, b: dict, fitted: dict) -> dict:
        e = b["estimand"]
        if e["type"] in COMPARE_TYPES:
            return self._compare(b, fitted)
        if e["type"] == "coverage":
            return self._coverage(b)
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
            out["arm_or_geomean"] = {k: round(float(np.exp(np.mean([np.log(x[k]) for x in out["arm_or"]]))), 4)
                                     for k in ("a11", "a10", "a01")}
            out["arm_prevalence"] = {k: round(float(np.mean([pooled(e[k], i) for i in range(nseed)])), 4)
                                     for k in ("a00", "a10", "a01", "a11")}
        elif e["type"] == "cost_per_outcome":
            dv_i = [pooled(e["den"], i) - pooled(e["num"], i) for i in range(nseed)]
            dc_i = [0.0] * nseed
            for drv, uc in (e.get("costs") or {}).items():       # unit cost x (driver in num - driver in den)
                for i in range(nseed):
                    dc_i[i] += float(uc) * (get(R[e["num"]][i], f"cost.{drv}") - get(R[e["den"]][i], f"cost.{drv}"))
            for drv, uc in (e.get("costs_num") or {}).items():   # incremental unit price x driver in num
                for i in range(nseed):
                    dc_i[i] += float(uc) * get(R[e["num"]][i], f"cost.{drv}")
            dv, dc = float(np.mean(dv_i)), float(np.mean(dc_i))
            base = float(np.mean([pooled(e["den"], i) for i in range(nseed)]))
            comp = float(dc / dv) if dv > 0 else float("inf")
            floor = float(dc / base) if base > 0 else float("inf")
            out.update(computed=comp, outcome_averted_per_capita=dv, cost_per_capita=dc,
                       comparator_outcome_per_capita=base, share_averted=float(dv / base) if base > 0 else None,
                       price_floor=floor, target_below_price_floor=bool(floor > b["target"]),
                       share_averted_needed_for_band=float(dc / (b["band"][1] * base)) if base > 0 else None)
            out.update(_spread([c / v if v > 0 else float("inf") for c, v in zip(dc_i, dv_i)]))
            return self._verdict(b, out)
        else:  # curve_rules
            pts, met_seed = [], []
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
            for i in range(nseed):
                c_i = []
                for a in e["arms"]:
                    x, y = get(R[a][i], e["x"]), get(R[a][i], e["y"])
                    for r in e["rules"]:
                        if "x_min" in r and x >= r["x_min"]:
                            c_i.append(y >= r["y_min"])
                        if "x_max" in r and x <= r["x_max"]:
                            c_i.append(y <= r["y_max"])
                met_seed.append(f"{sum(c_i)}/{len(c_i)}")
            out.update(points=pts, rules_checked=len(checks), rules_met=int(sum(checks)),
                       computed=float(np.mean(checks)) if checks else float("nan"), per_seed_rules_met=met_seed)
            out["in_band"] = bool(checks) and all(checks)
            out["status"] = "in_band" if out["in_band"] else "out_of_band"
            return out
        per = np.asarray(per, float)
        out.update(computed=float(np.exp(np.mean(np.log(per)))),
                   se_log=float(np.std(np.log(per), ddof=1) / math.sqrt(len(per))) if len(per) > 1 else None)
        out.update(_spread(per))
        return self._verdict(b, out)

    # -------------------------------------------------------------- v2d δ: value / identity / coverage
    def _side(self, x, R, i):
        if isinstance(x, bool):
            raise ValueError(f"a side cannot be a boolean ({x})")
        if isinstance(x, (int, float)):
            return float(x)
        if isinstance(x, list):
            return [self._side(v, R, i) for v in x]
        if not isinstance(x, dict):
            raise ValueError(f"bad side {x!r}")
        if "arm" in x:
            return get(R[x["arm"]][i], x["value"])
        om = _oracles(self.pack)
        args = {k: self._side(v, R, i) if self._is_side(v) else v for k, v in (x.get("args") or {}).items()}
        if "oracle" in x:
            return getattr(om, f"oracle_{x['oracle']}")(**args)
        if "probe" in x:
            return getattr(om, f"probe_{x['probe']}")(self.make_engine, **args)
        raise ValueError(f"bad side {x!r}")

    @staticmethod
    def _is_side(v) -> bool:
        if isinstance(v, dict):
            return any(k in v for k in ("arm", "oracle", "probe"))
        if isinstance(v, list):
            return any(isinstance(x, dict) and any(k in x for k in ("arm", "oracle", "probe")) for x in v)
        return False

    def _compare(self, b: dict, fitted: dict) -> dict:
        e = b["estimand"]
        if e.get("not_transcribed"):
            return {"computed": None, "in_band": False, "status": "not_assessable",
                    "not_assessable_because": f"published values not yet transcribed: {e['not_transcribed']}"}
        R = self.arm_results(_arms_of(e), fitted)
        (kind, tol), = e["tol"].items()
        cmp = e.get("compare", "eq")
        seeds = range(len(self.seeds)) if _arms_of(e) else range(1)     # oracle/probe-only: one evaluation
        per, worst = [], None
        for i in seeds:
            a, ref = self._side(e["a"], R, i), self._side(e["b"], R, i)
            av, bv = np.atleast_1d(np.asarray(a, float)), np.atleast_1d(np.asarray(ref, float))
            if av.shape != bv.shape:
                raise ValueError(f"{b['id']}: sides have shapes {av.shape} and {bv.shape}")
            d = av - bv
            err_abs = float(np.max(np.abs(d)))
            err_rel = float(np.max(np.abs(d) / np.maximum(np.abs(bv), 1e-300)))
            if not (np.all(np.isfinite(av)) and np.all(np.isfinite(bv))):
                ok_i = False
            elif cmp == "le":                   # element by element, each against its own reference
                ok_i = bool(np.all(d <= (tol if kind == "abs" else tol * np.abs(bv))))
            elif cmp == "differ":
                ok_i = (err_abs if kind == "abs" else err_rel) > tol
            else:
                ok_i = (err_abs if kind == "abs" else err_rel) <= tol
            rec = {"a": a, "b": ref, "error_abs": err_abs, "error_rel": err_rel, "ok": bool(ok_i)}
            per.append(rec)
            if worst is None or (not ok_i and worst["ok"]) or (ok_i == worst["ok"] and err_abs > worst["error_abs"]):
                worst = rec
        out = {"computed": worst["a"] if np.ndim(worst["a"]) == 0 else worst["error_abs"],
               "reference": worst["b"], "error_abs": worst["error_abs"], "error_rel": worst["error_rel"],
               "tolerance": {kind: tol}, "compare": cmp,
               "per_seed_error_abs": [p["error_abs"] for p in per]}
        if np.ndim(worst["a"]):
            out["computed_vector"] = [float(v) for v in worst["a"]]
        out["in_band"] = all(p["ok"] for p in per)
        out["status"] = "in_band" if out["in_band"] else "out_of_band"
        return out

    def _coverage(self, b: dict) -> dict:
        e = b["estimand"]
        om = _oracles(self.pack)
        gen = getattr(om, "generate_" + e["generator"]["fn"])
        try:
            mod, fn = e["calibrator"]["fn"].split(":")
            cal = getattr(importlib.import_module(mod), fn)
        except (ImportError, AttributeError, ValueError) as ex:
            return {"computed": None, "in_band": False, "status": "not_assessable",
                    "not_assessable_because": f"calibrator {e['calibrator']['fn']} not available "
                                              f"({type(ex).__name__}: {ex})"}
        qs = list(e["quantities"])
        classes = e.get("classes") or {}
        reps, seed = int(e["replicates"]), int(e.get("seed", 0))
        hits = {q: 0 for q in qs}
        cls_hits = {p: 0 for p in classes}
        invalid = 0
        for r in range(reps):
            rng = np.random.default_rng([seed, r])
            data, truth = gen(rng, **(e["generator"].get("args") or {}))
            post = cal(self.make_engine, data, **(e["calibrator"].get("args") or {}))
            bad = False
            for q in qs:
                iv = (post.get("intervals") or {}).get(q)
                try:
                    lo, hi, tv = float(iv[0]), float(iv[1]), float(truth[q])
                except (TypeError, ValueError, IndexError, KeyError):
                    bad = True
                    continue
                if not (math.isfinite(lo) and math.isfinite(hi) and math.isfinite(tv) and lo <= hi):
                    bad = True
                    continue
                hits[q] += int(lo <= tv <= hi)
            invalid += int(bad)
            for p, allowed in classes.items():
                cls_hits[p] += int((post.get("classes") or {}).get(p) in allowed)
        cov = {q: hits[q] / reps for q in qs}
        se = {q: math.sqrt(max(cov[q] * (1 - cov[q]), 1e-12) / reps) for q in qs}
        share = {p: cls_hits[p] / reps for p in classes}
        lo, hi = e["band"]
        need = float(e.get("class_share_min", 0.9))
        out = {"computed": min(cov.values()), "coverage": cov, "coverage_se": se, "class_share": share,
               "replicates": reps, "band": [lo, hi], "invalid_outputs": invalid}
        # a calibration that returns no usable interval is a failure, never "under-coverage detected"
        out["in_band"] = (invalid == 0 and all(lo <= c <= hi for c in cov.values())
                          and all(v >= need for v in share.values()))
        out["status"] = "in_band" if out["in_band"] else "out_of_band"
        return out

    @staticmethod
    def _verdict(b, out):
        lo, hi = b["band"]
        out["in_band"] = bool(lo <= out["computed"] <= hi)
        out["status"] = "in_band" if out["in_band"] else "out_of_band"
        if b["estimand"]["type"] == "cost_per_outcome" and out.get("price_floor", 0) > hi:
            out["status"] = "band_mis_based"
            out["mis_based_reason"] = "price_floor"
        return out


def apply_requires(rows: list) -> None:
    """Gate benchmarks on the benchmarks they require (in place)."""
    byid = {r["id"]: r for r in rows}
    for r in rows:
        req = r.get("requires") or {}
        if not req:
            continue
        vals = {k: byid[k]["computed"] for k in req}
        r["requires_values"] = {k: round(v, 4) for k, v in vals.items()}
        miss = [k for k, (lo, hi) in req.items() if not lo <= vals[k] <= hi]
        if miss:
            r["status"] = "not_assessable"
            r["not_assessable_because"] = {k: {"computed": round(vals[k], 4), "required": req[k]} for k in miss}


def reach_check(pack: dict, runner: Runner, rows: list, fit_info: dict | None) -> None:
    """For each out-of-band cost-per-outcome benchmark, evaluate it at every corner of the fitted
    parameters' range (the values consistent with the effect benchmarks). If no corner reaches
    the band, the band cannot be met at the pack's prices by any engine consistent with the
    effect evidence: status band_mis_based (reason unreachable_in_fitted_range). In place."""
    params = (fit_info or {}).get("parameters") or {}
    if not params:
        return
    byid = {b["id"]: b for b in pack["benchmarks"]}
    names = sorted(params)
    corners = [dict(zip(names, c)) for c in itertools.product(*[(params[k]["lo"], params[k]["hi"]) for k in names])]
    todo = [r for r in rows if byid[r["id"]]["estimand"]["type"] == "cost_per_outcome" and r["status"] == "out_of_band"]
    for r in todo:
        b = byid[r["id"]]
        runner.prefetch(_arms_of(b["estimand"]), corners)
        vals = [runner.evaluate(b, c)["computed"] for c in corners]
        lo, hi = b["band"]
        r["reach_in_fitted_range"] = [round(min(vals), 4), round(max(vals), 4)]
        if all(v > hi for v in vals) or all(v < lo for v in vals):
            r["status"] = "band_mis_based"
            r["mis_based_reason"] = "unreachable_in_fitted_range"


def _loss(b, comp):
    lo, hi = b["band"]
    if not (comp > 0 and math.isfinite(comp)):
        return 1e6
    hw = (math.log(hi) - math.log(lo)) / 2
    return ((math.log(comp) - math.log(b["target"])) / hw) ** 2


# ------------------------------------------------------------------------- legacy fit
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
    return {"parameters": out, "fit_rule": {"version": "sequential-v0", "description": "one-parameter grid fits in order"}}


# -------------------------------------------------------------------------- joint fit
def lhs(n: int, d: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    cut = (np.arange(n)[:, None] + rng.random((n, d))) / n
    for j in range(d):
        cut[:, j] = cut[rng.permutation(n), j]
    return cut


def _quad_features(X: np.ndarray) -> np.ndarray:
    n, d = X.shape
    cols = [np.ones(n)] + [X[:, j] for j in range(d)] + [X[:, i] * X[:, j] for i in range(d) for j in range(i, d)]
    return np.column_stack(cols)


def _quad_fit(X, y):
    F = _quad_features(X)
    coef, *_ = np.linalg.lstsq(F, y, rcond=None)
    loo = []
    for i in range(len(y)):
        m = np.arange(len(y)) != i
        c, *_ = np.linalg.lstsq(F[m], y[m], rcond=None)
        loo.append(float(F[i] @ c - y[i]))
    return coef, float(np.sqrt(np.mean(np.square(loo))))


def _candidates(d: int, seed: int) -> np.ndarray:
    if d <= 3:
        g = np.linspace(0, 1, {1: 801, 2: 161, 3: 41}[d])
        return np.array(list(itertools.product(*([g] * d))))
    return np.random.default_rng(seed).random((50000, d))


def fit_joint(pack: dict, engine, runner: Runner) -> dict:
    spec = pack["fit_joint"]
    byid = {b["id"]: b for b in pack["benchmarks"]}
    names = list(spec["params"])
    box = np.array([spec["params"][k] for k in names], float)
    d = len(names)
    targets = list(spec["targets"])
    to_theta = lambda x: {k: round(float(box[j, 0] + x[j] * (box[j, 1] - box[j, 0])), 6) for j, k in enumerate(names)}  # noqa: E731
    centre = to_theta(np.full(d, 0.5))
    settings = sorted({pack["arms"][a]["setting"] for t in targets for a in _arms_of(byid[t]["estimand"])})
    runner.calibrate_settings(centre, settings)
    cal_centre = {s: round(runner.calibrated[s]["value"], 4) for s in settings if s in runner.calibrated}
    dz = spec.get("design") or {}
    X = np.vstack([lhs(int(dz.get("n", 8 * d + 8)), d, int(dz.get("seed", 1))), np.full((1, d), 0.5)])
    thetas = [to_theta(x) for x in X]
    arms = sorted({a for t in targets for a in _arms_of(byid[t]["estimand"])})
    t0 = time.time()
    runner.prefetch(arms, thetas)
    print(f"joint fit: design of {len(thetas)} points x {len(arms)} arms x {len(runner.seeds)} seeds "
          f"in {time.time() - t0:.0f}s", flush=True)
    Y = {t: np.array([runner.evaluate(byid[t], th)["computed"] for th in thetas]) for t in targets}
    surr, cand = {}, _candidates(d, int(dz.get("seed", 1)))
    Fc = _quad_features(cand)
    pred, inband = {}, {}
    for t in targets:
        coef, loo = _quad_fit(X, np.log(np.maximum(Y[t], 1e-12)))
        surr[t] = {"loo_rmse_log": round(loo, 4)}
        pred[t] = np.exp(Fc @ coef)
        lo, hi = byid[t]["band"]
        inband[t] = (pred[t] >= lo) & (pred[t] <= hi)
    feasible = [t for t in targets if inband[t].any()]
    infeasible = [t for t in targets if t not in feasible]
    conflicts = [[a, b] for a, b in itertools.combinations(feasible, 2) if not (inband[a] & inband[b]).any()]
    n_out = sum((~inband[t]).astype(int) for t in feasible) if feasible else np.zeros(len(cand))
    L = sum(np.array([_loss(byid[t], v) for v in pred[t]]) for t in feasible) if feasible else np.zeros(len(cand))
    j = int(np.argmin(n_out * 1e6 + L))
    best = to_theta(cand[j])
    region = cand[n_out == 0] if (n_out == 0).any() else cand[L <= max(len(feasible), 1)]
    if not len(region):
        region = cand[j:j + 1]
    rng_ = {k: (round(float(box[i, 0] + region[:, i].min() * (box[i, 1] - box[i, 0])), 4),
                round(float(box[i, 0] + region[:, i].max() * (box[i, 1] - box[i, 0])), 4)) for i, k in enumerate(names)}
    # verification at the chosen value, settings re-calibrated there
    runner.calibrate_settings(best, settings)
    cal_best = {s: round(runner.calibrated[s]["value"], 4) for s in settings if s in runner.calibrated}
    ver = {}
    for t in targets:
        r = runner.evaluate(byid[t], best)
        ver[t] = {"computed": round(r["computed"], 4), "predicted": round(float(pred[t][j]), 4),
                  "in_band": r["in_band"], "seed_min": r.get("seed_min"), "seed_max": r.get("seed_max")}
    params = {k: {"value": best[k], "lo": min(rng_[k][0], best[k]), "hi": max(rng_[k][1], best[k]),
                  "box": [float(box[i, 0]), float(box[i, 1])], "benchmarks": targets}
              for i, k in enumerate(names)}
    for k, v in params.items():
        print(f"fit_joint {k}: {v['value']} [{v['lo']}, {v['hi']}]", flush=True)
    return {"parameters": params,
            "fit_rule": {"version": spec["rule"], "description": JOINT_RULES[spec["rule"]],
                         "seeds": list(runner.seeds), "design_points": len(thetas), "design_seed": int(dz.get("seed", 1)),
                         "targets": targets},
            "feasibility": {"feasible_in_box": feasible, "infeasible_in_box": infeasible, "conflicting_pairs": conflicts,
                            # verified values, not the surrogate: a failed verification withdraws the claim
                            "all_feasible_in_band_at_value": bool(feasible) and all(ver[t]["in_band"] for t in feasible),
                            "surrogate_all_feasible_in_band": bool(n_out[j] == 0),
                            "disproved_by_verification": [t for t in feasible if inband[t][j] and not ver[t]["in_band"]],
                            "feasibility_verified": all(ver[t]["in_band"] == bool(inband[t][j]) for t in targets)},
            "surrogate": surr,
            "calibration": {"at_box_centre": cal_centre, "at_fitted_value": cal_best},
            "verification": ver,
            "design": {"x": [{k: v for k, v in th.items()} for th in thetas],
                       "computed": {t: [round(float(v), 4) for v in Y[t]] for t in targets}}}


# ------------------------------------------------------------------------------ pack run
def run_pack(pack: dict, engine, runner: Runner, fitted: dict, fit_info: dict | None = None) -> dict:
    fit_ids = fit_targets(pack)
    infeasible = set(((fit_info or {}).get("feasibility") or {}).get("infeasible_in_box") or [])
    runner.calibrate_settings(fitted)
    rows = []
    for b in pack["benchmarks"]:
        r = runner.evaluate(b, fitted)
        row = {"id": b["id"], "metric": b["metric"], "role": "fit" if b["id"] in fit_ids else "held_out",
               "target": b.get("target"), "band": b.get("band"), "source": b.get("source", "")}
        if b.get("requires"):
            row["requires"] = b["requires"]
        if b["id"] in infeasible:
            row["infeasible_in_box"] = True
        new_type = b["estimand"]["type"] in COMPARE_TYPES + ("coverage",)
        row.update({k: (round(v, 4) if isinstance(v, float) and not new_type else v) for k, v in r.items()})
        rows.append(row)
    reach_check(pack, runner, rows, fit_info)
    apply_requires(rows)
    for row in rows:                       # a pass is a status, never a raw band test
        row["in_band"] = row["status"] == "in_band"
    for row in rows:
        comp = row["computed"]
        comp = f"{comp:.4g}" if isinstance(comp, (int, float)) else str(comp)
        ref = f" reference {row['reference']}" if "reference" in row else ""
        print(f"{row['id']:6s} {row['role']:8s} computed {comp} target {row.get('target')}{ref} "
              f"band {row.get('band')} {row['status']}", flush=True)
    by = {s: sum(r["status"] == s for r in rows) for s in STATUSES}
    from toolkit.engines.contract import engine_identity
    used = sorted(runner.exercised)
    configs_validated = {c: {"config": pack["configs"][c], "engine_identity": engine_identity(runner.make_engine(c)),
                             "config_sha256": config_digest(runner.make_engine(c))} for c in used}
    return {"benchmarks": rows, "settings_calibrated": runner.calibrated, "fitted_used": fitted,
            "configs_validated": configs_validated,
            "configs_not_exercised": sorted(set(pack.get("configs") or {}) - set(used)),
            "seeds": runner.seeds, "n_sims": runner.nsims,
            "summary": {"in_band": by["in_band"], "total": len(rows), "by_status": by,
                        "held_out_in_band": sum(r["status"] == "in_band" for r in rows if r["role"] == "held_out"),
                        "held_out_total": sum(r["role"] == "held_out" for r in rows)}}


PRODUCER = "toolkit/engines/benchmark.py"


def config_digest(engine):
    """sha256 of a configured engine's full configuration (`engine.config()`), or None for an engine without one.
    engine_identity() hashes only the card's structure, which does not show every field that changes the
    dynamics (generation interval, offspring distribution, ...); the stamp carries this digest beside it."""
    import hashlib
    import json
    if not callable(getattr(engine, "config", None)):
        return None
    return hashlib.sha256(json.dumps(engine.config(), sort_keys=True, default=str).encode()).hexdigest()


def stamp(out: dict, engine) -> dict:
    """Provenance on a results or fitted file: who wrote it, for which engine and code (Codex P1-13)."""
    from toolkit.engines.contract import engine_identity
    extra = {"engine_config_sha256": config_digest(engine)} if config_digest(engine) else {}
    return {"producer": PRODUCER, "engine_identity": engine_identity(engine), **extra, **out}


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
    if config_digest(engine) is not None and doc.get("engine_config_sha256") != config_digest(engine):
        return [], f"{os.path.basename(path)} was computed for another engine configuration: re-run the pack"
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
    eng = load_engine(pack["engine"], (pack.get("configs") or {}).get(pack.get("default_config")))
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
        out = fit_joint(pack, eng, runner) if pack.get("fit_joint") else fit(pack, eng, runner)
        path = a.out or os.path.join(d, "fitted.yaml")
        yaml.safe_dump(stamp(dict({"engine": pack["engine"], "engine_version": eng.describe().version,
                                   "fitted_at": time.strftime("%F %T"), "seeds": seeds,
                                   "n_sims": runner.nsims, "wall_s": round(time.time() - t0, 1)}, **out), eng),
                       open(path, "w"), sort_keys=False)
    else:
        fp = os.path.join(d, "fitted.yaml")
        f = yaml.safe_load(open(fp)) if os.path.exists(fp) else {}
        fitted = {k: v["value"] for k, v in (f.get("parameters") or {}).items()}
        out = run_pack(pack, eng, runner, fitted, f)
        out["engine_version"] = eng.describe().version
        out["fit_rule"] = (f.get("fit_rule") or {}).get("version")
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
    f = fit(pack, eng, r)["parameters"]
    ok(abs(f["eff_a"]["value"] - 0.6) < 0.02 and f["eff_a"]["lo"] <= 0.6 <= f["eff_a"]["hi"],
       f"BP4 the fit recovers the parameter that reproduces the target (eff_a 0.6): {f['eff_a']['value']}")
    res = run_pack(pack, eng, r, {"eff_a": f["eff_a"]["value"]})
    rows = {x["id"]: x for x in res["benchmarks"]}
    ok(rows["T1"]["role"] == "fit" and rows["T2"]["role"] == "held_out", "BP5 roles: fitted vs held-out benchmarks")
    ok(abs(rows["T2"]["computed"] - 1.0) > 0.01, "BP6 interaction OR of a multiplicative-in-burden engine is not forced to 1 on the odds scale")
    ok(abs(rows["T3"]["computed"] - 3 / (1.5 * f["eff_a"]["value"])) < 1e-3, f"BP7 cost per outcome = cost difference / outcome averted ({rows['T3']['computed']})")
    ok(abs(res["settings_calibrated"]["c"]["value"] - 0.758) < 0.08, "BP8 a setting is calibrated to its baseline target")
    ok(rows["T4"]["rules_checked"] >= 1 and len(rows["T4"]["per_seed_rules_met"]) == 2, "BP9 curve rules are checked, per seed too")
    ok(all(rows[k]["status"] == ("in_band" if rows[k]["in_band"] else "out_of_band") for k in rows)
       and len(rows["T1"]["per_seed"]) == 2 and rows["T3"]["seed_min"] is not None,
       "BP10 every row has a status and per-seed values")

    # ---- feasibility: price floor, requires-gating
    p2 = dict(pack, fit=[], benchmarks=[
        {"id": "C1", "metric": "too dear", "target": 0.5, "band": [0.1, 1.0],
         "estimand": {"type": "cost_per_outcome", "num": "a", "den": "none", "value": "per_capita.cases.all",
                      "costs": {"a_doses": 10.0}}},
        {"id": "C2", "metric": "affordable", "target": 20.0, "band": [5.0, 80.0],
         "estimand": {"type": "cost_per_outcome", "num": "a", "den": "none", "value": "per_capita.cases.all",
                      "costs": {"a_doses": 10.0}}},
        dict(pack["benchmarks"][0], id="M1"),
        dict(pack["benchmarks"][1], id="I1", requires={"M1": [0.3, 0.5]}),
        dict(pack["benchmarks"][1], id="I2", requires={"M1": [0.05, 0.1]})])
    ok(validate(p2, eng) == [], "BP11 a pack with requires-gating validates")
    r2 = Runner(p2, eng, 1)
    res2 = {x["id"]: x for x in run_pack(p2, eng, r2, {})["benchmarks"]}
    # cost 10 x 3 doses = 30 per person; comparator burden 1.5 cases per person -> floor 20 per case averted
    ok(res2["C1"]["status"] == "band_mis_based" and abs(res2["C1"]["price_floor"] - 20.0) < 1e-6,
       f"BP12 a cost band below the price floor is band_mis_based (floor {res2['C1']['price_floor']:.2f})")
    ok(res2["C2"]["status"] in ("in_band", "out_of_band"), "BP13 a cost band above the floor is assessed normally")
    ok(res2["I1"]["status"] != "not_assessable" and res2["I2"]["status"] == "not_assessable"
       and "M1" in res2["I2"]["not_assessable_because"] and "M1" in res2["I2"]["requires_values"],
       "BP14 an interaction is not assessable when a required main effect misses, and reports it")
    full2 = run_pack(p2, eng, Runner(p2, eng, 1), {})
    rows2 = {x["id"]: x for x in full2["benchmarks"]}
    ok(rows2["I2"]["in_band"] is False and all(x["in_band"] == (x["status"] == "in_band") for x in full2["benchmarks"])
       and full2["summary"]["in_band"] == full2["summary"]["by_status"]["in_band"]
       and full2["summary"]["in_band"] == sum(x["status"] == "in_band" for x in full2["benchmarks"]),
       "BP14b a not-assessable (or mis-based) row never counts as a pass: in_band and every total follow status")
    ok(any("requires unknown" in s for s in validate(dict(p2, benchmarks=p2["benchmarks"] + [
        dict(pack["benchmarks"][1], id="I3", requires={"ZZ": [0, 1]})]), eng)), "BP15 requires naming an unknown benchmark is refused")

    # ---- joint fit: recovery, infeasibility, reproducibility
    p3 = dict(pack, fit=None, benchmarks=[
        dict(pack["benchmarks"][0], id="J1"),                                     # a vs none: 1 - eff_a = 0.4
        {"id": "J2", "metric": "b vs none", "target": 0.8, "band": [0.75, 0.85],
         "estimand": {"type": "ratio", "num": "b", "den": "none", "value": "per_capita.cases.all"}},
        {"id": "J3", "metric": "unreachable", "target": 0.05, "band": [0.01, 0.1],
         "estimand": {"type": "ratio", "num": "a", "den": "none", "value": "per_capita.cases.all"}}],
        fit_joint={"rule": "joint-v1", "params": {"eff_a": [0.3, 0.7], "eff_b": [0.1, 0.5]},
                   "targets": ["J1", "J2", "J3"], "design": {"n": 12, "seed": 3}})
    p3.pop("fit")
    ok(validate(p3, eng) == [], "BP16 a joint-fit pack validates")
    ok(any("not in the joint fit" in s for s in validate(dict(p3, fit_joint=dict(p3["fit_joint"], params={"eff_a": [0.3, 0.7]})), eng)),
       "BP17 a joint fit must include every fitted engine parameter")
    ok(any("rule" in s for s in validate(dict(p3, fit_joint=dict(p3["fit_joint"], rule="nope")), eng)),
       "BP18 an unknown fit rule is refused (the rule is versioned)")
    j1 = fit_joint(p3, eng, Runner(p3, eng, 1))
    ok(abs(j1["parameters"]["eff_a"]["value"] - 0.6) < 0.01 and abs(j1["parameters"]["eff_b"]["value"] - 0.2) < 0.01,
       f"BP19 the joint fit recovers both parameters ({j1['parameters']['eff_a']['value']}, {j1['parameters']['eff_b']['value']})")
    ok(j1["feasibility"]["infeasible_in_box"] == ["J3"] and j1["feasibility"]["all_feasible_in_band_at_value"],
       "BP20 a target no value in the box can meet is reported infeasible_in_box and left out of the objective")
    ok(all(j1["verification"][t]["in_band"] for t in ("J1", "J2")) and j1["fit_rule"]["version"] == "joint-v1"
       and j1["fit_rule"]["seeds"] == [1, 2], "BP21 the fit is verified by re-running, and records its rule and seeds")
    j2 = fit_joint(p3, eng, Runner(p3, eng, 1))
    ok(j1["parameters"] == j2["parameters"] and j1["design"] == j2["design"], "BP22 the joint fit is reproducible")
    res3 = {x["id"]: x for x in run_pack(p3, eng, Runner(p3, eng, 1), {k: v["value"] for k, v in j1["parameters"].items()}, j1)["benchmarks"]}
    ok(res3["J3"].get("infeasible_in_box") is True and res3["J1"]["role"] == "fit", "BP23 the pack run carries the feasibility flags")
    conf = dict(p3, benchmarks=p3["benchmarks"][:2] + [
        {"id": "J4", "metric": "a vs none, other band", "target": 0.65, "band": [0.62, 0.68],
         "estimand": {"type": "ratio", "num": "a", "den": "none", "value": "per_capita.cases.all"}}],
        fit_joint=dict(p3["fit_joint"], targets=["J1", "J2", "J4"]))
    jc = fit_joint(conf, eng, Runner(conf, eng, 1))
    ok(["J1", "J4"] in jc["feasibility"]["conflicting_pairs"], "BP24 two targets that cannot be in band together are reported as a conflict")

    class Disproved(Runner):
        """Agrees with the engine during the design, then reports every target out of band."""
        n_design = len(p3["fit_joint"]["targets"]) * (p3["fit_joint"]["design"]["n"] + 1)
        k = 0

        def evaluate(self, b, fitted):
            r = super().evaluate(b, fitted)
            Disproved.k += 1
            if Disproved.k > Disproved.n_design:
                lo, hi = b["band"]
                r = dict(r, computed=hi * 10, in_band=False, status="out_of_band")
            return r
    jd = fit_joint(p3, eng, Disproved(p3, eng, 1))
    fz = jd["feasibility"]
    ok(fz["all_feasible_in_band_at_value"] is False and fz["feasibility_verified"] is False
       and set(fz["disproved_by_verification"]) == {"J1", "J2"},
       "BP26 a verification that disproves the surrogate overrides it: the feasibility claim is withdrawn")
    # reach check: cost per case = 30 / (1.5 eff_a) = 20 / eff_a, eff_a fitted range [0.3, 0.7] -> [28.6, 66.7]
    p4 = dict(pack, fit=[], benchmarks=[
        {"id": "R1", "metric": "unreachable", "target": 10.0, "band": [5.0, 20.0],
         "estimand": {"type": "cost_per_outcome", "num": "a", "den": "none", "value": "per_capita.cases.all", "costs": {"a_doses": 10.0}}},
        {"id": "R2", "metric": "reachable", "target": 30.0, "band": [5.0, 40.0],
         "estimand": {"type": "cost_per_outcome", "num": "a", "den": "none", "value": "per_capita.cases.all", "costs": {"a_doses": 10.0}}}])
    info = {"parameters": {"eff_a": {"value": 0.4, "lo": 0.3, "hi": 0.7}}}
    res4 = {x["id"]: x for x in run_pack(p4, eng, Runner(p4, eng, 1), {"eff_a": 0.4}, info)["benchmarks"]}
    ok(res4["R1"]["status"] == "band_mis_based" and res4["R1"]["mis_based_reason"] == "unreachable_in_fitted_range"
       and res4["R2"]["status"] == "out_of_band" and abs(res4["R2"]["reach_in_fitted_range"][0] - 20 / 0.7) < 1e-3,
       "BP25 a cost band no fitted-range value can reach is band_mis_based; a reachable one stays out_of_band with its reach")
    # provenance (workstream B, Codex P1-13 / r2 P1-6)
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        eng.results_path = os.path.join(d, "results.yaml")
        yaml.safe_dump(res, open(eng.results_path, "w"))
        ok(verified_benchmarks(eng)[1] and "provenance" in verified_benchmarks(eng)[1],
           "BP27 results without provenance are not verified (Codex P1-13)")
        yaml.safe_dump(stamp(res, eng), open(eng.results_path, "w"))
        rows2, why = verified_benchmarks(eng)
        ok(why is None and len(rows2) == len(res["benchmarks"]), f"BP28 stamped results for this engine verify: {why}")
        doc = yaml.safe_load(open(eng.results_path))
        doc["engine_identity"]["code_sha256"] = "0" * 64
        yaml.safe_dump(doc, open(eng.results_path, "w"))
        ok("another engine" in (verified_benchmarks(eng)[1] or ""), "BP29 results for other engine code are refused")
        yaml.safe_dump(stamp(res, eng), open(eng.results_path, "w"))
        from toolkit.engines.contract import ParamSpec

        class Refit(type(eng)):
            def parameters(self):
                return [ParamSpec(p.name, 0.55 if p.name == "eff_a" else p.value, p.kind, p.units, p.lo, p.hi,
                                  p.source) for p in super().parameters()]
        r2 = Refit()
        r2.results_path = eng.results_path
        ok("another engine" in (verified_benchmarks(r2)[1] or ""),
           "BP30 results stamped before a fitted parameter changed are refused (Codex r2 P1-6)")

    # ---- v2d δ: estimand types value / identity / coverage, oracles and probes, pack configs
    import types
    om = types.ModuleType("_bp_oracles")
    om.oracle_toy_cases = lambda x, eff_a=0.5, a=1, years=3.0: 0.5 * math.exp(0.8 * x) * (1 - eff_a * a) * years
    om.oracle_sum = lambda a, b: float(a) + float(b)
    om.probe_cases_vec = lambda make_engine, config, x: [make_engine(config).run(
        {}, UnitSpec("u", {"x": x}), Package("p", {}), Horizon(0, 3, (0, 3), {}), 1).per_capita["cases"]["all"]]

    def _gen(rng, mu_sd=1.0, n=20):
        mu = float(rng.normal(0.0, mu_sd))
        return {"y": rng.normal(mu, 1.0, n).tolist()}, {"mu": mu}

    def _cal(make_engine, data, level=0.95, shrink=1.0, mu_sd=1.0):
        y = np.asarray(data["y"])
        prec = 1.0 / mu_sd ** 2 + len(y)
        m, sd = y.sum() / prec, math.sqrt(1.0 / prec) * shrink
        zq = 1.959963984540054
        return {"intervals": {"mu": [m - zq * sd, m + zq * sd]}, "classes": {"mu": "identified"}}
    om.generate_normal = _gen
    om.calibrate_normal = _cal
    sys.modules["_bp_oracles"] = om
    p5 = {"engine": "toolkit.engines.contract:ToyEngine", "oracles": "_bp_oracles", "seeds": [1, 2],
          "horizon": pack["horizon"],
          "settings": {"s": {"unit": {"x": 0.3}}},
          "arms": {"none": {"setting": "s", "package": {}}, "a": {"setting": "s", "package": {"a": 1}}},
          "benchmarks": [
              {"id": "V1", "metric": "toy cases vs closed form", "estimand": {
                  "type": "value", "a": {"arm": "a", "value": "per_capita.cases.all"},
                  "b": {"oracle": "toy_cases", "args": {"x": 0.3}}, "tol": {"rel": 1e-12}}},
              {"id": "V2", "metric": "too tight against a wrong reference", "estimand": {
                  "type": "value", "a": {"arm": "a", "value": "per_capita.cases.all"},
                  "b": {"oracle": "toy_cases", "args": {"x": 0.31}}, "tol": {"abs": 1e-6}}},
              {"id": "V3", "metric": "identity of two computations, nested oracle args", "estimand": {
                  "type": "identity", "a": {"arm": "none", "value": "per_capita.cases.all"},
                  "b": {"oracle": "sum", "args": {"a": {"arm": "a", "value": "per_capita.cases.all"},
                                                  "b": {"arm": "a", "value": "per_capita.cases.all"}}},
                  "tol": {"rel": 1e-12}}},
              {"id": "V4", "metric": "inequality", "estimand": {
                  "type": "value", "compare": "le", "a": {"arm": "a", "value": "per_capita.cases.all"}, "b": 10.0,
                  "tol": {"abs": 0.0}}},
              {"id": "V5", "metric": "negative control: the two must differ", "estimand": {
                  "type": "value", "compare": "differ", "a": {"arm": "none", "value": "per_capita.cases.all"},
                  "b": {"arm": "a", "value": "per_capita.cases.all"}, "tol": {"abs": 0.1}}},
              {"id": "V6", "metric": "vector path and a probe", "estimand": {
                  "type": "value", "a": {"arm": "none", "value": "per_capita_vec.cases"},
                  "b": {"probe": "cases_vec", "args": {"config": None, "x": 0.3}}, "tol": {"abs": 1e-12}}},
              {"id": "V8", "metric": "held out, values not yet transcribed", "estimand": {
                  "type": "value", "not_transcribed": "to be transcribed"}}]}
    ok(validate(p5, eng) == [], f"BP31 a pack with value/identity estimands, oracles, probes and configs validates: "
       f"{validate(p5, eng)}")
    bad5 = dict(p5, settings=dict(p5["settings"], s=dict(p5["settings"]["s"], config="nope")),
                benchmarks=p5["benchmarks"] + [
                    {"id": "V9", "metric": "m", "estimand": {"type": "value", "a": {"oracle": "no_such"}, "b": 1.0,
                                                            "tol": {"abs": 1e-3}}},
                    {"id": "V10", "metric": "m", "estimand": {"type": "value", "a": {"arm": "a", "value": "windows.w.prev"},
                                                             "b": 1.0, "tol": {}}}])
    pr5 = validate(bad5, eng)
    ok(any("unknown config" in s for s in pr5) and any("no_such" in s for s in pr5) and any("tol" in s for s in pr5),
       f"BP32 an unknown config, an unknown oracle and a missing tolerance are refused: {pr5}")
    res5 = run_pack(p5, eng, Runner(p5, eng, 1), {})
    r5 = {x["id"]: x for x in res5["benchmarks"]}
    ok(r5["V1"]["status"] == "in_band" and r5["V2"]["status"] == "out_of_band" and r5["V2"]["error_abs"] > 1e-6,
       "BP33 value: in band against its oracle, out of band against a wrong one, with the error reported")
    ok(r5["V3"]["status"] == "in_band" and r5["V4"]["status"] == "in_band" and r5["V5"]["status"] == "in_band",
       "BP34 identity with nested oracle args; `le` and `differ` comparisons")
    ok(r5["V6"]["status"] == "in_band" and r5["V8"]["status"] == "not_assessable"
       and "transcribed" in r5["V8"]["not_assessable_because"],
       "BP35 vector paths and probes; a not-transcribed benchmark is not_assessable, never a pass")
    p7 = {"engine": "toolkit.engines.empirical.engine:PackageRateEngine", "default_config": "rate2", "seeds": [1],
          "horizon": pack["horizon"],
          "configs": {"rate2": {"components": ["x", "y"], "outcome": "cases"},
                      "rate3": {"components": ["x", "y", "z"], "outcome": "cases"}},
          "settings": {"pr": {"config": "rate2", "unit": {"log_rate": math.log(0.2)}},
                       "pr3": {"config": "rate3", "unit": {"log_rate": math.log(0.2)}, "params": {"lrr:z": math.log(0.5)}}},
          "arms": {"pr0": {"setting": "pr", "package": {}}, "prz": {"setting": "pr3", "package": {"z": 1}}},
          "benchmarks": [
              {"id": "V7", "metric": "a configured engine (pack configs)", "estimand": {
                  "type": "value", "a": {"arm": "pr0", "value": "per_capita.cases.all"}, "b": 0.6, "tol": {"rel": 1e-12}}},
              {"id": "V7b", "metric": "a second configuration of the same engine", "estimand": {
                  "type": "value", "a": {"arm": "prz", "value": "per_capita.cases.all"}, "b": 0.3, "tol": {"rel": 1e-12}}}]}
    e7 = load_engine(p7["engine"], p7["configs"]["rate2"])
    ok(validate(p7, e7) == [], f"BP36a a configured-engine pack validates: {validate(p7, e7)}")
    ok(any("lrr:z" in s for s in validate(dict(p7, settings=dict(p7["settings"], pr=dict(
        p7["settings"]["pr"], params={"lrr:z": 0.1}))), e7)),
       "BP36b a setting parameter is checked against its own configuration's engine")
    res7 = run_pack(p7, e7, Runner(p7, e7, 1), {})
    r7 = {x["id"]: x for x in res7["benchmarks"]}
    cv = res7.get("configs_validated") or {}
    ok(r7["V7"]["status"] == "in_band" and r7["V7b"]["status"] == "in_band" and set(cv) == {"rate2", "rate3"}
       and cv["rate3"]["config"] == {"components": ["x", "y", "z"], "outcome": "cases"}
       and cv["rate2"]["engine_identity"]["config_sha256"] != cv["rate3"]["engine_identity"]["config_sha256"],
       "BP36 configured engines run per setting; results list each validated configuration with its identity")
    k1 = _engine_key("m:C", {"b": 1, "a": [1, 2]})
    ok(k1 == _engine_key("m:C", {"a": [1, 2], "b": 1}) and k1 != _engine_key("m:C", {"a": [1, 3], "b": 1})
       and k1 != _engine_key("m:C", None), "BP37 the worker cache key carries the canonical config")
    p6 = {"engine": "toolkit.engines.contract:ToyEngine", "oracles": "_bp_oracles", "seeds": [1],
          "horizon": pack["horizon"], "settings": {}, "arms": {},
          "benchmarks": [
              {"id": "SBT1", "metric": "exact posterior covers", "estimand": {
                  "type": "coverage", "generator": {"fn": "normal", "args": {"mu_sd": 1.0}},
                  "calibrator": {"fn": "_bp_oracles:calibrate_normal", "args": {}}, "replicates": 400, "seed": 11,
                  "quantities": ["mu"], "band": [0.92, 0.98], "classes": {"mu": ["identified"]}, "class_share_min": 0.9}},
              {"id": "SBT2", "metric": "half-width posterior under-covers (negative control)", "estimand": {
                  "type": "coverage", "generator": {"fn": "normal", "args": {"mu_sd": 1.0}},
                  "calibrator": {"fn": "_bp_oracles:calibrate_normal", "args": {"shrink": 0.5}}, "replicates": 400,
                  "seed": 11, "quantities": ["mu"], "band": [0.0, 0.80]}},
              {"id": "SBT3", "metric": "a calibrator that does not exist yet", "estimand": {
                  "type": "coverage", "generator": {"fn": "normal", "args": {}},
                  "calibrator": {"fn": "toolkit.analysis.no_such_module:posterior", "args": {}}, "replicates": 10,
                  "seed": 1, "quantities": ["mu"], "band": [0.9, 0.99]}}]}
    ok(validate(p6, eng) == [], f"BP38 a coverage pack validates: {validate(p6, eng)}")
    r6 = {x["id"]: x for x in run_pack(p6, eng, Runner(p6, eng, 1), {})["benchmarks"]}
    c1 = r6["SBT1"]
    ok(c1["status"] == "in_band" and 0.92 <= c1["coverage"]["mu"] <= 0.98 and c1["class_share"]["mu"] == 1.0
       and c1["replicates"] == 400 and c1["coverage_se"]["mu"] > 0,
       f"BP39 coverage: an exact posterior covers at the nominal level, with its binomial SE ({c1.get('coverage')})")
    ok(r6["SBT2"]["status"] == "in_band" and r6["SBT2"]["coverage"]["mu"] < 0.8,
       "BP40 coverage: the negative control's under-coverage is detected (in its below-0.80 band)")
    ok(r6["SBT3"]["status"] == "not_assessable" and "calibrator" in r6["SBT3"]["not_assessable_because"],
       "BP41 coverage: a calibrator that cannot be imported is not_assessable, never a pass")
    rr = {x["id"]: x for x in run_pack(p6, eng, Runner(p6, eng, 1), {})["benchmarks"]}
    ok(rr["SBT1"]["coverage"] == c1["coverage"], "BP42 coverage is reproducible (fixed seeds)")
    # Codex round 1 (tests first)
    om.oracle_vec = lambda v: list(v)
    p8 = dict(p5, benchmarks=[
        {"id": "W1", "metric": "relative le per element", "estimand": {
            "type": "value", "compare": "le", "a": {"oracle": "vec", "args": {"v": [1e6, 1.0]}},
            "b": {"oracle": "vec", "args": {"v": [1e6, 1e-6]}}, "tol": {"rel": 0.01}}},
        {"id": "W2", "metric": "an oracle-only benchmark exercises no configuration", "estimand": {
            "type": "value", "a": {"oracle": "sum", "args": {"a": 1, "b": 1}}, "b": 2.0, "tol": {"abs": 0.0}}}],
        configs={"unused": {}})
    res8 = run_pack(p8, eng, Runner(p8, eng, 1), {})
    r8 = {x["id"]: x for x in res8["benchmarks"]}
    ok(r8["W1"]["status"] == "out_of_band", "BP43 (Codex r1-7) a relative `le` is judged element by element")
    ok(res8["configs_validated"] == {} and res8.get("configs_not_exercised") == ["unused"],
       f"BP44 (Codex r1-6) only configurations a benchmark exercised are listed as validated: "
       f"{res8['configs_validated']}, {res8.get('configs_not_exercised')}")
    om.calibrate_nan = lambda make_engine, data, **k: {"intervals": {"mu": [float("nan"), float("nan")]}, "classes": {}}
    p9 = dict(p6, benchmarks=[{"id": "SBN", "metric": "NaN intervals never pass a negative control", "estimand": {
        "type": "coverage", "generator": {"fn": "normal", "args": {}},
        "calibrator": {"fn": "_bp_oracles:calibrate_nan", "args": {}}, "replicates": 20, "seed": 1,
        "quantities": ["mu"], "band": [0.0, 0.80]}}])
    r9 = run_pack(p9, eng, Runner(p9, eng, 1), {})["benchmarks"][0]
    ok(r9["status"] == "out_of_band" and r9.get("invalid_outputs") == 20,
       f"BP45 (Codex r1-8) non-finite or unordered intervals fail the benchmark: {r9['status']}")
    p10 = dict(p7, settings=dict(p7["settings"], pr=dict((k, v) for k, v in p7["settings"]["pr"].items() if k != "config")),
               benchmarks=[p7["benchmarks"][0]])
    r10 = run_pack(p10, e7, Runner(p10, e7, 1), {})
    ok(r10["benchmarks"][0]["status"] == "in_band" and "rate2" in r10["configs_validated"],
       f"BP46 (Codex r2-1) a setting without config runs the pack's default_config, and counts as exercising it: "
       f"{r10['benchmarks'][0]['status']}, {sorted(r10['configs_validated'])}")
    # ---- v2d β: ridge names, a calibrator that refuses, replicates in parallel
    om.calibrate_ridge = lambda make_engine, data, with_ridge=True, **k: dict(
        _cal(make_engine, data), ridges=[{"params": ["mu", "nu"]}] if with_ridge else [])
    sb = {"type": "coverage", "generator": {"fn": "normal", "args": {}}, "replicates": 40, "seed": 2,
          "quantities": ["mu"], "band": [0.85, 1.0], "ridge_names": ["mu", "nu"]}
    p11 = dict(p6, benchmarks=[
        {"id": "R1", "metric": "ridge reported", "estimand": dict(sb, calibrator={"fn": "_bp_oracles:calibrate_ridge"})},
        {"id": "R2", "metric": "ridge missing", "estimand": dict(sb, calibrator={"fn": "_bp_oracles:calibrate_ridge",
                                                                                 "args": {"with_ridge": False}})}])
    r11 = {x["id"]: x for x in run_pack(p11, eng, Runner(p11, eng, 1), {})["benchmarks"]}
    ok(r11["R1"]["status"] == "in_band" and r11["R1"].get("ridge_share") == 1.0 and r11["R2"]["status"] == "out_of_band"
       and r11["R2"].get("ridge_share") == 0.0,
       f"BP47 (v2d β) `ridge_names`: the reported ridge must name them in >= class_share_min of replicates "
       f"({r11['R1'].get('ridge_share')}, {r11['R2'].get('ridge_share')})")

    def _refuse(make_engine, data, **k):
        raise ValueError("posterior_ess: effective sample size 3 below the floor 1000")
    om.calibrate_refuse = _refuse
    p12 = dict(p6, benchmarks=[{"id": "F1", "metric": "a calibrator that refuses", "estimand": dict(
        sb, calibrator={"fn": "_bp_oracles:calibrate_refuse"}, replicates=5, ridge_names=None, band=[0.0, 0.8])}])
    r12 = run_pack(p12, eng, Runner(p12, eng, 1), {})["benchmarks"][0]
    ok(r12["status"] == "out_of_band" and r12.get("invalid_outputs") == 5 and "posterior_ess" in str(r12.get("errors")),
       f"BP48 (v2d β) a calibrator that refuses counts as an invalid replicate (recorded), never under-coverage: "
       f"{r12['status']}, {r12.get('errors')}")
    p13 = dict(p6, oracles="toolkit.engines.benchmark", benchmarks=[{"id": "P1", "metric": "parallel", "estimand": {
        "type": "coverage", "generator": {"fn": "selftest_normal", "args": {}},
        "calibrator": {"fn": "toolkit.engines.benchmark:calibrate_selftest_normal", "args": {}}, "replicates": 30,
        "seed": 4, "quantities": ["mu"], "band": [0.8, 1.0]}}])
    s1 = run_pack(p13, eng, Runner(p13, eng, 1), {})["benchmarks"][0]
    s2 = run_pack(p13, eng, Runner(p13, eng, 2), {})["benchmarks"][0]
    ok(s1["coverage"] == s2["coverage"] and s1.get("per_replicate_sha256") == s2.get("per_replicate_sha256")
       and s1.get("per_replicate_sha256"),
       f"BP49 (v2d β) coverage replicates run in parallel give the serial result ({s1['coverage']}, {s2['coverage']})")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    sys.exit(main())
