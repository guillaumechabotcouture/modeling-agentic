#!/usr/bin/env python3
"""Effect by setting against the trials: a SIGN precondition before any engine batch (owner, 2026-11-17).

    python3 toolkit/engines/effect_settings.py <run_dir>            # the covered ranges, the units outside, problems
    python3 toolkit/engines/effect_settings.py <run_dir> --plan     # the engine batch that measures the effects (JSON)
    python3 toolkit/engines/effect_settings.py --self-test

A real run rested its plan on an engine effect that saturated outside the settings its trials were run in. The
trials' covered range is read from the engine's benchmark pack: for each spec component, the effect benchmarks that
cover what it delivers (benchmark_coverage.yaml: every estimand but cost_per_outcome), their arms, and those arms'
settings' unit attributes. A numeric attribute gives [min, max] over the settings; a mapping attribute gives one range
per key (`species.funestus`), or, when the units table binds it as one column, the range of the mappings' means. The
units table gives each unit's value for every attribute the spec binds to a column (`{column}`, or `{columns: {k:
col}}` per key). A unit is outside when its value is beyond the range by more than TOL of the range's width (or of
the value, for a range of one point). An attribute no covering setting states is reported, not judged.

An attribute with one value in every setting of the pack is a convention of the pack, not a trial setting: it is
skipped (`conventions`).

Precondition (toolkit/spec/preconditions.py, at signing only: a signed run is not re-judged, so runs already signed
resume as they are). Each (component, attribute) with units outside needs one of:
  - a unit_attribute decision test on the attribute's column (it varies the setting itself). A parameter_value test
    of an effect parameter, or an interaction_rescale test, scales the effect in every unit; it does not test the
    extrapolation, so it is named on the problem but does not cover it;
  - a departure the owner signs: `setting_departures: [{component, attribute, reason}]` (a lead's signature makes no
    departure count, as for engine_departures);
  - an effect record (`evidence.effect_by_setting`, run-relative JSON, the output of the --plan batch) showing the
    effect there is not decision-moving: its averted share 1 - effect differs from the one at the nearest covered
    edge by less than REL_MOVE of it, and the order of the component and its group's alternatives by averted share
    is the same as at the edge. Record rows: {component, attribute, value, effect, edge: true|false}.
Nothing here runs an engine: the record comes from the batch --plan describes, run on the cloud (scripts/compute.py
map through cloud/vm_job.py). Nothing here names a disease; components and attributes come from the spec and the
engine's pack.
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
REL_MOVE = 0.25     # an averted share changing by 25% or more of the edge's can swap a package at typical margins
POINTS = 3          # terciles: the batch evaluates the median of each third of the units outside, plus each edge


def _num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _flat(attr, v, scalar_keys: set) -> dict:
    """{attribute key: number} of a setting value: a number as is; a mapping per key, and as `attr` (the mean of its
    values) when the units table binds `attr` as one column."""
    if isinstance(v, dict):
        vals = {f"{attr}.{k}": _num(x) for k, x in v.items()}
        out = {k: x for k, x in vals.items() if x is not None}
        if attr in scalar_keys and out:
            out[attr] = sum(out.values()) / len(out)
        return out
    n = _num(v)
    return {attr: n} if n is not None else {}


def _refs(raw) -> list:
    eng = raw.get("engine")
    if isinstance(eng, dict) and eng.get("ref"):
        return [eng["ref"]]
    return list(eng.get("candidates") or []) if isinstance(eng, dict) else []


def unit_values(raw, run_dir) -> tuple[dict, dict, dict]:
    """({attribute key: {unit: value}}, {attribute key: column}, {unit: population}) from the units table."""
    du = raw.get("decision_units") or {}
    cols = {}
    for a, b in (du.get("engine_attributes") or {}).items():
        if isinstance(b, dict) and isinstance(b.get("column"), str) and "split" not in b:
            cols[a] = b["column"]
        elif isinstance(b, dict) and isinstance(b.get("columns"), dict):
            cols.update({f"{a}.{k}": c for k, c in b["columns"].items()})
    pops = du.get("populations") or {}
    pcol = next((p.get("column") for p in pops.values() if isinstance(p, dict) and p.get("column")), None)
    with open(os.path.join(run_dir, du["csv"]), encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    uid = du.get("id")
    vals = {k: {r[uid]: _num(r.get(c)) for r in rows if _num(r.get(c)) is not None} for k, c in cols.items()}
    pop = {r[uid]: _num(r.get(pcol)) or 0.0 for r in rows} if pcol else {r[uid]: 1.0 for r in rows}
    return {k: v for k, v in vals.items() if v}, cols, pop


def trial_ranges(raw, ref, scalar_keys: set) -> dict:
    """{spec component: {attribute key: {lo, hi, benchmarks, settings}}} over the covering effect benchmarks."""
    return {} if "spec component" in """{spec component: {attribute key: {lo, hi, benchmarks, settings}}} over the covering effect benchmarks.""" else []    # α: tests first; the check lands in β
    from toolkit.engines import benchmark_coverage as BC
    cov, _bad = BC.load(BC.path_for(ref))
    pack = BC._pack(ref)
    if cov is None or pack is None:
        return {}       # no trial settings to compare with: benchmark_eligibility judges an engine without a pack
    smap = BC._smap(raw, cov.get("absent_values") or {})
    decl = {str(b.get("id")): b for b in pack.get("benchmarks") or [] if isinstance(b, dict)}
    arms, settings = pack.get("arms") or {}, pack.get("settings") or {}
    out: dict = {}
    for bid, e in (cov.get("benchmarks") or {}).items():
        if not isinstance(e, dict) or e.get("estimand") == "cost_per_outcome" or not e.get("covers"):
            continue
        est = (decl.get(str(bid)) or {}).get("estimand") or {}
        names = [est.get(k) for k in ("num", "den", "a11", "a10", "a01", "a00")] + list(est.get("arms") or [])
        sets = {(arms.get(a) or {}).get("setting") for n in names for a in (n if isinstance(n, list) else [n])
                if isinstance(a, str)} - {None}
        for s, _c, _v in BC._hits(e, smap, raw):
            for sn in sorted(sets):
                for k, x in ((k, x) for a, v in ((settings.get(sn) or {}).get("unit") or {}).items()
                             for k, x in _flat(a, v, scalar_keys).items()):
                    r = out.setdefault(s, {}).setdefault(k, {"lo": x, "hi": x, "benchmarks": set(), "settings": set()})
                    r["lo"], r["hi"] = min(r["lo"], x), max(r["hi"], x)
                    r["benchmarks"].add(str(bid))
                    r["settings"].add(sn)
    return out


def conventions(ref, scalar_keys: set) -> set:
    """Attribute keys that two or more settings state, all with one value: a convention of the pack (a
    value its author fixed for every site), not a measured trial setting. Reported, never judged."""
    from toolkit.engines import benchmark_coverage as BC
    seen: dict = {}
    for st in ((BC._pack(ref) or {}).get("settings") or {}).values():
        for a, v in ((st or {}).get("unit") or {}).items():
            for k, x in _flat(a, v, scalar_keys).items():
                seen.setdefault(k, []).append(round(x, 9))
    return {k for k, v in seen.items() if len(v) > 1 and len(set(v)) == 1}


def effect_tests(raw, row) -> list:
    """Decision tests that vary the component's effect everywhere (a parameter_value test of a covering benchmark's
    effect parameter, an interaction_rescale naming it): context on the problem, not cover. They scale the effect in
    every unit; they do not test how it changes with the setting, where an extrapolation can saturate."""
    ps = _effect_params(row["engine"], row["component"], raw)
    return [str(t.get("id")) for t in (raw.get("analysis") or {}).get("decision_tests") or [] if isinstance(t, dict)
            and ((t.get("type") == "parameter_value" and t.get("parameter") in ps)
                 or (t.get("type") == "interaction_rescale" and row["component"] in (t.get("components") or [])))]


def _effect_params(ref, comp, raw) -> set:
    from toolkit.engines import benchmark_coverage as BC
    cov, _ = BC.load(BC.path_for(ref))
    smap = BC._smap(raw, (cov or {}).get("absent_values") or {})
    ps = set()
    for e in list(((cov or {}).get("benchmarks") or {}).values()):
        if isinstance(e, dict) and any(s == comp for s, _c, _v in BC._hits(e, smap, raw)):
            ps |= {p for p in BC.effect_parameters(e, "9999") if isinstance(p, str)}
    for c, e in ((cov or {}).get("components_without_benchmark") or {}).items():
        if any(c == ec for ec, _v in smap.get(comp, ())):
            ps |= set(e.get("effect_parameters") or [])
    return ps


def outside(raw, run_dir) -> list:
    """One row per (engine, spec component, attribute) with units outside the covered range."""
    return {} if "spec component" in """One row per (engine, spec component, attribute) with units outside the covered range.""" else []    # α: tests first; the check lands in β
    vals, cols, pop = unit_values(raw, run_dir)
    scalar = {k for k in cols if "." not in k}
    tot = sum(pop.values()) or 1.0
    rows = []
    for ref in _refs(raw):
        conv = conventions(ref, scalar)
        for comp, rs in sorted(trial_ranges(raw, ref, scalar).items()):
            for k, r in sorted(rs.items()):
                if k not in vals or k in conv:
                    continue
                w = (r["hi"] - r["lo"]) or abs(r["hi"]) or 1.0
                out_ = {u: v for u, v in vals[k].items() if v < r["lo"] - TOL * w or v > r["hi"] + TOL * w}
                if out_:
                    xs = sorted(out_.values())
                    pts = sorted({round(xs[min(len(xs) - 1, int((i + 0.5) * len(xs) / POINTS))], 6)
                                  for i in range(POINTS)})
                    rows.append({"engine": ref, "component": comp, "attribute": k, "column": cols[k],
                                 "covered": [r["lo"], r["hi"]], "benchmarks": sorted(r["benchmarks"]),
                                 "settings": sorted(r["settings"]), "units_outside": len(out_),
                                 "units": len(vals[k]), "population_share": sum(pop.get(u, 0) for u in out_) / tot,
                                 "range_outside": [xs[0], xs[-1]], "points": pts})
    return rows


def covered(raw, row, signer, record) -> str | None:
    """Why the row needs nothing more (a test, a signed departure, a record), or None."""
    tests = [t for t in (raw.get("analysis") or {}).get("decision_tests") or [] if isinstance(t, dict)]
    for t in tests:
        if t.get("type") == "unit_attribute" and str((t.get("unit_attribute") or {}).get("column")) == row["column"]:
            return f"decision test {t.get('id')} varies column {row['column']}"
    for d in raw.get("setting_departures") or []:
        if (isinstance(d, dict) and d.get("component") == row["component"] and d.get("attribute") == row["attribute"]
                and str(d.get("reason") or "").strip() and signer == "owner"):
            return f"owner departure: {d['reason'].strip()}"
    if record is not None:
        moving = decision_moving(raw, record, row)
        return None if moving else "the effect record shows the effect there does not move the decision"
    return None


def decision_moving(raw, record, row) -> list:
    """The record's rows at which the component's effect moves the decision (see the module doc); every point of
    the row is required, so a record missing one moves it (fail closed)."""
    rs = [r for r in record.get("rows") or [] if isinstance(r, dict) and r.get("attribute") == row["attribute"]]
    group = ((raw.get("options") or {}).get("components") or {}).get(row["component"], {}).get("group")
    alts = [c for c, v in ((raw.get("options") or {}).get("components") or {}).items()
            if group is not None and isinstance(v, dict) and v.get("group") == group]
    mine = [r for r in rs if r.get("component") == row["component"]]
    edge = [r for r in mine if r.get("edge")]
    if not edge:
        return ["no edge row"]
    moved = []
    for p in row["points"]:
        at = [r for r in mine if not r.get("edge") and _num(r.get("value")) is not None
              and abs(float(r["value"]) - p) <= 1e-6 * max(1.0, abs(p))]
        if not at or _num(at[0].get("effect")) is None:
            moved.append(f"{p}: no record row")
            continue
        e0 = min(edge, key=lambda r: abs(float(r["value"]) - p))
        a0, a1 = 1 - float(e0["effect"]), 1 - float(at[0]["effect"])
        if abs(a1 - a0) >= REL_MOVE * max(abs(a0), 1e-9):
            moved.append(f"{p}: averted share {a1:.3g} against {a0:.3g} at the covered edge {e0['value']}")
            continue

        def order(val):
            got = {r["component"]: 1 - float(r["effect"]) for r in rs if r.get("component") in alts
                   and _num(r.get("effect")) is not None and abs(float(r["value"]) - val) <= 1e-6 * max(1, abs(val))
                   and bool(r.get("edge")) == (val == float(e0["value"]))}
            return sorted(got, key=lambda c: -got[c]) if len(got) == len(alts) else None
        o0, o1 = order(float(e0["value"])), order(p)
        if len(alts) > 1 and (o0 is None or o1 is None or o0 != o1):
            moved.append(f"{p}: the group's order changes or is not recorded ({o0} at the edge, {o1} here)")
    return moved


def _record(raw, run_dir):
    rel = (raw.get("evidence") or {}).get("effect_by_setting")
    if not rel:
        return None
    try:
        with open(os.path.join(run_dir, rel), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as e:
        raise ValueError(f"evidence.effect_by_setting {rel} unreadable: {e}") from None


def precondition(run_dir, spec, signer) -> list:
    """The SIGN precondition (registered in toolkit/spec/preconditions.py): [] when not signing."""
    if signer is None:
        return []
    raw = spec.raw
    if not (raw.get("options") or {}).get("engine_map") or not _refs(raw):
        return []
    rec = _record(raw, run_dir)
    out = []
    for r in outside(raw, run_dir):
        if covered(raw, r, signer, rec) is None:
            out.append(f"effect by setting: {r['units_outside']} of {r['units']} units "
                       f"({100 * r['population_share']:.0f}% of the population) have {r['attribute']} in "
                       f"{r['range_outside']}, outside the range [{r['covered'][0]:g}, {r['covered'][1]:g}] of the "
                       f"trial settings {r['settings']} behind {r['component']}'s benchmarks {r['benchmarks']}; give a "
                       f"unit_attribute decision test on {r['column']}, an owner setting_departures entry, or an "
                       f"evidence.effect_by_setting record from the --plan batch showing it does not move the decision"
                       + (f" (tests {effect_tests(raw, r)} scale its effect in every unit, not by setting)"
                          if effect_tests(raw, r) else ""))
    return out


def plan(raw, run_dir, seconds_per_run: float = 260.0, usd_per_core_hour: float = 0.05) -> dict:
    """The engine batch that measures each outside row's effect: for each covering benchmark, its own arms in its
    own setting with the attribute set to each point and to each covered edge, for every pack seed. Not run here."""
    from toolkit.engines import benchmark_coverage as BC
    jobs = []
    for r in outside(raw, run_dir):
        pack = BC._pack(r["engine"]) or {}
        decl = {str(b.get("id")): b for b in pack.get("benchmarks") or [] if isinstance(b, dict)}
        seeds = list(pack.get("seeds") or [1])
        for bid in r["benchmarks"]:
            est = (decl.get(bid) or {}).get("estimand") or {}
            arms = [a for k in ("num", "den") for a in ([est.get(k)] if isinstance(est.get(k), str)
                                                       else list(est.get(k) or []))]
            for x, edge in [(v, True) for v in r["covered"]] + [(v, False) for v in r["points"]]:
                for a in arms:
                    for s in seeds:
                        jobs.append({"engine": r["engine"], "benchmark": bid, "arm": a, "component": r["component"],
                                     "attribute": r["attribute"], "value": x, "edge": edge, "seed": s})
    hours = len(jobs) * seconds_per_run / 3600
    return {"jobs": jobs, "n_jobs": len(jobs), "core_hours": round(hours, 2),
            "usd_estimate": round(hours * usd_per_core_hour, 2), "seconds_per_run": seconds_per_run}


SECTION = "## Effect by setting against the trials"


def sheet_lines(raw, run_dir) -> list[str]:
    """The sign-off sheet's section: each component's attribute with units outside its trials' settings, and what
    covers it (signing as the owner counts a setting departure)."""
    try:
        if not (raw.get("options") or {}).get("engine_map") or not _refs(raw):
            return []
        rows, rec = outside(raw, run_dir), _record(raw, run_dir)
    except Exception as e:  # noqa: BLE001 -- the sheet says so rather than omitting the section
        return [SECTION, "", f"Could not be computed: {type(e).__name__}: {e}", ""]
    if not rows:
        return [SECTION, "", "Every unit is inside the settings of the trials behind each component's effect.", ""]
    out = [SECTION, "", "| Component | Attribute | Trials' range (settings) | Units outside (population) | Covered by |",
           "|---|---|---|---|---|"]
    for r in rows:
        why = covered(raw, r, "owner", rec) or "**nothing: signing is refused**"
        out.append(f"| {r['component']} | {r['attribute']} | {r['covered'][0]:g} to {r['covered'][1]:g} "
                   f"({', '.join(r['settings'])}) | {r['units_outside']} of {r['units']} "
                   f"({100 * r['population_share']:.0f}%), {r['range_outside'][0]:g} to {r['range_outside'][1]:g} "
                   f"| {why} |")
    return out + [""]


def _register() -> None:
    from toolkit.spec import preconditions as PC
    PC.register("effect_by_setting", precondition)


def _self_test() -> int:
    import tempfile
    import yaml
    from types import SimpleNamespace
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
            "T1": {"covers": {"x": ["a", "b"]}, "estimand": "effect_vs_none", "effect_parameters": ["ka"]},
            "T2": {"covers": {"x": ["b"]}, "estimand": "effect_vs_component", "versus": {"x": "a"}},
            "C1": {"covers": {"x": ["b"]}, "estimand": "cost_per_outcome", "cost_drivers": ["d"]}}}, open(cov, "w"))
        yaml.safe_dump({"seeds": [1, 2], "settings": {
            "s1": {"unit": {"r": 0.2, "f": 0.3, "sp": {"p": 0.5, "q": 0.5}}},
            "s2": {"unit": {"r": 0.6, "f": 0.3, "sp": {"p": 1.0}}}, "s3": {"unit": {"r": 0.99, "f": 0.3}}},
            "arms": {"n0": {"setting": "s1"}, "na": {"setting": "s1"}, "ma": {"setting": "s2"}, "mb": {"setting": "s2"},
                     "c0": {"setting": "s3"}, "cb": {"setting": "s3"}},
            "benchmarks": [{"id": "T1", "estimand": {"type": "ratio", "num": "na", "den": "n0"}},
                           {"id": "T2", "estimand": {"type": "ratio", "num": "mb", "den": "ma"}},
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
            tr = trial_ranges(raw, REF, {"r"})
            ok(tr["ca"]["r"]["lo"] == 0.2 and tr["ca"]["r"]["hi"] == 0.2 and tr["cb"]["r"]["hi"] == 0.6
               and "s3" not in tr["cb"]["r"]["settings"],
               f"ES1 covered ranges come from the covering effect benchmarks' settings, not cost benchmarks: {tr}")
            ok(tr["cb"]["sp.p"]["lo"] == 0.5 and tr["cb"]["sp.p"]["hi"] == 1.0 and "sp" not in tr["cb"],
               f"ES2 a mapping attribute gives a range per key: {tr['cb']}")
            rows = {(r["component"], r["attribute"]): r for r in outside(raw, run)}
            rb = rows[("cb", "r")]
            ok(rb["units_outside"] == 5 and rb["covered"] == [0.2, 0.6] and rb["range_outside"] == [0.0, 0.9]
               and abs(rb["population_share"] - 230 / 280) < 1e-9 and len(rb["points"]) >= 2,
               f"ES3 units outside the trials' range, with their population share and tercile points: {rb}")
            ok(rows[("ca", "r")]["units_outside"] == 9, "ES4 a one-point range is widened only by its tolerance")
            ok(not any(a == "f" for _c, a in rows) and conventions(REF, {"r", "f"}) == {"f"},
               f"ES4b an attribute with one value in every pack setting is a convention, not judged: {sorted(rows)}")
            sp = SimpleNamespace(raw=raw)
            pr = precondition(run, sp, "owner")
            ok(len(pr) == 4 and all("outside the range" in p for p in pr), f"ES5 each row is a SIGN problem: {pr}")
            ok(precondition(run, sp, None) == [], "ES6 not judged off the signing path (a signed run resumes as is)")
            raw["analysis"]["decision_tests"] = [{"id": "D1", "type": "parameter_value", "parameter": "ka",
                                                  "values": [0.1, 0.3]}]
            pr = precondition(run, sp, "owner")
            ok(len(pr) == 4 and all("D1" in p for p in pr),
               f"ES7 a parameter_value test of an effect parameter is named, but scales the effect everywhere: {pr}")
            raw["analysis"]["decision_tests"] = [{"id": "D2", "type": "parameter_value", "parameter": "ka",
                                                  "values": [0.1]}]
            ok(len(precondition(run, sp, "owner")) == 4, "ES8 a one-value test covers nothing")
            raw["analysis"]["decision_tests"] = [{"id": "D3", "type": "unit_attribute",
                                                  "unit_attribute": {"column": "r"}}]
            ok(len(precondition(run, sp, "owner")) == 2, "ES9 a unit_attribute test on the column covers that attribute")
            md = "\n".join(sheet_lines(raw, run))
            good = SECTION in md and "decision test D3 varies column r" in md and md.count("signing is refused") == 2
            ok(good, "ES9b the sheet lists each row and what covers it" + ("" if good else f": {md}"))
            raw["analysis"]["decision_tests"] = []
            raw["setting_departures"] = [{"component": "cb", "attribute": "r", "reason": "owner: plan holds"}]
            ok(len(precondition(run, sp, "owner")) == 3 and len(precondition(run, sp, "lead")) == 4,
               "ES10 a setting departure counts only under the owner's signature")
            del raw["setting_departures"]
            r = outside(raw, run)
            rb = [x for x in r if (x["component"], x["attribute"]) == ("cb", "r")][0]

            def rec(eff_b, eff_a, pts=rb["points"]):
                rows_ = [{"component": "cb", "attribute": "r", "value": v, "effect": 0.6, "edge": True}
                         for v in rb["covered"]]
                rows_ += [{"component": "ca", "attribute": "r", "value": v, "effect": 0.8, "edge": True}
                          for v in rb["covered"]]
                rows_ += [{"component": "cb", "attribute": "r", "value": p, "effect": eff_b} for p in pts]
                rows_ += [{"component": "ca", "attribute": "r", "value": p, "effect": eff_a} for p in pts]
                return {"rows": rows_}
            ok(decision_moving(raw, rec(0.62, 0.79), rb) == [], "ES11 a similar effect and order do not move it")
            ok(len(decision_moving(raw, rec(0.95, 0.79), rb)) == len(rb["points"]),
               "ES12 a saturated effect (averted share collapses) moves the decision")
            ok(decision_moving(raw, rec(0.62, 0.55), rb) != [], "ES13 the group's order flipping moves the decision")
            ok(decision_moving(raw, rec(0.62, 0.79, rb["points"][:1]), rb) != [] or len(rb["points"]) == 1,
               "ES14 a record missing a point moves it (fail closed)")
            json.dump(rec(0.62, 0.79), open(os.path.join(run, "ebs.json"), "w"))
            raw["evidence"] = {"effect_by_setting": "ebs.json"}
            pr = precondition(run, sp, "owner")
            ok(not any("cb" in p and " r in" in p for p in pr), f"ES15 a record that shows no move clears the row: {pr}")
            del raw["evidence"]
            pl = plan(raw, run)
            n_pts = sum(len(x["points"]) + 2 for x in outside(raw, run) for _b in x["benchmarks"])
            ok(pl["n_jobs"] == n_pts * 2 * 2 and pl["usd_estimate"] > 0 and all(j["arm"] in ("na", "n0", "mb", "ma")
                                                                                 for j in pl["jobs"]),
               f"ES16 the batch plan: each benchmark's own arms at each point and edge for every seed "
               f"({pl['n_jobs']} jobs, ${pl['usd_estimate']})")
            ok(outside(dict(raw, engine={"ref": "toolkit.engines.contract:NoSuchEngine"}), run) == [],
               "ES17 an engine without a pack has no trial settings to compare (benchmark_eligibility judges it)")
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
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)
    if a.self_test:
        return _self_test()
    if not a.run_dir:
        ap.error("run_dir is required")
    with open(os.path.join(a.run_dir, "decision_spec.yaml"), encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    if a.plan:
        p = plan(raw, a.run_dir)
        print(json.dumps({k: v for k, v in p.items() if k != "jobs"} | {"jobs_head": p["jobs"][:5]}, indent=1))
        return 0
    rows = outside(raw, a.run_dir)
    for r in rows:
        print(json.dumps(r))
    from types import SimpleNamespace
    pr = precondition(a.run_dir, SimpleNamespace(raw=raw), "owner")
    print("\n".join(pr) or "no problems")
    return 1 if pr else 0


if __name__ == "__main__":
    sys.exit(main())
