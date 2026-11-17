#!/usr/bin/env python3
"""Red flag: the most expensive option of a component group wins in nearly every unit (owner, 2026-11-17).

    python3 toolkit/analysis/red_flag.py --self-test

When an optimiser chooses the dearest alternative almost everywhere, either its extra effect is real and large
everywhere, or the engine overstates it (for example an effect carried outside the settings its trials covered). The
plan cannot go to WRITE on that pattern until the run records why it wins. The gate (toolkit/analysis/contract.py)
raises a HIGH finding, not declarable, until `mechanism_checks.yaml` in the run directory holds a check for it:

    checks:
      - group: <options.components group>
        component: <the component that wins>
        why_it_wins: "<the mechanism: which part of benefit per cost (burden, relative effect, price) makes it win,
                      and why that holds in these units>"
        evidence: ["<a trial, benchmark or dossier entry that the mechanism rests on, with how it compares>", ...]

Rule: a group (two or more components sharing options.components.<c>.group) whose components carry fixed cost items;
in each unit whose plan holds one of the group, the dearest is the component with the highest fixed cost in that unit
(cost items with a basis other than per_outcome, through toolkit/alloc/costs.py item_cost; per_outcome items depend
on the model and are left out, which the finding states). The flag is raised when the dearest is chosen in at least
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
RECORD = "mechanism_checks.yaml"


def flags(alloc: dict, units: list, plan: dict) -> list:
    """[{group, component, dearest_share, units, chosen}] for each group whose dearest component wins nearly
    everywhere. `alloc`: the toolkit/alloc spec; `plan`: {unit id: package}."""
    return []                       # α: tests first; the rule lands in β
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


def record_problems(run_dir: str, flag: dict) -> list:
    """Why the run's mechanism record does not cover `flag` ([] when it does). Fail closed on an unreadable file."""
    return []                       # α
    p = os.path.join(run_dir, RECORD)
    if not os.path.exists(p):
        return [f"no {RECORD}"]
    try:
        with open(p, encoding="utf-8") as fh:
            doc = yaml.safe_load(fh)
    except (OSError, ValueError, yaml.YAMLError) as e:
        return [f"{RECORD} unreadable: {type(e).__name__}"]
    checks = doc.get("checks") if isinstance(doc, dict) else None
    if not isinstance(checks, list):
        return [f"{RECORD} has no list `checks`"]
    for c in checks:
        if not isinstance(c, dict) or c.get("group") != flag["group"] or c.get("component") != flag["component"]:
            continue
        why, ev = c.get("why_it_wins"), c.get("evidence")
        if not (isinstance(why, str) and why.strip()):
            return [f"{RECORD}: the check of {flag['component']} has no why_it_wins"]
        if not (isinstance(ev, list) and ev and all(isinstance(x, str) and x.strip() for x in ev)):
            return [f"{RECORD}: the check of {flag['component']} has no evidence list"]
        return []
    return [f"{RECORD} has no check for group {flag['group']}, component {flag['component']}"]


def findings(run_dir: str, point: dict | None) -> list:
    """Gate findings (toolkit/analysis/contract.py): one HIGH per uncovered flag; [] without a plan."""
    return []                       # α
    if not isinstance(point, dict) or not isinstance(point.get("plan"), dict):
        return []
    from toolkit.alloc import costs
    from toolkit.spec import schema
    try:
        sp = schema.load(os.path.join(run_dir, "decision_spec.yaml"))
        alloc = sp.alloc_spec()
        fl = flags(alloc, costs.load_units(alloc, run_dir), point["plan"])
    except Exception as e:  # noqa: BLE001 -- fail closed: the check could not run
        return [{"kind": "red_flag_mechanism", "severity": "HIGH", "stage": "ANALYZE", "scope_declarable": False,
                 "key": "red_flag", "source": "toolkit/analysis/red_flag.py",
                 "claim": f"the most-expensive-option check could not run: {type(e).__name__}: {e}"}]
    out = []
    for f in fl:
        bad = record_problems(run_dir, f)
        if bad:
            out.append({"kind": "red_flag_mechanism", "severity": "HIGH", "stage": "ANALYZE",
                        "scope_declarable": False, "key": f"red_flag:{f['group']}:{f['component']}",
                        "source": "toolkit/analysis/red_flag.py",
                        "claim": (f"The dearest option of group {f['group']} is chosen in {100 * f['dearest_share']:.0f}% "
                                  f"of the {f['units']} units whose plan holds the group ({f['component']} wins: "
                                  f"{f['chosen']}; fixed cost items only, per-outcome costs left out). Before WRITE, "
                                  f"record in {RECORD} why it wins (which of burden, relative effect or price drives "
                                  f"it, and why that holds in these units) with the trial or benchmark evidence it "
                                  f"rests on: {'; '.join(bad)}")})
    return out


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
    f = {"group": "g", "component": "hi"}
    with tempfile.TemporaryDirectory() as d:
        ok(record_problems(d, f) == [f"no {RECORD}"], "RF7 no record: the flag stands")
        p = os.path.join(d, RECORD)
        for body, want in (("{not yaml", "unreadable"), ("checks: 3\n", "no list"),
                           ("checks: [{group: g, component: lo, why_it_wins: w, evidence: [e]}]\n", "no check"),
                           ("checks: [{group: g, component: hi, why_it_wins: ' ', evidence: [e]}]\n", "why_it_wins"),
                           ("checks: [{group: g, component: hi, why_it_wins: w, evidence: []}]\n", "evidence")):
            open(p, "w").write(body)
            got = record_problems(d, f)
            ok(len(got) == 1 and want in got[0], f"RF8 a record that does not cover the flag ({want}): {got}")
        open(p, "w").write("checks: [{group: g, component: hi, why_it_wins: 'the trial shows a larger effect at a "
                           "small price gap', evidence: ['benchmark T2 in band']}]\n")
        ok(record_problems(d, f) == [], "RF9 a complete record covers the flag")
        ok(findings(d, None) == [] and findings(d, {"plan": "x"}) == [], "RF10 no plan: nothing to check")
        fs = findings(d, {"plan": {}})
        ok(len(fs) == 1 and fs[0]["severity"] == "HIGH" and fs[0]["scope_declarable"] is False
           and "could not run" in fs[0]["claim"], f"RF11 an unreadable spec fails closed: {fs}")
    print(f"{'FAIL' if fails else 'OK'}: " + (f"{len(fails)} case(s) failed" if fails else
                                               "all self-test cases passed."))
    return 1 if fails else 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    print(__doc__)
