#!/usr/bin/env python3
"""Effect by setting against the trials: a warning on the sign-off sheet (owner, 2026-11-17/18).

    python3 toolkit/engines/effect_settings.py <run_dir>     # the covered ranges and the units outside (JSON rows)
    python3 toolkit/engines/effect_settings.py --self-test

An engine carries a trial's relative effect to the decision's units. Where a unit's setting lies outside every
setting the trials behind a component's effect were run in, the engine's effect there is an extrapolation. This
lists those cases on the sign-off sheet for the owner. It never refuses a signature and adds no spec key.

The trials' covered range is read from the engine's benchmark pack: for each spec component an allowed package
holds (`Spec.packages()`), the effect benchmarks that cover what it delivers (benchmark_coverage.yaml; every estimand
but cost_per_outcome), the arms their estimand names (num, den, the four interaction arms, an `arms` list, and the
`arm` of a value/identity side), and those arms' settings' unit attributes. A numeric attribute gives [min, max]
over the settings. A mapping gives one range per key (`attr.k`); a scalar setting value of an attribute the units
table binds per key counts for every key, and the mean of a mapping counts for an attribute bound as one column.
The units table gives each unit's value for each attribute the spec binds with `{column}` or `{columns: {k: col}}`
(a list of columns, a profile, is not compared). A unit is outside when its value is beyond the range by more than
TOL of the range's width (of the value, or of 1 for a zero, when the range is one point). An attribute that every
setting of the pack states with the same value is a convention of the pack, not a trial setting, and is skipped. A
row says whether a unit_attribute decision test on a bound column changes the units outside. Nothing here names a
disease; components and attributes come from the spec and the engine's pack. Nothing runs an engine.
"""
from __future__ import annotations

import csv
import json
import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

TOL = 0.05          # a value beyond the covered range by less than 5% of its width counts as inside (rounding)
SECTION = "## Effect by setting against the trials (warning)"


def _num(x):
    if isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _flat(attr, v, scalar_keys: set, keyed: dict) -> dict:
    """{attribute key: number} of one setting value. `scalar_keys`: attributes the units table binds as one column;
    `keyed`: {attribute: [keys]} it binds per key."""
    out = {}
    if isinstance(v, dict):
        vals = {k: _num(x) for k, x in v.items()}
        vals.update({k: 0.0 for k in keyed.get(attr, ()) if k not in v})     # a key the mapping omits has zero share
        out = {f"{attr}.{k}": x for k, x in vals.items() if x is not None}
        if attr in scalar_keys and out:
            out[attr] = sum(out.values()) / len(out)
        return out
    n = _num(v)
    if n is None:
        return {}
    if attr not in keyed or attr in scalar_keys:
        out[attr] = n
    for k in keyed.get(attr, ()):           # a scalar applies to every key the units table binds
        out[f"{attr}.{k}"] = n
    return out


def _refs(raw) -> list:
    eng = raw.get("engine")
    if isinstance(eng, str) and ":" in eng:
        return [eng]
    if isinstance(eng, dict) and eng.get("ref"):
        return [eng["ref"]]
    return [str(c) for c in (eng.get("candidates") or [])] if isinstance(eng, dict) else []


def unit_values(raw, run_dir) -> tuple[dict, dict, dict, str | None]:
    """({attribute key: {unit: value}}, {attribute key: column}, {unit: population}, population column)."""
    du = raw.get("decision_units") or {}
    cols = {}
    for a, b in (du.get("engine_attributes") or {}).items():
        if isinstance(b, dict) and "value" in b:
            continue                                # a fixed value for every unit, not a column
        if isinstance(b, dict) and isinstance(b.get("column"), str) and "split" not in b:
            cols[a] = b["column"]
        elif isinstance(b, dict) and isinstance(b.get("columns"), dict):
            cols.update({f"{a}.{k}": c for k, c in b["columns"].items() if isinstance(c, str)})
    pops = du.get("populations") or {}
    pcol = next((p.get("column") for p in pops.values() if isinstance(p, dict) and p.get("column")), None)
    with open(os.path.join(run_dir, du["csv"]), encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    uid = du.get("id")
    vals = {k: {r[uid]: _num(r.get(c)) for r in rows if _num(r.get(c)) is not None} for k, c in cols.items()}
    pop = {r[uid]: _num(r.get(pcol)) or 0.0 for r in rows} if pcol else {r[uid]: 1.0 for r in rows}
    return {k: v for k, v in vals.items() if v}, cols, pop, pcol


def _arms(est) -> list:
    names = [est.get(k) for k in ("num", "den", "a11", "a10", "a01", "a00")] + list(est.get("arms") or [])
    names += [s_.get("arm") for s_ in (est.get("a"), est.get("b")) if isinstance(s_, dict)]
    return [a for n in names for a in (n if isinstance(n, list) else [n]) if isinstance(a, str)]


def _allowed(raw, run_dir):
    """The spec with the packages execution allows (`_packages` for benchmark_coverage), when they resolve."""
    try:
        from toolkit.spec import schema
        pk = schema.Spec(raw, run_dir).packages()
        return dict(raw, _packages=[set(cs) for cs in pk.values()]) if pk else raw
    except Exception:  # noqa: BLE001 -- the declared packages, as benchmark_coverage reads them
        return raw


def trial_ranges(raw, ref, scalar_keys: set, keyed: dict, run_dir=None) -> dict:
    """{spec component: {attribute key: {lo, hi, benchmarks, settings}}} over the covering effect benchmarks; {} for
    an engine without a coverage file or pack."""
    from toolkit.engines import benchmark_coverage as BC
    cov, _bad = BC.load(BC.path_for(ref))
    pack = BC._pack(ref)
    if cov is None or pack is None:
        return {}
    spec = _allowed(raw, run_dir)
    smap = BC._smap(spec, cov.get("absent_values") or {})
    decl = {str(b.get("id")): b for b in pack.get("benchmarks") or [] if isinstance(b, dict)}
    arms, settings = pack.get("arms") or {}, pack.get("settings") or {}
    out: dict = {}
    for bid, e in (cov.get("benchmarks") or {}).items():
        if not isinstance(e, dict) or e.get("estimand") == "cost_per_outcome" or not e.get("covers"):
            continue
        sets = {(arms.get(a) or {}).get("setting") for a in _arms((decl.get(str(bid)) or {}).get("estimand") or {})}
        for s, _c, _v in BC._hits(e, smap, spec):
            for sn in sorted(sets - {None}):
                unit = (settings.get(sn) or {}).get("unit") or {}
                for k, x in ((k, x) for a, v in unit.items() for k, x in _flat(a, v, scalar_keys, keyed).items()):
                    r = out.setdefault(s, {}).setdefault(k, {"lo": x, "hi": x, "benchmarks": set(), "settings": set()})
                    r["lo"], r["hi"] = min(r["lo"], x), max(r["hi"], x)
                    r["benchmarks"].add(str(bid))
                    r["settings"].add(sn)
    return out


def conventions(ref, scalar_keys: set, keyed: dict) -> set:
    """Attribute keys that every setting of the pack states, all with one value: a convention, not a trial setting."""
    from toolkit.engines import benchmark_coverage as BC
    sts = [st for st in ((BC._pack(ref) or {}).get("settings") or {}).values()]
    seen: dict = {}
    for st in sts:
        for a, v in ((st or {}).get("unit") or {}).items():
            for k, x in _flat(a, v, scalar_keys, keyed).items():
                seen.setdefault(k, []).append(x)
    return {k for k, v in seen.items() if len(sts) > 1 and len(v) == len(sts) and _same(v)}


def _same(vals) -> bool:
    """Every value equal (exactly, up to float noise relative to the values themselves)."""
    lo, hi = min(vals), max(vals)
    return hi - lo <= 1e-12 * max(abs(lo), abs(hi))


def tested_by(raw, row) -> list:
    """The unit_attribute decision tests on a column bound to the attribute that reach the units outside."""
    from toolkit.spec import perturb as PT       # W0-B: a perturb test in its per-target form
    out = []
    for t in PT.tests(raw.get("analysis")):
        ua = t.get("unit_attribute") if t.get("type") == "unit_attribute" else None
        if not isinstance(ua, dict) or str(ua.get("column")) != row["column"]:
            continue
        # the runner's default is every unit, except for set_from, which changes only the table's rows (W0-B review)
        us = ua.get("units", None if "set_from" in ua else "all")
        if us == "all" or (isinstance(us, list) and set(map(str, us)) & set(row["outside_units"])):
            out.append(str(t.get("id")))
    return out


def outside(raw, run_dir) -> tuple[list, list]:
    """(rows, notes): one row per (engine, spec component, attribute) with units outside the covered range; notes
    say what was not compared."""
    vals, cols, pop, pcol = unit_values(raw, run_dir)
    scalar = {k for k in cols if "." not in k}
    keyed: dict = {}
    for k in cols:
        if "." in k:
            a, _, sub = k.partition(".")
            keyed.setdefault(a, []).append(sub)
    tot = sum(pop.values()) or 1.0
    rows, notes = [], []
    for ref in _refs(raw):
        tr = trial_ranges(raw, ref, scalar, keyed, run_dir)
        if not tr:
            notes.append(f"`{ref}`: no benchmark pack with covering effect benchmarks: nothing compared")
            continue
        conv = conventions(ref, scalar, keyed)
        if not any(k in vals and k not in conv for rs in tr.values() for k in rs):
            notes.append(f"`{ref}`: no attribute the units table binds is stated by the trial settings: nothing "
                         "compared")
        for comp, rs in sorted(tr.items()):
            for k, r in sorted(rs.items()):
                if k not in vals or k in conv:
                    continue
                w = (r["hi"] - r["lo"]) or abs(r["hi"]) or 1.0
                out_ = {u: v for u, v in vals[k].items() if v < r["lo"] - TOL * w or v > r["hi"] + TOL * w}
                if out_:
                    xs = sorted(out_.values())
                    rows.append({"engine": ref, "component": comp, "attribute": k, "column": cols[k],
                                 "covered": [r["lo"], r["hi"]], "benchmarks": sorted(r["benchmarks"]),
                                 "settings": sorted(r["settings"]), "units_outside": len(out_), "units": len(vals[k]),
                                 "outside_units": sorted(out_), "population_column": pcol,
                                 "population_share": sum(pop.get(u, 0) for u in out_) / tot,
                                 "range_outside": [xs[0], xs[-1]]})
    return rows, notes


def sheet_lines(raw, run_dir) -> list[str]:
    """The sign-off sheet's warning section; [] when the spec allocates nothing through an engine map."""
    try:
        if not (raw.get("options") or {}).get("engine_map") or not _refs(raw):
            return []
        rows, notes = outside(raw, run_dir)
    except Exception as e:  # noqa: BLE001 -- the sheet says so rather than omitting the section
        return [SECTION, "", f"Could not be computed: {type(e).__name__}: {e}", ""]
    try:
        return render(raw, rows, notes)
    except Exception as e:  # noqa: BLE001 -- the sheet says so rather than failing to render
        return [SECTION, "", f"Could not be rendered: {type(e).__name__}: {e}", ""]


def render(raw, rows, notes) -> list[str]:
    out = [SECTION, "", "Where a unit's setting lies outside every trial setting behind a component's effect, the "
           "engine's effect there is an extrapolation. Shown for the owner; it does not refuse signing.", ""]
    out += [f"- {n}" for n in notes] + ([""] if notes else [])
    if not rows:
        return out + (["No unit is outside the compared trial settings."] if not notes else []) + [""]
    many = len({r["engine"] for r in rows}) > 1
    out += [("| Engine " if many else "") + "| Component | Attribute | Trials' range (settings) | Units outside "
            "(population share) | Tested by |", ("|---" if many else "") + "|---|---|---|---|---|"]
    for r in rows:
        t = tested_by(raw, r)
        out.append((f"| `{r['engine']}` " if many else "")
                   + f"| {r['component']} | {r['attribute']} | {r['covered'][0]:g} to {r['covered'][1]:g} "
                   f"({', '.join(map(str, r['settings']))}) | {r['units_outside']} of {r['units']} "
                   f"({100 * r['population_share']:.0f}% of {r['population_column'] or 'units'}), "
                   f"{r['range_outside'][0]:g} to {r['range_outside'][1]:g} | "
                   + (f"unit_attribute test(s) {', '.join(t)}" if t else "no test varies it") + " |")
    return out + [""]


def _self_test() -> int:
    import tempfile
    import yaml
    from toolkit.engines import benchmark_coverage as BC
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        c or fails.append(m)

    REF = "toolkit.engines.contract:ToyEngine"
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "eng", "benchmarks"))
        os.makedirs(os.path.join(d, "run", "data"))
        cov = os.path.join(d, "eng", BC.FILE)
        yaml.safe_dump({"engine": "toy", "absent_values": {"x": ["none"]}, "benchmarks": {
            "T1": {"covers": {"x": ["a", "b"]}, "estimand": "effect_vs_none"},
            "T2": {"covers": {"x": ["b"]}, "estimand": "effect_vs_component", "versus": {"x": "a"}},
            "T3": {"covers": {"x": ["b"]}, "estimand": "effect_vs_none"},
            "C1": {"covers": {"x": ["b"]}, "estimand": "cost_per_outcome", "cost_drivers": ["d"]}}}, open(cov, "w"))
        yaml.safe_dump({"seeds": [1], "settings": {
            "s1": {"unit": {"r": 0.2, "f": 0.3, "sp": {"p": 0.5, "q": 0.5}}},
            "s2": {"unit": {"r": 0.6, "f": 0.3, "sp": {"p": 1.0}}}, "s3": {"unit": {"r": 0.99, "f": 0.3}},
            "s4": {"unit": {"r": 0.4, "f": 0.3, "sp": 0.7}}},
            "arms": {"n0": {"setting": "s1"}, "na": {"setting": "s1"}, "ma": {"setting": "s2"}, "mb": {"setting": "s2"},
                     "c0": {"setting": "s3"}, "cb": {"setting": "s3"}, "v1": {"setting": "s4"}},
            "benchmarks": [{"id": "T1", "estimand": {"type": "ratio", "num": "na", "den": "n0"}},
                           {"id": "T2", "estimand": {"type": "ratio", "num": "mb", "den": "ma"}},
                           {"id": "T3", "estimand": {"type": "value", "a": {"arm": "v1", "value": "x"}, "b": 0.5}},
                           {"id": "C1", "estimand": {"type": "cost_per_outcome", "num": "cb", "den": "c0"}}]},
                       open(os.path.join(d, "eng", "benchmarks", "pack.yaml"), "w"))
        BC.OVERRIDES[REF] = cov
        run = os.path.join(d, "run")
        with open(os.path.join(run, "data", "units.csv"), "w") as fh:
            fh.write("u,pop,r,p,f\n" + "".join(f"u{i},{10 if i < 8 else 100},{0.1 * i:.1f},{0.1 * i:.1f},{0.1 * i:.1f}\n"
                                              for i in range(10)))
        raw = {"engine": {"ref": REF},
               "decision_units": {"csv": "data/units.csv", "id": "u", "populations": {"all": {"column": "pop"}},
                                  "engine_attributes": {"r": {"column": "r"}, "f": {"column": "f"},
                                                        "sp": {"columns": {"p": "p"}}}},
               "options": {"components": {"ca": {"group": "g"}, "cb": {"group": "g"}},
                           "engine_map": {"ca": {"x": "a"}, "cb": {"x": "b"}}},
               "analysis": {"decision_tests": []}}
        try:
            tr = trial_ranges(raw, REF, {"r", "f"}, {"sp": ["p"]})
            ok(tr["ca"]["r"]["lo"] == 0.2 and tr["ca"]["r"]["hi"] == 0.2 and tr["cb"]["r"]["hi"] == 0.6
               and "s3" not in tr["cb"]["r"]["settings"] and "s4" in tr["cb"]["r"]["settings"],
               f"ES1 covered ranges come from the covering effect benchmarks' arms (value sides too), not cost ones: {tr}")
            ok(tr["cb"]["sp.p"]["lo"] == 0.5 and tr["cb"]["sp.p"]["hi"] == 1.0 and "sp" not in tr["cb"],
               f"ES2 a mapping gives a range per key; a scalar setting value counts for every bound key: {tr['cb']}")
            rows, notes = outside(raw, run)
            rw = {(r["component"], r["attribute"]): r for r in rows}
            rb = rw[("cb", "r")]
            ok(rb["units_outside"] == 5 and rb["covered"] == [0.2, 0.6] and rb["range_outside"] == [0.0, 0.9]
               and abs(rb["population_share"] - 230 / 280) < 1e-9 and rb["population_column"] == "pop",
               f"ES3 units outside the trials' range, with the population share and its column: {rb}")
            ok(rw[("ca", "r")]["units_outside"] == 9, "ES4 a one-point range is widened only by its tolerance")
            ok(not any(a == "f" for _c, a in rw) and conventions(REF, {"r", "f"}, {}) == {"f"},
               f"ES5 an attribute every pack setting states with one value is a convention: {sorted(rw)}")
            md = "\n".join(sheet_lines(raw, run))
            ok(SECTION in md and "does not refuse signing" in md and md.count("no test varies it") == len(rows),
               "ES6 the sheet warns, row by row, and refuses nothing")
            raw["analysis"]["decision_tests"] = [{"id": "D3", "type": "unit_attribute",
                                                  "unit_attribute": {"column": "r", "units": "all"}},
                                                 {"id": "D4", "type": "unit_attribute",
                                                  "unit_attribute": {"column": "r", "units": ["u2"]}}]
            ok(tested_by(raw, rb) == ["D3"], "ES7 a unit_attribute test counts only when it reaches the units outside")
            from toolkit.spec import preconditions as PC
            ok("effect_by_setting" not in PC.CHECKS, "ES8 nothing is registered as a SIGN precondition")
            r2 = dict(raw, engine=REF)
            ok(outside(r2, run)[0], "ES9 a bare module:Class engine reference is compared")
            r3 = dict(raw, engine={"ref": "toolkit.engines.contract:NoSuchEngine"})
            rs3, n3 = outside(r3, run)
            ok(rs3 == [] and n3 and "nothing compared" in "\n".join(sheet_lines(r3, run)),
               "ES10 an engine without a pack is said to be not compared, not 'every unit inside'")
            ok(sheet_lines(dict(raw, options={}), run) == [], "ES11 a spec without an engine map gets no section")
            # Codex review of 1d57036 (1 P1, P2s), reproduced before the fix
            rows_n = [dict(rb, settings=[2020, "s1"])]
            try:
                md = "\n".join(render(raw, rows_n, []))
                ok("2020" in md, "ES12 (P1) a numeric setting id renders")
            except Exception as e:  # noqa: BLE001
                ok(False, f"ES12 (P1) a numeric setting id must not stop the sheet: {type(e).__name__}: {e}")
            ok(_flat("sp", {"q": 0.4}, set(), {"sp": ["p", "q"]}) == {"sp.q": 0.4, "sp.p": 0.0},
               "ES13 a key a setting's mapping omits is zero for a key the units table binds")
            ok(conventions(REF, {"r", "f"}, {}) == {"f"} and _same([1e-10, 3e-10]) is False,
               "ES14 distinct tiny setting values are not a convention")
            r4 = dict(raw, decision_units=dict(raw["decision_units"], engine_attributes={"y": {"column": "r"}}))
            md = "\n".join(sheet_lines(r4, run))
            ok("No unit is outside" not in md and "no attribute" in md, f"ES15 nothing compared is not an all-clear: {md}")
            r5 = dict(raw, decision_units=dict(raw["decision_units"], engine_attributes={
                "r": {"column": "r", "value": 0.2}}))
            ok(not [x for x in outside(r5, run)[0] if x["attribute"] == "r"], "ES16 a fixed-value binding is not a column")
            md = "\n".join(render(raw, [dict(rb, engine="e1:A"), dict(rb, engine="e2:B")], []))
            ok("e1:A" in md and "e2:B" in md, "ES17 rows of several candidate engines name their engine")
        finally:
            BC.OVERRIDES.pop(REF, None)
    print(f"{'FAIL' if fails else 'OK'}: " + (f"{len(fails)} case(s) failed" if fails else
                                               "all self-test cases passed."))
    return 1 if fails else 0


def main(argv=None) -> int:
    import argparse
    import yaml
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("run_dir", nargs="?")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)
    if a.self_test:
        return _self_test()
    if not a.run_dir:
        ap.error("run_dir is required")
    with open(os.path.join(a.run_dir, "decision_spec.yaml"), encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    rows, notes = outside(raw, a.run_dir)
    for r in rows:
        print(json.dumps({k: v for k, v in r.items() if k != "outside_units"}))
    for n in notes:
        print(n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
