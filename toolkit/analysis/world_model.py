#!/usr/bin/env python3
"""The world model (docs/design/v2-progressive-steps.md §4, v0): the modeller's working understanding of how the
decision works, as beliefs that papers, data and simulations confront. It is meant to be mostly right, not precise:
its numbers are bands and rough locations, never decision numbers (those come from the analysis' claims). The rule
throughout: absence of evidence, weak evidence or too few usable points is `inconclusive`, never `supported`.

    python3 toolkit/analysis/world_model.py validate <seed.yaml>
    python3 toolkit/analysis/world_model.py build <run_dir> --seed <seed.yaml> [--out DIR]
        [--engine-identity current|recorded] [--shadow-price LAMBDA]
    python3 toolkit/analysis/world_model.py --self-test

NOT YET FOR USE IN A RUN: the simulation-derived beliefs this version writes are marginal associations, unscreened
for multiple testing across package x feature x pair (`screened: false`). They must not seed or update a world model
until the follow-up (multiplicity control, marginal labels, track-record identity, pre-registration) lands. Nothing
is wired into the analysis or the stage machine. No LLM calls.

Seed (YAML), and the beliefs of analysis/world_model.yaml:

    beliefs:
      - id: B1
        mechanism: "PBO restores net killing where pyrethroid resistance is high"
        about: {package: net_pbo, feature: resistance}       # or {pair: [a, b]}, or {component: c}
        direction: reduces | raises | increases | decreases | negligible | flips | none
        magnitude: {band: [lo, hi], scale: relative_reduction}   # optional: a band, never a point value
        conditions: [{feature: resistance, range: [lo, hi]}]     # optional: where it holds or where it flips
        source_type: literature | data | simulation
        evidence:                 # literature: citation + location (table or page); data: dossier_entry;
          - {citation: "...", location: "Table 2"}       # simulation: artifact + point ids
        confidence: low | medium | high
        decision_relevant: true   # optional
    hypotheses:                   # analysis/hypotheses.yaml: testable expectations with a refutation test
      - {id: H1, belief: B1, claim: "...", test: {kind: ..., ...}}
    open_questions: [{id, question, beliefs: [...], decision_relevant}]

Tests (each declares its pass threshold; a test must concern its belief's package, pair, feature and conditions):
    effect_band {package, band, min_share, where?}  share of usable points whose relative reduction is in the band:
        supported at >= min_share, refuted at <= 1 - min_share; points with no baseline burden are excluded
    effect_direction {package, feature, direction: increases|decreases|negligible, equivalence?, where?}
        tie-aware Spearman rho with its 95% interval: increases / decreases when the interval excludes 0;
        negligible only when it lies inside +/- equivalence (required for that claim); else inconclusive
    paired_sign {a, b, feature, range, sign: positive|negative, min_share, where?}  net benefit of a minus b at the
        budget's shadow price; supported when the claimed option wins at >= min_share of usable points, refuted
        when the other one does; ties are indifferent, counted separately
    flip_in {a, b, feature, range, where?}  the a/b transition along the feature, refitted on the `where` points:
        supported when its whole located range lies inside `range`, refuted when disjoint, else inconclusive; a
        one-sided, rare or non-monotone regime is inconclusive, never refuted
    plan_share {component, where, min_share}  share of matching units whose point plan contains it; a matching unit
        missing from the plan, or a unit missing a filter column, makes it untestable
Statuses: open (not yet checked), supported, refuted, inconclusive, untestable.

The comparison measure is net benefit per person at the budget's shadow price, dfac x (burden - outcome) - lambda x
(cost - comparator cost): ranking by benefit per cost misranks options of different cost under a budget. lambda is
`--shadow-price`, else the cost-effectiveness of the marginal funded package of the point plan (the least
benefit / cost among units given a package, analysis/point.yaml), else none (paired tests untestable).

Fail closed: a belief without evidence, a hypothesis without a refutation test or threshold, a non-finite value,
a missing artifact, a result row not bound to its job (job_sha) and the expected engine identity.
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

PRODUCER = "toolkit/analysis/world_model.py"
SCHEMA, HSCHEMA = "analysis.world_model/0", "analysis.hypotheses/0"
SOURCE_TYPES = ("literature", "data", "simulation")
CONFIDENCE = ("low", "medium", "high")
DIRECTIONS = ("reduces", "raises", "increases", "decreases", "negligible", "flips", "none")
STATUSES = ("open", "supported", "refuted", "inconclusive", "untestable")
TESTS = {"effect_band": ("package", "band", "min_share"), "effect_direction": ("package", "feature", "direction"),
         "paired_sign": ("a", "b", "feature", "range", "sign", "min_share"), "flip_in": ("a", "b", "feature", "range"),
         "plan_share": ("component", "where", "min_share")}
# Settings (recorded in the output): bands are 10th-90th percentiles; a direction needs its 95% interval to exclude 0
# and |rho| >= 0.1, on at least 10 usable points with 3 or more distinct feature values; a flip needs each option to
# win at >= 5% of the non-tied points, no reversal of more than 0.25 in the winning share across 5 quantile bins,
# and a one-feature logistic fit gaining >= 6.63 in deviance (chi-square 1 df, p 0.01). Its range is the fit's 25-75%
# transition widened to the nearest sampled points on either side. At most 3 features are displayed per pair.
SETTINGS = {"band_quantiles": [0.1, 0.9], "z": 1.96, "rho_min": 0.1, "min_points": 10, "flip_min_share": 0.05,
            "flip_deviance_min": 6.63, "bins": 5, "reversal": 0.25, "flips_max": 3, "tie_rtol": 1e-9, "digits": 3}


class WorldModelError(ValueError):
    """An input the world model cannot use: it fails closed with what is missing or inconsistent."""


class Sweep:
    """Per-person sweep tables at the design points: features {name: n values}, packages {name: [components]}, the
    comparator, its burden (primary outcome per person), y {package: outcome per person}, cost {package: $ per
    person}, the shadow price `lam` (outcome per $) and its source, and the artifacts they came from."""

    def __init__(self, features, packages, comparator, burden, y, cost, groups=None, points=None, kinds=None,
                 artifacts=None, lam=None, lam_source=None, dfac=1.0):
        self.features = {k: np.asarray(v, float) for k, v in features.items()}
        self.packages, self.comparator = {k: sorted(v) for k, v in packages.items()}, comparator
        self.burden = np.asarray(burden, float)
        self.y = {k: np.asarray(v, float) for k, v in y.items()}
        self.cost = {k: np.asarray(v, float) for k, v in cost.items()}
        self.groups, self.kinds, self.artifacts = groups or {}, kinds or {}, artifacts or {}
        self.points = list(points if points is not None else range(len(self.burden)))
        self.lam, self.lam_source, self.dfac = lam, lam_source, float(dfac)
        for k in packages:
            if k not in self.y or k not in self.cost:
                raise WorldModelError(f"sweep: package {k!r} has no outcome or cost")
        bad = [n for n, v in [("burden", self.burden)] + [(f"feature {k}", v) for k, v in self.features.items()] +
               [(f"outcome {k}", v) for k, v in self.y.items()] + [(f"cost {k}", v) for k, v in self.cost.items()]
               if len(v) != len(self.points) or not np.all(np.isfinite(v))]
        if bad or (lam is not None and not (isinstance(lam, (int, float)) and math.isfinite(lam) and lam >= 0)):
            raise WorldModelError(f"sweep: non-finite or misaligned values in {bad[:4]} or shadow price {lam!r}")

    def reduction(self, p):
        """Relative reduction against the comparator; undefined (nan) where the baseline burden is not positive."""
        ok = self.burden > 0
        return np.where(ok, 1 - self.y[p] / np.where(ok, self.burden, 1.0), np.nan)

    def net(self, p):
        if self.lam is None:
            raise WorldModelError("no shadow price: pass --shadow-price or give the run an analysis/point.yaml")
        return self.dfac * (self.burden - self.y[p]) - self.lam * (self.cost[p] - self.cost[self.comparator])

    def diff(self, a, b):
        """(net benefit of a minus b, tie tolerance)."""
        na, nb = self.net(a), self.net(b)
        return na - nb, SETTINGS["tie_rtol"] * max(1.0, float(np.max(np.abs(np.concatenate([na, nb])))))


def _r(x):
    return None if x is None or not math.isfinite(float(x)) else float(f"{float(x):.{SETTINGS['digits']}g}")


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _band_ok(b):
    return isinstance(b, (list, tuple)) and len(b) == 2 and all(_num(v) for v in b) and b[0] <= b[1]


def _disjoint(a, b):
    return a[1] < b[0] or b[1] < a[0]


# ---------------------------------------------------------------- seed validation
def _evidence_problem(st, ev):
    if not isinstance(ev, list) or not ev:
        return "no evidence"
    for e in ev:
        if not isinstance(e, dict):
            return "an evidence entry that is not a mapping"
        if st == "literature" and not (str(e.get("citation") or "").strip() and str(e.get("location") or "").strip()):
            return "literature evidence needs a citation and its location (table or page)"
        if st == "data" and not str(e.get("dossier_entry") or "").strip():
            return "data evidence needs its dossier_entry"
        if st == "simulation" and not (e.get("artifact") and isinstance(e.get("points"), list) and e["points"]):
            return "simulation evidence needs its artifact and point ids"
    return None


def _related(t, b):
    """Why a test does not bear on its belief (None when it does): same package or pair, the belief's feature, and
    no region disjoint from the belief's conditions. Only a related test may move the belief's confidence."""
    ab, k = b.get("about") or {}, t["kind"]
    if k.startswith("effect_") and t.get("package") != ab.get("package"):
        return f"tests package {t.get('package')!r}, the belief is about {ab}"
    if k == "effect_direction" and ab.get("feature") and t.get("feature") != ab["feature"]:
        return f"tests feature {t.get('feature')!r}, the belief is about {ab['feature']!r}"
    if k in ("paired_sign", "flip_in") and sorted([t.get("a"), t.get("b")]) != sorted(ab.get("pair") or []):
        return f"tests the pair {[t.get('a'), t.get('b')]}, the belief is about {ab}"
    named = {ab.get("package"), ab.get("component"), *(ab.get("pair") or [])}
    if k == "plan_share" and t.get("component") not in named:
        return f"tests component {t.get('component')!r}, the belief is about {ab}"
    reg = dict(t.get("where") or {}, **({t["feature"]: t["range"]} if t.get("range") and t.get("feature") else {}))
    for c in b.get("conditions") or []:
        r = reg.get(c.get("feature"))
        if _band_ok(r) and _band_ok(c.get("range")) and _disjoint(r, c["range"]):
            return f"its region {c['feature']} {r} lies outside the belief's condition {c['range']}"
    return None


def validate_seed(seed) -> dict:
    if not isinstance(seed, dict):
        raise WorldModelError("world-model seed: a mapping {beliefs, hypotheses, open_questions}")
    errs, ids, hids, by = [], set(), set(), {}
    bs, hs = seed.get("beliefs"), seed.get("hypotheses") or []
    if not isinstance(bs, list) or not bs:
        errs.append("beliefs: a non-empty list")
    for k, b in enumerate(bs if isinstance(bs, list) else []):
        if not isinstance(b, dict):
            errs.append(f"beliefs[{k}]: a mapping")
            continue
        bid = b.get("id")
        if not bid or bid in ids:
            errs.append(f"beliefs[{k}]: a unique id")
        ids.add(bid)
        by[bid] = b
        w = f"belief {bid}"
        if not str(b.get("mechanism") or "").strip():
            errs.append(f"{w}: mechanism (what causes what)")
        if not isinstance(b.get("about"), dict) or not b["about"]:
            errs.append(f"{w}: about (the package, pair or feature it is about)")
        if b.get("direction") not in DIRECTIONS:
            errs.append(f"{w}: direction one of {DIRECTIONS}")
        if b.get("source_type") not in SOURCE_TYPES:
            errs.append(f"{w}: source_type one of {SOURCE_TYPES}")
        elif _evidence_problem(b["source_type"], b.get("evidence")):
            errs.append(f"{w}: {_evidence_problem(b['source_type'], b.get('evidence'))}")
        if b.get("confidence") not in CONFIDENCE:
            errs.append(f"{w}: confidence one of {CONFIDENCE}")
        mg = b.get("magnitude")
        if mg is not None and not (isinstance(mg, dict) and _band_ok(mg.get("band"))):
            errs.append(f"{w}: magnitude.band [lo, hi] of finite numbers (a band, not a point value)")
        for c in b.get("conditions") or []:
            if not (isinstance(c, dict) and c.get("feature") and _band_ok(c.get("range"))):
                errs.append(f"{w}: each condition {{feature, range: [lo, hi]}} of finite numbers")
    for b in bs if isinstance(bs, list) else []:
        for c in (b.get("conflicts") or []) if isinstance(b, dict) else []:
            if not isinstance(c, dict) or c.get("belief") not in ids:
                errs.append(f"belief {b.get('id')}: conflict with an unknown belief {c!r}")
    for k, h in enumerate(hs):
        if not isinstance(h, dict):
            errs.append(f"hypotheses[{k}]: a mapping")
            continue
        hid = h.get("id")
        if not hid or hid in hids:
            errs.append(f"hypotheses[{k}]: a unique id")
        hids.add(hid)
        w = f"hypothesis {hid}"
        if h.get("belief") not in ids:
            errs.append(f"{w}: belief names a belief of the seed ({h.get('belief')!r})")
        if not str(h.get("claim") or "").strip():
            errs.append(f"{w}: claim")
        if h.get("status", "open") not in STATUSES:
            errs.append(f"{w}: status one of {STATUSES}")
        t = h.get("test")
        if not isinstance(t, dict) or t.get("kind") not in TESTS:
            errs.append(f"{w}: a machine-checkable refutation test, kind one of {sorted(TESTS)}")
            continue
        miss = [f for f in TESTS[t["kind"]] if t.get(f) in (None, "", [])]
        if miss:
            errs.append(f"{w}: test {t['kind']} needs {miss} (every test declares its pass threshold)")
            continue
        for f in ("band", "range"):
            if f in t and not _band_ok(t[f]):
                errs.append(f"{w}: test {f} [lo, hi] of finite numbers")
        for f, v in (t.get("where") or {}).items() if isinstance(t.get("where"), dict) else []:
            if isinstance(v, (list, tuple)) and len(v) == 2 and all(isinstance(x, (int, float)) for x in v) \
                    and not _band_ok(v):
                errs.append(f"{w}: where.{f} [lo, hi] of finite numbers")
        lo_share = 0.0 if t["kind"] == "plan_share" else 0.5
        if "min_share" in TESTS[t["kind"]] and not (_num(t["min_share"]) and lo_share < t["min_share"] <= 1):
            errs.append(f"{w}: min_share a number in ({lo_share}, 1]")
        if t["kind"] == "paired_sign" and t.get("sign") not in ("positive", "negative"):
            errs.append(f"{w}: sign positive or negative")
        if t["kind"] == "effect_direction":
            if t.get("direction") not in ("increases", "decreases", "negligible"):
                errs.append(f"{w}: direction increases, decreases or negligible")
            elif t["direction"] == "negligible" and not (_num(t.get("equivalence")) and 0 < t["equivalence"] < 1):
                errs.append(f"{w}: a negligible claim needs an explicit equivalence bound on |rho| in (0, 1)")
        if t["kind"] == "plan_share" and not isinstance(t.get("where"), dict):
            errs.append(f"{w}: where {{feature or column: [lo, hi] or [values]}}")
        if h.get("belief") in by and not errs:
            why = _related(t, by[h["belief"]])
            if why:
                errs.append(f"{w}: does not bear on belief {h['belief']}: {why}")
    if errs:
        raise WorldModelError("world-model seed refused: " + "; ".join(errs))
    return seed


# ---------------------------------------------------------------- coarse summary of a sweep
def direction(x, y, equivalence=None):
    """Tie-aware Spearman association of y with x: increases, decreases, negligible (only with an equivalence bound
    the interval lies inside) or inconclusive (too few points, a constant or categorical input, or no clear sign)."""
    from scipy.stats import rankdata
    m = np.isfinite(x) & np.isfinite(y)
    n = int(m.sum())
    x, y = x[m], y[m]
    if n < SETTINGS["min_points"] or len(np.unique(x)) < 3 or np.ptp(y) == 0:
        return {"direction": "inconclusive", "n": n,
                "why": f"{n} usable points" if n < SETTINGS["min_points"] else "constant or categorical: undefined"}
    rho = float(np.corrcoef(rankdata(x), rankdata(y))[0, 1])
    zr, se = math.atanh(max(min(rho, 1 - 1e-12), -1 + 1e-12)), 1 / math.sqrt(n - 3)
    lo, hi = math.tanh(zr - SETTINGS["z"] * se), math.tanh(zr + SETTINGS["z"] * se)
    rmin = SETTINGS["rho_min"]
    d = "increases" if lo > 0 and rho >= rmin else "decreases" if hi < 0 and rho <= -rmin \
        else "negligible" if equivalence is not None and -equivalence <= lo and hi <= equivalence else "inconclusive"
    return {"direction": d, "rho": _r(rho), "ci": [_r(lo), _r(hi)], "n": n}


def _logistic(x, s, lam=1e-2):
    """One-feature logistic fit of a 0/1 outcome (Newton, a small ridge on the slope so separation stays finite):
    (deviance gain over a constant, midpoint, slope per feature unit)."""
    mu, sd = float(x.mean()), float(x.std()) or 1.0
    X = np.column_stack([np.ones_like(x), (x - mu) / sd])
    w = np.zeros(2)
    for _ in range(100):
        p = 1 / (1 + np.exp(-np.clip(X @ w, -30, 30)))
        g = X.T @ (s - p) - lam * np.array([0, w[1]])
        H = X.T @ (X * (p * (1 - p))[:, None]) + lam * np.diag([0, 1]) + 1e-9 * np.eye(2)
        step = np.linalg.solve(H, g)
        w += step
        if np.max(np.abs(step)) < 1e-9:
            break
    p = np.clip(1 / (1 + np.exp(-np.clip(X @ w, -30, 30))), 1e-12, 1 - 1e-12)
    p0 = float(s.mean())
    ll = np.sum(s * np.log(p) + (1 - s) * np.log(1 - p))
    ll0 = len(s) * (p0 * math.log(p0) + (1 - p0) * math.log(1 - p0))
    return 2 * (ll - ll0), (mu - w[0] * sd / w[1]) if w[1] != 0 else float("nan"), w[1] / sd


def flip_along(sw, a, b, f, mask=None):
    """Roughly where the choice between a and b flips along feature f, on the points in `mask` (a one-feature,
    marginal fit). status: flip, or why there is none to report (insufficient, one_sided, undefined, non_monotone,
    no_flip_detected); points: the ids used."""
    d, tol = sw.diff(a, b)
    m = np.ones(len(d), bool) if mask is None else mask
    win, lose = m & (d > tol), m & (d < -tol)
    keep = win | lose
    x, s = sw.features[f][keep], win[keep].astype(float)
    out = {"feature": f, "n": int(keep.sum()), "ties": int((m & ~keep).sum()), "points": [p for p, k in
                                                                                        zip(sw.points, keep) if k]}
    if keep.sum() < SETTINGS["min_points"]:
        return dict(out, status="insufficient")
    if min(s.mean(), 1 - s.mean()) < SETTINGS["flip_min_share"]:
        return dict(out, status="one_sided", share_a=_r(s.mean()))
    if len(np.unique(x)) < 3:
        return dict(out, status="undefined")
    edges = np.quantile(x, np.linspace(0, 1, SETTINGS["bins"] + 1))
    idx = np.clip(np.searchsorted(edges, x, side="right") - 1, 0, SETTINGS["bins"] - 1)
    sh = np.array([s[idx == k].mean() for k in range(SETTINGS["bins"]) if (idx == k).any()])
    st = np.diff(sh)
    if (st > SETTINGS["reversal"]).any() and (st < -SETTINGS["reversal"]).any():
        return dict(out, status="non_monotone", bin_shares=[_r(v) for v in sh])
    dev, t, b1 = _logistic(x, s)
    lo, hi = float(x.min()), float(x.max())
    if not (dev >= SETTINGS["flip_deviance_min"] and math.isfinite(t) and lo <= t <= hi):
        return dict(out, status="no_flip_detected", deviance_gain=_r(dev))
    h = math.log(3) / abs(b1)
    below, above = x[x <= t], x[x >= t]
    rlo = min(t - h, float(below.max()) if len(below) else lo)      # widened to the nearest sampled points
    rhi = max(t + h, float(above.min()) if len(above) else hi)
    return dict(out, status="flip", at=_r(t), range=[_r(max(lo, rlo)), _r(min(hi, rhi))],
                first_preferred_below=bool(b1 < 0), deviance_gain=_r(dev))


def flip(sw, a, b):
    """The a/b comparison along every feature; `along` displays at most flips_max located flips."""
    if sw.lam is None:
        return {"a": a, "b": b, "status": "no_shadow_price", "along": []}
    d, tol = sw.diff(a, b)
    fl = [flip_along(sw, a, b, f) for f in sorted(sw.features)]
    along = sorted([{k: v for k, v in x.items() if k != "points"} for x in fl if x["status"] == "flip"],
                   key=lambda r: (-r["deviance_gain"], r["feature"]))
    return {"a": a, "b": b, "measure": "net_benefit", "n": len(d), "share_a": _r(np.mean(d > tol)),
            "share_b": _r(np.mean(d < -tol)), "share_tie": _r(np.mean(np.abs(d) <= tol)),
            "along": along[:SETTINGS["flips_max"]], "located": len(along)}


def default_pairs(sw):
    """Competing options: single-component packages whose components share a group (one net type or another)."""
    single = {k: v[0] for k, v in sw.packages.items() if len(v) == 1}
    ks = sorted(single)
    return [(a, b) for i, a in enumerate(ks) for b in ks[i + 1:]
            if sw.groups.get(single[a]) and sw.groups.get(single[a]) == sw.groups.get(single[b])]


def summarise(sw, pairs=None, packages=None):
    """Each package's relative-effect band and its marginal direction along each feature, and roughly where each
    competing pair's choice flips."""
    pairs = sorted({tuple(sorted(p)) for p in (pairs or [])} | set(default_pairs(sw)))
    pk = sorted(({k for k, v in sw.packages.items() if len(v) == 1} | set(packages or []) |
                 {x for p in pairs for x in p}) - {sw.comparator})
    qlo, qhi = SETTINGS["band_quantiles"]
    eff = {}
    for p in pk:
        r = sw.reduction(p)
        u = r[np.isfinite(r)]
        eff[p] = {"n": int(len(u)), "excluded_no_baseline": int(len(r) - len(u)),
                  "median": _r(np.median(u)) if len(u) else None,
                  "band": [_r(np.quantile(u, qlo)), _r(np.quantile(u, qhi))] if len(u) else None,
                  "direction": {f: direction(x, r) for f, x in sorted(sw.features.items())}}
    return {"comparator": sw.comparator, "n_points": len(sw.points), "settings": dict(SETTINGS),
            "shadow_price": {"value": sw.lam, "source": sw.lam_source}, "screened": False,
            "effects": eff, "flips": {f"{a}~{b}": flip(sw, a, b) for a, b in pairs},
            "domain": {f: [_r(x.min()), _r(x.max())] for f, x in sorted(sw.features.items())}}


# ---------------------------------------------------------------- check(): mechanical refutation tests
def _region(sw, *wheres):
    """Points inside every numeric [lo, hi] of every mapping given (ranges on one feature intersect)."""
    m = np.ones(len(sw.points), bool)
    for wh in wheres:
        for f, v in (wh or {}).items():
            if f not in sw.features:
                raise WorldModelError(f"test region: unknown feature {f!r} (sweep features: {sorted(sw.features)})")
            m &= (sw.features[f] >= v[0]) & (sw.features[f] <= v[1])
    return m


def _match(row, where):
    """True or False; None when the row lacks a filter column (the test is then untestable)."""
    for f, v in where.items():
        x = row.get(f)
        if x is None or (isinstance(x, float) and not math.isfinite(x)):
            return None
        if _band_ok(v) and not isinstance(x, str):
            if not (v[0] <= float(x) <= v[1]):
                return False
        elif str(x) not in {str(e) for e in (v if isinstance(v, list) else [v])}:
            return False
    return True


def _ids(sw, m):
    return [p for p, k in zip(sw.points, m) if k]


def _test(t, ctx):
    """(status, observed, artifact, ids of the points or units used) for one refutation test."""
    sw, k = ctx["sweep"], t["kind"]
    art = sw.artifacts.get("results", "analysis/batch/results.jsonl")
    for p in [t.get(x) for x in ("package", "a", "b") if t.get(x)]:
        if p not in sw.packages:
            raise WorldModelError(f"test {k}: unknown package {p!r}")
    if k == "plan_share":
        plan, units = ctx.get("plan"), ctx.get("units") or {}
        if not plan:
            return "untestable", {"reason": "no optimised plan (analysis/point.yaml)"}, None, []
        got = {u: _match(row, t["where"]) for u, row in sorted(units.items())}
        nocol, hit = [u for u, v in got.items() if v is None], [u for u, v in got.items() if v]
        absent = [u for u in hit if u not in plan]
        if nocol or absent or not hit:
            return "untestable", {"reason": f"{len(nocol)} units lack a filter column, {len(absent)} matching units "
                                            f"are missing from the plan, {len(hit)} match"}, "analysis/point.yaml", []
        c = t["component"]
        share = sum(c in sw.packages.get(plan[u], []) or plan[u] == c for u in hit) / len(hit)
        cut = float(t["min_share"])
        st = "supported" if share >= cut else "refuted" if share <= 1 - cut else "inconclusive"
        return st, {"share": _r(share), "units": len(hit)}, "analysis/point.yaml", hit
    if k in ("paired_sign", "flip_in") and sw.lam is None:
        return "untestable", {"reason": "no shadow price for the net-benefit comparison"}, art, []
    if k == "flip_in":
        fa = flip_along(sw, t["a"], t["b"], t["feature"], _region(sw, t.get("where")))
        obs = {x: fa.get(x) for x in ("status", "range", "at", "n", "ties", "bin_shares", "share_a") if x in fa}
        if fa["status"] != "flip":
            return "inconclusive", obs, art, fa["points"]
        r, c = fa["range"], t["range"]
        st = "supported" if c[0] <= r[0] and r[1] <= c[1] else "refuted" if _disjoint(r, c) else "inconclusive"
        return st, obs, art, fa["points"]
    m = _region(sw, t.get("where"), {t["feature"]: t["range"]} if k == "paired_sign" else None)
    if k == "paired_sign":
        d, tol = sw.diff(t["a"], t["b"])
        d = d[m]
        n = len(d)
        if n < SETTINGS["min_points"]:
            return "inconclusive", {"reason": f"{n} usable points", "n": n}, art, _ids(sw, m)
        sa, sb = float(np.mean(d > tol)), float(np.mean(d < -tol))
        mine, other = (sa, sb) if t["sign"] == "positive" else (sb, sa)
        cut = float(t["min_share"])
        st = "supported" if mine >= cut else "refuted" if other >= cut else "inconclusive"
        return st, {"share_a": _r(sa), "share_b": _r(sb), "share_tie": _r(1 - sa - sb), "n": n}, art, _ids(sw, m)
    r = sw.reduction(t["package"])
    use = m & np.isfinite(r)
    obs = {"n": int(use.sum()), "excluded_no_baseline": int((m & ~np.isfinite(r)).sum())}
    if k == "effect_band":
        if use.sum() < SETTINGS["min_points"]:
            return "inconclusive", obs, art, _ids(sw, use)
        cov = float(np.mean((r[use] >= t["band"][0]) & (r[use] <= t["band"][1])))
        cut = float(t["min_share"])
        st = "supported" if cov >= cut else "refuted" if cov <= 1 - cut else "inconclusive"
        return st, dict(obs, coverage=_r(cov), median=_r(np.median(r[use]))), art, _ids(sw, use)
    res = direction(sw.features[t["feature"]][use], r[use], t.get("equivalence"))
    got, want = res["direction"], t["direction"]
    st = "supported" if got == want else "inconclusive" if got == "inconclusive" else "refuted"
    return st, dict(obs, **res), art, _ids(sw, use)


def check(hyps, ctx):
    """Evaluate each hypothesis' refutation test against the run's artifacts; returns the hypotheses with status and
    result (observed value, artifact, ids of the points or units used)."""
    out = []
    for h in hyps:
        st, obs, art, ids = _test(h["test"], ctx)
        out.append(dict(h, status=st, result={"observed": obs, "artifact": art or "analysis/batch/results.jsonl",
                                              "points": ids}))
    return out


# ---------------------------------------------------------------- build: seed + simulation summary + confrontation
def _sim_beliefs(sw, summ):
    ev = [{"artifact": sw.artifacts.get("results", "analysis/batch/results.jsonl"), "points": list(sw.points)}]
    out = []
    for p, e in summ["effects"].items():
        if e["band"] is None:
            continue
        along = {f: v["direction"] for f, v in e["direction"].items() if v["direction"] in ("increases", "decreases")}
        out.append({"id": f"sim.effect.{p}", "about": {"package": p}, "screened": False,
                    "mechanism": f"marginal association in the sweep: {p} against {sw.comparator}, relative reduction "
                                 f"{e['band'][0]} to {e['band'][1]}" +
                                 (f", {', '.join(f'{v} with {f}' for f, v in along.items())}" if along else ""),
                    "direction": "reduces" if (e["median"] or 0) >= 0 else "raises", "along": along,
                    "magnitude": {"band": e["band"], "scale": "relative_reduction"}, "source_type": "simulation",
                    "evidence": ev, "confidence": "medium"})
    for key, fl in summ["flips"].items():
        if fl["along"]:
            top = fl["along"][0]
            out.append({"id": f"sim.flip.{key}", "about": {"pair": [fl["a"], fl["b"]]}, "direction": "flips",
                        "screened": False,
                        "mechanism": f"marginal association in the sweep: {fl['a']} has the higher net benefit "
                                     f"{'below' if top['first_preferred_below'] else 'above'} roughly "
                                     f"{top['feature']} {top['range'][0]} to {top['range'][1]}",
                        "conditions": [{"feature": x["feature"], "range": x["range"]} for x in fl["along"]],
                        "source_type": "simulation", "evidence": ev, "confidence": "medium",
                        "decision_relevant": True})
    return out


def _conflicts(beliefs, sw):
    """Seed beliefs against the simulation on the same estimand: a relative-reduction band against the sweep's band
    over the belief's own conditions, a direction against the sweep's only for an unconditional belief, a flip
    location against the located range on the same feature. Recorded on both, side by side."""
    key = lambda ab: json.dumps({"package": ab["package"]} if "package" in ab else  # noqa: E731
                                {"pair": sorted(ab.get("pair") or [])}, sort_keys=True)
    sims = {key(b["about"]): b for b in beliefs if b["id"].startswith("sim.")}
    for b in [x for x in beliefs if not x["id"].startswith("sim.")]:
        ab, s = b["about"], sims.get(key(b["about"]))
        if s is None:
            continue
        why, conds = [], {c["feature"]: c["range"] for c in b.get("conditions") or []}
        mg = b.get("magnitude") or {}
        if mg.get("band") and mg.get("scale") == "relative_reduction" and "package" in ab \
                and set(conds) <= set(sw.features):
            r = sw.reduction(ab["package"])[_region(sw, conds)]
            r = r[np.isfinite(r)]
            if len(r) >= SETTINGS["min_points"]:
                sb = [_r(np.quantile(r, q)) for q in SETTINGS["band_quantiles"]]
                if _disjoint(mg["band"], sb):
                    why.append(f"magnitude band {mg['band']} against simulated {sb} over {conds or 'the sweep'}")
        sd = (s.get("along") or {}).get(ab.get("feature"))
        if not conds and b["direction"] in ("increases", "decreases") and sd and sd != b["direction"]:
            why.append(f"{b['direction']} with {ab['feature']} against simulated {sd}")
        sc = {c["feature"]: c["range"] for c in s.get("conditions") or []}
        if b["direction"] == "flips":
            why += [f"{f} range {r} against simulated {sc[f]}" for f, r in conds.items()
                    if f in sc and _disjoint(r, sc[f])]
        for x, y in ((b, s), (s, b)):
            if why:
                x.setdefault("conflicts", []).append({"belief": y["id"], "note": "; ".join(why) +
                                                      " (kept side by side, not averaged)"})


def build(seed, sw, plan=None, units=None, inputs=None):
    """(world model, hypotheses): the validated seed, the sweep's simulation beliefs, conflicts, every hypothesis
    checked, the track record and confidence updated, contradictions as open questions, and where to look next."""
    import copy
    seed = copy.deepcopy(validate_seed(seed))
    hyps = seed.get("hypotheses") or []
    pairs = [(h["test"]["a"], h["test"]["b"]) for h in hyps if h["test"]["kind"] in ("paired_sign", "flip_in")]
    pk = [h["test"]["package"] for h in hyps if h["test"].get("package")] + \
        [b["about"]["package"] for b in seed["beliefs"] if b["about"].get("package") in sw.packages]
    summ = summarise(sw, pairs, pk)
    beliefs = seed["beliefs"] + _sim_beliefs(sw, summ)
    _conflicts(beliefs, sw)
    checked = check(hyps, {"sweep": sw, "plan": plan, "units": units or {}})
    tr = copy.deepcopy(seed.get("track_record") or {"history": []})
    for s_ in STATUSES:
        tr.setdefault({"supported": "confirmed", "refuted": "contradicted"}.get(s_, s_), 0)
    by = {b["id"]: b for b in beliefs}
    qs = list(seed.get("open_questions") or [])
    for h in checked:
        tr[{"supported": "confirmed", "refuted": "contradicted"}.get(h["status"], h["status"])] += 1
        tr["history"].append({"hypothesis": h["id"], "belief": h["belief"], "status": h["status"]})
        b = by[h["belief"]]
        if h["status"] == "refuted":
            b.setdefault("contradicted_by", []).append(h["id"])
            qs.append({"id": f"Q.{h['id']}", "beliefs": [b["id"]],
                       "decision_relevant": bool(b.get("decision_relevant")),
                       "question": f"Why was {h['id']} refuted ({h['claim']}; observed {h['result']['observed']})? "
                                   "Either the world model misses a mechanism, or the source or the simulation is "
                                   "wrong: state the cause as a hypothesis and test it; never adjust inputs to agree."})
        elif h["status"] == "supported":
            b.setdefault("confirmed_by", []).append(h["id"])
    for b in seed["beliefs"]:
        i = CONFIDENCE.index(b["confidence"])
        # a contradiction lowers confidence; a confirmation raises it only when no evidence conflicts with the belief
        j = i - 1 if b.get("contradicted_by") else i + 1 if b.get("confirmed_by") and not b.get("conflicts") else i
        if j != i:
            b["confidence_seed"], b["confidence"] = b["confidence"], CONFIDENCE[min(max(j, 0), 2)]
    n = tr["confirmed"] + tr["contradicted"]
    tr["share_confirmed"] = _r(tr["confirmed"] / n) if n else None
    wm = {"schema": SCHEMA, "producer": PRODUCER, "produced_by": "harness", "inputs": inputs or sw.artifacts,
          "structure": {"comparator": sw.comparator, "packages": sorted(sw.packages),
                        "terms": "net benefit per person = discount x baseline burden x relative reduction - shadow "
                                 "price x incremental cost per person"},
          "note": "a working understanding, meant to be mostly right; its bands are never decision numbers. "
                  "Simulation beliefs are marginal associations unscreened for multiple testing (screened: false): "
                  "not for seeding or updating a world model yet",
          "beliefs": beliefs, "open_questions": qs, "track_record": tr, "simulation_summary": summ}
    wm["where_to_look"] = where_to_look(wm)
    return wm, {"schema": HSCHEMA, "producer": PRODUCER, "produced_by": "harness", "hypotheses": checked}


WEAK = {"low": 1.0, "medium": 0.5, "high": 0.2}


def where_to_look(wm, top=10):
    """Every contradicted decision-relevant belief, then the decision-relevant beliefs that are weakly held or in
    conflict, each with the simulation region or evidence that would most change it, and why."""
    flips = {tuple(sorted((f["a"], f["b"]))): f for f in wm["simulation_summary"]["flips"].values()}
    must, out = [], []
    for b in wm["beliefs"]:
        rel = bool(b.get("decision_relevant")) or "pair" in b["about"]
        weak = WEAK[b["confidence"]] + (1.0 if b.get("conflicts") else 0) + (1.0 if b.get("contradicted_by") else 0)
        score = _r(weak * (2.0 if rel else 0.5))
        pair = b["about"].get("pair")
        fl = flips.get(tuple(sorted(pair))) if pair else None
        region = {x["feature"]: x["range"] for x in (fl or {}).get("along", [])[:1]} or \
            {c["feature"]: c["range"] for c in b.get("conditions") or []}
        if b.get("contradicted_by") and rel:
            must.append({"belief": b["id"], "score": score, "kind": "contradiction", "region": region or None,
                         "reason": f"{b['mechanism']}: contradicted by {b['contradicted_by']}; find the cause (a "
                                   "missing mechanism, or a wrong source or simulation) before relying on it"})
        if region:
            out.append({"belief": b["id"], "score": score, "kind": "simulation", "region": region,
                        "reason": f"{b['mechanism']}: located only roughly; sweep points across {region} decide on "
                                  "which side units fall"})
        if b.get("conflicts") or (b["source_type"] != "simulation" and b["confidence"] == "low"):
            what = "; ".join(c["note"] for c in b.get("conflicts") or []) or f"{b['source_type']} evidence is weak"
            out.append({"belief": b["id"], "score": score, "kind": "evidence",
                        "reason": f"{b['mechanism']}: {what}; look for a trial, survey or engine benchmark on "
                                  f"{b['about']} that would settle it"})
    order = lambda r: (-(r["score"] or 0), r["belief"], r["kind"])  # noqa: E731
    return sorted(must, key=order) + sorted(out, key=order)[:top]


# ---------------------------------------------------------------- reading a run (offline)
def bind_rows(rows, jobs, ident):
    """{job id: result} for compute rows bound to their jobs: each row's provenance must carry its job's input hash
    (batch.job_sha) and the expected engine identity. Any other row fails closed (batch.verify_rows' checks, with the
    identity given, so an offline retro-check can bind to the run's recorded identity)."""
    from toolkit.analysis import batch as B
    want = {j["id"]: B.job_sha(j) for j in jobs}
    res, bad = {}, []
    for r in rows:
        jid, body = r.get("id"), r.get("result")
        pv = body.get("provenance") if isinstance(body, dict) else None
        if not isinstance(pv, dict):
            bad.append(f"{jid}: no provenance")
        elif pv.get("job_sha") != want.get(jid):
            bad.append(f"{jid}: inputs differ from the design's job")
        elif pv.get("engine") != ident:
            bad.append(f"{jid}: computed by another engine identity or code")
        else:
            res[jid] = body["result"]
    miss = sorted(set(want) - set(res) - {b.split(":")[0] for b in bad})
    if bad or miss:
        raise WorldModelError(f"result rows not bound to the design's jobs: {len(bad)} refused (e.g. {bad[:3]}), "
                              f"{len(miss)} missing (e.g. {miss[:3]})")
    return res


def _jobs(spec, design):
    """The jobs of a stored design (its own points, parameters and seeds), as batch.design() builds them."""
    from toolkit.analysis import batch as B
    by_id, pk, attr = {str(u[spec.uid]): u for u in spec.units()}, spec.packages(), spec["calibration"].get("attribute")
    jobs = []
    for p in design["points"]:
        u = by_id.get(str(p["unit_id"]))
        if u is None:
            raise WorldModelError(f"design point {p['point']}: unit {p['unit_id']} is not in the spec's units")
        a = spec.unit_attributes(u)
        if attr:
            a[attr] = B.calibrated_value(spec, p["z"])
        jobs += [dict({"point": p["point"], "package": n, "components": spec.engine_package(cs), "unit": a,
                       "params": p["theta"], "seed": p["seed"], "engine": spec["engine"], "horizon": design["horizon"],
                       "windows": B.windows(spec)}, id=f"{p['point']}|{n}") for n, cs in pk.items()]
    return jobs


def shadow_price(run_dir):
    """(lambda, source): the cost-effectiveness of the marginal funded package of the point plan, or (None, why)."""
    from toolkit.analysis.artifacts import read
    if not os.path.isfile(os.path.join(run_dir, "analysis", "point.yaml")):
        return None, "no analysis/point.yaml"
    pu = (read(run_dir, "analysis/point.yaml")["data"].get("per_unit") or {}).values()
    r = [float(v["benefit"]) / float(v["cost"]) for v in pu if _num(v.get("cost")) and v["cost"] > 0
         and _num(v.get("benefit"))]
    return (min(r), "analysis/point.yaml: least benefit / cost among units given a package (the marginal funded "
                    "package)") if r else (None, "point.yaml has no funded unit")


def load_sweep(run_dir, identity="current", lam=None):
    """The per-person sweep tables of a run's engine batch (analysis/batch/design.json and results.jsonl, rows bound
    to the stored design's jobs) and the signed spec (packages, populations, cost items). `identity`: the engine
    identity the rows must carry: `current` (the spec's engine as now) or `recorded` (analysis/point.yaml's
    engine_identity, for an offline retro-check of an older engine; recorded in the output)."""
    from toolkit.alloc import costs as acosts
    from toolkit.analysis import features as FT
    from toolkit.analysis.artifacts import read, sha256
    from toolkit.analysis.core import discount_factor
    from toolkit.analysis.model import _driver, _group
    from toolkit.engines.contract import engine_identity
    from toolkit.spec import schema
    need = {"spec": "decision_spec.yaml", "design": "analysis/batch/design.json",
            "results": "analysis/batch/results.jsonl"}
    miss = [rel for rel in need.values() if not os.path.isfile(os.path.join(run_dir, rel))]
    if miss:
        raise WorldModelError(f"world model: missing run artifacts {miss} (the sweep cannot be read)")
    spec = schema.load(os.path.join(run_dir, need["spec"]))
    design = json.load(open(os.path.join(run_dir, need["design"])))
    names, pk = design.get("packages"), spec.packages()
    if names != list(pk) or not design.get("points"):
        raise WorldModelError(f"design packages {names} differ from the spec's {list(pk)}, or no design points")
    if identity == "recorded":
        try:
            ident = read(run_dir, "analysis/point.yaml")["data"]["engine_identity"]
        except (OSError, KeyError, TypeError) as e:
            raise WorldModelError(f"--engine-identity recorded needs analysis/point.yaml engine_identity ({e})")
    elif identity == "current":
        ident = engine_identity(spec.engine())
    else:
        raise WorldModelError(f"engine identity {identity!r}: current or recorded")
    jobs = _jobs(spec, design)
    rows = []
    with open(os.path.join(run_dir, need["results"])) as f:
        for line in f:
            r = json.loads(line) if line.strip() else {}
            if r.get("ok") and isinstance(r.get("i"), int) and r["i"] < len(jobs):
                rows.append(dict(r, id=jobs[r["i"]]["id"]))
    res = bind_rows(rows, jobs, ident)
    pops, o = spec["decision_units"]["populations"], spec.outcomes()[0]
    co = (spec["analysis"].get("cost_outputs") or {})
    alloc, attr = spec.alloc_spec(), spec["calibration"].get("attribute")
    by_id = {str(u[spec.uid]): u for u in spec.units()}
    feats = [FT.name(x) for x in FT.declared(spec.raw)]
    pnames = list(design.get("fitted_names") or []) + list(design.get("prior_names") or []) + \
        list(design.get("spec_value_names") or [])
    F = {n_: [] for n_ in ([f"{attr} (calibration scale)"] if attr else []) + feats + pnames}
    y, cost, burden = {k: [] for k in names}, {k: [] for k in names}, []
    for p in design["points"]:
        u = by_id[str(p["unit_id"])]
        tot = sum(float(u[pm["column"]]) for pm in pops.values())
        rk = {k: res[f"{p['point']}|{k}"] for k in names}
        for k in names:
            y[k].append(sum(_group(rk[k], o, pm) * float(u[pm["column"]]) for pm in pops.values()) / tot)
        cm = acosts.cost_matrix(alloc, [u], outcome=lambda _u, k: {
            nm: _driver(rk[k], m["cost_driver"], f"design point {p['point']}, {k}") * tot for nm, m in co.items()})
        for k in names:
            cost[k].append(cm[str(u[spec.uid])][k]["total"] / tot)
        burden.append(y[spec.comparator][-1])
        vals = ([p["z"]] if attr else []) + FT.vector(spec, u) + [float(p["theta"][nm]) for nm in pnames]
        for n_, v in zip(F, vals):
            F[n_].append(float(v))
    groups = {c: (d or {}).get("group") for c, d in spec["options"]["components"].items() if (d or {}).get("group")}
    kinds = {n_: "parameter" if n_ in pnames else "unit" for n_ in F}
    arts = {rel: sha256(os.path.join(run_dir, rel)) for rel in need.values()}
    lam_v, lam_s = (float(lam), "--shadow-price") if lam is not None else shadow_price(run_dir)
    arts.update(results=need["results"], engine_identity_mode=identity)
    sw = Sweep(F, pk, spec.comparator, burden, y, cost, groups, [p["point"] for p in design["points"]], kinds,
               arts, lam_v, lam_s, discount_factor(spec))
    return sw, spec


def load_plan(run_dir, spec, sw):
    """(plan, units) from analysis/point.yaml and the spec's units (columns, features, calibrated value), or
    (None, None) when the run has no optimised plan. A plan missing units is kept as it is: a plan_share test over
    a missing unit is untestable."""
    from toolkit.analysis import features as FT
    from toolkit.analysis.artifacts import read
    if not os.path.isfile(os.path.join(run_dir, "analysis", "point.yaml")):
        return None, None
    d = read(run_dir, "analysis/point.yaml")["data"]
    plan = {str(k): v for k, v in d["plan"].items()}
    bad = sorted(set(plan.values()) - set(sw.packages))
    by_id = {str(u[spec.uid]): u for u in spec.units()}
    if bad or set(plan) - set(by_id):
        raise WorldModelError(f"point.yaml: unknown packages {bad[:3]} or units {sorted(set(plan) - set(by_id))[:3]}")
    feats = [FT.name(x) for x in FT.declared(spec.raw)]
    attr = spec["calibration"].get("attribute")
    units = {}
    for i, u in by_id.items():
        row = dict(u, **dict(zip(feats, FT.vector(spec, u))))
        pu = d.get("per_unit") or {}
        z = (pu.get(i) or pu.get(int(i) if i.isdigit() else i) or {}).get("z")
        if attr and z is not None:
            row[f"{attr} (calibration scale)"] = float(z)
        units[i] = row
    return plan, units


def _cli(argv):
    import argparse
    import yaml
    ap = argparse.ArgumentParser(prog="world_model.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("validate")
    v.add_argument("seed")
    b = sub.add_parser("build")
    b.add_argument("run_dir")
    b.add_argument("--seed", required=True)
    b.add_argument("--out")
    b.add_argument("--engine-identity", default="current", choices=("current", "recorded"))
    b.add_argument("--shadow-price", type=float)
    a = ap.parse_args(argv)
    seed = yaml.safe_load(open(a.seed))
    if a.cmd == "validate":
        validate_seed(seed)
        print(f"OK: {len(seed['beliefs'])} beliefs, {len(seed.get('hypotheses') or [])} hypotheses")
        return 0
    from toolkit.analysis.artifacts import _safe_io, sha256
    sw, spec = load_sweep(a.run_dir, a.engine_identity, a.shadow_price)
    plan, units = load_plan(a.run_dir, spec, sw)
    inputs = dict({k: v for k, v in sw.artifacts.items() if k != "results"}, seed=sha256(a.seed))
    if plan is not None:
        inputs["analysis/point.yaml"] = sha256(os.path.join(a.run_dir, "analysis", "point.yaml"))
    wm, hy = build(seed, sw, plan, units, inputs)
    out = a.out or os.path.join(a.run_dir, "analysis")
    os.makedirs(out, exist_ok=True)
    for name, doc in (("world_model.yaml", wm), ("hypotheses.yaml", hy)):      # no-follow, atomic (lib.safe_io)
        _safe_io().write_nofollow(out, name, yaml.safe_dump(json.loads(json.dumps(doc)), sort_keys=False,
                                                           width=110).encode(), atomic=True)
    st = {}
    for h in hy["hypotheses"]:
        st[h["status"]] = st.get(h["status"], 0) + 1
    print(f"wrote {out}/world_model.yaml and hypotheses.yaml: {len(wm['beliefs'])} beliefs, hypotheses {st}")
    return 0


# ---------------------------------------------------------------- self-test
EVIDENCE_TIME, REGISTERED = "2026-01-01T00:00:00+00:00", "2025-12-01T00:00:00+00:00"   # synthetic sweeps and seeds


def _sw(feats, rA, rB, cA=None, cB=None, burden=None, lam=0.1):
    """A sweep with packages none, A (component a) and B (component b, same group): relative reductions rA, rB."""
    n = len(rA)
    one = np.ones(n)
    bu = one if burden is None else np.asarray(burden, float)
    sw = Sweep(feats, {"none": [], "A": ["a"], "B": ["b"]}, "none", bu,
               {"none": bu, "A": bu * (1 - np.asarray(rA)), "B": bu * (1 - np.asarray(rB))},
               {"none": 0 * one, "A": one if cA is None else cA * one, "B": one if cB is None else cB * one},
               groups={"a": "g", "b": "g"}, points=list(range(n)), artifacts={"results": "synthetic"}, lam=lam)
    sw.evidence_time = EVIDENCE_TIME    # tests-first shim: the current Sweep takes no evidence time (ι passes it)
    return sw


def _planted(seed=3, n=200):
    """A planted-truth sweep: A's relative effect is constant (0.30); B's rises with f1 (0.6 x f1); f2 is irrelevant;
    equal costs, so A has the higher net benefit exactly where f1 < 0.5."""
    rng = np.random.default_rng(seed)
    f1, f2 = rng.uniform(0, 1, n), rng.uniform(0, 1, n)
    burden = 1.0 + 0.1 * rng.standard_normal(n)
    return _sw({"f1": f1, "f2": f2}, 0.30 + 0.02 * rng.standard_normal(n), 0.60 * f1 + 0.03 * rng.standard_normal(n),
               burden=burden)


def _seed():
    lit = [{"citation": "Trial 2020", "location": "Table 2"}]
    return {"beliefs": [
        {"id": "B1", "mechanism": "A reduces the outcome by about a third", "about": {"package": "A"},
         "direction": "reduces", "magnitude": {"band": [0.2, 0.4], "scale": "relative_reduction"},
         "source_type": "literature", "evidence": lit, "confidence": "medium"},
        {"id": "B2", "mechanism": "B's effect grows with f1", "about": {"package": "B", "feature": "f1"},
         "direction": "increases", "magnitude": {"band": [0.7, 0.9], "scale": "relative_reduction"},
         "source_type": "literature", "evidence": lit, "confidence": "low", "decision_relevant": True},
        {"id": "B3", "mechanism": "A is preferred over B where f1 is low", "about": {"pair": ["A", "B"]},
         "direction": "flips", "conditions": [{"feature": "f1", "range": [0.3, 0.7]}],
         "source_type": "data", "evidence": [{"dossier_entry": "D-1"}], "confidence": "low",
         "decision_relevant": True},
        {"id": "B4", "mechanism": "A beats B on net benefit where f1 is high", "about": {"pair": ["A", "B"]},
         "direction": "none", "source_type": "literature", "evidence": lit, "confidence": "medium"}],
        "hypotheses": [
            {"id": "H1", "registered_at": REGISTERED, "belief": "B1", "claim": "A's effect is in [0.2, 0.4] at 80% of points",
             "test": {"kind": "effect_band", "package": "A", "band": [0.2, 0.4], "min_share": 0.8}},
            {"id": "H2", "registered_at": REGISTERED, "belief": "B4", "claim": "A beats B wherever f1 is above 0.7",
             "test": {"kind": "paired_sign", "a": "A", "b": "B", "feature": "f1", "range": [0.7, 1.0],
                      "sign": "positive", "min_share": 0.8}},
            {"id": "H3", "registered_at": REGISTERED, "belief": "B3", "claim": "the A/B choice flips for f1 in [0.3, 0.7]",
             "test": {"kind": "flip_in", "a": "A", "b": "B", "feature": "f1", "range": [0.3, 0.7]}},
            {"id": "H4", "registered_at": REGISTERED, "belief": "B2", "claim": "B's effect increases with f1",
             "test": {"kind": "effect_direction", "package": "B", "feature": "f1", "direction": "increases"}},
            {"id": "H5", "registered_at": REGISTERED, "belief": "B3", "claim": "most low-f1 units get A",
             "test": {"kind": "plan_share", "component": "A", "where": {"f1": [0.3, 0.5]}, "min_share": 0.8}}]}


def _one(sw, t, plan=None, units=None):
    """The status and result of one test, through check()."""
    h = check([{"id": "H", "belief": "B", "claim": "c", "test": t}], {"sweep": sw, "plan": plan, "units": units})[0]
    return h["status"], h["result"]


def _self_test() -> int:
    import copy
    import json
    import tempfile
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)

    def tryit(label, fn):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            ok(False, f"{label}: {type(e).__name__}: {e}")
    sw = _planted()

    def wm1():
        s = summarise(sw)
        eb = s["effects"]["B"]["direction"]
        ok(eb["f1"]["direction"] == "increases" and eb["f2"]["direction"] == "inconclusive",
           f"WM1 the summariser recovers B's direction along f1, and no direction (inconclusive) along f2: {eb}")
        ea = s["effects"]["A"]
        ok(ea["band"][0] <= 0.30 <= ea["band"][1] and all(v["direction"] == "inconclusive" for v in
                                                          ea["direction"].values()),
           f"WM2 A's effect band contains the true 0.30 and A has no direction along any feature: {ea}")
        fl = s["flips"]["A~B"]
        ok(fl["along"] and fl["along"][0]["feature"] == "f1" and fl["along"][0]["range"][0] <= 0.5
           <= fl["along"][0]["range"][1] and fl["along"][0]["first_preferred_below"] is True,
           f"WM3 the A/B flip is located along f1 with a range containing the true 0.5, A preferred below: {fl}")
        ok(all(a["feature"] != "f2" for a in fl["along"]), f"WM4 no flip is reported along the irrelevant f2: {fl}")
        ok(json.dumps(summarise(_planted()), sort_keys=True) == json.dumps(s, sort_keys=True),
           "WM5 the summary is deterministic for a seed")
    tryit("WM1-5 summariser", wm1)

    def wm6():
        for mut, why in ((lambda b: b.pop("evidence"), "no evidence"),
                         (lambda b: b["evidence"][0].pop("location"), "literature without table or page"),
                         (lambda b: b.update(source_type="hunch"), "unknown source type"),
                         (lambda b: b.update(confidence="certain"), "unknown confidence"),
                         (lambda b: b["magnitude"].update(band=[float("nan"), 0.4]), "a NaN band")):
            sd = _seed()
            mut(sd["beliefs"][0])
            try:
                validate_seed(sd)
                ok(False, f"WM6 validation refuses a belief with {why}")
            except WorldModelError:
                ok(True, f"WM6 validation refuses a belief with {why}")
        for mut, why in ((lambda h: h.pop("test"), "no refutation test"),
                         (lambda h: h["test"].update(kind="vibes"), "an unknown test kind"),
                         (lambda h: h["test"].pop("band"), "a test missing a field"),
                         (lambda h: h.update(belief="B9"), "an unknown belief"),
                         (lambda h: h["test"].pop("min_share"), "no declared pass threshold"),
                         (lambda h: h["test"].update(min_share=-1), "min_share -1"),
                         (lambda h: h["test"].update(min_share=2), "min_share 2"),
                         (lambda h: h["test"].update(min_share=float("nan")), "min_share NaN"),
                         (lambda h: h["test"].update(package="B"), "a test on another package than its belief's"),
                         (lambda h: h["test"].update(kind="effect_direction", feature="f1", direction="negligible"),
                          "a negligible claim without an equivalence bound")):
            sd = _seed()
            mut(sd["hypotheses"][0])
            try:
                validate_seed(sd)
                ok(False, f"WM7 validation refuses a hypothesis with {why}")
            except WorldModelError:
                ok(True, f"WM7 validation refuses a hypothesis with {why}")
        sd = _seed()
        sd["hypotheses"][2]["test"]["range"] = [0.8, 0.9]
        try:
            validate_seed(sd)
            ok(False, "WM7 validation refuses a test whose region lies outside its belief's conditions")
        except WorldModelError:
            ok(True, "WM7 validation refuses a test whose region lies outside its belief's conditions")
        validate_seed(_seed())
        ok(True, "WM6b a seed whose beliefs all have evidence and whose hypotheses all have tests is accepted")
        try:
            _sw({"f": [0.0, 1.0]}, [0.1, float("nan")], [0.1, 0.2])
            ok(False, "WM7b a sweep with a NaN outcome is refused")
        except WorldModelError:
            ok(True, "WM7b a sweep with a NaN outcome is refused")
    tryit("WM6-7 validation", wm6)

    def wm8():
        rng = np.random.default_rng(1)
        units = {str(i): {"f1": float(x), "f2": 0.5} for i, x in enumerate(rng.uniform(0, 1, 40))}
        plan = {u: ("A" if r["f1"] < 0.5 else "B") for u, r in units.items()}
        wm, hy = build(_seed(), sw, plan=plan, units=units)
        st = {h["id"]: h["status"] for h in hy["hypotheses"]}
        ok(st == {"H1": "supported", "H2": "refuted", "H3": "supported", "H4": "supported", "H5": "supported"},
           f"WM8 check() supports the true hypotheses and flags the false one (H2) as refuted: {st}")
        tr = wm["track_record"]
        ok(tr["confirmed"] == 4 and tr["contradicted"] == 1 and any(h["hypothesis"] == "H2" for h in tr["history"]),
           f"WM9 the track record counts confirmations and contradictions: {tr}")
        b2 = next(b for b in wm["beliefs"] if b["id"] == "B2")
        ok(any(c["belief"].startswith("sim.") for c in b2.get("conflicts", [])) and b2["confidence"] == "low",
           f"WM10 the literature band for B (0.7-0.9) and the simulated band are kept side by side as a conflict, "
           f"and the confirmation of its direction does not raise its confidence: {b2}")
        q = next((o for o in wm["open_questions"] if "H2" in o["question"]), {})
        ok(q.get("beliefs") == ["B4"], f"WM11 a contradiction becomes an open question on its belief: {q}")
        ok(all(h["result"].get("artifact") for h in copy.deepcopy(hy)["hypotheses"]), "WM12 every result names "
                                                                                         "its artifact")
        look = where_to_look(wm)
        rest = [x for x in look if x["kind"] != "contradiction"]
        ok(look[0]["kind"] == "contradiction" and look[0]["belief"] == "B4" and rest[0]["belief"] in ("B2", "B3"),
           "WM13 where_to_look lists the contradicted belief first, then ranks the low-confidence, decision-relevant, "
           f"conflicting belief: {look[:2]}")
        reg = next((x for x in look if x["kind"] == "simulation" and "f1" in (x.get("region") or {})), None)
        ok(reg is not None and reg["region"]["f1"][0] <= 0.5 <= reg["region"]["f1"][1],
           f"WM14 where_to_look proposes simulation points over the flip range, which contains the true 0.5: {reg}")
        _, hy_np = build(copy.deepcopy(_seed()), sw, plan=None, units=None)
        ok(next(h for h in hy_np["hypotheses"] if h["id"] == "H5")["status"] == "untestable",
           "WM15 a plan hypothesis without a plan is untestable, never supported")
        sd = _seed()
        sd["beliefs"][3].update(decision_relevant=True, confidence="high")
        wm2, _ = build(sd, sw, plan=plan, units=units)
        ok(any(x["belief"] == "B4" and x["kind"] == "contradiction" for x in where_to_look(wm2, top=0)),
           "WM33 where_to_look always lists a contradicted decision-relevant belief, even with no conditions or "
           "conflicts and top=0")
    tryit("WM8-15, 33 build, check, where_to_look", wm8)

    def wm16():
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "analysis", "batch"))
            json.dump({"points": [], "packages": []}, open(os.path.join(d, "analysis", "batch", "design.json"), "w"))
            try:
                load_sweep(d)
                ok(False, "WM16 a run without results.jsonl fails closed")
            except WorldModelError as e:
                ok("results" in str(e) or "decision_spec" in str(e), f"WM16 a run without results fails closed: {e}")
    tryit("WM16 load", wm16)

    def wm17():
        from toolkit.analysis import batch as B
        jobs = [{"id": f"{i}|A", "point": i, "package": "A", "params": {"p": i}, "seed": i} for i in range(3)]
        ident = {"name": "e", "version": "1"}
        good = [{"id": j["id"], "result": {"result": {"v": 1}, "provenance": {"job_sha": B.job_sha(j),
                                                                              "engine": ident}}} for j in jobs]
        ok(set(bind_rows(good, jobs, ident)) == {j["id"] for j in jobs}, "WM17 rows bound to their jobs are kept")
        for bad, why in (([dict(r, result={"result": {"v": 1}}) for r in good], "rows without provenance"),
                         ([dict(good[0], id=jobs[1]["id"]), good[1], good[2]],
                          "a row whose inputs differ from its job"),
                         (good, "rows from another engine identity"), (good[:2], "a missing row")):
            try:
                bind_rows(bad, jobs, ident if why != "rows from another engine identity" else {"name": "e2"})
                ok(False, f"WM17 {why} are refused")
            except WorldModelError:
                ok(True, f"WM17 {why} are refused")
    tryit("WM17 provenance binding", wm17)

    def wm18():
        x = np.linspace(0, 1, 100)
        d1, d2 = direction(x, np.full(100, 0.3)), direction(np.full(20, 1.0), np.full(20, 2.0))
        ok(d1["direction"] == "inconclusive" and d2["direction"] == "inconclusive",
           f"WM18 tie-aware ranks: an exactly constant effect, or two constant inputs, have no direction: {d1}, {d2}")
        d3 = direction(np.arange(5.0), np.arange(5.0))
        ok(d3["direction"] == "inconclusive", f"WM19 five points are too few for any direction: {d3}")
        d4 = direction(np.array([0, 0, 0, 1, 1, 1, 1, 0, 1, 0, 1, 0] * 3, float), np.arange(36.0))
        ok(d4["direction"] == "inconclusive", f"WM19b a two-valued (categorical) feature has no direction: {d4}")
        rng = np.random.default_rng(0)
        xs = rng.uniform(0, 1, 400)
        s_neg, _ = _one(_sw({"x": xs}, 0.3 + 0 * xs, 0.3 + 0.01 * rng.standard_normal(400)),
                        {"kind": "effect_direction", "package": "B", "feature": "x", "direction": "negligible",
                         "equivalence": 0.2})
        ok(s_neg == "supported", f"WM19c 'negligible' is supported only with its explicit equivalence bound: {s_neg}")
    tryit("WM18-19 direction", wm18)

    def wm20():
        x = np.linspace(0, 1, 200)
        same = _sw({"x": x}, 0.3 + 0 * x, 0.3 + 0 * x)
        st, res = _one(same, {"kind": "paired_sign", "a": "A", "b": "B", "feature": "x", "range": [0, 1],
                              "sign": "negative", "min_share": 0.8})
        ok(st == "inconclusive" and res["observed"]["share_tie"] == 1.0,
           f"WM20 identical options are ties, never a win for the second option: {st} {res['observed']}")
        tie_above = _sw({"x": x}, np.where(x < 0.5, 0.5, 0.3), 0.3 + 0 * x)
        fl = summarise(tie_above)["flips"]["A~B"]
        ok(not fl["along"], f"WM20b A winning below 0.5 and tying above is not a flip (B never wins): {fl}")
    tryit("WM20 ties", wm20)

    def wm21():
        x = np.linspace(0, 1, 200)
        bu = np.where(np.arange(200) == 7, 1.0, 0.0)
        st, res = _one(_sw({"x": x}, 0.3 + 0 * x, 0.3 + 0 * x, burden=bu),
                       {"kind": "effect_band", "package": "A", "band": [0.2, 0.4], "min_share": 0.8})
        ok(st == "inconclusive" and res["points"] == [7] and res["observed"]["excluded_no_baseline"] == 199,
           f"WM21 the minimum sample counts usable points; exclusions are reported and only used ids listed: {st} "
           f"{res['observed']} {res['points']}")
        st0, _ = _one(_sw({"x": x}, 0 * x, 0 * x, burden=0 * x),
                      {"kind": "effect_band", "package": "A", "band": [0.9, 1.0], "min_share": 0.8})
        ok(st0 == "inconclusive", f"WM21b zero-baseline points are not 100% reductions: {st0}")
    tryit("WM21 usable sample", wm21)

    def wm22():
        rng = np.random.default_rng(5)
        x, z = rng.uniform(0, 1, 400), (rng.uniform(0, 1, 400) < 0.5).astype(float)
        thr = np.where(z == 0, 0.2, 0.8)
        cs = _sw({"x": x, "z": z}, np.where(x < thr, 0.5, 0.2), 0.35 + 0 * x)
        t = {"kind": "flip_in", "a": "A", "b": "B", "feature": "x", "where": {"z": [0, 0]}}
        s1, r1 = _one(cs, dict(t, range=[0.05, 0.35]))
        s2, _ = _one(cs, dict(t, range=[0.45, 0.55]))
        ok(s1 == "supported" and s2 == "refuted", f"WM22 a conditional flip test refits on its `where` points "
                                                  f"(true threshold 0.2 at z=0): {s1} {r1['observed']}, {s2}")
        x2 = np.linspace(0, 1, 1000)
        sh = _sw({"x": x2}, np.where(x2 < 0.5, 0.5, 0.2), 0.35 + 0 * x2)
        s3, r3 = _one(sh, {"kind": "flip_in", "a": "A", "b": "B", "feature": "x", "range": [0.47, 0.49]})
        ok(s3 != "supported", f"WM23 overlap with the located range is not confirmation (true 0.5, claim "
                              f"[0.47, 0.49]): {s3} {r3['observed']}")
        xg = np.concatenate([np.linspace(0, 0.1, 500), np.linspace(0.9, 1, 500)])
        gap = _sw({"x": xg}, np.where(xg < 0.2, 0.5, 0.2), 0.35 + 0 * xg)
        fg = flip_along(gap, "A", "B", "x")
        s4, _ = _one(gap, {"kind": "flip_in", "a": "A", "b": "B", "feature": "x", "range": [0.4, 0.6]})
        ok(fg["status"] == "flip" and fg["range"][0] <= 0.2 <= fg["range"][1] and s4 == "inconclusive",
           f"WM24 the located range spans the unsampled gap, so it contains the true 0.2: {fg.get('range')} {s4}")
        nm = _sw({"x": x2}, np.where((x2 > 0.45) & (x2 < 0.55), 0.5, 0.2), 0.35 + 0 * x2)
        s5, r5 = _one(nm, {"kind": "flip_in", "a": "A", "b": "B", "feature": "x", "range": [0.0, 1.0]})
        ok(s5 == "inconclusive", f"WM25 a non-monotone or rare regime is inconclusive, never refuted: {s5} "
                                 f"{r5['observed']['status']}")
        alias = _sw({f"x{k}": x2 for k in range(1, 5)}, np.where(x2 < 0.5, 0.5, 0.2), 0.35 + 0 * x2)
        s6, _ = _one(alias, {"kind": "flip_in", "a": "A", "b": "B", "feature": "x4", "range": [0.3, 0.7]})
        ok(s6 == "supported", f"WM26 a test evaluates its own feature, whatever the summary displays: {s6}")
        s7, r7 = _one(sh, {"kind": "paired_sign", "a": "A", "b": "B", "feature": "x", "range": [0.0, 0.4],
                           "where": {"x": [0.3, 1.0]}, "sign": "positive", "min_share": 0.9})
        ok(s7 == "supported" and r7["observed"]["n"] == 100, f"WM27 `where` intersects the test's range, never "
                                                             f"replaces it: {s7} {r7['observed']}")
    tryit("WM22-27 flips", wm22)

    def wm28():
        r = np.array([0.0] * 49 + [0.3] * 2 + [1.0] * 49)
        st, res = _one(_sw({"x": np.linspace(0, 1, 100)}, r, r), {"kind": "effect_band", "package": "A",
                                                                  "band": [0.2, 0.4], "min_share": 0.8})
        ok(st == "refuted" and res["observed"]["coverage"] == 0.02, f"WM28 effect_band tests coverage of the band, "
                                                                    f"not the median: {st} {res['observed']}")
    tryit("WM28 effect band", wm28)

    def wm29():
        units = {str(i): {"region": "north" if i < 100 else "south"} for i in range(110)}
        t = {"kind": "plan_share", "component": "a", "where": {"region": ["north"]}, "min_share": 0.8}
        s1, _ = _one(sw, t, plan={"0": "A"}, units=units)
        units["105"] = {}
        s2, _ = _one(sw, t, plan={u: "A" for u in units}, units=units)
        ok(s1 == "untestable" and s2 == "untestable", f"WM29 matching units missing from the plan, or a unit missing "
                                                      f"the filter column, make plan_share untestable: {s1} {s2}")
    tryit("WM29 plan share", wm29)

    def wm32():
        x = np.random.default_rng(2).uniform(0, 1, 400)
        cs = _sw({"x": x}, 0.3 + 0 * x, 0.6 * x)
        sd = {"beliefs": [{"id": "C1", "mechanism": "B is strong at high x", "about": {"package": "B"},
                           "direction": "reduces", "magnitude": {"band": [0.55, 0.62], "scale": "relative_reduction"},
                           "conditions": [{"feature": "x", "range": [0.95, 1.0]}], "source_type": "literature",
                           "evidence": [{"citation": "T", "location": "p. 3"}], "confidence": "low"},
                          {"id": "C2", "mechanism": "B halves the risk ratio", "about": {"package": "B"},
                           "direction": "reduces", "magnitude": {"band": [0.9, 1.0], "scale": "odds_ratio"},
                           "source_type": "literature", "evidence": [{"citation": "T", "location": "p. 3"}],
                           "confidence": "low"}]}
        wm, _ = build(sd, cs)
        ok(not any(b.get("conflicts") for b in wm["beliefs"] if b["id"] in ("C1", "C2")),
           "WM32 conflicts compare matching conditions and scales only (a conditional band, an odds-ratio band)")
    tryit("WM32 conflicts", wm32)

    def wm34():
        x = np.linspace(0, 1, 100)
        cs = _sw({"x": x}, 0.2 + 0 * x, 0.3 + 0 * x, cA=1.0, cB=2.0, lam=0.05)
        st, res = _one(cs, {"kind": "paired_sign", "a": "A", "b": "B", "feature": "x", "range": [0, 1],
                            "sign": "positive", "min_share": 0.8})
        ok(st == "refuted", f"WM34 options are compared on net benefit at the shadow price (A has the higher benefit "
                            f"per cost, B the higher net benefit): {st} {res['observed']}")
        st2, _ = _one(_sw({"x": x}, 0.2 + 0 * x, 0.3 + 0 * x, lam=None),
                      {"kind": "paired_sign", "a": "A", "b": "B", "feature": "x", "range": [0, 1], "sign": "positive",
                       "min_share": 0.8})
        ok(st2 == "untestable", f"WM34b without a shadow price a paired test is untestable: {st2}")
    tryit("WM34 net benefit", wm34)

    def wm35():
        rng = np.random.default_rng(11)
        n = 200
        feats = {f"n{k:03d}": rng.uniform(0, 1, n) for k in range(300)}
        null = _sw(feats, 0.3 + 0.02 * rng.standard_normal(n), 0.3 + 0.02 * rng.standard_normal(n))
        s = summarise(null)
        dirs = [(p, f) for p, e in s["effects"].items() for f, v in e["direction"].items()
                if v["direction"] in ("increases", "decreases")]
        fl, mu = s["flips"]["A~B"]["along"], s.get("multiplicity") or {}
        ok(not dirs and not fl and mu.get("tested", 0) >= 600, "WM35 a null sweep with 300 irrelevant features gives "
           f"no direction and no flip after FDR control: {len(dirs)} directions, {len(fl)} flips, {mu}")
        wm, _ = build({"beliefs": _seed()["beliefs"][:1]}, sw)
        sims = [b for b in wm["beliefs"] if b["id"].startswith("sim.")]
        ok(sims and all(b.get("screened") is True and b.get("scope") == "marginal" for b in sims),
           f"WM35b simulation beliefs come only from screened results and say they are marginal: {sims[:1]}")
    tryit("WM35 multiplicity", wm35)

    def wm36():
        rng = np.random.default_rng(12)
        x = rng.uniform(0, 1, 400)
        z = x + 0.05 * rng.standard_normal(400)
        cs = _sw({"x": x, "z": z}, 0.3 + 0 * x, 0.5 + 0.1 * x - 0.3 * z)
        dx = summarise(cs)["effects"]["B"]["direction"]["x"]
        ok(dx.get("scope") == "marginal" and "z" in (dx.get("correlated_with") or []),
           f"WM36 the summary labels a direction a marginal association and names correlated design features: {dx}")
        st, res = _one(cs, {"kind": "effect_direction", "package": "B", "feature": "x", "direction": "increases"})
        ok(st == "inconclusive" and "z" in json.dumps(res["observed"]), "WM36b a marginal association never refutes a "
           f"conditional claim on a feature correlated with another (true: increases with x at fixed z): {st} "
           f"{res['observed']}")
    tryit("WM36 marginal vs conditional", wm36)

    def wm37():
        rng = np.random.default_rng(1)
        units = {str(i): {"f1": float(x), "f2": 0.5} for i, x in enumerate(rng.uniform(0, 1, 40))}
        plan = {u: ("A" if r["f1"] < 0.5 else "B") for u, r in units.items()}
        wm1, hy1 = build(_seed(), sw, plan=plan, units=units)
        wm2, hy2 = build(wm1, sw, plan=plan, units=units, hypotheses=hy1["hypotheses"])
        ids = [b["id"] for b in wm2["beliefs"]]
        ok(len(ids) == len(set(ids)) and wm2.get("version") == 2 and wm2.get("superseded_simulation"),
           f"WM37 rebuilding from a produced world model replaces its simulation beliefs (versioned): {ids}")
        conf = lambda w: {b["id"]: b["confidence"] for b in w["beliefs"] if not b["id"].startswith("sim.")}  # noqa
        ok(wm2["track_record"]["confirmed"] == wm1["track_record"]["confirmed"] and conf(wm2) == conf(wm1) and
           all(h["confrontation"]["repeat"] for h in hy2["hypotheses"]),
           f"WM38 replaying the same evidence counts once and moves no confidence: {wm2['track_record']}")
        wm3, _ = build(wm2, sw, plan=plan, units=units, hypotheses=hy2["hypotheses"])
        ok(len({b["id"] for b in wm3["beliefs"]}) == len(wm3["beliefs"]), "WM37b the output validates on reuse")
    tryit("WM37-38 reuse and replay", wm37)

    def wm39():
        rng = np.random.default_rng(1)
        units = {str(i): {"f1": float(x), "f2": 0.5} for i, x in enumerate(rng.uniform(0, 1, 40))}
        plan = {u: ("A" if r["f1"] < 0.5 else "B") for u, r in units.items()}
        sd = _seed()
        sd["hypotheses"].append(dict(sd["hypotheses"][0], id="H1b"))
        wa, _ = build(sd, sw, plan=plan, units=units)
        ok(wa["track_record"]["confirmed"] == 4, f"WM39 an alias (identical test, same evidence) counts once: "
                                                 f"{wa['track_record']['confirmed']}")
        sd = _seed()
        sd["track_record"] = {"confirmed": 99, "contradicted": 0, "history": []}
        wr, _ = build(sd, sw, plan=plan, units=units)
        ok(wr["track_record"]["confirmed"] == 4 and wr["track_record"].get("reconciled"),
           f"WM40 seed counters are recomputed from the confrontations, not trusted: {wr['track_record']}")
        sd = _seed()
        sd["hypotheses"][0]["registered_at"] = "2026-02-01T00:00:00+00:00"
        sd["hypotheses"][3].pop("registered_at")
        wp, hp = build(sd, sw, plan=plan, units=units)
        tr = wp["track_record"]
        ok(tr["confirmed"] == 2 and (tr.get("post_hoc") or {}).get("confirmed") == 2 and
           [h["confrontation"]["post_hoc"] for h in hp["hypotheses"]] == [True, False, False, True, False],
           f"WM41 a hypothesis registered after its evidence, or with no registration time, is post hoc: reported "
           f"separately, never in the share confirmed: {tr}")
        b1 = next(b for b in wp["beliefs"] if b["id"] == "B1")
        ok(b1["confidence"] == "medium", f"WM41b a post-hoc confirmation does not raise confidence: {b1['confidence']}")
        sd = _seed()
        sd["beliefs"][0]["id"] = "sim.effect.A"
        sd["hypotheses"][0]["belief"] = "sim.effect.A"
        try:
            validate_seed(sd)
            ok(False, "WM42 a hand-written belief may not use the reserved sim. prefix")
        except WorldModelError:
            ok(True, "WM42 a hand-written belief may not use the reserved sim. prefix")
    tryit("WM39-42 track record and pre-registration", wm39)

    def wm43():
        import subprocess
        import yaml
        ident = {"version": "0.11", "code_sha256": "c" * 64}
        with tempfile.TemporaryDirectory() as d:
            put = lambda i: yaml.safe_dump({"engine_identity": i}, open(os.path.join(d, "results.yaml"), "w"))  # noqa
            vcs = lambda *a: subprocess.run(["git", "-C", d, *a], capture_output=True, text=True)  # noqa: E731
            vcs("init", "-q")
            vcs("config", "user.email", "t@t")
            vcs("config", "user.name", "t")
            put(ident)
            ok(benchmark_record(d, ident)["matched"] == "working tree", "WM43 an identity in the benchmark file")
            vcs("add", "results.yaml")
            vcs("commit", "-qm", "v0.11")
            put({"version": "0.12", "code_sha256": "d" * 64})
            vcs("commit", "-qam", "v0.12")
            r = benchmark_record(d, ident)
            ok(r["matched"] == "git history" and len(r.get("commit", "")) == 40, f"WM43b an identity found only in "
                                                                                 f"an ancestor commit: {r}")
            vcs("checkout", "-q", "-b", "side")
            put({"version": "0.13", "code_sha256": "e" * 64})
            vcs("commit", "-qam", "side")
            vcs("checkout", "-q", "-")
            for i, why in (({"version": "0.13", "code_sha256": "e" * 64}, "only on a branch that is not an ancestor"),
                           ({"version": "0.9", "code_sha256": "f" * 64}, "nowhere")):
                try:
                    benchmark_record(d, i)
                    ok(False, f"WM43c a recorded identity {why} is refused")
                except WorldModelError:
                    ok(True, f"WM43c a recorded identity {why} is refused")
    tryit("WM43 recorded engine identity", wm43)
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    if not sys.argv[1:]:
        print(__doc__)
        sys.exit(2)
    try:
        sys.exit(_cli(sys.argv[1:]))
    except WorldModelError as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        sys.exit(1)
