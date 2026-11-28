#!/usr/bin/env python3
"""Effect-fit core (W0-C): fit an engine's intervention-effect parameters jointly to trial results, each target
simulated at its trial's own estimand (decision design principle 3: relative effects come from trials, and a
parameter is anchored where the trial measured it, never by multiplying decision outputs by a trial ratio).

    python3 toolkit/engines/effect_fit.py check <engine>/benchmarks/effect_fit.yaml   # problems vs pack, coverage
    python3 toolkit/engines/effect_fit.py --self-test

An engine pack declares the fit in data (`effect_fit.yaml` beside its `pack.yaml`); nothing here names a disease,
an intervention or a parameter:

    schema: effect_fit/v1
    targets:                # training targets: benchmark ids of the pack, each with its trial's estimand
      <id>: {contrast: {treatment: <arm>, comparator: <arm>}, outcome: <text>, scale: ratio | odds_ratio,
             window: <horizon window>, population: <text>, measurement: <text>, mapping: <optional text>}
    monitor: [<id>]         # evaluated at the verified points and reported; never used to choose
    parameters: {<name>: {box: [lo, hi], source: <text>}}
    confounded: [[<name>, <name>]]
    design: {waves: [{n, seed, box: {<name>: [lo, hi]} (optional)}]}
    history_matching: {cutoff: <number>, candidate_seed: <int>}
    acceptance: {rule: seed_interval_overlaps_band, level, verify_n, pick_seed, sample_n, control_tolerance,
                 must_meet: [<id>] (optional)}
    edge_checks: [{param, side: lo | hi, within, targets: [<id>], basis}]   (optional)

The estimand fields are checked against the pack, not only present: the contrast must be the benchmark's arms, the
scale its estimand type, and the window the one its outcome is read over (and a window of each arm's setting).

Method. Each design wave is a Latin hypercube in its box (the first adds the box centre). Each target's log value
(geometric mean over the pack's seeds) gets a quadratic surrogate with its leave-one-out error (LOO). On dense
candidates drawn inside each wave's box (never extrapolated past engine-evaluated points), a candidate's
implausibility for a target is its surrogate's signed distance outside the target's band in units of that LOO:
max(log lo - m, m - log hi) / LOO, negative inside the band. A candidate is not implausible when every target's
implausibility is at most `cutoff` (0: surrogate in band; -1: inside by one LOO; 3: the usual history-matching
allowance for emulator error). The chosen point is the region's least band-scaled squared log error (CHOICE).
Every engine row holds exactly one finite positive value per declared seed, or the fit stops (fail closed).
`verify_n` points drawn from the not-implausible region and the chosen point are re-run on the engine and judged by
the declared acceptance rule: a target is met when the `level` interval of the seed geometric mean,
exp(mean(log v) +- z sd(log v) / sqrt(n)), overlaps its band; control arms the evaluator reports must be within
`control_tolerance` of the pack's own calibration target for their setting.

Conflicts (the rule on v2, #312: declare and test, not a stop). A target no candidate in the support can meet is
recorded under `conflicts` with its least implausibility; the fit continues on the other targets and the record's
status is `accepted_with_conflicts`, for the conflict rule to declare and test. A conflict on a `must_meet` target,
two meetable targets with no joint candidate, fewer verified points than declared, or any verified point failing
the rule makes it `rejected`, and its sample is withdrawn (`sample_status` invalid).

The record (`write_record`; the spec references it by sha256, `read_record` refuses other bytes) holds the point,
`joint_region` (hull, edge flags, correlation, sample, `sample_status`), per-target implausibility and verified
intervals, monitors, failures, conflicts, target digests, engine identity and cache digest. Engine rows are keyed by
the targets' resolved declarations, the engine identity and the seeds: a changed estimand or engine reuses none.
"""
from __future__ import annotations

import hashlib
import inspect
import itertools
import json
import math
import os
import statistics
import sys

import numpy as np
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from toolkit.engines import benchmark as BM  # noqa: E402

SCHEMA = "effect_fit/v1"
PRODUCER = "toolkit/engines/effect_fit.py"
KEYS = {"schema", "targets", "monitor", "parameters", "confounded", "design", "history_matching", "acceptance",
        "edge_checks"}
ESTIMAND_FIELDS = ("contrast", "outcome", "scale", "window", "population", "measurement")
FIT_SCALES = ("ratio", "odds_ratio")
RULES = {"seed_interval_overlaps_band": (
    "a point meets a target when the `level` interval of its seed geometric mean, exp(mean(log v) +- z sd(log v) / "
    "sqrt(n)) with sd on n - 1, overlaps the target's band; exactly one finite, positive value per declared seed, "
    "else the point fails; control arms within `control_tolerance` (relative) of their calibration target")}
ACCEPTANCE_KEYS = ("rule", "level", "verify_n", "pick_seed", "sample_n", "control_tolerance")
CHOICE = ("least band-scaled squared log error inside the not-implausible region; when it is empty, fewest targets "
          "past the cutoff, then least error")


def _sha(x) -> str:
    return hashlib.sha256(json.dumps(x, sort_keys=True, default=str).encode()).hexdigest()


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _int(x) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def _txt(x) -> bool:
    return isinstance(x, str) and bool(x.strip())


def _arm_ok(x) -> bool:
    return _txt(x) or (isinstance(x, list) and bool(x) and all(_txt(a) for a in x))


# ------------------------------------------------------------------------------------------- declaration
def _window_of(path) -> str | None:
    parts = str(path or "").split(".")
    return parts[1] if len(parts) >= 3 and parts[0] == "windows" else None


def _setting_windows(pack: dict, arm: str) -> set:
    s = (pack.get("settings") or {}).get((pack.get("arms") or {}).get(arm, {}).get("setting")) or {}
    return set(((s.get("horizon") or pack.get("horizon") or {}).get("windows") or {}))


def estimand_problems(tid: str, est, bench: dict | None, pack: dict) -> list:
    """Why `est` does not state, or does not match, the trial estimand of benchmark `tid` in `pack`."""
    if not isinstance(est, dict):
        return [f"{tid}: estimand fields missing (a mapping of {list(ESTIMAND_FIELDS)})"]
    out = []
    miss = [k for k in ESTIMAND_FIELDS if not (est.get(k) if k == "contrast" else _txt(est.get(k)))]
    if miss:
        out.append(f"{tid}: estimand without {miss}: a training target states its trial's contrast, outcome, scale, "
                   "time window, population and measurement")
    if bench is None:
        return out + [f"{tid}: not a benchmark of the pack"]
    e = bench.get("estimand") or {}
    band = bench.get("band")
    if not (isinstance(band, list) and len(band) == 2 and all(_num(v) and v > 0 for v in band) and band[0] < band[1]
            and _num(bench.get("target"))):
        out.append(f"{tid}: the pack gives no positive band [lo, hi] and target")
    if e.get("type") not in FIT_SCALES:
        out.append(f"{tid}: pack estimand type {e.get('type')!r} is not a two-arm trial contrast ({list(FIT_SCALES)})")
    elif _txt(est.get("scale")) and est["scale"] != e["type"]:
        out.append(f"{tid}: scale {est['scale']!r} is not the pack's estimand type {e['type']!r}")
    c = est.get("contrast")
    if c:
        if not (isinstance(c, dict) and _arm_ok(c.get("treatment")) and _arm_ok(c.get("comparator"))):
            out.append(f"{tid}: contrast: {{treatment: <arm>, comparator: <arm>}}")
        elif [c["treatment"], c["comparator"]] != [e.get("num"), e.get("den")]:
            out.append(f"{tid}: contrast {c['treatment']} vs {c['comparator']} is not the pack's {e.get('num')} vs "
                       f"{e.get('den')}")
    arms = [a for k in ("num", "den") for a in (e.get(k) if isinstance(e.get(k), list) else [e.get(k)])]
    A, S = pack.get("arms") or {}, pack.get("settings") or {}
    gone = sorted(str(a) for a in arms if not (isinstance(a, str) and isinstance(A.get(a), dict)
                                                 and isinstance(A[a].get("setting"), str) and A[a]["setting"] in S))
    if gone:
        out.append(f"{tid}: arms {gone} are not arms of the pack with a declared setting")
    if "value" in e and "values" in e:
        out.append(f"{tid}: the pack's estimand gives both value and values (ambiguous outcome)")
    w = est.get("window")
    if _txt(w) and not gone:
        paths = [e.get("value")] if "values" not in e else [v.get("path") if isinstance(v, dict) else None
                                                            for v in e.get("values") or [None]]
        used = {_window_of(p) for p in paths}
        if used != {w}:
            out.append(f"{tid}: window {w!r} is not the window its outcome is read over {sorted(map(str, used))}")
        lost = sorted(str(a) for a in arms if w not in _setting_windows(pack, a))
        if lost:
            out.append(f"{tid}: window {w!r} is not a horizon window of arms {lost}")
    return out


def _box_ok(b) -> bool:
    return isinstance(b, list) and len(b) == 2 and all(_num(v) for v in b) and b[0] < b[1]


def declaration_problems(decl, pack: dict) -> list:
    """Every reason the declaration cannot drive a fit on `pack` (fail closed: nothing has a default but `monitor`,
    `confounded`, `edge_checks` and `must_meet`, which default to none)."""
    if not isinstance(decl, dict):
        return ["effect fit declaration: a mapping"]
    out = [] if decl.get("schema") == SCHEMA else [f"schema: {SCHEMA!r}"]
    if set(decl) - KEYS:
        out.append(f"unknown keys {sorted(set(decl) - KEYS)}")
    byid = {b.get("id"): b for b in pack.get("benchmarks") or [] if isinstance(b, dict)}
    T = decl.get("targets")
    if not (isinstance(T, dict) and T):
        out.append("targets: a mapping {benchmark id: estimand fields}, at least one")
        T = {}
    for tid, est in T.items():
        out += estimand_problems(tid, est, byid.get(tid), pack)
    mon = decl.get("monitor") or []
    if not isinstance(mon, list) or any(m not in byid or m in T for m in mon):
        out.append("monitor: a list of the pack's benchmark ids that are not targets")
    P = decl.get("parameters")
    if not (isinstance(P, dict) and P):
        out.append("parameters: a mapping {name: {box: [lo, hi], source}}, at least one")
        P = {}
    for k, v in P.items():
        if not (isinstance(v, dict) and _box_ok(v.get("box")) and _txt(v.get("source"))):
            out.append(f"parameters.{k}: {{box: [lo, hi] with lo < hi, source: <text>}}")
    for pr in decl.get("confounded") or []:
        if not (isinstance(pr, list) and len(pr) == 2 and all(x in P for x in pr)):
            out.append(f"confounded: {pr!r} is not a pair of fitted parameters")
    waves = (decl.get("design") or {}).get("waves") if isinstance(decl.get("design"), dict) else None
    if not (isinstance(waves, list) and waves):
        out.append("design.waves: a list of {n, seed, box (optional)}")
        waves = []
    for i, w in enumerate(waves):
        if not (isinstance(w, dict) and _int(w.get("n")) and w["n"] >= 1 and _int(w.get("seed"))):
            out.append(f"design.waves[{i}]: {{n >= 1, seed: <int>}}")
            continue
        for k, b in (w.get("box") or {}).items():
            pb = (P.get(k) or {}).get("box")
            if k not in P or not _box_ok(b) or (_box_ok(pb) and (b[0] < pb[0] or b[1] > pb[1])):
                out.append(f"design.waves[{i}].box.{k}: inside the parameter's box")
    d = len(P)
    n_coef = 1 + d + d * (d + 1) // 2
    n_pts = sum(int(w.get("n", 0)) for w in waves if isinstance(w, dict) and _int(w.get("n"))) + 1
    if P and n_pts < n_coef + 2:
        out.append(f"design.waves: {n_pts} points for a quadratic surrogate of {n_coef} coefficients in {d} "
                   f"dimensions (at least {n_coef + 2}: its leave-one-out error needs spare points)")
    hm = decl.get("history_matching")
    if not (isinstance(hm, dict) and _num(hm.get("cutoff")) and _int(hm.get("candidate_seed"))):
        out.append("history_matching: {cutoff: <number>, candidate_seed: <int>}")
    a = decl.get("acceptance")
    if not isinstance(a, dict) or any(k not in a for k in ACCEPTANCE_KEYS):
        out.append(f"acceptance: declares {list(ACCEPTANCE_KEYS)} (no module default)")
    else:
        if a["rule"] not in RULES:
            out.append(f"acceptance.rule: one of {sorted(RULES)}")
        if not (_num(a["level"]) and 0 < a["level"] < 1):
            out.append("acceptance.level: in (0, 1)")
        if not (all(_int(a[k]) and a[k] >= 1 for k in ("verify_n", "sample_n")) and _int(a["pick_seed"])
                and a["sample_n"] >= a["verify_n"]):
            out.append("acceptance: verify_n and sample_n integers, 1 <= verify_n <= sample_n; pick_seed an integer")
        if not (_num(a["control_tolerance"]) and a["control_tolerance"] > 0):
            out.append("acceptance.control_tolerance: a positive number")
        if any(m not in T for m in a.get("must_meet") or []):
            out.append("acceptance.must_meet: targets only")
    for c in decl.get("edge_checks") or []:
        if not (isinstance(c, dict) and c.get("param") in P and c.get("side") in ("lo", "hi") and _num(c.get("within"))
                and 0 < c["within"] < 1 and _txt(c.get("basis"))
                and all(t in T or t in mon for t in c.get("targets") or [])):
            out.append(f"edge_checks: {c!r}: {{param, side: lo|hi, within in (0, 1), targets, basis}}")
    seeds = pack.get("seeds")
    if not (isinstance(seeds, list) and len(seeds) >= 2 and all(_int(s) for s in seeds) and len(set(seeds)) == len(seeds)):
        out.append("pack seeds: at least two distinct integers (the acceptance rule needs a seed interval)")
    return out


def coverage_problems(decl: dict, coverage: dict | None, version: str) -> list:
    """The engine's benchmark_coverage.yaml must name, for every training target, effect parameters among the fitted
    ones (for this engine version), and every fitted parameter must be named by some target (else no target can
    identify it)."""
    from toolkit.engines.benchmark_coverage import effect_parameters
    ents = (coverage or {}).get("benchmarks") or {}
    out, named = [], set()
    for t in decl.get("targets") or {}:
        hit = set(effect_parameters(ents.get(t), version)) & set(decl.get("parameters") or {})
        if not hit:
            out.append(f"{t}: benchmark_coverage names no fitted effect parameter for it (engine version {version}); "
                       "required for a training target")
        named |= hit
    return out + [f"parameter {k}: no training target's effect_parameters name it, so no target can identify it"
                  for k in decl.get("parameters") or {} if k not in named]


def pack_files(decl_path: str) -> tuple:
    """(declaration, pack, coverage, engine version) for a pack's effect_fit.yaml: pack.yaml beside it, and the
    engine's benchmark_coverage.yaml and card.yaml one level up."""
    here = os.path.dirname(os.path.abspath(decl_path))
    eng = os.path.dirname(here)
    rd = lambda p: yaml.safe_load(open(p)) if os.path.exists(p) else None  # noqa: E731
    card = rd(os.path.join(eng, "card.yaml")) or {}
    return (rd(decl_path), rd(os.path.join(here, "pack.yaml")) or {}, rd(os.path.join(eng, "benchmark_coverage.yaml")),
            str(card.get("version", "")))


def target_digests(decl: dict, pack: dict) -> dict:
    """{id: sha256 of the pack's benchmark declaration and the fit's estimand fields} for targets and monitors."""
    byid = {b["id"]: b for b in pack["benchmarks"]}
    ids = list(decl["targets"]) + list(decl.get("monitor") or [])
    from toolkit.engines.benchmark_sources import band_digest     # its arms, settings, horizon, seeds and engine too
    return {t: _sha({"benchmark": band_digest(byid[t], pack), "estimand": decl["targets"].get(t)}) for t in ids}


# ------------------------------------------------------------------------------------------- statistics
def z_of(level: float) -> float:
    return statistics.NormalDist().inv_cdf(0.5 + float(level) / 2)


def seed_interval(values, n_seeds: int, level: float) -> tuple | None:
    """(lo, hi) of the seed geometric mean's `level` interval; None unless exactly `n_seeds` (>= 2) finite positive
    values."""
    if not isinstance(values, (list, tuple)):
        return None
    try:
        v = [float(x) for x in values]
    except (TypeError, ValueError):
        return None
    if n_seeds < 2 or len(v) != n_seeds or not all(math.isfinite(x) and x > 0 for x in v):
        return None
    lg = np.log(v)
    m, h = float(np.mean(lg)), z_of(level) * float(np.std(lg, ddof=1)) / math.sqrt(len(v))
    return math.exp(m - h), math.exp(m + h)


def ci_in_band(values, band, n_seeds: int, level: float) -> bool:
    iv = seed_interval(values, n_seeds, level)
    return iv is not None and iv[0] <= float(band[1]) and iv[1] >= float(band[0])


def seed_values_problem(v, n_seeds: int) -> str | None:
    """Why `v` is not exactly one finite, positive number per declared seed (booleans are not numbers)."""
    if not isinstance(v, list) or len(v) != n_seeds:
        return f"{len(v) if isinstance(v, list) else 'no'} values for {n_seeds} declared seeds"
    if not all(_num(x) and x > 0 for x in v):
        return f"a value that is not a finite positive number ({v})"
    return None


def judge(row: dict, bands: dict, seeds: list, acc: dict, calib: dict | None = None) -> dict:
    """One engine-evaluated point under the declared acceptance rule (targets in `bands` only). A control arm is
    judged against the pack's calibration target `calib[setting]`, never a target the evaluator reports."""
    per = row.get("per_seed") or {}
    by = {}
    for t, b in bands.items():
        iv = seed_interval(per.get(t), len(seeds), acc["level"])
        by[t] = {"interval": [round(iv[0], 6), round(iv[1], 6)] if iv else None,
                 "in_band": ci_in_band(per.get(t), b, len(seeds), acc["level"])}
    off = []
    for s, c in sorted((row.get("controls") or {}).items()):
        a, tg = c.get("achieved"), (calib or {}).get(s)
        if not (_num(a) and _num(tg) and tg != 0 and abs(a / tg - 1) <= acc["control_tolerance"]) or c.get("at_grid_edge"):
            off.append(s)
    return {"point": row["theta"], "by_target": by, "controls_off": off,
            "met": all(v["in_band"] for v in by.values()) and not off}


# --------------------------------------------------------------------------------------- design and rows
def _names_box(decl: dict) -> tuple:
    names = list(decl["parameters"])
    return names, np.array([decl["parameters"][k]["box"] for k in names], float)


def design_points(decl: dict) -> tuple:
    """(design points, support boxes): each wave's Latin hypercube in its box (default the parameter box), the first
    wave with the box centre."""
    names, box = _names_box(decl)
    pts, support = [], []
    for i, w in enumerate(decl["design"]["waves"]):
        wb = np.array([(w.get("box") or {}).get(k, box[j].tolist()) for j, k in enumerate(names)], float)
        U = BM.lhs(int(w["n"]), len(names), int(w["seed"]))
        if i == 0:
            U = np.vstack([U, np.full((1, len(names)), 0.5)])
        th = np.clip(wb[:, 0] + U * (wb[:, 1] - wb[:, 0]), wb[:, 0], wb[:, 1])
        pts += [{k: float(x[j]) for j, k in enumerate(names)} for x in th]
        support.append({k: [float(wb[j, 0]), float(wb[j, 1])] for j, k in enumerate(names)})
    return pts, support


class Cache:
    """Engine rows {scope + point: {per_seed, controls}}; with `path`, appended via lib.safe_io under trusted `root`."""

    def __init__(self, path: str | None = None, digest: str | None = None, root: str | None = None):
        self.path, self.root, self.rows = path, root, {}
        if path and os.path.exists(path):
            data = open(path, "rb").read()
            if digest is not None and hashlib.sha256(data).hexdigest() != digest:
                raise ValueError(f"cache {path}: sha256 {hashlib.sha256(data).hexdigest()} is not {digest}")
            for ln in data.decode().splitlines():
                r = json.loads(ln)
                self.rows[r["key"]] = r
        elif digest is not None:
            raise ValueError(f"cache {path}: missing, expected sha256 {digest}")

    @staticmethod
    def key(scope: str, theta: dict) -> str:
        return _sha({"scope": scope, "theta": {k: repr(float(v)) for k, v in sorted(theta.items())}})   # exact

    def get(self, scope: str, theta: dict):
        return self.rows.get(self.key(scope, theta))

    def put(self, scope: str, theta: dict, res: dict) -> None:
        r = {"key": self.key(scope, theta), "theta": theta, "per_seed": res["per_seed"],
             "controls": res.get("controls") or {}}
        self.rows[r["key"]] = r
        if self.path:
            from toolkit.analysis.artifacts import _safe_io
            _safe_io().append_text(self.path, json.dumps(r, sort_keys=True) + "\n", self.root)

    def digest(self) -> str:
        return _sha(sorted(self.rows))


def evaluate(points: list, evaluator, ids: list, cache: Cache, scope: str, n_seeds: int) -> tuple:
    """(rows, engine calls): each point's per-seed values for every id, from the cache or `evaluator(theta, ids)`,
    which returns {per_seed: {id: [value per seed]}, controls: {setting: {target, achieved, at_grid_edge}}}."""
    rows, calls = [], 0
    for th in points:
        r = cache.get(scope, th)
        if r is None:
            r = evaluator(dict(th), list(ids))
            calls += 1
        bad = {t: seed_values_problem((r.get("per_seed") or {}).get(t), n_seeds) for t in ids}
        bad = {t: w for t, w in bad.items() if w}
        if bad:      # fail closed, cached or not: a row is one finite positive value per declared seed
            raise ValueError(f"engine rows at {th}: {bad}")
        if cache.get(scope, th) is None:
            cache.put(scope, th, r)
        rows.append({"theta": dict(th), "per_seed": r["per_seed"], "controls": r.get("controls") or {}})
    return rows, calls


def _geo_log(v) -> float:
    return float(np.mean(np.log([float(x) for x in v])))


def candidates(decl: dict, support: list, rows: list) -> np.ndarray:
    """Dense candidates (parameter units) inside each wave's box, each wave covered on its own, plus every
    engine-evaluated point; clipped to their wave's box (no candidate leaves the support) and distinct."""
    names, _box = _names_box(decl)
    seed = int(decl["history_matching"]["candidate_seed"])
    out = [np.array([[r["theta"][k] for k in names] for r in rows], float)]
    for i, bx in enumerate(support):       # each wave its own draw, so equal boxes do not repeat one draw
        lo, hi = np.array([bx[k][0] for k in names]), np.array([bx[k][1] for k in names])
        out.append(np.clip(lo + BM._candidates(len(names), seed + i) * (hi - lo), lo, hi))
    return np.unique(np.vstack(out), axis=0)


# ------------------------------------------------------------------------------------- history matching
def history_match(rows: list, decl: dict, pack: dict, support: list) -> dict:
    names, box = _names_box(decl)
    byid = {b["id"]: b for b in pack["benchmarks"]}
    targets, cut, hm = list(decl["targets"]), float(decl["history_matching"]["cutoff"]), decl["history_matching"]
    norm = lambda th: (th - box[:, 0]) / (box[:, 1] - box[:, 0])  # noqa: E731
    X = norm(np.array([[r["theta"][k] for k in names] for r in rows], float))
    cth = candidates(decl, support, rows)
    Fc = BM._quad_features(norm(cth))
    imp, err, surr = {}, {}, {}
    for t in targets:
        coef, loo = BM._quad_fit(X, np.array([_geo_log(r["per_seed"][t]) for r in rows]))
        lv = Fc @ coef
        lo, hi = math.log(byid[t]["band"][0]), math.log(byid[t]["band"][1])
        imp[t] = np.maximum(lo - lv, lv - hi) / max(loo, 1e-9)
        err[t] = ((lv - math.log(byid[t]["target"])) / ((hi - lo) / 2)) ** 2
        surr[t] = {"loo_rmse_log": round(loo, 6), "n_points": len(rows)}
    feasible = [t for t in targets if (imp[t] <= cut).any()]
    ok = np.ones(len(cth), bool)
    for t in feasible:
        ok &= imp[t] <= cut
    pairs = [[a, b] for a, b in itertools.combinations(feasible, 2) if not ((imp[a] <= cut) & (imp[b] <= cut)).any()]
    n_out = sum(((imp[t] > cut).astype(int) for t in feasible), np.zeros(len(cth), int))
    L = sum((err[t] for t in feasible), np.zeros(len(cth)))
    # lexicographic: inside the region when it is not empty, else fewest targets past the cutoff; then least error
    j = int(np.flatnonzero(ok)[np.argmin(L[ok])]) if ok.any() else int(np.lexsort((L, n_out))[0])
    to = lambda i: {k: float(cth[i, m]) for m, k in enumerate(names)}  # noqa: E731
    acc = cth[ok]
    hull = ({k: [float(acc[:, m].min()), float(acc[:, m].max())] for m, k in enumerate(names)} if len(acc) else None)
    sup_lo = {k: min(b[k][0] for b in support) for k in names}
    sup_hi = {k: max(b[k][1] for b in support) for k in names}
    tol = {k: 1e-3 * (sup_hi[k] - sup_lo[k]) for k in names}
    edge = sorted(k for k in names if hull and (hull[k][0] <= sup_lo[k] + tol[k] or hull[k][1] >= sup_hi[k] - tol[k]))
    idx = np.flatnonzero(ok)
    n_s = int(decl["acceptance"]["sample_n"])
    if len(idx) > n_s:
        idx = np.sort(np.random.default_rng(int(hm["candidate_seed"])).choice(idx, n_s, replace=False))
    return {"chosen": to(j), "chosen_in_region": bool(ok[j]), "n_out_at_chosen": int(n_out[j]),
            "implausibility_at_chosen": {t: round(float(imp[t][j]), 4) for t in targets},
            "least_implausibility": {t: round(float(imp[t].min()), 4) for t in targets},
            "feasible": feasible, "infeasible": [t for t in targets if t not in feasible], "conflicting_pairs": pairs,
            "surrogate": surr,
            "region": {"n_candidates": len(cth), "n_accepted": int(ok.sum()),
                       "share_of_support": round(float(ok.mean()), 6), "hull": hull,
                       "at_support_edge": edge, "support": support,
                       "correlation": {"names": names, "matrix": np.corrcoef(acc.T).round(3).tolist()
                                       if len(acc) > 2 else None},
                       "sample": [to(i) for i in idx]}}


# ----------------------------------------------------------------------------------------------- the fit
def edge_diagnostics(decl: dict, point: dict, row: dict, bands: dict) -> list:
    """Each edge check at the chosen point; a target is missed when the chosen row's seed geometric mean (its point
    estimate, not the acceptance interval) is outside its band."""
    out = []
    for c in decl.get("edge_checks") or []:
        lo, hi = decl["parameters"][c["param"]]["box"]
        v = float(point[c["param"]])
        near = v >= hi - c["within"] * (hi - lo) if c["side"] == "hi" else v <= lo + c["within"] * (hi - lo)
        gm = {t: math.exp(_geo_log(row["per_seed"][t])) for t in c.get("targets") or []}
        missed = [t for t in gm if not bands[t][0] <= gm[t] <= bands[t][1]]
        out.append({"param": c["param"], "value": v, "side": c["side"], "bound": hi if c["side"] == "hi" else lo,
                    "near_bound": bool(near), "targets_missed_at_chosen": missed if near else [], "basis": c["basis"]})
    return out


def fit(decl: dict, pack: dict, evaluator, engine_identity: dict | None = None, cache: Cache | None = None,
        cost: dict | None = None) -> dict:
    """The effect fit (module docstring). Raises ValueError on a declaration problem, a missing engine identity (the
    cache scope needs it) or a malformed engine row; returns the record."""
    bad = declaration_problems(decl, pack)
    if not (isinstance(engine_identity, dict) and engine_identity and all(
            (_txt(v) or _num(v)) for v in engine_identity.values())):
        bad.append("engine_identity: required (it scopes the engine-row cache)")
    if bad:
        raise ValueError("effect fit declaration refused: " + "; ".join(bad))
    acc, seeds = decl["acceptance"], list(pack["seeds"])
    byid = {b["id"]: b for b in pack["benchmarks"]}
    targets, monitor = list(decl["targets"]), list(decl.get("monitor") or [])
    ids = targets + monitor
    tdig = target_digests(decl, pack)
    # an evaluator's live state (runner seeds, pack, base and stored fitted values) joins the cache scope, read
    # before any cached row is used; its seeds and pack must be this fit's
    ev_scope = evaluator.scope() if callable(getattr(evaluator, "scope", None)) else None
    if isinstance(ev_scope, dict) and (list(ev_scope.get("seeds", seeds)) != seeds
                                       or ev_scope.get("pack", _sha(pack)) != _sha(pack)):
        raise ValueError("the evaluator runs other seeds or another pack than this fit's")
    scope = _sha({"targets": tdig, "engine": engine_identity, "seeds": seeds, "evaluator": ev_scope})
    cache = cache if cache is not None else Cache()
    pts, support = design_points(decl)
    rows, calls = evaluate(pts, evaluator, ids, cache, scope, len(seeds))
    hm = history_match(rows, decl, pack, support)
    sample = hm["region"]["sample"]
    k = min(int(acc["verify_n"]), len(sample))
    pick = sorted(np.random.default_rng(int(acc["pick_seed"])).choice(len(sample), k, replace=False).tolist()) if k else []
    vrows, vcalls = evaluate([sample[i] for i in pick] + [hm["chosen"]], evaluator, ids, cache, scope, len(seeds))
    bands = {t: byid[t]["band"] for t in hm["feasible"]}
    calib = {s: (v.get("calibrate") or {}).get("target") for s, v in (pack.get("settings") or {}).items()}
    checked = [judge(r, bands, seeds, acc, calib) for r in vrows[:-1]]
    chosen = judge(vrows[-1], bands, seeds, acc, calib)
    for j, r in zip(checked + [chosen], vrows):
        j["monitor"] = {m: judge(r, {m: byid[m]["band"]}, seeds, acc)["by_target"][m] for m in monitor}
    must = set(acc.get("must_meet") or [])
    failures = [{"kind": "conflict_must_meet", "target": t, "reason": "a must_meet target no candidate can meet"}
                for t in hm["infeasible"] if t in must]
    failures += [{"kind": "conflicting_pair", "targets": p, "reason": "no candidate meets both"}
                 for p in hm["conflicting_pairs"]]
    for j in checked + [chosen]:
        bad_t = [t for t, v in j["by_target"].items() if not v["in_band"]]
        if bad_t or j["controls_off"]:
            failures.append({"kind": "verification", "point": j["point"], "chosen": j is chosen, "targets": bad_t,
                             "controls_off": j["controls_off"]})
    clamped = sorted({s for r in rows for s, c in (r.get("controls") or {}).items()
                      if c.get("at_grid_edge") is not False})       # a missing clamp result is not a pass
    if clamped:
        failures.append({"kind": "calibration_grid_edge", "settings": clamped,
                         "reason": "a design point's calibration clamps at its grid edge"})
    if not hm["feasible"]:
        failures.append({"kind": "no_meetable_target", "reason": "no target can be met anywhere in the support"})
    if not checked:
        failures.append({"kind": "no_region", "reason": "no candidate is not implausible on every meetable target"})
    elif len(checked) < int(acc["verify_n"]):
        failures.append({"kind": "too_few_verified", "reason": f"{len(checked)} verified points for the declared "
                         f"{acc['verify_n']}: the region holds fewer candidates"})
    if not hm["chosen_in_region"]:
        failures.append({"kind": "chosen_outside_region", "point": hm["chosen"]})
    region = dict(hm["region"])
    region["sample_status"] = ("unverified" if not checked else "verified" if not failures else "invalid")
    if region["sample_status"] != "verified":
        region["sample_unverified"], region["sample"] = region["sample"], []
    conflicts = [{"target": t, "least_implausibility": hm["least_implausibility"][t],
                  "reason": f"no candidate in the design support has implausibility <= {decl['history_matching']['cutoff']}"}
                 for t in hm["infeasible"]]
    status = "rejected" if failures else "accepted_with_conflicts" if conflicts else "accepted"
    sm = region.get("sample") or region.get("sample_unverified") or []
    conf = []
    for a, b in decl.get("confounded") or []:
        r = float(np.corrcoef([p[a] for p in sm], [p[b] for p in sm])[0, 1]) if len(sm) > 2 else None
        conf.append({"pair": [a, b], "correlation_in_region": None if r is None or not math.isfinite(r) else round(r, 4)})
    return {"schema": SCHEMA, "producer": PRODUCER, "status": status, "point": hm["chosen"],
            "declaration_digest": _sha(decl), "target_digests": tdig, "engine_identity": engine_identity,
            "seeds": seeds, "parameters": decl["parameters"],
            "method": {"history_matching": dict(decl["history_matching"]), "choice": CHOICE,
                       "acceptance": dict(acc, text=RULES[acc["rule"]])},
            "targets": {t: {"band": byid[t]["band"], "target": byid[t]["target"], "estimand": decl["targets"][t],
                            "implausibility_at_chosen": hm["implausibility_at_chosen"][t],
                            "verified_at_chosen": chosen["by_target"].get(t), "meetable": t in hm["feasible"]}
                        for t in targets},
            "monitor": chosen["monitor"], "joint_region": region, "surrogate": hm["surrogate"],
            "verification": {"points": checked, "chosen": chosen}, "failures": failures, "conflicts": conflicts,
            "confounded": conf,
            "edge": edge_diagnostics(decl, hm["chosen"], vrows[-1], {t: byid[t]["band"] for t in ids}),
            "design": {"n_points": len(rows), "support": support},
            "engine_rows": {"calls": calls + vcalls, "cache_digest": cache.digest()}, "cost": cost}


def write_record(path: str, rec: dict, root: str) -> str:
    """Write the record through lib.safe_io under the trusted `root`; returns its sha256 (what the spec references)."""
    from toolkit.analysis.artifacts import _safe_io
    data = yaml.safe_dump(rec, sort_keys=False, allow_unicode=True).encode()
    _safe_io().publish(path, data, root)
    return hashlib.sha256(data).hexdigest()


def read_record(path: str, digest: str) -> dict:
    """The record at `path`, refused (ValueError) unless its bytes hash to `digest` and it is this core's record."""
    data = open(path, "rb").read()
    if hashlib.sha256(data).hexdigest() != digest:
        raise ValueError(f"effect fit record {path}: sha256 {hashlib.sha256(data).hexdigest()} is not {digest}")
    rec = yaml.safe_load(data)
    if not isinstance(rec, dict) or rec.get("schema") != SCHEMA or rec.get("producer") != PRODUCER:
        raise ValueError(f"effect fit record {path}: not a {SCHEMA} record of {PRODUCER}")
    return rec


def runner_evaluator(runner):
    """An evaluator over a benchmark.Runner in fit mode: each point recalibrates the target arms' settings (their
    `calibrate` rule), reads every id's per-seed values and each calibrated setting's control arm (the arm of that
    setting whose package is the rule's arm_package) against its target."""
    pack = runner.pack
    byid = {b["id"]: b for b in pack["benchmarks"]}
    state = lambda: {"seeds": list(runner.seeds), "pack": _sha(runner.pack), "fitted": runner.fitted_for({}),  # noqa
                     "base_params": dict(getattr(runner, "base_params", {}) or {})}
    built = state()
    runner.cache = {}                       # no arm row computed before this adapter (another state) is reused

    def ev(theta: dict, ids: list) -> dict:
        if state() != built:
            raise ValueError("the runner's seeds, pack, base or stored fitted values changed since the evaluator "
                             "was built")
        full = runner.fitted_for(theta)     # the values every run of this point uses, stored fitted values included
        arms = sorted({a for t in ids for a in BM._arms_of(byid[t]["estimand"])})
        cal = sorted({pack["arms"][a]["setting"] for a in arms} & {s for s, v in pack["settings"].items()
                                                                   if v.get("calibrate")})
        runner.calibrate_settings(full, cal)
        key = tuple(sorted(full.items()))   # arm rows of this point run under an earlier calibration are not reused
        runner.cache = {k: v for k, v in runner.cache.items()
                        if k[1] != key and not (isinstance(k[1], tuple) and k[1] and k[1][0] == key)}
        prev, runner.full_precision = getattr(runner, "full_precision", False), True
        try:
            per = {t: runner.evaluate(byid[t], theta).get("per_seed_full") for t in ids}
        finally:
            runner.full_precision = prev
        if any(v is None for v in per.values()):
            raise ValueError("the runner reports no unrounded per-seed values")
        controls = {}
        for s in cal:
            c = pack["settings"][s]["calibrate"]
            ctrl = sorted(a for a, v in pack["arms"].items()
                          if v["setting"] == s and (v.get("package") or {}) == (c.get("arm_package") or {}))
            got = None
            if ctrl:
                res = runner.arm_results([ctrl[0]], full)[ctrl[0]]
                got = float(np.mean([BM.get(r, c["value_path"]) for r in res]))
            cv, g = runner.calibrated.get(s, {}), [float(x) for x in c.get("grid") or []]
            v = cv.get("value")      # the calibrated value at or past a grid end is a clamp, whatever the flag says
            edge = bool(cv.get("at_grid_edge")) or not _num(v) or (bool(g) and not min(g) < v < max(g))
            controls[s] = {"target": c["target"], "achieved": got, "at_grid_edge": edge}
        return {"per_seed": {t: list(v) for t, v in per.items()}, "controls": controls}
    ev.scope = state
    return ev


# ------------------------------------------------------------------------------------------- self-test
def _toy_pack(extra: list | None = None) -> dict:
    """Two arms of one site; targets are ratios at two follow-up windows (a waning effect)."""
    est = lambda w: {"type": "ratio", "num": "trt", "den": "ctl", "value": f"windows.{w}.prev"}  # noqa: E731
    return {"seeds": [1, 2, 3, 4], "horizon": {"windows": {"m6": [0.0, 0.5], "m18": [1.0, 1.5]}},
            "settings": {"site": {"calibrate": {"target": 0.4}}}, "arms": {"trt": {"setting": "site", "package": {"x": 1}},
                                               "ctl": {"setting": "site", "package": {}}},
            "benchmarks": [{"id": "T6", "target": 0.5526, "band": [0.52, 0.59], "estimand": est("m6")},
                           {"id": "T18", "target": 0.7224, "band": [0.69, 0.76], "estimand": est("m18")}]
            + (extra or [])}


def _toy_decl(**kw) -> dict:
    fields = lambda w: {"contrast": {"treatment": "trt", "comparator": "ctl"}, "outcome": "prevalence",  # noqa: E731
                        "scale": "ratio", "window": w, "population": "all ages", "measurement": "simulated"}
    d = {"schema": SCHEMA, "targets": {"T6": fields("m6"), "T18": fields("m18")},
         "parameters": {"eff": {"box": [0.4, 1.2], "source": "toy"}, "wane": {"box": [0.2, 1.2], "source": "toy"}},
         "design": {"waves": [{"n": 30, "seed": 1}]}, "history_matching": {"cutoff": -1.0, "candidate_seed": 2},
         "acceptance": {"rule": "seed_interval_overlaps_band", "level": 0.95, "verify_n": 6, "pick_seed": 3,
                        "sample_n": 200, "control_tolerance": 0.1}}
    d.update(kw)
    return d


def _toy_engine(calls: list):
    """Ratio exp(-eff exp(-wane t)) at t = 0.5 (m6) and 1.5 (m18) years, with a small seeded noise per seed."""
    def ev(theta, ids):
        calls.append(theta)
        out = {}
        for t in ids:
            tt = {"T6": 0.5, "T18": 1.5, "TX": 0.5}[t]
            v = math.exp(-theta["eff"] * math.exp(-theta["wane"] * tt))
            out[t] = [v * math.exp(0.004 * math.sin(7 * s + 13 * theta["eff"])) for s in (1, 2, 3, 4)]
        return {"per_seed": out, "controls": {"site": {"target": 0.4, "achieved": 0.41, "at_grid_edge": False}}}
    return ev


def _self_test() -> int:
    import copy
    import tempfile
    fails = []

    def ok(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    def refused(fn) -> bool:
        try:
            fn()
        except ValueError:
            return True
        return False

    pack, decl = _toy_pack(), _toy_decl()
    ok(declaration_problems(decl, pack) == [], f"EF_DECL_OK: the toy declaration is valid {declaration_problems(decl, pack)}")
    calls = []
    rec = fit(decl, pack, _toy_engine(calls), engine_identity={"name": "toy"})
    h = rec["joint_region"]["hull"] or {}
    truth = {"eff": 0.8, "wane": 0.6}
    ok(rec["status"] == "accepted" and rec["joint_region"]["sample_status"] == "verified",
       f"EF_TOY_RECOVERS: status {rec['status']}, sample {rec['joint_region']['sample_status']} {rec['failures']}")
    ok(all(h.get(k, [1, 0])[0] <= v <= h.get(k, [0, -1])[1] for k, v in truth.items()),
       f"EF_TOY_RECOVERS: the true effect {truth} is inside the accepted region {h}")
    ok(abs(rec["point"]["eff"] - 0.8) < 0.1 and abs(rec["point"]["wane"] - 0.6) < 0.15,
       f"EF_TOY_RECOVERS: chosen point {rec['point']} near the truth")
    ok(all(v["verified_at_chosen"]["in_band"] for v in rec["targets"].values())
       and set(rec["target_digests"]) == {"T6", "T18"} and rec["engine_identity"] == {"name": "toy"},
       "EF_RECORD: per-target verification at the chosen point, target digests and engine identity recorded")

    # EF_ESTIMAND_REQUIRED: each missing field is refused, and a field that does not match the pack is refused
    for f in ESTIMAND_FIELDS:
        d = copy.deepcopy(decl)
        del d["targets"]["T6"][f]
        ok(any("estimand without" in p and f in p for p in declaration_problems(d, pack)),
           f"EF_ESTIMAND_REQUIRED: a target without {f} is refused")
    for f, v, word in (("window", "m18", "window"), ("scale", "odds_ratio", "scale"),
                       ("contrast", {"treatment": "ctl", "comparator": "trt"}, "contrast")):
        d = copy.deepcopy(decl)
        d["targets"]["T6"][f] = v
        ok(any(word in p for p in declaration_problems(d, pack)),
           f"EF_ESTIMAND_MATCH: a {f} that is not the pack's estimand is refused")
    d = copy.deepcopy(decl)
    del d["targets"]["T18"]["window"]
    ok(refused(lambda: fit(d, pack, _toy_engine([]), {"name": "toy"})), "EF_ESTIMAND_REQUIRED: fit refuses no window")
    for k in ACCEPTANCE_KEYS:
        d = copy.deepcopy(decl)
        del d["acceptance"][k]
        ok(any("acceptance" in p for p in declaration_problems(d, pack)),
           f"EF_ACCEPTANCE_DECLARED: acceptance without {k} is refused (no module default)")
    d = copy.deepcopy(decl)
    del d["history_matching"]["cutoff"]
    ok(any("history_matching" in p for p in declaration_problems(d, pack)), "EF_ACCEPTANCE_DECLARED: no cutoff refused")

    # EF_SUPPORT_ONLY: a wave box narrower than the parameter box keeps every candidate inside it
    d = _toy_decl(design={"waves": [{"n": 30, "seed": 1, "box": {"eff": [0.6, 1.0], "wane": [0.3, 0.9]}}]})
    r2 = fit(d, pack, _toy_engine([]), {"name": "toy"})
    pts = (r2["joint_region"]["sample"] or r2["joint_region"]["sample_unverified"]) + [r2["point"]]
    ok(pts and all(0.6 - 1e-9 <= p["eff"] <= 1.0 + 1e-9 and 0.3 - 1e-9 <= p["wane"] <= 0.9 + 1e-9 for p in pts),
       f"EF_SUPPORT_ONLY: no candidate outside the design support ({len(pts)} points)")

    # EF_NOISE_RULE: the seed interval decides, not the point estimate
    band, lv = [0.5, 0.6], math.log(0.6)
    near = [math.exp(lv + 0.03 + e) for e in (-0.12, -0.04, 0.04, 0.12)]       # mean 1 SE-ish above the band
    far = [math.exp(lv + 0.30 + e) for e in (-0.01, 0.0, 0.0, 0.01)]
    ok(math.exp(np.mean(np.log(near))) > 0.6 and ci_in_band(near, band, 4, 0.95),
       "EF_NOISE_RULE: a point estimate just outside the band whose seed interval overlaps it is in band")
    ok(not ci_in_band(far, band, 4, 0.95) and not ci_in_band(near[:3], band, 4, 0.95)
       and not ci_in_band(near[:3] + [-1.0], band, 4, 0.95),
       "EF_NOISE_RULE: an interval clear of the band, a missing seed or a non-positive value fails")
    j = judge({"theta": {}, "per_seed": {"T": near}, "controls": {"s": {"target": 0.4, "achieved": 0.5}}},
              {"T": band}, [1, 2, 3, 4], decl["acceptance"])
    ok(j["controls_off"] == ["s"] and not j["met"], "EF_CONTROLS: a control arm 25% off its target fails the point")

    # EF_CONFLICT: an unreachable target is a declared conflict, the fit continues; must_meet makes it a rejection
    tx = {"id": "TX", "target": 0.07, "band": [0.05, 0.1], "estimand": {"type": "ratio", "num": "trt", "den": "ctl",
                                                                          "value": "windows.m6.prev"}}
    pk = _toy_pack([tx])
    d = copy.deepcopy(decl)
    d["targets"]["TX"] = dict(decl["targets"]["T6"])
    r3 = fit(d, pk, _toy_engine([]), {"name": "toy"})
    ok(r3["status"] == "accepted_with_conflicts" and [c["target"] for c in r3["conflicts"]] == ["TX"]
       and r3["joint_region"]["sample_status"] == "verified",
       f"EF_CONFLICT: an unmeetable target is declared, the rest verified ({r3['status']}, {r3['conflicts']})")
    d["acceptance"]["must_meet"] = ["TX"]
    r4 = fit(d, pk, _toy_engine([]), {"name": "toy"})
    ok(r4["status"] == "rejected" and r4["failures"][0]["kind"] == "conflict_must_meet"
       and r4["joint_region"]["sample"] == [] and r4["joint_region"]["sample_status"] == "invalid",
       "EF_CONFLICT: a must_meet target that cannot be met rejects the fit and withdraws the sample (Codex r1)")
    d = copy.deepcopy(decl)
    d["monitor"] = ["TX"]
    r5 = fit(d, pk, _toy_engine([]), {"name": "toy"})
    ok(r5["status"] == "accepted" and r5["monitor"]["TX"]["in_band"] is False and "TX" not in r5["targets"],
       "EF_MONITOR: a monitored benchmark is reported at the chosen point and never chooses")

    # EF_CACHE: a second fit reuses every engine row; a changed estimand or engine reuses none
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "rows.jsonl")
        c1, n1 = [], []
        fit(decl, pack, _toy_engine(c1), engine_identity={"name": "toy"}, cache=Cache(p, root=td))
        dig = hashlib.sha256(open(p, "rb").read()).hexdigest()
        fit(decl, pack, _toy_engine(n1), engine_identity={"name": "toy"}, cache=Cache(p, digest=dig, root=td))
        ok(c1 and not n1, f"EF_CACHE: the replay from the stored rows ran no engine ({len(c1)} then {len(n1)} calls)")
        n2 = []
        fit(decl, pack, _toy_engine(n2), engine_identity={"name": "toy2"}, cache=Cache(p, root=td))
        ok(len(n2) == len(c1), "EF_CACHE: another engine identity reuses no row")
        n3, pk3 = [], _toy_pack()
        pk3["settings"]["site"]["calibrate"]["target"] = 0.8
        r6 = fit(decl, pk3, _toy_engine(n3), engine_identity={"name": "toy"}, cache=Cache(p, root=td))
        ok(len(n3) == len(c1) and r6["status"] == "rejected",
           "EF_CACHE: a changed calibration target of a target's setting reuses no row, and its control fails (Codex r1)")
        ok(refused(lambda: fit(decl, pack, _toy_engine([]), cache=Cache(p, root=td))),
           "EF_CACHE: a fit without an engine identity is refused (Codex r1)")
        ok(refused(lambda: Cache(p, digest="0" * 64)), "EF_CACHE: a cache whose digest differs is refused")
        rp = os.path.join(td, "effect_fit.yaml")
        dg = write_record(rp, rec, td)
        ok(read_record(rp, dg)["point"] == rec["point"], "EF_RECORD_DIGEST: the record reads back by its digest")
        with open(rp, "a") as f:
            f.write("# edited\n")
        ok(refused(lambda: read_record(rp, dg)), "EF_RECORD_DIGEST: an edited record is refused")

    # Codex r1: malformed engine rows, controls judged on the pack's target, declared counts, estimand ambiguity
    rows_of = lambda per: (lambda th, ids: {"per_seed": {t: per for t in ids}, "controls": {}})  # noqa: E731
    for label, per in (("one seed value", [0.55]), ("booleans", [True] * 4), ("no values", [])):
        ok(refused(lambda: fit(decl, pack, rows_of(per), {"name": "toy"})),
           f"EF_SEED_ROWS: a design row with {label} is refused")
    j = judge({"theta": {}, "per_seed": {"T": [0.55] * 4}, "controls": {"site": {"target": 0.8, "achieved": 0.8}}},
              {"T": [0.5, 0.6]}, [1, 2, 3, 4], decl["acceptance"], {"site": 0.4})
    ok(j["controls_off"] == ["site"], "EF_CONTROLS: a control is judged against the pack's target, not the evaluator's")
    d = copy.deepcopy(decl)
    d["acceptance"]["sample_n"] = 1
    ok(any("verify_n" in x for x in declaration_problems(d, pack)), "EF_VERIFY_N: sample_n below verify_n is refused")
    pk = _toy_pack()
    pk["benchmarks"][0]["estimand"]["values"] = [{"path": "windows.m18.prev", "weight": 1}]
    ok(any("ambiguous" in x for x in declaration_problems(decl, pk)), "EF_ESTIMAND_MATCH: value and values refused")
    pk = _toy_pack()
    del pk["arms"]["trt"]
    ok(any("not arms of the pack" in x for x in declaration_problems(decl, pk)),
       "EF_ESTIMAND_MATCH: a contrast arm the pack does not have is refused")
    nar = _toy_decl(design={"waves": [{"n": 30, "seed": 1}, {"n": 3, "seed": 2, "box": {"eff": [0.8, 0.8000001]}}]})
    pts_, sup_ = design_points(nar)
    cth = candidates(nar, sup_, [{"theta": x} for x in pts_])
    ok(int(((cth[:, 0] >= 0.8) & (cth[:, 0] <= 0.8000001)).sum()) > 3 and np.all((cth >= np.array([0.4, 0.2])) & (cth <= np.array([1.2, 1.2]))),
       "EF_SUPPORT_ONLY: every wave, however narrow, gets its own candidates, none outside the box (Codex r1)")
    ok(rec["verification"]["chosen"]["met"] and r2["status"] in ("accepted", "rejected")
       and not any(f["kind"] == "chosen_outside_region" for f in rec["failures"]),
       "EF_CHOICE: the chosen point is inside the region when the region is not empty")

    # EF_RUNNER: the Runner adapter recalibrates each point at its full values and reports the control arm
    class FakeRunner:
        def __init__(self):
            self.calibrated, self.log, self.seeds, self.base_params = {}, [], [1, 2, 3, 4], {"b": 1}
            self.cal_value = 1.0
            self.cache, self.full_precision = {("ctl", (("background", 2.0), ("eff", 1.0), ("wane", 1.0))): "stale"}, False
            self.pack = dict(_toy_pack(), settings={"site": {"calibrate": {"target": 0.4, "value_path": "windows.m6.prev",
                                                                            "arm_package": {}, "grid": [0.5, 2.0]}}})

        def fitted_for(self, theta):
            return dict({"background": 2.0}, **theta)

        def calibrate_settings(self, theta, names):
            self.log.append(("cal", tuple(names), theta.get("background")))
            self.calibrated = {n: {"at_grid_edge": False, "value": self.cal_value} for n in names}

        def evaluate(self, b, theta):
            assert self.full_precision and not self.cache, "unrounded values from fresh arm rows"
            return {"per_seed": [0.5, 0.51, 0.52, 0.5], "per_seed_full": [0.50004, 0.51, 0.52, 0.5]}

        def arm_results(self, arms, theta):
            return {a: [{"windows": {"m6": {"prev": 0.2 * theta["background"]}}}] * 4 for a in arms}
    fr = FakeRunner()
    ev = runner_evaluator(fr)
    out = ev({"eff": 1.0, "wane": 1.0}, ["T6", "T18"])
    ok(fr.log == [("cal", ("site",), 2.0)] and out["controls"]["site"]["achieved"] == 0.4
       and out["per_seed"]["T6"][0] == 0.50004 and not fr.full_precision and ev.scope()["base_params"] == {"b": 1},
       f"EF_RUNNER: calibration and control at the full values, stale arm rows of the point dropped, unrounded values "
       f"only during the call, base parameters in the cache scope (Codex r1 on #329) ({out['controls']})")
    fr.seeds = [5, 6, 7, 8]
    ok(refused(lambda: ev({"eff": 1.0, "wane": 1.0}, ["T6"])), "EF_RUNNER: seeds changed after construction refused")
    ok(refused(lambda: fit(decl, pack, ev, {"name": "toy"})), "EF_RUNNER: an evaluator on other seeds is refused by fit")
    fr = FakeRunner()
    fr.cal_value = 2.0
    ev = runner_evaluator(fr)
    edge_ = ev({"eff": 1.0, "wane": 1.0}, ["T6"])["controls"]["site"]["at_grid_edge"]
    fr.base_params["b"] = 2
    ok(edge_ and refused(lambda: ev({"eff": 1.0, "wane": 0.5}, ["T6"])),
       "EF_RUNNER: a calibration at its grid end is a clamp whatever the flag; changed base values refused (Codex r2)")
    fr = FakeRunner()
    ok(refused(lambda: fit(decl, _toy_pack(), runner_evaluator(fr), {"name": "toy"})),
       "EF_RUNNER: an evaluator built on another pack is refused by fit (Codex r2)")
    clamp = lambda th, ids: dict(_toy_engine([])(th, ids), controls={"site": {"achieved": 0.4, "at_grid_edge":  # noqa
                                                                               th == design_points(decl)[0][3]}})
    r7 = fit(decl, pack, clamp, {"name": "toy"})
    ok(r7["status"] == "rejected" and any(f["kind"] == "calibration_grid_edge" for f in r7["failures"]),
       "EF_CONTROLS: a calibration grid clamp at one design point rejects the fit (Codex r1 on #329)")
    de = edge_diagnostics({"parameters": {"x": {"box": [0, 1]}}, "edge_checks": [
        {"param": "x", "side": "hi", "within": 0.02, "targets": ["T"], "basis": "b"}]}, {"x": 0.99},
        {"per_seed": {"T": [0.6, 0.8] * 4}}, {"T": [0.72, 0.98]})
    ok(de[0]["near_bound"] and de[0]["targets_missed_at_chosen"] == ["T"],
       "EF_EDGE: a target is missed at the edge when its point estimate is outside its band")

    # EF_COVERAGE: a training target needs effect parameters in benchmark_coverage; a parameter no target names fails
    cov = {"benchmarks": {"T6": {"effect_parameters": ["eff", "wane"]}, "T18": {"effect_parameters": ["wane"]}}}
    ok(coverage_problems(decl, cov, "1") == [], "EF_COVERAGE: the toy coverage names every fitted parameter")
    cov["benchmarks"]["T6"]["effect_parameters"] = []
    cp = coverage_problems(decl, cov, "1")
    ok(any(p.startswith("T6:") for p in cp) and any("parameter eff" in p for p in cp),
       f"EF_COVERAGE: a target without effect parameters, and a parameter no target names, are refused {cp}")

    # EF_PACKS: every engine pack that declares an effect fit declares one this core accepts (no engine run)
    import glob
    found = sorted(glob.glob(os.path.join(ROOT, "toolkit", "engines", "*", "benchmarks", "effect_fit.yaml")))
    for f in found:
        dcl, pk_, cv, ver = pack_files(f)
        probs = declaration_problems(dcl, pk_) or coverage_problems(dcl, cv, ver)
        ok(not probs, f"EF_PACKS: {os.path.relpath(f, ROOT)} is valid against its pack and coverage {probs}")
    ok(bool(found), f"EF_PACKS: at least one pack declares an effect fit ({len(found)})")
    # Codex r2: cached rows are checked too; exact keys; distinct candidates; key presence; identity values
    cc = Cache()
    fit(decl, pack, _toy_engine([]), {"name": "toy"}, cache=cc)
    next(iter(cc.rows.values()))["per_seed"]["T6"].pop()
    ok(refused(lambda: fit(decl, pack, _toy_engine([]), {"name": "toy"}, cache=cc)),
       "EF_SEED_ROWS: a cached row with a missing seed value is refused")
    ok(Cache.key("s", {"x": 0.5}) != Cache.key("s", {"x": 0.5000000001}), "EF_CACHE: keys are exact, not rounded")
    tiny = _toy_decl(parameters={"eff": {"box": [0.5000000001, 0.5000000002], "source": "toy"},
                                 "wane": {"box": [0.2, 1.2], "source": "toy"}})
    tp, ts_ = design_points(tiny)
    ct = candidates(tiny, ts_, [{"theta": x} for x in tp])
    ok(ct[:, 0].min() >= 0.5000000001 and len(np.unique(ct, axis=0)) == len(ct),
       "EF_SUPPORT_ONLY: design points and candidates stay inside a tiny box and are distinct")
    pk = _toy_pack()
    pk["benchmarks"][0]["estimand"]["values"] = []
    ok(any("ambiguous" in x for x in declaration_problems(decl, pk)), "EF_ESTIMAND_MATCH: an empty values key counts")
    pk = _toy_pack()
    del pk["arms"]["trt"]
    pk["settings"][None] = {}
    ok(any("not arms of the pack" in x for x in declaration_problems(decl, pk)),
       "EF_ESTIMAND_MATCH: a missing arm does not resolve to a null setting")
    ok(refused(lambda: fit(decl, pack, _toy_engine([]), {"name": None})),
       "EF_CACHE: an engine identity with an empty value is refused")

    # Generic posterior load/ranges/hard_bounds/fixture tests
    from toolkit.engines.contract import ToyEngine
    from toolkit.spec.toy_engines import ToySpecEngine
    toy_eng = ToyEngine()
    ok(_pack_for_engine(toy_eng)[0].get("fit_joint", {}).get("targets") == ["T6", "T18"]
       and _pack_for_engine(ToySpecEngine())[0].get("fit_joint", {}).get("targets") == ["T6", "T18"],
       "EF_PACK_TOY: _pack_for_engine returns fixture pack for ToyEngine and ToySpecEngine")
    with tempfile.TemporaryDirectory() as td:
        fp = os.path.join(td, "fit.yaml")
        rows_f = [{"eff_a": 0.3 + 0.02 * i, "eff_b": 0.2 + 0.01 * i} for i in range(10)]
        _fixture_placeholder(fp, toy_eng, rows_f, {"eff_a": [0, 1], "eff_b": [0, 1]})
        dig_f = hashlib.sha256(open(fp, "rb").read()).hexdigest()
        raw_b = read_signed(fp, dig_f)
        ne_f = load_posterior(fp, toy_eng, {"eff_a": (0, 1), "eff_b": (0, 1)}, dig_f, raw_b)
        rng_f = posterior_ranges(ne_f, 0.1, hard_bounds(toy_eng, ne_f["names"]))
        ok(rng_f["eff_a"][0] < 0.3 and rng_f["eff_a"][1] > 0.48, f"EF_POSTERIOR: ranges widened by margin: {rng_f}")
        ok(refused(lambda: read_signed(fp, "0" * 64)), "EF_POSTERIOR: wrong digest refused")
    from toolkit import import_guard as IG
    for f in found:
        dcl, pk_, _cv, _ver = pack_files(f)
        for ec in dcl.get("edge_checks") or []:
            pname = ec.get("param")
            box = (dcl.get("parameters") or {}).get(pname, {}).get("box")
            eng_dir = os.path.dirname(os.path.dirname(os.path.abspath(f)))
            eng_mod_name = os.path.basename(eng_dir)
            mod = IG.module("engine", f"toolkit.engines.{eng_mod_name}.engine")
            eng_cls = next(getattr(mod, a) for a in dir(mod) if a.endswith("Engine") and a != "Engine")
            hb_ec = hard_bounds(eng_cls(), [pname])
            ok(pname in hb_ec and hb_ec[pname][0] >= box[0] and hb_ec[pname][1] <= box[1],
               f"EF_EDGE_BOUNDS: {pname} hard_bounds {hb_ec.get(pname)} respects edge_checks box {box}")

    print("OK: all self-test cases passed." if not fails else f"FAILED: {len(fails)} case(s)")
    return 1 if fails else 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--self-test"]:
        return _self_test()
    if len(argv) == 2 and argv[0] == "check":
        dcl, pk, cv, ver = pack_files(argv[1])
        probs = declaration_problems(dcl, pk) or coverage_problems(dcl, cv, ver)
        for p in probs:
            print(f"PROBLEM {p}")
        print("OK: the declaration can drive a fit" if not probs else f"{len(probs)} problem(s)")
        return 1 if probs else 0
    print(__doc__)
    return 2


# ---------------------------------------------------------------------------
# Generic posterior loading, hard bounds, range derivation & test fixture APIs
# (ported from net_effect_fit.py; strictly disease- and parameter-agnostic)
# ---------------------------------------------------------------------------

class PosteriorRefused(ValueError):
    """The fit's output cannot be the draws' joint sample (fail closed)."""


UNIT_BOUNDS = {
    "probability": (0.0, 1.0),
    "share": (0.0, 1.0),
    "days": (0.0, math.inf),
    "months": (0.0, math.inf),
    "years": (0.0, math.inf),
}
DURATIONS = ("days", "months", "years")
_Z95 = 1.96
_CAL_TOL = 0.10
_ACCEPTANCE_RULE = (
    "a verified point, and the chosen point, is in band for a target when the 95% interval "
    "of its seed mean overlaps the target's band (geometric mean over declared seeds)."
)
_ACCEPTANCE_STATISTIC = "geometric mean over seeds; 95% interval exp(mean(log v) +- 1.96 sd(log v)/sqrt(n))"


def _fixture_pack() -> dict:
    """Synthetic pack with two training targets (T6, T18) and one held-out target (TH) for toy tests."""
    p = _toy_pack([{
        "id": "TH",
        "target": 0.65,
        "band": [0.60, 0.70],
        "estimand": {"type": "ratio", "num": "trt", "den": "ctl", "value": "windows.m6.prev"},
    }])
    p["fit_joint"] = {"targets": ["T6", "T18"]}
    return p


def _pack_for_engine(engine) -> tuple[dict, dict | None]:
    """Discover (<eng_dir>/benchmarks/pack.yaml, effect_fit.yaml) via the engine class; fall back for toy engines."""
    if (
        getattr(engine, "is_toy", False)
        or type(engine).__module__ in ("toolkit.engines.contract", "toolkit.engines.hooks", "toolkit.spec.toy_engines")
        or type(engine).__name__.startswith("Toy")
        or any(
            c.__module__ in ("toolkit.engines.contract", "toolkit.engines.hooks", "toolkit.spec.toy_engines")
            and c.__name__.startswith("Toy")
            for c in type(engine).__mro__
        )
    ):
        return _fixture_pack(), None
    try:
        eng_file = inspect.getfile(type(engine))
    except TypeError as ex:
        raise PosteriorRefused(f"cannot locate benchmark pack for engine {type(engine).__name__}: {ex}") from ex
    bdir = os.path.join(os.path.dirname(os.path.abspath(eng_file)), "benchmarks")
    pack_path = os.path.join(bdir, "pack.yaml")
    if not os.path.isfile(pack_path):
        raise PosteriorRefused(f"no benchmark pack found at {pack_path}")
    with open(pack_path, encoding="utf-8") as fh:
        pack = yaml.safe_load(fh) or {}
    eff_path = os.path.join(bdir, "effect_fit.yaml")
    eff = None
    if os.path.isfile(eff_path):
        with open(eff_path, encoding="utf-8") as fh:
            eff = yaml.safe_load(fh)
    return pack, eff


def _pack_targets(pack: dict, eff_decl: dict | None = None) -> list[str]:
    if isinstance(eff_decl, dict) and isinstance(eff_decl.get("targets"), dict):
        return list(eff_decl["targets"])
    return list((pack.get("fit_joint") or {}).get("targets") or [])


def _pack_bands(pack: dict, eff_decl: dict | None = None) -> dict:
    tids = set(_pack_targets(pack, eff_decl))
    return {b["id"]: list(b["band"]) for b in pack.get("benchmarks") or [] if b.get("id") in tids}


def _pack_seeds(pack: dict) -> list:
    return list(pack.get("seeds") or [])


def _replay_target_digests(base: dict, ids) -> dict:
    from toolkit.engines.benchmark_sources import band_digest
    return {b["id"]: band_digest(b, base) for b in base.get("benchmarks") or [] if b.get("id") in set(ids)}


def read_signed(path: str, digest) -> bytes:
    """The file's bytes, read once, if their sha256 matches `digest`; else refused."""
    if not isinstance(digest, str) or len(digest) != 64:
        raise PosteriorRefused(f"net-effect posterior {path}: no signed sha256 (64 hex characters) for the file")
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as ex:
        raise PosteriorRefused(f"net-effect posterior {path}: unreadable ({ex})") from ex
    got = hashlib.sha256(data).hexdigest()
    if got != digest.lower():
        raise PosteriorRefused(f"net-effect posterior {path}: its bytes are not the ones signed (sha256 "
                               f"{got}, signed sha256 {digest})")
    return data


def _same_point(a, b) -> bool:
    return (isinstance(a, dict) and isinstance(b, dict) and sorted(a) == sorted(b)
            and all(isinstance(a[k], (int, float)) and isinstance(b[k], (int, float))
                    and round(float(a[k]), 6) == round(float(b[k]), 6) for k in a))


def declared_seeds(seeds) -> list:
    if not isinstance(seeds, (list, tuple)) or not seeds:
        raise ValueError(f"no declared seeds ({seeds!r})")
    if any(isinstance(x, bool) or not isinstance(x, int) for x in seeds):
        raise ValueError(f"declared seeds must be integers ({list(seeds)!r})")
    dup = sorted({x for x in seeds if list(seeds).count(x) > 1})
    if dup:
        raise ValueError(f"duplicate declared seeds {dup} in {list(seeds)}")
    return list(seeds)


def _seed_interval_95(values, n_seeds: int) -> tuple[float, float] | None:
    if not isinstance(values, (list, tuple)):
        return None
    try:
        v = [float(x) for x in values]
        n_seeds = int(n_seeds)
    except (TypeError, ValueError):
        return None
    if n_seeds < 2 or len(v) != n_seeds or not all(np.isfinite(v)) or min(v) <= 0:
        return None
    lg = np.log(v)
    m, h = float(np.mean(lg)), _Z95 * float(np.std(lg, ddof=1)) / float(np.sqrt(len(v)))
    return float(np.exp(m - h)), float(np.exp(m + h))


def _ci_in_band_95(values, band, n_seeds: int) -> bool:
    iv = _seed_interval_95(values, n_seeds)
    return iv is not None and iv[0] <= float(band[1]) and iv[1] >= float(band[0])


def _per_seed(p) -> dict:
    per = (p or {}).get("per_seed") if isinstance(p, dict) else None
    return per if isinstance(per, dict) else {}


def seed_count_errors(p: dict, bands: dict, seeds) -> list:
    per, n = _per_seed(p), len(seeds) if isinstance(seeds, (list, tuple)) else 0
    out = []
    for t in bands:
        v = per.get(t)
        k = len(v) if isinstance(v, (list, tuple)) else 0
        if k != n:
            out.append(f"{t}: {k} seed values for {n} declared seeds")
    return out


def point_in_band(p: dict, bands: dict, seeds) -> tuple[bool, dict]:
    try:
        n = len(declared_seeds(seeds))
    except ValueError:
        return False, {t: None for t in bands}
    per = _per_seed(p)
    ivs = {t: _seed_interval_95(per.get(t) or [], n) for t in bands}
    return all(_ci_in_band_95(per.get(t) or [], b, n) for t, b in bands.items()), {
        t: [round(v[0], 4), round(v[1], 4)] if v else None for t, v in ivs.items()}


def calibration_ok(cal: dict) -> list:
    return sorted(s for s, c in (cal or {}).items()
                  if not abs(float(c["achieved"]) / float(c["target"]) - 1.0) <= _CAL_TOL)


def finalize(reg: dict, checked: list, chosen: dict | None = None, grid_edge=(), bands: dict | None = None,
             seeds=None) -> tuple[dict, int]:
    bands = {} if bands is None else bands
    seeds = [] if seeds is None else seeds
    why = []
    try:
        declared_seeds(seeds)
    except ValueError as ex:
        why.append(f"the run's seeds cannot be checked against: {ex}")
    for c in checked:
        c["in_band_by_rule"], c["seed_mean_interval"] = point_in_band(c, bands, seeds)
    failed = [c["point"] for c in checked if not c["in_band_by_rule"] or calibration_ok(c.get("calibration_check"))]
    why += [f"verified point out of band (rule) or off calibration: {p}" for p in failed]
    for c in checked:
        if seed_count_errors(c, bands, seeds):
            why.append(f"verified point without one seed value per declared seed at {c.get('point')}: "
                       f"{seed_count_errors(c, bands, seeds)}")
    for c in checked:
        if calibration_ok(c.get("calibration_check")):
            why.append(f"control arms off target by more than {_CAL_TOL:.0%} at {c['point']}: "
                       f"{calibration_ok(c.get('calibration_check'))}")
    if chosen is None:
        why.append("the chosen point's verification is missing")
    else:
        chosen["in_band_by_rule"], chosen["seed_mean_interval"] = point_in_band(chosen, bands, seeds)
        if not chosen["in_band_by_rule"]:
            why.append(f"the chosen point fails its verification under the rule: {chosen['seed_mean_interval']}")
        if seed_count_errors(chosen, bands, seeds):
            why.append(f"the chosen point lacks one seed value per declared seed: "
                       f"{seed_count_errors(chosen, bands, seeds)}")
        if calibration_ok(chosen.get("calibration_check")):
            why.append(f"the chosen point's control arms are off target: {calibration_ok(chosen['calibration_check'])}")
    if grid_edge:
        why.append(f"the calibration grid clamps at its edge for {sorted(grid_edge)}")
    if not checked:
        why.append("no verified sample point")
    reg["verified"] = {
        "n": len(checked), "all_in_band": len(checked) - len(failed), "failed": failed,
        "points": checked, "chosen": chosen, "calibration_tolerance": _CAL_TOL,
        "acceptance_rule": _ACCEPTANCE_RULE, "acceptance_statistic": _ACCEPTANCE_STATISTIC,
        "seeds": list(seeds) if isinstance(seeds, (list, tuple)) else seeds,
        "bands": {t: list(b) for t, b in bands.items()},
    }
    reg["acceptance"] = why
    if why:
        reg["sample_status"] = "unverified" if why == ["no verified sample point"] else "invalid"
        reg["sample_unverified"], reg["sample"] = reg.get("sample") or [], []
        return reg, 2
    reg["sample_status"] = "verified"
    return reg, 0


def hard_bounds(engine, names, both: bool = False) -> dict:
    """{name: (lo, hi, what)}: each parameter's hard bounds from the engine (UNIT_BOUNDS, priors.yaml, edge_checks)."""
    from toolkit.spec import priors as PR
    card = PR.load_card(engine)["priors"]
    units = {q.name: q.units for q in engine.parameters()}
    try:
        _, eff = _pack_for_engine(engine)
    except PosteriorRefused:
        eff = None
    edge_params = {
        c["param"]: c for c in ((eff or {}).get("edge_checks") or [])
        if isinstance(c, dict) and c.get("param")
    }
    eff_params = (eff or {}).get("parameters") or {}
    out = {}
    for k in names:
        b = [(*UNIT_BOUNDS[units[k]], f"card units {units[k]}")] if units.get(k) in UNIT_BOUNDS else []
        if k in edge_params and isinstance((eff_params.get(k) or {}).get("box"), list):
            box = eff_params[k]["box"]
            b.append((float(box[0]), float(box[1]), "effect_fit edge ceiling: " + str(edge_params[k].get("basis") or "")))
        elif k in card:
            b.append((float(card[k].lo), float(card[k].hi), "priors.yaml range"))
        if b:
            out[k] = b if both else (max(x[0] for x in b), min(x[1] for x in b), "; ".join(x[2] for x in b))
    return out


def load_posterior(path: str, engine, ranges: dict, digest, data: bytes | None = None,
                   exception: dict | None = None) -> dict:
    """Load and verify a posterior joint sample with full finalize() replay and hard-bound checking."""
    import copy as _copy
    from toolkit.engines.contract import engine_identity

    def no(why):
        raise PosteriorRefused(f"net-effect posterior {path}: {why}")

    data = read_signed(path, digest) if data is None else data
    if hashlib.sha256(data).hexdigest() != str(digest).lower():
        no(f"the bytes given are not the ones signed (signed sha256 {digest})")
    try:
        doc = yaml.safe_load(data)
    except yaml.YAMLError as ex:
        no(f"malformed YAML ({ex})")
    if not isinstance(doc, dict) or not isinstance(doc.get("joint_region"), dict) or doc.get("producer") not in (BM.PRODUCER, PRODUCER):
        no("not an output of effect_fit (producer, joint_region)")
    if doc.get("smoke") is not False:
        no("a smoke run is not evidence")
    have, want = doc.get("engine_identity") or {}, engine_identity(engine)
    for k in ("name", "code_sha256"):
        if have.get(k) != want[k]:
            no(f"fitted under engine {k} {have.get(k)}, this engine's is {want[k]}")
    reg = doc["joint_region"]
    names = list((doc.get("plan") or {}).get("params") or {})
    rows = reg.get("sample") or reg.get("sample_unverified")
    if not names or not isinstance(rows, list) or not rows:
        no("no fitted parameters or an empty sample")
    X = []
    for i, r in enumerate(rows):
        if not isinstance(r, dict) or sorted(r) != sorted(names):
            no(f"sample row {i} holds {sorted(r) if isinstance(r, dict) else r}, the fit's parameters are {names}")
        if not all(isinstance(r[k], (int, float)) and not isinstance(r[k], bool) and math.isfinite(r[k]) for k in names):
            no(f"sample row {i} holds a non-numeric or non-finite value: {r}")
        X.append([float(r[k]) for k in names])
    ver, edge = reg.get("verified") or {}, (doc.get("calibration_grid") or {}).get("at_grid_edge")
    pts = ver.get("points")
    if not isinstance(edge, list) or not isinstance(pts, list) or any(
            not isinstance(p, dict) or p.get("point") not in rows for p in pts):
        no("the fit's acceptance gate cannot be replayed: a calibration_grid.at_grid_edge record and verified points "
           "that are rows of the sample are needed")
    base, eff = _pack_for_engine(engine)
    seeds, fr = _pack_seeds(base), doc.get("fit_rule")
    rec = fr.get("seeds") if isinstance(fr, dict) else None
    try:
        declared_seeds(rec)
    except ValueError as ex:
        no(f"the run's recorded seeds (fit_rule.seeds) are not a valid declaration: {ex}")
    if rec != seeds:
        no(f"the run's recorded seeds {rec} are not pack.yaml's {seeds}")
    bands = _pack_bands(base, eff)
    exc = None
    if exception is not None:
        if reg.get("sample") != [] or not reg.get("sample_unverified") or reg.get("sample_status") != "invalid":
            no("an exception applies only to a sample the fit recorded as invalid (sample_unverified)")
        need = (doc.get("plan") or {}).get("verified_sample_points")
        distinct = {json.dumps(p.get("point"), sort_keys=True) for p in pts}
        if isinstance(need, bool) or not isinstance(need, int) or need < 1 or len(distinct) < need:
            no(f"the fit verified {len(distinct)} distinct sample points against a declared {need!r}: not an "
               f"exception's case")
        fails = []
        for p in pts:
            per = _per_seed(p)
            bad_ev = seed_count_errors(p, bands, seeds) or [t for t in bands if
                                                              _seed_interval_95(per.get(t) or [], len(seeds)) is None]
            if not point_in_band(p, bands, seeds)[0]:
                cal = p.get("calibration_check")
                bad_ev = bad_ev or not isinstance(cal, dict) or not cal or calibration_ok(cal)
            if bad_ev:
                no(f"verified point {p.get('point')} fails on its seed evidence or calibration, which no exception "
                   f"waives")
            if not point_in_band(p, bands, seeds)[0]:
                per = _per_seed(p)
                fails.append({"point": p["point"], "targets": sorted(
                    t for t, b in bands.items() if not _ci_in_band_95(per.get(t) or [], b, len(seeds)))})
        decl = [{"point": dict(x["point"]), "targets": sorted(x["targets"])} for x in exception["failed_points"]]
        pair = [[j for j, x in enumerate(decl) if _same_point(f["point"], x["point"]) and f["targets"] == x["targets"]]
                for f in fails]
        if not fails or len(fails) != len(decl) or any(len(m) != 1 for m in pair) \
                or sorted(m[0] for m in pair) != list(range(len(decl))):
            no(f"the declared exception {decl} is not the file's recorded failures {fails} (the same points, the same "
               f"targets, one to one)")
        failed = [f["point"] for f in fails]
        pts = [p for p in pts if not any(p["point"] == fp for fp in failed)]
        rows = list(reg["sample_unverified"])
        if exception.get("draws") == "exclude":
            rows = [r for r in rows if not any(r == fp for fp in failed)]
        X = [[float(r[k]) for k in names] for r in rows]
        exc = dict(exception, failed_points=decl)
    try:
        rr, code = finalize({"sample": _copy.deepcopy(rows)}, _copy.deepcopy(pts), _copy.deepcopy(ver.get("chosen")),
                            edge, bands=bands, seeds=seeds)
        why = rr["acceptance"]
    except (KeyError, TypeError, ValueError, AttributeError) as ex:
        code, why = 2, [f"{type(ex).__name__}: {ex}"]
    if code != 0 or (exc is None and reg.get("sample") != rows) or not rows:
        no(f"the fit's acceptance gate (finalize, replayed on the file's evidence) does not pass: {why}")
    miss = [k for k in names if k not in ranges]
    if miss:
        no(f"{miss} are not varied by these draws (an engine-card or spec prior, or a fitted parameter under the "
           f"engine_parameters source, and not calibrated); the draws vary {sorted(ranges)}")
    X = np.asarray(X, float)
    for j, k in enumerate(names):
        for lo, hi, what in hard_bounds(engine, [k], both=True).get(k, []):
            tol = 1e-6 * max(1.0, hi - lo) if math.isfinite(hi - lo) else 1e-6
            if X[:, j].min() < lo - tol or X[:, j].max() > hi + tol:
                no(f"{k} spans [{X[:, j].min()}, {X[:, j].max()}], outside its hard bound [{lo}, {hi}] ({what})")
    from toolkit.engines.benchmark_sources import band_digest
    pl = doc.get("plan") or {}
    tt, ho = pl.get("targets_train"), pl.get("held_out_untouched") or []
    decl_map = {b["id"]: b for b in base["benchmarks"]}
    td = doc.get("target_digests") if isinstance(doc.get("target_digests"), dict) else {}
    trained = {k: td[k] for k in sorted(set(bands) & set(tt) - set(ho)) if decl_map[k].get("band") == bands[k]
               and td.get(k) == band_digest(decl_map[k], base)} if isinstance(tt, list) and isinstance(ho, list) else {}
    return dict({"path": os.path.abspath(path), "sha256": str(digest).lower(), "names": names, "rows": X,
                 "trained": trained}, **({"exception": exc} if exc else {}))


def posterior_ranges(ne: dict, margin=0.0, bounds: dict | None = None) -> dict:
    """{name: (lo, hi)} of each column of a loaded, verified sample."""
    if isinstance(margin, bool) or not isinstance(margin, (int, float)) or not math.isfinite(margin) \
            or not 0 <= margin <= 1:
        raise ValueError(f"net-effect posterior margin {margin!r}: a share of the hull's width in [0, 1]")
    X = np.asarray(ne["rows"], float)
    out = {}
    for j, k in enumerate(ne["names"]):
        lo, hi = float(X[:, j].min()), float(X[:, j].max())
        if not hi > lo:
            raise PosteriorRefused(f"net-effect posterior: column {k} has no spread ({lo}): it cannot set a range")
        if margin:
            w = hi - lo
            blo, bhi, what = (bounds or {}).get(k, (-math.inf, math.inf, ""))
            lo, hi = min(lo, max(lo - margin * w, blo)), max(hi, min(hi + margin * w, bhi))
            if lo <= 0 < X[:, j].min() and any(f"card units {u}" in what for u in DURATIONS):
                raise PosteriorRefused(f"net-effect posterior: margin {margin} takes {k}, a duration, to {lo} (a zero "
                                       "duration is not physical); use a smaller margin")
        out[k] = (lo, hi)
    return out


def _fixture_placeholder(path: str, eng, rows: list, box: dict, gate_ok: bool = True, point=None,
                         fail_target: str | None = None) -> None:
    """Write a placeholder verified-sample fixture for self-tests via safe_io."""
    pack, eff = _pack_for_engine(eng)
    bands = _pack_bands(pack, eff)
    seeds = _pack_seeds(pack)
    ks = [(-1, 0, 1, 0)[i % 4] for i in range(len(seeds))]
    mid = lambda f: {t: [round(float(np.sqrt(lo * hi)) * f * (1 + 0.01 * k), 4) for k in ks]  # noqa: E731
                     for t, (lo, hi) in bands.items()}
    pt = {"point": rows[0], "per_seed": mid(1.0 if gate_ok else 3.0), "calibration_check": {}}
    ch = {"per_seed": mid(1.0), "all_in_band": True, "disproved_by_verification": [], "calibration_check": {}}
    if point is not False:
        ch["point"] = dict(rows[0] if point is None else point)
    pts = [pt]
    if fail_target:
        per = mid(1.0)
        per[fail_target] = [v * 3.0 for v in per[fail_target]]
        pts.append({"point": rows[1], "per_seed": per, "calibration_check": {"site": {"achieved": 1.0, "target": 1.0}}})
    reg, _ = finalize({"sample": [dict(r) for r in rows]}, pts, ch, bands=bands, seeds=seeds)
    fit_doc = {"parameters": {k: {"value": v} for k, v in ch.get("point", {}).items()}}
    doc = BM.stamp({
        "purpose": "effect_fit placeholder fixture",
        "smoke": False,
        "plan": {"params": box, "verified_sample_points": len(pts)},
        "calibration_grid": {"at_grid_edge": []},
        "fit_rule": {"seeds": seeds},
        "target_digests": _replay_target_digests(pack, bands),
        "joint_region": reg,
        **fit_doc,
    }, eng)
    from toolkit.analysis.artifacts import _safe_io
    _safe_io().publish(path, yaml.safe_dump(doc, sort_keys=False).encode(), os.path.dirname(os.path.abspath(path)))


if __name__ == "__main__":
    sys.exit(main())
