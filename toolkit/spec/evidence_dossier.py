#!/usr/bin/env python3
"""Evidence dossier: a precondition of the owner's sign-off (F6, owner decision 2026-11-12).

    python3 toolkit/spec/evidence_dossier.py --self-test
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _p in (ROOT, os.path.join(ROOT, "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

TOY = "toolkit.spec.toy_engines:ToyDossierEngine"


def toy_entries() -> dict:
    """A complete dossier for the toy dossier engine's component a: one input observed, one with no data."""
    return {
        "parameter:eff_a": {
            "model_value": 0.5, "model_source": "engine card (fitted to toy benchmark T1)",
            "observations": [{"source": "Toy trial 2020", "where": "Table 2", "year": 2020, "sites": "3 toy sites",
                              "n": 1200, "sampling_frame": "cluster-randomised, children under 5", "value": 0.42}],
            "comparison": "the trial's 0.42 is below the card's 0.5, inside its fitted range [0.3, 0.7]",
            "chosen": {"range": [0.3, 0.6], "rule": "the trial's confidence interval, widened to the card's lower bound"},
            "decision_test": "DT1"},
        "unit_attribute:x": {
            "model_value": "units.csv column cov (calibrated)", "model_source": "toy surface",
            "no_data": {"reason": "no field measurement of the toy index",
                        "borrowing_rule": "the calibrated value per unit; the calibration's interval carries it"},
            "chosen": {"value": "calibrated per unit", "rule": "no direct data: the calibration is the evidence"}},
    }


def toy_dossier_spec(d: str, entries: dict | None = None, ref: str = TOY, dossier: bool = True) -> str:
    """The toy spec (toolkit/spec/schema.py toy_spec) on the toy dossier engine, with `evidence.dossier` and the dossier
    file (entries: toy_entries() by default) when `dossier`."""
    import yaml
    from toolkit.spec import schema
    p = schema.toy_spec(d)
    raw = yaml.safe_load(open(p))
    raw["engine"] = {"ref": ref}
    if dossier:
        raw["evidence"] = {"dossier": DOSSIER_FILE}
        with open(os.path.join(d, DOSSIER_FILE), "w") as f:
            yaml.safe_dump({"inputs": toy_entries() if entries is None else entries}, f, sort_keys=False)
    with open(p, "w") as f:
        yaml.safe_dump(raw, f, sort_keys=False)
    return p


def _self_test() -> int:
    import copy
    import tempfile
    import yaml
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)

    from toolkit.spec import toy_engines as T
    T.enable_for_test()
    try:
        with tempfile.TemporaryDirectory() as d:
            raw = yaml.safe_load(open(toy_dossier_spec(d)))
            req, probs = required_inputs(raw, d)
            ok(sorted(req) == ["parameter:eff_a", "unit_attribute:x"] and probs == []
               and any("a" in u for u in req["parameter:eff_a"]["used_by"]),
               f"ED-R1 the spec's options need the inputs the engine declares for component a ({sorted(req)}, {probs})")
            ok(check(raw, open(os.path.join(d, DOSSIER_FILE), "rb").read(), req) == [],
               "ED-C1 a complete dossier (one input observed, one 'no data' with its borrowing rule) passes")
            for name, mut, want in (
                    ("uncovered", lambda e: e.pop("unit_attribute:x"), "unit_attribute:x"),
                    ("blank entry", lambda e: e.__setitem__("unit_attribute:x", None), "unit_attribute:x"),
                    ("blank observation field", lambda e: e["parameter:eff_a"]["observations"][0].__setitem__(
                        "sampling_frame", " "), "sampling_frame"),
                    ("blank rule", lambda e: e["unit_attribute:x"]["chosen"].__setitem__("rule", ""), "rule"),
                    ("no data without a borrowing rule", lambda e: e["unit_attribute:x"]["no_data"].pop(
                        "borrowing_rule"), "borrowing_rule"),
                    ("both observations and no data", lambda e: e["unit_attribute:x"].__setitem__(
                        "observations", e["parameter:eff_a"]["observations"]), "exactly one"),
                    ("observations and no comparison", lambda e: e["parameter:eff_a"].pop("comparison"), "comparison"),
                    ("range without a decision test", lambda e: e["parameter:eff_a"].pop("decision_test"),
                     "decision_test"),
                    ("range with an unknown decision test", lambda e: e["parameter:eff_a"].__setitem__(
                        "decision_test", "DT9"), "DT9")):
                e = copy.deepcopy(toy_entries())
                mut(e)
                got = check(raw, yaml.safe_dump({"inputs": e}).encode(), req)
                ok(any(want in g for g in got), f"ED-C2 {name} is refused, naming {want!r}: {got[:3]}")
            ok(check(raw, b"inputs: [unclosed", req) and check(raw, None, req)
               and check(raw, b"- a list\n", req),
               "ED-C3 an unparseable, absent or malformed dossier is refused (fail closed)")
        with tempfile.TemporaryDirectory() as d:
            raw = yaml.safe_load(open(toy_dossier_spec(d, ref="toolkit.engines.contract:ToyEngine", dossier=False)))
            ok(required_inputs(raw, d) == ({}, []),
               "ED-R2 a spec whose options use no component with decision inputs needs none (and no dossier)")
            raw["engine"] = {"auto": True, "candidates": [TOY]}
            req, probs = required_inputs(raw, d)
            ok(sorted(req) == ["parameter:eff_a", "unit_attribute:x"],
               f"ED-R3 engine: auto takes the inputs of every eligible candidate engine ({sorted(req)}, {probs})")
            raw["engine"] = {"ref": "toolkit.spec.toy_engines:ToySpecRateEngine", "config": {"components": ["a", "b"]}}
            req, probs = required_inputs(raw, d)
            ok(req == {} and probs and "declares no decision inputs" in probs[0],
               f"ED-R4 an engine that declares no decision inputs for a mapped component fails closed: {probs}")
        # every shipped engine's declaration is valid against its card
        for row in shipped_declaration_problems():
            ok(not row[1], f"ED-E1 {row[0]}: its decision inputs validate against its card {row[1][:3]}")
        ok(len(shipped_declaration_problems()) == 4, "ED-E2 the malaria, compartmental, renewal and package-rate "
                                                     "engines declare decision inputs")
    except NameError as e:
        ok(False, f"evidence dossier: {e}")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    print(__doc__)
