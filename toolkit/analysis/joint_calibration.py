#!/usr/bin/env python3
"""The spec adapter of the joint calibration (toolkit/calibration/history_matching.py): ANALYZE's calibration step
before the outcome batch, and a diagnostic command.

    python3 toolkit/analysis/joint_calibration.py <run_dir> [--spec decision_spec.yaml] [--out DIR] [--units FILE]
                                                  [--workers N] [--waves N] [--points-per-unit N]
    python3 toolkit/analysis/joint_calibration.py --self-test

The spec field (signed with the spec; without it nothing changes):

    calibration:
      method: emulator_inversion      # unchanged: attribute, scale, range, target_column, window, metric
      joint:
        shared: [names]               # engine parameters calibrated jointly; each must be a prior the uncertainty
                                      # draws vary (engine-card priors, uncertainty.priors, or an engine_parameters
                                      # value with a range), so its sourced range is its prior and the outcome
                                      # emulator's design spans it
        features: [columns]           # optional unit features of the calibration emulator: a numeric column, or
                                      # {column, count: <separator>, empty: <text>} (the number of listed items)
        discrepancy_sd: 0.1           # model discrepancy sd of every target on its scale (a target may override)
        seed, waves, points_per_unit, shared_points   # optional; defaults in toolkit/calibration/thresholds.yaml
        targets:
          - {id, kind: unit, column, window: [a, b], metric, scale, sd | sd_column | ci_columns: [lo, hi] |
             n_column, bias_sd, discrepancy_sd, source}
          - {id, kind: group, csv, filter: {col: value}, key: {csv: col, units: col}, value, multiplier,
             ci: [lo_col, hi_col], ci_level, weights: <units column>, window, metric, scale, bias_sd, discrepancy_sd,
             source}
          - {id: <benchmark id>, kind: benchmark, discrepancy_sd}   # a fit target of the engine's own pack only

A benchmark the engine's pack holds out (every id not in toolkit/engines/benchmark.py fit_targets) is refused: it
never enters the fit (the PR #140 rule). Unit and group targets are simulated with the comparator package through
the analysis batch (scripts/compute.py, so a Coiled cluster runs them as it runs the outcome batch); benchmark
targets through the benchmark runner at the shared values. Nothing here names a disease.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from toolkit.calibration import history_matching as HM  # noqa: E402

PRODUCER = "toolkit/analysis/joint_calibration.py"
FIELD = "joint"
TARGET_KINDS = ("unit", "group", "benchmark")
OUT_REL = "analysis/joint_calibration"


def _isnum(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def declared(spec) -> dict | None:
    raw = spec.raw if hasattr(spec, "raw") else spec
    j = (raw.get("calibration") or {}).get(FIELD)
    return j if j is not None else None


def _window(w):
    return isinstance(w, list) and len(w) == 2 and all(_isnum(x) for x in w) and w[0] < w[1]


def _feature(row, f):
    if isinstance(f, str):
        return float(row[f])
    v = str(row.get(f["column"], "")).strip()
    if not v or v == str(f.get("empty", "")):
        return 0.0
    return float(len([x for x in v.split(f.get("count") or " ") if x.strip()]))


def _benchmarks(eng):
    from toolkit.engines import benchmark as BM
    from toolkit.spec import engine_parameters as PV
    pack, why = PV.benchmark_pack(eng)
    if pack is None:
        return None, set(), why
    return pack, BM.fit_targets(pack), None


def spec_problems(spec) -> list[str]:
    """Shape and meaning of calibration.joint (fail closed): every reason, or []."""
    j = declared(spec)
    if j is None:
        return []
    w = f"calibration.{FIELD}"
    if not isinstance(j, dict):
        return [f"{w}: a mapping {{shared, targets, ...}}"]
    raw = spec.raw
    cal = raw["calibration"]
    bad = []
    if cal.get("method") != "emulator_inversion":
        bad.append(f"{w}: needs calibration.method emulator_inversion (its attribute, range and scale are the "
                   "per-unit parameter)")
    if (raw.get("analysis") or {}).get("evaluation") != "emulator":
        bad.append(f"{w}: needs analysis.evaluation emulator")
    unk = sorted(set(j) - {"shared", "features", "discrepancy_sd", "seed", "waves", "points_per_unit",
                           "shared_points", "targets"})
    if unk:
        bad.append(f"{w}: unknown keys {unk}")
    sh = j.get("shared")
    if not (isinstance(sh, list) and all(isinstance(x, str) and x for x in sh) and len(set(sh)) == len(sh)):
        bad.append(f"{w}.shared: a list of distinct engine parameter names")
        sh = []
    for k in ("waves", "points_per_unit", "shared_points"):
        if k in j and not (isinstance(j[k], int) and not isinstance(j[k], bool) and j[k] >= 1):
            bad.append(f"{w}.{k}: a positive integer")
    if "seed" in j and not (isinstance(j["seed"], int) and not isinstance(j["seed"], bool)):
        bad.append(f"{w}.seed: an integer")
    if "discrepancy_sd" in j and not (_isnum(j["discrepancy_sd"]) and j["discrepancy_sd"] >= 0):
        bad.append(f"{w}.discrepancy_sd: a number >= 0")
    try:
        cols = set(spec.units()[0]) if spec.units() else set()
    except (OSError, KeyError) as e:
        return bad + [f"{w}: the units table is unreadable ({e})"]
    for f in j.get("features") or []:
        c = f if isinstance(f, str) else (f.get("column") if isinstance(f, dict) else None)
        if c not in cols:
            bad.append(f"{w}.features: no units column {c!r}")
    if not spec.engine_pending:
        from toolkit.spec import priors as PRI
        eng = spec.engine()
        try:
            dr = PRI.drawn(spec)
        except Exception as e:  # noqa: BLE001 -- the priors' own check names it
            dr = {}
            bad.append(f"{w}: the priors cannot be read ({e})")
        for s in sh:
            if s not in dr:
                bad.append(f"{w}.shared: {s} is not a prior the uncertainty draws vary (an engine-card prior, an "
                           "uncertainty.priors entry or an engine_parameters value with a range): a shared parameter "
                           "needs a sourced range, and the outcome emulator must span it")
        card = eng.describe()
        wins = card.outputs.get("windows", [])
    else:
        eng, wins = None, None
    tg = j.get("targets")
    if not (isinstance(tg, list) and tg):
        return bad + [f"{w}.targets: a non-empty list"]
    ids = []
    for n, t in enumerate(tg):
        tw = f"{w}.targets[{n}]"
        if not isinstance(t, dict):
            bad.append(f"{tw}: a mapping")
            continue
        ids.append(t.get("id"))
        k = t.get("kind")
        if k not in TARGET_KINDS:
            bad.append(f"{tw}: kind one of {TARGET_KINDS}")
            continue
        if not (isinstance(t.get("id"), str) and t["id"]):
            bad.append(f"{tw}: id (text)")
        if "discrepancy_sd" in t and not (_isnum(t["discrepancy_sd"]) and t["discrepancy_sd"] >= 0):
            bad.append(f"{tw}.discrepancy_sd: a number >= 0")
        if k == "benchmark":
            if eng is not None:
                pack, fit, why = _benchmarks(eng)
                ids_pack = {b["id"] for b in (pack or {}).get("benchmarks") or []}
                if pack is None:
                    bad.append(f"{tw}: the engine has no benchmark pack ({why})")
                elif t.get("id") not in ids_pack:
                    bad.append(f"{tw}: {t.get('id')} is not a benchmark of the engine's pack")
                elif t["id"] not in fit:
                    bad.append(f"{tw}: {t['id']} is a held-out benchmark of the engine's pack; a held-out benchmark "
                               "never enters a fit (only the pack's fit targets may)")
            dep = t.get("depends_on")
            if dep is not None and not (isinstance(dep, list) and dep and set(dep) <= set(sh)):
                bad.append(f"{tw}.depends_on: a non-empty list of shared parameters (the ones the benchmark's settings "
                           "use; the emulator of this benchmark takes only those)")
            continue
        if not _window(t.get("window")):
            bad.append(f"{tw}.window: [a, b] with a < b")
        if wins is not None and t.get("metric") not in wins:
            bad.append(f"{tw}.metric: {t.get('metric')!r} is not an engine window metric")
        if t.get("scale", "logit") not in HM.SCALES:
            bad.append(f"{tw}.scale: one of {HM.SCALES}")
        if not str(t.get("source") or "").strip():
            bad.append(f"{tw}.source: the observed values' source")
        for q in ("bias_sd", "sd"):
            if q in t and not (_isnum(t[q]) and t[q] >= 0):
                bad.append(f"{tw}.{q}: a number >= 0")
        if k == "unit":
            c = t.get("column", cal.get("target_column"))
            if c not in cols:
                bad.append(f"{tw}.column: no units column {c!r}")
            for q in ("sd_column", "n_column"):
                if q in t and t[q] not in cols:
                    bad.append(f"{tw}.{q}: no units column {t[q]!r}")
            if "ci_columns" in t and not (isinstance(t["ci_columns"], list) and len(t["ci_columns"]) == 2
                                          and all(c in cols for c in t["ci_columns"])):
                bad.append(f"{tw}.ci_columns: [lo, hi] units columns")
        else:
            for q in ("csv", "value", "weights"):
                if not t.get(q):
                    bad.append(f"{tw}.{q}: required for a group target")
            key = t.get("key")
            if not (isinstance(key, dict) and key.get("csv") and key.get("units") in cols):
                bad.append(f"{tw}.key: {{csv: <column>, units: <units column>}}")
            if t.get("weights") and t["weights"] not in cols:
                bad.append(f"{tw}.weights: no units column {t['weights']!r}")
            if not (isinstance(t.get("ci"), list) and len(t["ci"]) == 2) and "sd" not in t:
                bad.append(f"{tw}: ci: [lo, hi] columns of the table, or sd")
    if len(set(ids)) != len(ids):
        bad.append(f"{w}.targets: repeated ids")
    return bad


# --------------------------------------------------------------------------- problem
def theta0(spec) -> dict:
    """The point parameter values every simulation starts from (as toolkit/analysis/core.py Analysis.theta0)."""
    from toolkit.analysis.batch import fitted_ranges
    from toolkit.spec import engine_parameters as PV
    from toolkit.spec import priors as PRI
    eng = spec.engine()
    dr = dict(fitted_ranges(eng), **{k: (p.lo, p.hi, p.value) for k, p in PRI.drawn(spec).items()})
    return PV.params(spec, {k: v[2] for k, v in dr.items()})


def _unit_sd(t, row, scale, value):
    if "sd_column" in t:
        return float(row[t["sd_column"]])
    if "ci_columns" in t:
        return HM.sd_from_ci(float(row[t["ci_columns"][0]]), float(row[t["ci_columns"][1]]), scale,
                             float(t.get("ci_level", 0.95)))
    if "n_column" in t:
        return HM.sd_from_n(value, float(row[t["n_column"]]), scale)
    return float(t.get("sd", 0.0))


def _windows(j) -> dict:
    """{window key: [a, b]}, one key per distinct window of the unit and group targets (jc0, jc1, ...)."""
    out = {}
    for t in j["targets"]:
        if t["kind"] in ("unit", "group"):
            w = [float(x) for x in t["window"]]
            if w not in out.values():
                out[f"jc{len(out)}"] = w
    return out


def _observable(t, wins):
    key = next(k for k, v in wins.items() if v == [float(x) for x in t["window"]])
    return f"{key}.{t['metric']}"


def build(spec, units=None) -> tuple:
    """(Problem, meta) from the spec. `units`: a subset of unit ids (diagnostics only); group targets then need every
    unit of their group in the subset, or they are dropped and listed in meta['dropped']."""
    bad = spec_problems(spec)
    if bad:
        raise HM.CalibrationInputError(bad)
    from toolkit.spec import priors as PRI
    j = declared(spec)
    cal = spec.raw["calibration"]
    dr = PRI.drawn(spec)
    shared = [HM.Param(n, dr[n].lo, dr[n].hi, dr[n].dist, dr[n].value, dr[n].source) for n in j["shared"]]
    rows = {str(r[spec.uid]): r for r in spec.units()}
    ids = [i for i in spec.unit_ids() if units is None or i in set(units)]
    missing = sorted(set(units or []) - set(rows))
    if missing:
        raise HM.CalibrationInputError([f"--units: not units of the spec: {missing[:5]}"])
    wins = _windows(j)
    md0 = float(j.get("discrepancy_sd", 0.0))
    targets, dropped = [], []
    for t in j["targets"]:
        md = float(t.get("discrepancy_sd", md0))
        if t["kind"] == "unit":
            col = t.get("column", cal.get("target_column"))
            sc = t.get("scale", "logit")
            for i in ids:
                v = float(rows[i][col])
                targets.append(HM.Target(f"{t['id']}:{i}", "unit", _observable(t, wins), v, sc,
                                         sd_obs=_unit_sd(t, rows[i], sc, v), sd_bias=float(t.get("bias_sd", 0.0)),
                                         sd_md=md, unit=i, source=t["source"]))
        elif t["kind"] == "group":
            sc = t.get("scale", "logit")
            mult = float(t.get("multiplier", 1.0))
            with open(spec.resolve(t["csv"]), newline="", encoding="utf-8") as f:
                tab = [r for r in csv.DictReader(f)
                       if all(str(r.get(k)) == str(v) for k, v in (t.get("filter") or {}).items())]
            if not tab:
                raise HM.CalibrationInputError([f"group target {t['id']}: no row of {t['csv']} matches "
                                                f"{t.get('filter')}"])
            for r in tab:
                g = str(r[t["key"]["csv"]])
                members_all = [i for i, u in rows.items() if str(u[t["key"]["units"]]) == g]
                members = [i for i in members_all if i in ids]
                if not members_all:
                    raise HM.CalibrationInputError([f"group target {t['id']}: group {g!r} has no unit in the units "
                                                    f"table (key {t['key']})"])
                if len(members) < len(members_all):
                    dropped.append({"target": f"{t['id']}:{g}", "why": f"{len(members)} of {len(members_all)} units "
                                                                       "of the group are calibrated"})
                    continue
                v = mult * float(r[t["value"]])
                sd = (HM.sd_from_ci(mult * float(r[t["ci"][0]]), mult * float(r[t["ci"][1]]), sc,
                                    float(t.get("ci_level", 0.95))) if t.get("ci") else float(t["sd"]))
                targets.append(HM.Target(f"{t['id']}:{g}", "group", _observable(t, wins), v, sc, sd_obs=sd,
                                         sd_bias=float(t.get("bias_sd", 0.0)), sd_md=md,
                                         weights={i: float(rows[i][t["weights"]]) for i in members},
                                         source=t["source"]))
        else:
            pack, fit, _ = _benchmarks(spec.engine())
            b = next(x for x in pack["benchmarks"] if x["id"] == t["id"])
            lo, hi = (float(x) for x in b["band"])
            sc = "log" if lo > 0 and float(b["target"]) > 0 else "identity"
            sd = (float(HM.tr(hi, sc)) - float(HM.tr(lo, sc))) / (2 * 1.959964)
            targets.append(HM.Target(t["id"], "shared", f"bench.{t['id']}", float(b["target"]), sc, sd_obs=sd,
                                     sd_md=md, source=f"engine benchmark pack: {b.get('source', '')} (band as a 95% "
                                                      "interval)", depends_on=t.get("depends_on")))
    feats = j.get("features") or []
    F = {i: [_feature(rows[i], f) for f in feats] for i in ids} if feats else {}
    up = {"name": cal["attribute"], "lo": float(cal["range"][0]), "hi": float(cal["range"][1]),
          "scale": cal.get("scale", "log")}
    P = HM.Problem(shared, up, ids, targets, features=F,
                   feature_names=[f if isinstance(f, str) else f["column"] for f in feats])
    return P, {"windows": wins, "dropped": dropped, "units": ids}


# --------------------------------------------------------------------------- simulation
class Simulator:
    """HistoryMatch's simulate(): unit points through the analysis batch (comparator package), shared points through
    the benchmark runner. Every call writes its own results file under analysis/joint_calibration/."""

    def __init__(self, spec, run_dir, meta, compute_kw=None, log=print):
        from toolkit.analysis.batch import calibrated_value
        self.spec, self.run_dir, self.meta, self.log = spec, run_dir, meta, log
        self.kw = dict(compute_kw or {})
        self.eng = spec.engine()
        self.th0 = theta0(spec)
        self.cv = calibrated_value
        self.calls = 0
        self.rows = {str(r[spec.uid]): r for r in spec.units()}
        pk = spec.packages()
        self.comp = spec.comparator
        self.comp_engine = spec.engine_package(pk[self.comp])
        h = spec.raw["horizon"]
        self.horizon = {"start": h["start"], "stop": h["stop"], "window": list(spec.raw["objective"]["horizon"])}
        if spec.raw["objective"].get("delivery") is not None:
            self.horizon["delivery"] = [float(x) for x in spec.raw["objective"]["delivery"]]
        self.bench = None

    def _theta(self, th):
        from toolkit.spec import engine_parameters as PV
        return PV.params(self.spec, dict({k: v for k, v in self.th0.items()}, **th))

    def _shared(self, pts, six, out):
        """Benchmark targets at each point's shared values: the pack's settings re-calibrated there, then each
        benchmark evaluated (the runner's own process pool)."""
        from toolkit.analysis.batch import fitted_ranges
        from toolkit.engines import benchmark as BM
        pack, _, _ = _benchmarks(self.eng)
        byid = {b["id"]: b for b in pack["benchmarks"]}
        fitted = {k: v[2] for k, v in fitted_ranges(self.eng).items()}
        want = [t for t in self.spec.raw["calibration"][FIELD]["targets"] if t["kind"] == "benchmark"]
        settings = sorted({pack["arms"][a]["setting"] for t in want for a in BM._arms_of(byid[t["id"]]["estimand"])})
        for n in six:
            R = BM.Runner(pack, self.eng, workers=int(self.kw.get("bench_workers", self.kw.get("max_workers", 2))),
                          base_params={k: v for k, v in self._theta(pts[n]["theta"]).items() if k not in fitted})
            R.calibrate_settings(fitted, settings)
            out[n] = {f"bench.{t['id']}": float(R.evaluate(byid[t["id"]], fitted)["computed"]) for t in want}

    def __call__(self, pts):
        from toolkit.analysis import batch as Bt
        from concurrent.futures import ThreadPoolExecutor
        self.calls += 1
        out = [None] * len(pts)
        uix = [n for n, p in enumerate(pts) if p["kind"] == "unit"]
        six = [n for n, p in enumerate(pts) if p["kind"] == "shared"]
        attr = self.spec.raw["calibration"]["attribute"]
        ex = ThreadPoolExecutor(1)       # benchmark settings run here while the unit batch runs on the compute backend
        fut = ex.submit(self._shared, pts, six, out) if six else None
        try:
            self._units(pts, uix, out, attr, Bt)
        finally:
            if fut is not None:
                fut.result()
            ex.shutdown()
        return out

    def _units(self, pts, uix, out, attr, Bt):
        if uix:
            jobs = []
            for k, n in enumerate(uix):
                p = pts[n]
                a = self.spec.unit_attributes(self.rows[p["unit"]])
                a[attr] = self.cv(self.spec, p["z"])
                jobs.append({"id": f"jc{self.calls}|{k}", "point": k, "package": self.comp,
                             "components": self.comp_engine, "unit": a, "params": self._theta(p["theta"]),
                             "seed": int(p["seed"]) % (2 ** 31), "engine": self.spec.raw["engine"],
                             "horizon": self.horizon, "windows": self.meta["windows"]})
            b = Bt.run(self.run_dir, jobs, self.eng, out_rel=f"{OUT_REL}/call{self.calls}/results.jsonl",
                       **{k: v for k, v in self.kw.items() if k != "bench_workers"})
            if b["problems"]:
                raise HM.CalibrationInputError([f"joint calibration batch: {len(b['problems'])} rows lack matching "
                                                f"provenance (e.g. {b['problems'][:2]})"])
            for k, n in enumerate(uix):
                r = b["results"].get(jobs[k]["id"])
                if r is None:
                    raise HM.CalibrationInputError([f"joint calibration batch: job {jobs[k]['id']} (unit "
                                                    f"{pts[n]['unit']}) did not complete"])
                out[n] = {f"{w}.{m}": v for w, d in (r.get("windows") or {}).items() for m, v in d.items()}


# --------------------------------------------------------------------------- the step
def run(spec, run_dir, compute_kw=None, log=print, units=None, **override) -> dict:
    """The joint calibration of the spec: the history-matching report, plus the adapter's meta. Raises
    CalibrationInputError on a refused declaration; a blocked result is returned (report['blocked'])."""
    P, meta = build(spec, units)
    j = declared(spec)
    ov = {k: j[k] for k in ("points_per_unit", "shared_points") if k in j}
    ov.update(override)
    sim = Simulator(spec, run_dir, meta, compute_kw, log)
    rep = HM.calibrate(P, sim, seed=int(j.get("seed", 0)), waves=j.get("waves"), log=log, **ov)
    rep["adapter"] = {"windows": meta["windows"], "dropped_targets": meta["dropped"], "units": len(meta["units"]),
                      "shared": list(j["shared"]), "simulator_calls": sim.calls,
                      "theta_held": {k: v for k, v in sim.th0.items() if k not in j["shared"]}}
    return rep


def region_sample(rep) -> dict:
    """The canonical sample the uncertainty draws read: every region row, shared values and each unit's value
    together (Codex r1 P1-2: a unit's value comes from the joint fit of all its targets, never from re-inverting one
    target)."""
    s = rep["sample"]
    return {"sample_id": s["sample_id"], "names": list(s["names"]), "rows": np.asarray(s["rows"], float)}


def block_hits(rep) -> list:
    """The blocked calibration as AnalysisBlocked hits: one per unmatched or conflicting unit, one per other reason."""
    hits = []
    for u in rep["unmatched"] + rep["conflicting"]:
        x = rep["units"][u]
        hits.append({"unit": u, "package": "*", "kind": f"calibration_{x['status']}",
                     "best_implausibility": round(x["best_implausibility"], 3), "direction": x["direction"]})
    for u, x in rep["units"].items():
        if x["status"] == "verification_failed":
            hits.append({"unit": u, "package": "*", "kind": "calibration_verification_failed"})
    for b in rep["blocks"]:
        if b["code"] not in ("unmatched_units", "verification_failed"):
            hits.append({"unit": "*", "package": "*", "kind": f"calibration_{b['code']}", "detail": b["detail"]})
    return hits


def inputs(spec) -> list:
    """The files the joint calibration reads beyond the units table: its group-target tables."""
    j = declared(spec) or {}
    return sorted({spec.resolve(t["csv"]) for t in j.get("targets") or [] if t.get("kind") == "group"})


def block_reason(rep) -> str:
    parts = []
    if rep["unmatched"]:
        parts.append(f"{len(rep['unmatched'])} units no value in the prior box matches")
    if rep["conflicting"]:
        parts.append(f"{len(rep['conflicting'])} units unmatched at every compatible shared value")
    parts += [b["code"] for b in rep["blocks"] if b["code"] not in ("unmatched_units", "conflicting_units")]
    return "joint calibration blocked: " + "; ".join(parts or [b["code"] for b in rep["blocks"]])


def artifact(rep) -> dict:
    """The analysis/joint_calibration.yaml payload: the report without the bulky sample rows (their id is kept)."""
    out = {k: v for k, v in rep.items() if k != "sample"}
    if rep.get("sample"):
        s = region_sample(rep)
        out["sample"] = {"sample_id": s["sample_id"], "names": s["names"], "rows": s["rows"].tolist(),
                         "full_sample_id": rep["sample"]["sample_id"], "weighting": rep["sample"]["weighting"]}
    return out


def summary(rep) -> str:
    L = [f"joint calibration: {rep['status']}; {rep['n_matched']} of {rep['n_units']} units matched; region "
         f"{rep['region']['accepted']} of {rep['region']['candidates']} candidates (share of prior "
         f"{rep['region']['share_of_prior']})"]
    for n, r in rep["region"]["ranges"].items():
        L.append(f"  {n}: region {r} vs prior {rep['region']['prior_ranges'][n]} (contraction "
                 f"{rep['region']['contraction'][n]})")
    if rep["unmatched"] or rep["conflicting"]:
        L.append(f"  unmatched {len(rep['unmatched'])}, conflicting {len(rep['conflicting'])}")
    for b in rep["blocks"]:
        L.append(f"  block: {b['code']}")
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_dir", nargs="?")
    ap.add_argument("--spec", default="decision_spec.yaml")
    ap.add_argument("--out", help="diagnostic output directory (default <run_dir>/analysis/joint_calibration)")
    ap.add_argument("--units", help="a file of unit ids, one per line (diagnostics only)")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--bench-workers", type=int, help="processes of the benchmark runner (default --workers)")
    ap.add_argument("--max-core-hours", type=float, help="compute.py allowance per batch call")
    ap.add_argument("--waves", type=int)
    ap.add_argument("--points-per-unit", type=int)
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)
    if a.self_test:
        return _self_test()
    if not a.run_dir:
        ap.error("run_dir is required")
    import yaml
    from toolkit.spec import schema
    rd = os.path.abspath(a.run_dir)
    spec = schema.load(os.path.join(rd, a.spec))
    units = [x.strip() for x in open(a.units) if x.strip()] if a.units else None
    kw = {"max_workers": a.workers, "bench_workers": a.bench_workers or a.workers}
    if a.max_core_hours:
        kw["max_core_hours"] = a.max_core_hours
    ov = {}
    if a.points_per_unit:
        ov["points_per_unit"] = a.points_per_unit
    if a.waves:
        spec.raw["calibration"][FIELD]["waves"] = a.waves
    rep = run(spec, rd, kw, log=lambda *x, **k: print(*x, flush=True), units=units, **ov)
    out = os.path.abspath(a.out or os.path.join(rd, OUT_REL))
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "joint_calibration.yaml"), "w") as f:
        yaml.safe_dump(HM_plain(artifact(rep)), f, sort_keys=False)
    print(summary(rep), flush=True)
    return 0 if not rep["blocked"] else 3


def HM_plain(x):
    from toolkit.analysis.artifacts import plain
    return plain(x)


# --------------------------------------------------------------------------- self-test
def _toy_joint(d, n=12, unreachable=None, shared=("base",), held_out=False, group=True):
    """The toy spec (toolkit/spec/schema.py toy_spec) with a prior on `base` and a joint calibration block."""
    import yaml
    from toolkit.spec import schema
    p = schema.toy_spec(d, n=n)
    raw = yaml.safe_load(open(p))
    rows = list(csv.DictReader(open(os.path.join(d, "units.csv"))))
    if unreachable is not None:
        for r in rows:
            if r["id"] == unreachable:
                r["tgt"] = "0.9999"
        with open(os.path.join(d, "units.csv"), "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
    with open(os.path.join(d, "region_targets.csv"), "w") as f:
        f.write("region,level,prev,lo,hi\n")
        for g in sorted({r["region"] for r in rows}):
            m = [r for r in rows if r["region"] == g]
            ws = sum(float(r["pop"]) for r in m)
            v = sum(float(r["pop"]) / ws * float(r["tgt"]) for r in m)
            f.write(f"{g},region,{v:.5f},{max(v - 0.05, 0.001):.5f},{min(v + 0.05, 0.999):.5f}\n")
    raw["uncertainty"]["priors"] = {"base": {"dist": "uniform", "range": [0.3, 0.7], "source": "toy range"}}
    tg = [{"id": "unit_t", "kind": "unit", "column": "tgt", "window": [1, 2], "metric": "prev", "scale": "logit",
           "sd": 0.1, "source": "toy targets"}]
    if group:
        tg.append({"id": "region_t", "kind": "group", "csv": "region_targets.csv", "filter": {"level": "region"},
                   "key": {"csv": "region", "units": "region"}, "value": "prev", "ci": ["lo", "hi"],
                   "weights": "pop", "window": [1, 2], "metric": "prev", "scale": "logit", "source": "toy survey"})
    if held_out:
        tg.append({"id": "T2", "kind": "benchmark"})
    raw["calibration"]["joint"] = {"shared": list(shared), "discrepancy_sd": 0.05, "seed": 3, "waves": 1,
                                   "points_per_unit": 6, "targets": tg}
    yaml.safe_dump(raw, open(p, "w"), sort_keys=False)
    return p


def _self_test() -> int:
    import tempfile
    from toolkit.spec import schema
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)

    def safe(fn, msg):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            ok(False, f"{msg}: raised {type(e).__name__}: {e}")

    q = lambda *a, **k: None  # noqa: E731
    fast = {"candidates_log2": 8}

    def ja1():
        with tempfile.TemporaryDirectory() as d:
            p = _toy_joint(d, shared=("k_not_a_prior",))
            bad = spec_problems(schema.load(p))
            ok(any("not a prior the uncertainty draws vary" in b for b in bad),
               "JA1 a shared parameter without a sourced prior is refused")
    safe(ja1, "JA1")

    def ja2():
        with tempfile.TemporaryDirectory() as d:
            from toolkit.spec import toy_engines as TE
            TE.enable_for_test()
            p = _toy_joint(d)
            import yaml
            raw = yaml.safe_load(open(p))
            raw["engine"] = {"ref": "toolkit.spec.toy_engines:ToySpecEngine"}
            raw["calibration"]["joint"]["targets"].append({"id": "TH1", "kind": "benchmark"})
            yaml.safe_dump(raw, open(p, "w"), sort_keys=False)
            bad = spec_problems(schema.load(p))
            ok(any("held-out benchmark" in b and "TH1" in b for b in bad),
               f"JA2a a held-out benchmark of the engine's pack never enters the fit ({bad})")
            raw["calibration"]["joint"]["targets"][-1]["id"] = "TF1"
            yaml.safe_dump(raw, open(p, "w"), sort_keys=False)
            bad = spec_problems(schema.load(p))
            ok(not any("TF1" in b for b in bad), f"JA2b a fit target of the pack is accepted ({bad})")
            raw["calibration"]["joint"]["targets"][-1]["id"] = "NOPE"
            yaml.safe_dump(raw, open(p, "w"), sort_keys=False)
            ok(any("not a benchmark" in b for b in spec_problems(schema.load(p))), "JA2c an unknown benchmark id")
    safe(ja2, "JA2")

    def ja3():
        with tempfile.TemporaryDirectory() as d:
            p = _toy_joint(d)
            P, meta = build(schema.load(p))
            g = [t for t in P.targets if t.kind == "group"]
            u = [t for t in P.targets if t.kind == "unit"]
            ok(len(g) == 3 and len(u) == 12 and all(t.sd_obs > 0 and t.sd_md == 0.05 for t in g)
               and meta["windows"] == {"jc0": [1.0, 2.0]} and u[0].observable == "jc0.prev",
               "JA3a unit and group targets built from the spec: sampling sd from the table's interval, the declared "
               "discrepancy, one window key per distinct window")
            P2, meta2 = build(schema.load(p), units=[str(i) for i in range(6)])
            ok(len([t for t in P2.targets if t.kind == "group"]) == 0 and len(meta2["dropped"]) == 3,
               "JA3b with a subset of units, a group target missing some of its units is dropped and listed")
    safe(ja3, "JA3")

    def ja4():
        with tempfile.TemporaryDirectory() as d:
            p = _toy_joint(d)
            rep = run(schema.load(p), d, {"max_workers": 2}, log=q, **fast)
            ok(rep["status"] == "matched" and rep["n_matched"] == 12 and rep["sample"] is not None,
               f"JA4a the toy engine through the analysis batch: every unit matched ({summary(rep)})")
            ok(os.path.exists(os.path.join(d, OUT_REL, "call1", "results.jsonl")),
               "JA4b the simulations went through the analysis batch (scripts/compute.py), with provenance")
            s = region_sample(rep)
            from toolkit.analysis import calibration as C
            ok(s["names"] == ["base"] + [f"z:{i}" for i in range(12)] and C.check_provenance(s, C.draws(s, 5, 1)) == [],
               "JA4c the region sample is a canonical sample the uncertainty draws can cite")
    safe(ja4, "JA4")

    def ja5():
        with tempfile.TemporaryDirectory() as d:
            p = _toy_joint(d, unreachable="5", group=False)
            rep = run(schema.load(p), d, {"max_workers": 2}, log=q, **fast)
            hits = block_hits(rep)
            ok(rep["blocked"] and "5" in rep["unmatched"] and any(h["unit"] == "5" and h["direction"] ==
                                                                   "target_above_reach" for h in hits),
               f"JA5 a unit whose target the toy cannot reach is unmatched and blocks, with the direction ({hits[:2]})")
    safe(ja5, "JA5")

    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
