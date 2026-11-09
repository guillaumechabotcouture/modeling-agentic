#!/usr/bin/env python3
"""Benchmark bands checked against their published intervals (F6 S1-2).

    python3 -m toolkit.engines.benchmark_sources <pack.yaml>    # exit 1 and list each finding
    python3 toolkit/engines/benchmark_sources.py --self-test

A trial or cost benchmark (estimand type ratio, odds_ratio, interaction_or or cost_per_outcome) records where its band
comes from, in one of two ways:

    published: {lower, upper, level: 0.95, scale: ratio, table: "<where in the source>"}
    estimand_type: odds_ratio | risk_ratio | rate_ratio | hazard_ratio     # what the source reports
    diagnostic: "<as the source states it>"                                 # optional
    age_band: "<as the source states it>"                                   # optional
    estimand_mapping: "<why the engine's estimand stands for the published one>"   # when the types differ

or, when the band is not the published interval (no interval is published, or the band is deliberately different):

    band_override: {band: [lo, hi], reason: "<why>", status: <optional, e.g. pending_owner_review>}

The band is derived: `band_override.band` when present, else [published.lower, published.upper]. The stored `band`
field stays in the pack, because the runner, the fit and every reader of results read it there; this check fails on any
stored band that differs from its derivation, so the stored copy cannot drift from its source. It also fails when:
- a benchmark of those types has neither `published` nor `band_override` (fail closed);
- `published` is malformed, or its target lies outside it;
- the published estimand type does not match the engine estimand's `type` and no `estimand_mapping` explains it
  (an engine `ratio` stands for a risk or rate ratio; `odds_ratio` and `interaction_or` for an odds ratio; a hazard
  ratio always needs a mapping note);
- a `requires` interval on another benchmark differs from that benchmark's published interval.

Other estimand types (value, identity, coverage, curve_rules) are oracle or rule checks with no trial interval, and are
out of scope. Disease-agnostic: nothing here names a disease, trial or intervention.
"""
from __future__ import annotations

import math
import os
import sys

if not __package__:  # run as a file (the self-test runner): make the repository importable
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

SOURCED_TYPES = ("ratio", "odds_ratio", "interaction_or", "cost_per_outcome")
ESTIMAND_TYPES = ("odds_ratio", "risk_ratio", "rate_ratio", "hazard_ratio")
# engine estimand type -> published estimand types it stands for without a mapping note
COMPATIBLE = {"odds_ratio": {"odds_ratio"}, "interaction_or": {"odds_ratio"}, "ratio": {"risk_ratio", "rate_ratio"},
              "cost_per_outcome": set()}
SCALES = ("ratio",)
TOL = 1e-9


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _pair(x) -> bool:
    return isinstance(x, (list, tuple)) and len(x) == 2 and all(_num(v) for v in x) and x[0] <= x[1]


def _text(x) -> bool:
    return isinstance(x, str) and bool(x.strip())


def published_problems(p) -> list[str]:
    if not isinstance(p, dict):
        return ["published must be a mapping {lower, upper, level, scale, table}"]
    bad = []
    if not (_num(p.get("lower")) and _num(p.get("upper")) and p["lower"] <= p["upper"]):
        bad.append("published needs numbers lower <= upper")
    if not (_num(p.get("level")) and 0 < p["level"] < 1):
        bad.append("published.level must be a number in (0, 1), e.g. 0.95")
    if p.get("scale") not in SCALES:
        bad.append(f"published.scale must be one of {SCALES}")
    if not _text(p.get("table")):
        bad.append("published.table must say where in the source the interval is")
    return bad


def derive_band(b: dict):
    """The band a benchmark's sources give: the override's band, else the published interval, else None."""
    o = b.get("band_override")
    if isinstance(o, dict) and _pair(o.get("band")):
        return [float(o["band"][0]), float(o["band"][1])]
    p = b.get("published")
    if isinstance(p, dict) and not published_problems(p):
        return [float(p["lower"]), float(p["upper"])]
    return None


def _same(a, b) -> bool:
    return _pair(a) and _pair(b) and abs(a[0] - b[0]) <= TOL and abs(a[1] - b[1]) <= TOL


def check_benchmark(b: dict, byid: dict | None = None) -> list[str]:
    bid = b.get("id")
    e = b.get("estimand") or {}
    etype = e.get("type")
    if etype not in SOURCED_TYPES:
        return []
    bad = []
    pub, ovr = b.get("published"), b.get("band_override")
    if pub is None and ovr is None:
        return [f"{bid}: band {b.get('band')} has neither a published interval nor a recorded band_override"]
    if pub is not None:
        bad += [f"{bid}: {x}" for x in published_problems(pub)]
        if not published_problems(pub) and _num(b.get("target")) and not (pub["lower"] <= b["target"] <= pub["upper"]):
            bad.append(f"{bid}: target {b['target']} is outside the published interval [{pub['lower']}, {pub['upper']}]")
        st = b.get("estimand_type")
        if st not in ESTIMAND_TYPES:
            bad.append(f"{bid}: estimand_type {st!r} must be one of {ESTIMAND_TYPES} when a published interval is given")
        elif st not in COMPATIBLE.get(etype, set()) and not _text(b.get("estimand_mapping")):
            bad.append(f"{bid}: the source reports a {st} but the engine computes {etype!r}; refused without an "
                       "estimand_mapping note")
    if ovr is not None:
        if not (isinstance(ovr, dict) and _pair(ovr.get("band"))):
            bad.append(f"{bid}: band_override needs band: [lo, hi]")
        elif not _text(ovr.get("reason")):
            bad.append(f"{bid}: band_override needs a reason")
    want = derive_band(b)
    if want is not None and not _same(b.get("band"), want):
        src = "band_override" if ovr is not None else "published interval"
        bad.append(f"{bid}: band {b.get('band')} differs from its {src} {want} with no recorded override")
    for r, iv in (b.get("requires") or {}).items():
        rp = ((byid or {}).get(r) or {}).get("published")
        if isinstance(rp, dict) and not published_problems(rp) and not _same(iv, [rp["lower"], rp["upper"]]):
            bad.append(f"{bid}: requires {r} {iv} differs from {r}'s published interval [{rp['lower']}, {rp['upper']}]")
    return bad


def check_pack(pack: dict) -> list[str]:
    bs = [b for b in (pack or {}).get("benchmarks") or [] if isinstance(b, dict)]
    if not isinstance((pack or {}).get("benchmarks"), list):
        return ["pack has no benchmarks list"]
    byid = {b.get("id"): b for b in bs}
    return [x for b in bs for x in check_benchmark(b, byid)]


def failing_ids(pack: dict) -> set:
    bs = {b.get("id"): b for b in pack.get("benchmarks") or []}
    return {bid for bid, b in bs.items() if check_benchmark(b, bs)}


def main(argv=None) -> int:
    import yaml
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1:
        print(__doc__)
        return 2
    try:
        pack = yaml.safe_load(open(argv[0]))
    except (OSError, yaml.YAMLError) as ex:
        print(f"cannot read {argv[0]}: {ex}")
        return 1
    bad = check_pack(pack if isinstance(pack, dict) else {})
    for x in bad:
        print("FINDING " + x)
    print(f"{len(bad)} finding(s)" if bad else "every band matches its source or a recorded override")
    return 1 if bad else 0


def _self_test() -> int:
    import yaml
    fails = []

    def ok(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    pub = {"lower": 0.30, "upper": 0.67, "level": 0.95, "scale": "ratio", "table": "abstract"}
    good = {"id": "A", "target": 0.45, "band": [0.30, 0.67], "published": pub, "estimand_type": "odds_ratio",
            "estimand": {"type": "odds_ratio"}}
    ok(check_benchmark(good) == [], "BS1 a band equal to its published interval passes")
    sym = dict(good, band=[0.23, 0.67])
    ok(any("differs from its published interval" in x for x in check_benchmark(sym)),
       "BS2 a symmetric band wider than the published interval fails")
    ok(any("neither a published interval" in x for x in check_benchmark({"id": "N", "target": 1, "band": [0, 2],
                                                                         "estimand": {"type": "ratio"}})),
       "BS3 fail closed: a trial benchmark with no published interval and no override fails")
    ovr = dict(sym, band_override={"band": [0.23, 0.67], "reason": "deliberately wider: heterogeneity"})
    ok(check_benchmark(ovr) == [], "BS4 a band different from the published interval passes with a recorded override")
    ok(any("needs a reason" in x for x in check_benchmark(dict(ovr, band_override={"band": [0.23, 0.67]}))),
       "BS5 an override without a reason fails")
    ok(any("differs from its band_override" in x for x in check_benchmark(dict(ovr, band=[0.3, 0.67]))),
       "BS6 a band that differs from its own override fails")
    hr = dict(good, estimand_type="hazard_ratio", estimand={"type": "ratio"})
    ok(any("refused without an estimand_mapping" in x for x in check_benchmark(hr)),
       "BS7 a published hazard ratio against an engine ratio is refused without a mapping note")
    ok(check_benchmark(dict(hr, estimand_mapping="engine rate ratio over the follow-up stands for the HR")) == [],
       "BS8 ... and allowed with an explicit mapping note")
    ok(any("refused" in x for x in check_benchmark(dict(good, estimand_type="risk_ratio"))),
       "BS9 a published risk ratio against an engine odds ratio is refused")
    ok(check_benchmark(dict(good, estimand_type="rate_ratio", estimand={"type": "ratio"})) == [],
       "BS10 a rate ratio against an engine ratio is allowed")
    ok(any("estimand_type" in x for x in check_benchmark({k: v for k, v in good.items() if k != "estimand_type"})),
       "BS11 a published interval without an estimand type fails")
    ok(any("published.table" in x for x in check_benchmark(dict(good, published=dict(pub, table="")))),
       "BS12 a published interval without its location in the source fails")
    ok(any("outside the published" in x for x in check_benchmark(dict(good, target=0.8))),
       "BS13 a target outside its published interval fails")
    ok(check_benchmark({"id": "V", "band": [0, 9], "estimand": {"type": "value"}}) == [],
       "BS14 oracle and rule types (value, identity, coverage, curve_rules) are out of scope")
    req = {"id": "I", "target": 2.0, "band": [1.0, 4.0], "band_override": {"band": [1.0, 4.0], "reason": "r"},
           "requires": {"A": [0.23, 0.67]}, "estimand": {"type": "interaction_or"}}
    ok(any("requires A" in x for x in check_pack({"benchmarks": [good, req]})),
       "BS15 a requires interval that differs from the referenced published interval fails")
    ok(check_pack({}) == ["pack has no benchmarks list"], "BS16 fail closed: a pack without benchmarks is a finding")
    ok(derive_band(good) == [0.30, 0.67] and derive_band(ovr) == [0.23, 0.67] and derive_band({"id": "x"}) is None,
       "BS17 derive_band: override first, then the published interval")
    # the real packs: every band matches its source or a recorded override
    root = os.path.dirname(os.path.abspath(__file__))
    mp = os.path.join(root, "malaria", "benchmarks", "pack.yaml")
    pk = yaml.safe_load(open(mp))
    bad = check_pack(pk)
    ok(bad == [], "BS18 the malaria pack: every band matches its published interval or a recorded override"
       + ("" if not bad else f" (failing: {sorted(failing_ids(pk))}; " + "; ".join(bad) + ")"))
    for name in ("compartmental", "renewal"):
        p = os.path.join(root, name, "benchmarks", "pack.yaml")
        if os.path.exists(p):
            ok(check_pack(yaml.safe_load(open(p))) == [], f"BS19 the {name} pack passes (oracle types only)")
    # the runner CLI and the harness step refuse a pack whose band differs from its source, before any run
    import tempfile
    from toolkit.engines import benchmark as BM
    from toolkit.spec import engine_parameters as EP
    toy = {"engine": "toolkit.engines.contract:ToyEngine", "seeds": [1],
           "initialisation": {"method": "closed form", "evidence": ["not applicable: no dynamic state"]},
           "horizon": {"start": 0, "stop": 3, "window": [0, 3], "windows": {"w": [1, 2]}},
           "settings": {"s": {"unit": {"x": 0.0}}},
           "arms": {"none": {"setting": "s", "package": {}}, "a": {"setting": "s", "package": {"a": 1}}},
           "benchmarks": [{"id": "T1", "metric": "a vs none", "target": 0.4, "band": [0.3, 0.5],
                           "published": dict(pub, lower=0.3, upper=0.5), "estimand_type": "risk_ratio",
                           "estimand": {"type": "ratio", "num": "a", "den": "none", "value": "per_capita.cases.all"}}],
           "fit": [{"param": "eff_a", "grid": [0.3, 0.5], "targets": ["T1"]}]}
    wide = dict(toy, benchmarks=[dict(toy["benchmarks"][0], band=[0.2, 0.5])])
    with tempfile.TemporaryDirectory() as d:
        rc = {}
        for name, pk_ in (("ok", toy), ("wide", wide)):
            path = os.path.join(d, name + ".yaml")
            yaml.safe_dump(pk_, open(path, "w"))
            rc[name] = BM.main([path, "validate"])
    ok(rc == {"ok": 0, "wide": 2}, f"BS20 the runner CLI validates a matching band and refuses a wider one ({rc})")
    try:
        EP.spec_benchmarks(wide, None, None)
        refused = False
    except ValueError as ex:
        refused = "do not match their sources" in str(ex)
    ok(refused, "BS21 the harness step (spec_benchmarks) refuses the pack before it touches the engine or spec")
    from toolkit.analysis import joint_calibration as JC
    orig = EP.benchmark_pack
    try:
        EP.benchmark_pack = lambda eng: (wide, None)
        jw = JC._benchmarks(None)
        EP.benchmark_pack = lambda eng: (toy, None)
        jo = JC._benchmarks(None)
    finally:
        EP.benchmark_pack = orig
    ok(jw[0] is None and "do not match their sources" in str(jw[2]) and jo[0] is toy and jo[1] == {"T1"},
       "BS22 joint calibration reads a band as a 95% interval only when it matches its source")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    sys.exit(main())
