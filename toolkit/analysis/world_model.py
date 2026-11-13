#!/usr/bin/env python3
"""The world model (docs/design/v2-progressive-steps.md §4, v0): the modeller's working understanding of how the
decision works, as beliefs that papers, data and simulations confront. It is meant to be mostly right, not precise:
its numbers are bands and rough locations, never decision numbers (those come from the analysis' claims). The rule
throughout: absence of evidence, weak evidence or too few usable points is `inconclusive`, never `supported`.

    python3 toolkit/analysis/world_model.py validate <seed.yaml>
    python3 toolkit/analysis/world_model.py register <seed.yaml> <run_dir>      # before the evidence exists
    python3 toolkit/analysis/world_model.py build <run_dir> --seed <seed.yaml or a world_model.yaml> [--out DIR]
        [--hypotheses hypotheses.yaml] [--registry ledger.jsonl] [--engine-identity current|recorded]
        [--shadow-price LAMBDA] [--without-plan] [--anchor-ref <remote>/<branch>]
    python3 toolkit/analysis/world_model.py --self-test

Simulation beliefs are screened: every direction (package x feature) and flip (pair x feature) of the summary is one
test in a Benjamini-Hochberg false-discovery control (q 0.05; the number tested is reported), and only discoveries
become beliefs. They are marginal associations (`scope: marginal`, with the design features each is correlated
with), not mechanisms: a hypothesis claims a conditional mechanism by default (`scope: conditional`), and a feature
correlated (|rho| >= 0.5) with another in the tested points makes its direction or flip test inconclusive, never
supported or refuted. Pre-registered hypotheses are single tests and are not corrected.

Track record (PROVISIONAL: its scores are not to be relied on until the follow-up on evidence identity, evidence
first-availability time and verified history lands). Each confrontation is identified by the sha256 of its canonical
test (defaults resolved, numbers as floats, the belief's conditions applied; unknown test fields are refused) and the
sha256 of its evidence. A replay or an alias counts once and moves no confidence; counters are recomputed from the
history, never taken from the seed (`reconciled` records a difference). Registration is a separate, append-only
ledger (`register`, analysis/hypotheses_registry.jsonl): each line holds a canonical test, its sha256, the time it
was registered, the previous line's sha256 and its own, and the chain is verified on every read (an edited, removed,
reordered or backdated line is refused). The ledger cannot attest itself, and no file on a machine the same actor
controls can prove when a registration happened, so the claim is narrow: a hypothesis is "remote-anchored" only
when its current canonical test sha256 is in a ledger line covered by a ledger commit reachable from a fetched
remote-tracking ref (--anchor-ref refs/remotes/<remote>/<branch>; HEAD, local branches and commit ids are refused)
whose commit time is before its evidence existed, recorded as "anchored in remote git (commit <sha>); the commit
time is the committer's clock". Everything else is "post hoc": reported separately and never in the share
confirmed. A ledger that does not extend every such head (rewritten or truncated) is refused. run_state.json
heads are not used (v2 does not yet hash-protect run_state.json). The track record is not tamper-proof: whoever
controls the machine and can push to the remote can rewrite both.
A produced world model can seed the next build: its simulation beliefs are replaced (`version`,
`superseded_simulation`), literature and data beliefs kept, and hypotheses reloaded with --hypotheses. Ids starting
`sim.` are reserved for generated beliefs. Nothing is wired into the analysis or the stage machine. No LLM calls.

Small samples: below perm_n_max (50) usable points, a direction's p-value is a permutation p-value (20,000
resamples, so the smallest attainable p, about 5e-5, still reaches the first Benjamini-Hochberg threshold of
1,000 tests at q 0.05), and no interval is reported (an equivalence claim is then inconclusive); from 50 points on,
Fisher's z interval and p-value.

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
      - {id: H1, belief: B1, claim: "...", registered_at: "2026-11-12T09:00:00+00:00", test: {kind: ..., scope: ...}}
    open_questions: [{id, question, beliefs: [...], decision_relevant}]

Tests (each declares its pass threshold; a test must concern its belief's package, pair, feature and conditions):
    effect_band {package, band, min_share, where?}  share of usable points whose relative reduction is in the band:
        supported at >= min_share, refuted at <= 1 - min_share; points with no baseline burden are excluded
    effect_direction {package, feature, direction: increases|decreases|negligible, equivalence?, where?}
        tie-aware Spearman rho with its 95% interval: increases / decreases when the interval excludes 0; a
        negligible claim is judged on its bound alone (interval inside +/- equivalence: supported; outside it:
        refuted; crossing it: inconclusive); inputs where one value holds more than half the points are inconclusive
    paired_sign {a, b, feature, range, sign: positive|negative, min_share, where?}  net benefit of a minus b at the
        budget's shadow price; supported when the claimed option wins at >= min_share of usable points, refuted
        when the other one does; ties are indifferent, counted separately
    flip_in {a, b, feature, range, where?}  the a/b transition along the feature, refitted on the `where` points:
        supported when its whole located range lies inside `range`, refuted when disjoint, else inconclusive; a
        one-sided, rare or non-monotone regime is inconclusive, never refuted
    plan_share {component | package, where, min_share}  share of matching units whose point-plan package contains
        the component (its composition), or is the package; a numeric filter reads the column as a number (a value
        that is not one fails closed); a matching unit missing from the plan, or a missing column, is untestable
A belief's conditions are applied to its hypotheses' checks (intersected with the test's own region; for a belief
that a choice flips, its condition on the test's own feature locates the flip and is not applied). Every numeric
region is an exact, finite, ordered pair. Verdicts compare full-precision values; numbers are rounded on output.
Statuses: open (not yet checked), supported, refuted, inconclusive, untestable.

The comparison measure is net benefit per person at the budget's shadow price, dfac x (burden - outcome) - lambda x
(cost - comparator cost): ranking by benefit per cost misranks options of different cost under a budget. lambda is
`--shadow-price`, else the budget row's dual recorded in analysis/point.yaml (solver.budget_dual), or 0 when the plan
records that its budget does not bind (solver.budget_slack: true); otherwise there is none and paired tests are
untestable. No value is inferred from average benefit / cost. A lower lambda favours the costlier option, so a
paired test or flip between options of different cost moves with it (the value used is in every result).
analysis/point.yaml is used only when the run's completed analysis manifest (analysis/manifest.json) lists it, at
its current sha256, together with the current batch (analysis/batch.yaml bound to the current design and results),
and when it is bound to this run: its spec sha256 and engine identity must match, and its
inputs (it must record some) must verify all the way down the artifact graph (point.yaml -> calibration.yaml -> the
units table and the files that artifact records ...), else the build is refused; --without-plan builds without it.
Every data and settings file consumed (the units table, the settings file, the
engine choice) is fingerprinted in the output.

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
         "plan_share": ("where", "min_share")}
OPTIONAL = {"effect_band": ("where",), "effect_direction": ("equivalence", "where", "scope"), "paired_sign": ("where",),
            "flip_in": ("where", "scope"), "plan_share": ("component", "package")}
LEDGER = "analysis/hypotheses_registry.jsonl"
PROVISIONAL = ("provisional: the track record's scores are not to be relied on until evidence identity, evidence "
               "first-availability time and verified history land (follow-up PR)")
# Settings (recorded in the output): bands are 10th-90th percentiles; a direction needs its 95% interval to exclude 0
# and |rho| >= 0.1, on at least 10 usable points with 3 or more distinct feature values; a flip needs each option to
# win at >= 5% of the non-tied points, no reversal of more than 0.25 in the winning share across 5 quantile bins,
# and a one-feature logistic fit gaining >= 6.63 in deviance (chi-square 1 df, p 0.01). Its range is the fit's 25-75%
# transition widened to the nearest sampled points on either side. At most 3 features are displayed per pair.
SETTINGS = {"band_quantiles": [0.1, 0.9], "z": 1.96, "rho_min": 0.1, "min_points": 10, "flip_min_share": 0.05,
            "flip_deviance_min": 6.63, "bins": 5, "reversal": 0.25, "flips_max": 3, "tie_rtol": 1e-9, "digits": 3,
            "fdr_q": 0.05, "correlated": 0.5, "tie_share_max": 0.5,
            "alpha": 0.05, "perm_n_max": 50, "perm_resamples": 20000, "perm_seed": 0, "perm_minority_min": 10}


class WorldModelError(ValueError):
    """An input the world model cannot use: it fails closed with what is missing or inconsistent."""


class Sweep:
    """Per-person sweep tables at the design points: features {name: n values}, packages {name: [components]}, the
    comparator, its burden (primary outcome per person), y {package: outcome per person}, cost {package: $ per
    person}, the shadow price `lam` (outcome per $) and its source, and the artifacts they came from."""

    def __init__(self, features, packages, comparator, burden, y, cost, groups=None, points=None, kinds=None,
                 artifacts=None, lam=None, lam_source=None, dfac=1.0, evidence_time=None):
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
        import hashlib
        h = hashlib.sha256(json.dumps([self.points, self.comparator, self.packages, lam, self.dfac],
                                      sort_keys=True, default=str).encode())
        for d_ in (self.features, self.y, self.cost, {"burden": self.burden}):
            for k in sorted(d_):
                h.update(k.encode() + d_[k].tobytes())
        self.evidence_sha, self.evidence_time = h.hexdigest(), evidence_time      # what a confrontation checked

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
    """A finite float at full precision, or None: verdicts compare these; `_rounded` rounds only the output."""
    return None if x is None or not math.isfinite(float(x)) else float(x)


def _rounded(x):
    if isinstance(x, float):
        return float(f"{x:.{SETTINGS['digits']}g}") if math.isfinite(x) else x
    if isinstance(x, dict):
        return {k: _rounded(v) for k, v in x.items()}
    return [_rounded(v) for v in x] if isinstance(x, (list, tuple)) else x


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _band_ok(b):
    return isinstance(b, (list, tuple)) and len(b) == 2 and all(_num(v) for v in b) and b[0] <= b[1]


def _disjoint(a, b):
    return a[1] < b[0] or b[1] < a[0]


def _where_problem(where, sweep):
    """Why a `where` filter is malformed (None when it is fine). A numeric region is an exact, finite, ordered pair;
    a plan filter may instead list categories (text values); a sweep test filters on numbers only."""
    if not isinstance(where, dict):
        return "where: a mapping"
    for f, v in where.items():
        nums = isinstance(v, (list, tuple)) and any(isinstance(e, (int, float)) for e in v)
        if sweep or nums or isinstance(v, (int, float)):
            if not _band_ok(v):
                return f"where.{f}: an exact, finite, ordered numeric pair [lo, hi]"
        elif not (isinstance(v, str) or (isinstance(v, (list, tuple)) and v and all(isinstance(e, str) for e in v))):
            return f"where.{f}: a numeric pair or a list of category values"
    return None


def _pair(v):
    return isinstance(v, (list, tuple)) and len(v) == 2 and all(_num(x) for x in v)


def _intersect(region, f, r):
    """region with feature f's numeric range intersected with r (r added when f is not there; an empty intersection
    stays empty, lo > hi). Categorical filters are kept as they are; a numeric range on one is refused."""
    out = {k: (list(v) if isinstance(v, (list, tuple)) else v) for k, v in region.items()}
    if f in out and not _pair(out[f]):
        raise WorldModelError(f"a numeric condition on {f!r}, which the test filters by category")
    out[f] = [max(out[f][0], r[0]), min(out[f][1], r[1])] if f in out else list(r)
    return out


def _when(x):
    """An aware datetime from an ISO 8601 string with a time zone, else None."""
    import datetime as dt
    try:
        v = dt.datetime.fromisoformat(str(x).replace("Z", "+00:00"))
    except ValueError:
        return None
    return v if v.tzinfo is not None else None


def _sha(x):
    import hashlib
    return hashlib.sha256(json.dumps(x, sort_keys=True, default=str).encode()).hexdigest()


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
    if k == "plan_share" and (t.get("component") or t.get("package")) not in named:
        return f"tests {t.get('component') or t.get('package')!r}, the belief is about {ab}"
    reg = {f: v for f, v in (t.get("where") or {}).items() if _band_ok(v)}
    if t.get("range") and t.get("feature"):
        reg = _intersect(reg, t["feature"], t["range"])
    for c in b.get("conditions") or []:
        r = reg.get(c.get("feature"))
        if r and _band_ok(c.get("range")) and (r[0] > r[1] or _disjoint(r, c["range"])):
            return f"its region {c['feature']} {r} is empty or lies outside the belief's condition {c['range']}"
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
        if str(bid).startswith("sim."):
            errs.append(f"{w}: the sim. prefix is reserved for generated simulation beliefs")
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
        cc = {}
        for c in b.get("conditions") or []:
            if not (isinstance(c, dict) and c.get("feature") and _band_ok(c.get("range"))):
                errs.append(f"{w}: each condition {{feature, range: [lo, hi]}} of finite numbers")
            else:
                cc = _intersect(cc, c["feature"], c["range"])
        if any(r[0] > r[1] for r in cc.values()):
            errs.append(f"{w}: its conditions on one feature do not intersect (an empty region)")
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
        if h.get("registered_at") is not None and _when(h["registered_at"]) is None:
            errs.append(f"{w}: registered_at an ISO 8601 time with a time zone")
        t = h.get("test")
        if not isinstance(t, dict) or t.get("kind") not in TESTS:
            errs.append(f"{w}: a machine-checkable refutation test, kind one of {sorted(TESTS)}")
            continue
        extra = sorted(set(t) - {"kind"} - set(TESTS[t["kind"]]) - set(OPTIONAL[t["kind"]]))
        if extra:
            errs.append(f"{w}: test {t['kind']} has unknown fields {extra} (they would change its identity, not its "
                        "check)")
        miss = [f for f in TESTS[t["kind"]] if t.get(f) in (None, "", [])]
        if miss:
            errs.append(f"{w}: test {t['kind']} needs {miss} (every test declares its pass threshold)")
            continue
        for f in ("band", "range"):
            if f in t and not _band_ok(t[f]):
                errs.append(f"{w}: test {f} [lo, hi] of finite numbers")
        if t.get("where") is not None and _where_problem(t["where"], t["kind"] != "plan_share"):
            errs.append(f"{w}: {_where_problem(t['where'], t['kind'] != 'plan_share')}")
        if t["kind"] == "plan_share" and bool(t.get("component")) == bool(t.get("package")):
            errs.append(f"{w}: plan_share names exactly one of component (package composition) or package")
        lo_share = 0.0 if t["kind"] == "plan_share" else 0.5
        if "min_share" in TESTS[t["kind"]] and not (_num(t["min_share"]) and lo_share < t["min_share"] <= 1):
            errs.append(f"{w}: min_share a number in ({lo_share}, 1]")
        if t.get("scope", "conditional") not in ("conditional", "marginal"):
            errs.append(f"{w}: scope conditional or marginal")
        if t["kind"] == "paired_sign" and t.get("sign") not in ("positive", "negative"):
            errs.append(f"{w}: sign positive or negative")
        if t["kind"] == "effect_direction":
            if t.get("direction") not in ("increases", "decreases", "negligible"):
                errs.append(f"{w}: direction increases, decreases or negligible")
            elif t["direction"] == "negligible" and not (_num(t.get("equivalence")) and 0 < t["equivalence"] < 1):
                errs.append(f"{w}: a negligible claim needs an explicit equivalence bound on |rho| in (0, 1)")
            elif t["direction"] != "negligible" and "equivalence" in t:
                errs.append(f"{w}: an equivalence bound applies only to a negligible claim (it would change the "
                            "test's identity, not its check)")
        if h.get("belief") in by and not errs:
            why = _related(t, by[h["belief"]])
            if why:
                errs.append(f"{w}: does not bear on belief {h['belief']}: {why}")
    if errs:
        raise WorldModelError("world-model seed refused: " + "; ".join(errs))
    return seed


# ---------------------------------------------------------------- coarse summary of a sweep
def direction(x, y):
    """Tie-aware Spearman association of y with x with its 95% interval: increases, decreases, or inconclusive (too
    few points, a constant or categorical input, an input where one value holds more than tie_share_max of the
    points, for which the interval is not calibrated, or no clear sign)."""
    from scipy.stats import rankdata
    m = np.isfinite(x) & np.isfinite(y)
    n = int(m.sum())
    x, y = x[m], y[m]
    if n < SETTINGS["min_points"] or len(np.unique(x)) < 3 or np.ptp(y) == 0:
        return {"direction": "inconclusive", "n": n,
                "why": f"{n} usable points" if n < SETTINGS["min_points"] else "constant or categorical: undefined"}
    tied = max(np.unique(v, return_counts=True)[1].max() for v in (x, y)) / n
    if tied > SETTINGS["tie_share_max"]:
        return {"direction": "inconclusive", "n": n, "why": f"heavily tied: one value holds {tied:.0%} of the points"}
    rx, ry = rankdata(x), rankdata(y)
    rho = float(np.corrcoef(rx, ry)[0, 1])
    rmin = SETTINGS["rho_min"]
    if n < SETTINGS["perm_n_max"]:                 # small samples: a permutation p-value, no interval
        B = SETTINGS["perm_resamples"]
        yp = ry[np.argsort(np.random.default_rng(SETTINGS["perm_seed"]).random((B, n)), axis=1)]
        yc, xc = yp - yp.mean(1, keepdims=True), rx - rx.mean()
        rc = (yc @ xc) / (np.sqrt((yc ** 2).sum(1)) * math.sqrt(float((xc ** 2).sum())))
        p = float((1 + np.sum(np.abs(rc) >= abs(rho) - 1e-12)) / (B + 1))
        d = "increases" if p <= SETTINGS["alpha"] and rho >= rmin else \
            "decreases" if p <= SETTINGS["alpha"] and rho <= -rmin else "inconclusive"
        return {"direction": d, "rho": _r(rho), "ci": None, "n": n, "p": p, "p_method": "permutation"}
    zr, se = math.atanh(max(min(rho, 1 - 1e-12), -1 + 1e-12)), 1 / math.sqrt(n - 3)
    lo, hi = math.tanh(zr - SETTINGS["z"] * se), math.tanh(zr + SETTINGS["z"] * se)
    d = "increases" if lo > 0 and rho >= rmin else "decreases" if hi < 0 and rho <= -rmin else "inconclusive"
    return {"direction": d, "rho": _r(rho), "ci": [_r(lo), _r(hi)], "n": n,
            "p": _r(math.erfc(abs(zr) / se / math.sqrt(2))), "p_method": "fisher"}


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
    from scipy.stats import chi2
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
    k_min = int(min(s.sum(), len(s) - s.sum()))
    if len(x) < SETTINGS["perm_n_max"] or k_min < SETTINGS["perm_minority_min"]:   # permutation: winners' rank sum
        from scipy.stats import rankdata
        rk, k = rankdata(x), int(s.sum())
        perm = np.argsort(np.random.default_rng(SETTINGS["perm_seed"]).random((SETTINGS["perm_resamples"], len(x))),
                          axis=1)[:, :k]
        mu = k * (len(x) + 1) / 2
        pv = float((1 + np.sum(np.abs(rk[perm].sum(1) - mu) >= abs(rk[s == 1].sum() - mu) - 1e-9)) /
                   (SETTINGS["perm_resamples"] + 1))
        found, how = pv <= chi2.sf(SETTINGS["flip_deviance_min"], 1), "permutation"
    else:
        pv, found, how = float(chi2.sf(max(dev, 0.0), 1)), dev >= SETTINGS["flip_deviance_min"], "logistic"
    if not (found and math.isfinite(t) and lo <= t <= hi):
        return dict(out, status="no_flip_detected", deviance_gain=_r(dev), p=_r(pv), p_method=how)
    h = math.log(3) / abs(b1)
    below, above = x[x <= t], x[x >= t]
    rlo = min(t - h, float(below.max()) if len(below) else lo)      # widened to the nearest sampled points
    rhi = max(t + h, float(above.min()) if len(above) else hi)
    return dict(out, status="flip", at=_r(t), range=[_r(max(lo, rlo)), _r(min(hi, rhi))],
                first_preferred_below=bool(b1 < 0), deviance_gain=_r(dev), p=_r(pv), p_method=how)


def flip(sw, a, b):
    """The a/b comparison along every feature; `along` displays at most flips_max located flips."""
    if sw.lam is None:
        return {"a": a, "b": b, "status": "no_shadow_price", "along": []}
    d, tol = sw.diff(a, b)
    fl = [{k: v for k, v in flip_along(sw, a, b, f).items() if k != "points"} for f in sorted(sw.features)]
    return {"a": a, "b": b, "measure": "net_benefit", "lambda": sw.lam, "n": len(d), "share_a": _r(np.mean(d > tol)),
            "share_b": _r(np.mean(d < -tol)), "share_tie": _r(np.mean(np.abs(d) <= tol)), "along": [], "_all": fl}


def correlations(sw, mask=None, only=None):
    """{feature: [design features whose tie-aware rank correlation with it is >= SETTINGS correlated]} over the
    points in `mask` (constant features excluded); `only`: just that feature's list."""
    from scipy.stats import rankdata
    m = np.ones(len(sw.points), bool) if mask is None else mask
    names = [f for f in sorted(sw.features) if np.ptp(sw.features[f][m]) > 0] if m.sum() > 2 else []
    R = {f: rankdata(sw.features[f][m]) for f in names}
    out = {}
    for i, f in enumerate(names):
        for g in names[i + 1:]:
            if only in (None, f, g) and abs(np.corrcoef(R[f], R[g])[0, 1]) >= SETTINGS["correlated"]:
                out.setdefault(f, []).append(g)
                out.setdefault(g, []).append(f)
    return {f: sorted(v) for f, v in out.items()}


def _bh(ps, q):
    """The Benjamini-Hochberg cut: the largest p-value declared a discovery (-1 when none)."""
    ps = sorted(ps)
    m = len(ps)
    return max([p for i, p in enumerate(ps, 1) if p <= q * i / m], default=-1.0)


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
    flips = {f"{a}~{b}": flip(sw, a, b) for a, b in pairs}
    corr = correlations(sw)
    tested = [1.0 if v.get("p") is None else v["p"] for e in eff.values() for v in e["direction"].values()] + \
        [1.0 if x.get("p") is None else x["p"] for fl in flips.values() for x in fl.get("_all", [])]
    cut, found = _bh(tested, SETTINGS["fdr_q"]), 0
    for e in eff.values():
        for f, v in e["direction"].items():
            v.update(scope="marginal", correlated_with=corr.get(f, []))
            if v["direction"] in ("increases", "decreases"):
                if v["p"] <= cut:
                    found += 1
                else:
                    v.update(direction="inconclusive", why="not significant after false-discovery control")
    for fl in flips.values():
        keep = [dict(x, scope="marginal", correlated_with=corr.get(x["feature"], []))
                for x in fl.pop("_all", []) if x["status"] == "flip" and x["p"] <= cut]
        found += len(keep)
        fl["along"] = sorted(keep, key=lambda r: (-r["deviance_gain"], r["feature"]))[:SETTINGS["flips_max"]]
        fl["located"] = len(keep)
    return {"comparator": sw.comparator, "n_points": len(sw.points), "settings": dict(SETTINGS),
            "evidence_sha": sw.evidence_sha, "screened": True,
            "multiplicity": {"method": "Benjamini-Hochberg", "q": SETTINGS["fdr_q"], "tested": len(tested),
                             "discoveries": found, "p_cut": cut if cut >= 0 else None},
            "shadow_price": {"value": sw.lam, "source": sw.lam_source,
                             "note": "the budget row's recorded dual, or the value passed: paired results between "
                                     "options of different cost depend on it"},
            "effects": eff, "flips": flips, "feature_correlations": corr,
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
        if _band_ok(v):
            try:
                x = float(x)
            except (TypeError, ValueError):
                raise WorldModelError(f"filter {f}: value {x!r} is not a number under the numeric range {v}")
            if not math.isfinite(x):
                return None
            if not (v[0] <= x <= v[1]):
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
        got = {u: _match(row, t.get("where") or {}) for u, row in sorted(units.items())}
        nocol, hit = [u for u, v in got.items() if v is None], [u for u, v in got.items() if v]
        absent = [u for u in hit if u not in plan]
        if nocol or absent or not hit:
            return "untestable", {"reason": f"{len(nocol)} units lack a filter column, {len(absent)} matching units "
                                            f"are missing from the plan, {len(hit)} match"}, "analysis/point.yaml", []
        if t.get("package"):
            share = sum(plan[u] == t["package"] for u in hit) / len(hit)
        else:
            c = t["component"]
            if not any(c in v for v in sw.packages.values()):
                raise WorldModelError(f"plan_share: no package contains component {c!r}")
            share = sum(c in sw.packages.get(plan[u], []) for u in hit) / len(hit)
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
        return (*_scope(sw, t, st, obs, _region(sw, t.get("where"))), art, fa["points"])
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
    res = direction(sw.features[t["feature"]][use], r[use])
    got, want = res["direction"], t["direction"]
    if want == "negligible":           # judged on its equivalence bound alone, whatever the significance
        eq, ci = float(t["equivalence"]), res.get("ci")
        st = "inconclusive" if not ci else "supported" if -eq <= ci[0] and ci[1] <= eq else \
            "refuted" if ci[0] > eq or ci[1] < -eq else "inconclusive"
    else:
        st = "supported" if got == want else "inconclusive" if got == "inconclusive" else "refuted"
    return (*_scope(sw, t, st, dict(obs, **res), use), art, _ids(sw, use))


def _scope(sw, t, st, obs, m):
    """A conditional claim (the default) cannot be settled by a marginal association along a feature that is
    correlated with another design feature in the tested points: supported or refuted becomes inconclusive."""
    cw = correlations(sw, m, t["feature"]).get(t["feature"]) if st in ("supported", "refuted") else None
    if t.get("scope", "conditional") != "conditional" or not cw:
        return st, obs
    return "inconclusive", dict(obs, marginal_only={"correlated_with": cw, "marginal_result": st, "why": (
        "a marginal association along a feature correlated with others does not settle a conditional claim")})


def _constrained(t, b):
    """The test as checked: its belief's conditions intersected into its `where` (a flip belief's condition on the
    test's own feature locates the flip, so it is not applied)."""
    conds = {}
    for c in (b or {}).get("conditions") or []:          # repeated conditions on one feature intersect
        conds = _intersect(conds, c["feature"], c["range"])
    if (b or {}).get("direction") == "flips":
        conds.pop(t.get("feature"), None)
    if not conds:
        return t
    w = dict(t.get("where") or {})
    for f, r in conds.items():
        w = _intersect(w, f, r)
    return dict(t, where=w)


def _canon(v):
    if v is None or isinstance(v, (bool, str)):
        return v
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, (list, tuple)):
        return [_canon(e) for e in v]
    return {k: _canon(x) for k, x in sorted(v.items())} if isinstance(v, dict) else v


def canonical_test(t, belief=None):
    """The test exactly as checked: its belief's conditions applied, defaults resolved (scope), an empty `where`
    dropped and numbers as floats. Its sha256 is the test's identity in the ledger and the track record."""
    c = dict(_constrained(t, belief))
    if c["kind"] in ("effect_direction", "flip_in"):
        c.setdefault("scope", "conditional")
    if not c.get("where"):
        c.pop("where", None)
    return {k: _canon(v) for k, v in sorted(c.items()) if v is not None}


def check(hyps, ctx):
    """Evaluate each hypothesis' refutation test, constrained by its belief's conditions (ctx["beliefs"]), against
    the run's artifacts; returns the hypotheses with status and result (observed value, artifact, the test as
    applied, ids of the points or units used)."""
    out, by = [], ctx.get("beliefs") or {}
    for h in hyps:
        t = canonical_test(h["test"], by.get(h.get("belief")))
        st, obs, art, ids = _test(t, ctx)
        out.append(dict(h, status=st, result={"observed": obs, "artifact": art or "analysis/batch/results.jsonl",
                                              "test_applied": t, "points": ids}))
    return out


# ---------------------------------------------------------------- build: seed + simulation summary + confrontation
def _sim_beliefs(sw, summ):
    ev = [{"artifact": sw.artifacts.get("results", "analysis/batch/results.jsonl"), "points": list(sw.points)}]
    out = []
    for p, e in summ["effects"].items():
        if e["band"] is None:
            continue
        along = {f: v["direction"] for f, v in e["direction"].items() if v["direction"] in ("increases", "decreases")}
        out.append({"id": f"sim.effect.{p}", "about": {"package": p}, "screened": True, "scope": "marginal",
                    "correlated_with": {f: e["direction"][f]["correlated_with"] for f in along},
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
                        "screened": True, "scope": "marginal",
                        "correlated_with": {x["feature"]: x["correlated_with"] for x in fl["along"]},
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


def _prior(seed, hypotheses):
    """A seed, or a produced world model to build on: its simulation beliefs are superseded (recorded), its
    literature and data beliefs kept with their confidence, and hypotheses reloaded only when given."""
    import copy
    s = copy.deepcopy(seed) if isinstance(seed, dict) else seed
    if isinstance(s, dict) and s.get("schema") == SCHEMA:
        sims = [b["id"] for b in s.get("beliefs") or [] if str(b.get("id")).startswith("sim.")]
        sup = list(s.get("superseded_simulation") or []) + [
            {"version": s.get("version", 1), "beliefs": sims,
             "evidence_sha": (s.get("simulation_summary") or {}).get("evidence_sha")}]
        keep = [b for b in s.get("beliefs") or [] if b.get("id") not in sims]
        for b in keep:
            b["conflicts"] = [c for c in b.get("conflicts") or [] if c.get("belief") not in sims]
            if not b["conflicts"]:
                b.pop("conflicts")
            for k in ("confirmed_by", "contradicted_by"):
                b.pop(k, None)
        s = {"beliefs": keep, "open_questions": s.get("open_questions") or [], "hypotheses": [],
             "track_record": s.get("track_record"), "version": s.get("version", 1), "superseded_simulation": sup}
    if hypotheses is not None and isinstance(s, dict):
        s["hypotheses"] = [{k: v for k, v in h.items() if k not in ("status", "result", "confrontation")}
                           for h in hypotheses]
    return s


NAMES = {"supported": "confirmed", "refuted": "contradicted"}


def _line_sha(e):
    return _sha({k: v for k, v in e.items() if k != "line_sha"})


def verify_ledger(lines):
    """The registration ledger's lines when its chain verifies: each line's own sha256, the previous line's sha256,
    its canonical test's sha256 and a registration time no earlier than the line before. Otherwise refused."""
    prev, t0 = "0" * 64, None
    for k, e in enumerate(lines):
        t = _when(e.get("registered_at")) if isinstance(e, dict) else None
        if t is None or e.get("prev") != prev or e.get("line_sha") != _line_sha(e) or \
                e.get("test_sha") != _sha(e.get("test")):
            raise WorldModelError(f"registration ledger line {k + 1}: edited, reordered, removed or malformed")
        if t0 is not None and t < t0:
            raise WorldModelError(f"registration ledger line {k + 1}: dated before the line it follows (backdated)")
        prev, t0 = e["line_sha"], t
    return lines


def anchor_cover(led, anchors):
    """{ledger line index: (time, source)} of the earliest recorded head covering each line. A ledger that does not
    extend a recorded head (its line at the head's count is not the head) was rewritten or truncated: refused."""
    cov = {}
    for a in sorted(anchors or [], key=lambda a: str(a.get("at"))):
        n = a.get("lines")
        if _when(a.get("at")) is None or not isinstance(n, int) or not 0 < n <= len(led) or \
                led[n - 1]["line_sha"] != a.get("head"):
            raise WorldModelError(f"the registration ledger does not extend the head recorded at {a.get('at')} "
                                  f"({a.get('source')}): it was rewritten or truncated")
        for i in range(n):
            cov.setdefault(i, (a["at"], a.get("source")))
    return cov


def cli_anchors(run_dir, reg_path, ref):
    """The anchors a build uses: ledger commits reachable from a fetched remote-tracking ref, and nothing else
    (run_state.json heads are not anchors until v2 hash-protects run_state.json)."""
    return git_anchors(reg_path, ref) if ref else []


def git_anchors(path, ref):
    """Recorded heads from git: each commit of the ledger file that is an ancestor of the fetched remote `ref`, with
    the verified ledger it held and its commit time. `ref` must resolve to refs/remotes/... (refused otherwise).
    Residual trust: the commit time is the committer's clock; the remote shows the commit was pushed by the time it
    was fetched. Fetch before use; git unavailable: none."""
    import subprocess
    d, name = os.path.dirname(os.path.abspath(path)), os.path.basename(path)
    out = []
    try:
        run = lambda *a: subprocess.run(["git", "-C", d, *a], capture_output=True, text=True, timeout=120)  # noqa
        full = run("rev-parse", "--symbolic-full-name", ref).stdout.strip()
        if not full.startswith("refs/remotes/"):
            raise WorldModelError(f"--anchor-ref {ref!r} resolves to {full or 'no symbolic ref'}: a fetched "
                                  "remote-tracking ref (refs/remotes/<remote>/<branch>) is required")
        log = run("log", "--format=%H %cI", full, "--", name)
        for row in (log.stdout.splitlines() if log.returncode == 0 else []):
            c, at = row.split()
            if run("merge-base", "--is-ancestor", c, full).returncode != 0:
                continue
            show = run("show", f"{c}:./{name}")
            try:
                lines = verify_ledger([json.loads(x) for x in show.stdout.splitlines() if x.strip()])
            except (WorldModelError, ValueError):
                continue
            if show.returncode == 0 and lines:
                out.append({"at": at, "lines": len(lines), "head": lines[-1]["line_sha"], "commit": c,
                            "source": f"anchored in remote git (commit {c}); the commit time is the committer's "
                                      "clock", "ref": full})
    except WorldModelError:
        raise
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    return out


def register(seed, ledger, at=None):
    """The ledger with every hypothesis of the seed whose canonical test is not yet in it appended, registered at
    `at` (default now; never before the ledger's last line). The ledger is verified first."""
    import datetime as dt
    led = list(verify_ledger([dict(e) for e in ledger or []]))
    seed = validate_seed(_prior(seed, None))
    by = {b["id"]: b for b in seed["beliefs"]}
    at = at or dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    if _when(at) is None or (led and _when(at) < _when(led[-1]["registered_at"])):
        raise WorldModelError(f"registration time {at!r}: an ISO 8601 time with a zone, not before the ledger's last")
    have = {e["test_sha"] for e in led}
    for h in seed.get("hypotheses") or []:
        c = canonical_test(h["test"], by.get(h["belief"]))
        if _sha(c) in have:
            continue
        e = {"hypothesis": h["id"], "belief": h["belief"], "test": c, "test_sha": _sha(c), "registered_at": at,
             "prev": led[-1]["line_sha"] if led else "0" * 64}
        e["line_sha"] = _line_sha(e)
        led.append(e)
        have.add(e["test_sha"])
    return led


def _record(hist, seed_tr):
    """Counts from the identified confrontations (first occurrence of each (test, evidence) pair): pre-registered
    ones in the main counts and the share confirmed, post-hoc ones apart. Seed counters are reconciled, not used."""
    blank = lambda: {v: 0 for v in ("confirmed", "contradicted", "inconclusive", "untestable", "open")}  # noqa
    pre, post, seen, unid = blank(), blank(), set(), 0
    for e in hist:
        k = (e.get("test_sha"), e.get("evidence_sha"))
        if not k[0]:
            unid += 1
            continue
        if e.get("repeat") or k in seen:
            continue
        seen.add(k)
        (post if e.get("post_hoc", True) else pre)[NAMES.get(e["status"], e["status"])] += 1
    n = pre["confirmed"] + pre["contradicted"]
    tr = dict(pre, post_hoc=post, share_confirmed=_r(pre["confirmed"] / n) if n else None, history=hist)
    if unid:
        tr["unidentified_entries"] = unid
    old = {k: v for k, v in (seed_tr or {}).items() if k in pre}
    if any(old.get(k, 0) != pre[k] for k in old):
        tr["reconciled"] = {"seed_counts": old, "recomputed": pre}
    return tr


def build(seed, sw, plan=None, units=None, inputs=None, hypotheses=None, plan_time=None, registry=None):
    """(world model, hypotheses): the validated seed (or the previous world model), the sweep's screened
    simulation beliefs, conflicts, every hypothesis checked, the confrontations identified and recorded, confidence
    moved only by new pre-registered ones, contradictions as open questions, and where to look next."""
    seed = validate_seed(_prior(seed, hypotheses))
    hyps = seed.get("hypotheses") or []
    pairs = [(h["test"]["a"], h["test"]["b"]) for h in hyps if h["test"]["kind"] in ("paired_sign", "flip_in")]
    pk = [h["test"]["package"] for h in hyps if h["test"].get("package")] + \
        [b["about"]["package"] for b in seed["beliefs"] if b["about"].get("package") in sw.packages]
    summ = summarise(sw, pairs, pk)
    beliefs = seed["beliefs"] + _sim_beliefs(sw, summ)
    _conflicts(beliefs, sw)
    checked = check(hyps, {"sweep": sw, "plan": plan, "units": units or {},
                           "beliefs": {b["id"]: b for b in seed["beliefs"]}})
    hist = [e for e in (seed.get("track_record") or {}).get("history") or [] if isinstance(e, dict)]
    seen = {(e.get("test_sha"), e.get("evidence_sha")) for e in hist if e.get("test_sha")}
    plan_sha = _sha([plan, units]) if plan else None
    lines, anchors = (registry.get("lines"), registry.get("anchors")) if isinstance(registry, dict) else \
        (registry, None)
    led = verify_ledger(list(lines or []))
    cover = anchor_cover(led, anchors)
    first = {}
    for i, e in enumerate(led):
        first.setdefault(e["test_sha"], (i, e))
    by = {b["id"]: b for b in beliefs}
    qs = list(seed.get("open_questions") or [])
    for h in checked:
        on_plan = h["test"]["kind"] == "plan_share"
        ev_sha, ev_time = (plan_sha, plan_time) if on_plan else (sw.evidence_sha, sw.evidence_time)
        key = (_sha(h["result"]["test_applied"]), ev_sha)
        i, e = first.get(key[0], (None, None))
        anchor, at = cover.get(i), _when(ev_time)
        post = anchor is None or at is None or _when(anchor[0]) >= at
        again = key in seen
        seen.add(key)
        h["confrontation"] = {"test_sha": key[0], "evidence_sha": ev_sha, "evidence_time": ev_time,
                              "post_hoc": post, "repeat": again, "registration": e["line_sha"] if e else None,
                              "anchor": {"at": anchor[0], "source": anchor[1]} if anchor else None,
                              "registration_status": "post hoc" if post else "remote-anchored"}
        hist.append(dict({"hypothesis": h["id"], "belief": h["belief"], "status": h["status"]}, **h["confrontation"]))
        b = by[h["belief"]]
        if again:
            continue
        if h["status"] == "refuted" and f"Q.{h['id']}" not in {q.get("id") for q in qs}:
            qs.append({"id": f"Q.{h['id']}", "beliefs": [b["id"]], "post_hoc": post,
                       "decision_relevant": bool(b.get("decision_relevant")),
                       "question": f"Why was {h['id']} refuted ({h['claim']}; observed {h['result']['observed']})? "
                                   "Either the world model misses a mechanism, or the source or the simulation is "
                                   "wrong: state the cause as a hypothesis and test it; never adjust inputs to agree."})
        if not post and h["status"] in NAMES:
            b.setdefault("contradicted_by" if h["status"] == "refuted" else "confirmed_by", []).append(h["id"])
    for b in seed["beliefs"]:
        i = CONFIDENCE.index(b["confidence"])
        # a contradiction lowers confidence; a confirmation raises it only when no evidence conflicts with the belief
        j = i - 1 if b.get("contradicted_by") else i + 1 if b.get("confirmed_by") and not b.get("conflicts") else i
        if j != i:
            b.setdefault("confidence_seed", b["confidence"])
            b["confidence"] = CONFIDENCE[min(max(j, 0), 2)]
    tr = dict(_record(hist, seed.get("track_record")), provisional=PROVISIONAL, tamper_proof=False,
              note="pre-registration is remote-anchored (a ledger commit reachable from a fetched remote-tracking "
                   "ref, whose commit time is the committer's clock) or post hoc; the track record is not "
                   "tamper-proof: whoever controls the machine and can push to the remote can rewrite both",
              registry={"lines": len(led), "head": led[-1]["line_sha"] if led else None,
                        "anchors": [{k: a.get(k) for k in ("at", "lines", "source")} for a in anchors or []]})
    wm = {"schema": SCHEMA, "producer": PRODUCER, "produced_by": "harness", "version": seed.get("version", 0) + 1,
          "inputs": inputs or sw.artifacts, "superseded_simulation": seed.get("superseded_simulation") or [],
          "structure": {"comparator": sw.comparator, "packages": sorted(sw.packages),
                        "terms": "net benefit per person = discount x baseline burden x relative reduction - shadow "
                                 "price x incremental cost per person"},
          "note": "a working understanding, meant to be mostly right; its bands are never decision numbers. "
                  "Simulation beliefs are screened marginal associations, not mechanisms",
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


def benchmark_record(bench_dir, ident):
    """Where the engine's benchmark history records identity `ident` (version and code_sha256): its results.yaml
    or history/*.yaml in the working tree, else results.yaml in a commit that is an ancestor of HEAD. Refused
    (WorldModelError) otherwise, or when git is unavailable."""
    import glob
    import subprocess
    import yaml
    keys = ("name", "version", "code_sha256", "params_sha256", "config_sha256")
    want = tuple(str(ident.get(k)) for k in keys)
    same = lambda doc: isinstance(doc, dict) and tuple(  # noqa: E731
        str((doc.get("engine_identity") or {}).get(k)) for k in keys) == want
    files = [os.path.join(bench_dir, "results.yaml")] + sorted(glob.glob(os.path.join(bench_dir, "history", "*.yaml")))
    for f in files:
        try:
            if same(yaml.safe_load(open(f))):
                return {"matched": "working tree", "file": os.path.relpath(f, bench_dir), **dict(zip(keys, want))}
        except (OSError, yaml.YAMLError):
            continue
    try:
        run = lambda *a: subprocess.run(["git", "-C", bench_dir, *a], capture_output=True, text=True,  # noqa
                                        timeout=120)
        log = run("log", "--format=%H", "-S", str(want[2]), "--", "results.yaml")
        for c in log.stdout.split() if log.returncode == 0 else []:
            if run("merge-base", "--is-ancestor", c, "HEAD").returncode != 0:
                continue
            for rev in (c, c + "^"):            # -S lists the commits that add or remove the string
                show = run("show", f"{rev}:./results.yaml")
                if show.returncode == 0 and same(yaml.safe_load(show.stdout)):
                    sha = run("rev-parse", rev).stdout.strip()
                    return {"matched": "git history", "file": "results.yaml", "commit": sha, **dict(zip(keys, want))}
    except (OSError, subprocess.SubprocessError, yaml.YAMLError):
        pass
    raise WorldModelError(f"recorded engine identity {dict(zip(keys, want))} is not in the engine's benchmark history "
                          f"({bench_dir}: results.yaml, history/*, or results.yaml in an ancestor commit)")


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


def shadow_price(point):
    """(lambda, source) from a point plan's data: the budget row's recorded dual, or 0 when the plan records that its
    budget does not bind; else (None, why). Never inferred from average benefit / cost."""
    sol = (point or {}).get("solver") or {}
    if _num(sol.get("budget_dual")) and sol["budget_dual"] >= 0:
        return float(sol["budget_dual"]), "analysis/point.yaml solver.budget_dual (the budget row's dual)"
    if sol.get("budget_slack") is True:
        return 0.0, "analysis/point.yaml solver.budget_slack: the budget does not bind"
    return None, "no recorded dual of the budget row (analysis/point.yaml solver.budget_dual): pass --shadow-price"


def _manifest_problems(run_dir):
    """Why the run's completed analysis manifest does not vouch for its point plan with the current batch (empty when
    it does): it must be complete and list analysis/point.yaml and analysis/batch.yaml at their current sha256, and
    batch.yaml must record the current design and results."""
    import yaml
    from toolkit.analysis.artifacts import sha256
    try:
        m = json.load(open(os.path.join(run_dir, "analysis", "manifest.json")))
    except (OSError, ValueError):
        return ["no completed analysis manifest (analysis/manifest.json)"]
    arts = m.get("artifacts") if isinstance(m, dict) else None
    if not (isinstance(arts, dict) and m.get("complete") is True):
        return ["the analysis manifest is not complete"]
    bad = [f"the manifest does not list {rel} at its current sha256" for rel in ("analysis/point.yaml",
                                                                               "analysis/batch.yaml")
           if (arts.get(rel) or {}).get("sha256") is None
           or (arts.get(rel) or {}).get("sha256") != sha256(os.path.join(run_dir, rel))]
    try:
        inp = (yaml.safe_load(open(os.path.join(run_dir, "analysis", "batch.yaml"))) or {}).get("inputs") or {}
    except (OSError, yaml.YAMLError, AttributeError):
        inp = {}
    bad += [f"analysis/batch.yaml does not record the current {rel}" for rel in
            ("analysis/batch/design.json", "analysis/batch/results.jsonl")
            if inp.get(rel) is None or inp.get(rel) != sha256(os.path.join(run_dir, rel))]
    return bad


def point_doc(run_dir, ident):
    """analysis/point.yaml when the completed analysis manifest lists it with the current batch and it is bound to
    this run (its spec sha256, its engine identity and every input it records match), None when absent; a stale or
    unbound plan is refused."""
    from toolkit.analysis.artifacts import read, sha256
    rel = "analysis/point.yaml"
    if not os.path.isfile(os.path.join(run_dir, rel)):
        return None
    doc = read(run_dir, rel)
    if not isinstance(doc, dict) or not isinstance(doc.get("data"), dict):
        raise WorldModelError(f"{rel}: not an analysis artifact")
    bad = _manifest_problems(run_dir)
    bad += [] if doc.get("spec_sha256") == sha256(os.path.join(run_dir, "decision_spec.yaml")) else ["the spec"]
    bad += [] if doc["data"].get("engine_identity") == ident else ["the engine identity"]
    bad += _input_problems(run_dir, rel, doc, set())
    if bad:
        raise WorldModelError(f"{rel} is not bound to this run: {bad} differ from what it was computed with")
    return doc


def _input_problems(run_dir, rel, doc, seen):
    """What does not verify in an artifact's recorded inputs, recursively through the harness artifacts among them:
    each must exist with its recorded sha256, and the artifact must record at least one."""
    import yaml
    from toolkit.analysis.artifacts import sha256
    inp = doc.get("inputs") if isinstance(doc, dict) else None
    if not isinstance(inp, dict) or not inp:
        return [f"{rel}: records no inputs"]
    bad = []
    for q_rel, h in sorted(inp.items()):
        q = os.path.normpath(os.path.join(run_dir, q_rel))
        got = sha256(q)
        if not (isinstance(h, str) and len(h) == 64) or got is None or got != h:
            bad.append(f"{rel} -> {q_rel}: " + ("no recorded sha256" if not (isinstance(h, str) and len(h) == 64)
                                                else "missing or unreadable" if got is None else "changed"))
        elif q_rel.endswith((".yaml", ".yml")) and q not in seen:
            seen.add(q)
            try:
                sub = yaml.safe_load(open(q))
            except (OSError, yaml.YAMLError):
                sub = None
            if isinstance(sub, dict) and ("inputs" in sub or str(sub.get("schema") or "").startswith("analysis.")):
                bad += _input_problems(run_dir, q_rel, sub, seen)      # a harness artifact must record its inputs
    return bad


def consumed_inputs(spec, run_dir):
    """{path relative to the run: sha256} of the data and settings files the world model reads beyond the spec and
    the batch: the units table, the settings file and the engine choice. A missing one fails closed."""
    from toolkit.analysis.artifacts import sha256
    from toolkit.spec import schema
    paths = [spec.resolve(spec["decision_units"]["csv"])] + ([spec.settings_path] if spec.settings_path else []) + \
        ([os.path.join(run_dir, schema.ENGINE_CHOICE)] if getattr(spec, "engine_auto", False) else [])
    out = {os.path.relpath(q, run_dir): sha256(q) for q in paths}
    if any(v is None for v in out.values()):
        raise WorldModelError(f"inputs the world model reads are missing: {[k for k, v in out.items() if v is None]}")
    return out


def load_sweep(run_dir, identity="current", lam=None, use_plan=True):
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
        import inspect
        bench = benchmark_record(os.path.join(os.path.dirname(inspect.getfile(type(spec.engine()))), "benchmarks"),
                                 ident)
    elif identity == "current":
        ident, bench = engine_identity(spec.engine()), {"matched": "the current engine"}
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
    arts = dict({rel: sha256(os.path.join(run_dir, rel)) for rel in need.values()}, **consumed_inputs(spec, run_dir))
    point = point_doc(run_dir, ident) if use_plan else None
    lam_v, lam_s = (float(lam), "--shadow-price") if lam is not None else shadow_price((point or {}).get("data"))
    try:        # when the evidence existed: the analysis manifest's finish time, else the results file's mtime
        et, ets = json.load(open(os.path.join(run_dir, "analysis", "manifest.json")))["finished_at"], "manifest"
    except (OSError, ValueError, KeyError, TypeError):
        import datetime as dt
        et = dt.datetime.fromtimestamp(os.path.getmtime(os.path.join(run_dir, need["results"])),
                                       dt.timezone.utc).isoformat()
        ets = "results.jsonl modification time"
    arts.update(results=need["results"], engine_identity_mode=identity, engine_identity_check=bench,
                evidence_time={"value": et, "source": ets})
    sw = Sweep(F, pk, spec.comparator, burden, y, cost, groups, [p["point"] for p in design["points"]], kinds,
               arts, lam_v, lam_s, discount_factor(spec), evidence_time=et)
    sw.point = point
    return sw, spec


def load_plan(run_dir, spec, sw):
    """(plan, units, written_at) from analysis/point.yaml and the spec's units (columns, features, calibrated
    value), or (None, None, None) when the run has no optimised plan. A plan missing units is kept as it is: a
    plan_share test over a missing unit is untestable."""
    from toolkit.analysis import features as FT
    doc = getattr(sw, "point", None)          # bound to the run by load_sweep (point_doc)
    if doc is None:
        return None, None, None
    d = doc["data"]
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
    return plan, units, doc.get("written_at")


def _cli(argv):
    import argparse
    import yaml
    ap = argparse.ArgumentParser(prog="world_model.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("validate")
    v.add_argument("seed")
    g = sub.add_parser("register")
    g.add_argument("seed")
    g.add_argument("run_dir", help=f"the run whose ledger ({LEDGER}) receives the registrations")
    b = sub.add_parser("build")
    b.add_argument("run_dir")
    b.add_argument("--seed", required=True)
    b.add_argument("--out")
    b.add_argument("--engine-identity", default="current", choices=("current", "recorded"))
    b.add_argument("--shadow-price", type=float)
    b.add_argument("--hypotheses", help="a hypotheses.yaml to reload (with a world_model.yaml as --seed)")
    b.add_argument("--registry", help=f"the registration ledger (default: <run_dir>/{LEDGER} when present)")
    b.add_argument("--without-plan", action="store_true", help="build without analysis/point.yaml")
    b.add_argument("--anchor-ref", help="a fetched remote ref (origin/<branch>) whose commits anchor the ledger")
    a = ap.parse_args(argv)
    seed = yaml.safe_load(open(a.seed))
    if a.cmd == "validate":
        seed = validate_seed(_prior(seed, None))
        print(f"OK: {len(seed['beliefs'])} beliefs, {len(seed.get('hypotheses') or [])} hypotheses")
        return 0
    from toolkit.analysis.artifacts import _safe_io, sha256
    jsonl = lambda q: [json.loads(x) for x in open(q) if x.strip()] if q and os.path.isfile(q) else []  # noqa: E731
    if a.cmd == "register":              # one lock across reading the head, building the lines and appending
        import fcntl
        with _safe_io().open_append_nofollow(a.run_dir, LEDGER) as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                old = jsonl(os.path.join(a.run_dir, LEDGER))
                new = register(seed, old)[len(old):]
                f.write("".join(json.dumps(e, sort_keys=True) + "\n" for e in new))
                f.flush()
                os.fsync(f.fileno())
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        print(f"registered {len(new)} test(s) in {a.run_dir}/{LEDGER} ({len(old) + len(new)} lines)")
        return 0
    sw, spec = load_sweep(a.run_dir, a.engine_identity, a.shadow_price, use_plan=not a.without_plan)
    plan, units, plan_time = load_plan(a.run_dir, spec, sw)
    hyps = (yaml.safe_load(open(a.hypotheses)) or {}).get("hypotheses") if a.hypotheses else None
    inputs = dict({k: v for k, v in sw.artifacts.items() if k != "results"}, seed=sha256(a.seed))
    if plan is not None:
        inputs["analysis/point.yaml"] = sha256(os.path.join(a.run_dir, "analysis", "point.yaml"))
    if a.hypotheses:
        inputs["hypotheses"] = sha256(a.hypotheses)
    reg_path = a.registry or os.path.join(a.run_dir, LEDGER)
    if os.path.isfile(reg_path):
        inputs["registry"] = sha256(reg_path)
    anchors = cli_anchors(a.run_dir, reg_path, a.anchor_ref)
    wm, hy = build(seed, sw, plan, units, inputs, hyps, plan_time,
                   registry={"lines": jsonl(reg_path), "anchors": anchors})
    out = a.out or os.path.join(a.run_dir, "analysis")
    os.makedirs(out, exist_ok=True)
    for name, doc in (("world_model.yaml", wm), ("hypotheses.yaml", hy)):      # no-follow, atomic (lib.safe_io)
        _safe_io().write_nofollow(out, name, yaml.safe_dump(_rounded(json.loads(json.dumps(doc))), sort_keys=False,
                                                           width=110).encode(), atomic=True)
    st = {}
    for h in hy["hypotheses"]:
        st[h["status"]] = st.get(h["status"], 0) + 1
    print(f"wrote {out}/world_model.yaml and hypotheses.yaml: {len(wm['beliefs'])} beliefs, hypotheses {st}")
    return 0


# ---------------------------------------------------------------- self-test
EVIDENCE_TIME, REGISTERED = "2026-01-01T00:00:00+00:00", "2025-12-01T00:00:00+00:00"   # synthetic sweeps and seeds


def _head(ledger, at):
    """A recorded ledger head (what the harness writes into run_state.json history at a step's start)."""
    return {"at": at, "lines": len(ledger), "head": ledger[-1]["line_sha"], "source": "test"}


def _anch(ledger, at=None):
    return {"lines": ledger, "anchors": [_head(ledger, at or REGISTERED)]}


def _bind(d):
    """A completed analysis manifest listing the run's point.yaml with the current batch (design, results)."""
    import yaml
    from toolkit.analysis.artifacts import sha256
    b = os.path.join(d, "analysis", "batch")
    os.makedirs(b, exist_ok=True)
    for name in ("design.json", "results.jsonl"):
        if not os.path.exists(os.path.join(b, name)):
            open(os.path.join(b, name), "w").write("{}\n")
    yaml.safe_dump({"written_at": EVIDENCE_TIME, "inputs": {
        f"analysis/batch/{n}": sha256(os.path.join(b, n)) for n in ("design.json", "results.jsonl")}},
        open(os.path.join(d, "analysis", "batch.yaml"), "w"))
    json.dump({"complete": True, "status": "complete", "settings_sha256": sha256(os.path.join(d, "analysis_settings.yaml")),
               "artifacts": {rel: {"sha256": sha256(os.path.join(d, rel))}
                             for rel in ("analysis/point.yaml", "analysis/batch.yaml")}},
              open(os.path.join(d, "analysis", "manifest.json"), "w"))


def _sw(feats, rA, rB, cA=None, cB=None, burden=None, lam=0.1):
    """A sweep with packages none, A (component a) and B (component b, same group): relative reductions rA, rB."""
    n = len(rA)
    one = np.ones(n)
    bu = one if burden is None else np.asarray(burden, float)
    sw = Sweep(feats, {"none": [], "A": ["a"], "B": ["b"]}, "none", bu,
               {"none": bu, "A": bu * (1 - np.asarray(rA)), "B": bu * (1 - np.asarray(rB))},
               {"none": 0 * one, "A": one if cA is None else cA * one, "B": one if cB is None else cB * one},
               groups={"a": "g", "b": "g"}, points=list(range(n)), artifacts={"results": "synthetic"}, lam=lam,
               evidence_time=EVIDENCE_TIME)
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
            {"id": "H1", "registered_at": REGISTERED, "belief": "B1",
             "claim": "A's effect is in [0.2, 0.4] at 80% of points",
             "test": {"kind": "effect_band", "package": "A", "band": [0.2, 0.4], "min_share": 0.8}},
            {"id": "H2", "registered_at": REGISTERED, "belief": "B4", "claim": "A beats B wherever f1 is above 0.7",
             "test": {"kind": "paired_sign", "a": "A", "b": "B", "feature": "f1", "range": [0.7, 1.0],
                      "sign": "positive", "min_share": 0.8}},
            {"id": "H3", "registered_at": REGISTERED, "belief": "B3",
             "claim": "the A/B choice flips for f1 in [0.3, 0.7]",
             "test": {"kind": "flip_in", "a": "A", "b": "B", "feature": "f1", "range": [0.3, 0.7]}},
            {"id": "H4", "registered_at": REGISTERED, "belief": "B2", "claim": "B's effect increases with f1",
             "test": {"kind": "effect_direction", "package": "B", "feature": "f1", "direction": "increases"}},
            {"id": "H5", "registered_at": REGISTERED, "belief": "B3", "claim": "most low-f1 units get A",
             "test": {"kind": "plan_share", "package": "A", "where": {"f1": [0.3, 0.5]}, "min_share": 0.8}}]}


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
        wm, hy = build(_seed(), sw, plan=plan, units=units, plan_time=EVIDENCE_TIME,
                       registry=_anch(register(_seed(), [], at=REGISTERED)))
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
        wm2, _ = build(sd, sw, plan=plan, units=units, plan_time=EVIDENCE_TIME,
                       registry=_anch(register(sd, [], REGISTERED)))
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
        s6, _ = _one(alias, {"kind": "flip_in", "a": "A", "b": "B", "feature": "x4", "range": [0.3, 0.7],
                             "scope": "marginal"})
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
        reg = _anch(register(_seed(), [], at=REGISTERED))
        wm1, hy1 = build(_seed(), sw, plan=plan, units=units, plan_time=EVIDENCE_TIME, registry=reg)
        wm2, hy2 = build(wm1, sw, plan=plan, units=units, hypotheses=hy1["hypotheses"], plan_time=EVIDENCE_TIME,
                         registry=reg)
        ids = [b["id"] for b in wm2["beliefs"]]
        ok(len(ids) == len(set(ids)) and wm2.get("version") == 2 and wm2.get("superseded_simulation"),
           f"WM37 rebuilding from a produced world model replaces its simulation beliefs (versioned): {ids}")
        conf = lambda w: {b["id"]: b["confidence"] for b in w["beliefs"] if not b["id"].startswith("sim.")}  # noqa
        ok(wm2["track_record"]["confirmed"] == wm1["track_record"]["confirmed"] and conf(wm2) == conf(wm1) and
           all(h["confrontation"]["repeat"] for h in hy2["hypotheses"]),
           f"WM38 replaying the same evidence counts once and moves no confidence: {wm2['track_record']}")
        wm3, _ = build(wm2, sw, plan=plan, units=units, hypotheses=hy2["hypotheses"], plan_time=EVIDENCE_TIME,
                       registry=reg)
        ok(len({b["id"] for b in wm3["beliefs"]}) == len(wm3["beliefs"]), "WM37b the output validates on reuse")
    tryit("WM37-38 reuse and replay", wm37)

    def wm39():
        rng = np.random.default_rng(1)
        units = {str(i): {"f1": float(x), "f2": 0.5} for i, x in enumerate(rng.uniform(0, 1, 40))}
        plan = {u: ("A" if r["f1"] < 0.5 else "B") for u, r in units.items()}
        sd = _seed()
        sd["hypotheses"].append(dict(sd["hypotheses"][0], id="H1b"))
        wa, _ = build(sd, sw, plan=plan, units=units, plan_time=EVIDENCE_TIME,
                       registry=_anch(register(sd, [], REGISTERED)))
        ok(wa["track_record"]["confirmed"] == 4, f"WM39 an alias (identical test, same evidence) counts once: "
                                                 f"{wa['track_record']['confirmed']}")
        sd = _seed()
        sd["track_record"] = {"confirmed": 99, "contradicted": 0, "history": []}
        wr, _ = build(sd, sw, plan=plan, units=units, plan_time=EVIDENCE_TIME,
                       registry=_anch(register(sd, [], REGISTERED)))
        ok(wr["track_record"]["confirmed"] == 4 and wr["track_record"].get("reconciled"),
           f"WM40 seed counters are recomputed from the confrontations, not trusted: {wr['track_record']}")
        sd = _seed()
        reg1 = register(dict(sd, hypotheses=[h for h in sd["hypotheses"] if h["id"] not in ("H1", "H4")]), [],
                        at=REGISTERED)
        reg2 = register(dict(sd, hypotheses=[sd["hypotheses"][0]]), reg1, at="2026-02-01T00:00:00+00:00")
        reg = {"lines": reg2, "anchors": [_head(reg1, REGISTERED), _head(reg2, "2026-02-01T00:00:00+00:00")]}
        wp, hp = build(sd, sw, plan=plan, units=units, plan_time=EVIDENCE_TIME, registry=reg)
        tr = wp["track_record"]
        ok(tr["confirmed"] == 2 and (tr.get("post_hoc") or {}).get("confirmed") == 2 and
           [h["confrontation"]["post_hoc"] for h in hp["hypotheses"]] == [True, False, False, True, False],
           f"WM41 a hypothesis registered after its evidence, or not in the ledger, is post hoc: reported "
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

    def wm44():
        units = {str(i): {"age": str(v)} for i, v in enumerate([20, 30] + [25] * 9)}
        plan = {u: ("A" if units[u]["age"] in ("20", "30") else "B") for u in units}
        t = {"kind": "plan_share", "component": "a", "where": {"age": [20, 30]}, "min_share": 0.8}
        st, res = _one(sw, t, plan=plan, units=units)
        ok(st == "refuted" and res["observed"].get("units") == 11, "WM44 a numeric filter over CSV text matches the "
           f"whole range, not only its endpoints (2 of 11 units get A): {st} {res['observed']}")
        units["0"]["age"] = "unknown"
        try:
            _one(sw, t, plan=plan, units=units)
            ok(False, "WM44b a non-numeric value under a numeric filter fails closed")
        except WorldModelError:
            ok(True, "WM44b a non-numeric value under a numeric filter fails closed")
    tryit("WM44 numeric filters", wm44)

    def wm45():
        point = {"per_unit": {"1": {"package": "B", "benefit": 0.3, "cost": 2.0}}, "solver": {"status": "optimal"}}
        ok(shadow_price(point)[0] is None, "WM45 no shadow price is inferred from average benefit / cost: without a "
                                           f"recorded dual it is none: {shadow_price(point)}")
        point["solver"]["budget_dual"] = 0.07
        ok(shadow_price(point)[0] == 0.07, f"WM45b a recorded dual is used: {shadow_price(point)}")
        point["solver"] = {"budget_slack": True}
        ok(shadow_price(point)[0] == 0.0, f"WM45c lambda is 0 only when the plan says its budget is slack: "
                                          f"{shadow_price(point)}")
    tryit("WM45 shadow price", wm45)

    def wm46():
        x = np.linspace(1000, 1001, 200)
        cs = _sw({"x": x}, np.where(x < 1000.5, 0.5, 0.2), 0.35 + 0 * x)
        st, res = _one(cs, {"kind": "flip_in", "a": "A", "b": "B", "feature": "x", "range": [1000.0, 1000.0],
                            "scope": "marginal"})
        ok(st != "supported", f"WM46 verdicts compare full-precision values (a flip at 1000.5 is not 'at 1000'): {st} "
                              f"{res['observed']}")
        x2 = np.linspace(0, 1, 20)
        cs2 = _sw({"x": x2}, np.where(x2 < 0.5, 0.5, 0.2), 0.35 + 0 * x2)
        st2, res2 = _one(cs2, {"kind": "flip_in", "a": "A", "b": "B", "feature": "x", "range": [0.467, 0.533],
                               "scope": "marginal"})
        ok(st2 != "supported", f"WM46b a claim equal to the rounded range is not inside the computed one: {st2} "
                               f"{res2['observed']}")
    tryit("WM46 precision", wm46)

    def wm47():
        rng = np.random.default_rng(21)
        x = rng.uniform(0, 1, 4000)
        cs = _sw({"x": x}, 0.3 + 0 * x, 0.3 + 0.01 * (x + 1.9 * rng.standard_normal(4000)))
        t = {"kind": "effect_direction", "package": "B", "feature": "x", "direction": "negligible"}
        s1, r1 = _one(cs, dict(t, equivalence=0.3))
        s2, r2 = _one(cs, dict(t, equivalence=0.16))
        ok(s1 == "supported" and s2 == "inconclusive", "WM47 an equivalence claim is judged on its bound, whatever the "
           f"significance (interval inside 0.3: supported; crossing 0.16: inconclusive): {s1} "
           f"{r1['observed'].get('ci')}"
           f", {s2}")
    tryit("WM47 equivalence", wm47)

    def wm48():
        rng = np.random.default_rng(3)
        base = np.array([0.0] * 23 + [1.0, 2.0])
        heavy = sum(direction(base, rng.permutation(base))["direction"] in ("increases", "decreases")
                    for _ in range(600)) / 600
        lv = np.repeat([0.0, 1.0, 2.0, 3.0], 10)
        mod = sum(direction(lv, rng.permutation(lv))["direction"] in ("increases", "decreases")
                  for _ in range(600)) / 600
        ok(heavy <= 0.05 and mod <= 0.08, f"WM48 directions on tied data are calibrated under the null (heavily tied: "
                                          f"{heavy:.3f}; four levels: {mod:.3f})")
    tryit("WM48 ties", wm48)

    def wm49():
        import yaml
        from toolkit.analysis.artifacts import sha256
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "analysis"))
            open(os.path.join(d, "decision_spec.yaml"), "w").write("x: 1\n")
            open(os.path.join(d, "analysis", "calibration.yaml"), "w").write("c: 1\n")
            ident = {"name": "e", "version": "1"}
            good = {"spec_sha256": sha256(os.path.join(d, "decision_spec.yaml")),
                    "inputs": {"analysis/calibration.yaml": sha256(os.path.join(d, "analysis", "calibration.yaml"))},
                    "data": {"engine_identity": ident, "plan": {}}}
            pth = os.path.join(d, "analysis", "point.yaml")
            yaml.safe_dump(good, open(pth, "w"))
            _bind(d)
            ok(point_doc(d, ident)["data"]["plan"] == {}, "WM49 a point plan bound to this run is read")
            for mut, why in ((lambda g: g.update(spec_sha256="0" * 64), "another spec"),
                             (lambda g: g["data"].update(engine_identity={"name": "e", "version": "2"}),
                              "another engine identity"),
                             (lambda g: g["inputs"].update({"analysis/calibration.yaml": "0" * 64}), "a changed input"),
                             (lambda g: g["inputs"].update({"analysis/gone.yaml": "0" * 64}), "a missing input")):
                bad = json.loads(json.dumps(good))
                mut(bad)
                yaml.safe_dump(bad, open(pth, "w"))
                _bind(d)
                try:
                    point_doc(d, ident)
                    ok(False, f"WM49b a stale point plan ({why}) is refused")
                except WorldModelError:
                    ok(True, f"WM49b a stale point plan ({why}) is refused")
    tryit("WM49 stale plan", wm49)

    def wm50():
        x = np.linspace(0, 1, 200)
        cs = _sw({"x": x}, np.where(x < 0.8, 0.3, 0.9), 0.3 + 0 * x)
        lit = [{"citation": "T", "location": "p. 1"}]
        sd = {"beliefs": [{"id": "C1", "mechanism": "A cuts the outcome by 20-40% at high x", "about": {"package": "A"},
                           "direction": "reduces", "magnitude": {"band": [0.2, 0.4], "scale": "relative_reduction"},
                           "conditions": [{"feature": "x", "range": [0.8, 1.0]}], "source_type": "literature",
                           "evidence": lit, "confidence": "low"}],
              "hypotheses": [{"id": "K1", "belief": "C1", "claim": "A's effect is 20-40%", "registered_at": REGISTERED,
                              "test": {"kind": "effect_band", "package": "A", "band": [0.2, 0.4],
                                       "min_share": 0.75}}]}
        _, hy = build(sd, cs)
        ok(hy["hypotheses"][0]["status"] == "refuted", "WM50 a belief's conditions constrain its hypothesis' check "
                                                       f"(every point at x >= 0.8 fails the band): "
                                                       f"{hy['hypotheses'][0]['status']}")
        sd2 = {"beliefs": [{"id": "C2", "mechanism": "A beats B at low x", "about": {"pair": ["A", "B"]},
                            "direction": "none", "conditions": [{"feature": "x", "range": [0.0, 0.4]}],
                            "source_type": "literature", "evidence": lit, "confidence": "low"}],
               "hypotheses": [{"id": "K2", "belief": "C2", "claim": "A beats B", "registered_at": REGISTERED,
                               "test": {"kind": "paired_sign", "a": "A", "b": "B", "feature": "x", "range": [0.0, 1.0],
                                        "where": {"x": [0.8, 1.0]}, "sign": "positive", "min_share": 0.8}}]}
        try:
            validate_seed(sd2)
            ok(False, "WM50b a test whose intersected region lies outside its belief's conditions is refused")
        except WorldModelError:
            ok(True, "WM50b a test whose intersected region lies outside its belief's conditions is refused")
    tryit("WM50 belief conditions", wm50)

    def wm51():
        from toolkit.spec import schema
        with tempfile.TemporaryDirectory() as d:
            spec = schema.load(schema.toy_spec(d))
            got = consumed_inputs(spec, d)
            ok("units.csv" in got and all(isinstance(v, str) and len(v) == 64 for v in got.values()),
               f"WM51 the units table and every data or settings file consumed are fingerprinted: {sorted(got)}")
    tryit("WM51 inputs", wm51)

    def wm52():
        for wh, kind, why in (({"f1": [0, 1, float("nan")]}, "effect_band", "a three-element region with NaN"),
                              ({"f1": [0.5, 0.2]}, "effect_band", "an unordered region"),
                              ({"f1": "low"}, "effect_band", "a text region on a sweep test"),
                              ({"f1": [True, 1]}, "effect_band", "a boolean bound"),
                              ({"region": ["north", 3]}, "plan_share", "a categorical filter with a number in it")):
            sd = _seed()
            h = sd["hypotheses"][0] if kind == "effect_band" else sd["hypotheses"][4]
            h["test"]["where"] = wh
            try:
                validate_seed(sd)
                ok(False, f"WM52 validation refuses {why}")
            except WorldModelError:
                ok(True, f"WM52 validation refuses {why}")
    tryit("WM52 regions", wm52)

    def wm53():
        one, x = np.ones(30), np.linspace(0, 1, 30)
        col = Sweep({"x": x}, {"none": [], "A": ["a"], "a": ["b"]}, "none", one,
                    {"none": one, "A": 0.7 * one, "a": 0.8 * one}, {"none": 0 * one, "A": one, "a": one},
                    points=list(range(30)), lam=0.1, evidence_time=EVIDENCE_TIME)
        units = {str(i): {"x": float(v)} for i, v in enumerate(x)}
        plan = {u: "a" for u in units}
        s1, r1 = _one(col, {"kind": "plan_share", "component": "a", "where": {"x": [0, 1]}, "min_share": 0.8},
                      plan=plan, units=units)
        s2, _ = _one(col, {"kind": "plan_share", "package": "a", "where": {"x": [0, 1]}, "min_share": 0.8},
                     plan=plan, units=units)
        ok(s1 == "refuted" and s2 == "supported", "WM53 a component test inspects package composition (no package "
                                                  f"named 'a' contains component a), a package test the package: {s1} "
                                                  f"{r1['observed']}, {s2}")
    tryit("WM53 component and package", wm53)

    def wm54():
        rng = np.random.default_rng(1)
        units = {str(i): {"f1": float(x), "f2": 0.5} for i, x in enumerate(rng.uniform(0, 1, 40))}
        plan = {u: ("A" if r["f1"] < 0.5 else "B") for u, r in units.items()}
        wn, hn = build(_seed(), sw, plan=plan, units=units, plan_time=EVIDENCE_TIME)
        ok(all(h["confrontation"]["post_hoc"] for h in hn["hypotheses"]) and wn["track_record"]["confirmed"] == 0,
           "WM54a a registered_at written in the seed does not pre-register; only the registration ledger does: "
           f"{wn['track_record']['confirmed']} counted")
        sd = _seed()
        sd["hypotheses"][0]["test"]["band"] = [0.7, 0.9]                  # registered: a band the sweep refutes
        reg = _anch(register(sd, [], at=REGISTERED))
        wm0, hy0 = build(sd, sw, plan=plan, units=units, plan_time=EVIDENCE_TIME, registry=reg)
        ok(hy0["hypotheses"][0]["status"] == "refuted" and not hy0["hypotheses"][0]["confrontation"]["post_hoc"],
           "WM54 a hypothesis whose test is in the registration ledger before its evidence is pre-registered")
        sd["hypotheses"][0]["test"]["band"] = [0.2, 0.4]                  # edited after seeing the result
        wm1, hy1 = build(sd, sw, plan=plan, units=units, plan_time=EVIDENCE_TIME, registry=reg)
        h1 = hy1["hypotheses"][0]
        ok(h1["status"] == "supported" and h1["confrontation"]["post_hoc"] and wm1["track_record"]["confirmed"] == 3,
           f"WM54b an edit after registration is post hoc, whatever its registered_at: {h1['confrontation']} "
           f"{wm1['track_record']['confirmed']}")
        late = _anch(register(_seed(), [], at="2026-02-01T00:00:00+00:00"), "2026-02-01T00:00:00+00:00")
        _, hy2 = build(_seed(), sw, plan=plan, units=units, plan_time=EVIDENCE_TIME, registry=late)
        ok(all(h["confrontation"]["post_hoc"] for h in hy2["hypotheses"]),
           "WM54c a ledger entry dated after the evidence does not pre-register")
        ok(wm1["track_record"].get("provisional"), "WM59 the track record says it is provisional")
    tryit("WM54 registration ledger", wm54)

    def wm55():
        reg = register(_seed(), [], at=REGISTERED)
        ok(register(_seed(), reg, at="2025-12-02T00:00:00+00:00") == reg and len(verify_ledger(reg)) == 5,
           "WM55 registering the same tests again adds nothing; the ledger verifies")
        for mut, why in ((lambda L: L[1].update(registered_at="2025-01-01T00:00:00+00:00"), "an edited line"),
                         (lambda L: L.reverse(), "reordered lines"),
                         (lambda L: L.pop(1), "a removed line")):
            bad = json.loads(json.dumps(reg))
            mut(bad)
            try:
                verify_ledger(bad)
                ok(False, f"WM55b a ledger with {why} is refused")
            except WorldModelError:
                ok(True, f"WM55b a ledger with {why} is refused")
        try:
            verify_ledger(register(dict(_seed(), hypotheses=[dict(_seed()["hypotheses"][0], id="H9", test=dict(
                _seed()["hypotheses"][0]["test"], min_share=0.9))]), reg, at="2025-11-01T00:00:00+00:00"))
            ok(False, "WM55c an appended line dated before the line it follows (backdating) is refused")
        except WorldModelError:
            ok(True, "WM55c an appended line dated before the line it follows (backdating) is refused")
    tryit("WM55 ledger chain", wm55)

    def wm56():
        b = _seed()["beliefs"][1]
        t = {"kind": "effect_direction", "package": "B", "feature": "f1", "direction": "increases"}
        ok(canonical_test(t, b) == canonical_test(dict(t, scope="conditional"), b) and
           canonical_test(dict(_seed()["hypotheses"][0]["test"], min_share=1), None) ==
           canonical_test(dict(_seed()["hypotheses"][0]["test"], min_share=1.0), None),
           "WM56 canonical tests resolve defaults and number spellings (an explicit default is the same test)")
        sd = _seed()
        sd["hypotheses"][0]["test"]["unused_note"] = "x"
        try:
            validate_seed(sd)
            ok(False, "WM56b a test with an unknown field is refused")
        except WorldModelError:
            ok(True, "WM56b a test with an unknown field is refused")
        rng = np.random.default_rng(1)
        units = {str(i): {"f1": float(x), "f2": 0.5} for i, x in enumerate(rng.uniform(0, 1, 40))}
        plan = {u: ("A" if r["f1"] < 0.5 else "B") for u, r in units.items()}
        sd = _seed()
        sd["hypotheses"].append(dict(sd["hypotheses"][3], id="H4b", test=dict(sd["hypotheses"][3]["test"],
                                                                                scope="conditional")))
        wa, _ = build(sd, sw, plan=plan, units=units, plan_time=EVIDENCE_TIME,
                       registry=_anch(register(sd, [], REGISTERED)))
        ok(wa["track_record"]["confirmed"] == 4, f"WM56c an alias spelled with an explicit default counts once: "
                                                 f"{wa['track_record']['confirmed']}")
    tryit("WM56 canonical tests", wm56)

    def wm57():
        import yaml
        from toolkit.analysis.artifacts import sha256
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "analysis"))
            put = lambda rel, txt: open(os.path.join(d, rel), "w").write(txt)  # noqa: E731
            put("decision_spec.yaml", "x: 1\n")
            put("units.csv", "id,region\n1,north\n")
            cal = {"inputs": {"units.csv": sha256(os.path.join(d, "units.csv"))}, "data": {}}
            yaml.safe_dump(cal, open(os.path.join(d, "analysis", "calibration.yaml"), "w"))
            ident = {"name": "e"}
            pt = {"spec_sha256": sha256(os.path.join(d, "decision_spec.yaml")),
                  "inputs": {"analysis/calibration.yaml": sha256(os.path.join(d, "analysis", "calibration.yaml"))},
                  "data": {"engine_identity": ident, "plan": {}}}
            pth = os.path.join(d, "analysis", "point.yaml")
            yaml.safe_dump(pt, open(pth, "w"))
            _bind(d)
            ok(point_doc(d, ident) is not None, "WM57 a plan whose inputs verify all the way down is used")
            put("units.csv", "id,region\n1,south\n")
            try:
                point_doc(d, ident)
                ok(False, "WM57b a plan whose calibration's units table changed is refused (transitive inputs)")
            except WorldModelError:
                ok(True, "WM57b a plan whose calibration's units table changed is refused (transitive inputs)")
            for inp, why in (({}, "an empty inputs mapping"), (None, "no inputs mapping")):
                yaml.safe_dump(dict(pt, inputs=inp), open(pth, "w"))
                _bind(d)
                try:
                    point_doc(d, ident)
                    ok(False, f"WM57c a plan with {why} is refused")
                except WorldModelError:
                    ok(True, f"WM57c a plan with {why} is refused")
    tryit("WM57 transitive provenance", wm57)

    def wm58():
        d = direction(np.arange(10.0), np.array([0, 2, 1, 3, 4, 6, 5, 9, 7, 8], float))
        ok(1e-4 <= d["p"] <= 4.2e-4 and d.get("p_method") == "permutation",
           f"WM58 a small sample's p-value is a permutation p-value (exact 2.06e-4, not Fisher's 4.55e-6): {d}")
        x = np.linspace(0, 1, 60)
        d2 = direction(x, x + 0.3 * np.random.default_rng(4).standard_normal(60))
        ok(d2.get("p_method") == "fisher", f"WM58b from {SETTINGS.get('perm_n_max')} points on, Fisher: {d2}")
    tryit("WM58 small samples", wm58)

    def wm67():
        rng = np.random.default_rng(1)
        units = {str(i): {"f1": float(x), "f2": 0.5} for i, x in enumerate(rng.uniform(0, 1, 40))}
        plan = {u: ("A" if r["f1"] < 0.5 else "B") for u, r in units.items()}
        kw = dict(plan=plan, units=units, plan_time=EVIDENCE_TIME)
        sd = _seed()
        sd["hypotheses"][0]["test"]["band"] = [0.7, 0.9]
        reg = register(sd, [], at=REGISTERED)
        forged = json.loads(json.dumps(reg))            # after seeing the result: a winning band, every hash recomputed
        forged[0]["test"]["band"] = [0.2, 0.4]
        prev = "0" * 64
        for e in forged:
            e["test_sha"], e["prev"] = _sha(e["test"]), prev
            e["line_sha"] = _line_sha(e)
            prev = e["line_sha"]
        sd2 = _seed()
        _, h0 = build(sd2, sw, registry=forged, **kw)
        ok(h0["hypotheses"][0]["confrontation"]["post_hoc"], "WM67 a ledger with no recorded head pre-registers "
                                                             "nothing "
                                                             "(a self-attesting, rehashed rewrite counts post hoc)")
        for lines, why in ((forged, "a rehashed rewrite"), (reg[:3], "a truncated ledger")):
            try:
                build(sd2, sw, registry={"lines": lines, "anchors": [_head(reg, REGISTERED)]}, **kw)
                ok(False, f"WM67b {why} that does not extend the recorded head is refused")
            except WorldModelError:
                ok(True, f"WM67b {why} that does not extend the recorded head is refused")
        later = register(dict(sd2, hypotheses=[dict(sd2["hypotheses"][0], id="H1x")]), reg, at=REGISTERED)
        _, h1 = build(dict(sd2, hypotheses=[dict(sd2["hypotheses"][0], id="H1x")]), sw,
                      registry={"lines": later, "anchors": [_head(reg, REGISTERED)]}, **kw)
        ok(h1["hypotheses"][0]["confrontation"]["post_hoc"], "WM67c a line appended after the last recorded head, even "
                                                             "dated early, is post hoc")
    tryit("WM67 anchored ledger", wm67)

    def wm68():
        import subprocess
        with tempfile.TemporaryDirectory() as d:
            work, remote = os.path.join(d, "work"), os.path.join(d, "remote.git")
            vcs = lambda c, *a: subprocess.run(["git", "-C", c, *a], capture_output=True, text=True)  # noqa: E731
            subprocess.run(["git", "init", "-q", "--bare", remote], capture_output=True)
            os.makedirs(work)
            vcs(work, "init", "-q")
            vcs(work, "config", "user.email", "t@t")
            vcs(work, "config", "user.name", "t")
            reg = register(_seed(), [], at=REGISTERED)
            path = os.path.join(work, "hypotheses_registry.jsonl")
            open(path, "w").write("".join(json.dumps(e, sort_keys=True) + "\n" for e in reg))
            vcs(work, "add", ".")
            vcs(work, "commit", "-qm", "register")
            vcs(work, "remote", "add", "origin", remote)
            vcs(work, "push", "-q", "origin", "HEAD:refs/heads/main")
            vcs(work, "fetch", "-q", "origin")
            more = register(dict(_seed(), hypotheses=[dict(_seed()["hypotheses"][0], id="H9", test=dict(
                _seed()["hypotheses"][0]["test"], min_share=0.9))]), reg, at=REGISTERED)
            open(path, "w").write("".join(json.dumps(e, sort_keys=True) + "\n" for e in more))
            vcs(work, "commit", "-qam", "local only")
            an = git_anchors(path, "origin/main")
            ok(len(an) == 1 and an[0]["lines"] == 5 and an[0]["source"].startswith("anchored in remote git (commit ")
               and len(an[0]["commit"]) == 40, f"WM68 a ledger head in a commit on the fetched remote is an anchor; an "
                                               f"unpushed commit is not: {an}")
    tryit("WM68 git anchor", wm68)

    def wm69():
        import yaml
        from toolkit.analysis.artifacts import sha256
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "analysis"))
            open(os.path.join(d, "decision_spec.yaml"), "w").write("x: 1\n")
            ident = {"name": "e"}
            base = {"spec_sha256": sha256(os.path.join(d, "decision_spec.yaml")), "data": {"engine_identity": ident}}
            pth = os.path.join(d, "analysis", "point.yaml")
            yaml.safe_dump(dict(base, inputs={"missing.csv": None}), open(pth, "w"))
            _bind(d)
            try:
                point_doc(d, ident)
                ok(False, "WM69 a missing input recorded with a null hash is refused")
            except WorldModelError:
                ok(True, "WM69 a missing input recorded with a null hash is refused")
            yaml.safe_dump({"schema": "analysis.calibration/1", "data": {}},
                           open(os.path.join(d, "analysis", "calibration.yaml"), "w"))
            yaml.safe_dump(dict(base, inputs={"analysis/calibration.yaml": sha256(os.path.join(
                d, "analysis", "calibration.yaml"))}), open(pth, "w"))
            _bind(d)
            try:
                point_doc(d, ident)
                ok(False, "WM69b a harness artifact (schema analysis.*) without inputs is refused, not a leaf")
            except WorldModelError:
                ok(True, "WM69b a harness artifact (schema analysis.*) without inputs is refused, not a leaf")
    tryit("WM69 provenance leaves", wm69)

    def wm70():
        import yaml
        ident = {"name": "e", "version": "1", "code_sha256": "c" * 64, "params_sha256": "p" * 64,
                 "config_sha256": "k" * 64}
        with tempfile.TemporaryDirectory() as d:
            yaml.safe_dump({"engine_identity": ident}, open(os.path.join(d, "results.yaml"), "w"))
            for k in ("name", "params_sha256", "config_sha256"):
                try:
                    benchmark_record(d, dict(ident, **{k: "x"}))
                    ok(False, f"WM70 a recorded identity differing in {k} does not match the benchmark")
                except WorldModelError:
                    ok(True, f"WM70 a recorded identity differing in {k} does not match the benchmark")
    tryit("WM70 full identity", wm70)

    def wm71():
        sd = _seed()
        sd["hypotheses"][3]["test"]["equivalence"] = 0.2
        try:
            validate_seed(sd)
            ok(False, "WM71 an equivalence bound on an increases/decreases claim is refused (it changes nothing)")
        except WorldModelError:
            ok(True, "WM71 an equivalence bound on an increases/decreases claim is refused (it changes nothing)")
        sd = _seed()
        sd["beliefs"][2]["conditions"] = [{"feature": "f1", "range": [0.0, 0.2]},
                                          {"feature": "f1", "range": [0.8, 1.0]}]
        try:
            validate_seed(sd)
            ok(False, "WM72 repeated conditions on one feature are intersected; an empty intersection is refused")
        except WorldModelError:
            ok(True, "WM72 repeated conditions on one feature are intersected; an empty intersection is refused")
        b = {"conditions": [{"feature": "f1", "range": [0.0, 0.6]}, {"feature": "f1", "range": [0.4, 1.0]}]}
        c = canonical_test({"kind": "effect_band", "package": "A", "band": [0.2, 0.4], "min_share": 0.8}, b)
        ok(c["where"] == {"f1": [0.4, 0.6]}, f"WM72b two conditions on one feature apply as their intersection: {c}")
        bn = {"conditions": [{"feature": "f1", "range": [0.0, 0.5]}]}
        c2 = canonical_test({"kind": "plan_share", "package": "A", "where": {"region": "north"}, "min_share": 0.8}, bn)
        ok(c2["where"]["region"] == "north", f"WM73 a categorical filter stays intact when conditions apply: {c2}")
        units = {str(i): {"f1": 0.1 * i} for i in range(10)}
        st, _ = _one(sw, {"kind": "plan_share", "package": "A", "where": {}, "min_share": 0.8},
                     plan={u: "A" for u in units}, units=units)
        ok(st == "supported", f"WM74 plan_share over every unit (where: {{}}) works after canonicalisation: {st}")
    tryit("WM71-74 canonical tests and conditions", wm71)

    def wm75():
        import subprocess
        import yaml
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "analysis"))
            paths = []
            for k in range(4):
                sd = _seed()
                for h in sd["hypotheses"]:
                    h["id"] += f"_{k}"
                    if "min_share" in h["test"]:
                        h["test"]["min_share"] = 0.81 + 0.01 * k
                paths.append(os.path.join(d, f"seed{k}.yaml"))
                yaml.safe_dump(sd, open(paths[-1], "w"))
            procs = [subprocess.Popen([sys.executable, os.path.abspath(__file__), "register", q, d],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for q in paths]
            codes = [q.wait() for q in procs]
            lines = [json.loads(x) for x in open(os.path.join(d, LEDGER)) if x.strip()]
            try:
                verify_ledger(lines)
                good = True
            except WorldModelError:
                good = False
            ok(codes == [0] * 4 and good and len(lines) == 2 + 3 * 4, f"WM75 concurrent registrations hold one lock: "
                                                                      f"the ledger verifies ({len(lines)} lines, "
                                                                      f"exit codes {codes})")
    tryit("WM75 registration lock", wm75)

    def wm76():
        x = np.arange(10.0)
        cs = _sw({"x": x}, np.where(x >= 8, 0.5, 0.2), 0.35 + 0 * x)
        fa = flip_along(cs, "A", "B", "x")
        ok(0.03 <= (fa.get("p") or 0) <= 0.06 and fa.get("p_method") == "permutation",
           f"WM76 a small-sample flip's p-value is a permutation p-value (exact 0.0444, not the logistic 0.0026): {fa}")
    tryit("WM76 small-sample flips", wm76)

    def wm77():
        import subprocess
        with tempfile.TemporaryDirectory() as d:
            work, remote = os.path.join(d, "work"), os.path.join(d, "remote.git")
            vcs = lambda c, *a: subprocess.run(["git", "-C", c, *a], capture_output=True, text=True)  # noqa: E731
            subprocess.run(["git", "init", "-q", "--bare", remote], capture_output=True)
            os.makedirs(work)
            vcs(work, "init", "-q", "-b", "main")
            vcs(work, "config", "user.email", "t@t")
            vcs(work, "config", "user.name", "t")
            reg = register(_seed(), [], at=REGISTERED)
            path = os.path.join(work, "hypotheses_registry.jsonl")
            open(path, "w").write("".join(json.dumps(e, sort_keys=True) + "\n" for e in reg))
            vcs(work, "add", ".")
            vcs(work, "commit", "-qm", "register")
            vcs(work, "remote", "add", "origin", remote)
            vcs(work, "push", "-q", "origin", "HEAD:refs/heads/main")
            vcs(work, "fetch", "-q", "origin")
            sha = vcs(work, "rev-parse", "HEAD").stdout.strip()
            for ref in ("HEAD", "main", sha):
                try:
                    git_anchors(path, ref)
                    ok(False, f"WM77 --anchor-ref {ref[:12]} (not a remote-tracking ref) is refused")
                except WorldModelError:
                    ok(True, f"WM77 --anchor-ref {ref[:12]} (not a remote-tracking ref) is refused")
            an = git_anchors(path, "origin/main")
            ok(an and an[0]["source"].startswith("anchored in remote git (commit ") and
               "the commit time is the committer's clock" in an[0]["source"],
               f"WM77b a fetched remote-tracking ref anchors, labelled as remote git: {an[:1]}")
    tryit("WM77 remote anchors only", wm77)

    def wm78():
        with tempfile.TemporaryDirectory() as d:
            head = {"at": "2025-01-01T00:00:00+00:00", "lines": 1, "head": "0" * 64}
            json.dump({"history": [{"registry_head": head}]},
                      open(os.path.join(d, "run_state.json"), "w"))
            ok(cli_anchors(d, os.path.join(d, LEDGER), None) == [],
               "WM78 run_state.json heads are not anchors (v2 does not hash-protect run_state.json): none without a "
               "remote ref")
        rng = np.random.default_rng(1)
        units = {str(i): {"f1": float(x), "f2": 0.5} for i, x in enumerate(rng.uniform(0, 1, 40))}
        plan = {u: ("A" if r["f1"] < 0.5 else "B") for u, r in units.items()}
        wm, hy = build(_seed(), sw, plan=plan, units=units, plan_time=EVIDENCE_TIME,
                       registry=_anch(register(_seed(), [], at=REGISTERED)))
        st = {h["confrontation"].get("registration_status") for h in hy["hypotheses"]}
        ok(st == {"remote-anchored"} and wm["track_record"].get("tamper_proof") is False and
           "not tamper-proof" in wm["track_record"].get("note", ""),
           f"WM79 the output says each registration is remote-anchored or post hoc and that the track record is not "
           f"tamper-proof: {st} {wm['track_record'].get('note')}")
    tryit("WM78-79 anchors and statements", wm78)

    def wm80():
        rng = np.random.default_rng(13)
        feats = {f"n{k:03d}": rng.uniform(0, 1, 30) for k in range(100)}
        null = _sw(feats, 0.3 + 0.02 * rng.standard_normal(30), 0.3 + 0.02 * rng.standard_normal(30))
        mu = summarise(null)["multiplicity"]
        ok(mu["tested"] == 300 and mu["discoveries"] == 0, f"WM80 every attempted test enters the false-discovery "
                                                           f"family (failed fits with p = 1): {mu}")
    tryit("WM80 multiplicity family", wm80)

    def wm81():
        x = np.arange(50.0)
        cs = _sw({"x": x}, np.where(x >= 47, 0.5, 0.2), 0.35 + 0 * x)
        fa = flip_along(cs, "A", "B", "x")
        ok(5e-5 <= (fa.get("p") or 0) <= 3e-4 and fa.get("p_method") == "permutation",
           f"WM81 with fewer than 10 winners a flip's p-value is a permutation p-value at any n (50 points, 3 winners: "
           f"exact 1.02e-4, logistic 1.19e-5): {fa.get('p')} {fa.get('p_method')}")
    tryit("WM81 sparse winners", wm81)

    def wm82():
        import yaml
        from toolkit.analysis.artifacts import sha256
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "analysis"))
            open(os.path.join(d, "decision_spec.yaml"), "w").write("x: 1\n")
            open(os.path.join(d, "units.csv"), "w").write("id\n1\n")
            ident = {"name": "e"}
            pt = {"spec_sha256": sha256(os.path.join(d, "decision_spec.yaml")), "data": {"engine_identity": ident},
                  "inputs": {"units.csv": sha256(os.path.join(d, "units.csv"))}}
            yaml.safe_dump(pt, open(os.path.join(d, "analysis", "point.yaml"), "w"))
            try:
                point_doc(d, ident)
                ok(False, "WM82 a point plan the run's completed analysis manifest does not list is refused")
            except WorldModelError:
                ok(True, "WM82 a point plan the run's completed analysis manifest does not list is refused")
            _bind(d)
            ok(point_doc(d, ident) is not None, "WM82b a plan listed by the completed manifest with the current batch "
                                                "is used")
            open(os.path.join(d, "analysis", "batch", "results.jsonl"), "w").write("{\"i\": 1}\n")
            try:
                point_doc(d, ident)
                ok(False, "WM82c a plan whose manifest names another batch (a rerun replaced the results) is refused")
            except WorldModelError:
                ok(True, "WM82c a plan whose manifest names another batch (a rerun replaced the results) is refused")
            _bind(d)
            m = json.load(open(os.path.join(d, "analysis", "manifest.json")))
            json.dump(dict(m, complete=False), open(os.path.join(d, "analysis", "manifest.json"), "w"))
            try:
                point_doc(d, ident)
                ok(False, "WM82d a plan from an incomplete analysis is refused")
            except WorldModelError:
                ok(True, "WM82d a plan from an incomplete analysis is refused")
    tryit("WM82 plan bound to the completed analysis", wm82)

    def wm83():
        import yaml
        from toolkit.analysis.artifacts import sha256
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "analysis"))
            open(os.path.join(d, "decision_spec.yaml"), "w").write("x: 1\n")
            open(os.path.join(d, "units.csv"), "w").write("id\n1\n")
            ident = {"name": "e"}
            pt = {"spec_sha256": sha256(os.path.join(d, "decision_spec.yaml")), "data": {"engine_identity": ident},
                  "inputs": {"units.csv": sha256(os.path.join(d, "units.csv"))}}
            yaml.safe_dump(pt, open(os.path.join(d, "analysis", "point.yaml"), "w"))
            _bind(d)
            st = os.path.join(d, "analysis_settings.yaml")
            ok(point_doc(d, ident) is not None, "WM83 no settings recorded and none present: the plan is used")
            for step, why in ((lambda: open(st, "w").write("uncertainty: {draws: 8}\n"), "added"),
                              (lambda: (_bind(d), open(st, "w").write("uncertainty: {draws: 9}\n")),
                               "changed"),
                              (lambda: (open(st, "w").write("uncertainty: {draws: 8}\n"), _bind(d),
                                        os.remove(st)), "removed")):
                step()
                try:
                    point_doc(d, ident)
                    ok(False, f"WM83b analysis settings {why} after the analysis make its plan stale (refused)")
                except WorldModelError:
                    ok(True, f"WM83b analysis settings {why} after the analysis make its plan stale (refused)")

            cal = {"schema": "analysis.calibration/1", "spec_sha256": "0" * 64, "engine": {"name": "e"},
                   "inputs": {"units.csv": sha256(os.path.join(d, "units.csv"))}}
            yaml.safe_dump(cal, open(os.path.join(d, "analysis", "calibration.yaml"), "w"))
            pt2 = dict(pt, engine={"name": "e"}, inputs={"analysis/calibration.yaml": sha256(os.path.join(
                d, "analysis", "calibration.yaml"))})
            yaml.safe_dump(pt2, open(os.path.join(d, "analysis", "point.yaml"), "w"))
            _bind(d)
            for c, why in ((cal, "another spec"), (dict(cal, spec_sha256=pt["spec_sha256"], engine={"name": "f"}),
                                                   "another engine")):
                yaml.safe_dump(c, open(os.path.join(d, "analysis", "calibration.yaml"), "w"))
                yaml.safe_dump(dict(pt2, inputs={"analysis/calibration.yaml": sha256(os.path.join(
                    d, "analysis", "calibration.yaml"))}), open(os.path.join(d, "analysis", "point.yaml"), "w"))
                _bind(d)
                try:
                    point_doc(d, ident)
                    ok(False, f"WM87 a descendant artifact computed under {why} is refused in the provenance walk")
                except WorldModelError:
                    ok(True, f"WM87 a descendant artifact computed under {why} is refused in the provenance walk")
    tryit("WM83, 87 settings and descendant identity", wm83)

    def wm84():
        t = {"kind": "plan_share", "package": "A", "min_share": 0.8}
        ok(canonical_test(dict(t, where={"region": ["north", "south"]})) ==
           canonical_test(dict(t, where={"region": ["south", "north", "south"]})),
           "WM84 category lists canonicalise as sets (sorted, deduplicated)")
        ps = {"kind": "paired_sign", "feature": "f1", "range": [0, 1], "min_share": 0.8}
        ok(canonical_test(dict(ps, a="A", b="B", sign="positive")) ==
           canonical_test(dict(ps, a="B", b="A", sign="negative")),
           "WM84b a paired test and its mirror (b over a, opposite sign) are one test")
        fl = {"kind": "flip_in", "feature": "f1", "range": [0.3, 0.7]}
        ok(canonical_test(dict(fl, a="A", b="B")) == canonical_test(dict(fl, a="B", b="A")),
           "WM84c a flip test is symmetric in its pair")
    tryit("WM84 symmetric and set-valued fields", wm84)

    def wm85():
        x = np.linspace(0, 1, 60)
        mu = summarise(_sw({"x": x}, 0.3 + 0.1 * x, 0.2 + 0 * x, lam=None))["multiplicity"]
        ok(mu["tested"] == 3, f"WM85 a flip not computed for want of a shadow price enters the family at p = 1: {mu}")
    tryit("WM85 missing shadow price", wm85)

    def wm86():
        x = np.linspace(0, 1, 400)
        cs = _sw({"x": x}, np.where(x < 0.5, 0.5, 0.2), 0.35 + 0 * x)
        lit = [{"citation": "T", "location": "p. 1"}]
        sd = {"beliefs": [{"id": "F1", "mechanism": "A beats B at low x", "about": {"pair": ["A", "B"]},
                           "direction": "flips", "conditions": [{"feature": "x", "range": [0.0, 0.1]}],
                           "source_type": "literature", "evidence": lit, "confidence": "low"}],
              "hypotheses": [{"id": "K", "belief": "F1", "claim": "the choice flips",
                              "test": {"kind": "flip_in", "a": "A", "b": "B", "feature": "x", "range": [0.0, 1.0],
                                       "scope": "marginal"}}]}
        _, hy = build(sd, cs)
        ok(hy["hypotheses"][0]["status"] == "refuted", "WM86 a flip belief's location condition constrains its flip "
                                                       f"test's verdict (the flip is at 0.5, the belief says 0-0.1): "
                                                       f"{hy['hypotheses'][0]['status']}")
    tryit("WM86 flip location", wm86)
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
