#!/usr/bin/env python3
"""Benchmark coverage: which package components each engine benchmark speaks to (owner decision 2026-11-12).

    python3 toolkit/engines/benchmark_coverage.py --self-test

An engine is not eligible for a spec when a benchmark that covers a component value the spec uses is not
`in_band` in the engine's verified benchmark record, unless the signed spec declares a departure for it
(`engine_departures`). This is a precondition of the engine choice, not a gate after the analysis.

Each engine with benchmarks keeps `benchmark_coverage.yaml` beside its module (a yaml file: it does not change the
engine's code fingerprint):

    engine: <card name>
    absent_values: {<component>: [<value meaning "not delivered">]}
    benchmarks:
      <id>: {covers: {<component>: [<values>]}, estimand: <ESTIMANDS>, versus: {<component>: <value>},
             match: any | all, note: <text>}
      <id>: {covers: {}, covers_none_because: <text>}      # a benchmark of no package component, said explicitly
    components_without_benchmark: {<component> | "*": <reason>}   # used, but no benchmark covers it (listed)

`match: all` (an interaction) applies only when the spec uses every covered component. Statuses: only `in_band`
passes. `out_of_band` contradicts the trial. `not_assessable` gives no evidence for a component the decision
uses (a requirement missed, values not transcribed, a calibrator missing). `band_mis_based` says the band cannot
be met at the pack's prices, so the benchmark neither supports nor refutes the engine. Both fail closed: the owner
can sign a departure saying why the decision may still use the engine. A record that is not the verified
record of the engine version in use (toolkit/engines/benchmark.py verified_results) counts for nothing.

A departure: {engine: <card name>, benchmark: <id>, reason: <text>, decision_test: <analysis.decision_tests id>}
or, instead of decision_test, no_decision_test_because: <text>. Nothing here names a disease.
"""
from __future__ import annotations

import os
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

FILE = "benchmark_coverage.yaml"
ESTIMANDS = ("effect_vs_none", "effect_vs_component", "durability", "interaction", "cost_per_outcome")
DEPARTURE_FIELDS = ("engine", "benchmark", "reason", "decision_test", "no_decision_test_because")


def path_for(ref: str) -> str:
    """The coverage file beside an in-repository engine module, located without importing it."""
    mod = str(ref).split(":", 1)[0]
    return os.path.join(ROOT, *mod.split(".")[:-1], FILE)


def load(path: str) -> tuple[dict | None, list[str]]:
    """(coverage, problems); (None, []) when the file does not exist."""
    if not os.path.exists(path):
        return None, []
    try:
        doc = yaml.safe_load(open(path)) or {}
    except (OSError, yaml.YAMLError) as e:
        return {}, [f"{FILE} unreadable: {e}"]
    return doc, problems(doc)


def _txt(x) -> bool:
    return isinstance(x, str) and bool(x.strip())


def problems(doc) -> list[str]:
    """Shape problems of a coverage document (fail closed: any problem makes the engine ineligible)."""
    if not isinstance(doc, dict) or not _txt(doc.get("engine")) or not isinstance(doc.get("benchmarks"), dict):
        return [f"{FILE}: needs engine (the card name) and a benchmarks mapping"]
    bad = []
    for k in ("absent_values", "components_without_benchmark"):
        if not isinstance(doc.get(k, {}), dict):
            bad.append(f"{FILE}: {k} must be a mapping")
    for c, why in (doc.get("components_without_benchmark") or {}).items():
        if not _txt(why):
            bad.append(f"{FILE}: components_without_benchmark.{c} needs a reason")
    for i, e in doc["benchmarks"].items():
        cov = e.get("covers") if isinstance(e, dict) else None
        if not isinstance(cov, dict) or not all(isinstance(v, list) and v for v in cov.values()):
            bad.append(f"{FILE}: {i}: covers must map components to non-empty value lists")
        elif not cov and not _txt(e.get("covers_none_because")):
            bad.append(f"{FILE}: {i}: a benchmark that covers no component says why (covers_none_because)")
        elif cov and e.get("estimand") not in ESTIMANDS:
            bad.append(f"{FILE}: {i}: estimand {e.get('estimand')!r} not in {ESTIMANDS}")
        elif cov and e.get("estimand") == "effect_vs_component" and not isinstance(e.get("versus"), dict):
            bad.append(f"{FILE}: {i}: an effect against another component names it (versus)")
        elif isinstance(e, dict) and e.get("match", "any") not in ("any", "all"):
            bad.append(f"{FILE}: {i}: match must be any or all")
    return bad


def _used(needs_components: dict, absent: dict) -> set:
    return {(c, v) for c, vals in (needs_components or {}).items() for v in (vals or [])
            if v not in (absent.get(c) or [])}


def _applies(entry: dict, used: set) -> list:
    hits = [(c, v) for c, vals in (entry.get("covers") or {}).items() for v in vals if (c, v) in used]
    if entry.get("match") == "all":
        comps = set(entry.get("covers") or {})
        return hits if comps and comps <= {c for c, _ in hits} else []
    return hits


def gate(cov: dict | None, cov_problems: list, card, needs_components: dict, record, departures: list,
         departures_status: str) -> dict:
    """Benchmark precondition for one engine. `record()` returns (rows, problem) for the engine version in use and
    is called only when a benchmark covers a used component. `departures_status`: "signed" (they are in the signed
    spec), "proposed" (pre-sign check) or "unsigned" (they do not count). Returns {why_not, departures, uncovered,
    blocking, record}."""
    out = {"why_not": [], "departures": [], "uncovered": {}, "blocking": [], "record": None}
    if cov is None:
        if card.benchmarks:
            out["why_not"].append(f"the engine has benchmarks but no {FILE} saying which components they cover")
        return out
    if cov_problems:
        out["why_not"] += cov_problems
        return out
    absent, entries = cov.get("absent_values") or {}, cov["benchmarks"]
    used = _used(needs_components, absent)
    nob = cov.get("components_without_benchmark") or {}
    covered = {(c, v) for e in entries.values() for c, vals in (e.get("covers") or {}).items() for v in vals}
    for c, v in sorted(used - covered, key=str):
        if c in nob or "*" in nob:
            out["uncovered"][c] = nob.get(c, nob.get("*"))
        else:
            out["why_not"].append(f"component {c}={v!r}: {FILE} names no benchmark for it and does not declare it "
                                  "uncovered (components_without_benchmark)")
    relevant = {i: _applies(e, used) for i, e in entries.items()}
    relevant = {i: h for i, h in relevant.items() if h}
    if not relevant:
        return out
    rows, why = record()
    out["record"] = why or "verified"
    if why:
        out["why_not"].append(f"benchmark record refused ({why}): the benchmarks covering "
                              f"{', '.join(sorted(relevant))} cannot be read for this engine version")
        return out
    byid = {r.get("id"): r for r in rows}
    out["why_not"] += [f"benchmark {i} in the record has no entry in {FILE} (its coverage is unknown)"
                       for i in byid if i not in entries]
    deps = {d.get("benchmark"): d for d in departures or [] if isinstance(d, dict) and d.get("engine") == cov["engine"]}
    for i, hits in sorted(relevant.items()):
        r = byid.get(i) or {}
        st = r.get("status", "missing from the record")
        if st == "in_band":
            continue
        comps = [f"{c}={v}" for c, v in hits]
        dep = deps.get(i)
        if dep is not None and departures_status in ("signed", "proposed"):
            out["departures"].append(dict(dep, status=departures_status, benchmark_status=st, components=comps))
            continue
        out["blocking"].append(i)
        note = {"unsigned": "; the spec's departure for it is not signed (or the spec changed since sign-off)"}.get(
            departures_status, "") if dep is not None else ""
        out["why_not"].append(
            f"benchmark {i} ({r.get('metric', '')}) covers {', '.join(comps)} and is {st} (computed "
            f"{r.get('computed')}, band {r.get('band')}): the engine is not eligible for it without an owner-signed "
            f"engine_departures entry{note}")
    return out


def departure_problems(raw: dict) -> list[str]:
    """Shape of the spec's `engine_departures` (each names a decision test, or says why none is possible)."""
    deps = raw.get("engine_departures")
    if deps is None:
        return []
    if not isinstance(deps, list):
        return ["engine_departures must be a list"]
    tests = {str(t.get("id")) for t in ((raw.get("analysis") or {}).get("decision_tests") or []) if isinstance(t, dict)}
    bad, seen = [], set()
    for j, d in enumerate(deps):
        if not isinstance(d, dict):
            bad.append(f"engine_departures[{j}] must be a mapping")
            continue
        bad += [f"engine_departures[{j}]: unknown field {k}" for k in d if k not in DEPARTURE_FIELDS]
        bad += [f"engine_departures[{j}]: {k} must be a non-empty text" for k in ("engine", "benchmark", "reason")
                if not _txt(d.get(k))]
        has_dt, has_none = "decision_test" in d, "no_decision_test_because" in d
        if has_dt == has_none:
            bad.append(f"engine_departures[{j}]: name a decision_test or say no_decision_test_because (exactly one)")
        elif has_dt and str(d["decision_test"]) not in tests:
            bad.append(f"engine_departures[{j}]: decision_test {d['decision_test']!r} is not an analysis.decision_tests id")
        elif has_none and not _txt(d["no_decision_test_because"]):
            bad.append(f"engine_departures[{j}]: no_decision_test_because must be a non-empty text")
        key = (d.get("engine"), d.get("benchmark"))
        if key in seen:
            bad.append(f"engine_departures[{j}]: a second departure for {key[1]} of {key[0]}")
        seen.add(key)
    return bad


def _test_of(d: dict) -> str:
    return (f"decision test {d['decision_test']}" if d.get("decision_test") is not None
            else f"no decision test: {d.get('no_decision_test_because')}")


def sheet_lines(raw: dict, candidates: list | None) -> list[str]:
    """The sign-off sheet's section: each departure the spec declares, with what the candidates' records show."""
    deps = [d for d in raw.get("engine_departures") or [] if isinstance(d, dict)]
    rows = {(r.get("name"), d.get("benchmark")): d for r in candidates or [] for d in r.get("departures") or []}
    if not deps:
        return []
    out = ["## Engine departures from benchmark evidence",
           "Signing accepts that the engine is used for the components each benchmark covers although its result "
           "is not in the trial's band."]
    for d in deps:
        seen = rows.get((d.get("engine"), d.get("benchmark")))
        what = (f" Covers {', '.join(seen['components'])}; the record shows {seen['benchmark_status']}." if seen
                else " No candidate engine's record needs it.")
        out.append(f"- **{d.get('benchmark')}** (engine {d.get('engine')}): {d.get('reason')} ({_test_of(d)}).{what}")
    return out + [""]


def results_lines(raw: dict) -> list[str]:
    """results.md: the signed departures, as declared limitations of the engine's evidence."""
    deps = [d for d in raw.get("engine_departures") or [] if isinstance(d, dict)]
    if not deps:
        return []
    return (["## Declared engine departures (signed in the spec)", "",
             "The engine was used for components whose benchmark against the trial is not in band; the owner "
             "signed each departure:", ""]
            + [f"- {d.get('benchmark')} (engine {d.get('engine')}): {d.get('reason')} ({_test_of(d)})." for d in deps]
            + [""])


def _pack_ids(engine_dir: str) -> list:
    p = os.path.join(engine_dir, "benchmarks", "pack.yaml")
    return [b.get("id") for b in (yaml.safe_load(open(p)) or {}).get("benchmarks") or []] if os.path.exists(p) else []


def _self_test() -> int:
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)

    eng = os.path.join(ROOT, "toolkit", "engines")
    for name in sorted(os.listdir(eng)):
        d = os.path.join(eng, name)
        ids = _pack_ids(d)
        if not ids:
            continue
        doc, bad = load(os.path.join(d, FILE))
        miss = [i for i in ids if i not in ((doc or {}).get("benchmarks") or {})]
        extra = [i for i in ((doc or {}).get("benchmarks") or {}) if i not in ids]
        ok(doc is not None and not bad and not miss and not extra,
           f"BC1 {name}: {FILE} maps every benchmark of its pack and no other ({bad[:2]}, missing {miss}, extra {extra})")
        card = yaml.safe_load(open(os.path.join(d, "card.yaml"))) if os.path.exists(os.path.join(d, "card.yaml")) else {}
        comps = set((card or {}).get("components") or {})
        named = {c for e in (doc or {}).get("benchmarks", {}).values() for c in (e.get("covers") or {})}
        named |= set((doc or {}).get("components_without_benchmark") or {}) - {"*"}
        ok(named <= comps, f"BC2 {name}: every component the coverage names is a card component ({named - comps})")
    ok(problems({"engine": "x", "benchmarks": {"A": {"covers": {}}}}) != [],
       "BC3 a benchmark covering nothing without saying why is refused")
    ok(problems({"engine": "x", "benchmarks": {"A": {"covers": {"c": [1]}, "estimand": "vibes"}}}) != [],
       "BC4 an unknown estimand is refused")

    class Card:
        benchmarks = []
    cov = {"engine": "e", "benchmarks": {"I": {"covers": {"p": [1], "q": [1]}, "match": "all",
                                               "estimand": "interaction"}},
           "components_without_benchmark": {"p": "r", "q": "r"}}
    rec = lambda: ([{"id": "I", "status": "out_of_band"}], None)  # noqa: E731
    ok(gate(cov, [], Card, {"p": [1]}, rec, [], "unsigned")["why_not"] == [],
       "BC5 an interaction benchmark (match all) does not apply when only one of its components is used")
    ok(gate(cov, [], Card, {"p": [1], "q": [1]}, rec, [], "unsigned")["blocking"] == ["I"],
       "BC6 it applies when both are used")
    ok(gate(None, [], Card, {"p": [1]}, rec, [], "unsigned")["why_not"] == [],
       "BC7 an engine with no benchmarks needs no coverage file")
    dep = {"engine": "e", "benchmark": "I", "reason": "why", "no_decision_test_because": "a structural choice"}
    md = "\n".join(results_lines({"engine_departures": [dep]}))
    ok("Declared engine departures" in md and "a structural choice" in md and results_lines({}) == []
       and sheet_lines({}, None) == [], "BC8 results.md declares each signed departure; nothing when there is none")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    print(__doc__)
    sys.exit(2)
