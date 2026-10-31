"""Decision tests (spike lesson 8): does the plan change when a quantity is varied?

Types (spec `analysis.decision_tests`):
  interaction_rescale  the spike's DT1: the increment of `components` on top of any of `with_any`
                       is rescaled so the engine's interaction odds ratio in the benchmark setting
                       equals each value (arm odds ratios from the engine card's benchmark, or
                       `engine_arm_or` in the spec);
  parameter_value      an engine parameter set to each value (an emulator input on the emulator path);
  group_rule           a constraints.group_rules rule removed (or `add:` a rule) and the plan re-solved;
  envelope             the plan re-solved at each envelope.
Each writes `models/decision_tests/<id>.yaml` in the Phase 49 result schema (finding, varied,
criterion, decision_changes, recommendation.with / without, share_units_changed,
share_threshold) with `produced_by: harness`.

Relevance, up front: when a test changes only benefits, and every benefit it changes goes down and
belongs to a package no unit receives in the point plan, the point plan stays optimal (its value is
unchanged and no alternative improves). The file then says first that the test cannot move the
plan, and whether the components it acts on appear in any re-optimised uncertainty draw.
"""
from __future__ import annotations

import copy
import math
import os

import numpy as np
import yaml

from toolkit.alloc import packages as apackages
from toolkit.analysis.artifacts import plain

PRODUCER = "toolkit/analysis/decision_tests.py"
DIR = os.path.join("models", "decision_tests")


def _threshold(A) -> float:
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "thresholds.yaml")) as f:
        default = yaml.safe_load(f)["decision_tests"]["share_threshold"]
    return float(A.spec["analysis"].get("share_threshold", default))


def _lowers_only_unchosen(ben0, ben, plan) -> bool:
    for i in ben0:
        for k, v in ben0[i].items():
            if ben[i][k] != v and (ben[i][k] > v or plan[i] == k):
                return False
    return True


def run_all(A, z0, out0, ben0, cost0, plan, uq_data, run_dir) -> list[dict]:
    os.makedirs(os.path.join(run_dir, DIR), exist_ok=True)
    out = []
    for dt in A.spec["analysis"].get("decision_tests") or []:
        rec = run_one(A, dt, z0, ben0, cost0, plan, uq_data)
        p = os.path.join(run_dir, DIR, f"{dt['id']}.yaml")
        with open(p, "w") as f:
            yaml.safe_dump(plain(rec), f, sort_keys=False, width=120)
        rec["path"] = os.path.relpath(p, run_dir)
        out.append(rec)
    return out


def run_one(A, dt, z0, ben0, cost0, plan, uq_data) -> dict:
    t = dt["type"]
    thr = _threshold(A)
    cases, varied, lowers = [], "", []
    if t == "interaction_rescale":
        varied, cases, lowers, extra = _interaction(A, dt, ben0, cost0, plan)
    elif t == "parameter_value":
        p = dt["parameter"]
        if A.model.kind == "emulator" and p not in A.fr:
            raise ValueError(f"decision test {dt['id']}: {p} is not an emulator input; use direct evaluation")
        varied = f"engine parameter {p} set to {dt['values']} (point value {A.theta0.get(p)})"
        extra = {}
        for v in dt["values"]:
            theta = dict(A.theta0, **{p: float(v)})
            zs = {i: x[0] for i, x in A.calibrate(theta).items()} if A.method == "emulator_inversion" else z0
            _, ben, cost = A.evaluate(theta, zs)
            same_cost = all(abs(cost[i][k] - cost0[i][k]) <= 1e-9 * max(1.0, abs(cost0[i][k])) for i in A.ids for k in A.pk)
            lowers.append(same_cost and _lowers_only_unchosen(ben0, ben, plan))
            cases.append((v, ben, cost, None, None))
    elif t == "group_rule":
        a = copy.deepcopy(A.alloc)
        rules = dict(a.get("group_rules") or {})
        if dt.get("add"):
            add = dict(dt["add"])
            rules[add.pop("id", dt["id"])] = add
            varied = f"group rule added: {dt['add']}"
        else:
            rules.pop(dt["rule"], None)
            varied = f"group rule {dt['rule']} removed"
        a["group_rules"] = rules
        if not rules:
            a.pop("group_rules")
        feas = apackages.feasibility(a, A.spec.units())
        cases = [("with" if dt.get("add") else "without", ben0, cost0, None, feas)]
        lowers = [False]
        extra = {}
    elif t == "envelope":
        varied = f"envelope set to {dt['values']} USD (point {A.spec['constraints']['envelope_usd']})"
        fixed = sum(float(v) for v in (A.spec["constraints"].get("fixed_costs_usd") or {}).values())
        cases = [(v, ben0, cost0, float(v) - fixed, None) for v in dt["values"]]
        lowers = [False] * len(cases)
        extra = {}
    else:
        raise ValueError(f"decision test {dt.get('id')}: unknown type {t}")
    results = []
    for (v, ben, cost, budget, feas), low in zip(cases, lowers):
        sol = A.solve(ben, cost, budget=budget, feas=feas)
        ch = sol["choice"]
        n_changed = sum(ch[i] != plan[i] for i in A.ids)
        results.append({"value": v, "units_changed": n_changed, "share_units_changed": n_changed / len(A.ids),
                        "objective": sol["objective"], "point_plan_objective_under_test":
                            sum(ben[i][plan[i]] for i in A.ids), "recommendation": A.mix_text(ch),
                        "provably_unchanged": bool(low), **(extra.get(v) or {})})
    worst = max(results, key=lambda r: r["share_units_changed"])
    share = worst["share_units_changed"]
    comps = set(dt.get("components") or [])
    in_draws = sorted(comps & set(uq_data.get("components_in_any_draw_plan") or [])) if comps else None
    in_plan = sum(bool(A.pk[plan[i]] & comps) for i in A.ids) if comps else None
    if all(r["provably_unchanged"] for r in results):
        stmt = ("This test cannot move the plan: it only lowers the benefit of packages that no unit receives in the "
                "point plan, so the point plan stays optimal")
        if comps:
            stmt += (f"; {', '.join(sorted(comps))} is in {in_plan} units of the point plan and in "
                     f"{'some' if in_draws else 'none'} of the {uq_data.get('draws')} re-optimised uncertainty draws")
        can_move = False
    else:
        stmt = "This test can move the plan: it was re-solved for each value"
        can_move = True
    finding = dt.get("finding") or f"decision_test:{dt['id']}"
    rec = {"statement": stmt + ".", "can_move_plan": can_move, "finding": finding, "test_id": dt["id"],
           "type": t, "question": dt.get("text", ""), "varied": varied,
           "criterion": f"the package changes in more than {thr:.0%} of units",
           "decision_changes": bool(share > thr), "share_units_changed": float(share), "share_threshold": thr,
           "recommendation": {"with": A.mix_text(plan), "without": worst["recommendation"]},
           "results": results, "produced_by": "harness", "producer": PRODUCER}
    if comps:
        rec["relevance"] = {"components": sorted(comps), "units_with_component_point_plan": in_plan,
                            "components_in_any_draw_plan": in_draws}
    if t == "interaction_rescale":
        rec["engine_arm_or"], rec["engine_interaction"] = extra.get("_arm"), extra.get("_int")
    return rec


def _interaction(A, dt, ben0, cost0, plan):
    arm = dt.get("engine_arm_or")
    if arm is None:
        rows = {b["id"]: b for b in (A.eng.describe().benchmarks or [])}
        b = rows.get(dt.get("benchmark"))
        if not b or not b.get("arm_or"):
            raise ValueError(f"decision test {dt['id']}: benchmark {dt.get('benchmark')} with arm odds ratios is not "
                             "in the engine card")
        arm = {k: float(np.exp(np.mean([np.log(x[k]) for x in b["arm_or"]]))) for k in ("a11", "a10", "a01")}
    eng_int = arm["a11"] / (arm["a10"] * arm["a01"])
    comp, with_any = set(dt["components"]), set(dt["with_any"])
    cases, lowers, extra = [], [], {"_arm": arm, "_int": eng_int}
    for v in dt["values"]:
        num = math.log(v * arm["a01"])
        den = math.log(arm["a11"] / arm["a10"])
        gfac = float(np.clip(num / den, 0.0, 5.0)) if den < 0 else 5.0
        ben = {}
        for i in A.ids:
            ben[i] = {}
            for k, cs in A.pk.items():
                if comp <= cs and cs & with_any:
                    base = next(n for n, c2 in A.pk.items() if c2 == cs - comp)
                    ben[i][k] = ben0[i][base] + gfac * (ben0[i][k] - ben0[i][base])
                else:
                    ben[i][k] = ben0[i][k]
        cases.append((v, ben, cost0, None, None))
        lowers.append(_lowers_only_unchosen(ben0, ben, plan))
        extra[v] = {"increment_scale": gfac}
    varied = (f"the increment of {sorted(comp)} given any of {sorted(with_any)} rescaled so the engine's interaction "
              f"odds ratio in the {dt.get('benchmark', 'benchmark')} setting ({eng_int:.3g}) equals {dt['values']}")
    return varied, cases, lowers, extra
