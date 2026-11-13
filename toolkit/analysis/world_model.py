#!/usr/bin/env python3
"""The world model (docs/design/v2-progressive-steps.md §4, v0): the modeller's working understanding of how the
decision works, as beliefs that papers, data and simulations confront. It is meant to be mostly right, not precise:
its numbers are bands and rough locations, never decision numbers (those come from the analysis' claims).

    python3 toolkit/analysis/world_model.py validate <seed.yaml>
    python3 toolkit/analysis/world_model.py build <run_dir> --seed <seed.yaml> [--out DIR]
    python3 toolkit/analysis/world_model.py --self-test

A person or a bounded reasoning task (a later PR) writes the seed; this module validates it, summarises a run's
engine sweep coarsely, checks each hypothesis mechanically and says where to look next. No LLM calls.

Seed (YAML), and the beliefs of analysis/world_model.yaml:

    beliefs:
      - id: B1
        mechanism: "PBO restores net killing where pyrethroid resistance is high"
        about: {package: net_pbo, feature: resistance}       # or {pair: [a, b]}: what the belief is about
        direction: reduces | increases | decreases | none | flips | raises
        magnitude: {band: [lo, hi], scale: relative_reduction}   # optional: a band, never a point value
        conditions: [{feature: resistance, range: [lo, hi]}]     # optional: where it holds or where it flips
        source_type: literature | data | simulation
        evidence:                 # literature: citation + location (table or page); data: dossier_entry;
          - {citation: "...", location: "Table 2"}       # simulation: artifact + point ids
        confidence: low | medium | high
        decision_relevant: true   # optional
        conflicts: [{belief: B7, note: "..."}]   # conflicting evidence, side by side, never averaged
    hypotheses:                   # analysis/hypotheses.yaml: testable expectations with a refutation test
      - id: H1
        belief: B1
        claim: "..."
        test: {kind: effect_band, package: P, band: [lo, hi], where: {feature: [lo, hi]}}
              # effect_direction {package, feature, direction: increases|decreases|none}
              # paired_sign {a, b, feature, range, sign: positive|negative}  (sign of a's minus b's benefit per cost)
              # flip_in {a, b, feature, range}                   (the a/b choice flips inside range)
              # plan_share {component, where, min_share}         (share of matching units whose plan has it)
        status: open | supported | refuted | untestable          (written by check())
    open_questions: [{id, question, beliefs: [...], decision_relevant}]
    track_record: {confirmed, contradicted, open, untestable, history: [{hypothesis, belief, status}]}

Fail closed: a belief without evidence (or literature without its table or page), a hypothesis without a
refutation test, an unknown test kind, a missing run artifact or result row. Names come from the spec.
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
DIRECTIONS = ("reduces", "increases", "decreases", "none", "flips", "raises")
STATUSES = ("open", "supported", "refuted", "untestable")
TESTS = {"effect_band": ("package", "band"), "effect_direction": ("package", "feature", "direction"),
         "paired_sign": ("a", "b", "feature", "range", "sign"), "flip_in": ("a", "b", "feature", "range"),
         "plan_share": ("component", "where", "min_share")}
# The coarse summary's settings (recorded in the output): effect bands are the 10th-90th percentiles over the sweep;
# a direction needs |Spearman rho| above max(0.1, 3/sqrt(n-1)); a flip needs both options preferred at >= 5% of points
# and a one-feature logistic fit of the sign gaining >= 6.63 in deviance (chi-square 1 df, p 0.01); its range is where
# the fitted share is 25-75%, at least span/sqrt(n) on each side (the sweep's resolution); at most 3 features per pair.
SETTINGS = {"band_quantiles": [0.1, 0.9], "rho_min": 0.1, "rho_z": 3.0, "flip_min_share": 0.05,
            "flip_deviance_min": 6.63, "flips_max": 3, "sign_share": 0.8, "min_points": 5, "digits": 3}


class WorldModelError(ValueError):
    """An input the world model cannot use: it fails closed with what is missing or inconsistent."""


class Sweep:
    """Per-capita sweep tables at the design points: features {name: n values}, packages {name: [components]},
    the comparator, its burden (primary outcome per person), y {package: outcome per person}, cost {package: $ per
    person}, component groups, point ids and the artifacts they came from."""

    def __init__(self, features, packages, comparator, burden, y, cost, groups=None, points=None, kinds=None,
                 artifacts=None):
        self.features = {k: np.asarray(v, float) for k, v in features.items()}
        self.packages, self.comparator = {k: sorted(v) for k, v in packages.items()}, comparator
        self.burden = np.asarray(burden, float)
        self.y = {k: np.asarray(v, float) for k, v in y.items()}
        self.cost = {k: np.asarray(v, float) for k, v in cost.items()}
        self.groups, self.kinds, self.artifacts = groups or {}, kinds or {}, artifacts or {}
        self.points = list(points if points is not None else range(len(self.burden)))
        for k in packages:
            if k not in self.y or k not in self.cost:
                raise WorldModelError(f"sweep: package {k!r} has no outcome or cost")

    def reduction(self, p):
        return 1 - self.y[p] / np.maximum(self.burden, 1e-12)

    def per_cost(self, p):
        """Benefit per cost: burden x relative reduction / incremental cost, per person (nan where it costs nothing)."""
        c = self.cost[p] - self.cost[self.comparator]
        return np.where(c > 0, (self.burden - self.y[p]) / np.where(c > 0, c, 1.0), np.nan)


def _r(x):
    return None if x is None or not math.isfinite(float(x)) else float(f"{float(x):.{SETTINGS['digits']}g}")


def _band_ok(b):
    return (isinstance(b, (list, tuple)) and len(b) == 2 and all(isinstance(v, (int, float)) and not isinstance(v, bool)
                                                                 for v in b) and b[0] <= b[1])


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


def validate_seed(seed) -> dict:
    if not isinstance(seed, dict):
        raise WorldModelError("world-model seed: a mapping {beliefs, hypotheses, open_questions}")
    errs, ids, hids = [], set(), set()
    bs, hs = seed.get("beliefs"), seed.get("hypotheses") or []
    if not isinstance(bs, list) or not bs:
        errs.append("beliefs: a non-empty list")
    for k, b in enumerate(bs if isinstance(bs, list) else []):
        w = f"beliefs[{k}]"
        if not isinstance(b, dict):
            errs.append(f"{w}: a mapping")
            continue
        bid = b.get("id")
        if not bid or bid in ids:
            errs.append(f"{w}: a unique id")
        ids.add(bid)
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
            errs.append(f"{w}: magnitude.band [lo, hi] (a band, not a point value)")
        for c in b.get("conditions") or []:
            if not (isinstance(c, dict) and c.get("feature") and _band_ok(c.get("range"))):
                errs.append(f"{w}: each condition {{feature, range: [lo, hi]}}")
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
            errs.append(f"{w}: test {t['kind']} needs {miss}")
        for f in ("band", "range"):
            if f in t and not _band_ok(t[f]):
                errs.append(f"{w}: test {f} [lo, hi]")
        if t["kind"] == "paired_sign" and t.get("sign") not in ("positive", "negative"):
            errs.append(f"{w}: sign positive or negative")
        if t["kind"] == "effect_direction" and t.get("direction") not in ("increases", "decreases", "none"):
            errs.append(f"{w}: direction increases, decreases or none")
        if t["kind"] == "plan_share" and not isinstance(t.get("where"), dict):
            errs.append(f"{w}: where {{feature or column: [lo, hi] or [values]}}")
    if errs:
        raise WorldModelError("world-model seed refused: " + "; ".join(errs))
    return seed


# ---------------------------------------------------------------- coarse summary of a sweep
def _rho(x, y):
    m = np.isfinite(x) & np.isfinite(y)
    if m.sum() < SETTINGS["min_points"]:
        return float("nan"), int(m.sum())
    rx, ry = np.argsort(np.argsort(x[m])), np.argsort(np.argsort(y[m]))
    if rx.std() == 0 or ry.std() == 0:
        return 0.0, int(m.sum())
    return float(np.corrcoef(rx, ry)[0, 1]), int(m.sum())


def _direction(x, y):
    rho, n = _rho(x, y)
    sig = math.isfinite(rho) and abs(rho) > max(SETTINGS["rho_min"], SETTINGS["rho_z"] / math.sqrt(max(n - 1, 1)))
    return {"direction": ("increases" if rho > 0 else "decreases") if sig else "none", "rho": _r(rho)}


def _logistic(x, s, lam=1e-2):
    """One-feature logistic fit of a 0/1 sign (Newton, a small ridge on the slope so separation stays finite):
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
    b1 = w[1] / sd
    return 2 * (ll - ll0), (mu - w[0] * sd / w[1]) if w[1] != 0 else float("nan"), b1


def flip(sw, a, b):
    """Roughly where the choice between a and b flips: the sign of a's minus b's benefit per cost at each point, and
    for each feature a one-feature fit of that sign (marginal: design features may be correlated)."""
    d = sw.per_cost(a) - sw.per_cost(b)
    m = np.isfinite(d)
    s = (d[m] > 0).astype(float)
    out = {"a": a, "b": b, "measure": "benefit_per_cost", "n": int(m.sum()),
           "share_first_preferred": _r(s.mean()) if m.any() else None, "along": []}
    if m.sum() < SETTINGS["min_points"] or min(s.mean(), 1 - s.mean()) < SETTINGS["flip_min_share"]:
        return out
    for f, x in sw.features.items():
        x = x[m]
        if x.std() == 0:
            continue
        dev, t, b1 = _logistic(x, s)
        lo, hi = float(x.min()), float(x.max())
        if not (dev >= SETTINGS["flip_deviance_min"] and math.isfinite(t) and lo <= t <= hi):
            continue
        h = max(math.log(3) / abs(b1), (hi - lo) / math.sqrt(len(x)))
        out["along"].append({"feature": f, "at": _r(t), "range": [_r(max(lo, t - h)), _r(min(hi, t + h))],
                             "first_preferred_below": bool(b1 < 0), "deviance_gain": _r(dev)})
    out["along"] = sorted(out["along"], key=lambda r: (-r["deviance_gain"], r["feature"]))[:SETTINGS["flips_max"]]
    return out


def default_pairs(sw):
    """Competing options: single-component packages whose components share a group (one net type or another)."""
    single = {k: v[0] for k, v in sw.packages.items() if len(v) == 1}
    ks = sorted(single)
    return [(a, b) for i, a in enumerate(ks) for b in ks[i + 1:]
            if sw.groups.get(single[a]) and sw.groups.get(single[a]) == sw.groups.get(single[b])]


def summarise(sw, pairs=None, packages=None):
    """Direction of each package's relative effect along each feature, its rough size band, and roughly where each
    competing pair's choice flips."""
    pairs = sorted(set(map(tuple, pairs or [])) | set(default_pairs(sw)))
    pk = sorted({k for k, v in sw.packages.items() if len(v) == 1} | set(packages or []) |
                {x for p in pairs for x in p} - {sw.comparator})
    qlo, qhi = SETTINGS["band_quantiles"]
    eff = {}
    for p in pk:
        r = sw.reduction(p)
        eff[p] = {"median": _r(np.median(r)), "band": [_r(np.quantile(r, qlo)), _r(np.quantile(r, qhi))],
                  "direction": {f: _direction(x, r) for f, x in sorted(sw.features.items())}}
    return {"comparator": sw.comparator, "n_points": len(sw.points), "settings": dict(SETTINGS),
            "effects": eff, "flips": {f"{a}~{b}": flip(sw, a, b) for a, b in pairs},
            "domain": {f: [_r(x.min()), _r(x.max())] for f, x in sorted(sw.features.items())}}


# ---------------------------------------------------------------- check(): mechanical refutation tests
def _region(sw, where):
    m = np.ones(len(sw.points), bool)
    for f, (lo, hi) in (where or {}).items():
        if f not in sw.features:
            raise WorldModelError(f"test region: unknown feature {f!r} (sweep features: {sorted(sw.features)})")
        m &= (sw.features[f] >= lo) & (sw.features[f] <= hi)
    return m


def _match(row, where):
    for f, v in where.items():
        x = row.get(f)
        if x is None:
            return None
        if _band_ok(v) and not isinstance(x, str):
            if not (v[0] <= float(x) <= v[1]):
                return False
        elif str(x) not in {str(e) for e in (v if isinstance(v, list) else [v])}:
            return False
    return True


def _test(t, ctx):
    """(status, observed, artifact, point ids) for one refutation test."""
    sw, summ, k = ctx["sweep"], ctx["summary"], t["kind"]
    art = sw.artifacts.get("results", "analysis/batch/results.jsonl")
    for p in [t.get(x) for x in ("package", "a", "b") if t.get(x)]:
        if p not in sw.packages:
            raise WorldModelError(f"test {k}: unknown package {p!r}")
    if k == "plan_share":
        plan, units = ctx.get("plan"), ctx.get("units")
        if not plan:
            return "untestable", {"reason": "no optimised plan (analysis/point.yaml)"}, None, []
        hit = [u for u, row in sorted(units.items()) if _match(row, t["where"]) and u in plan]
        if not hit:
            return "untestable", {"reason": f"no unit matches {t['where']}"}, "analysis/point.yaml", []
        c = t["component"]
        share = sum(c in sw.packages.get(plan[u], []) or plan[u] == c for u in hit) / len(hit)
        st = "supported" if share >= float(t["min_share"]) else "refuted"
        return st, {"share": _r(share), "units": len(hit)}, "analysis/point.yaml", []
    m = _region(sw, t.get("where") or ({t["feature"]: t["range"]} if k in ("paired_sign", "flip_in") else {}))
    ids = [p for p, x in zip(sw.points, m) if x]
    if m.sum() < SETTINGS["min_points"]:
        return "untestable", {"reason": f"{int(m.sum())} sweep points in the region"}, art, ids
    if k == "effect_band":
        med = float(np.median(sw.reduction(t["package"])[m]))
        return ("supported" if t["band"][0] <= med <= t["band"][1] else "refuted"), {"median": _r(med)}, art, ids
    if k == "effect_direction":
        r = _direction(sw.features[t["feature"]][m], sw.reduction(t["package"])[m])
        if r["direction"] == t["direction"]:
            return "supported", r, art, ids
        return ("open" if r["direction"] == "none" else "refuted"), r, art, ids
    d = (sw.per_cost(t["a"]) - sw.per_cost(t["b"]))[m]
    d = d[np.isfinite(d)]
    share = float((d > 0).mean()) if len(d) else float("nan")
    if k == "paired_sign":
        want = share if t["sign"] == "positive" else 1 - share
        cut = SETTINGS["sign_share"]
        st = "supported" if want >= cut else "refuted" if want <= 1 - cut else "open"
        return st, {"share_first_preferred": _r(share)}, art, ids
    fl = summ["flips"].get(f"{t['a']}~{t['b']}") or flip(sw, t["a"], t["b"])
    al = next((x for x in fl["along"] if x["feature"] == t["feature"]), None)
    if al is not None:
        inside = al["range"][0] <= t["range"][1] and al["range"][1] >= t["range"][0]
        return ("supported" if inside else "refuted"), {"flip_range": al["range"]}, art, ids
    one_sided = max(share, 1 - share) >= SETTINGS["sign_share"]
    return ("refuted" if one_sided else "open"), {"share_first_preferred": _r(share), "flip": None}, art, ids


def check(hyps, ctx):
    """Evaluate each hypothesis' refutation test against the run's artifacts; returns the hypotheses with status and
    result (observed value, artifact, point ids)."""
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
        along = {f: v["direction"] for f, v in e["direction"].items() if v["direction"] != "none"}
        out.append({"id": f"sim.effect.{p}", "about": {"package": p},
                    "mechanism": f"{p} changes the primary outcome against {sw.comparator}; relative reduction "
                                 f"{e['band'][0]} to {e['band'][1]} across the sweep" +
                                 (f", {', '.join(f'{v} with {f}' for f, v in along.items())}" if along else ""),
                    "direction": "reduces" if (e["median"] or 0) >= 0 else "raises", "along": along,
                    "magnitude": {"band": e["band"], "scale": "relative_reduction"}, "source_type": "simulation",
                    "evidence": ev, "confidence": "medium"})
    for key, fl in summ["flips"].items():
        if fl["along"]:
            top = fl["along"][0]
            out.append({"id": f"sim.flip.{key}", "about": {"pair": [fl["a"], fl["b"]]}, "direction": "flips",
                        "mechanism": f"{fl['a']} is preferred over {fl['b']} on benefit per cost "
                                     f"{'below' if top['first_preferred_below'] else 'above'} roughly "
                                     f"{top['feature']} {top['range'][0]} to {top['range'][1]}",
                        "conditions": [{"feature": x["feature"], "range": x["range"]} for x in fl["along"]],
                        "source_type": "simulation", "evidence": ev, "confidence": "medium",
                        "decision_relevant": True})
    return out


def _disjoint(a, b):
    return a[1] < b[0] or b[1] < a[0]


def _conflicts(beliefs):
    """Seed beliefs against simulation beliefs on the same package or pair: disjoint bands, opposite directions, or
    disjoint flip ranges along one feature are recorded on both, side by side."""
    key_of = lambda ab: json.dumps({"package": ab["package"]} if "package" in ab else  # noqa: E731
                                   {"pair": sorted(ab.get("pair") or [])}, sort_keys=True)
    sims = {key_of(b["about"]): b for b in beliefs if b["id"].startswith("sim.")}
    for b in [x for x in beliefs if not x["id"].startswith("sim.")]:
        ab = b["about"]
        s = sims.get(key_of(ab))
        if s is None:
            continue
        why = []
        bb, sb = (b.get("magnitude") or {}).get("band"), (s.get("magnitude") or {}).get("band")
        if bb and sb and _disjoint(bb, sb):
            why.append(f"magnitude band {bb} against simulated {sb}")
        sd = (s.get("along") or {}).get(ab.get("feature"))
        if b["direction"] in ("increases", "decreases") and sd and sd != b["direction"]:
            why.append(f"{b['direction']} with {ab['feature']} against simulated {sd}")
        sc = {c["feature"]: c["range"] for c in s.get("conditions") or []}
        for c in b.get("conditions") or []:
            if c["feature"] in sc and _disjoint(c["range"], sc[c["feature"]]):
                why.append(f"{c['feature']} range {c['range']} against simulated {sc[c['feature']]}")
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
    summ = summarise(sw, pairs, [h["test"]["package"] for h in hyps if h["test"].get("package")])
    beliefs = seed["beliefs"] + _sim_beliefs(sw, summ)
    _conflicts(beliefs)
    checked = check(hyps, {"sweep": sw, "summary": summ, "plan": plan, "units": units or {}})
    tr = copy.deepcopy(seed.get("track_record") or {"confirmed": 0, "contradicted": 0, "open": 0, "untestable": 0,
                                                    "history": []})
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
          "structure": {"comparator": sw.comparator, "terms": "benefit per person = baseline burden x relative "
                        "reduction of a package against the comparator; benefit per cost = burden x reduction / "
                        "incremental cost per person", "packages": sorted(sw.packages)},
          "note": "a working understanding, meant to be mostly right; its bands are never decision numbers",
          "beliefs": beliefs, "open_questions": qs, "track_record": tr, "simulation_summary": summ}
    wm["where_to_look"] = where_to_look(wm)
    return wm, {"schema": HSCHEMA, "producer": PRODUCER, "produced_by": "harness", "hypotheses": checked}


WEAK = {"low": 1.0, "medium": 0.5, "high": 0.2}


def where_to_look(wm, top=10):
    """Beliefs that matter to the decision and are weakly held, conflicting or contradicted, each with the
    simulation region or evidence that would most change it, and why."""
    flips = {tuple(sorted((f["a"], f["b"]))): f for f in wm["simulation_summary"]["flips"].values()}
    out = []
    for b in wm["beliefs"]:
        rel = bool(b.get("decision_relevant")) or "pair" in b["about"]
        weak = WEAK[b["confidence"]] + (1.0 if b.get("conflicts") else 0) + (1.0 if b.get("contradicted_by") else 0)
        score = _r(weak * (2.0 if rel else 0.5))
        pair = b["about"].get("pair")
        fl = flips.get(tuple(sorted(pair))) if pair else None
        region = {x["feature"]: x["range"] for x in (fl or {}).get("along", [])[:1]} or \
            {c["feature"]: c["range"] for c in b.get("conditions") or []}
        if region:
            out.append({"belief": b["id"], "score": score, "kind": "simulation", "region": region,
                        "reason": f"{b['mechanism']}: located only roughly; sweep points across {region} decide on "
                                  "which side units fall" + (" and test the contradiction" if b.get("contradicted_by")
                                                             else "")})
        if b.get("conflicts") or (b["source_type"] != "simulation" and b["confidence"] == "low"):
            what = "; ".join(c["note"] for c in b.get("conflicts") or []) or f"{b['source_type']} evidence is weak"
            out.append({"belief": b["id"], "score": score, "kind": "evidence",
                        "reason": f"{b['mechanism']}: {what}; look for a trial, survey or engine benchmark on "
                                  f"{b['about']} that would settle it"})
    return sorted(out, key=lambda r: (-(r["score"] or 0), r["belief"], r["kind"]))[:top]


# ---------------------------------------------------------------- reading a run (offline)
def load_sweep(run_dir):
    """The per-capita sweep tables of a run's engine batch, from analysis/batch/design.json, results.jsonl and the
    signed spec (packages, populations, cost items). Fails closed on a missing file, a missing result row, a package
    list that differs from the spec's, or rows from more than one engine identity."""
    from toolkit.alloc import costs as acosts
    from toolkit.analysis import features as FT
    from toolkit.analysis.artifacts import sha256
    from toolkit.analysis.model import _driver, _group
    from toolkit.spec import schema
    need = {"spec": "decision_spec.yaml", "design": "analysis/batch/design.json",
            "results": "analysis/batch/results.jsonl"}
    miss = [rel for rel in need.values() if not os.path.isfile(os.path.join(run_dir, rel))]
    if miss:
        raise WorldModelError(f"world model: missing run artifacts {miss} (the sweep cannot be read)")
    spec = schema.load(os.path.join(run_dir, need["spec"]))
    design = json.load(open(os.path.join(run_dir, need["design"])))
    names, pk = design.get("packages"), spec.packages()
    if names != list(pk):
        raise WorldModelError(f"design packages {names} differ from the spec's {list(pk)}")
    rows, idents = {}, set()
    with open(os.path.join(run_dir, need["results"])) as f:
        for line in f:
            r = json.loads(line) if line.strip() else {}
            if r.get("ok") and isinstance(r.get("i"), int):
                rows[r["i"]] = r["result"]["result"]
                idents.add(json.dumps((r["result"].get("provenance") or {}).get("engine"), sort_keys=True))
    P, pts = len(names), design["points"]
    gaps = [f"{p['point']}|{k}" for n, p in enumerate(pts) for j, k in enumerate(names) if n * P + j not in rows]
    if gaps or not pts:
        raise WorldModelError(f"{len(gaps)} engine results missing (e.g. {gaps[:3]}) or no design points")
    if len(idents) != 1:
        raise WorldModelError(f"result rows come from {len(idents)} engine identities: one sweep needs one engine")
    by_id = {str(u[spec.uid]): u for u in spec.units()}
    pops, o = spec["decision_units"]["populations"], spec.outcomes()[0]
    co = (spec["analysis"].get("cost_outputs") or {})
    alloc, attr = spec.alloc_spec(), spec["calibration"].get("attribute")
    feats = [FT.name(x) for x in FT.declared(spec.raw)]
    pnames = list(design.get("fitted_names") or []) + list(design.get("prior_names") or []) + \
        list(design.get("spec_value_names") or [])
    F = {n_: [] for n_ in ([f"{attr} (calibration scale)"] if attr else []) + feats + pnames}
    y, cost = {k: [] for k in names}, {k: [] for k in names}
    burden = []
    for n, p in enumerate(pts):
        u = by_id.get(str(p["unit_id"]))
        if u is None:
            raise WorldModelError(f"design point {p['point']}: unit {p['unit_id']} is not in the spec's units")
        tot = sum(float(u[pm["column"]]) for pm in pops.values())
        res = {k: rows[n * P + j] for j, k in enumerate(names)}
        for k in names:
            y[k].append(sum(_group(res[k], o, pm) * float(u[pm["column"]]) for pm in pops.values()) / tot)
        cm = acosts.cost_matrix(alloc, [u], outcome=lambda _u, k: {
            nm: _driver(res[k], m["cost_driver"], f"design point {p['point']}, {k}") * tot for nm, m in co.items()})
        for k in names:
            cost[k].append(cm[str(u[spec.uid])][k]["total"] / tot)
        burden.append(y[spec.comparator][-1])
        vals = ([p["z"]] if attr else []) + FT.vector(spec, u) + [float(p["theta"][nm]) for nm in pnames]
        for n_, v in zip(F, vals):
            F[n_].append(float(v))
    groups = {c: (d or {}).get("group") for c, d in spec["options"]["components"].items() if (d or {}).get("group")}
    kinds = {n_: "parameter" if n_ in pnames else "unit" for n_ in F}
    arts = {rel: sha256(os.path.join(run_dir, rel)) for rel in need.values()}
    sw = Sweep(F, pk, spec.comparator, burden, y, cost, groups, [p["point"] for p in pts], kinds,
               dict(arts, results=need["results"]))
    return sw, spec


def load_plan(run_dir, spec, sw):
    """(plan, units) from analysis/point.yaml and the spec's units (columns, features, calibrated value), or
    (None, None) when the run has no optimised plan."""
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
    a = ap.parse_args(argv)
    seed = yaml.safe_load(open(a.seed))
    if a.cmd == "validate":
        validate_seed(seed)
        print(f"OK: {len(seed['beliefs'])} beliefs, {len(seed.get('hypotheses') or [])} hypotheses")
        return 0
    from toolkit.analysis.artifacts import sha256
    sw, spec = load_sweep(a.run_dir)
    plan, units = load_plan(a.run_dir, spec, sw)
    inputs = dict({k: v for k, v in sw.artifacts.items() if k != "results"}, seed=sha256(a.seed))
    if plan is not None:
        inputs["analysis/point.yaml"] = sha256(os.path.join(a.run_dir, "analysis", "point.yaml"))
    wm, hy = build(seed, sw, plan, units, inputs)
    out = a.out or os.path.join(a.run_dir, "analysis")
    os.makedirs(out, exist_ok=True)
    for name, doc in (("world_model.yaml", wm), ("hypotheses.yaml", hy)):
        with open(os.path.join(out, name), "w") as f:
            yaml.safe_dump(json.loads(json.dumps(doc)), f, sort_keys=False, width=110)
    st = {}
    for h in hy["hypotheses"]:
        st[h["status"]] = st.get(h["status"], 0) + 1
    print(f"wrote {out}/world_model.yaml and hypotheses.yaml: {len(wm['beliefs'])} beliefs, hypotheses {st}")
    return 0


# ---------------------------------------------------------------- self-test
def _planted(seed=3, n=200):
    """A planted-truth sweep: A's relative effect is constant (0.30); B's rises with f1 (0.6 x f1); f2 is irrelevant;
    equal costs, so A beats B on benefit per cost exactly where f1 < 0.5."""
    rng = np.random.default_rng(seed)
    f1, f2 = rng.uniform(0, 1, n), rng.uniform(0, 1, n)
    burden = 1.0 + 0.1 * rng.standard_normal(n)
    rA = 0.30 + 0.02 * rng.standard_normal(n)
    rB = 0.60 * f1 + 0.03 * rng.standard_normal(n)
    one = np.ones(n)
    return Sweep({"f1": f1, "f2": f2}, {"none": [], "A": ["a"], "B": ["b"]}, "none", burden,
                 {"none": burden, "A": burden * (1 - rA), "B": burden * (1 - rB)},
                 {"none": 0 * one, "A": one, "B": one}, groups={"a": "g", "b": "g"}, points=list(range(n)),
                 kinds={"f1": "unit", "f2": "unit"}, artifacts={"results": "synthetic"})


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
         "decision_relevant": True}],
        "hypotheses": [
            {"id": "H1", "belief": "B1", "claim": "A's effect is in [0.2, 0.4]",
             "test": {"kind": "effect_band", "package": "A", "band": [0.2, 0.4]}},
            {"id": "H2", "belief": "B3", "claim": "A beats B wherever f1 is above 0.7",
             "test": {"kind": "paired_sign", "a": "A", "b": "B", "feature": "f1", "range": [0.7, 1.0],
                      "sign": "positive"}},
            {"id": "H3", "belief": "B3", "claim": "the A/B choice flips for f1 in [0.3, 0.7]",
             "test": {"kind": "flip_in", "a": "A", "b": "B", "feature": "f1", "range": [0.3, 0.7]}},
            {"id": "H4", "belief": "B2", "claim": "B's effect increases with f1",
             "test": {"kind": "effect_direction", "package": "B", "feature": "f1", "direction": "increases"}},
            {"id": "H5", "belief": "B3", "claim": "most low-f1 units get A",
             "test": {"kind": "plan_share", "component": "a", "where": {"f1": [0.0, 0.5]}, "min_share": 0.8}}]}


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
        ok(eb["f1"]["direction"] == "increases" and eb["f2"]["direction"] == "none",
           f"WM1 the summariser recovers B's direction along f1 and none along the irrelevant f2: {eb}")
        ea = s["effects"]["A"]
        ok(ea["band"][0] <= 0.30 <= ea["band"][1] and all(v["direction"] == "none" for v in ea["direction"].values()),
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
                         (lambda b: b.update(confidence="certain"), "unknown confidence")):
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
                         (lambda h: h.update(belief="B9"), "an unknown belief")):
            sd = _seed()
            mut(sd["hypotheses"][0])
            try:
                validate_seed(sd)
                ok(False, f"WM7 validation refuses a hypothesis with {why}")
            except WorldModelError:
                ok(True, f"WM7 validation refuses a hypothesis with {why}")
        validate_seed(_seed())
        ok(True, "WM6b a seed whose beliefs all have evidence and whose hypotheses all have tests is accepted")
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
        q = next(o for o in wm["open_questions"] if "H2" in o["question"])
        ok(q["decision_relevant"], f"WM11 a contradiction becomes a decision-relevant open question: {q}")
        hy2 = copy.deepcopy(hy)
        ok(all(h["result"].get("artifact") for h in hy2["hypotheses"]), "WM12 every result names its artifact")
        look = where_to_look(wm)
        ok(look and look[0]["belief"] in ("B2", "B3"), "WM13 where_to_look ranks the low-confidence, decision-relevant,"
           f" contradicted or conflicting belief first: {look[:2]}")
        reg = next((x for x in look if x["kind"] == "simulation" and "f1" in (x.get("region") or {})), None)
        ok(reg is not None and reg["region"]["f1"][0] <= 0.5 <= reg["region"]["f1"][1],
           f"WM14 where_to_look proposes simulation points over the flip range, which contains the true 0.5: {reg}")
        h5 = copy.deepcopy(_seed())
        wm_np, hy_np = build(h5, sw, plan=None, units=None)
        ok(next(h for h in hy_np["hypotheses"] if h["id"] == "H5")["status"] == "untestable",
           "WM15 a plan hypothesis without a plan is untestable, never supported")
    tryit("WM8-15 build, check, where_to_look", wm8)

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
