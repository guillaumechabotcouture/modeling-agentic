"""One-at-a-time sensitivity of the decision (design §2).

Each parameter is set to each end of its range, the others at their point values:
  fitted engine parameters   the engine-card range (emulator inputs on the emulator path);
  prior parameters           their declared prior range (drawn, so emulator inputs too);
  calibrated parameters      estimate -/+ 1.96 standard errors (likelihood calibration);
  cost items                 `analysis.sensitivity.cost_items: {id: [lo, hi]}` multipliers.
For each end: the point plan's objective, the re-optimised objective, and the share of units
whose package changes. Rows are sorted by the swing of the point plan's objective (a tornado).
On the emulator path a changed parameter re-runs the calibration, as the uncertainty draws do.
"""
from __future__ import annotations

import math

PRODUCER = "toolkit/analysis/sensitivity.py"


def _ranges(A) -> dict:
    sens = A.spec["analysis"].get("sensitivity") or {}
    sel = sens.get("parameters", "fitted")
    out = {}
    for k, (lo, hi, _v) in A.fr.items():
        out[k] = (lo, hi, "engine-card range")
    for k, p in A.pr.items():                    # v2 priors: drawn, so emulator inputs
        out[k] = (p.lo, p.hi, "declared prior range")
    if A.method == "likelihood":
        se = {p: v.get("se") for p, v in (A.lik["identifiability"].get("parameters") or {}).items()}
        if not se and A.lik.get("cov") is not None:
            se = {n: math.sqrt(A.lik["cov"][j][j]) for j, n in enumerate(A.lik["names"]) if n in A.lik["params"]}
        for p in A.lik["params"]:
            if se.get(p):
                out[p] = (A.theta0[p] - 1.96 * se[p], A.theta0[p] + 1.96 * se[p], "estimate +/- 1.96 SE")
    if isinstance(sel, list):
        bad = [p for p in sel if p not in out]
        if bad and A.model.kind == "emulator":
            raise ValueError(f"analysis.sensitivity: {bad} are not emulator inputs; vary them with engine runs "
                             "(direct evaluation), or give them a published prior (the engine's priors.yaml or "
                             "uncertainty.priors) so they are drawn and enter the emulator design")
        for p in bad:
            ps = next(x for x in A.eng.parameters() if x.name == p)
            if ps.lo is None or ps.hi is None:
                raise ValueError(f"analysis.sensitivity: parameter {p} has no range on the engine card")
            out[p] = (ps.lo, ps.hi, "engine-card range")
        out = {p: out[p] for p in sel}
    return out


def run(A, z0, ben0, cost0, plan, obj0) -> dict:
    rows = []
    for p, (lo, hi, basis) in _ranges(A).items():
        ends = []
        for v in (lo, hi):
            theta = dict(A.theta0, **{p: float(v)})
            clamped = 0
            if A.method == "emulator_inversion":
                cz = A.calibrate(theta)
                zs = {i: x[0] for i, x in cz.items()}
                clamped = sum(bool(x[1]) for x in cz.values())     # Codex r1 P1-3: recorded per end
            else:
                zs = z0
            _, ben, cost = A.evaluate(theta, zs)
            ends.append(dict(_end(A, v, ben, cost, plan), calibration_clamped_units=clamped))
        rows.append({"name": p, "kind": "parameter", "basis": basis, "point": A.theta0.get(p), "low": ends[0],
                     "high": ends[1]})
    for it, (lo, hi) in ((A.spec["analysis"].get("sensitivity") or {}).get("cost_items") or {}).items():
        ends = []
        for m in (lo, hi):
            _, ben, cost = A.evaluate(A.theta0, z0, cost_mult={it: float(m)})
            ends.append(_end(A, m, ben, cost, plan))
        rows.append({"name": it, "kind": "cost_item", "basis": "multiplier", "point": 1.0, "low": ends[0],
                     "high": ends[1]})
    for r in rows:
        r["swing_point_plan"] = abs(r["high"]["point_plan_objective"] - r["low"]["point_plan_objective"])
        r["swing_pct_of_point"] = 100.0 * r["swing_point_plan"] / obj0 if obj0 else None
    rows.sort(key=lambda r: -r["swing_point_plan"])
    return {"rows": rows, "objective_point": obj0, "outcome": A.primary}


def _end(A, v, ben, cost, plan) -> dict:
    sol = A.solve(ben, cost)
    A.guard(f"sensitivity at {v}", [plan, sol["choice"]])
    spent = sum(cost[i][plan[i]] for i in A.ids)
    return {"value": float(v), "point_plan_objective": sum(ben[i][plan[i]] for i in A.ids),
            "point_plan_cost": spent, "point_plan_within_budget": spent <= A.budget * (1 + 1e-9),
            "reoptimised_objective": sol["objective"],
            "share_units_changed": sum(sol["choice"][i] != plan[i] for i in A.ids) / len(A.ids)}
