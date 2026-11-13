#!/usr/bin/env python3
"""Decision spec loader and validator (architecture-v2 §3.1), fail closed.

    python3 spike/analysis/spec.py <decision_spec.yaml>
    python3 spike/analysis/spec.py --self-test

Checks, before any simulation:
- required sections; the engine imports and implements the contract;
- the units table has every column the spec names (engine attributes, populations,
  calibration target, hold-out group, LOO covariates);
- every engine attribute the spec maps is one the engine declares, and the calibrated
  attribute is declared too;
- the options section passes toolkit/alloc cost and package validation, and the
  feasibility table has a feasible package for every unit;
- every alloc component maps to declared engine component values, so every package
  can be simulated; the comparator package exists;
- decision tests name known components and supported types.
"""
from __future__ import annotations

import csv
import importlib
import os
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from toolkit.alloc import costs as acosts, packages as apackages  # noqa: E402
from toolkit.engines.contract import Engine  # noqa: E402

REQUIRED = ("engine", "decision_units", "calibration", "horizon", "options", "objective", "constraints",
            "comparator", "uncertainty", "deliverable")
DT_TYPES = ("interaction_rescale",)


def load_engine(ref: str):
    mod, cls = ref.split(":")
    return getattr(importlib.import_module(mod), cls)()


def read_units(spec: dict, base: str = ROOT) -> list[dict]:
    p = spec["decision_units"]["csv"]
    with open(p if os.path.isabs(p) else os.path.join(base, p), newline="") as f:
        return list(csv.DictReader(f))


def engine_package(spec: dict, comps) -> dict:
    out = {}
    for c in sorted(comps):
        out.update(spec["options"]["engine_map"][c])
    return out


def validate(spec: dict, base: str = ROOT) -> list[str]:
    bad = [f"missing section {k}" for k in REQUIRED if k not in spec]
    if bad:
        return bad
    try:
        eng = load_engine(spec["engine"])
    except Exception as e:  # noqa: BLE001
        return [f"engine {spec['engine']} does not import: {e}"]
    if not isinstance(eng, Engine):
        return [f"engine {spec['engine']} does not implement the Engine contract"]
    card = eng.describe()
    try:
        units = read_units(spec, base)
    except OSError as e:
        return [f"units table unreadable: {e}"]
    if not units:
        return ["units table is empty"]
    cols = set(units[0])
    du = spec["decision_units"]
    need = {du["id"], du.get("holdout_group"), spec["calibration"]["target_column"]}
    for k, pm in (du.get("populations") or {}).items():
        if not isinstance(pm, dict) or "column" not in pm or not pm.get("age_groups"):
            bad.append(f"decision_units.populations.{k}: give column and age_groups")
            continue
        need.add(pm["column"])
        bad += [f"population {k}: engine has no age group {g}" for g in pm["age_groups"] if g not in card.age_groups]
    need |= set(spec["calibration"].get("loo_covariates") or [])
    for attr, m in (du.get("engine_attributes") or {}).items():
        if attr not in card.unit_attributes:
            bad.append(f"engine attribute {attr} is not declared by engine {card.name}")
        if "column" in m:
            need.add(m["column"])
        elif "columns" in m:
            need |= set(m["columns"].values() if isinstance(m["columns"], dict) else m["columns"])
        elif "value" not in m:
            bad.append(f"engine attribute {attr}: give column, columns or value")
    need.discard(None)
    bad += [f"units table has no column {c}" for c in sorted(need - cols)]
    if spec["calibration"]["attribute"] not in card.unit_attributes:
        bad.append(f"calibrated attribute {spec['calibration']['attribute']} is not declared by the engine")
    opts = spec["options"]
    bad += [f"options: {e}" for e in acosts.validate(opts, cols)]
    if bad:
        return bad
    try:
        apackages.feasibility(opts, units)
    except ValueError as e:
        bad.append(f"options: {e}")
    pk = acosts.packages(opts)
    emap = opts.get("engine_map") or {}
    for c in opts["components"]:
        if c not in emap:
            bad.append(f"engine_map: component {c} has no engine mapping")
            continue
        for k, v in emap[c].items():
            if k not in card.components or v not in card.components[k]:
                bad.append(f"engine_map.{c}: {k}={v!r} is not an engine component value")
    for name, cs in pk.items():
        keys = [k for c in cs for k in (emap.get(c) or {})]
        if len(keys) != len(set(keys)):
            bad.append(f"package {name}: two components map to the same engine component")
    if spec["comparator"].get("package") not in pk:
        bad.append(f"comparator package {spec['comparator'].get('package')} is not a package")
    for dt in spec.get("decision_tests") or []:
        if dt.get("type") not in DT_TYPES:
            bad.append(f"decision test {dt.get('id')}: type {dt.get('type')} not in {DT_TYPES}")
        for c in list(dt.get("components") or []) + list(dt.get("with_any") or []):
            if c not in opts["components"]:
                bad.append(f"decision test {dt.get('id')}: unknown component {c}")
    c = spec["constraints"]
    if not isinstance(c.get("envelope_usd"), (int, float)) or c["envelope_usd"] <= sum((c.get("fixed_costs_usd") or {}).values()):
        bad.append("constraints: envelope_usd must exceed the fixed costs")
    return bad


def _self_test() -> int:
    import tempfile
    fails = []

    def ok(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "u.csv")
        with open(p, "w") as f:
            f.write("id,x,pop,tgt,grp,elig\n1,0.1,100,0.2,a,1\n2,0.5,200,0.3,b,0\n3,-0.3,150,0.1,a,1\n")
        spec = {
            "engine": "toolkit.engines.contract:ToyEngine",
            "decision_units": {"csv": p, "id": "id", "holdout_group": "grp", "populations": {"all": {"column": "pop", "age_groups": ["all"]}},
                               "engine_attributes": {"group": {"column": "grp"}}},
            "calibration": {"attribute": "x", "target_column": "tgt", "log_range": [-2, 2], "window": [1, 2], "metric": "prev"},
            "horizon": {"start": 0, "stop": 3, "window": [0, 3]},
            "options": {"units": {"csv": p, "id": "id", "populations": {"total": "pop"}},
                        "components": {"a": {}, "b": {}}, "packages": {"generate": True},
                        "eligibility": {"b": {"column": "elig"}},
                        "engine_map": {"a": {"a": 1}, "b": {"b": 1}},
                        "prices": {"base_year": "2024", "index": {"2024": 1.0}},
                        "cost_items": [{"id": "ca", "component": "a", "value": 1.0, "price_year": "2024", "price_rule": "as_published",
                                        "basis": "per_person", "includes": ["x"]},
                                       {"id": "cb", "component": "b", "value": 2.0, "price_year": "2024", "price_rule": "as_published",
                                        "basis": "per_person", "includes": ["y"]}]},
            "objective": {"outcome": "dalys"}, "constraints": {"envelope_usd": 500},
            "comparator": {"package": "none"}, "uncertainty": {"draws": 4},
            "decision_tests": [{"id": "DT1", "type": "interaction_rescale", "components": ["b"], "with_any": ["a"]}],
            "deliverable": "package per unit"}
        ok(validate(spec) == [], f"S1 a well-formed toy spec validates: {validate(spec)}")
        import copy
        s2 = copy.deepcopy(spec)
        s2["decision_units"]["engine_attributes"]["zz"] = {"column": "x"}
        ok(any("not declared" in e for e in validate(s2)), "S2 an engine attribute the engine does not declare is refused")
        s3 = copy.deepcopy(spec)
        s3["options"]["engine_map"]["b"] = {"b": 7}
        ok(any("not an engine component value" in e for e in validate(s3)), "S3 a component mapped to an unknown engine value is refused")
        s4 = copy.deepcopy(spec)
        s4["calibration"]["target_column"] = "nope"
        ok(any("no column nope" in e for e in validate(s4)), "S4 a missing calibration target column is refused")
        s5 = copy.deepcopy(spec)
        s5["comparator"]["package"] = "x"
        ok(any("comparator" in e for e in validate(s5)), "S5 an unknown comparator package is refused")
        s6 = copy.deepcopy(spec)
        del s6["options"]["cost_items"][1]
        ok(any("no cost item" in e for e in validate(s6)), "S6 a packaged component without a cost item is refused (toolkit/alloc)")
        s7 = copy.deepcopy(spec)
        s7["constraints"] = {"envelope_usd": 10, "fixed_costs_usd": {"f": 20}}
        ok(any("envelope" in e for e in validate(s7)), "S7 an envelope below its fixed costs is refused")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    s = yaml.safe_load(open(sys.argv[1]))
    errs = validate(s)
    print("spec valid" if not errs else "spec invalid:\n  " + "\n  ".join(errs))
    sys.exit(1 if errs else 0)
