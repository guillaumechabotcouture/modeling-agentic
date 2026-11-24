"""Which plan the deliverable carries (spec `deliverable.plan`, hb1 fix 11).

`point` (the default, and what a spec without the key gets): the plan solved on the outcomes and costs at the point
values, as before. `expected_value`: the plan with the best mean benefit over the uncertainty draws, within the budget
in every draw. It is the same solve as the value-of-information step's `robust` candidate (`mean_plan`, shared by
both; both leave out the packages a point-value model correction bears on). The draws are the uncertainty step's own
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
    budget in every draw, leaving out the packages a model correction bears on in a draw or at the point values
    (`point_events`); its cost at the point costs `cost0` is recorded, with whether it is over the budget there (the
    point costs are not a draw). Returns {"solution", "record"}; the
    record (point.plan_rule) holds the point-value plan, the units where the two differ, both plans' mean benefit (the
    point-value plan's is None, with the reason, when a model correction bears on it in a draw) and each unit's mean
    outcome averted under the delivered plan. A correction on the comparator or a whole unit raises AnalysisBlocked
    (the run publishes analysis/blocked.yaml); a failed solve raises ValueError."""
    from toolkit.analysis.core import AnalysisBlocked
    if not rows:
        raise ValueError("deliverable.plan expected_value: no uncertainty draws to average over")
    Bn, Cs = tables(A, rows)
    evs = [r["events"] for r in rows]
    hard = [dict(e, draw=d) for d, ev in enumerate(evs) for e in ev if e.get("package") in ("*", A.comp)]
    hard += [dict(e, draw="point") for e in point_events if e.get("package") in ("*", A.comp)]
    if hard:
        raise AnalysisBlocked("expected-value plan", hard)
    try:
        sol = mean_plan(A, Bn, Cs, evs, cap_name="voi_draw", extra_events=point_events)
    except (RuntimeError, ValueError) as e:
        raise ValueError(f"deliverable.plan expected_value: the plan over the draws could not be solved ({e})") from e
    plan = dict(sol["choice"])
    at = {k: j for j, k in enumerate(A.pk)}
    ar = np.arange(len(A.ids))
    mean_of = (lambda p: float(Bn[:, ar, [at[p[i]] for i in A.ids]].sum(1).mean()))
    per_unit = {i: {o: float(np.mean([r["out"][i]["y"][A.comp][o] - r["out"][i]["y"][plan[i]][o] for r in rows]))
                    for o in A.outcomes} for i in A.ids}
    differ = [i for i in A.ids if plan[i] != point_plan[i]]
    pp_hits = [d for d, ev in enumerate(evs) if A.plan_corrections(ev, point_plan)]
    rec = {"rule": "expected_value", "draws": len(rows), "point_plan": dict(point_plan),
           "units_differ": differ, "n_units_differ": len(differ),
           "mean_benefit": mean_of(plan),
           "point_plan_mean_benefit": None if pp_hits else mean_of(point_plan),
           "per_unit_mean": per_unit,
           "point_cost": (pc := float(sum(cost0[i][plan[i]] for i in A.ids))),
           "point_cost_over_budget": bool(pc > float(A.budget) * (1 + 1e-9)),
           "note": "The plan with the best mean benefit over the uncertainty draws, within the budget in every draw "
                   "(deliverable.plan: expected_value). Per-unit values are means over the same "
                   "draws. Stability, sensitivity and the decision tests re-solve at the point values, so they compare "
                   "with the point-value plan (point_plan); the budget frontier figure is not drawn."}
    if pp_hits:
        rec["point_plan_mean_benefit_not_computed"] = (f"model corrections bear on the point-value plan in {len(pp_hits)} "
                                                       f"draws (e.g. draw {pp_hits[0]})")
    bg = (((getattr(A, "spec", None) and getattr(A.spec, "raw", None)) or {}).get("deliverable") or {}).get("by_group")
    if isinstance(bg, dict) and isinstance(bg.get("quantity"), dict):
        qmap = bg["quantity"]
        rec["per_unit_group_mean"] = {
            i: {g: {o: float(np.mean([
                float(r["out"][i]["y_group"][A.comp][o].get(g, 0.0))
                - float(r["out"][i]["y_group"][plan[i]][o].get(g, 0.0))
                for r in rows])) for o in A.outcomes} for g in qmap}
            for i in A.ids}
        rec["per_unit_group_quantity"] = {
            i: {g: float(np.mean([float(r["out"][i]["cost"][plan[i]][qname]) for r in rows]))
                for g, qname in qmap.items()}
            for i in A.ids}
    return {"solution": sol, "record": rec}


def contract_findings(raw: dict, point, unc) -> list[str]:
    """The completed run carries the plan rule its signed spec asks for, and its arithmetic (fail closed: a malformed
    record is a finding, never a crash)."""
    try:
        return _contract_findings(raw, point, unc)
    except Exception as e:  # noqa: BLE001 -- Codex r3 P2-5
        return [f"point.yaml's plan rule record is malformed ({type(e).__name__}: {e})"]


def reference_findings(point, dt_payload) -> list[str]:
    """The decision tests' reference plan (analysis/decision_tests.yaml `reference_plan`) agrees with the run's plan
    rule (point.yaml `plan_rule`): under expected_value they name the point-value plan, under point nothing (Codex r3
    P1-4). No artifact: nothing to check here (its absence is the contract's own finding)."""
    if not isinstance(dt_payload, dict):
        return []
    ev = isinstance(point, dict) and isinstance(point.get("plan_rule"), dict)
    got = dt_payload.get("reference_plan")
    if ev and got != "point_plan":
        return ["the decision tests do not name their reference plan (point_plan) in an expected-value run"]
    if not ev and got is not None:
        return [f"the decision tests name a reference plan ({got!r}) in a point-plan run"]
    return []


def _contract_findings(raw: dict, point, unc) -> list[str]:
    import math
    want = rule(raw)
    if want not in RULES:
        return [f"deliverable.plan {want!r} is not one of {RULES}"]
    if not isinstance(point, dict):
        return ["point.yaml is unreadable, so the plan rule cannot be checked"]
    exact = bool(((raw or {}).get("constraints") or {}).get("envelope_exact"))

    def _exact_findings() -> list[str]:
        if not exact:
            return []
        try:
            ca, ba = float(point["cost_allocated"]), float(point["budget_allocatable"])
            if not (math.isfinite(ca) and math.isfinite(ba)) or abs(ca - ba) > 1e-6 * max(1.0, abs(ba)):
                return [f"constraints.envelope_exact is true, but cost_allocated ({ca:g}) differs from "
                        f"budget_allocatable ({ba:g})"]
        except (KeyError, TypeError, ValueError):
            return ["constraints.envelope_exact is true, but point.yaml has no finite cost_allocated and "
                    "budget_allocatable"]
        return []

    pr = point.get("plan_rule")
    if want == "point":
        return ([] if pr is None else ["point.yaml records a plan rule the spec does not ask for"]) + _exact_findings()
    if not isinstance(pr, dict) or pr.get("rule") != "expected_value":
        return ["the spec asks for deliverable.plan: expected_value and point.yaml records no expected-value plan "
                "(a run by code that ignores the key?)"]
    bad = []
    hl = ((raw.get("deliverable") or {}).get("headline") or {}).get("outcome")
    pu, plan, pp = point.get("per_unit"), point.get("plan"), pr.get("point_plan")
    fp = ((unc or {}).get("fixed_plan") or {}).get(hl) if isinstance(unc, dict) else None
    try:
        vals = [float(v["averted_mean"][hl]) for v in pu.values()]
        mean = float(fp["mean"])
        n_ = max(1, len(vals))
        if not vals or not all(math.isfinite(x) for x in vals + [mean]) \
                or abs(math.fsum(x / n_ for x in vals) - mean / n_) > 1e-6 * max(1.0, abs(mean)) / n_:
            bad.append(f"the per-unit means of {hl} are not finite or do not add up to the fixed plan's mean over the "
                       "draws")
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        bad.append(f"point.yaml has no per-unit mean of {hl}, or uncertainty.yaml no fixed-plan mean")
    pc, over = pr.get("point_cost"), pr.get("point_cost_over_budget")
    try:
        ca, ba = float(point["cost_allocated"]), float(point["budget_allocatable"])
        if not (isinstance(pc, (int, float)) and not isinstance(pc, bool) and isinstance(over, bool)
                and abs(float(pc) - ca) <= 1e-6 * max(1.0, abs(ca)) and over == (ca > ba * (1 + 1e-9))):
            bad.append("plan_rule.point_cost and point_cost_over_budget are missing or disagree with the plan's cost "
                       "at the point values (a record from another rule?)")
    except (KeyError, TypeError, ValueError):
        bad.append("point.yaml has no cost_allocated or budget_allocatable to check the plan rule's point cost")
    bad += _exact_findings()
    if not (isinstance(plan, dict) and isinstance(pp, dict) and set(pp) == set(plan)):
        bad.append("plan_rule.point_plan does not name the delivered plan's units (the decision tests' reference plan)")
    elif not isinstance(pr.get("units_differ"), list) or sorted(map(str, pr["units_differ"])) != sorted(str(i) for i in plan if plan[i] != pp[i]) \
            or pr.get("n_units_differ") != sum(plan[i] != pp[i] for i in plan):
        bad.append("plan_rule.units_differ does not match the delivered and point-value plans")
    if isinstance(unc, dict) and pr.get("draws") != unc.get("draws"):
        bad.append("the expected-value plan was solved over a different number of draws than the uncertainty step's")
    bg = (raw.get("deliverable") or {}).get("by_group") if isinstance(raw.get("deliverable"), dict) else None
    if isinstance(bg, dict) and isinstance(bg.get("quantity"), dict):
        pgm, pgq = pr.get("per_unit_group_mean"), pr.get("per_unit_group_quantity")
        groups = list(bg["quantity"])
        try:
            if not (isinstance(pgm, dict) and isinstance(pgq, dict) and isinstance(pu, dict)
                    and set(pgm) == set(pu) and set(pgq) == set(pu)):
                raise ValueError("missing per_unit_group_mean or per_unit_group_quantity")
            for u_id, puv in pu.items():
                tot = float(puv["averted_mean"][hl])
                gvals = [float(pgm[u_id][g][hl]) for g in groups]
                qvals = [float(pgq[u_id][g]) for g in groups]
                if not (all(math.isfinite(x) for x in gvals + qvals + [tot])
                        and abs(math.fsum(gvals) - tot) <= 1e-6 * max(1.0, abs(tot))):
                    raise ValueError(f"unit {u_id} group means do not sum to averted_mean")
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError) as e:
            bad.append(f"deliverable.by_group under expected_value: plan_rule.per_unit_group_mean / "
                       f"per_unit_group_quantity missing, non-finite, or inconsistent ({e})")
    return bad


# --------------------------------------------------------------------------- self-test
class _FakeA:
    """Two units, packages none/a, a budget for one `a`: solves by enumeration under every cap."""

    def __init__(self, budget=1.0):
        self.ids, self.pk, self.comp, self.outcomes = ["u1", "u2"], {"none": [], "a": ["a"]}, "none", ["cases"]
        self.budget, self.feas, self.solves = budget, {"caps": [], "feasible": {u: {"none": True, "a": True} for u in self.ids}}, []

    def plan_corrections(self, events, plan):
        return [e for e in events if e["package"] in ("*", self.comp) or plan.get(e["unit"]) == e["package"]]

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
    # the point costs are not a draw: the plan is the robust one; its cost there is recorded, flagged when over
    hi = {"u1": {"none": 0.0, "a": 1.0}, "u2": {"none": 0.0, "a": 5.0}}
    r3 = expected_value_plan(_FakeA(), rows, point, hi)
    ok(r3["solution"]["choice"] == {"u1": "none", "u2": "a"} and r3["record"]["point_cost"] == 5.0
       and r3["record"]["point_cost_over_budget"] is True and rec["point_cost_over_budget"] is False,
       f"PR5 the plan is the same solve as the VOI robust candidate (budget in every draw); its cost at the point "
       f"costs is recorded and flagged when over the budget ({r3['record'].get('point_cost')})")
    # model corrections: a corrected package is left out; one on the comparator fails closed
    A4 = _FakeA()
    r4 = expected_value_plan(A4, [_row(10.0, 8.0), _row(0.0, 8.0, events=[{"unit": "u2", "package": "a"}])], point,
                             cost0)
    ok(r4["solution"]["choice"]["u2"] == "none", f"PR6 a package a correction bears on is left out "
                                                  f"({r4['solution']['choice']})")
    r4b = expected_value_plan(_FakeA(), rows, point, cost0, point_events=[{"unit": "u2", "package": "a"}])
    ok(r4b["solution"]["choice"]["u2"] == "none", f"PR6b a package a point-value correction bears on is left out "
                                                   f"({r4b['solution']['choice']})")
    from toolkit.analysis.core import AnalysisBlocked
    for ev, pe, why in (([{"unit": "u1", "package": "none"}], (), "comparator in a draw"),
                        ([{"unit": "u2", "package": "*"}], (), "whole unit in a draw"),
                        ([], [{"unit": "u1", "package": "none"}], "comparator at the point values")):
        try:
            expected_value_plan(_FakeA(), [_row(1.0, 1.0, events=ev)], point, cost0, point_events=pe)
            ok(False, f"PR7 a correction on the comparator or a whole unit blocks the run: {why}")
        except AnalysisBlocked as e:
            ok(e.where == "expected-value plan" and len(e.hits) == 1,
               f"PR7 a correction on the comparator or a whole unit blocks the run (analysis/blocked.yaml, Codex r1 "
               f"P1-9): {why} ({e.hits})")
    try:
        expected_value_plan(_FakeA(), [], point, cost0)
        ok(False, "PR7b no draws fails closed")
    except ValueError as e:
        ok("deliverable.plan expected_value" in str(e), f"PR7b no draws fails closed ({e})")
    try:
        expected_value_plan(_FakeA(budget=-1.0), rows, point, cost0)
        ok(False, "PR8 an unsolvable plan fails closed")
    except ValueError as e:
        ok("could not be solved" in str(e), f"PR8 an unsolvable plan fails closed ({e})")
    # Codex r1 P1-6: a correction on the point-value plan in a draw: its mean is not published
    r9 = expected_value_plan(_FakeA(), [_row(10.0, 8.0), _row(0.0, 8.0, events=[{"unit": "u1", "package": "a"}])],
                             point, cost0)["record"]
    ok(r9["point_plan_mean_benefit"] is None and "draw 1" in r9.get("point_plan_mean_benefit_not_computed", "")
       and r9["mean_benefit"] == 8.0,
       f"PR9 the point-value plan's mean is not published when a correction bears on it in a draw ({r9})")
    # Codex r2 P1-2: the VOI robust solve is mean_plan with the same point-value corrections: the same plan
    A10 = _FakeA()
    pe = [{"unit": "u2", "package": "a"}]
    Bn, Cs = tables(A10, rows)
    ok(mean_plan(A10, Bn, Cs, [r["events"] for r in rows], extra_events=pe)["choice"]
       == expected_value_plan(_FakeA(), rows, point, cost0, point_events=pe)["solution"]["choice"],
       "PR10 the VOI robust solve (mean_plan with the point corrections) is the delivered plan")
    # Codex r1 P1-10: the completion gate checks the plan rule the signed spec asks for
    ev_raw = {"deliverable": {"plan": "expected_value", "headline": {"outcome": "cases"}}}
    good_pt = {"plan": {"u1": "none", "u2": "a"}, "cost_allocated": 1.0, "budget_allocatable": 1.0,
               "plan_rule": {"rule": "expected_value", "draws": 2, "point_plan": {"u1": "a", "u2": "none"},
                             "units_differ": ["u1", "u2"], "n_units_differ": 2, "point_cost": 1.0,
                             "point_cost_over_budget": False},
               "per_unit": {"u1": {"averted_mean": {"cases": 0.0}}, "u2": {"averted_mean": {"cases": 8.0}}}}
    no_pp = dict(good_pt, plan_rule={k: v for k, v in good_pt["plan_rule"].items() if k != "point_plan"})
    bad_diff = dict(good_pt, plan_rule=dict(good_pt["plan_rule"], units_differ=["u1"]))
    nan_pt = dict(good_pt, per_unit={"u1": {"averted_mean": {"cases": float("nan")}},
                                     "u2": {"averted_mean": {"cases": 8.0}}})
    unc = {"draws": 2, "fixed_plan": {"cases": {"mean": 8.0}}}
    cases = {"good": (ev_raw, good_pt, unc, 0), "old runner": (ev_raw, {"per_unit": {}}, unc, 1),
             "sum off": (ev_raw, dict(good_pt, per_unit={"u1": {"averted_mean": {"cases": 1.0}}}), unc, 1),
             "draws off": (ev_raw, good_pt, dict(unc, draws=3), 1),
             "point rule, no record": ({}, {"per_unit": {}}, unc, 0),
             "point rule, a record": ({}, good_pt, unc, 1), "unreadable point": (ev_raw, None, unc, 1),
             "no point_plan": (ev_raw, no_pp, unc, 1), "units_differ off": (ev_raw, bad_diff, unc, 1),
             "a NaN mean": (ev_raw, nan_pt, unc, 1), "per_unit a list": (ev_raw, dict(good_pt, per_unit=[1]), unc, 1),
             "no point_cost (an earlier rule)": (ev_raw, dict(good_pt, plan_rule={
                 k: v for k, v in good_pt["plan_rule"].items() if k != "point_cost"}), unc, 1),
             "point_cost off": (ev_raw, dict(good_pt, cost_allocated=0.5), unc, 1),
             "over flag off": (ev_raw, dict(good_pt, plan_rule=dict(good_pt["plan_rule"], point_cost_over_budget=True)),
                               unc, 1),
             "units_differ an integer": (ev_raw, dict(good_pt, plan_rule=dict(good_pt["plan_rule"], units_differ=1)),
                                         unc, 1),
             "means near the largest float": (ev_raw, dict(good_pt, per_unit={
                 "u1": {"averted_mean": {"cases": 1e308}}, "u2": {"averted_mean": {"cases": 1e308}}}), unc, 1)}
    got = {}
    for k, (r_, p_, u_, _n) in cases.items():
        try:
            got[k] = len(contract_findings(r_, p_, u_))
        except Exception as e:  # noqa: BLE001 -- a crash is a failing case (Codex r3 P2-5)
            got[k] = f"{type(e).__name__}"
    ok(got == {k: v[3] for k, v in cases.items()}, f"PR11 contract findings for the plan rule ({got})")
    # Codex r3 P1-4: the decision tests' reference plan agrees with the run's plan rule
    ref = {"ev, point_plan": (good_pt, {"reference_plan": "point_plan", "tests": [{}]}, 0),
           "ev, no reference": (good_pt, {"tests": [{}]}, 1),
           "point, a reference": ({"plan": {}}, {"reference_plan": "point_plan", "tests": []}, 1),
           "point, none": ({"plan": {}}, {"tests": [{}]}, 0), "no artifact": (good_pt, None, 0)}
    try:
        gr = {k: len(reference_findings(p_, d_)) for k, (p_, d_, _n) in ref.items()}
    except Exception as e:  # noqa: BLE001 -- before the check exists
        gr = {"error": f"{type(e).__name__}: {e}"}
    ok(gr == {k: v[2] for k, v in ref.items()}, f"PR12 the decision tests' reference plan agrees with the plan rule "
                                                f"({gr})")
    # PR13: expected_value with deliverable.by_group records per-group means and quantities
    A_bg = _FakeA()
    A_bg.spec = type("_S", (), {"raw": {"deliverable": {"plan": "expected_value", "headline": {"outcome": "cases"},
                                                        "by_group": {"columns": {"group": "g", "quantity": "q",
                                                                                 "value": "v"},
                                                                     "quantity": {"g1": "d1", "g2": "d2"}}}}})()
    rows_bg = []
    for r_ in [_row(10.0, 8.0), _row(0.0, 8.0)]:
        rb_ = {**r_, "out": {i: {**v,
                                 "y_group": {k: {"cases": {"g1": 0.25 * yv["cases"], "g2": 0.75 * yv["cases"]}}
                                             for k, yv in v["y"].items()},
                                 "cost": {k: {"d1": 1.0 if k == "a" else 0.0, "d2": 2.0 if k == "a" else 0.0}
                                          for k in v["y"]}}
                             for i, v in r_["out"].items()}}
        rows_bg.append(rb_)
    r_bg = expected_value_plan(A_bg, rows_bg, point, cost0)["record"]
    pgm = r_bg.get("per_unit_group_mean") or {}
    pgq = r_bg.get("per_unit_group_quantity") or {}
    ok(pgm == {"u1": {"g1": {"cases": 0.0}, "g2": {"cases": 0.0}},
               "u2": {"g1": {"cases": 2.0}, "g2": {"cases": 6.0}}}
       and pgq == {"u1": {"g1": 0.0, "g2": 0.0}, "u2": {"g1": 1.0, "g2": 2.0}},
       f"PR13 expected_value with by_group computes per-group means summing to per_unit_mean ({pgm}, {pgq})")
    bg_raw = {"deliverable": {"plan": "expected_value", "headline": {"outcome": "cases"},
                              "by_group": {"columns": {"group": "g", "quantity": "q", "value": "v"},
                                           "quantity": {"g1": "d1", "g2": "d2"}}}}
    bg_pt = dict(good_pt, plan_rule=dict(good_pt["plan_rule"], per_unit_group_mean=pgm, per_unit_group_quantity=pgq))
    bg_bad = dict(good_pt, plan_rule=dict(good_pt["plan_rule"], per_unit_group_mean={
        "u1": {"g1": {"cases": 0.0}, "g2": {"cases": 0.0}}, "u2": {"g1": {"cases": 1.0}, "g2": {"cases": 1.0}}}))
    ok(contract_findings(bg_raw, bg_pt, unc) == [] and len(contract_findings(bg_raw, good_pt, unc)) >= 1
       and len(contract_findings(bg_raw, bg_bad, unc)) >= 1,
       f"PR13b contract_findings verifies by_group per-group means ({contract_findings(bg_raw, bg_pt, unc)}, "
       f"{contract_findings(bg_raw, bg_bad, unc)})")
    # PR14: constraints.envelope_exact: true checked at contract gate on point & expected_value
    ex_pt_raw = {"constraints": {"envelope_exact": True}}
    ex_ev_raw = dict(ev_raw, constraints={"envelope_exact": True})
    pt_ok = {"cost_allocated": 11.0, "budget_allocatable": 11.0}
    pt_short = {"cost_allocated": 8.0, "budget_allocatable": 11.0}
    ok(contract_findings(ex_pt_raw, pt_ok, unc) == [] and len(contract_findings(ex_pt_raw, pt_short, unc)) == 1
       and contract_findings(ex_ev_raw, good_pt, unc) == []
       and len(contract_findings(ex_ev_raw, dict(good_pt, cost_allocated=0.5,
                                                plan_rule=dict(good_pt["plan_rule"], point_cost=0.5)), unc)) == 1,
       "PR14 contract_findings checks constraints.envelope_exact on both point and expected_value plans")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    print(__doc__)
