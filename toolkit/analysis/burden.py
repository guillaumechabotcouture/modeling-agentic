#!/usr/bin/env python3
"""Absolute burden validation (F6 S2-5, critique C7): the continuation arm's burden against external estimates.

    python3 toolkit/analysis/burden.py --self-test

Spec (optional section; checked by toolkit/spec/schema.py, no values in code):

    validation:
      burden:
        csv: data/burden_references.csv   # the reference estimates, under the contract below
        group: region                     # the units-table column the references' groups name (default: the region)
        arm: none                         # the continuation package (default: the comparator)
        daly_parts: {dalys: dalys, deaths: deaths, yll_per_death: {<population group> | all: <number | parameter>}}

Reference CSV contract (COLUMNS): id; group (a value of the group column, or * for every unit); age_band (blank: all
ages, or a decision_units.populations key); outcome (one the spec reports); measure (count | rate, per `per` people of
the band); year (within max_year_gap of the decision window); value, lower, upper (finite, >= 0, lower <= value <=
upper); kind (estimated | reported: reported values are shown for information only, never judged or fitted); source
(the citation); tolerance (ratio: model / value inside [ratio_low, ratio_high], blank for the manifest default and
never wider than its widest band | overlap: the model interval meets [lower, upper]).

The model value is the arm's annual mean over the decision window (window total / window years), summed over the
group's units. Its interval comes from the uncertainty draws for all-age references; an age-banded reference is the
point value, and the artifact says so. Thresholds: toolkit/analysis/thresholds.yaml `burden`.
"""
from __future__ import annotations

import csv
import math
import os
import re
import sys

import numpy as np
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
PRODUCER = "toolkit/analysis/burden.py"
KEYS = {"csv", "group", "arm", "daly_parts"}
COLUMNS = ("id", "group", "age_band", "outcome", "measure", "per", "year", "value", "lower", "upper", "kind", "source",
           "tolerance", "ratio_low", "ratio_high")
KINDS = ("estimated", "reported")
TOLERANCES = ("ratio", "overlap")
STATUSES = ("within", "outside", "information", "unmatched")


def thresholds() -> dict:
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "thresholds.yaml")) as f:
        return yaml.safe_load(f)["burden"]


def section(raw):
    v = raw.get("validation") if isinstance(raw, dict) else None
    return v.get("burden") if isinstance(v, dict) else None


def _num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def config(s) -> dict:
    b = section(s.raw) or {}
    return {"csv": b.get("csv"), "group": b.get("group") or s["decision_units"].get("region"),
            "arm": b.get("arm") or s.comparator, "daly_parts": b.get("daly_parts")}


def csv_path(s) -> str:
    return s.resolve(config(s)["csv"])


def _band(lo, hi, th) -> tuple:
    d = th["default_ratio_band"]
    return (d[0] if lo is None else lo), (d[1] if hi is None else hi)


def _band_ok(lo, hi, th) -> bool:
    w = th["widest_ratio_band"]
    return lo is not None and hi is not None and w[0] <= lo <= 1.0 <= hi <= w[1]


def load_references(s) -> tuple[list[dict], list[str]]:
    """(references, problems) for the spec's validation.burden: its keys and the reference CSV contract."""
    v = s.raw.get("validation")
    if v is None:
        return [], []
    if not isinstance(v, dict) or set(v) - {"burden"} or not isinstance(v.get("burden"), dict):
        return [], ["validation: a mapping with `burden` (the only check this section declares)"]
    b, th, P = v["burden"], thresholds(), "validation.burden"
    bad = [f"{P}: unknown key {k}" for k in sorted(set(b) - KEYS)]
    cfg = config(s)
    units = s.units()
    pops = s["decision_units"]["populations"]
    if not cfg["group"] or not units or cfg["group"] not in units[0]:
        bad.append(f"{P}.group: {cfg['group']!r} is not a units-table column")
    if cfg["arm"] not in s.packages():
        bad.append(f"{P}.arm: {cfg['arm']!r} is not a package")
    dp = cfg["daly_parts"]
    if dp is not None:
        y = dp.get("yll_per_death") if isinstance(dp, dict) else None
        names = {p.name for p in s.engine().parameters()}
        if not isinstance(dp, dict) or set(dp) - {"dalys", "deaths", "yll_per_death"} or (
                y is not None and not (isinstance(y, dict) and (set(y) == {"all"} or set(y) == set(pops))
                                       and all((_num(x) or 0) > 0 or x in names for x in y.values()))):
            bad.append(f"{P}.daly_parts: {{dalys, deaths, yll_per_death: {{all or every population group: a positive "
                       "number or an engine parameter}}}}")
    if not isinstance(cfg["csv"], str) or not cfg["csv"]:
        return [], bad + [f"{P}.csv: the reference estimates file"]
    path = s.resolve(cfg["csv"])
    tg = ((s.raw.get("calibration") or {}).get("targets") or {}).get("csv")
    if tg and os.path.abspath(s.resolve(tg)) == os.path.abspath(path):
        bad.append(f"{P}.csv: the calibration targets file; references validate, they are never fitted")
    try:
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except OSError as e:
        return [], bad + [f"{P}.csv: unreadable: {e}"]
    if not rows or set(COLUMNS) - set(rows[0]):
        return [], bad + [f"{P}.csv: needs a row and the columns {', '.join(COLUMNS)}"]
    groups = {str(u.get(cfg["group"])) for u in units} if cfg["group"] and cfg["group"] in units[0] else set()
    outs = s.outcomes()
    t0, t1 = (float(x) for x in s["objective"]["horizon"])
    refs, seen = [], set()
    for n, r in enumerate(rows, 2):
        e = []
        rid = str(r.get("id") or "")
        if not re.fullmatch(r"[A-Za-z0-9_\-]+", rid) or rid in seen:
            e.append("id: unique, letters, digits, _ or -")
        seen.add(rid)
        g, band = str(r["group"]), str(r["age_band"] or "").strip()
        if g != "*" and g not in groups:
            e.append(f"group {g!r} is not a value of {cfg['group']} (or *): no unit to compare")
        if band and band not in pops:
            e.append(f"age_band {band!r} is not a decision_units.populations group")
        if r["outcome"] not in outs:
            e.append(f"outcome {r['outcome']!r} is not reported by the spec {outs}")
        per = _num(r["per"])
        if r["measure"] not in ("count", "rate") or (r["measure"] == "rate" and not (per or 0) > 0):
            e.append("measure: count, or rate with per > 0")
        yr = _num(r["year"])
        gap = None if yr is None else max(t0 - yr, yr - t1, 0.0)
        if gap is None or gap > float(th["max_year_gap"]):
            e.append(f"year {r['year']!r}: more than {th['max_year_gap']} years from the decision window {t0:g}-{t1:g}")
        val, lo, hi = _num(r["value"]), _num(r["lower"]), _num(r["upper"])
        if None in (val, lo, hi) or not 0 <= lo <= val <= hi:
            e.append("value, lower, upper: finite, >= 0, lower <= value <= upper")
        if r["kind"] not in KINDS:
            e.append(f"kind {r['kind']!r}: estimated or reported")
        if not str(r["source"] or "").strip():
            e.append("source: the citation")
        rlo, rhi = _num(r["ratio_low"]), _num(r["ratio_high"])
        band_r = _band(rlo, rhi, th)
        if r["tolerance"] not in TOLERANCES:
            e.append(f"tolerance {r['tolerance']!r}: ratio or overlap")
        elif r["tolerance"] == "ratio" and (not _band_ok(*band_r, th) or not (val or 0) > 0):
            e.append(f"ratio band {list(band_r)} must contain 1 within {th['widest_ratio_band']}, with value > 0")
        if e:
            bad += [f"{P}.csv line {n}: {x}" for x in e]
            continue
        refs.append({"id": rid, "group": g, "age_band": band or None, "outcome": r["outcome"],
                     "measure": r["measure"], "per": per if r["measure"] == "rate" else None, "year": yr,
                     "year_gap": gap, "value": val, "lower": lo, "upper": hi, "kind": r["kind"],
                     "source": r["source"].strip(), "tolerance": {"type": r["tolerance"], "ratio_band": list(band_r)
                                                                  if r["tolerance"] == "ratio" else None}})
    if refs and not any(x["kind"] == "estimated" for x in refs):
        bad.append(f"{P}.csv: no estimated reference (reported values are information only)")
    return refs, bad


# --------------------------------------------------------------------------- model values
def _years(A) -> float:
    t0, t1 = (float(x) for x in A.spec["objective"]["horizon"])
    return t1 - t0


def _members(A, cfg, g) -> list:
    return [i for i in A.ids if g == "*" or str(A.by_id[i].get(cfg["group"])) == g]


def _pop(A, ids, band) -> float:
    pops = A.spec["decision_units"]["populations"]
    cols = [pops[band]["column"]] if band else [pm["column"] for pm in pops.values()]
    return sum(float(A.by_id[i][c]) for i in ids for c in cols)


def _value(A, cfg, ref, out, arm):
    """The arm's annual mean over the window for the reference's group (a rate per `per` people of its band), or None
    when no unit (or no population) matches."""
    ids = _members(A, cfg, ref["group"])
    tot = sum(out[i]["y"][arm][ref["outcome"]] for i in ids) / _years(A)
    if not ids:
        return None
    if ref["measure"] == "rate":
        p = _pop(A, ids, ref["age_band"])
        return tot * ref["per"] / p if p > 0 else None
    return tot


def by_population(A, theta, zs, bands, arm) -> dict:
    """{population group: outcomes of that group alone} at the given values: the outcome model predicts one population
    group at a time (its other groups and packages left out), then is restored."""
    m, res = A.model, {}
    saved = (m.pops, m.packages, list(getattr(m, "last_events", []) or []))
    try:
        for b in bands:
            m.pops = {b: A.spec["decision_units"]["populations"][b]}
            m.packages = list(dict.fromkeys([A.comp, arm]))
            res[b] = m.predict(zs, theta, A.ids)
    finally:
        m.pops, m.packages, m.last_events = saved
    return res


class Collector:
    """Each uncertainty draw's all-age reference values (UQ.run's on_draw hook)."""

    def __init__(self, A):
        self.A, self.cfg = A, config(A.spec)
        self.refs, bad = load_references(A.spec)
        if bad:
            raise ValueError("validation.burden: " + "; ".join(bad))
        self.draws = {r["id"]: [] for r in self.refs if not r["age_band"]}

    def add(self, out):
        for r in self.refs:
            if r["id"] in self.draws:
                self.draws[r["id"]].append(_value(self.A, self.cfg, r, out, self.cfg["arm"]))


def verdict(row, th=None) -> str:
    """The status a row's numbers support (the contract re-judges every row with this)."""
    th = th or thresholds()
    m, tol = row.get("model") or {}, row.get("tolerance") or {}
    if row.get("kind") == "reported":
        return "information"
    p, lo, hi = _num(m.get("point")), _num(m.get("low")), _num(m.get("high"))
    if p is None or lo is None or hi is None:
        return "unmatched"
    if tol.get("type") == "ratio":
        b = tol.get("ratio_band") or [None, None]
        v = _num(row.get("value"))
        if not (v and v > 0 and _band_ok(_num(b[0]), _num(b[1]), th)):
            return "invalid"
        return "within" if b[0] <= p / v <= b[1] else "outside"
    if tol.get("type") == "overlap":
        return "within" if lo <= row["upper"] and hi >= row["lower"] else "outside"
    return "invalid"


def compare(ref, point, draws, level=0.95) -> dict:
    """One reference against the model: ratios, the model interval (draws, else the point value) and the status."""
    ok = point is not None and (draws is None or all(x is not None for x in draws))
    a = 100 * (1 - level) / 2
    if ok and draws:
        lo, med, hi = (float(np.percentile(draws, q)) for q in (a, 50, 100 - a))
        src = f"{len(draws)} uncertainty draws, central {level:.0%}"
    else:
        lo = med = hi = point
        src = ("the point value: the draws keep all-age totals only" if ref["age_band"] else
               "the point value: no uncertainty draws") if ok else "no model value: no unit or population matches"
    row = dict(ref, model={"point": point, "median": med if draws else None, "low": lo, "high": hi,
                           "interval_from": "draws" if ok and draws else "point", "note": src})
    v = ref["value"]
    row["ratio"] = point / v if ok and v > 0 else None
    row["ratio_interval"] = [point / ref["upper"] if ok and ref["upper"] > 0 else None,
                             point / ref["lower"] if ok and ref["lower"] > 0 else None]
    row["status"] = verdict(row)
    return row


def _param(A, x) -> float:
    if _num(x) is not None:
        return float(x)
    if x in A.theta0:
        return float(A.theta0[x])
    return float(next(p.value for p in A.eng.parameters() if p.name == x))


def daly_parts(A, cfg, arm, out0, by_pop, point) -> dict:
    """The share of the arm's DALYs from deaths (deaths x years of life lost per death), and the plan's DALYs averted
    per death averted."""
    dp = cfg["daly_parts"] or {}
    dn, de = dp.get("dalys", "dalys"), dp.get("deaths", "deaths")
    res = {"dalys": dn, "deaths": de, "deaths_share_of_dalys": None, "dalys_averted_per_death_averted": None}
    if dn not in A.outcomes or de not in A.outcomes:
        res["statement"] = f"not computed: the spec does not report both {dn} and {de}"
        return res
    av = point["averted"]
    if av[de] > 0:
        res["dalys_averted_per_death_averted"] = av[dn] / av[de]
    y = dp.get("yll_per_death")
    tot = sum(out0[i]["y"][arm][dn] for i in A.ids)
    if not y or tot <= 0:
        res["statement"] = "share from deaths not computed: " + ("no yll_per_death declared" if not y else "no DALYs")
        return res
    if "all" in y:
        yll = sum(out0[i]["y"][arm][de] for i in A.ids) * _param(A, y["all"])
    else:
        yll = sum(by_pop[b][i]["y"][arm][de] * _param(A, x) for b, x in y.items() for i in A.ids)
    res["deaths_share_of_dalys"] = yll / tot
    res["statement"] = "deaths x declared years of life lost per death, over the arm's DALYs in the window"
    return res


def run(A, out0, z0, point, col: Collector, level=0.95) -> dict:
    """The burden_validation artifact's payload."""
    cfg, arm, refs = col.cfg, col.cfg["arm"], col.refs
    yp = (cfg["daly_parts"] or {}).get("yll_per_death") or {}
    bands = sorted({r["age_band"] for r in refs if r["age_band"]} | ({b for b in yp if b != "all"}))
    by_pop = by_population(A, A.theta0, z0, bands, arm) if bands else {}
    rows = []
    for r in refs:
        out = by_pop[r["age_band"]] if r["age_band"] else out0
        rows.append(compare(r, _value(A, cfg, r, out, arm), col.draws.get(r["id"]) or None, level))
    t0, t1 = A.spec["objective"]["horizon"]
    cnt = {s: sum(x["status"] == s for x in rows) for s in STATUSES}
    return {"arm": arm, "group_column": cfg["group"], "window": [t0, t1], "csv": cfg["csv"],
            "draws": max((len(v) for v in col.draws.values()), default=0), "references": rows,
            "summary": dict(cnt, n=len(rows)), "daly_parts": daly_parts(A, cfg, arm, out0, by_pop, point),
            "statement": f"the {arm} arm's annual mean over {t0:g}-{t1:g} against each reference's year"}


# --------------------------------------------------------------------------- gate, claims, results.md
def _f(kind, claim, key, declarable):
    return {"kind": kind, "severity": "HIGH", "stage": "ANALYZE", "target_stage": "ANALYZE", "claim": claim,
            "key": key, "scope_declarable": declarable, "source": PRODUCER}


def findings(raw, data) -> list[dict]:
    """Gate findings: a group outside its tolerance is declarable (the gap is declared with its decision test); a
    missing artifact, an unmatched reference or a status its numbers do not support is not (fail closed)."""
    if section(raw) is None:
        return []
    F = "analysis/burden_validation.yaml"
    rows = (data or {}).get("references") if isinstance(data, dict) else None
    if not isinstance(rows, list) or not rows:
        return [_f("burden_validation_missing", f"the spec declares validation.burden but {F} has no references",
                   "burden_validation", False)]
    out, th = [], thresholds()
    for r in rows:
        rid = str((r or {}).get("id"))
        want = verdict(r or {}, th)
        if r.get("status") != want or want == "invalid":
            out.append(_f("burden_validation_inconsistent", f"{F}: reference {rid} has status {r.get('status')!r}; "
                          f"its numbers give {want!r}", f"burden:{rid}", False))
        elif want == "unmatched":
            out.append(_f("burden_reference_unmatched", f"{F}: reference {rid} ({r.get('group')}) has no model value",
                          f"burden:{rid}", False))
        elif want == "outside":
            m = r["model"]
            out.append(_f("burden_outside_tolerance", (
                f"{r['outcome']} in {r['group']}{' ages ' + r['age_band'] if r.get('age_band') else ''}: model "
                f"{m['point']:.4g} ({m['low']:.4g}-{m['high']:.4g}) against the {r['kind']} {r['value']:.4g} "
                f"({r['lower']:.4g}-{r['upper']:.4g}, {r['source']}); outside its {r['tolerance']['type']} tolerance"),
                f"burden:{rid}", True))
    return out


def claims(c, art, data) -> list[dict]:
    """Claim specs (toolkit/analysis/claims.py `_c`) for each reference row and the DALY parts."""
    cl = []
    for j, r in enumerate(data["references"]):
        f, k = f"data.references[{j}]", f"burden.{r['id']}"
        cl += [c(f"{k}.model", "scalar", art, f"{f}.model.point", r["outcome"]),
               c(f"{k}.model_low", "scalar", art, f"{f}.model.low", r["outcome"]),
               c(f"{k}.model_high", "scalar", art, f"{f}.model.high", r["outcome"]),
               c(f"{k}.value", "scalar", art, f"{f}.value", r["outcome"]),
               c(f"{k}.lower", "scalar", art, f"{f}.lower", r["outcome"]),
               c(f"{k}.upper", "scalar", art, f"{f}.upper", r["outcome"]),
               c(f"{k}.status", "label", art, f"{f}.status")]
        if r.get("ratio") is not None:
            cl.append(c(f"{k}.ratio", "scalar", art, f"{f}.ratio", "ratio"))
    dp = data["daly_parts"]
    for n in ("deaths_share_of_dalys", "dalys_averted_per_death_averted"):
        if dp.get(n) is not None:
            cl.append(c(f"burden.{n}", "scalar", art, f"data.daly_parts.{n}", "ratio"))
    return cl


def results_lines(data) -> list[str]:
    t0, t1 = data["window"]
    L = ["## Absolute burden against external estimates", "",
         f"The {data['arm']} arm's annual mean over {t0:g}-{t1:g}, by {data['group_column']}, against each reference "
         f"for its year (`validation.burden`). Within: {data['summary']['within']}; outside: "
         f"{data['summary']['outside']}; reported values shown for information only: {data['summary']['information']}.",
         "", "| Reference | Group | Ages | Outcome | Year | Model | Model interval | Reference (interval) | Ratio | Kind "
         "| Tolerance | Status |", "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in data["references"]:
        k, tol = f"burden.{r['id']}", r["tolerance"]
        unit = f" per {r['per']:g}" if r["measure"] == "rate" else ""
        L.append(f"| {r['id']} ({r['source']}) | {r['group']} | {r['age_band'] or 'all'} | {r['outcome']}{unit} | "
                 f"{r['year']:g} | [CLAIM:{k}.model] | [CLAIM:{k}.model_low] to [CLAIM:{k}.model_high] "
                 f"({r['model']['interval_from']}) | [CLAIM:{k}.value] ([CLAIM:{k}.lower] to [CLAIM:{k}.upper]) | "
                 + (f"[CLAIM:{k}.ratio]" if r.get("ratio") is not None else "n/a") + f" | {r['kind']} | "
                 + (f"ratio {tol['ratio_band'][0]:g} to {tol['ratio_band'][1]:g}" if tol["type"] == "ratio"
                    else "intervals overlap") + f" | [CLAIM:{k}.status] |")
    pts = sorted({r["model"]["note"] for r in data["references"] if r["model"]["interval_from"] == "point"})
    L += [""] + [f"Model interval from {p}." for p in pts]
    dp = data["daly_parts"]
    s = []
    if dp.get("deaths_share_of_dalys") is not None:
        s.append("share of the arm's DALYs from deaths [CLAIM:burden.deaths_share_of_dalys]")
    if dp.get("dalys_averted_per_death_averted") is not None:
        s.append("the plan's DALYs averted per death averted [CLAIM:burden.dalys_averted_per_death_averted]")
    s = "; ".join(s)
    L += ["", (s[:1].upper() + s[1:] + ". " if s else "") + f"({dp.get('statement', '')}.)", ""]
    return L


# --------------------------------------------------------------------------- self-test
def _self_test() -> int:
    import tempfile
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)
    th = thresholds()
    ref = {"id": "r", "group": "g", "age_band": None, "outcome": "cases", "measure": "count", "per": None,
           "year": 1.0, "year_gap": 0.0, "value": 100.0, "lower": 80.0, "upper": 130.0, "kind": "estimated",
           "source": "synthetic", "tolerance": {"type": "ratio", "ratio_band": [0.5, 2.0]}}
    ok(compare(ref, 150.0, [140.0, 150.0, 160.0])["status"] == "within"
       and abs(compare(ref, 150.0, None)["ratio"] - 1.5) < 1e-12, "BV1 a ratio inside its band is within")
    r2 = compare(ref, 250.0, [240.0, 260.0])
    ok(r2["status"] == "outside" and r2["ratio_interval"] == [250 / 130, 250 / 80], "BV2 a ratio outside its band")
    ov = dict(ref, tolerance={"type": "overlap", "ratio_band": None})
    ok(compare(ov, 70.0, [60.0, 70.0, 85.0])["status"] == "within" and compare(ov, 70.0, [60.0, 75.0])["status"]
       == "outside", "BV3 interval overlap: the draws' interval meets the reference interval, or not")
    sec = {"validation": {"burden": {"csv": "x.csv"}}}
    rref = dict(ref, id="p", kind="reported")
    rp = compare(rref, 900.0, None)
    ok(rp["status"] == "information" and findings(sec, {"references": [compare(ref, 150.0, None), rp]}, [ref, rref])
       == [], "BV4 a reported value is information only: never judged, never a finding")
    pt = compare(dict(ref, age_band="u5"), 120.0, None)
    ok(pt["model"]["interval_from"] == "point" and "all-age" in pt["model"]["note"],
       "BV5 an age-banded reference is the point value, with a statement")
    fo = findings(sec, {"references": [r2]}, [ref])
    ok([(x["kind"], x["scope_declarable"], x["key"]) for x in fo] == [("burden_outside_tolerance", True, "burden:r")],
       f"BV6 a group outside tolerance is one declarable finding: {fo}")
    got = {x["kind"]: x["scope_declarable"] for x in findings(sec, {"references": [compare(ref, None, None)]}, [ref])}
    ok(got == {"burden_reference_unmatched": False}, f"BV7 an unmatched reference fails closed: {got}")
    wref = dict(ref, id="w", tolerance={"type": "ratio", "ratio_band": [0.01, 100.0]})
    got = [x["kind"] for x in findings(sec, {"references": [dict(r2, status="within"), compare(wref, 150.0, None)]},
                                       [ref, wref])]
    ok(got == ["burden_validation_inconsistent"] * 2,
       f"BV8 a status its numbers do not support, or a band wider than the manifest's, fails closed: {got}")
    ok([x["kind"] for x in findings(sec, None, [ref])] == ["burden_validation_missing"] and findings({}, None) == [],
       "BV9 a declared check with no artifact fails closed; no declaration, no finding")
    # Codex r1 P2: the artifact is reconciled with the declared references before any row is judged
    ok_row = compare(ref, 150.0, None)
    for name, rows, exp, want in (
            ("an unrelated row", [ok_row, compare(dict(ref, id="x"), 150.0, None)], [ref], "burden_validation_unexpected"),
            ("a declared reference missing", [ok_row], [ref, rref], "burden_validation_missing"),
            ("a reference twice", [ok_row, ok_row], [ref], "burden_validation_unexpected"),
            ("an unknown kind", [dict(ok_row, kind="modelled")], [ref], "burden_validation_inconsistent"),
            ("changed metadata", [dict(ok_row, value=140.0)], [ref], "burden_validation_inconsistent"),
            ("a reported-only artifact", [rp], [rref], "burden_validation_no_estimate"),
            ("a reported row with no model value", [ok_row, compare(rref, None, None)], [ref, rref],
             "burden_reference_unmatched")):
        got = {(x["kind"], x["scope_declarable"]) for x in findings(sec, {"references": rows}, exp)}
        ok((want, False) in got, f"BV18 the gate refuses {name}: {sorted(got)}")
    ok([x["kind"] for x in findings(sec, {"references": [ok_row]}, None)] == ["burden_validation_missing"],
       "BV18 the gate fails closed without the declared references")
    # Codex r1 P2: corrections on the validation arm stop the analysis, at the point, in every draw and per age band
    from toolkit.analysis.core import Analysis, AnalysisBlocked
    clip = [{"unit": "u1", "package": "a", "kind": "reduction_clip"}]

    class _G:
        comp, ids, outcomes, theta0 = "none", ["u1", "u2"], ["dalys", "deaths"], {}
        by_id = {"u1": {"g": "g1", "py": 100.0, "po": 300.0}, "u2": {"g": "g2", "py": 50.0, "po": 50.0}}
        spec = {"decision_units": {"populations": {"young": {"column": "py"}, "old": {"column": "po"}}},
                "objective": {"horizon": [0, 2]}}
        plan_corrections, guard = Analysis.plan_corrections, Analysis.guard

    class _M:
        clip = False

        def __init__(self):
            self.pops, self.packages, self.last_events = dict(_G.spec["decision_units"]["populations"]), ["none", "a"], []

        def predict(self, zs, theta, ids, noise=None):
            rate = {"young": {"deaths": 0.01, "dalys": 0.8}, "old": {"deaths": 0.002, "dalys": 0.1}}
            self.last_events = list(clip) if self.clip else []
            return {i: {"y": {k: {o: sum(rate[b][o] * _G.by_id[i][pm["column"]] for b, pm in self.pops.items())
                                  for o in ("deaths", "dalys")} for k in self.packages}} for i in ids}
    _G.model = _M()
    col = Collector.__new__(Collector)
    col.A, col.cfg, col.refs, col.draws = _G, {"arm": "a", "group": "g"}, [], {}
    try:
        col.add({}, clip)
        ok(False, "BV19 a correction on the validation arm in a draw stops the analysis")
    except AnalysisBlocked:
        ok(True, "BV19 a correction on the validation arm in a draw stops the analysis")
    try:
        guard_point(_G, "a", clip)
        ok(False, "BV19 a correction on the validation arm at the point stops the analysis")
    except AnalysisBlocked:
        ok(True, "BV19 a correction on the validation arm at the point stops the analysis")
    try:
        col.add({}, [dict(clip[0], package="b")])
        guard_point(_G, "a", [dict(clip[0], package="b")])
        ok(True, "BV19 a correction on another unchosen package does not")
    except AnalysisBlocked:
        ok(False, "BV19 a correction on another unchosen package does not")
    _G.model.clip, pops0 = True, _G.model.pops
    try:
        by_population(_G, {}, {}, ["young", "old"], "a")
        ok(False, "BV19 a correction in the age-band predictions stops the analysis")
    except AnalysisBlocked:
        ok(_G.model.pops is pops0 and _G.model.last_events == [],
           "BV19 a correction in the age-band predictions stops the analysis; the model state is restored")
    # genuine age separation: two bands with different rates and years of life lost per death
    _G.model.clip = False
    bp = by_population(_G, {}, {}, ["young", "old"], "a")
    full = _G.model.predict({}, {}, _G.ids)
    cfg = {"arm": "a", "group": "g", "daly_parts": {"yll_per_death": {"young": 60.0, "old": 20.0}}}
    yr = dict(ref, group="g1", age_band="young", outcome="deaths", measure="rate", per=1000.0)
    dpa = daly_parts(_G, cfg, "a", full, bp, {"averted": {"dalys": 8.0, "deaths": 0.2}})
    want = (1.5 * 60 + 0.7 * 20) / 155.0
    ok(abs(bp["young"]["u1"]["y"]["a"]["deaths"] - 1.0) < 1e-12 and abs(bp["old"]["u1"]["y"]["a"]["deaths"] - 0.6)
       < 1e-12 and abs(_value(_G, cfg, yr, bp["young"], "a") - 1000 * 0.01 / 2) < 1e-12
       and abs((dpa["deaths_share_of_dalys"] or 0) - want) < 1e-12
       and abs((dpa["dalys_averted_per_death_averted"] or 0) - 40) < 1e-9,
       f"BV20 per-band values and per-band years of life lost ({dpa['deaths_share_of_dalys']} vs {want})")
    # Codex r1 P3: zero and censored endpoints are drawn, not dropped
    from toolkit.analysis import figure_style as FS, figures as FG

    class _R:
        def art(self, k):
            z = dict(compare(dict(ov, lower=0.0), 0.0, None), id="z")
            return {"references": [z, compare(ref, 150.0, None)], "summary": {"within": 1}, "window": [0, 1],
                    "arm": "none"}
    fz = FG.fig_burden_validation(_R(), FS.setup("light"))
    zr = next(r for r in fz[3] if r["reference"] == "z")
    texts = [x.get_text() for a in fz[0].axes for x in a.texts]
    ok("model 0" in str(zr.get("shown_as")) and "reference lower 0" in str(zr.get("shown_as"))
       and any(t_.startswith("0") for t_ in texts), f"BV21 zero endpoints are shown at the axis floor: {zr.get('shown_as')}")
    ok(th["widest_ratio_band"][0] <= th["default_ratio_band"][0] <= 1 <= th["default_ratio_band"][1]
       <= th["widest_ratio_band"][1], "BV10 the manifest's default band lies within its widest band")
    # the spec contract on a toy spec
    from toolkit.spec import schema
    with tempfile.TemporaryDirectory() as d:
        p = schema.toy_spec(d)
        head = ",".join(COLUMNS)
        good = "a,r0,,cases,count,,1,100,80,130,estimated,synthetic,ratio,,"

        def probs(lines, extra=None):
            open(os.path.join(d, "refs.csv"), "w").write("\n".join([head] + lines) + "\n")
            raw = yaml.safe_load(open(p))
            raw["validation"] = {"burden": dict({"csv": "refs.csv"}, **(extra or {}))}
            return load_references(schema.Spec(raw, d))[1]
        ok(probs([good]) == [], f"BV11 a valid reference file passes: {probs([good])}")
        cases = [("missing group", ["a,zz,,cases,count,,1,100,80,130,estimated,s,ratio,,"], "is not a value"),
                 ("unknown band", ["a,r0,kids,cases,count,,1,100,80,130,estimated,s,ratio,,"], "age_band"),
                 ("unreported outcome", ["a,r0,,deaths,count,,1,100,80,130,estimated,s,ratio,,"], "outcome"),
                 ("interval", ["a,r0,,cases,count,,1,100,120,130,estimated,s,ratio,,"], "lower <= value"),
                 ("kind", ["a,r0,,cases,count,,1,100,80,130,modelled,s,ratio,,"], "kind"),
                 ("source", ["a,r0,,cases,count,,1,100,80,130,estimated,,ratio,,"], "source"),
                 ("wide band", ["a,r0,,cases,count,,1,100,80,130,estimated,s,ratio,0.01,50"], "ratio band"),
                 ("year", ["a,r0,,cases,count,,40,100,80,130,estimated,s,ratio,,"], "years from"),
                 ("rate per", ["a,r0,,cases,rate,,1,100,80,130,estimated,s,ratio,,"], "per > 0"),
                 ("only reported", ["a,r0,,cases,count,,1,100,80,130,reported,s,ratio,,"], "no estimated"),
                 ("duplicate id", [good, good], "unique"),
                 ("unparsable band", ["a,r0,,cases,count,,1,100,80,130,estimated,s,ratio,oops,typo"], "ratio_low"),
                 ("non-finite band", ["a,r0,,cases,count,,1,100,80,130,estimated,s,ratio,NaN,NaN"], "ratio_low"),
                 ("one bound unparsable", ["a,r0,,cases,count,,1,100,80,130,estimated,s,ratio,0.5,x"], "ratio_high")]
        for name, lines, needle in cases:
            got = probs(lines)
            ok(any(needle in x for x in got), f"BV12 the reference contract refuses: {name} ({got})")
        ok(any("arm" in x for x in probs([good], {"arm": "zz"})) and any("unknown key" in x for x in
                                                                          probs([good], {"tolerance": 2})),
           "BV13 an unknown arm or key is refused")
        raw = yaml.safe_load(open(p))
        raw["validation"] = {"burden": {"csv": "units.csv"}}
        raw["calibration"]["targets"] = {"csv": "units.csv"}
        ok(any("never fitted" in x for x in load_references(schema.Spec(raw, d))[1]),
           "BV14 the calibration targets file cannot be the reference file")
        raw = yaml.safe_load(open(p))
        raw["validation"] = {"burden": {"csv": "refs.csv", "daly_parts": {"yll_per_death": {"all": -1}}}}
        ok(any("daly_parts" in x for x in load_references(schema.Spec(raw, d))[1])
           and any("validation.burden" in x for x in schema.validate(schema.Spec(raw, d))),
           "BV15 daly_parts needs positive years of life lost; schema.validate reports the section")
        # Codex r1 P1: a reference file is never a calibration input, by any name, nor do its rows repeat fitted values
        import shutil
        open(os.path.join(d, "refs.csv"), "w").write(head + "\n" + good + "\n")
        os.symlink(os.path.join(d, "refs.csv"), os.path.join(d, "alias.csv"))
        shutil.copy(os.path.join(d, "refs.csv"), os.path.join(d, "copy.csv"))

        def indep(cal_mut, ref_csv="refs.csv"):
            raw = yaml.safe_load(open(p))
            raw["validation"] = {"burden": {"csv": ref_csv}}
            cal_mut(raw["calibration"])
            return load_references(schema.Spec(raw, d))[1]
        jt = lambda f: lambda c: c.update(joint={"targets": [{"id": "g", "kind": "group", "csv": f}]})  # noqa: E731
        for name, mut, rc in (("a symlink of the targets file", lambda c: c.update(targets={"csv": "alias.csv"}), "refs.csv"),
                              ("a copy of the targets file", lambda c: c.update(targets={"csv": "copy.csv"}), "refs.csv"),
                              ("a joint-calibration group target", jt("refs.csv"), "refs.csv"),
                              ("an alias of a joint target", jt("alias.csv"), "refs.csv"),
                              ("the units table", lambda c: None, "units.csv")):
            got = indep(mut, rc)
            ok(any("calibration input" in x for x in got), f"BV17 the reference file is refused as {name}: {got}")
        u0 = next(u for u in schema.load(p).units() if u["region"] == "r0")
        open(os.path.join(d, "coin.csv"), "w").write(head + "\n" + f"a,r0,,cases,count,,1,{u0['tgt']},0,1,"
                                                     "estimated,s,overlap,,\n")
        got = indep(lambda c: None, "coin.csv")
        ok(any("fitted observation" in x for x in got), f"BV17 a reference repeating a fitted target value is refused: {got}")
        raw = yaml.safe_load(open(p))
        raw["validation"] = {"burden": {"csv": "refs.csv"}}
        pv = provenance(schema.Spec(raw, d))
        ok(pv.get("sha256") and pv.get("independent") is True
           and any(x["path"].endswith("units.csv") for x in pv.get("calibration_inputs") or []),
           f"BV17 the artifact's provenance names the reference file and the calibration inputs it is independent of: {pv}")
    # DALY parts arithmetic on a stub analysis
    class _A:
        outcomes = ["dalys", "deaths"]
        ids = ["u"]
        theta0 = {"le": 30.0}
    out = {"u": {"y": {"none": {"dalys": 600.0, "deaths": 10.0}}}}
    dp = daly_parts(_A, {"daly_parts": {"yll_per_death": {"all": "le"}}}, "none", out, {},
                    {"averted": {"dalys": 90.0, "deaths": 3.0}})
    ok(abs(dp["deaths_share_of_dalys"] - 0.5) < 1e-12 and abs(dp["dalys_averted_per_death_averted"] - 30) < 1e-12,
       f"BV16 share of DALYs from deaths and DALYs averted per death averted: {dp}")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(_self_test() if sys.argv[1:2] == ["--self-test"] else (print(__doc__) or 2))
