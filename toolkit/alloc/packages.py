#!/usr/bin/env python3
"""Toolkit, "budget allocation across units" template: packages and feasibility (T1).

    python3 toolkit/alloc/packages.py <spec.yaml> [--units <csv>] [--json]
    python3 toolkit/alloc/packages.py --self-test

E7 attempt 7 built the package list and the feasibility mask by hand
(`lga_outcomes.packages`, `optimize.feasible_mask`): eligibility was one
hard-coded boolean, other funders' footprints and already-delivered units were
patched in per round, and caps lived as ad-hoc solver arguments. Here they are
declared in the question spec, validated fail-closed, and turned into one
`feasible[unit][package]` table with the reason for every infeasible pair.

Spec (beside the `units`, `components` and `packages` sections of
toolkit/alloc/costs.py; packages are read with costs.packages()):

    eligibility:                 # per component: where it may be delivered
      c2: {column: elig_flag}                    # a boolean column (true/false, 1/0, yes/no)
      c3: {column: burden, min: 0.3}             # a threshold (min and/or max, inclusive)
      c4: {column: zone, in: [north, east]}      # a set of values
      c5: {population: eligible}                 # units.populations.eligible > 0
    # A component with `components.<c>.eligible` and no rule here gets the
    # population rule for that eligible population (> 0), so a targeted
    # component is never feasible where nobody can receive it.
    locks:                       # units whose package is fixed (e.g. already delivered)
      u7: {package: c1, reason: delivered in the previous cycle}
    footprints:                  # components another funder already delivers in a unit
      c1: {units: [u3, u9], funder: other}       # or any eligibility-style rule
    caps:                        # coupling constraints, handed to toolkit/alloc/optimize.py
      c2: {max_units: 12}                        # at most 12 units receive c2
      c1: {max_quantity: 250000, quantity: total}  # sum of a population (or column) where c1 is delivered

A package is infeasible in a unit when it contains a component that is not
eligible there, or one another funder already delivers there (not chargeable
or allocatable to this budget), or when the unit is locked to another package.

Validation (fail closed, before computing):
- rules on unknown components, unknown columns or populations, or values that
  do not parse (a boolean column with "maybe", a threshold on a non-number);
- a lock on an unknown unit, to an unknown package, or to a package that is
  itself infeasible in that unit (ineligible or another funder's footprint);
- a cap on an unknown component, or with neither max_units nor max_quantity,
  or that the locked units alone already exceed;
- a unit left with no feasible package.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import costs  # noqa: E402  (toolkit/alloc/costs.py)

TRUE = {"true", "1", "yes", "y", "t"}
FALSE = {"false", "0", "no", "n", "f", ""}
RULE_KEYS = {"column", "min", "max", "in", "population", "units", "funder", "reason"}


def _float(v):
    if isinstance(v, bool):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _bool(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)) and v in (0, 1):
        return bool(v)
    s = str(v).strip().lower()
    return True if s in TRUE else False if s in FALSE else None


def _rule_errors(where: str, rule, spec: dict, fields: set | None, ids: set) -> list[str]:
    if not isinstance(rule, dict):
        return [f"{where}: a rule must be a mapping (column, population or units)"]
    errs = [f"{where}: unknown key {k}" for k in sorted(set(rule) - RULE_KEYS)]
    kinds = [k for k in ("column", "population", "units") if k in rule]
    if len(kinds) != 1:
        return errs + [f"{where}: give exactly one of column, population or units"]
    if "column" in rule:
        if fields is not None and rule["column"] not in fields:
            errs.append(f"{where}: column {rule['column']} is not in the units table")
        for k in ("min", "max"):
            if k in rule and _float(rule[k]) is None:
                errs.append(f"{where}: {k} must be a number")
        if "in" in rule and not (isinstance(rule["in"], list) and rule["in"]):
            errs.append(f"{where}: in must be a non-empty list of values")
        if "in" in rule and ("min" in rule or "max" in rule):
            errs.append(f"{where}: use either in or min/max, not both")
    elif "population" in rule:
        pops = (spec.get("units") or {}).get("populations") or {}
        if rule["population"] not in pops:
            errs.append(f"{where}: population {rule['population']} is not in units.populations")
        elif fields is not None and pops[rule["population"]] not in fields:
            errs.append(f"{where}: population column {pops[rule['population']]} is not in the units table")
    else:
        if not isinstance(rule["units"], list):
            errs.append(f"{where}: units must be a list of unit ids")
        else:
            errs += [f"{where}: unknown unit {u}" for u in rule["units"] if str(u) not in ids]
    return errs


def _apply(rule: dict, unit: dict, spec: dict, uid: str):
    """(holds, error) for one unit; error is set when the unit's value cannot be read."""
    if "units" in rule:
        return uid in {str(u) for u in rule["units"]}, None
    if "population" in rule:
        col = spec["units"]["populations"][rule["population"]]
        x = _float(unit.get(col))
        return (None, f"unit {uid}: {col}={unit.get(col)!r} is not a number") if x is None else (x > 0, None)
    col, v = rule["column"], unit.get(rule["column"])
    if "in" in rule:
        return str(v).strip() in {str(a).strip() for a in rule["in"]}, None
    if "min" in rule or "max" in rule:
        x = _float(v)
        if x is None:
            return None, f"unit {uid}: {col}={v!r} is not a number"
        return ((("min" not in rule) or x >= float(rule["min"]))
                and (("max" not in rule) or x <= float(rule["max"]))), None
    b = _bool(v)
    return (None, f"unit {uid}: {col}={v!r} is not a boolean") if b is None else (b, None)


def _describe(rule: dict) -> str:
    if "units" in rule:
        return "listed units"
    if "population" in rule:
        return f"{rule['population']} population > 0"
    if "in" in rule:
        return f"{rule['column']} in {rule['in']}"
    parts = ([f"{rule['column']} >= {rule['min']}"] if "min" in rule else []) + \
            ([f"{rule['column']} <= {rule['max']}"] if "max" in rule else [])
    return " and ".join(parts) or f"{rule['column']} is true"


def eligibility_rules(spec: dict) -> dict[str, dict]:
    """Component -> its eligibility rule, including the implicit rule of a targeted component."""
    rules = {str(k): v for k, v in (spec.get("eligibility") or {}).items()}
    for c, d in (spec.get("components") or {}).items():
        if (d or {}).get("eligible") and c not in rules:
            rules[c] = {"population": d["eligible"], "reason": f"implicit: components.{c}.eligible"}
    return rules


def _quantity(cap: dict, unit: dict, spec: dict):
    pops = (spec.get("units") or {}).get("populations") or {}
    q = cap.get("quantity")
    return _float(unit.get(pops.get(q, q)))


def validate(spec: dict, units: list[dict]) -> list[str]:
    comps = spec.get("components")
    if not isinstance(comps, dict) or not comps:
        return ["components: a mapping of every intervention component is required"]
    uidk = (spec.get("units") or {}).get("id")
    if not uidk:
        return ["units.id: the unit id column is required"]
    if not units:
        return ["units: the units table is empty"]
    fields = set(units[0])
    if uidk not in fields:
        return [f"units.id: column {uidk} is not in the units table"]
    ids = [str(u[uidk]) for u in units]
    errs = [f"units: duplicate unit id {i}" for i in sorted({i for i in ids if ids.count(i) > 1})]
    ids_set = set(ids)
    pk = costs.packages(spec)
    if not pk:
        errs.append("packages: at least one package is required")
    for name, cs in pk.items():
        if cs - set(comps):
            errs.append(f"packages.{name}: unknown components {', '.join(sorted(cs - set(comps)))}")
    for sec in ("eligibility", "footprints", "locks", "caps"):
        if spec.get(sec) is not None and not isinstance(spec[sec], dict):
            errs.append(f"{sec}: must be a mapping")
    if errs:
        return errs
    for sec, rules in (("eligibility", eligibility_rules(spec)), ("footprints", spec.get("footprints") or {})):
        for c, r in rules.items():
            if c not in comps:
                errs.append(f"{sec}.{c}: unknown component")
                continue
            e = _rule_errors(f"{sec}.{c}", r, spec, fields, ids_set)
            errs += e
            if not e:
                errs += sorted({f"{sec}.{c}: {m}" for u, i in zip(units, ids) for m in [_apply(r, u, spec, i)[1]] if m})
    for c, cap in (spec.get("caps") or {}).items():
        if c not in comps:
            errs.append(f"caps.{c}: unknown component")
            continue
        if not isinstance(cap, dict) or not ({"max_units", "max_quantity"} & set(cap)):
            errs.append(f"caps.{c}: give max_units and/or max_quantity")
            continue
        for k in ("max_units", "max_quantity"):
            if k in cap and not ((_float(cap[k]) is not None) and _float(cap[k]) >= 0):   # YAML reads 1e6 as a string
                errs.append(f"caps.{c}.{k}: must be a number >= 0")
        if "max_quantity" in cap:
            q = cap.get("quantity")
            pops = (spec.get("units") or {}).get("populations") or {}
            if not q or (pops.get(q, q) not in fields):
                errs.append(f"caps.{c}: max_quantity needs `quantity`, a population name or a column of the units table")
            else:
                errs += sorted({f"caps.{c}: unit {i}: quantity is not a non-negative number"
                                for u, i in zip(units, ids)
                                if (_quantity(cap, u, spec) is None or _quantity(cap, u, spec) < 0)})
        for k in set(cap) - {"max_units", "max_quantity", "quantity", "reason"}:
            errs.append(f"caps.{c}: unknown key {k}")
    for u, lk in (spec.get("locks") or {}).items():
        lk = lk if isinstance(lk, dict) else {"package": lk}
        if str(u) not in ids_set:
            errs.append(f"locks.{u}: unknown unit")
        if str(lk.get("package")) not in pk:
            errs.append(f"locks.{u}: unknown package {lk.get('package')}")
    return errs


def feasibility(spec: dict, units: list[dict]) -> dict:
    """{"feasible": {unit: {package: bool}}, "reasons": {unit: {package: [why]}}, "locks": {unit: package},
        "footprints": {unit: [component]}, "caps": [{"name", "component", "max", "weights": {unit: {package: w}}}]}.

    Raises ValueError on an invalid spec, a lock that is itself infeasible, a cap the locks already exceed,
    or a unit with no feasible package."""
    errs = validate(spec, units)
    if errs:
        raise ValueError("invalid packages spec: " + "; ".join(errs))
    uidk = spec["units"]["id"]
    pk = costs.packages(spec)
    elig = eligibility_rules(spec)
    fps = spec.get("footprints") or {}
    locks = {str(u): str((v if isinstance(v, dict) else {"package": v})["package"])
             for u, v in (spec.get("locks") or {}).items()}
    feas, reasons, foot = {}, {}, {}
    errs = []
    for u in units:
        i = str(u[uidk])
        bad_c = {}
        for c, r in elig.items():
            if not _apply(r, u, spec, i)[0]:
                bad_c[c] = f"{c} not eligible ({_describe(r)})"
        for c, r in fps.items():
            if _apply(r, u, spec, i)[0]:
                foot.setdefault(i, []).append(c)
                who = r.get("funder") or "another funder"
                bad_c[c] = (bad_c[c] + "; " if c in bad_c else "") + f"{c} already delivered by {who}"
        feas[i], reasons[i] = {}, {}
        for p, cs in pk.items():
            why = [bad_c[c] for c in sorted(cs) if c in bad_c]
            if i in locks and why and p == locks[i]:
                errs.append(f"locks.{i}: locked to {p}, which is itself infeasible there: {'; '.join(why)}")
            if i in locks and p != locks[i]:
                why.append(f"unit locked to {locks[i]}")
            feas[i][p] = not why
            if why:
                reasons[i][p] = why
        if not any(feas[i].values()) and not any(e.startswith(f"locks.{i}:") for e in errs):
            errs.append(f"unit {i}: no feasible package")
    caps = []
    for c, cap in (spec.get("caps") or {}).items():
        for kind in ("max_units", "max_quantity"):
            if kind not in cap:
                continue
            w = {str(u[uidk]): {p: ((1.0 if kind == "max_units" else _quantity(cap, u, spec)) if c in cs else 0.0)
                                for p, cs in pk.items()} for u in units}
            used = sum(w[i][p] for i, p in locks.items())
            if used > float(cap[kind]) + 1e-9:
                errs.append(f"caps.{c}.{kind}: the locked units alone use {used:g}, above the cap {cap[kind]}")
            caps.append({"name": f"{c}.{kind}", "component": c, "max": float(cap[kind]), "weights": w})
    if errs:
        raise ValueError("infeasible packages spec: " + "; ".join(errs))
    return {"feasible": feas, "reasons": reasons, "locks": locks, "footprints": foot, "caps": caps}


# --------------------------------------------------------------------------- self-test

def _planted() -> tuple[dict, list[dict]]:
    units = [{"id": f"u{i}", "pop": 1000 + 10 * i, "elig": 100 * (i % 2), "flag": "yes" if i % 3 else "no",
              "score": 0.1 * i, "zone": "abcd"[i % 4]} for i in range(12)]
    spec = {"units": {"id": "id", "populations": {"total": "pop", "eligible": "elig"}},
            "components": {"c1": {}, "c2": {"eligible": "eligible"}},
            "packages": {"generate": True}}
    return spec, units


def _self_test() -> int:
    fails = []

    def ok(c, m):
        if not c:
            fails.append(m)

    def errs_of(spec, units):
        try:
            feasibility(spec, units)
            return ""
        except ValueError as e:
            return str(e)

    spec, units = _planted()
    f = feasibility(spec, units)
    ok(set(f["feasible"]["u0"]) == {"none", "c1", "c2", "c1+c2"}, f"TC1 packages from costs.packages(): {f['feasible']['u0']}")
    ok(f["feasible"]["u1"]["c2"] and not f["feasible"]["u0"]["c2"] and not f["feasible"]["u0"]["c1+c2"]
       and f["feasible"]["u0"]["c1"],
       "TC2 a targeted component is infeasible where its eligible population is 0 (implicit rule)")
    ok("c2 not eligible (eligible population > 0)" in f["reasons"]["u0"]["c2"], f"TC3 reason given: {f['reasons']['u0']}")
    ok(all(f["reasons"][u][p] for u in f["feasible"] for p, v in f["feasible"][u].items() if not v),
       "TC4 every infeasible pair has a reason")
    s = {**spec, "eligibility": {"c2": {"column": "flag"}}}
    f = feasibility(s, units)
    ok(f["feasible"]["u1"]["c2"] and not f["feasible"]["u0"]["c2"] and not f["feasible"]["u3"]["c2"],
       "TC5 a boolean column rule (an explicit rule replaces the implicit one)")
    s = {**spec, "eligibility": {"c1": {"column": "score", "min": 0.5, "max": 0.9}}}
    f = feasibility(s, units)
    ok([u for u in f["feasible"] if f["feasible"][u]["c1"]] == ["u5", "u6", "u7", "u8", "u9"],
       f"TC6 a threshold rule, inclusive: {[u for u in f['feasible'] if f['feasible'][u]['c1']]}")
    s = {**spec, "eligibility": {"c1": {"column": "zone", "in": ["a"]}}}
    ok(sum(feasibility(s, units)["feasible"][u]["c1"] for u in f["feasible"]) == 3, "TC7 a set-membership rule")
    # fail-closed validation
    ok("column nope is not in the units table" in errs_of({**spec, "eligibility": {"c2": {"column": "nope"}}}, units),
       "TC8 an unknown column fails")
    ok("is not a boolean" in errs_of({**spec, "eligibility": {"c1": {"column": "zone"}}}, units),
       "TC9 a boolean rule on a non-boolean column fails")
    ok("is not a number" in errs_of({**spec, "eligibility": {"c1": {"column": "zone", "min": 1}}}, units),
       "TC10 a threshold on a non-numeric column fails")
    ok("eligibility.c9: unknown component" in errs_of({**spec, "eligibility": {"c9": {"column": "flag"}}}, units),
       "TC11 a rule on an unknown component fails")
    # locks
    s = {**spec, "locks": {"u3": {"package": "c1+c2", "reason": "delivered"}}}
    f = feasibility(s, units)
    ok(f["locks"] == {"u3": "c1+c2"} and [p for p, v in f["feasible"]["u3"].items() if v] == ["c1+c2"]
       and "unit locked to c1+c2" in f["reasons"]["u3"]["none"], "TC12 a locked unit has only its package")
    ok("unknown package zz" in errs_of({**spec, "locks": {"u3": "zz"}}, units), "TC13 a lock to an unknown package fails")
    ok("locks.u99: unknown unit" in errs_of({**spec, "locks": {"u99": "c1"}}, units), "TC14 a lock on an unknown unit fails")
    ok("itself infeasible" in errs_of({**spec, "locks": {"u0": "c2"}}, units),
       "TC15 a lock to a package the unit is not eligible for fails")
    # footprints
    s = {**spec, "footprints": {"c1": {"units": ["u1", "u2"], "funder": "funder B"}}}
    f = feasibility(s, units)
    ok(not f["feasible"]["u1"]["c1"] and not f["feasible"]["u1"]["c1+c2"] and f["feasible"]["u1"]["c2"]
       and f["footprints"] == {"u1": ["c1"], "u2": ["c1"]}
       and "c1 already delivered by funder B" in f["reasons"]["u1"]["c1"], "TC16 another funder's footprint")
    ok("itself infeasible" in errs_of({**s, "locks": {"u2": "c1"}}, units), "TC17 a lock onto another funder's footprint fails")
    ok("unknown unit u77" in errs_of({**spec, "footprints": {"c1": {"units": ["u77"]}}}, units),
       "TC18 a footprint on an unknown unit fails")
    # caps
    s = {**spec, "caps": {"c2": {"max_units": 2}, "c1": {"max_quantity": 5000, "quantity": "total"}}}
    f = feasibility(s, units)
    cu = {c["name"]: c for c in f["caps"]}
    ok(cu["c2.max_units"]["weights"]["u1"] == {"none": 0.0, "c1": 0.0, "c2": 1.0, "c1+c2": 1.0}
       and cu["c1.max_quantity"]["weights"]["u1"]["c1"] == 1010.0 and cu["c1.max_quantity"]["max"] == 5000.0,
       f"TC19 cap weights: {cu}")
    ok("locked units alone use 2" in errs_of({**s, "caps": {"c2": {"max_units": 1}},
                                               "locks": {"u1": "c2", "u3": "c1+c2"}}, units),
       "TC20 a cap the locks already exceed fails")
    ok("give max_units" in errs_of({**spec, "caps": {"c2": {}}}, units), "TC21 a cap with no limit fails")
    ok("needs `quantity`" in errs_of({**spec, "caps": {"c2": {"max_quantity": 3}}}, units), "TC22 a quantity cap needs its quantity")
    f = feasibility({**spec, "caps": {"c1": {"max_quantity": "1e6", "quantity": "total"}}}, units)
    ok(f["caps"][0]["max"] == 1e6, "TC24 (Codex) a cap written 1e6, which YAML reads as a string, is parsed")
    ok("must be a number" in errs_of({**spec, "caps": {"c1": {"max_units": "lots"}}}, units),
       "TC25 a non-numeric cap fails")
    ok("no feasible package" in errs_of({**spec, "packages": {"only_c2": ["c2"]}}, units),
       "TC23 a unit left with no feasible package fails")
    if fails:
        print("SELF-TEST FAILURES:")
        for m in fails:
            print("  -", m)
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("spec", nargs="?")
    ap.add_argument("--units")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)
    if a.self_test:
        return _self_test()
    if not a.spec:
        ap.error("spec required")
    spec = yaml.safe_load(open(a.spec))
    units = costs.load_units(spec, os.path.dirname(os.path.abspath(a.spec)), a.units)
    try:
        f = feasibility(spec, units)
    except ValueError as e:
        print("ERROR: " + str(e))
        return 1
    if a.json:
        print(json.dumps({k: v for k, v in f.items() if k != "caps"} | {"caps": [
            {k: v for k, v in c.items() if k != "weights"} for c in f["caps"]]}, indent=1))
    else:
        for p in costs.packages(spec):
            n = sum(f["feasible"][u][p] for u in f["feasible"])
            print(f"{p}: feasible in {n} of {len(f['feasible'])} units")
        for c in f["caps"]:
            print(f"cap {c['name']} <= {c['max']:g}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
