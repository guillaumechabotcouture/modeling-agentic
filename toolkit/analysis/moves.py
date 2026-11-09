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
    """{criterion: threshold}: thresholds.yaml decision_tests, each overridden by the spec's analysis field."""
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "thresholds.yaml")) as f:
        base = yaml.safe_load(f)["decision_tests"]
    default = {"share": base["share_threshold"], "regret": base["regret_share_threshold"],
               "dollars": base["dollars_share_threshold"]}
    return {k: float((analysis or {}).get(SPEC_KEYS[k], default[k])) for k in CRITERIA}


def component_totals(ccost: dict, plan: dict, ids) -> dict:
    """{component: dollars} of a plan under a cost table split by component ({unit: {package: {component: $}}})."""
    tot: dict = {}
    for i in ids:
        for c, v in ccost[i][plan[i]].items():
            tot[c] = tot.get(c, 0.0) + float(v)
    return tot


def infeasible_reasons(ids, plan, cost, feas, plan_budget) -> list[str]:
    """Why the point plan cannot be kept under a test: its cost under the test's costs above the planning budget,
    a package the test's feasibility rules out, a cap it exceeds, or a group rule it breaks. [] when it can."""
    out = []
    spent = sum(float(cost[i][plan[i]]) for i in ids)
    if spent > float(plan_budget) * (1 + 1e-9):
        out.append(f"costs {spent:,.0f} under the test, above the planning budget {float(plan_budget):,.0f}")
    if feas:
        bad = [i for i in ids if not feas["feasible"][i].get(plan[i], False)]
        if bad:
            out.append(f"{len(bad)} units' packages are not feasible under the test (e.g. {bad[:3]})")
        for cap in feas.get("caps") or []:
            used = sum(cap["weights"][i][plan[i]] for i in ids)
            if used > cap["max"] + 1e-9:
                out.append(f"cap {cap['name']}: {used:g} above {cap['max']:g}")
        for L in feas.get("links") or []:
            split = [g for g, m in L["groups"].items() if len({L["class"][u][plan[u]] for u in m}) > 1]
            if split:
                out.append(f"group rule {L['name']}: {len(split)} groups take more than one decision")
    return out


def compare(ids, plan, ch, ben, ccost, objective, pop, region, budget, thr, components, plan_budget=None,
            feas=None) -> dict:
    """What moves from the point plan `plan` to the plan `ch` re-solved under a test (benefits `ben`, costs `ccost`
    split by component, re-optimised objective `objective`, solved within `plan_budget` (default `budget`) under
    `feas`). `pop`: {unit: population}; `region`: {unit: group} or None. Fails closed on a unit without a population
    or costs without a component split. A point plan the test makes infeasible changes the plan: it has no regret
    (it cannot be kept), only the signed objective gap."""
    if ccost is None:
        raise ValueError("decision summary: the costs carry no split by component (toolkit/analysis/core.py "
                         "CostTable); dollars moved cannot be computed")
    miss = [i for i in ids if pop.get(i) is None]
    if miss:
        raise ValueError(f"decision summary: no population for units {miss[:5]}")
    cost = {i: {k: sum(ccost[i][k].values()) for k in {plan[i], ch[i]}} for i in ids}     # the two plans only
    why = infeasible_reasons(ids, plan, cost, feas, budget if plan_budget is None else plan_budget)
    changed = [i for i in ids if ch[i] != plan[i]]
    point = sum(ben[i][plan[i]] for i in ids)
    gap = float(objective) - point
    regret = None if why else gap
    pt, re = component_totals(ccost, plan, ids), component_totals(ccost, ch, ids)
    delta = {c: re.get(c, 0.0) - pt.get(c, 0.0) for c in components}
    moved = max(sum(v for v in delta.values() if v > 0), -sum(v for v in delta.values() if v < 0))
    # Codex r2 P2-2: over unit x component before anything cancels, so a swap between components inside a unit and a
    # transfer between units within a component both count
    realloc = 0.5 * sum(abs(ccost[i][ch[i]].get(c, 0.0) - ccost[i][plan[i]].get(c, 0.0))
                        for i in ids for c in set(ccost[i][ch[i]]) | set(ccost[i][plan[i]]))
    total_pop = sum(float(pop[i]) for i in ids)
    pc = sum(float(pop[i]) for i in changed)
    by = None
    if region is not None:
        n: dict = {}
        for i in ids:
            g = n.setdefault(str(region[i]), [0, 0])
            g[1] += 1
            g[0] += ch[i] != plan[i]
        by = [{"group": g, "changed": v[0], "units": v[1]} for g, v in sorted(n.items(), key=lambda kv: (-kv[1][0],
                                                                                                         kv[0])) if v[0]]
    share = lambda x: x / float(budget) if budget > 0 else None  # noqa: E731
    rec = {"units_changed": len(changed), "share_units_changed": len(changed) / len(ids),
           "point_plan_feasible": not why, "point_plan_infeasible": why,
           "point_plan_objective_under_test": point, "reoptimised_objective": float(objective), "objective_gap": gap,
           "regret": regret, "regret_share": regret / float(objective) if regret is not None and objective > 0 else None,
           "population_changed": pc, "population_changed_share": pc / total_pop if total_pop > 0 else None,
           "dollars_by_component": delta, "dollars_moved": moved, "dollars_moved_share": share(moved),
           "dollars_reallocated": realloc, "dollars_reallocated_share": share(realloc),
           "dollars_share": share(realloc), "units_changed_by_group": by}
    rec["triggered_by"] = (["infeasible"] if why else []) + [k for k in CRITERIA if fires(rec, k, thr[k])]
    rec["decision_changes"] = bool(rec["triggered_by"])
    return rec


REGRET_TOL = 1e-9      # relative to max(1, |objective|); scripts/mechanism_manifest.py holds the same rule


def fires(rec: dict, k: str, t: float) -> bool:
    """Criterion k against threshold t. Regret under a non-positive re-optimised objective has no share: any regret
    above the tolerance is then a change (Codex r2 P2-1)."""
    if k == "share":
        return rec["share_units_changed"] > t
    if k == "dollars":
        return rec["dollars_share"] is not None and rec["dollars_share"] > t
    if rec.get("regret") is None:
        return False
    if rec.get("regret_share") is not None:
        return rec["regret_share"] > t
    return rec["regret"] > REGRET_TOL * max(1.0, abs(rec["reoptimised_objective"]))


def component_stability(pk: dict, pairs, components) -> dict:
    """Per component, over (point package, fold package) pairs: provided in both (stable provision), absent in both
    (stable absence), or changed; so a component consistently not chosen is not counted as a stable choice."""
    by = {c: {"provided": 0, "absent": 0, "changed": 0} for c in components}
    for a, b in pairs:
        for c in components:
            ia, ib = c in pk[a], c in pk[b]
            by[c]["provided" if ia and ib else "absent" if not ia and not ib else "changed"] += 1
    return {"by_component": by, "stable_provision": sum(v["provided"] for v in by.values()),
            "stable_absence": sum(v["absent"] for v in by.values()),
            "changed": sum(v["changed"] for v in by.values())}


def population(A) -> dict:
    """{unit: population} from the spec's options.populations.total column (toolkit/alloc/costs.py requires it)."""
    col = ((A.alloc.get("units") or {}).get("populations") or {}).get("total")
    if not col:
        raise ValueError("decision summary: options.populations.total names no population column")
    return {i: float(A.by_id[i][col]) for i in A.ids}


SUMMARY = ("units_changed", "share_units_changed", "point_plan_feasible", "point_plan_infeasible",
           "point_plan_objective_under_test", "reoptimised_objective", "objective_gap", "regret", "regret_share",
           "population_changed", "population_changed_share", "dollars_by_component", "dollars_moved",
           "dollars_moved_share", "dollars_reallocated", "dollars_reallocated_share", "dollars_share",
           "units_changed_by_group", "triggered_by", "decision_changes")


def aggregate(rows: list) -> dict:
    """A test's summary over its tested values (compare() rows with `value`): `worst_by_criterion` gives, for the
    share, regret, dollars and population, the worst value with its own coherent summary (Codex r1 P2-3); the
    top-level numbers are each criterion's worst, the breakdowns those of the value they come from."""
    def worst(key):
        c = [r for r in rows if r.get(key) is not None]
        return max(c, key=lambda r: r[key]) if c else None
    rg = [r for r in rows if "regret" in (r.get("triggered_by") or [])]   # a value that fires on regret comes first
    w = {"share": worst("share_units_changed"),
         "regret": (max(rg, key=lambda r: r["regret"]) if rg else worst("regret_share") or worst("regret") or rows[0]),
         "dollars": worst("dollars_reallocated") or rows[0], "dollars_moved": worst("dollars_moved") or rows[0],
         "population": worst("population_changed")}
    wb = {k: dict({f: r.get(f) for f in SUMMARY}, value=r.get("value")) for k, r in w.items()}
    trig = [k for k in ("infeasible",) + CRITERIA if any(k in (r.get("triggered_by") or []) for r in rows)]
    return {"decision_changes": bool(trig), "triggered_by": trig,
            "point_plan_feasible": all(r.get("point_plan_feasible", True) for r in rows),
            "share_units_changed": w["share"]["share_units_changed"],
            "units_changed_by_group": w["share"]["units_changed_by_group"],
            **{k: w["regret"].get(k) for k in ("regret", "regret_share", "reoptimised_objective", "objective_gap")},
            # Codex r2 P2-3: each dollar measure from its own worst value
            **{k: w["dollars"].get(k) for k in ("dollars_share", "dollars_reallocated", "dollars_reallocated_share")},
            **{k: w["dollars_moved"].get(k) for k in ("dollars_moved", "dollars_moved_share", "dollars_by_component")},
            "population_changed": w["population"]["population_changed"],
            "population_changed_share": w["population"]["population_changed_share"], "worst_by_criterion": wb}


def summarise(A, plan, ch, ben, cost, objective, budget=None, feas=None) -> dict:
    """compare() for the analysis state: its population, region, allocatable budget, thresholds and components; the
    point plan is checked against `budget` (default: the budget plans are solved at) and `feas` (default A.feas)."""
    reg = {i: A.by_id[i].get(A.region) for i in A.ids} if A.region else None
    if budget is None:
        budget = A.budget if getattr(A, "plan_budget", None) is None else A.plan_budget
    return compare(A.ids, plan, ch, ben, getattr(cost, "by_component", None), objective, population(A), reg,
                   A.budget, thresholds(A.spec["analysis"]), list(A.spec["options"]["components"]),
                   plan_budget=budget, feas=feas if feas is not None else getattr(A, "feas", None))


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
           "regret_threshold": 0.01, "dollars_share": 0.0, "dollars_threshold": 0.05, "triggered_by": ["regret"]}
    ok(MM._result_conflicts("t.yaml", res, "x", None) == [],
       "MV12 a result changed on regret, below the share threshold, is consistent")
    ok(MM._result_conflicts("t.yaml", dict(res, decision_changes=False), "x", None) != []
       and MM._result_conflicts("t.yaml", dict(res, triggered_by=["share"]), "x", None) != [],
       "MV13 a verdict or triggered_by that its criteria contradict is a conflict")
    # Codex r1 P2-1: a point plan the test makes infeasible (costs up, same budget) is a change, never NO_CHANGE
    ids100 = [f"v{k}" for k in range(100)]
    one = {i: {"none": {}, "a": {"a": 1.02}} for i in ids100}
    b100 = {i: {"none": 0.0, "a": 1.0} for i in ids100}
    p100 = {i: "a" for i in ids100}
    c100 = dict(p100, v98="none", v99="none")
    m5, e9 = attempt(compare, ids100, p100, c100, b100, one, 98.0, {i: 1.0 for i in ids100}, None, 100.0,
                     {"share": 0.1, "regret": 0.01, "dollars": 0.05}, ["a"])
    m5 = m5 or {}
    ok(e9 is None and m5.get("point_plan_feasible") is False and abs(m5.get("objective_gap", 0) + 2.0) < 1e-9
       and m5.get("regret") is None and "infeasible" in (m5.get("triggered_by") or []) and m5.get("decision_changes")
       and m5.get("point_plan_infeasible"),
       f"MV14 an infeasible point plan is reported and changes the plan; the signed gap is kept apart from regret "
       f"({e9!r}, {m5.get('point_plan_feasible')}, {m5.get('triggered_by')})")
    ok(MM._result_conflicts("t.yaml", dict(res, decision_changes=False, triggered_by=[], regret_share=0.0,
                                           point_plan_feasible=False), "x", None) != [],
       "MV15 the loader refuses NO_CHANGE for an infeasible point plan")
    # Codex r1 P2-2: a zero objective leaves the regret share undefined, which the loader accepts when stated
    z = {i: {"none": 0.0, "a": 0.0, "b": 0.0} for i in ids}
    m6, _ = attempt(compare, ids, plan, plan, z, cc, 0.0, pop, reg, 100.0, thr, ["a", "b"])
    rows = [dict(m6 or {}, value=1.0)]
    agg, e10 = attempt(aggregate, rows)
    doc = dict(agg or {}, varied="v", criterion="c", share_threshold=thr["share"], regret_threshold=thr["regret"],
               dollars_threshold=thr["dollars"])
    ok(e10 is None and doc.get("regret_share") is None and doc.get("decision_changes") is False
       and MM._result_conflicts("t.yaml", doc, "x", None) == []
       and MM._result_conflicts("t.yaml", dict(doc, reoptimised_objective=None), "x", None) != [],
       f"MV16 zero objective, emitter to loader: an undefined regret share is accepted when the objective and the "
       f"regret say why, refused otherwise ({e10!r})")
    # Codex r1 P2-3: each worst value by criterion carries its own tested value and the matching breakdown
    r_a = dict(m or {}, value="A", share_units_changed=0.1, dollars_share=0.3, dollars_by_component={"a": 30.0})
    r_b = dict(m or {}, value="B", share_units_changed=0.7, dollars_share=0.01, dollars_by_component={"a": 1.0})
    ag, e11 = attempt(aggregate, [r_a, r_b])
    w = (ag or {}).get("worst_by_criterion") or {}
    ok(e11 is None and w.get("dollars", {}).get("value") == "A" and w["dollars"]["dollars_by_component"] == {"a": 30.0}
       and w.get("share", {}).get("value") == "B" and ag["dollars_share"] == 0.3 and ag["share_units_changed"] == 0.7,
       f"MV17 maxima from different tested values each carry their own value and breakdown ({e11!r}, {w.keys()})")
    # coordinator: money moved between units inside one component is counted (dollars reallocated)
    p2, c2 = {"u1": "a", "u2": "none", "u3": "none", "u4": "none"}, {"u1": "none", "u2": "a", "u3": "none", "u4": "none"}
    m7, _ = attempt(compare, ids, p2, c2, ben, cc, 5.0, pop, reg, 100.0, {"share": 0.9, "regret": 0.9, "dollars": 0.05},
                    ["a", "b"])
    m7 = m7 or {}
    ok(m7.get("dollars_moved") == 0.0 and m7.get("dollars_reallocated") == 10.0
       and abs(m7.get("dollars_share", 0) - 0.1) < 1e-12 and m7.get("triggered_by") == ["dollars"],
       f"MV18 dollars reallocated between units inside one component trigger the dollars criterion "
       f"({m7.get('dollars_moved')}, {m7.get('dollars_reallocated')}, {m7.get('triggered_by')})")
    # Codex r2 P2-1: a positive regret under a non-positive optimum is a change (any regret above the tolerance)
    pz = {"u1": "a", "u2": "none", "u3": "none", "u4": "none"}
    bz = {i: {"none": 0.0, "a": -100.0 if i == "u1" else 0.0, "b": 0.0} for i in ids}
    cz = {"u1": "none", "u2": "none", "u3": "none", "u4": "none"}
    m8, _ = attempt(compare, ids, pz, cz, bz, cc, 0.0, pop, reg, 1000.0, {"share": 0.9, "regret": 0.5, "dollars": 0.9},
                    ["a", "b"])
    m8 = m8 or {}
    ag8, _ = attempt(aggregate, [dict(m8, value=1)])
    d8 = dict(ag8 or {}, varied="v", criterion="c", share_threshold=0.9, regret_threshold=0.5, dollars_threshold=0.9)
    ok(m8.get("regret") == 100.0 and m8.get("regret_share") is None and m8.get("triggered_by") == ["regret"]
       and d8.get("decision_changes") is True and MM._result_conflicts("t.yaml", d8, "x", None) == []
       and MM._result_conflicts("t.yaml", dict(d8, decision_changes=False, triggered_by=[]), "x", None) != [],
       f"MV16b a regret above the tolerance under a non-positive optimum changes the plan, emitter to loader "
       f"({m8.get('regret')}, {m8.get('triggered_by')})")
    # Codex r2 P2-2: a reciprocal swap of equal-cost packages between units moves the whole programme
    sw = {"u1": {"none": {}, "a": {"a": 600.0}, "b": {"b": 600.0}}, "u2": {"none": {}, "a": {"a": 600.0}, "b": {"b": 600.0}}}
    m9, _ = attempt(compare, ["u1", "u2"], {"u1": "a", "u2": "b"}, {"u1": "b", "u2": "a"},
                    {"u1": {"a": 1.0, "b": 1.0, "none": 0}, "u2": {"a": 1.0, "b": 1.0, "none": 0}}, sw, 2.0,
                    {"u1": 1.0, "u2": 1.0}, None, 1200.0, {"share": 1.0, "regret": 0.5, "dollars": 0.05}, ["a", "b"])
    m9 = m9 or {}
    ok(m9.get("dollars_moved") == 0.0 and m9.get("dollars_reallocated") == 1200.0 and m9.get("dollars_share") == 1.0
       and m9.get("triggered_by") == ["dollars"],
       f"MV19 dollars reallocated over unit x component: a reciprocal swap moves the programme "
       f"({m9.get('dollars_moved')}, {m9.get('dollars_reallocated')}, {m9.get('triggered_by')})")
    # Codex r2 P2-3: each dollar measure keeps its own maximum, tested value and breakdown
    ra = dict(m or {}, value="R", dollars_moved=0.0, dollars_reallocated=490.0, dollars_share=0.49,
              dollars_by_component={"a": 0.0})
    rb = dict(m or {}, value="M", dollars_moved=500.0, dollars_reallocated=5.0, dollars_share=0.005,
              dollars_by_component={"a": 500.0})
    ag9, e12 = attempt(aggregate, [ra, rb])
    w9 = (ag9 or {}).get("worst_by_criterion") or {}
    ok(e12 is None and ag9["dollars_reallocated"] == 490.0 and ag9["dollars_moved"] == 500.0
       and w9["dollars"]["value"] == "R" and w9.get("dollars_moved", {}).get("value") == "M"
       and ag9["dollars_by_component"] == {"a": 500.0},
       f"MV20 the two dollar maxima come from different values and each keeps its own ({e12!r}, {list(w9)})")
    # Codex r3 P2-1: one-sided spending from unspent budget fires on dollars moved (half of it is reallocated)
    m10, _ = attempt(compare, ids, {"u1": "none", "u2": "none", "u3": "none", "u4": "none"},
                     {"u1": "a", "u2": "none", "u3": "none", "u4": "none"}, z, {i: {"none": {}, "a": {"a": 60.0},
                                                                                   "b": {}} for i in ids},
                     0.0, pop, reg, 1000.0, {"share": 0.9, "regret": 0.9, "dollars": 0.05}, ["a", "b"])
    m10 = m10 or {}
    ok(m10.get("dollars_moved") == 60.0 and m10.get("dollars_reallocated") == 30.0
       and abs(m10.get("dollars_share", 0) - 0.06) < 1e-12 and m10.get("triggered_by") == ["dollars"],
       f"MV21 the dollars criterion is the larger of dollars moved and dollars reallocated ({m10.get('dollars_share')}, "
       f"{m10.get('triggered_by')})")
    # Codex r3 P2-2: one value fires on regret under a zero optimum, another makes the point plan infeasible
    inf = dict(m8, value=2, point_plan_feasible=False, point_plan_infeasible=["over budget"], regret=None,
               regret_share=None, triggered_by=["infeasible"], decision_changes=True)
    ag11, e13 = attempt(aggregate, [dict(m8, value=1), inf])
    d11 = dict(ag11 or {}, varied="v", criterion="c", share_threshold=0.9, regret_threshold=0.5, dollars_threshold=0.9)
    ok(e13 is None and d11.get("triggered_by") == ["infeasible", "regret"]
       and MM._result_conflicts("t.yaml", d11, "x", None) == []
       and MM._result_conflicts("t.yaml", dict(d11, triggered_by=["infeasible"]), "x", None) != [],
       f"MV22 mixed feasible and infeasible values: the loader judges each value on its own ({e13!r}, "
       f"{MM._result_conflicts('t.yaml', d11, 'x', None)[:1]})")
    # Codex r3 P2-3: the absolute and the percentage regret maxima each keep their own value
    ga = dict(m or {}, value="A", regret=100.0, regret_share=0.1)
    gb = dict(m or {}, value="B", regret=1.0, regret_share=0.5)
    ag12, e14 = attempt(aggregate, [ga, gb])
    w12 = (ag12 or {}).get("worst_by_criterion") or {}
    ok(e14 is None and ag12["regret_share"] == 0.5 and ag12["regret"] == 100.0 and w12["regret"]["value"] == "B"
       and w12.get("regret_abs", {}).get("value") == "A",
       f"MV23 the percentage and the absolute regret maxima come from their own values ({e14!r}, {list(w12)})")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    print(__doc__)
