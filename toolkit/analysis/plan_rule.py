"""Which plan the deliverable carries (spec `deliverable.plan`, hb1 fix 11).

`point` (the default, and what a spec without the key gets): the plan solved on the outcomes and costs at the point
values, as before. `expected_value`: the plan with the best mean benefit over the uncertainty draws, within the budget
in every draw and at the point values. It is the same solve as the value-of-information step's `robust` candidate
(`mean_plan`, shared by both), with the point costs as one more budget row. The draws are the uncertainty step's own
(evaluated once and reused by it), so the plan, its per-unit means and the fixed-plan mean claim describe the same
draws.

A question that asks for the options that maximise the expected outcome over an uncertain input is answered by
`expected_value`; the point plan answers it only when the outcome is linear in that input.
"""
from __future__ import annotations

import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

PRODUCER = "toolkit/analysis/plan_rule.py"
RULES = ("point", "expected_value")


def rule(raw: dict) -> str:
    """The spec's plan rule: `deliverable.plan`, `point` when absent."""
    return ((raw or {}).get("deliverable") or {}).get("plan") or "point"


def tables(A, rows) -> tuple:
    """Each draw's benefit and cost tables as arrays [draw, unit, package] in A.ids x A.pk order."""
    pk = list(A.pk)
    Bn = np.array([[[float(r["ben"][i][k]) for k in pk] for i in A.ids] for r in rows], float)
    Cs = np.array([[[float(r["cost"][i][k]) for k in pk] for i in A.ids] for r in rows], float)
    return Bn, Cs


def mean_plan(A, Bn, Cs, events, mask=None, budget=None, caps=(), cap_name="voi_draw", extra_events=()):
    """The solve with the best mean benefit over the masked draws within `budget` (A.budget when None) in each of them:
    one budget row per draw beside the signed caps and any extra `caps`. Packages a model correction bears on in these
    draws are left out first. None when a correction bears on the comparator or a whole unit; a failed solve raises
    (RuntimeError or ValueError from the allocator). `events[d]` is draw d's model corrections; `extra_events` are
    corrections from outside the draws (the point evaluation's) that bear the same way."""
    n = Bn.shape[0]
    mask = np.ones(n, bool) if mask is None else np.asarray(mask, bool)
    B = float(A.budget if budget is None else budget)
    pk = list(A.pk)
    tab = (lambda M: {i: {k: float(M[u, j]) for j, k in enumerate(pk)} for u, i in enumerate(A.ids)})
    rows = list(A.feas.get("caps") or []) + list(caps) + [{"name": f"{cap_name}_{d}", "max": B, "weights": tab(Cs[d])}
                                                          for d in np.flatnonzero(mask)]
    f = dict(A.feas, caps=rows, feasible={u: dict(v) for u, v in A.feas["feasible"].items()})
    for e in [e for d in np.flatnonzero(mask) for e in events[d]] + list(extra_events):
        if e.get("package") in ("*", getattr(A, "comp", None)):
            return None
        if e.get("package") in f["feasible"].get(str(e.get("unit")), {}):
            f["feasible"][str(e["unit"])][e["package"]] = False
    return A.solve(tab(Bn[mask].mean(0)), tab(Cs[mask].mean(0)), budget=B, feas=f)


def expected_value_plan(A, rows, point_plan: dict, cost0, point_events=()) -> dict:
    """The plan with the best mean benefit over the uncertainty draws `rows` (UQ.evaluate_draws' rows), within the
    budget in every draw and at the point costs `cost0`, leaving out the packages a model correction bears on in a draw
    or at the point values (`point_events`). Returns {"solution", "record"}; the record (point.plan_rule)
    holds the point-value plan, the units where the two differ, both plans' mean benefit and each unit's mean outcome
    averted under the delivered plan. Fails closed (ValueError) when no plan can be solved."""
    if not rows:
        raise ValueError("deliverable.plan expected_value: no uncertainty draws to average over")
    Bn, Cs = tables(A, rows)
    pc = [{"name": "point_values", "max": float(A.budget),
           "weights": {i: {k: float(cost0[i][k]) for k in A.pk} for i in A.ids}}]
    try:
        sol = mean_plan(A, Bn, Cs, [r["events"] for r in rows], caps=pc, cap_name="draw",
                        extra_events=point_events)
    except (RuntimeError, ValueError) as e:
        raise ValueError(f"deliverable.plan expected_value: the plan over the draws could not be solved ({e})") from e
    if sol is None:
        raise ValueError("deliverable.plan expected_value: a model correction bears on the comparator or a whole unit "
                         "in a draw, so no plan over the draws can be guarded")
    plan = dict(sol["choice"])
    at = {k: j for j, k in enumerate(A.pk)}
    ar = np.arange(len(A.ids))
    mean_of = (lambda p: float(Bn[:, ar, [at[p[i]] for i in A.ids]].sum(1).mean()))
    per_unit = {i: {o: float(np.mean([r["out"][i]["y"][A.comp][o] - r["out"][i]["y"][plan[i]][o] for r in rows]))
                    for o in A.outcomes} for i in A.ids}
    differ = [i for i in A.ids if plan[i] != point_plan[i]]
    rec = {"rule": "expected_value", "draws": len(rows), "point_plan": dict(point_plan),
           "units_differ": differ, "n_units_differ": len(differ),
           "mean_benefit": mean_of(plan), "point_plan_mean_benefit": mean_of(point_plan),
           "per_unit_mean": per_unit,
           "note": "The plan with the best mean benefit over the uncertainty draws, within the budget in every draw and "
                   "at the point values (deliverable.plan: expected_value). Per-unit values are means over the same "
                   "draws. Stability, sensitivity and the decision tests re-solve at the point values, so they compare "
                   "with the point-value plan (point_plan)."}
    return {"solution": sol, "record": rec}


# --------------------------------------------------------------------------- self-test
class _FakeA:
    """Two units, packages none/a, a budget for one `a`: solves by enumeration under every cap."""

    def __init__(self, budget=1.0):
        self.ids, self.pk, self.comp, self.outcomes = ["u1", "u2"], {"none": [], "a": ["a"]}, "none", ["cases"]
        self.budget, self.feas, self.solves = budget, {"caps": [], "feasible": {u: {"none": True, "a": True} for u in self.ids}}, []

    def solve(self, ben, cost, budget=None, feas=None):
        import itertools
        f = feas or self.feas
        self.solves.append(f)
        best = None
        for combo in itertools.product(list(self.pk), repeat=len(self.ids)):
            p = dict(zip(self.ids, combo))
            if any(f["feasible"].get(i, {}).get(p[i]) is False for i in self.ids):
                continue
            if sum(cost[i][p[i]] for i in self.ids) > budget + 1e-12:
                continue
            if any(sum(c["weights"][i][p[i]] for i in self.ids) > c["max"] + 1e-12 for c in f["caps"]):
                continue
            v = sum(ben[i][p[i]] for i in self.ids)
            if best is None or v > best[0] + 1e-12:
                best = (v, p)
        if best is None:
            raise RuntimeError("infeasible")
        return {"choice": best[1], "objective": best[0], "status": "optimal", "method": "enumeration",
                "cost": sum(cost[i][best[1][i]] for i in self.ids)}


def _row(b1, b2, c=1.0, events=()):
    """A draw: the benefit of `a` in each unit (equal to the cases it averts), cost c for `a`."""
    ben = {"u1": {"none": 0.0, "a": b1}, "u2": {"none": 0.0, "a": b2}}
    cost = {"u1": {"none": 0.0, "a": c}, "u2": {"none": 0.0, "a": c}}
    out = {i: {"y": {"none": {"cases": 100.0}, "a": {"cases": 100.0 - ben[i]["a"]}}} for i in ben}
    return {"ben": ben, "cost": cost, "out": out, "events": list(events)}


def _self_test() -> int:
    fails = []

    def ok(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    ok(rule({}) == "point" and rule({"deliverable": {}}) == "point"
       and rule({"deliverable": {"plan": "expected_value"}}) == "expected_value",
       "PR1 the rule is deliverable.plan, point when absent")
    # at the point values u1 is better (10 vs 8); over the draws u2 is (mean 8 vs 5)
    A = _FakeA()
    rows = [_row(10.0, 8.0), _row(0.0, 8.0)]
    cost0 = rows[0]["cost"]
    point = A.solve(rows[0]["ben"], cost0, budget=1.0)["choice"]
    r = expected_value_plan(A, rows, point, cost0)
    rec = r["record"]
    ok(point == {"u1": "a", "u2": "none"} and r["solution"]["choice"] == {"u1": "none", "u2": "a"}
       and rec["units_differ"] == ["u1", "u2"] and rec["mean_benefit"] == 8.0 and rec["point_plan_mean_benefit"] == 5.0,
       f"PR2 the plan with the best mean over the draws is delivered, not the point-value plan ({rec})")
    ok(rec["per_unit_mean"] == {"u1": {"cases": 0.0}, "u2": {"cases": 8.0}}
       and abs(sum(v["cases"] for v in rec["per_unit_mean"].values()) - rec["mean_benefit"]) < 1e-12,
       f"PR3 per-unit means of the outcome averted under the delivered plan ({rec['per_unit_mean']})")
    # a draw where `a` costs 2 in u2 makes u2 unaffordable in that draw: the plan must fit the budget in every draw
    A2 = _FakeA()
    rows2 = [_row(10.0, 8.0), dict(_row(0.0, 8.0), cost={"u1": {"none": 0.0, "a": 1.0}, "u2": {"none": 0.0, "a": 2.0}})]
    r2 = expected_value_plan(A2, rows2, point, cost0)
    ok(r2["solution"]["choice"] == {"u1": "a", "u2": "none"},
       f"PR4 the plan fits the budget in every draw, not only on mean costs ({r2['solution']['choice']})")
    # the point costs are one more budget row
    A3 = _FakeA()
    hi = {"u1": {"none": 0.0, "a": 1.0}, "u2": {"none": 0.0, "a": 5.0}}
    r3 = expected_value_plan(A3, rows, point, hi)
    ok(r3["solution"]["choice"] == {"u1": "a", "u2": "none"},
       f"PR5 the plan fits the budget at the point costs too ({r3['solution']['choice']})")
    # model corrections: a corrected package is left out; one on the comparator fails closed
    A4 = _FakeA()
    r4 = expected_value_plan(A4, [_row(10.0, 8.0), _row(0.0, 8.0, events=[{"unit": "u2", "package": "a"}])], point,
                             cost0)
    ok(r4["solution"]["choice"]["u2"] == "none", f"PR6 a package a correction bears on is left out "
                                                  f"({r4['solution']['choice']})")
    r4b = expected_value_plan(_FakeA(), rows, point, cost0, point_events=[{"unit": "u2", "package": "a"}])
    ok(r4b["solution"]["choice"]["u2"] == "none", f"PR6b a package a point-value correction bears on is left out "
                                                   f"({r4b['solution']['choice']})")
    for ev, why in (([{"unit": "u1", "package": "none"}], "comparator"), ([], "no draws")):
        try:
            expected_value_plan(_FakeA(), [] if why == "no draws" else [_row(1.0, 1.0, events=ev)], point, cost0)
            ok(False, f"PR7 fails closed: {why}")
        except ValueError as e:
            ok("deliverable.plan expected_value" in str(e), f"PR7 fails closed: {why} ({e})")
    try:
        expected_value_plan(_FakeA(budget=-1.0), rows, point, cost0)
        ok(False, "PR8 an unsolvable plan fails closed")
    except ValueError as e:
        ok("could not be solved" in str(e), f"PR8 an unsolvable plan fails closed ({e})")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    print(__doc__)
