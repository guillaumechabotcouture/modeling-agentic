#!/usr/bin/env python3
"""Tests extracted from toolkit/spec/schema.py."""
from __future__ import annotations

import copy
import csv
import hashlib
import importlib
import json
import math
import os
import re
import sys

import yaml

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(TESTS_DIR)
ROOT = REPO
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from toolkit.spec import schema
from toolkit.spec.schema import (
    CalibrationScaleError, ENGINE_CHOICE, PLAN_RULES, SETTINGS, Spec, SpecError, _read_binding, _validate_deliverable, acosts, attribute_notes, attribute_notes_markdown, attribute_problems, binding_for_ref, calibration_scale, card_scale, check_engine_ref, conditional_driver_problems, derived_toy_spec, direct_toy, engine_class, frame_problems, labels_problems, load, load_binding, load_engine, parameter_domain, registered_engines, resolved_scale, scale_problems, sha256_file, spec_scale_problems, toy_spec, validate,
)


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    ap.parse_args(argv)
    return _self_test()


def _self_test_joint_packages(ok):
    """SPJ: packages generated within the envelope (a joint dose-block space) and the by-group deliverable."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        raw = yaml.safe_load(open(toy_spec(d)))
        # three groups of mutually exclusive levels a1..a3, b1..b3, c1..c2 (cost = level); envelope 3
        comps = {f"{g}{k}": {"group": g} for g, n in (("a", 3), ("b", 3), ("c", 2)) for k in range(1, n + 1)}
        raw["options"]["components"] = comps
        raw["options"]["engine_map"] = {c: {"a": 1} for c in comps}
        raw["options"]["cost_items"] = [{"id": f"k_{c}", "component": c, "value": float(c[1:]), "price_year": "2024",
                                         "price_rule": "as_published", "basis": "fixed", "includes": [c]}
                                        for c in comps]
        raw["options"]["packages"] = {"generate": True, "within_envelope": True}
        raw["constraints"] = {"envelope_usd": 3.0}
        s = Spec(raw, d, os.path.join(d, "decision_spec.yaml"))
        pk = s.packages()
        levels = lambda cs: sum(int(c[1:]) for c in cs)  # noqa: E731
        full = acosts.packages(raw["options"])
        ok(len(full) == 4 * 4 * 3 and all(levels(cs) <= 3 for cs in pk.values())
           and len(pk) == sum(1 for cs in full.values() if levels(cs) <= 3) and "none" in pk,
           f"SPJ1 within_envelope keeps exactly the generated packages a plan can afford ({len(pk)} of {len(full)})")
        al = s.alloc_spec()
        ok(set(acosts.packages(al)) == set(pk), "SPJ2 the alloc spec carries the same pruned package list")
        raw2 = copy.deepcopy(raw)
        raw2["options"]["cost_items"][0]["value"] = -1.0
        try:
            Spec(raw2, d).packages()
            ok(False, "SPJ3 a negative cost item refuses pruning")
        except SpecError as e:
            ok("non-negative" in str(e), f"SPJ3 a negative cost item refuses pruning: {e}")
        raw5 = copy.deepcopy(raw)                 # Codex r1 P1-4: a cost draw at half price keeps level-6 plans
        raw5["uncertainty"]["sources"] = list(raw5["uncertainty"]["sources"]) + [
            {"kind": "costs", "items": {f"k_{c}": [0.5, 1.5] for c in comps}}]
        raw5["uncertainty"]["excluded"] = [e for e in raw5["uncertainty"]["excluded"] if e.get("kind") != "costs"]
        s5 = Spec(raw5, d)
        pk5 = s5.packages()
        ok(any(levels(cs) == 6 for cs in pk5.values()) and not any(levels(cs) > 6 for cs in pk5.values())
           and s5.prune_budget == 3.0,
           f"SPJ6 pruning keeps every package affordable at the lowest cost multiplier ({len(pk5)} packages)")
        raw6 = copy.deepcopy(raw)                 # the same bound for a cost_items decision test at half price
        raw6["analysis"]["decision_tests"] = list(raw6["analysis"].get("decision_tests") or []) + [
            {"id": "DTc", "type": "cost_items", "text": "t", "multipliers": {f"k_{c}": 0.5 for c in comps}}]
        pk6 = Spec(raw6, d).packages()
        ok(any(levels(cs) == 6 for cs in pk6.values()) and not any(levels(cs) > 6 for cs in pk6.values()),
           f"SPJ7 pruning keeps every package a cost_items decision test can afford ({len(pk6)} packages)")
        raw3 = copy.deepcopy(raw)
        raw3["options"]["packages"] = {"generate": True}
        ok(len(Spec(raw3, d).packages()) == len(full), "SPJ4 without within_envelope every combination is kept")
        r4 = yaml.safe_load(open(toy_spec(d)))
        r4["deliverable"]["by_group"] = {"columns": {"group": "g"}, "quantity": {"zz": "nope"}}
        errs = _validate_deliverable(Spec(r4, d))
        ok(any("by_group.columns" in e for e in errs) and any("not an engine group" in e for e in errs)
           and any("not in analysis.cost_outputs" in e for e in errs) and any("evaluation: direct" in e for e in errs),
           f"SPJ5 a malformed by_group deliverable is refused: {errs}")


def _self_test_plan_rule(ok):
    """SPEV (hb1 fix 11, J): deliverable.plan picks the plan the deliverable carries; absent is the point plan."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        raw = yaml.safe_load(open(toy_spec(d)))
        ok(_validate_deliverable(Spec(raw, d)) == [] and "plan" not in raw["deliverable"],
           "SPEV1 a spec without deliverable.plan validates as before (the point plan)")
        good = []
        for v in PLAN_RULES:
            r = copy.deepcopy(raw)
            r["deliverable"]["plan"] = v
            good.append(_validate_deliverable(Spec(r, d)))
        ok(good == [[] for _ in PLAN_RULES], f"SPEV2 each declared plan rule {PLAN_RULES} validates: {good}")
        r = copy.deepcopy(raw)
        r["deliverable"]["plan"] = "mean"
        errs = _validate_deliverable(Spec(r, d))
        ok(any("deliverable.plan" in e for e in errs), f"SPEV3 an unknown plan rule is refused: {errs}")
        bad = {"no uncertainty source": lambda x: x["uncertainty"].update(sources=[]),
               "affordability enforce": lambda x: x["uncertainty"].update(
                   affordability={"enforce": True, "max_share_over_budget": 0.05}),
               "estimates kind": lambda x: x["deliverable"].update(kind="estimates"),
               "headline variant": lambda x: x["analysis"].update(plan_variants={"headline": "v", "variants": []}),
               "variants, no headline": lambda x: x["analysis"].update(plan_variants={"variants": [
                   {"id": "given", "type": "fixed_plan", "column": "given", "text": "t"}]})}
        miss = []
        for k, mut in bad.items():
            r = copy.deepcopy(raw)
            r["deliverable"]["plan"] = "expected_value"
            mut(r)
            if not any("deliverable.plan" in e for e in _validate_deliverable(Spec(r, d))):
                miss.append(k)
        # expected_value is supported together with deliverable.by_group
        r_bg = copy.deepcopy(raw)
        r_bg["analysis"]["evaluation"] = "direct"
        r_bg["analysis"]["cost_outputs"] = {"q_all": {"cost_driver": "a_doses"}}
        r_bg["deliverable"]["plan"] = "expected_value"
        r_bg["deliverable"]["by_group"] = {"columns": {"group": "age_group", "quantity": "doses", "value": "averted"},
                                           "quantity": {"all": "q_all"}}
        ev_bg_errs = _validate_deliverable(Spec(r_bg, d))
        ok(ev_bg_errs == [], f"SPEV6 expected_value validates together with deliverable.by_group: {ev_bg_errs}")
        # constraints.envelope_exact (fixed already-held supply)
        r_ex = copy.deepcopy(raw)
        r_ex["constraints"]["envelope_exact"] = True
        r_ex["uncertainty"]["sources"] = [{"kind": "engine_parameters"}]
        r_ex["uncertainty"]["excluded"] = [
            {"kind": "calibration", "reason": "r"}, {"kind": "costs", "reason": "fixed supply"},
            {"kind": "emulator", "reason": "r"}]
        sp_ex_path = os.path.join(d, "spec_exact.yaml")
        yaml.safe_dump(r_ex, open(sp_ex_path, "w"), sort_keys=False)
        ex_errs = [e for e in validate(load(sp_ex_path)) if "envelope_exact" in e]
        ok(ex_errs == [], f"SPEX1 constraints.envelope_exact: true validates with fixed unit costs: {ex_errs}")
        ex_bad = {}
        for tag, mut in (
                ("non_bool", lambda x: x["constraints"].update(envelope_exact="yes")),
                ("with_costs_uq", lambda x: x["uncertainty"].update(
                    sources=[{"kind": "engine_parameters"}, {"kind": "costs"}],
                    excluded=[{"kind": "calibration", "reason": "r"}, {"kind": "emulator", "reason": "r"}])),
                ("with_affordability", lambda x: x["uncertainty"].update(
                    affordability={"enforce": True, "max_share_over_budget": 0.05}))):
            rb = copy.deepcopy(r_ex)
            mut(rb)
            pb = os.path.join(d, f"spec_exact_{tag}.yaml")
            yaml.safe_dump(rb, open(pb, "w"), sort_keys=False)
            ex_bad[tag] = [e for e in validate(load(pb)) if "envelope_exact" in e]
        ok(all(ex_bad.values()), f"SPEX2 invalid envelope_exact combinations are refused: {ex_bad}")
        from toolkit.spec import estimates as ES
        es_bad = {}
        with tempfile.TemporaryDirectory() as de:
            ep = ES.toy_dir(de)
            es_bad["(none)"] = validate(load(ep))
            for v in ("expected_value", "bogus", "point"):
                re_ = yaml.safe_load(open(ep))
                re_["deliverable"]["plan"] = v
                yaml.safe_dump(re_, open(ep, "w"), sort_keys=False)
                es_bad[v] = [e for e in validate(load(ep)) if "deliverable.plan" in e]
        es_bad["(none)"] = not es_bad["(none)"]       # the plain estimates spec validates
        ok(all(es_bad.values()), f"SPEV5 the public validate() refuses deliverable.plan on an estimates spec, whatever "
                                 f"its value (Codex r1 P1-4): {es_bad}")
        ok(not miss, f"SPEV4 expected_value is refused without an uncertainty source, with a reserve "
                     f"(affordability enforce), with plan variants and on an estimates deliverable (not refused: {miss})")


def _self_test_outcome_adjustments(ok, s, d, bad):
    """F6 run 4, owner decision R3: base outcome adjustments (analysis.outcome_adjustments): a package's outcomes set to
    a trial ratio of its counterpart's in every evaluation, checked at sign-off; an outcome_ratio test may take the
    value "engine" (the base adjustment removed) only for an adjusted component."""
    good = {"id": "OA1", "component": "a", "reference": "b", "value": 0.5, "source": "toy trial"}

    def oa(*items):
        return lambda r: r["analysis"].update(outcome_adjustments=[dict(x) for x in items])
    bad(lambda r: r["analysis"].update(outcome_adjustments={"a": 1}), "outcome_adjustments",
        "SP-OA1 outcome_adjustments must be a list of adjustments")
    bad(oa(dict(good, component="zz")), "unknown component", "SP-OA2 an unknown component is refused")
    bad(oa(dict(good, reference="a")), "differ", "SP-OA3 a component adjusted against itself is refused")
    bad(oa(dict(good, value=0.0)), "positive", "SP-OA4 a ratio that is not positive is refused")
    bad(oa(good, dict(good, id="OA2", component="b", reference="a")), "itself adjusted",
        "SP-OA5 a reference that is itself adjusted is refused (no chains)")
    bad(oa(good, dict(good, id="OA2")), "more than once", "SP-OA6 a component adjusted twice is refused")
    bad(oa(dict(good, source="")), "source", "SP-OA7 an adjustment without its source is refused")
    bad(lambda r: (r["comparator"].update(package="a"), oa(good)(r)), "comparator",
        "SP-OA8 an adjustment of a component the comparator holds is refused (every benefit is measured from it)")
    r_g = copy.deepcopy(s.raw)
    oa(good)(r_g)
    ok(validate(Spec(r_g, d)) == [], f"SP-OA9 a well-formed outcome adjustment validates: {validate(Spec(r_g, d))[:3]}")
    dor = {"id": "DTe", "type": "outcome_ratio", "text": "t",
           "outcome_ratio": {"component": "a", "reference": "b", "values": ["engine", 0.4]}}
    bad(lambda r: r["analysis"]["decision_tests"].append(copy.deepcopy(dor)), "engine",
        "SP-OA10 an outcome_ratio value \"engine\" without a base adjustment of its component is refused")
    r_x = copy.deepcopy(r_g)      # Codex review of #251 P1-4: a test on a base adjustment's reference is refused
    r_x["analysis"]["decision_tests"].append({"id": "DTr", "type": "outcome_ratio", "text": "t",
                                              "outcome_ratio": {"component": "b", "reference": "a", "values": [0.5]}})
    ok(any("base adjustment" in e for e in validate(Spec(r_x, d))),
       f"SP-OA12 (P1-4) an outcome_ratio test involving a base adjustment's reference is refused: "
       f"{[e for e in validate(Spec(r_x, d)) if 'DTr' in e][:2]}")
    r_1 = copy.deepcopy(r_g)      # P2-9: with a base adjustment, a ratio of 1 is a real case
    r_1["analysis"]["decision_tests"].append({"id": "DT1r", "type": "outcome_ratio", "text": "t",
                                              "outcome_ratio": {"component": "a", "reference": "b", "values": [1.0]}})
    ok(validate(Spec(r_1, d)) == [], f"SP-OA13 (P2-9) a ratio of 1 on an adjusted component validates: "
                                     f"{validate(Spec(r_1, d))[:2]}")
    r_e = copy.deepcopy(r_g)
    r_e["analysis"]["decision_tests"].append(copy.deepcopy(dor))
    ok(validate(Spec(r_e, d)) == [], f"SP-OA11 \"engine\" with a base adjustment validates: {validate(Spec(r_e, d))[:3]}")


def _self_test_burden_scale(ok, s, d, bad):
    """F6 run 3, owner decision D-c: a burden_scale decision test scales every package's outcomes in the units of the
    listed external references' groups by reference / model (validation.burden), checked at sign-off."""
    from toolkit.analysis import burden as BV
    open(os.path.join(d, "bs_refs.csv"), "w").write("\n".join([",".join(BV.COLUMNS),
        "A,r0,,cases,count,,1,1e6,0,1e15,estimated,synthetic A,overlap,,,REF-A,",
        "B,r1,,dalys,count,,1,1e12,1e11,1e13,estimated,synthetic B,ratio,,,REF-B,",
        "C,r2,,cases,count,,1,1,0.5,2,reported,synthetic C,ratio,,,REF-C,",
        "E,*,,cases,count,,2,1e6,0,1e15,estimated,synthetic E,overlap,,,REF-E,",
        "F,r2,all,cases,rate,1000,1,1e3,0,1e9,estimated,synthetic F,overlap,,,REF-F,"]) + "\n")

    def bs(burden=True, **kw):
        def mut(r):
            if burden:
                r["validation"] = {"burden": {"csv": "bs_refs.csv"}}
                r["calibration"]["target_source_id"] = "TOY-TARGETS"
            r["analysis"]["decision_tests"].append(dict({"id": "DTb", "type": "burden_scale", "text": "t"}, **kw))
        return mut
    good = {"references": ["A", "B"], "shares": [0.5, 1.0]}
    bad(bs(), "give burden_scale", "SP-BS1 a burden_scale test without its mapping is refused")
    bad(bs(burden=False, burden_scale=good), "validation.burden",
        "SP-BS2 a burden_scale test without validation.burden references is refused")
    bad(bs(burden_scale=dict(good, references=["A", "zz"])), "unknown reference",
        "SP-BS3 a burden_scale test naming a reference the file does not hold is refused")
    bad(bs(burden_scale=dict(good, references=["C"])), "estimated",
        "SP-BS4 a reported (not estimated) reference cannot set a scale")
    bad(bs(burden_scale=dict(good, shares=[0.0])), "shares", "SP-BS5 a share outside (0, 1] is refused")
    bad(bs(burden_scale=dict(good, references=["A", "E"])), "more than one",
        "SP-BS6 references whose groups share a unit are refused (a unit takes one factor)")
    bad(bs(burden_scale=dict(good, references=["F"])), "model window",
        "SP-BS8 (Codex review of #246 P2-3) an age-banded row with no model window is refused: its value needs a new "
        "prediction, and the test reads the point outputs only")
    r_b = copy.deepcopy(s.raw)
    bs(burden_scale=good)(r_b)
    ok(validate(Spec(r_b, d)) == [], f"SP-BS7 a well-formed burden_scale test validates: {validate(Spec(r_b, d))[:3]}")


def _self_test_unit_attribute(ok, s, d, bad):
    """F6: a unit_attribute decision test is checked at sign-off by the runner's own planner (decision_tests.py
    plan_unit_attribute): column, units, values within the column's declared domain, and not an identity."""
    def ua(**kw):
        return lambda r: (direct_toy(r), r["analysis"]["decision_tests"].append(
            dict({"id": "DTu", "type": "unit_attribute", "text": "t"}, **kw)))
    bad(ua(), "give unit_attribute", "SP-UA1 a unit_attribute test without its unit_attribute mapping is refused")
    bad(ua(unit_attribute={"column": "nocol", "values": [0.5]}), "not a column of the units table",
        "SP-UA2 a unit_attribute test on an unknown column is refused")
    bad(ua(unit_attribute={"column": "cov", "units": ["0", "zz"], "values": [0.5]}), "'zz'",
        "SP-UA3 a unit_attribute test naming a unit id not in the table is refused")
    bad(ua(unit_attribute={"column": "cov", "values": [9.0]}), "declared domain",
        "SP-UA4 a unit_attribute value outside the column's declared domain (the engine card's valid range) is refused")
    bad(ua(unit_attribute={"column": "cov", "scale": [1.0]}), "identity",
        "SP-UA5 a unit_attribute test that leaves every unit unchanged is refused")
    bad(ua(unit_attribute={"column": "pop", "values": [5.0]}), "feeds no engine unit attribute",
        "SP-UA6 a unit_attribute test on a column bound to no engine attribute is refused")
    bad(ua(unit_attribute={"column": "cov", "values": [0.5], "scale": [2.0]}), "exactly one of",
        "SP-UA7 a unit_attribute test with two value modes is refused")
    bad(ua(unit_attribute={"column": "cov", "set_from": "../elsewhere.csv"}), "inside the run directory",
        "SP-UA8 a set_from table outside the run directory is refused")
    r = copy.deepcopy(s.raw)
    r["decision_units"]["engine_attributes"] = {"x": {"column": "cov"}}
    r["analysis"]["decision_tests"].append({"id": "DTu", "type": "unit_attribute", "text": "t",
                                            "unit_attribute": {"column": "cov", "values": [0.5]}})
    e9 = validate(Spec(r, d))
    ok(any("calibration sets" in e for e in e9),
       f"SP-UA9 a column bound to the calibrated attribute is refused (the calibration overrides it): {e9[:2]}")
    r = copy.deepcopy(s.raw)
    ua(unit_attribute={"column": "cov", "units": ["0", "1"], "values": [0.5, -0.5]})(r)
    ok(validate(Spec(r, d)) == [], f"SP-UA10 a well-formed unit_attribute test validates: {validate(Spec(r, d))[:3]}")
    r = direct_toy(copy.deepcopy(s.raw))         # #228 r1 P2-5: sign-off reads the emulator design box too
    r["analysis"].update(evaluation="emulator", emulator=copy.deepcopy(s.raw["analysis"]["emulator"]))
    r["analysis"]["emulator"]["features"] = ["cov"]
    r["analysis"]["decision_tests"] = [{"id": "DTu", "type": "unit_attribute", "text": "t",
                                        "unit_attribute": {"column": "cov", "values": [1.9]}}]
    e11 = validate(Spec(r, d))
    ok(any("design box" in e for e in e11),
       f"SP-UA11 at sign-off a value inside the declared domain but outside the emulator's design box is refused: "
       f"{e11[:2]}")


def _self_test() -> int:
    import tempfile
    fails = []

    def ok(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    # F6 S2-2: a cost driver an engine produces only under a parameter setting (outputs.cost_drivers_when)
    when = {"c_kids": {"switch": 1.0}}

    def cdp(raw, drawn=None):
        dom = parameter_domain(raw, {"switch": 0.0}, drawn or {})
        return conditional_driver_problems("kids", "c_kids", when, dom)
    on = {"engine_parameters": {"switch": {"value": 1}}}
    ok(cdp({}) and "switch" in cdp({})[0] and cdp(on) == []
       and conditional_driver_problems("t", "treated", when, parameter_domain({}, {"switch": 0.0}, {})) == [],
       "SP-CD1 a conditional cost driver is refused unless the run sets its parameter (engine_parameters) to the "
       "stated value; an unconditional driver is never refused")
    # Codex r1 P2-1: the predicate must hold over every value the analysis can give the parameter
    rng = {"engine_parameters": {"switch": {"value": 1, "range": [0, 1], "dist": "uniform"}}}
    dt = dict(on, analysis={"decision_tests": [{"id": "sw_off", "type": "parameter_value", "parameter": "switch",
                                                "values": [1, 0]}]})
    pri = dict(on, uncertainty={"priors": {"switch": {"dist": "uniform", "range": [0.5, 1]}}})
    ok(any("range" in x for x in cdp(rng)) and any("sw_off" in x for x in cdp(dt)) and cdp(pri)
       and cdp(on, {"switch": (0.0, 1.0)}) and cdp(dict(on, analysis={"decision_tests": [
           {"id": "keep", "type": "parameter_value", "parameter": "switch", "values": [1]}]})) == [],
       "SP-CD2 a range (engine_parameters, uncertainty.priors or a drawn card prior, which can reach a fractional "
       "switch) or a decision-test value that turns the driver off is refused; a test at the stated value is not")
    with tempfile.TemporaryDirectory() as d:
        p = toy_spec(d)
        s = load(p)
        ok(validate(s) == [], f"SP1 a well-formed toy spec validates: {validate(s)}")

        # Codex r2 P2-2 (integrated): the toy spec draws the fitted parameters (uncertainty source engine_parameters)
        from toolkit import import_guard as _G  # an unregistered toy, registered for this self-test only
        _G.register_test_engine("toolkit.engines.conditional_drivers:ToyConditionalEngine")
        def cd_errs(when):
            r = copy.deepcopy(s.raw)
            r["engine"] = {"ref": "toolkit.engines.conditional_drivers:ToyConditionalEngine", "config": {"when": when}}
            r["analysis"]["cost_outputs"] = {"extra": {"cost_driver": "extra_doses"}}
            return [x for x in validate(Spec(r, d)) if "extra_doses" in x]
        e_fit, e_fix = cd_errs({"extra_doses": {"eff_a": 0.5}}), cd_errs({"extra_doses": {"noise": 0.0}})
        ok(any("fixed" in x and "eff_a" in x for x in e_fit) and e_fix == [],
           f"SP-CD3 with fitted-parameter uncertainty on, a conditional driver whose predicate names a fitted parameter "
           f"is refused; one on a fixed parameter at its value is accepted ({e_fit[:1]}, {e_fix[:1]})")

        def bad(mut, needle, label):
            r = copy.deepcopy(s.raw)
            mut(r)
            errs = validate(Spec(r, d))
            ok(any(needle in e for e in errs), f"{label}: {errs[:3]}")
        bad(lambda r: r["decision_units"]["engine_attributes"].update(zz={"column": "cov"}), "not declared",
            "SP2 an engine attribute the engine does not declare is refused")
        bad(lambda r: r["options"]["engine_map"].update(b={"b": 7}), "not an engine component value",
            "SP3 a component mapped to an unknown engine value is refused")
        bad(lambda r: r["calibration"].update(target_column="nope"), "no column nope",
            "SP4 a missing calibration target column is refused")
        bad(lambda r: r["calibration"].update(scale="log"), "includes values below 0",
            "SP4s a log scale for an attribute whose card range includes negative values is refused before sign-off")
        import types as _types
        cneg, cpos = (_types.SimpleNamespace(valid_ranges={"a": [-8, 2]}), _types.SimpleNamespace(valid_ranges={"a": [0.2, 9]}))
        ok(calibration_scale({"attribute": "a"}, cneg) == "linear" and calibration_scale({"attribute": "a"}, cpos) == "log"
           and calibration_scale({"attribute": "a"}, None) == "linear"
           and calibration_scale({"attribute": "a", "scale": "log"}, cpos) == "log"
           and calibration_scale({"attribute": "a", "scale": "linear"}, cpos) == "linear"
           and calibration_scale({"attribute": "a"}, _types.SimpleNamespace(valid_ranges={"a": [0.0, 1.0]})) == "log",
           "SP4t one scale default: declared, else the card's range (below 0: linear; from 0 up: log), else linear")
        try:
            calibration_scale({"attribute": "a", "scale": "log"}, cneg)
            neg = "resolved"
        except CalibrationScaleError as e:
            neg = f"refused: {e}"
        ok(neg.startswith("refused") and "below 0" in neg,
           f"SP4t2 the resolution itself refuses a declared log scale on a card range going below 0 ({neg[:120]})")
        bad(lambda r: r["calibration"].update(scale=None), "calibration.scale: log or linear",
            "SP4v an explicit null scale is refused, not read as the default")
        czero = _types.SimpleNamespace(valid_ranges={"a": [0.0, 0.1]})
        ok(not scale_problems({"attribute": "a", "scale": "log", "range": [-13.8, -9.2]}, czero),
           "SP4w a log scale on a card range from 0 up (tiny positive values) is accepted")
        raw_r = copy.deepcopy(s.raw)
        raw_r["calibration"].pop("scale", None)
        ok(resolved_scale(raw_r) == resolved_scale(Spec(raw_r, d)) == "linear",
           f"SP4x a raw mapping and its Spec resolve the same scale from the engine card ({resolved_scale(raw_r)})")
        ok(scale_problems({"attribute": "a", "scale": "log"}, cneg) and not scale_problems({"attribute": "a"}, cneg)
           and not scale_problems({"attribute": "a", "scale": "log"}, cpos),
           "SP4u only a declared log scale on a card range going below 0 is refused")
        # Codex r2 of #334: one resolution that fails closed, reached by every path that consumes the scale
        import types as _ty
        from types import MappingProxyType as _MP

        class _Flaky:          # a card read that fails once, then answers [0, 0.1] (a log range)
            n = 0

            def describe(self):
                _Flaky.n += 1
                if _Flaky.n == 1:
                    raise OSError("planted: card unreadable")
                return _ty.SimpleNamespace(valid_ranges={"seed": [0, 0.1]})
        rf = copy.deepcopy(s.raw)
        rf["calibration"].pop("scale", None)
        rf["calibration"]["attribute"] = "seed"
        sf = Spec(rf, d)
        sf._engine = _Flaky()
        try:
            got1 = sf.calibration_scale()
        except SpecError as e:
            got1 = f"refused: {e}"
        ok(str(got1).startswith("refused") and "card" in str(got1),
           f"SP4y (r2 P1-1) a failed card read refuses the scale instead of falling back to linear ({got1})")
        rb = copy.deepcopy(rf)
        rb["engine"] = {"ref": "toolkit.engines.contract:NoSuchEngine"}
        try:
            got2 = resolved_scale(rb)
        except SpecError as e:
            got2 = f"refused: {e}"
        ok(str(got2).startswith("refused"),
           f"SP4y2 (r2 P1-1) a raw mapping whose engine card cannot be read refuses too ({got2})")
        r_pl = copy.deepcopy(s.raw)
        r_pl["engine"] = {"auto": True, "candidates": ["toolkit.engines.contract:ToyEngine"]}
        r_pl["calibration"]["scale"] = "log"
        e_log = validate(Spec(r_pl, d), pending_ok=True)
        r_pl["calibration"]["scale"] = None
        e_nul = validate(Spec(r_pl, d), pending_ok=True)
        ok(any("below 0" in e for e in e_log) and any("calibration.scale" in e for e in e_nul),
           f"SP4z (r2 P1-3) sign-off with the engine pending checks the scale against every candidate's card "
           f"({e_log[-2:]}, {e_nul[-2:]})")
        r_pu = copy.deepcopy(s.raw)
        r_pu["engine"] = {"auto": True, "candidates": ["toolkit.engines.contract:ToyEngine",
                                                       "toolkit.engines.compartmental.engine:CompartmentalEngine"]}
        r_pu["calibration"].pop("scale", None)
        r_pu["calibration"]["attribute"] = "seed"
        e_und = validate(Spec(r_pu, d), pending_ok=True)
        ok(any("calibration.scale" in e for e in e_und),
           f"SP4z2 (r2 P1-3) an undeclared scale is refused at sign-off when the candidates' cards cannot settle it "
           f"({e_und[-2:]})")

        def _none(r, sc):
            r["calibration"]["method"] = "none"
            r["calibration"]["scale"] = sc
            r["uncertainty"]["sources"] = [x for x in r["uncertainty"]["sources"] if x.get("kind") != "calibration"]
            r["uncertainty"]["excluded"] = r["uncertainty"]["excluded"] + [{"kind": "calibration", "reason": "toy"}]
        bad(lambda r: _none(r, "log"), "below 0",
            "SP4aa (r2 P1-4) method none still has its scale checked (the emulator design consumes the attribute)")
        bad(lambda r: _none(r, None), "calibration.scale",
            "SP4aa2 (r2 P1-4) method none refuses an explicit null scale too")
        r_ch = copy.deepcopy(s.raw)
        r_ch["calibration"].pop("scale", None)
        s_ch = Spec(r_ch, d)
        first = s_ch.calibration_scale()
        r_ch["engine"] = {"ref": "toolkit.engines.renewal.engine:RenewalEngine",
                          "config": {"generation_interval": [1.0]}}
        r_ch["calibration"]["attribute"] = "seed"
        got_s, got_r = s_ch.calibration_scale(), resolved_scale(copy.deepcopy(r_ch))
        ok(first == "linear" and got_s == got_r == "log" and type(s_ch.engine()).__name__ == "RenewalEngine",
           f"SP4ab (r2 P1-5) a Spec whose engine changed reads the new engine's card ({first}, {got_s}, {got_r}, "
           f"{type(s_ch.engine()).__name__})")
        mp = _MP({"calibration": {"attribute": "seed", "scale": "log"}})
        ok(resolved_scale(mp) == "log", "SP4ac (r2 P1-6) a read-only mapping keeps its declared scale")
        cinf = _ty.SimpleNamespace(valid_ranges={"x": [-math.inf, 0]})
        ok(scale_problems({"attribute": "x", "scale": "log"}, cinf) != [] and card_scale(cinf, "x") == "linear",
           "SP4ad (r2 P1-7) a card range from -inf is a range going below 0: a log scale is refused")
        # Codex r3 of #334
        import json as _json
        import yaml as _yaml
        from unittest import mock as _mock
        from toolkit.spec import preconditions as _PC
        r_pc = copy.deepcopy(s.raw)
        r_pc["calibration"]["scale"] = None
        pc_bad = _PC.problems(d, None, "owner", spec=Spec(r_pc, d))
        ok(any("calibration.scale" in e for e in pc_bad),
           f"SP4ae (r3 P1-1) the shared signing preconditions run the scale check ({pc_bad[-2:]})")
        with tempfile.TemporaryDirectory() as dc:
            r_ec = copy.deepcopy(s.raw)
            r_ec["engine"] = {"auto": True, "candidates": ["toolkit.engines.contract:ToyEngine"]}
            r_ec["calibration"].update(attribute="seed", scale="log")
            with open(os.path.join(dc, "decision_spec.yaml"), "w") as f:
                _yaml.safe_dump(r_ec, f)
            with open(os.path.join(dc, ENGINE_CHOICE), "w") as f:
                _json.dump({"engine": {"ref": "toolkit.engines.contract:ToyEngine"}}, f)
            with _mock.patch.object(schema, "_auto_cards", new=
                                    lambda raw: [("planted", _ty.SimpleNamespace(valid_ranges={"seed": [-1, 1]}))]):
                s_ec = load(os.path.join(dc, "decision_spec.yaml"))
                ec_bad = spec_scale_problems(s_ec)
            ok(not s_ec.engine_pending and any("below 0" in e and "planted" in e for e in ec_bad),
               f"SP4af (r3 P1-2) with an engine choice written, the scale is still checked against the signed "
               f"candidates of engine: auto ({ec_bad})")
        r_pr = copy.deepcopy(s.raw)
        r_pr["engine"] = {"auto": True, "candidates": ["toolkit.engines.empirical.engine:PackageRateEngine"]}
        r_pr["calibration"].pop("scale", None)
        r_pr["calibration"]["attribute"] = "log_rate"
        s_pr = Spec(r_pr, d)
        pr_bad = spec_scale_problems(s_pr)
        ok(pr_bad == [] and s_pr.calibration_scale() == "linear",
           f"SP4ag (r3 P1-3) a candidate is read with the configuration engine selection gives it ({pr_bad})")
        r_sw = copy.deepcopy(s.raw)
        r_sw["calibration"].pop("scale", None)
        s_sw = Spec(r_sw, d)
        s_sw.calibration_scale()
        r_sw["engine"] = {"auto": True, "candidates": ["toolkit.engines.contract:ToyEngine"]}
        try:
            sw = s_sw.calibration_scale()
        except SpecError as e:
            sw = f"refused: {e}"
        ok(sw == "linear", f"SP4ah (r3 P2-4) a Spec switched to engine: auto in place reads the candidates ({sw})")
        mp_auto = _MP({"engine": _MP({"auto": True, "candidates": ["toolkit.engines.contract:ToyEngine"]}),
                       "calibration": {"attribute": "x"}})
        try:
            mpa = resolved_scale(mp_auto)
        except SpecError as e:
            mpa = f"refused: {e}"
        ok(mpa == "linear", f"SP4ai (r3 P2-5) a read-only engine: auto mapping keeps its candidates ({mpa})")
        try:
            ne = resolved_scale(Spec({"calibration": {"attribute": "x"}}, d))
        except SpecError as e:
            ne = f"refused: {e}"
        ok(ne == resolved_scale({"calibration": {"attribute": "x"}}) == "linear",
           f"SP4aj (r3 P2-6) a Spec naming no engine has the documented default, as its raw mapping does ({ne})")
        bad(lambda r: r["comparator"].update(package="x"), "comparator", "SP5 an unknown comparator is refused")
        bad(lambda r: r["options"]["cost_items"].pop(), "no cost item",
            "SP6 a packaged component without a cost item is refused (toolkit/alloc)")
        bad(lambda r: r["constraints"].update(fixed_costs_usd={"f": 1e5}), "exceed the fixed costs",
            "SP7 an envelope below its fixed costs is refused")
        bad(lambda r: r["uncertainty"].update(excluded=[{"kind": "costs", "reason": "toy"}]), "neither declared",
            "SP8 lesson 6: an uncertainty source neither declared nor excluded with a reason is refused")
        # F6 S1-4: a scenario engine parameter needs a declared range, or a named exclusion with a reason
        def without_synergy(r):
            r["uncertainty"]["excluded"] = [x for x in r["uncertainty"].get("excluded") or []
                                            if x.get("parameter") != "synergy"]
            r["uncertainty"].pop("priors", None)
        r_s = copy.deepcopy(s.raw)
        without_synergy(r_s)
        e_s = validate(Spec(r_s, d))
        ok(any("scenario_without_range" in e and "synergy" in e for e in e_s),
           f"SP-S14a a scenario parameter with no range and no exclusion is refused: {e_s[:2]}")
        r_s["uncertainty"]["excluded"].append({"parameter": "synergy", "reason": "toy: no interaction assumed"})
        e_s = validate(Spec(r_s, d))
        ok(not [e for e in e_s if "synergy" in e], f"SP-S14b excluded by name with a reason: accepted ({e_s[:2]})")
        r_s["uncertainty"]["excluded"][-1]["reason"] = " "
        ok(any("synergy" in e and "reason" in e for e in validate(Spec(r_s, d))),
           "SP-S14c an exclusion without a reason is refused")
        r_p = copy.deepcopy(s.raw)
        without_synergy(r_p)
        r_p["uncertainty"]["priors"] = {"synergy": {"dist": "uniform", "range": [-0.05, 0.1], "source": "toy range"}}
        e_p = validate(Spec(r_p, d))
        ok(not [e for e in e_p if "synergy" in e], f"SP-S14d a declared range: accepted ({e_p[:2]})")
        # F6 S1-1: the affordability rule is part of the signed spec; malformed rules are refused
        bad(lambda r: r["uncertainty"].update(affordability={"max_share_over_budget": 1.5}),
            "uncertainty.affordability", "SP8c an affordability share outside [0, 1) is refused")
        bad(lambda r: r["uncertainty"].update(affordability={"max_share": 0.1}), "uncertainty.affordability",
            "SP8d an affordability rule with an unknown key is refused")
        bad(lambda r: r["uncertainty"].update(affordability=[0.1]), "uncertainty.affordability",
            "SP8e an affordability rule that is not a mapping is refused")
        r_ok = copy.deepcopy(s.raw)
        r_ok["uncertainty"]["affordability"] = {"max_share_over_budget": 0.1}
        ok(not [e for e in validate(Spec(r_ok, d)) if "affordability" in e],
           "SP8f a well-formed affordability rule validates")
        # F6 S1-1b: `enforce` asks the analysis for a plan that meets the rule (a budget reserve)
        bad(lambda r: r["uncertainty"].update(affordability={"enforce": True}), "uncertainty.affordability",
            "SP8j enforce without max_share_over_budget is refused")
        bad(lambda r: r["uncertainty"].update(affordability={"max_share_over_budget": 0.1, "enforce": "yes"}),
            "uncertainty.affordability.enforce", "SP8k a non-boolean enforce is refused")
        for en in (True, False):
            r_ok["uncertainty"]["affordability"] = {"max_share_over_budget": 0.1, "enforce": en}
            ok(not [e for e in validate(Spec(r_ok, d)) if "affordability" in e],
               f"SP8l an affordability rule with enforce: {en} validates")
        # a list column whose empty list is written as a declared token (a blank cell is refused as
        # attribute_column_empty, so an empty list needs an explicit marker)
        ea = {"yrs": {"column": "yrs", "split": " ", "empty": "none"}}
        sx = Spec({"decision_units": {"engine_attributes": ea}}, d)
        try:
            got = [sx.unit_attributes({"yrs": v})["yrs"] for v in ("none", "2019 2020", " none ")]
            ok(got == [[], [2019.0, 2020.0], []], f"SP8b a split column's declared empty token gives []: {got}")
        except Exception as e:  # noqa: BLE001 -- a missing feature is a failing case
            ok(False, f"SP8b a split column's declared empty token gives [] ({type(e).__name__}: {e})")
        bad(lambda r: r["decision_units"]["engine_attributes"].update(x={"column": "cov", "empty": "none"}),
            "empty", "SP8c `empty` without `split` is refused")
        bad(lambda r: r["decision_units"]["engine_attributes"].update(x={"column": "cov", "split": " ", "empty": " "}),
            "empty", "SP8d a blank `empty` token is refused")
        bad(lambda r: r["decision_units"]["engine_attributes"].update(x={"column": "cov", "split": " ", "empty": None}),
            "empty", "SP8e a null `empty` token is refused (Codex r1 P1-4)")
        bad(lambda r: r["decision_units"]["engine_attributes"].update(x={"column": "cov", "split": " ", "empty": -1}),
            "empty", "SP8f a non-string `empty` token is refused")
        bad(lambda r: r["decision_units"]["engine_attributes"].update(x={"column": "cov", "split": " ", "empty": "-1"}),
            "empty", "SP8g a numeric `empty` token is refused (a reader could plot it as data; Codex r1 P2-6)")
        bad(lambda r: r["decision_units"]["engine_attributes"].update(x={"column": "cov", "split": " ",
                                                                         "empty": "none 2020"}),
            "empty", "SP8h an `empty` token with a numeric piece after the split is refused (Codex r2 P2)")
        bad(lambda r: r["uncertainty"]["excluded"][0].update(reason=""), "neither declared",
            "SP9 an exclusion without a reason is refused")
        bad(lambda r: r["constraints"].update(group_rules=[{"id": "g", "components": ["a"], "by": "nocol"}]),
            "not a column", "SP10 lesson 10: a group rule on an unknown column is refused")
        bad(lambda r: r["analysis"]["decision_tests"].append({"id": "DT2", "type": "magic", "text": "t"}),
            "not in", "SP11 an unknown decision-test type is refused")
        bad(lambda r: r["analysis"]["decision_tests"].append({"id": "prior_range_eff_a", "type": "parameter_value",
                                                               "parameter": "eff_a", "values": [0.3], "text": "t"}),
            "generated by the harness", "SP11b the spec cannot declare (or shadow) an automatic prior_range test")
        bad(lambda r: r["analysis"]["decision_tests"].append({"id": "anchor_range_N0", "type": "envelope",
                                                               "values": [9000.0], "text": "t"}),
            "generated by the harness", "SP11c (F6 S2-5d review r3) the spec cannot shadow an automatic anchor_range test")
        bad(lambda r: (r["uncertainty"]["sources"].append({"kind": "burden_anchor"}),
                       r["uncertainty"]["excluded"].append({"kind": "burden_anchor", "reason": "x"})),
            "burden_anchor is both a source and excluded", "SP9b (F6 S2-5d review r3) burden_anchor cannot be both drawn "
            "and excluded")
        # F6 cost amendment: a price scenario (several cost items multiplied together) as a decision test
        dtc = lambda **kw: (lambda r: r["analysis"]["decision_tests"].append(  # noqa: E731
            dict({"id": "DTc", "type": "cost_items", "text": "t"}, **kw)))
        bad(dtc(), "multipliers", "SP11g a cost_items decision test without multipliers is refused")
        bad(dtc(multipliers={"nope": 1.2}), "unknown cost item",
            "SP11h a cost_items decision test on an unknown cost item is refused")
        bad(dtc(multipliers={"ca": 0}), "positive", "SP11i a cost_items multiplier that is not positive is refused")
        r_c = copy.deepcopy(s.raw)
        dtc(multipliers={"ca": 0.8, "cb": 1.3})(r_c)
        ok(validate(Spec(r_c, d)) == [], f"SP11j a cost_items decision test on declared cost items validates: "
                                         f"{validate(Spec(r_c, d))[:3]}")
        # Retired type: perturb must be refused; canonical per-type decision tests validate directly
        dtp = lambda **kw: (lambda r: r["analysis"]["decision_tests"].append(  # noqa: E731
            dict({"id": "DTp", "type": "parameter_value", "text": "t", "parameter": "eff_a", "values": [0.3, 0.6]},
                 **kw)))
        r_p = copy.deepcopy(s.raw)
        dtc(multipliers={"ca": 0.8, "cb": 1.3})(r_p)
        dtp(id="DTq")(r_p)
        ok(validate(Spec(r_p, d)) == [], f"SPP1 direct decision tests (cost_items, parameter_value) validate: "
                                         f"{validate(Spec(r_p, d))[:3]}")
        bad(lambda r: r["analysis"]["decision_tests"].append({"id": "DTp", "type": "perturb", "text": "t",
                                                               "set": [{"target": "parameter", "name": "eff_a",
                                                                        "values": [0.3]}]}),
            "not in", "SPP2 retired type perturb is refused")
        bad(dtc(multipliers={"nope": 1.2}), "unknown cost item",
            "SPP3 direct cost_items test on an unknown cost item is refused")
        bad(dtp(parameter="nope", values=[1.0]), "not an engine parameter",
            "SPP4 direct parameter_value test with unknown parameter is refused")
        bad(dtp(id="prior_range_eff_a"), "generated by the harness",
            "SPP6 a decision test cannot shadow an automatic test")
        _self_test_unit_attribute(ok, s, d, bad)
        # F6 run 1, owner decision R1: a calibration-target test (the listed units' targets scaled, the calibration redone)
        dct = lambda **kw: (lambda r: r["analysis"]["decision_tests"].append(  # noqa: E731
            dict({"id": "DTt", "type": "calibration_target", "text": "t"}, **kw)))
        bad(dct(), "give calibration_target", "SP-CT1 a calibration_target test without its mapping is refused")
        bad(dct(calibration_target={"units": ["0", "zz"], "scale": 0.8}), "'zz'",
            "SP-CT2 a calibration_target test naming an unknown unit is refused")
        bad(dct(calibration_target={"units": ["5"], "scale": 1.5}), "inside (0, 1)",
            "SP-CT3 a scale that takes a target outside (0, 1) is refused")
        bad(dct(calibration_target={"units": ["0"], "scale": 1.0}), "changes no target",
            "SP-CT4 a scale of 1 (an identity) is refused")
        bad(dct(calibration_target={"units": [], "scale": 0.8}), "a non-empty list",
            "SP-CT5 an empty unit list is refused")
        bad(lambda r: (r["calibration"].update(method="none"),
                       dct(calibration_target={"units": ["0"], "scale": 0.8})(r)), "emulator_inversion",
            "SP-CT6 a calibration_target test without an emulator-inversion calibration is refused")
        # F6 run 2, owner decision (DT13): a package's burden set to a ratio of its counterpart's (one component swapped)
        dor = lambda **kw: (lambda r: r["analysis"]["decision_tests"].append(  # noqa: E731
            dict({"id": "DTo", "type": "outcome_ratio", "text": "t"}, **kw)))
        bad(dor(), "give outcome_ratio", "SP-OR1 an outcome_ratio test without its mapping is refused")
        bad(dor(outcome_ratio={"component": "zz", "reference": "b", "values": [0.8]}), "unknown component",
            "SP-OR2 an unknown component is refused")
        bad(dor(outcome_ratio={"component": "a", "reference": "a", "values": [0.8]}), "differ",
            "SP-OR3 a component tested against itself is refused")
        bad(dor(outcome_ratio={"component": "a", "reference": "b", "values": [0.0]}), "positive",
            "SP-OR4 a ratio that is not positive is refused")
        bad(dor(outcome_ratio={"component": "a", "reference": "b", "values": [1.0]}), "identity",
            "SP-OR5 a ratio of 1 only (an identity) is refused")      # #251: allowed only for an adjusted component
        bad(lambda r: (r["comparator"].update(package="a"),
                       dor(outcome_ratio={"component": "a", "reference": "b", "values": [0.5]})(r)), "comparator",
            "SP-OR7 (Codex review of #245 P2-1) a test that changes the comparator's own burden is refused")
        r_o = copy.deepcopy(s.raw)
        dor(outcome_ratio={"component": "a", "reference": "b", "values": [0.74, 0.84]})(r_o)
        ok(validate(Spec(r_o, d)) == [], f"SP-OR6 a well-formed outcome_ratio test validates: {validate(Spec(r_o, d))[:3]}")
        # F6 run 4 R3 (RIG-002): the boundary check's engine tables in place of the emulator's, re-solved
        dbe = lambda **kw: (lambda r: r["analysis"]["decision_tests"].append(  # noqa: E731
            dict({"id": "DTbe", "type": "boundary_engine", "text": "t"}, **kw)))
        bad(dbe(units=["0"]), "boundary_engine takes no", "SP-BE1 a boundary_engine test takes no parameters")
        bad(lambda r: (r["analysis"].update(evaluation="direct"), dbe()(r)), "emulator",
            "SP-BE2 a boundary_engine test needs the emulator path (the boundary check runs only there)")
        r_be = copy.deepcopy(s.raw)
        dbe()(r_be)
        ok(validate(Spec(r_be, d)) == [], f"SP-BE3 a boundary_engine test validates: {validate(Spec(r_be, d))[:3]}")
        _self_test_burden_scale(ok, s, d, bad)
        _self_test_outcome_adjustments(ok, s, d, bad)
        # external review of v2 (owner R3, point a): a signed analysis field the code does not read is refused
        bad(lambda r: r["analysis"].update(outcome_adjustmnts=[]), "unknown key",
            "SP-AK1 an unknown analysis key is refused (a misspelt signed field would otherwise be a no-op)")
        r_t = copy.deepcopy(s.raw)
        dct(calibration_target={"units": ["0", "3"], "scale": 0.8})(r_t)
        ok(validate(Spec(r_t, d)) == [], f"SP-CT7 a well-formed calibration_target test validates: "
                                         f"{validate(Spec(r_t, d))[:3]}")
        bad(lambda r: r["analysis"].update(regret_threshold=2), "analysis.regret_threshold",
            "SP11c F6 S1-5: a regret threshold outside [0, 1] is refused")
        bad(lambda r: r["analysis"].update(dollars_threshold="x"), "analysis.dollars_threshold",
            "SP11d F6 S1-5: a dollars-moved threshold that is not a fraction is refused")
        bad(lambda r: r["analysis"].update(plan_variants={"variants": [
            {"id": "v", "type": "group_rule", "components": ["a"], "by": "nocol", "text": "one per group"}]}),
            "by 'nocol'", "SP11e F6 S1-6: a plan variant on an unknown column is refused")
        bad(lambda r: r["analysis"].update(plan_variants={"variants": [
            {"id": "v", "type": "minimum_service", "text": "a service everywhere"}], "headline": "w"}),
            "headline", "SP11f F6 S1-6: a headline that names no variant is refused")
        bad(lambda r: r["objective"].update(outcome="nope"), "per-capita output", "SP12 an unknown objective outcome")
        # owner 2026-11-02: display labels are not part of the signed spec (an unsigned presentation.yaml instead)
        bad(lambda r: r.update(presentation={"labels": {"components": {"a": "Service A"}}}), "presentation.yaml",
            "SP-P1 a presentation block in the signed spec is refused, with a pointer to the presentation file")
        # Codex r2 P1-3: the refusal also holds for engine: auto before the choice (the sign-off path)
        r_pend = copy.deepcopy(s.raw)
        r_pend["engine"] = {"auto": True, "candidates": ["toolkit.engines.contract:ToyEngine"]}
        r_pend["presentation"] = {"labels": {"components": {"a": "Service A"}}}
        sp_pend = Spec(r_pend, d)
        errs_pend = validate(sp_pend, pending_ok=True)
        ok(sp_pend.engine_pending and any("presentation.yaml" in e for e in errs_pend),
           f"SP-P5 a presentation block is refused at sign-off too (engine pending): {errs_pend[:3]}")
        eng = s.engine()
        pn, card = {p.name for p in eng.parameters()}, eng.describe()
        lab_ok = {"components": {"a": "Service A"}, "parameters": {"eff_a": "Effect of A"}, "outcomes": {"dalys": "DALYs"},
                  "metrics": {"prev": "prevalence"}, "units_title": "districts"}
        ok(labels_problems(s.raw, lab_ok, pn, card) == [], f"SP-P1b valid labels: {labels_problems(s.raw, lab_ok, pn, card)}")
        for lab, needle, label in (({"components": {"zz": "Z"}}, "labels.components", "SP-P2 a label for an unknown component"),
                                   ({"colours": {}}, "unknown key", "SP-P3 an unknown labels key"),
                                   ({"outcomes": {"dalys": ""}}, "non-empty display name", "SP-P4 an empty display name")):
            got = labels_problems(s.raw, lab, pn, card)
            ok(any(needle in e for e in got), f"{label} is refused: {got}")
        bad(lambda r: r.pop("deliverable"), "missing section deliverable", "SP13 a missing section is refused")
        bad(lambda r: r["objective"].update(delivery=[0, 4]), "objective.delivery",
            "SP-D1 a delivery period outside the decision window is refused")
        bad(lambda r: r["objective"].update(delivery="2024-2027"), "objective.delivery",
            "SP-D1b a delivery period that is not [t0, t1] with t0 < t1 is refused")
        bad(lambda r: r["objective"].update(delivery=[0, 2]), "does not honour",
            "SP-D2 a delivery period for an engine that does not declare supports_delivery is refused (fail closed)")
        from toolkit.engines.contract import ToyEngine
        r = copy.deepcopy(s.raw)
        r["objective"]["delivery"] = [0, 2]
        ToyEngine.supports_delivery = True
        try:
            sd = Spec(r, d)
            errs = validate(sd)
            ok(errs == [] and getattr(sd.horizon(), "delivery", None) == (0.0, 2.0)
               and getattr(s.horizon(), "delivery", None) is None and tuple(sd.horizon().window) == (0.0, 3.0),
               f"SP-D3 a delivery period inside the window validates and reaches the engine horizon: {errs[:3]}")
        finally:
            del ToyEngine.supports_delivery
        bad(lambda r: r["uncertainty"]["sources"].append({"kind": "emulator"}) or r["uncertainty"].update(excluded=[
            {"kind": "costs", "reason": "toy"}]) or r["analysis"].update(evaluation="direct"),
            "there is no emulator", "SP14 an emulator source on the direct path is refused")
        bad(lambda r: r["calibration"].update(method="likelihood", parameters=["eff_a"], targets={}), "not calibrated",
            "SP15 likelihood calibration of a parameter the engine card does not mark calibrated is refused")
        r = copy.deepcopy(s.raw)
        r["constraints"]["coverage"] = {"b": {"max_units_share": 0.25}}
        r["constraints"]["group_rules"] = [{"id": "camp", "components": ["a"], "by": "region", "rule": "same_choice"}]
        s2 = Spec(r, d)
        a = s2.alloc_spec()
        ok(validate(s2) == [] and a["caps"]["b"]["max_units"] == 3.0 and "camp" in a["group_rules"],
           f"SP16 coverage becomes a toolkit/alloc cap and group rules pass through: {validate(s2)}")
        sc = s.uncertainty_scope()
        ok(sc["label"] == "95% UI over engine parameters, calibration; held fixed: synergy and 3 other engine parameters; "
                         "not covered: costs, emulator", f"SP17 the interval label states what is covered and what is not: {sc['label']}")
        ok(validate({"schema_version": 1}) and validate(Spec({"engine": 3}, d)), "SP18 malformed specs fail closed")
        r = copy.deepcopy(s.raw)
        draws = r["uncertainty"].pop("draws")
        pts = r["analysis"]["emulator"].pop("points")
        with open(p, "w") as f:
            yaml.safe_dump(r, f)
        with open(os.path.join(d, SETTINGS), "w") as f:
            yaml.safe_dump({"uncertainty": {"draws": draws}, "analysis": {"emulator": {"points": pts}}}, f)
        s3 = load(p)
        ok(validate(s3) == [] and s3["uncertainty"]["draws"] == draws and s3["analysis"]["emulator"]["points"] == pts
           and s3.settings_sha256, f"SP19 computational settings (draws, design size) can live in {SETTINGS}")
        ok(s3.path == p and s3.sha256 == sha256_file(p),
           f"SP19b loading with a settings file keeps the spec's own path (Codex r2 P1-1): {s3.path}")
        for bad_set, label in (({"objective": {"outcome": "cases"}}, "a decision section"),
                               ({"calibration": {"range": [-9, 9]}}, "a calibration setting"),
                               ({"analysis": {"decision_tests": []}}, "the decision tests"),
                               ({"uncertainty": {"sources": []}}, "the uncertainty sources"),
                               ({"analysis": {"cost_outputs": {}}}, "a cost mapping"),
                               ({"uncertainty": {"draws": 9}, "analysis": {"emulator": {"points": 9, "seed": 1}}}
                                if False else {"analysis": {"emulator": {"holdout_share": 0.5}}}, "the hold-out share")):
            with open(os.path.join(d, SETTINGS), "w") as f:
                yaml.safe_dump(bad_set, f)
            try:
                load(p)
                ok(False, f"SP20 {label} in the settings file is refused (Codex P1-1)")
            except SpecError:
                ok(True, f"SP20 {label} in the settings file is refused (Codex P1-1)")
        with open(os.path.join(d, SETTINGS), "w") as f:
            yaml.safe_dump({"uncertainty": {"draws": draws, "seed": 3}}, f)
        r2 = copy.deepcopy(r)
        r2["uncertainty"]["draws"] = draws
        with open(p, "w") as f:
            yaml.safe_dump(r2, f)
        try:
            load(p)
            ok(False, "SP21 a leaf in both files is refused")
        except SpecError:
            ok(True, "SP21 a leaf in both files is refused")
        os.remove(os.path.join(d, SETTINGS))
        r2["analysis"]["emulator"]["points"] = pts
        with open(p, "w") as f:
            yaml.safe_dump(r2, f)
        r = copy.deepcopy(s.raw)
        r["constraints"]["caps"] = {"b": {"max_units": 2}}
        r["constraints"]["coverage"] = {"b": {"max_units_share": 0.5}}
        ok(Spec(r, d).alloc_spec()["caps"]["b"]["max_units"] == 2,
           "SP22 a max_units cap and a coverage share: the binding (smaller) limit is kept (Codex P1-2)")
        r["constraints"]["caps"] = {"b": {"max_units": 9}}
        ok(Spec(r, d).alloc_spec()["caps"]["b"]["max_units"] == 6, "SP22b ... whichever is smaller")
        r = copy.deepcopy(s.raw)
        r["analysis"]["decision_tests"][0]["engine_arm_or"] = {"a11": 0.4, "a10": 0.5, "a01": 0.7}
        ok(any("cannot be supplied" in e for e in validate(Spec(r, d))),
           "SP24 arm ratios supplied in the spec are refused (Codex P1-13)")
    with tempfile.TemporaryDirectory() as d:     # engine: auto (the engine is the AI's choice, made after sign-off)
        p = toy_spec(d)
        raw = yaml.safe_load(open(p))
        toy = raw["engine"]
        raw["engine"] = {"auto": True, "candidates": ["toolkit.engines.contract:ToyEngine"]}
        yaml.safe_dump(raw, open(p, "w"), sort_keys=False)
        try:
            s = load(p)
            errs, pend = validate(s), validate(s, pending_ok=True)
            ok(s.engine_pending and any(ENGINE_CHOICE in e for e in errs) and pend == [],
               f"SP25 engine auto before the choice: the decision sections validate, the engine-dependent ones wait: "
               f"{errs[:2]} {pend[:2]}")
        except Exception as e:  # noqa: BLE001
            ok(False, f"SP25 engine auto before the choice validates its decision sections: {type(e).__name__}: {e}")
        cp = os.path.join(d, ENGINE_CHOICE) if hasattr(schema, "ENGINE_CHOICE") else os.path.join(d, "engine_choice.json")
        import json
        json.dump({"engine": toy, "why": "the only eligible engine", "candidates": [
            {"ref": toy["ref"], "eligible": True, "why_not": ""}], "cross_check": {}}, open(cp, "w"))
        try:
            s = load(p)
            ok(s["engine"] == toy and validate(s) == [] and s.engine_choice_sha256 == sha256_file(cp),
               "SP26 engine auto: the engine comes from engine_choice.json and the spec validates in full")
        except Exception as e:  # noqa: BLE001
            ok(False, f"SP26 engine auto reads engine_choice.json: {type(e).__name__}: {e}")
        json.dump({"engine": {"ref": "toolkit.engines.empirical.engine:PackageRateEngine"}, "why": "x"}, open(cp, "w"))
        try:
            load(p)
            ok(False, "SP27 a chosen engine outside the signed candidates is refused")
        except Exception as e:  # noqa: BLE001
            ok(isinstance(e, SpecError) and "candidate" in str(e), f"SP27 a chosen engine outside the signed candidates "
                                                                   f"is refused: {e}")
        raw["engine"] = toy
        yaml.safe_dump(raw, open(p, "w"), sort_keys=False)
        s = load(p)
        ok(s["engine"] == toy and getattr(s, "engine_choice_path", None) is None,
           "SP28 an explicit engine in the signed spec wins; engine_choice.json is ignored")
    try:
        _self_test_derived(ok)
    except Exception as e:  # noqa: BLE001 -- before the checks exist (step α) this is the failure
        ok(False, f"DI spec checks: {type(e).__name__}: {e}")
    _self_test_engine_refs(ok)
    try:
        _self_test_registry(ok)
    except Exception as e:  # noqa: BLE001 -- a crash in these cases is a failure, not a skip
        ok(False, f"engine registry cases could not run: {type(e).__name__}: {e}")
    _self_test_joint_packages(ok)
    try:
        _self_test_plan_rule(ok)
    except Exception as e:  # noqa: BLE001 -- before the rule exists (step α) this is the failure
        ok(False, f"SPEV deliverable.plan checks: {type(e).__name__}: {e}")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def _self_test_engine_refs(ok) -> None:
    """SP-RW1..4 (run console review round 2, P1-1): a spec names its engine by "module:Class"; the spec is drafted by
    an agent, so a reference outside the registered engines is refused before any import or instantiation."""
    import tempfile
    seen, real = [], importlib.import_module

    def spy(name, *a, **k):
        seen.append(name)
        return real(name, *a, **k)
    with tempfile.TemporaryDirectory() as d:
        marker = os.path.join(d, "ran")
        importlib.import_module = spy
        try:
            refused = []
            for ref, cfg in (("subprocess:Popen", {"args": ["touch", marker]}), ("os:system", {}),
                             ("json.decoder:JSONDecoder", {}), ("toolkit.alloc.costs:Nope", {})):
                try:
                    load_engine({"ref": ref, "config": cfg})
                    refused.append(False)
                except (SpecError, ValueError):
                    refused.append(True)
                except Exception:  # noqa: BLE001 -- imported and failed later: not refused up front
                    refused.append(False)
        finally:
            importlib.import_module = real
        bad = [m for m in seen if m.split(".")[0] in ("subprocess", "os", "json") or m.startswith("toolkit.alloc")]
        ok(all(refused) and not bad and not os.path.exists(marker),
           f"SP-RW1 subprocess:Popen, os:system and modules outside toolkit.engines are refused without an import "
           f"({refused}; imported {bad})")
        p = toy_spec(d)
        raw = yaml.safe_load(open(p))
        raw["engine"] = {"ref": "subprocess:Popen", "config": {"args": ["touch", marker]}}
        yaml.safe_dump(raw, open(p, "w"))
        probs = validate(load(p))
        ok(probs and not os.path.exists(marker) and any("engine" in x for x in probs),
           f"SP-RW2 validating a spec with such an engine reports it and runs nothing ({probs[:1]})")
        raw["engine"] = {"auto": True, "candidates": ["os:system", "toolkit.engines.contract:ToyEngine"]}
        yaml.safe_dump(raw, open(p, "w"))
        try:
            load(p)
            ok(False, "SP-RW3 an unregistered engine among the signed candidates is refused")
        except SpecError as e:
            ok("os:system" in str(e), f"SP-RW3 an unregistered engine among the signed candidates is refused ({e})")
    # SP-RW5/6 (round 3, P1-1): the binding lookup and the legacy spike loader are guarded too
    seen = []
    importlib.import_module = spy
    try:
        refused = []
        for fn in (lambda: binding_for_ref("os:system"), lambda: binding_for_ref("subprocess:Popen")):
            try:
                fn()
                refused.append(False)
            except (SpecError, ValueError):
                refused.append(True)
            except Exception:  # noqa: BLE001
                refused.append(False)
        try:
            from spike.analysis import spec as _spike
            _spike.load_engine("os:system")
            refused.append(False)
        except (SpecError, ValueError):
            refused.append(True)
        except Exception:  # noqa: BLE001
            refused.append(False)
    finally:
        importlib.import_module = real
    bad = [m for m in seen if m.split(".")[0] in ("subprocess", "os")]
    ok(all(refused) and not bad, f"SP-RW5 binding_for_ref and the spike loader refuse os:system and subprocess:Popen "
                                 f"before an import ({refused}; imported {bad})")
    try:
        k = engine_class("toolkit.engines.contract:ToyEngine")
        ok(k.__name__ == "ToyEngine" and load_engine("toolkit.engines.contract:ToyEngine") is not None,
           "SP-RW4 a registered engine still loads")
    except Exception as e:  # noqa: BLE001
        ok(False, f"SP-RW4 a registered engine still loads: {type(e).__name__}: {e}")


def _self_test_registry(ok) -> None:
    """ER1-ER6 (PR #363 question 15): a spec names only a registered engine (agents/analysis_backend.py ENGINES), at
    sign-off and when the engine is built; self-tests extend the registry in code only, never from the spec."""
    import json
    import subprocess
    import tempfile
    from toolkit import import_guard as G
    hook = "toolkit.engines.hooks:ToyScheduleEngine"      # allowed by the import policy, not registered
    reg = registered_engines()
    with tempfile.TemporaryDirectory() as d:
        p = toy_spec(d)
        raw = yaml.safe_load(open(p))
        raw["engine"] = {"ref": hook}
        yaml.safe_dump(raw, open(p, "w"))
        probs = validate(load(p))
        try:
            load(p).engine()
            built = "built"
        except SpecError as e:
            built = str(e)
        ok(any("not a registered engine" in x and all(r in x for r in reg) for x in probs[:1])
           and "not a registered engine" in built,
           f"ER1 a spec naming an unregistered engine is refused by validate (first problem, naming the registered "
           f"engines) and when the engine is built ({probs[:1]}; {built[:80]})")
        raw["engine"] = {"auto": True, "candidates": [hook]}
        yaml.safe_dump(raw, open(p, "w"))
        try:
            load(p)
            cand = "loaded"
        except SpecError as e:
            cand = str(e)
        raw["engine"] = "auto"
        yaml.safe_dump(raw, open(p, "w"))
        json.dump({"engine": {"ref": hook}}, open(os.path.join(d, ENGINE_CHOICE), "w"))
        try:
            load(p)
            choice = "loaded"
        except SpecError as e:
            choice = str(e)
        ok("not a registered engine" in cand and "not a registered engine" in choice,
           f"ER2 an unregistered engine: auto candidate and an engine choice of one are refused ({cand[:80]}; "
           f"{choice[:80]})")
        os.remove(os.path.join(d, ENGINE_CHOICE))
        raw["engine"] = {"ref": reg[0]}
        yaml.safe_dump(raw, open(p, "w"))
        er3 = [x for x in validate(load(p)) if "registered engine" in x]
        passes = []
        for r in reg:
            try:
                check_engine_ref(r)
                passes.append(True)
            except SpecError:
                passes.append(False)
        ok(all(passes) and not er3, f"ER3 every registered engine passes the check, and a spec naming one has no "
                                    f"registry problem ({passes}; {er3[:1]})")
        # ER4: no spec field opts in (the registry is read from code only)
        raw["engine"] = {"ref": hook, "registered": True, "test": True, "test_only": True}
        raw["test_engines"] = [hook]
        raw["register_test_engine"] = hook
        yaml.safe_dump(raw, open(p, "w"))
        probs4 = validate(load(p))
        ok(any("not a registered engine" in x for x in probs4) and not G.test_engine_registered(hook),
           f"ER4 no spec field registers an engine ({probs4[:1]})")
    # ER5: no environment variable does either: a fresh process with test-looking variables set refuses the ref
    code = ("import sys; sys.path.insert(0, %r)\n"
            "from toolkit.spec import schema\n"
            "try:\n"
            "    schema.check_engine_ref(%r); print('allowed')\n"
            "except schema.SpecError:\n"
            "    print('refused')") % (ROOT, hook)
    env = dict(os.environ, MA_TEST_ENGINES=hook, MA_IMPORT_GUARD_TEST_REFS=json.dumps([f"engine|{hook}"]))
    res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, cwd=ROOT)
    ok(res.stdout.strip() == "refused", f"ER5 the environment registers no engine ({res.stdout.strip() or res.stderr[-200:]})")
    # ER6: the test-only path: code in a self-test registers an engine the import policy allows, nothing else
    try:
        G.register_test_engine("os:system")
        bad = "registered"
    except G.ImportRefused:
        bad = "refused"
    G.register_test_engine(hook)
    try:
        check_engine_ref(hook)
        er6 = "allowed"
    except SpecError as e:
        er6 = str(e)
    finally:
        G._TEST_ENGINES.discard(hook)
    ok(bad == "refused" and er6 == "allowed",
       f"ER6 a self-test registers an engine in-process (allowed: {er6}); a ref outside the import policy cannot be "
       f"registered ({bad})")


def _self_test_derived(ok) -> None:
    """DI1, DI7, DI11, DI12 and DI13 (docs/design/v2-derived-inputs.md §4)."""
    import tempfile
    sys.path.insert(0, os.path.join(ROOT, "scripts"))

    def has(errs, code, needle=""):
        return any(e.startswith(code) and needle in e for e in errs)

    with tempfile.TemporaryDirectory() as d:          # DI1: the units table written by the task, not derived
        p = toy_spec(d)
        errs = validate(load(p), derived=True)
        ok(has(errs, "units_not_derived"), f"DI1 a units table written by the task (no derivation) gives "
                                           f"units_not_derived: {errs[:3]}")
        ok(validate(load(p)) == [], "DI12a the same spec outside a v2 machine run (no derivation declared) validates "
                                    "unchanged")
    with tempfile.TemporaryDirectory() as d:
        p = derived_toy_spec(d)
        s = load(p)
        ok(validate(s) == [], f"DI7a the derived toy spec (one default accepted by name) validates: {validate(s)[:3]}")

        def errs_of(mut):
            r = copy.deepcopy(s.raw)
            mut(r)
            return validate(Spec(r, d, p))
        e = errs_of(lambda r: r["decision_units"].pop("defaults_accepted"))
        ok(has(e, "engine_attribute_unbound", "group"), f"DI7b an attribute with a card default, neither bound nor "
                                                         f"accepted, gives engine_attribute_unbound: {e[:3]}")
        e = errs_of(lambda r: r["calibration"].update(attribute="group"))
        ok(has(e, "engine_attribute_unbound", "x"), f"DI7c a required attribute left unbound gives "
                                                     f"engine_attribute_unbound: {e[:3]}")
        e = errs_of(lambda r: (r["calibration"].update(attribute="group"),
                               r["decision_units"]["defaults_accepted"].update(x="accept the engine's value")))
        ok(has(e, "engine_attribute_unbound", "x"), f"DI7d a required attribute cannot be accepted as a default: {e[:3]}")
        e = errs_of(lambda r: r["decision_units"]["engine_attributes"].update(group={"column": "lbl"}))
        ok(has(e, "attribute_column_empty", "lbl"), f"DI7e a bound column blank in a row gives attribute_column_empty: "
                                                     f"{e[:3]}")
        e = errs_of(lambda r: r["calibration"].update(target_frame={"band": "prev_young", "source": "a survey"}))
        ok(has(e, "target_frame_unmapped", "prev_young"), f"DI11a a target whose frame band differs from the engine "
                                                           f"output's, with no mapping, gives target_frame_unmapped: {e[:3]}")
        e = errs_of(lambda r: r["calibration"].update(target_frame={
            "band": "prev_young", "source": "a survey",
            "mapping": {"to": "prev", "method": "age-band conversion", "source": "a published conversion"}}))
        ok(not has(e, "target_frame_unmapped"), f"DI11b with a declared mapping it passes: {e[:3]}")
        e = errs_of(lambda r: r["calibration"].pop("target_frame"))
        ok(has(e, "target_frame_unmapped"), f"DI11c a target with no declared frame gives target_frame_unmapped: {e[:3]}")
        os.remove(os.path.join(d, "data", "derivations.yaml"))
        e = validate(load(p), derived=True)
        ok(has(e, "units_not_derived") or has(e, "data_ref_not_derived"),
           f"DI1b a spec citing data/derived/ with no declaration is refused: {e[:3]}")
    # card prior and limitation of a calibrated unit attribute (owner decision on #146: a single latent multiplier
    # keeps a sourced card range, and the results say what it simplifies)
    with tempfile.TemporaryDirectory() as d:
        try:
            from toolkit.engines.compartmental.engine import CompartmentalEngine
            b, bad = load_binding(CompartmentalEngine)
            cs = b.get("coverage_scale") or {}
            pr = cs.get("prior") or {}
            ok(bad == [] and len(pr.get("range") or []) == 2 and pr["range"][0] < 1.0 <= pr["range"][1]
               and str(pr.get("source") or "").strip() and str(cs.get("limitation") or "").strip(),
               f"AB1 the compartmental card gives coverage_scale a sourced prior range around its default and a "
               f"limitation ({bad}, {pr})")
        except ImportError as e:
            print(f"SKIP AB1: {e}")
        bp = os.path.join(d, "b.yaml")
        for case, v, want in (("no source", {"default": 1.0, "meaning": "m", "prior": {"dist": "uniform",
                                                                                        "range": [0.5, 1.1]}}, "source"),
                              ("lo >= hi", {"default": 1.0, "meaning": "m", "prior": {"dist": "uniform", "range": [1.1, 0.5],
                                                                                       "source": "s"}}, "range"),
                              ("unknown dist", {"default": 1.0, "meaning": "m", "prior": {"dist": "normal", "range": [0, 1],
                                                                                           "source": "s"}}, "dist"),
                              ("blank limitation", {"default": 1.0, "meaning": "m", "limitation": " "}, "limitation")):
            yaml.safe_dump({"attribute_binding": {"a": v}}, open(bp, "w"))
            got, bad = _read_binding(bp)
            ok("a" not in got and any(want in x for x in bad), f"AB2 a card prior or limitation with {case} is refused: "
                                                               f"{bad}")
        b = {"a": {"default": 1.0, "meaning": "m", "limitation": "a stand-in for X",
                   "prior": {"dist": "uniform", "range": [0.5, 1.1], "source": "S"}}, "c": "required"}
        notes = attribute_notes({"method": "posterior", "attribute": "a",
                                 "priors": {"a": {"dist": "uniform", "range": [0.6, 1.2], "source": "planner"}}}, b)
        md = "\n".join(attribute_notes_markdown(notes))
        ok(len(notes) == 1 and "a stand-in for X" in md and "0.5" in md and "S" in md and "0.6" in md and "planner" in md,
           f"AB3 a calibrated attribute with a card limitation is listed with the card prior and the run's prior: {md}")
        n_log = attribute_notes({"method": "posterior", "attribute": "a", "scale": "log",
                                 "priors": {"a": {"dist": "uniform", "range": [-0.693, 0.0953], "source": "p"}}}, b)
        n_beta = attribute_notes({"method": "posterior", "attribute": "a",
                                  "priors": {"a": {"dist": "beta", "a": 2.0, "b": 5.0, "source": "p"}}}, b)
        n_res = attribute_notes({"method": "posterior", "attribute": "a",
                                 "priors": {"a": {"dist": "uniform", "range": [-0.693, 0.0953], "source": "p"}}}, b,
                                scale="log")
        ok(n_res and n_res[0]["scale"] == "log",
           "AB3c an undeclared scale is shown as the spec resolved it (the engine card's), not read as linear")
        md_log, md_beta = "\n".join(attribute_notes_markdown(n_log)), "\n".join(attribute_notes_markdown(n_beta))
        ok("log scale" in md_log and "exp" in md_log and "a=2" in md_beta and "None" not in md_beta,
           f"AB3b (Codex r3 P2-3) the run's prior is shown on its declared scale, and a beta prior by its shape "
           f"parameters: {md_log} | {md_beta}")
        md_bad = "\n".join(attribute_notes_markdown([], problems=["engine_attribute_unbound: x: unreadable"]))
        ok("could not be read" in md_bad and "unreadable" in md_bad,
           f"AB5 (Codex r3 P2-2) an unreadable card binding is stated in the results, not dropped: {md_bad}")
        ok(attribute_notes({"method": "none", "attribute": "a"}, b) == []
           and attribute_notes({"method": "posterior", "attribute": "c"}, b) == []
           and attribute_notes_markdown([]) == [],
           "AB4 nothing is listed when the attribute is not calibrated or its card gives no limitation")
    # DI12: contract.py and the engines' code are unchanged (the fingerprints still match the benchmark stamps).
    from toolkit.engines.contract import code_fingerprint
    for mod, cls, res in (("toolkit.engines.malaria.engine", "MalariaEngine", "toolkit/engines/malaria/benchmarks/results.yaml"),
                          ("toolkit.engines.compartmental.engine", "CompartmentalEngine",
                           "toolkit/engines/compartmental/benchmarks/results.yaml")):
        try:
            klass = getattr(importlib.import_module(mod), cls)
            stamp = (yaml.safe_load(open(os.path.join(ROOT, res))) or {})["engine_identity"]["code_sha256"]
            ok(code_fingerprint(klass.__new__(klass)) == stamp,
               f"DI12b {cls}: the engine code fingerprint (with contract.py) still matches its benchmark stamp")
        except ImportError as e:
            print(f"SKIP DI12b {cls}: {e}")
    try:
        import pipeline as P
        src = "".join(inspect_source(f) for f in (attribute_problems, frame_problems, load_binding))
        ok(not P.domain_terms(src), f"DI13 the spec checks name no disease, place or intervention: {P.domain_terms(src)}")
    except Exception as e:  # noqa: BLE001
        ok(False, f"DI13 spec checks: {type(e).__name__}: {e}")


def inspect_source(fn) -> str:
    import inspect
    return inspect.getsource(fn)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
