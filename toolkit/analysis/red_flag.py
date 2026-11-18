#!/usr/bin/env python3
"""Red flag, a critic heuristic: the most expensive option of a component group wins in nearly every unit.

    python3 toolkit/analysis/red_flag.py --self-test

When an optimiser chooses the dearest alternative almost everywhere, either its extra effect is real and large
everywhere, or the engine overstates it. This is a note the critics see, never a gate and never a finding: the v2
critique task (agents/v2_sdk_tasks.py critique) adds `critic_notes(run_dir)` to the reviewer's message, and the
reviewer judges whether it needs a blocker (owner direction 2026-11-18: no one-incident rules in the gate).

Rule: a group (two or more components sharing options.components.<c>.group) whose components carry fixed cost items;
in each unit whose plan holds one of the group, the dearest is the component with the highest fixed cost in that unit
(cost items with a basis other than per_outcome, through toolkit/alloc/costs.py item_cost; per_outcome items depend
on the model and are left out, which the note states). The note is written when the dearest is chosen in at least
SHARE of those units and in at least MIN_UNITS of them. Nothing here names a disease.
"""
from __future__ import annotations

import os
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

SHARE = 0.9          # "nearly all units"
MIN_UNITS = 5        # a pattern, not a handful of units


def flags(alloc: dict, units: list, plan: dict) -> list:
    """[{group, component, dearest_share, units, chosen}] for each group whose dearest component wins nearly
    everywhere. `alloc`: the toolkit/alloc spec; `plan`: {unit id: package}."""
    from toolkit.alloc import costs
    comps = alloc.get("components") or {}
    groups: dict = {}
    for c, v in comps.items():
        if isinstance(v, dict) and v.get("group") is not None:
            groups.setdefault(v["group"], []).append(c)
    pk = costs.packages(alloc)
    fixed = [it for it in alloc.get("cost_items") or [] if it.get("basis") != "per_outcome"]
    uid = alloc["units"]["id"]
    out = []
    for g, cs in sorted(groups.items()):
        if len(cs) < 2:
            continue
        n = hits = 0
        for u in units:
            chosen = [c for c in pk.get(plan.get(str(u[uid])), ()) if c in cs]
            if not chosen:
                continue
            cost = {c: sum(costs.item_cost(it, u, alloc) for it in fixed if it["component"] == c) for c in cs}
            if len(set(cost.values())) < 2:
                continue                   # no price difference in this unit: no dearest option
            n += 1
            hits += chosen[0] == max(cs, key=lambda c: cost[c])
        if n >= MIN_UNITS and hits / n >= SHARE:
            won = {}
            for u in units:
                for c in pk.get(plan.get(str(u[uid])), ()):
                    if c in cs:
                        won[c] = won.get(c, 0) + 1
            out.append({"group": g, "component": max(won, key=won.get), "dearest_share": hits / n, "units": n,
                        "chosen": won})
    return out


def critic_notes(run_dir: str) -> list[str]:
    """The heuristic notes for the critics ([] when there is no plan or nothing to note). Never raises: a note that
    cannot be computed says so."""
    p = os.path.join(run_dir, "analysis", "point.yaml")
    if not os.path.exists(p):
        return []
    try:
        from toolkit.alloc import costs
        from toolkit.spec import schema
        with open(p, encoding="utf-8") as fh:
            doc = yaml.safe_load(fh) or {}
        plan = (doc.get("data") or doc).get("plan")
        if not isinstance(plan, dict) or not plan:
            return []
        alloc = schema.load(os.path.join(run_dir, "decision_spec.yaml")).alloc_spec()
        fl = flags(alloc, costs.load_units(alloc, run_dir), plan)
    except Exception as e:  # noqa: BLE001 -- a heuristic: say it could not run, never stop the review
        return [f"The most-expensive-option heuristic could not run ({type(e).__name__}: {e})."]
    return [f"Heuristic (not a finding): the dearest option of group {f['group']} is chosen in "
            f"{100 * f['dearest_share']:.0f}% of the {f['units']} units whose plan holds the group ({f['component']} "
            f"wins: {f['chosen']}; fixed cost items only). Check the mechanism: which of burden, relative effect or "
            f"price makes it win, and whether its effect in these units rests on trial evidence or on the engine "
            f"carrying an effect beyond the settings its trials covered. Raise a blocker only if it does not hold."
            for f in fl]


def _self_test() -> int:
    import tempfile
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        c or fails.append(m)

    alloc = {"units": {"id": "u", "populations": {"total": "pop"}},
             "components": {"lo": {"group": "g"}, "hi": {"group": "g"}, "x": {}},
             "packages": {"none": [], "lo": ["lo"], "hi": ["hi"], "x": ["x"], "hi+x": ["hi", "x"]},
             "prices": {"base_year": 2024},
             "cost_items": [{"id": "a", "component": "lo", "basis": "per_person", "value": 1.0, "price_year": 2024, "price_rule": "as_published"},
                            {"id": "b", "component": "hi", "basis": "per_person", "value": 2.0, "price_year": 2024, "price_rule": "as_published"},
                            {"id": "c", "component": "x", "basis": "per_person", "value": 9.0, "price_year": 2024, "price_rule": "as_published"}]}
    units = [{"u": str(i), "pop": "100"} for i in range(10)]
    plan = {str(i): ("hi+x" if i % 2 else "hi") for i in range(10)}
    try:
        fl = flags(alloc, units, plan)
        ok(len(fl) == 1 and fl[0]["component"] == "hi" and fl[0]["dearest_share"] == 1.0 and fl[0]["units"] == 10,
           f"RF1 the dearest option chosen in every unit is flagged: {fl}")
        plan2 = dict(plan, **{"0": "lo", "1": "lo"})
        ok(flags(alloc, units, plan2) == [], "RF2 at 80% of units it is not 'nearly all'")
        ok(flags(alloc, units[:4], {str(i): "hi" for i in range(4)}) == [], "RF3 fewer than MIN_UNITS is no pattern")
        ok(flags(alloc, units, {str(i): "lo" for i in range(10)}) == [], "RF4 the cheaper option winning is no flag")
        a2 = dict(alloc, cost_items=[dict(it, value=1.0) for it in alloc["cost_items"]])
        ok(flags(a2, units, plan) == [], "RF5 equal prices: no dearest option")
        a3 = dict(alloc, cost_items=alloc["cost_items"] + [{"id": "o", "component": "lo", "basis": "per_outcome",
                                                            "outcome": "treated", "value": 50.0, "price_year": 2024, "price_rule": "as_published"}])
        ok(len(flags(a3, units, plan)) == 1, "RF6 per-outcome items are left out of the price comparison")
    except Exception as e:  # noqa: BLE001
        ok(False, f"RF1-6 raised {type(e).__name__}: {e}")
    with tempfile.TemporaryDirectory() as d:
        ok(critic_notes(d) == [], "RF7 no analysis: no note")
        os.makedirs(os.path.join(d, "analysis"))
        open(os.path.join(d, "analysis", "point.yaml"), "w").write("data: {plan: {'1': a}}\n")
        got = critic_notes(d)
        ok(len(got) == 1 and "could not run" in got[0], f"RF8 a note that cannot be computed says so, never raises: {got}")
        from unittest import mock
        with mock.patch(__name__ + ".flags", lambda *a: [{"group": "g", "component": "hi", "dearest_share": 1.0,
                                                          "units": 10, "chosen": {"hi": 10}}]), \
                mock.patch("toolkit.spec.schema.load", lambda p: type("S", (), {"alloc_spec": lambda self: alloc})()), \
                mock.patch("toolkit.alloc.costs.load_units", lambda a, d: units):
            got = critic_notes(d)
        ok(len(got) == 1 and got[0].startswith("Heuristic (not a finding)") and "group g" in got[0],
           f"RF9 a flag becomes a note for the critics: {got}")
    import ast
    src = open(os.path.join(ROOT, "toolkit", "analysis", "contract.py")).read()
    ok("red_flag" not in src, "RF10 the gate (toolkit/analysis/contract.py) does not use it")
    tasks = open(os.path.join(ROOT, "agents", "v2_sdk_tasks.py")).read()
    ok("critic_notes(run_dir)" in tasks and ast.parse(tasks) is not None, "RF11 the critique task adds the notes")
    print(f"{'FAIL' if fails else 'OK'}: " + (f"{len(fails)} case(s) failed" if fails else
                                               "all self-test cases passed."))
    return 1 if fails else 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    print(__doc__)
