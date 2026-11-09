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


def load_references(s) -> tuple[list[dict], list[str]]:
    raise NotImplementedError("F6 S2-5 beta")


def by_population(A, theta, zs, bands, arm) -> dict:
    raise NotImplementedError("F6 S2-5 beta")


def verdict(row, th=None) -> str:
    raise NotImplementedError("F6 S2-5 beta")


def compare(ref, point, draws, level=0.95) -> dict:
    raise NotImplementedError("F6 S2-5 beta")


def daly_parts(A, cfg, arm, out0, by_pop, point) -> dict:
    raise NotImplementedError("F6 S2-5 beta")


def run(A, out0, z0, point, col, level=0.95) -> dict:
    raise NotImplementedError("F6 S2-5 beta")


def findings(raw, data) -> list[dict]:
    raise NotImplementedError("F6 S2-5 beta")


def claims(c, art, data) -> list[dict]:
    raise NotImplementedError("F6 S2-5 beta")


def results_lines(data) -> list[str]:
    raise NotImplementedError("F6 S2-5 beta")


class Collector:
    def __init__(self, A):
        raise NotImplementedError("F6 S2-5 beta")


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
    rp = compare(dict(ref, kind="reported"), 900.0, None)
    ok(rp["status"] == "information" and findings({"validation": {"burden": {}}}, {"references": [rp]}) == [],
       "BV4 a reported value is information only: never judged, never a finding")
    pt = compare(dict(ref, age_band="u5"), 120.0, None)
    ok(pt["model"]["interval_from"] == "point" and "all-age" in pt["model"]["note"],
       "BV5 an age-banded reference is the point value, with a statement")
    sec = {"validation": {"burden": {"csv": "x.csv"}}}
    fo = findings(sec, {"references": [r2, compare(ref, 150.0, None)]})
    ok([(x["kind"], x["scope_declarable"], x["key"]) for x in fo] == [("burden_outside_tolerance", True, "burden:r")],
       f"BV6 a group outside tolerance is one declarable finding: {fo}")
    um = compare(ref, None, None)
    bad_st = dict(r2, status="within")
    wide = dict(compare(ref, 150.0, None), tolerance={"type": "ratio", "ratio_band": [0.01, 100.0]})
    got = {x["kind"]: x["scope_declarable"] for x in findings(sec, {"references": [um]})}
    ok(got == {"burden_reference_unmatched": False}, f"BV7 an unmatched reference fails closed: {got}")
    got = [x["kind"] for x in findings(sec, {"references": [bad_st, wide]})]
    ok(got == ["burden_validation_inconsistent"] * 2,
       f"BV8 a status its numbers do not support, or a band wider than the manifest's, fails closed: {got}")
    ok([x["kind"] for x in findings(sec, None)] == ["burden_validation_missing"] and findings({}, None) == [],
       "BV9 a declared check with no artifact fails closed; no declaration, no finding")
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
                 ("duplicate id", [good, good], "unique")]
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
