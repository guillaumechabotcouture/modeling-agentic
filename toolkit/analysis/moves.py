"""What moves when a plan is re-solved (F6 S1-5): beside the share of units whose package changes.

F5's decision tests, stability folds and sensitivity rows reported only that share against a threshold, which
hid programme-sized changes. For a point plan and the plan re-solved under a test, `compare` reports:
  regret               the re-optimised objective minus the point plan's objective under the test (the value lost
                       by keeping the point plan), and that as a share of the re-optimised objective;
  population_changed   the population (the spec's `options.populations.total` column) of the units whose package
                       changes, and its share of the total;
  dollars_by_component the re-solved plan's cost minus the point plan's, component by component, under the test's
                       costs; `dollars_moved` is the larger of the increases and the decreases (money moved between
                       components), also as a share of the allocatable budget;
  units_changed_by_group  the units changing package in each `decision_units.region` group.
The plan changes on any of three criteria: the share of units (kept), the regret share, or the dollars-moved share,
each against its threshold (toolkit/analysis/thresholds.yaml decision_tests; the spec's analysis.share_threshold,
analysis.regret_threshold and analysis.dollars_threshold override them). `triggered_by` names the ones that fired.
"""
from __future__ import annotations

import os
import sys

import yaml

PRODUCER = "toolkit/analysis/moves.py"
CRITERIA = ("share", "regret", "dollars")
SPEC_KEYS = {"share": "share_threshold", "regret": "regret_threshold", "dollars": "dollars_threshold"}


def thresholds(analysis: dict) -> dict:
    raise NotImplementedError


def component_totals(ccost: dict, plan: dict, ids) -> dict:
    raise NotImplementedError


def compare(ids, plan, ch, ben, ccost, objective, pop, region, budget, thr, components) -> dict:
    raise NotImplementedError


def component_stability(pk: dict, pairs, components) -> dict:
    raise NotImplementedError


def summarise(A, plan, ch, ben, cost, objective) -> dict:
    raise NotImplementedError


# --------------------------------------------------------------------------- self-test
def _self_test() -> int:
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)

    def attempt(fn, *a):
        try:
            return fn(*a), None
        except Exception as e:  # noqa: BLE001 -- a failing case, not a crash of the self-test
            return None, e

    ids = ["u1", "u2", "u3", "u4"]
    reg = {"u1": "N", "u2": "N", "u3": "S", "u4": "S"}
    pop = {"u1": 100.0, "u2": 200.0, "u3": 300.0, "u4": 400.0}
    cc = {i: {"none": {}, "a": {"a": 10.0}, "b": {"b": 6.0}} for i in ids}
    ben = {"u1": {"none": 0.0, "a": 5.0, "b": 8.0}, "u2": {"none": 0.0, "a": 5.0, "b": 1.0},
           "u3": {"none": 0.0, "a": 1.0, "b": 4.0}, "u4": {"none": 0.0, "a": 3.0, "b": 1.0}}
    plan = {"u1": "a", "u2": "a", "u3": "b", "u4": "none"}
    ch = {"u1": "b", "u2": "a", "u3": "b", "u4": "a"}
    thr = {"share": 0.6, "regret": 0.1, "dollars": 0.05}
    m, e = attempt(compare, ids, plan, ch, ben, cc, 20.0, pop, reg, 100.0, thr, ["a", "b"])
    m = m or {}
    ok(e is None and abs(m.get("regret", -1) - 6.0) < 1e-12 and abs(m.get("regret_share", -1) - 0.3) < 1e-12
       and m.get("point_plan_objective_under_test") == 14.0,
       f"MV1 regret is the re-optimised objective minus the point plan's under the test ({e or m.get('regret')})")
    ok(m.get("units_changed") == 2 and m.get("share_units_changed") == 0.5 and m.get("population_changed") == 500.0
       and abs(m.get("population_changed_share", -1) - 0.5) < 1e-12,
       f"MV2 population whose package changes, from the population column ({m.get('population_changed')})")
    ok(m.get("dollars_by_component") == {"a": 0.0, "b": 6.0} and m.get("dollars_moved") == 6.0
       and abs(m.get("dollars_moved_share", -1) - 0.06) < 1e-12,
       f"MV3 dollars moved, component by component, as a share of the allocatable budget "
       f"({m.get('dollars_by_component')}, {m.get('dollars_moved')})")
    ok(m.get("units_changed_by_group") == [{"group": "N", "changed": 1, "units": 2},
                                           {"group": "S", "changed": 1, "units": 2}],
       f"MV4 units changing package, by group ({m.get('units_changed_by_group')})")
    ok(m.get("triggered_by") == ["regret", "dollars"] and m.get("decision_changes") is True,
       f"MV5 a test below the share threshold changes the plan on regret and dollars, and says so "
       f"({m.get('triggered_by')}, {m.get('decision_changes')})")
    m2, _ = attempt(compare, ids, plan, ch, ben, cc, 20.0, pop, reg, 100.0, {"share": 0.4, "regret": 0.5,
                                                                             "dollars": 0.5}, ["a", "b"])
    ok((m2 or {}).get("triggered_by") == ["share"] and (m2 or {}).get("decision_changes") is True,
       f"MV6 the share criterion is kept ({(m2 or {}).get('triggered_by')})")
    m3, _ = attempt(compare, ids, plan, plan, ben, cc, 14.0, pop, reg, 100.0, thr, ["a", "b"])
    ok((m3 or {}).get("triggered_by") == [] and (m3 or {}).get("decision_changes") is False
       and (m3 or {}).get("regret") == 0.0 and (m3 or {}).get("dollars_moved") == 0.0,
       f"MV7 an unchanged plan moves nothing ({m3})")
    m4, _ = attempt(compare, ids, plan, ch, ben, cc, 20.0, pop, None, 100.0, thr, ["a", "b"])
    ok(m4 is not None and m4.get("units_changed_by_group") is None,
       "MV8 without a region column there is no by-group count (None, not an empty list)")
    _, e5 = attempt(compare, ids, plan, ch, ben, cc, 20.0, {"u1": 100.0}, reg, 100.0, thr, ["a", "b"])
    _, e6 = attempt(compare, ids, plan, ch, ben, None, 20.0, pop, reg, 100.0, thr, ["a", "b"])
    ok(isinstance(e5, ValueError) and isinstance(e6, ValueError),
       f"MV9 a unit without a population, or costs without a component split, fail closed ({e5!r}, {e6!r})")
    t, e7 = attempt(thresholds, {"regret_threshold": 0.02})
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "thresholds.yaml")) as f:
        base = yaml.safe_load(f)["decision_tests"]
    ok(e7 is None and t == {"share": base["share_threshold"], "regret": 0.02,
                            "dollars": base["dollars_share_threshold"]}
       and 0 < base["regret_share_threshold"] < 1,
       f"MV10 thresholds come from thresholds.yaml, the spec overrides each ({t}, {e7!r})")
    pk = {"none": frozenset(), "a": frozenset({"a"}), "b": frozenset({"b"}), "a+b": frozenset({"a", "b"})}
    s, e8 = attempt(component_stability, pk, [("a", "a"), ("none", "none"), ("a+b", "a"), ("b", "none")], ["a", "b"])
    ok(e8 is None and s == {"by_component": {"a": {"provided": 2, "absent": 2, "changed": 0},
                                             "b": {"provided": 0, "absent": 2, "changed": 2}},
                            "stable_provision": 2, "stable_absence": 4, "changed": 2},
       f"MV11 stable provision is counted apart from stable absence, per component ({s}, {e8!r})")
    # mechanism_manifest (the Phase 49 loader) accepts a verdict that a regret or dollars criterion explains
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                                    "scripts"))
    import mechanism_manifest as MM
    res = {"decision_changes": True, "share_units_changed": 0.05, "share_threshold": 0.1, "regret_share": 0.02,
           "regret_threshold": 0.01, "dollars_moved_share": 0.0, "dollars_threshold": 0.05, "triggered_by": ["regret"]}
    ok(MM._result_conflicts("t.yaml", res, "x", None) == [],
       "MV12 a result changed on regret, below the share threshold, is consistent")
    ok(MM._result_conflicts("t.yaml", dict(res, decision_changes=False), "x", None) != []
       and MM._result_conflicts("t.yaml", dict(res, triggered_by=["share"]), "x", None) != [],
       "MV13 a verdict or triggered_by that its criteria contradict is a conflict")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    print(__doc__)
