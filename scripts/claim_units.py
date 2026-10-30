#!/usr/bin/env python3
"""A18: a `percentage` claim whose source holds a proportion.

    python3 scripts/claim_units.py <run_dir>
    python3 scripts/claim_units.py --self-test

E7 attempt 7 (red team R-064): the ledger claim
`r5p_as_delivered_gain_topped_up_p10` had `claim_kind: percentage` and value
0.587, read from `as_delivered.gain_topped_up_p10`, a proportion (P(gain >= 10%)
over the PSA draws). scripts/render_claims.py renders a percentage as
f"{v:.1f}%", so the report would have read 0.6% where 58.7% was meant. The
provenance check (scripts/lib/provenance.py `values_match`) accepts x or 100x
for a percentage, and the builder copies the source value, so neither can see
it.

Claims come from a fresh build of `models/claims_spec.yaml`
(scripts/build_claims_ledger.py), else from `models/claims_ledger.yaml`. The
gate collects `gate_findings(run_dir)`; render_claims reports `problems()` for
the claims a draft cites when it renders.
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import math
import os
import re
import sys
from typing import Any

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib import provenance as P  # noqa: E402


def _builder():
    """scripts/build_claims_ledger.py by path (a run may hold its own module of that name)."""
    m = sys.modules.get("_ma_claim_units_builder")
    if m is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "build_claims_ledger.py")
        spec = importlib.util.spec_from_file_location("_ma_claim_units_builder", path)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        sys.modules["_ma_claim_units_builder"] = m
    return m


# E7 attempt 7 (red team R-064): `claim_kind: percentage` with source field
# `as_delivered.gain_topped_up_p10` = 0.587, a proportion; render_claims prints
# a percentage as f"{v:.1f}%", so the report would have read 0.6% for 58.7%.
# The provenance check accepts x or 100x for percentages, so it cannot see this.
#
# Rule. A `percentage` claim is flagged when all of these hold:
#  1. its value v has 0 < v <= 1, so it would render as at most "1%", and v is
#     the source's own value (within 1%): a ledger value of 100 x the source
#     is a conversion already made, and a source that does not resolve or
#     disagrees otherwise is left to the provenance check;
#  2. the quantity's own values lie in [0, 1], as a proportion's must: v, the
#     claim's CI, the CI the source gives (ci_low/ci_high, lo/hi, ci_95: [lo,
#     hi], ...), and, for a statistic of one quantity (`x.mean`), every
#     numeric statistic beside it (`x.ci_low`, `x.ci_high`, ...); for a CSV
#     column, every value of the column. A negative bound or a bound above 1
#     means the number is not a proportion (a percent change of 0.12 with a
#     CI of -0.2 to 0.48 is a real 0.12%);
#  3. no segment of its source field (or CSV column) carries a percent marker
#     (pct, percent, perc, pp, ...): a field named as a percent is one;
#  4. the source itself says the number is on the 0-1 scale, by one of:
#     marked-siblings  the mapping that holds the quantity names its percents
#                      and writes them on the 0-100 scale: a sibling whose name
#                      carries a percent marker holds |x| > 1, and this field
#                      is not named as one;
#     all-in-unit      no sibling is named as a percent, every numeric sibling
#                      lies in [0, 1], and at least two lie strictly inside
#                      (0, 1) (zeros and ones say nothing about the scale); for
#                      a CSV column, at least two of its values lie inside (0, 1);
#     units            the claim's `units` names a proportion (proportion,
#                      fraction, share, probability, "0-1").
# A proportion-like name (share, prop, frac, prob, a `p` or `p<digits>` token)
# is reported as a hint, but is not evidence on its own: a real 0.5% exists,
# and `p10` is also a 10th percentile.
_PCT_TOKENS = frozenset({"pct", "percent", "percentage", "percentages", "perc", "pc", "pp", "pctpt", "ppt"})
_PROP_TOKENS = frozenset({"share", "shares", "prop", "proportion", "proportions", "frac", "fraction",
                          "fractions", "prob", "probability", "p", "pr"})
_STAT_KEYS = frozenset({"mean", "median", "value", "point", "estimate", "lo", "hi", "low", "high",
                        "lower", "upper", "ci_low", "ci_high", "ci_lower", "ci_upper"})
_PROP_UNITS = re.compile(r"\b(proportion|fraction|share|probability|0\s*[-–]\s*1|unit interval)\b", re.I)
_PCT_UNITS = re.compile(r"%|\bper\s*cent\b|\bpercent", re.I)


def _tokens(name: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", str(name).lower()) if t]


def _pct_marked(name: str) -> bool:
    if "%" in str(name):
        return True
    return any(t in _PCT_TOKENS or t.endswith("pct") or t.startswith("pct") for t in _tokens(name))


def _prop_named(name: str) -> bool:
    return any(t in _PROP_TOKENS or re.fullmatch(r"p\d+", t) for t in _tokens(name))


def _num_items(node: Any) -> dict[str, float]:
    if not isinstance(node, dict):
        return {}
    out = {}
    for k, v in node.items():
        n = None if isinstance(v, bool) else P._num(v)
        if n is not None:
            out[str(k)] = n
    return out


def _unit(xs) -> bool:
    return all(0 <= x <= 1 for x in xs)


def _inside(xs) -> int:
    return sum(1 for x in xs if 0 < x < 1)


def _ci_nums(ci) -> list[float]:
    if not isinstance(ci, (list, tuple)):
        return []
    return [x for x in (None if isinstance(e, bool) else P._num(e) for e in ci) if x is not None]


def _scale_evidence(c: dict, v: float, run_dir: str, cache: dict) -> tuple[list[str], str] | None:
    """(evidence, quantity name) when the source says `c` (value v) is on the
    0-1 scale; None when it is named as a percent, is not a proportion, cannot
    be read, or the source says nothing either way."""
    src, field = str(c.get("source_artifact") or ""), str(c.get("source_field") or "")
    path = P._resolve_artifact_path(run_dir, src)
    if path is None or not field:
        return None
    own = [v] + [x for x in (P._num(c.get("ci_low")), P._num(c.get("ci_high"))) if x is not None]
    # The source's own value: flag only when the ledger value is the source value. A
    # ledger value of 100 x the source is a conversion already made (Codex P47B-1);
    # any other mismatch, or a source that does not resolve, is the provenance check's.
    try:
        point, ci = P.resolve_claim(c, run_dir, cache)
    except P.ClaimUnresolved:
        return None
    sv = None if isinstance(point, bool) else P._num(point)
    if sv is None or not math.isclose(v, sv, rel_tol=0.01, abs_tol=1e-12):
        return None
    own += [sv] + _ci_nums(ci)
    ev: list[str] = []
    units = str(c.get("units") or "")
    if _PROP_UNITS.search(units) and not _PCT_UNITS.search(units):
        ev.append(f"units {units!r} name a proportion")
    ext = os.path.splitext(path)[1].lower()
    if ext in (".yaml", ".yml", ".json"):
        segs = [s for s in re.split(r"\.|\[[^\]]*\]", field) if s]
        if not segs or any(_pct_marked(s) for s in segs):
            return None
        stat = segs[-1].lower() in _STAT_KEYS and len(segs) >= 2
        qname = segs[-2] if stat else segs[-1]
        try:
            if path not in cache:
                cache[path] = P._load_structured(path)
            doc = cache[path]
            # The node of the quantity (for `x.mean`, `x`) and the mapping that holds it.
            qpath = field[: -len(segs[-1])].rstrip(".") if stat and field.endswith(segs[-1]) else field
            ppath = qpath[: -len(qname)].rstrip(".") if qpath.endswith(qname) else None
            qnode = P.resolve_dotpath(doc, qpath) if stat else None
            parent = (P.resolve_dotpath(doc, ppath) if ppath else doc) if ppath is not None else None
        except (P._Unresolved, OSError, ValueError, UnicodeDecodeError, yaml.YAMLError):
            return (ev, qname) if ev and _unit(own) else None
        if stat:
            own += list(_num_items(qnode).values())
        for node in (qnode, P.resolve_dotpath(doc, field) if not stat else None):
            if isinstance(node, dict):
                # CI arrays (ci_95: [lo, hi]) and named bounds, as provenance reads them (P47B-2).
                own += _num_items(node).values() if node is not qnode else []
                own += _ci_nums(P._ci_from_mapping(node))
        if not _unit(own):
            return None
        sib = {k: x for k, x in _num_items(parent).items() if k != qname}
        marked_big = sorted(k for k, x in sib.items() if _pct_marked(k) and abs(x) > 1)
        if marked_big:
            ev.append(f"the mapping that holds it names its percents and writes them on the 0-100 "
                      f"scale ({marked_big[0]} = {sib[marked_big[0]]:.4g}); this field is not named as one")
        elif sib and not any(_pct_marked(k) for k in sib) and _unit(sib.values()) \
                and _inside(sib.values()) >= 2:
            ev.append(f"all {len(sib)} numeric values beside it are in [0, 1], "
                      f"{_inside(sib.values())} strictly inside")
    elif ext == ".csv":
        f2 = field
        fm = P._CSV_FILTER_PREFIX_RE.match(f2) or P._CSV_FILTER_SUFFIX_RE.match(f2)
        if fm:
            f2 = fm.group("rest")
        m = P._CSV_AGG_RE.match(f2)
        col = (m.group("col") or "").strip() if m else ""
        if not col or m.group("agg") in ("count", "nunique", "len") or _pct_marked(col):
            return None
        qname = col
        try:
            with open(path, encoding="utf-8", newline="") as fh:
                vals = [P._num(r.get(col)) for r in csv.DictReader(fh)]
        except (OSError, UnicodeDecodeError, ValueError, csv.Error):
            return (ev, qname) if ev and _unit(own) else None
        vals = [x for x in vals if x is not None]
        if not _unit(own + vals):
            return None
        if _inside(vals) >= 2:
            ev.append(f"all {len(vals)} values of column {col!r} are in [0, 1], {_inside(vals)} strictly inside")
    else:
        qname = field
        if _pct_marked(field) or not _unit(own):
            return None
    return (ev, qname) if ev else None


def problems(claims: list, run_dir: str) -> list[dict]:
    """A18: percentage claims whose cited source holds a proportion (the rule
    above). Each problem: id, value, rendered (as render_claims would print it),
    evidence, name_hint."""
    out, cache = [], {}
    for c in claims or []:
        if not isinstance(c, dict) or str(c.get("claim_kind") or "").lower() != "percentage":
            continue
        if P.is_external(c.get("source_artifact")):
            continue
        v = None if isinstance(c.get("value"), bool) else P._num(c.get("value"))
        if v is None or not 0 < v <= 1:
            continue
        found = _scale_evidence(c, v, run_dir, cache)
        if not found:
            continue
        ev, qname = found
        out.append({"id": c.get("id"), "value": v, "rendered": f"{v:.1f}%".replace(".0%", "%"),
                    "as_percent": f"{v * 100:.1f}%".replace(".0%", "%"), "evidence": ev,
                    "name_hint": _prop_named(qname),
                    "source": f"{c.get('source_artifact')}::{c.get('source_field')}"})
    return out


_DRAFTS = ("manuscript.md", "report.md")
_SCALE_CAP = 5


def _cited_ids(run_dir: str) -> set[str]:
    ids: set[str] = set()
    for rel in _DRAFTS:
        try:
            with open(os.path.join(run_dir, rel), encoding="utf-8", errors="replace") as f:
                ids |= {m.strip() for m in re.findall(r"\[CLAIM:([^\]\n]*)\]", f.read())}
        except OSError:
            continue
    return ids


def gate_findings(run_dir: str) -> list[dict]:
    """A18 gate findings, `claim_percentage_is_proportion`: HIGH when the draft
    (manuscript.md or report.md) cites the claim, so it would render wrong;
    MEDIUM while no draft cites it. Not scope-declarable. Claims come from a
    fresh build of models/claims_spec.yaml, else models/claims_ledger.yaml; a
    spec or ledger that cannot be read is reported by the claims-ledger check
    (claims_spec_invalid / claims_ledger_invalid), so it is not repeated here."""
    if os.path.exists(os.path.join(run_dir, _builder().SPEC_REL)):
        doc, _ = _builder().build(run_dir)
        claims = (doc or {}).get("claims") or []
    else:
        try:
            with open(os.path.join(run_dir, _builder().LEDGER_REL), encoding="utf-8") as f:
                led = yaml.safe_load(f)
        except (OSError, yaml.YAMLError):
            return []
        claims = (led.get("claims") if isinstance(led, dict) else None) or []
        if not isinstance(claims, list):
            return []
    probs = problems(claims, run_dir)
    cited = _cited_ids(run_dir)
    out = []
    for p in probs:
        sev = "HIGH" if p["id"] in cited else "MEDIUM"
        out.append({
            "kind": "claim_percentage_is_proportion", "severity": sev, "stage": "ANALYZE",
            "scope_declarable": False,
            "claim": (f"[{p['id']}] has claim_kind percentage and value {p['value']:.4g} from "
                      f"`{p['source']}`, which holds a proportion: "
                      + "; ".join(p["evidence"])
                      + (" (its name also reads as a proportion)" if p["name_hint"] else "")
                      + f". The report would print {p['rendered']} where {p['as_percent']} is meant"
                      + (" (the draft cites it)" if sev == "HIGH" else "")
                      + ". Write the value times 100 to a field named as a percent (e.g. `<name>_pct`) "
                        "and point the claim at it, or use claim_kind scalar if a proportion is meant."),
        })
    if len(out) > _SCALE_CAP:
        rest = out[_SCALE_CAP:]
        out = out[:_SCALE_CAP] + [{
            "kind": "claim_percentage_is_proportion",
            "severity": "HIGH" if any(r["severity"] == "HIGH" for r in rest) else "MEDIUM",
            "stage": "ANALYZE", "scope_declarable": False,
            "claim": f"{len(rest)} more: " + ", ".join(p["id"] for p in probs[_SCALE_CAP:])}]
    return out


def _self_test() -> int:
    import tempfile
    fails: list[str] = []

    def ok(c, m):
        if not c:
            fails.append(m)

    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        w = lambda rel, obj: yaml.safe_dump(obj, open(os.path.join(d, rel), "w"))
        w("models/derived.yaml", {
            # attempt 7's shape (R-064): percents named *_pct on the 0-100 scale, P(gain >= 10) a proportion
            "as_delivered": {"cost_m": 250.4, "gain_topped_up_pct": 10.37, "gain_topped_up_psa_lo": 6.3,
                             "gain_topped_up_p10": 0.587, "small_change_pct": 0.5},
            # a real 0.88% (a percent gain whose CI crosses zero), beside *_pct percents
            "r057": {"primary_gain_pct": 13.27, "psa_within": {"mean": 0.876, "ci_low": -4.44, "ci_high": 6.96}},
            # a real 0.83% beside zeros (attempt 8's driver decomposition)
            "drv": {"net_prices": 0.0, "residual": 0.8333, "case_load": 0.0},
            # unmarked siblings all inside (0, 1): proportions
            "coverage": {"region_a": 0.42, "region_b": 0.61, "region_c": 0.35},
            # a proportion-like name alone, beside costs
            "plan": {"share_smc": 0.4, "cost_m": 120.0, "n_units": 30},
            "growth": {"value": 0.3},
        })
        open(os.path.join(d, "models", "units.csv"), "w").write(
            "unit,frac_reached,reached_pct\nA,0.2,20\nB,0.6,60\nC,0.9,90\n")
        spec = {"claims": [
            {"id": "p10", "claim_kind": "percentage", "source_artifact": "models/derived.yaml",
             "source_field": "as_delivered.gain_topped_up_p10"},
            {"id": "real_half", "claim_kind": "percentage", "source_artifact": "models/derived.yaml",
             "source_field": "as_delivered.small_change_pct"},
            {"id": "ci_cross", "claim_kind": "percentage", "source_artifact": "models/derived.yaml",
             "source_field": "r057.psa_within.mean"},
            {"id": "zeros", "claim_kind": "percentage", "source_artifact": "models/derived.yaml",
             "source_field": "drv.residual"},
            {"id": "cov_a", "claim_kind": "percentage", "source_artifact": "models/derived.yaml",
             "source_field": "coverage.region_a"},
            {"id": "share_only", "claim_kind": "percentage", "source_artifact": "models/derived.yaml",
             "source_field": "plan.share_smc"},
            {"id": "units_prop", "claim_kind": "percentage", "units": "proportion",
             "source_artifact": "models/derived.yaml", "source_field": "growth.value"},
            {"id": "csv_frac", "claim_kind": "percentage", "source_artifact": "models/units.csv",
             "source_field": "frac_reached.mean()"},
            {"id": "csv_pct", "claim_kind": "percentage", "source_artifact": "models/units.csv",
             "source_field": "reached_pct.min()"},
            {"id": "big", "claim_kind": "percentage", "source_artifact": "models/derived.yaml",
             "source_field": "as_delivered.gain_topped_up_pct"},
            {"id": "scalar_p", "claim_kind": "scalar", "units": "probability",
             "source_artifact": "models/derived.yaml", "source_field": "as_delivered.gain_topped_up_p10"},
            {"id": "ext", "claim_kind": "percentage", "value": 0.8, "source_artifact": "external",
             "source_field": "a published case-fatality ratio (PMID 0)"},
        ]}
        w("models/claims_spec.yaml", spec)
        doc, errs = _builder().write(d)
        probs = {p["id"]: p for p in problems(doc["claims"], d)}
        ok(errs == [] and set(probs) == {"p10", "cov_a", "units_prop", "csv_frac"},
           f"U1 flags exactly the proportions with source evidence: {sorted(probs)} {errs}")
        p = probs.get("p10") or {}
        ok(p.get("rendered") == "0.6%" and p.get("as_percent") == "58.7%" and p.get("name_hint") is True
           and "gain_topped_up_pct = 10.37" in " ".join(p.get("evidence", [])),
           f"U2 attempt 7's claim: renders 0.6% for 58.7%, evidence names the marked sibling: {p}")
        ok(not probs.get("cov_a", {}).get("name_hint") and "csv_frac" in probs and "csv_pct" not in probs,
           f"U3 all-in-unit siblings and a CSV column of proportions are evidence; a *_pct column is not: {probs}")
        f = gate_findings(d)
        ok(len(f) == 4 and {x["severity"] for x in f} == {"MEDIUM"}
           and all(x["kind"] == "claim_percentage_is_proportion" and x["scope_declarable"] is False for x in f),
           f"U4 MEDIUM while no draft cites them: {f}")
        open(os.path.join(d, "manuscript.md"), "w").write("P(gain >= 10%) = [CLAIM:p10].\n")
        f = {x["claim"].split("]")[0][1:]: x for x in gate_findings(d)}
        ok(f["p10"]["severity"] == "HIGH" and "0.6%" in f["p10"]["claim"] and "58.7%" in f["p10"]["claim"]
           and f["cov_a"]["severity"] == "MEDIUM", f"U5 HIGH once the draft cites it (it would render): {f}")
        # A hand-written ledger (no spec) is checked the same way.
        os.remove(os.path.join(d, _builder().SPEC_REL))
        ok({x["claim"].split("]")[0][1:] for x in gate_findings(d)} == set(probs),
           "U6 a ledger without a spec is checked too")
        os.remove(os.path.join(d, _builder().LEDGER_REL))
        ok(gate_findings(d) == [], "U7 no ledger, no spec: nothing to check")
        many = [{"id": f"c{i}", "claim_kind": "percentage", "source_artifact": "models/derived.yaml",
                 "source_field": "coverage.region_a"} for i in range(8)]
        w("models/claims_spec.yaml", {"claims": many})
        f = gate_findings(d)
        ok(len(f) == _SCALE_CAP + 1 and f[-1]["claim"].startswith("3 more"), f"U8 capped: {len(f)}")

    # Codex round 1 (P47B-1, P47B-2).
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        yaml.safe_dump({
            "a": {"gain_pct": 10.4, "p_small": 0.005, "p10": 0.587},
            "g": {"gain_pct": 13.3,
                  "within": {"mean": 0.12, "ci_95": [-0.2, 0.48]},
                  "share": {"mean": 0.12, "ci_95": [0.05, 0.3]}},
        }, open(os.path.join(d, "models", "s.yaml"), "w"))
        mk = lambda cid, field, v: {"id": cid, "claim_kind": "percentage", "value": v,
                                    "source_artifact": "models/s.yaml", "source_field": field}
        got = {p["id"] for p in problems([
            mk("converted", "a.p_small", 0.5),       # ledger 0.5% = 100 x source 0.005: converted
            mk("as_source", "a.p_small", 0.005),     # ledger = source 0.005 beside *_pct > 1: flagged
            mk("mismatch", "a.p10", 0.3),            # neither: the provenance check's finding
            mk("ci_neg", "g.within.mean", 0.12),     # ci_95 crosses zero: a real 0.12%
            mk("ci_unit", "g.share.mean", 0.12),     # ci_95 inside [0, 1]: a proportion
            mk("unresolved", "a.nope", 0.4),
        ], d)}
        ok(got == {"as_source", "ci_unit"},
           f"U9 (Codex P47B-1/2) a converted value and a CI array crossing zero are exempt: {sorted(got)}")


    if fails:
        print("SELF-TEST FAILURES:", *fails, sep="\n  - ")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_dir", nargs="?")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return _self_test()
    if not a.run_dir or not os.path.isdir(a.run_dir):
        ap.error("run_dir required")
    fs = gate_findings(a.run_dir)
    for x in fs:
        print(f"[{x['severity']}] {x['kind']}: {x['claim']}")
    print(f"{len(fs)} finding(s)")
    return 1 if any(x["severity"] == "HIGH" for x in fs) else 0


if __name__ == "__main__":
    sys.exit(main())
