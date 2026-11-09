"""Uncertainty over the declared sources only, labelled with what it covers (spike lesson 6).

Each draw samples the spec's `uncertainty.sources`, in this order (the spike's order for
its two sources, so the stored spike batch reproduces):
  engine_parameters  every fitted engine parameter, uniform within its engine-card range
  calibration        emulator_inversion: a per-region common-mode shift of the logit target
                     (sd `calibration.target_uncertainty.region_sd_logit`), then re-inversion;
                     likelihood: (calibrated parameters, unit attributes) from N(estimate, covariance);
                     posterior: rows of the canonical joint posterior sample (v2d §3.3: each draw records the
                     sample id, its row and the values applied, re-checked against the sample, fail closed)
  costs              a uniform multiplier per listed cost item
  emulator           one joint draw of the reduction errors of every unit: a common (population-mean) component
                     plus a per-unit one, from the held-out residuals (toolkit/analysis/model.py EmulatorError)
  priors             (v2 priors, drawn whenever there is one) every prior parameter from its declared bounded
                     distribution (toolkit/spec/priors.py), on its own random stream, before calibration
The point plan is evaluated in every draw (the headline interval) and the plan is
re-optimised in every draw (package stability). The point plan's cost is recorded in every draw
too (F6 S1-1): costs move with treated cases and unit prices, so a plan within the budget at the
point values can exceed it in a draw. `affordability` reports the share of draws over the
allocatable budget and the cost quantiles, and checks the spec's `uncertainty.affordability`
rule (max_share_over_budget) when one is declared. With `enforce: true` (S1-1b) the analysis first evaluates these
draws once (`evaluate_draws`) and `reserve_search` finds the largest budget fraction whose plan meets the rule; run()
then reads the same evaluations (`pre=`). The interval's label comes from the spec
(`Spec.uncertainty_scope`): what it covers and what it does not. Writes
`uncertainty_report.yaml` in the schema of scripts/propagate_uncertainty.py (shared
summariser), plus the scope and the draw plan: every source drawn or not, every engine
parameter drawn or held fixed with why, and the emulator-error model with its held-out
evidence, so a report cannot claim more precision than the draws carry.
"""
from __future__ import annotations

import os
import sys

import numpy as np
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from toolkit.analysis.core import q  # noqa: E402
PRODUCER = "toolkit/analysis/uncertainty.py"
ORDER = ("engine_parameters", "calibration", "costs", "emulator")


def _pu():
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import propagate_uncertainty
    return propagate_uncertainty


class Drawer:
    """Draws the declared sources; `draw()` returns (theta, zs, noise, cost multipliers, sampled values)."""

    def __init__(self, A, seed):
        self.A = A
        sp = A.spec["uncertainty"]
        self.src = {s["kind"]: s for s in sp.get("sources") or []}
        self.rng = np.random.default_rng(int(seed))
        from toolkit.spec.priors import PRIOR_STREAM
        self.prng = np.random.default_rng([int(seed), PRIOR_STREAM])     # priors: their own stream
        if "emulator" in self.src and getattr(A.model, "error", None) is None:
            raise ValueError("uncertainty: the emulator source is declared but the emulator has no error model (fewer "
                             "than 3 held-out points); raise analysis.emulator.holdout_share or exclude it with a reason")
        cal = A.spec["calibration"]
        self.sd = float((cal.get("target_uncertainty") or {}).get("region_sd_logit", 0.0))
        self.regions = sorted({u[A.region] for u in A.spec.units()}) if A.region else []
        if A.method == "likelihood" and "calibration" in self.src:
            if A.lik.get("cov") is None:
                raise ValueError("uncertainty: the calibration covariance is singular; the calibration source cannot "
                                 "be drawn (see analysis/calibration.yaml identifiability)")
            self.mvn = (np.array([A.lik["x"][n] for n in A.lik["names"]]), np.array(A.lik["cov"]))
        self.seed = int(seed)
        self.post_draws, self.records = None, []
        self.jc_draws, self.jc_records = None, []
        # joint calibration: its shared parameters are no longer free priors; with the calibration source they are
        # drawn from the region sample, without it they stay at the region's best point
        self.jc_names = set((A.joint or {}).get("shared") or [])

    def prepare(self, nd):
        from toolkit.analysis import calibration as C
        if self.A.method == "posterior" and "calibration" in self.src:
            self.post_draws = C.draws(C.sample_of(self.A.lik), nd, self.seed)
        if self.A.joint is not None and "calibration" in self.src:
            self.jc_draws = C.draws(self.A.joint["sample"], nd, self.seed)

    def draw(self):
        A = self.A
        theta, sampled = dict(A.theta0), {}
        shifts, zs, noise, mult = None, None, None, None
        for k, p in A.pr.items():
            v = p.sample(self.prng)           # drawn even when replaced, so the other priors' stream is unchanged
            if k in self.jc_names:
                continue
            theta[k] = v
            sampled[k] = theta[k]
        if self.jc_draws is not None:
            r = self.jc_draws[len(self.jc_records)]
            for k in A.joint["shared"]:
                theta[k] = float(r["values"][k])
                sampled[k] = theta[k]
            zs = {i: float(r["values"][f"z:{i}"]) for i in A.ids}
            self.jc_records.append({"sample_id": r["sample_id"], "row": r["row"],
                                    "values": dict({k: theta[k] for k in A.joint["shared"]},
                                                   **{f"z:{i}": zs[i] for i in A.ids})})
        for kind in ORDER:
            if kind not in self.src:
                continue
            if kind == "engine_parameters":
                for k, v in A.fr.items():
                    x = float(self.rng.uniform(v[0], v[1]))     # drawn either way: the stream is unchanged
                    if k in self.jc_names and self.jc_draws is not None:
                        continue                                # jointly calibrated: the region row's value
                    theta[k] = x
                    sampled[k] = theta[k]
            elif kind == "calibration" and A.method == "emulator_inversion" and A.joint is None:
                shifts = {r: float(self.rng.normal(0, self.sd)) for r in self.regions}
                sampled.update({f"shift:{r}": v for r, v in shifts.items()})
            elif kind == "calibration" and A.method == "likelihood":
                mu, cov = self.mvn
                x = dict(zip(A.lik["names"], self.rng.multivariate_normal(mu, cov)))
                for p in A.lik["params"]:
                    theta[p] = float(x[p])
                    sampled[p] = theta[p]
                zs = {i: float(x.get(f"z:{i}", A.lik["fixed_z"].get(i))) for i in A.ids}
            elif kind == "calibration" and A.method == "posterior":
                r = self.post_draws[len(self.records)]
                v = r["values"]
                for p in A.lik["params"]:
                    theta[p] = float(v[p]) if p in v else float(A.lik["fixed_params"][p])
                    sampled[p] = theta[p]
                fz = A.lik["fixed_z"]
                zs = {i: float(v[f"z:{i}"]) if f"z:{i}" in v else (None if fz[i] is None else float(fz[i]))
                      for i in A.ids}
                # observation targets: the sources' bias parameters are sampled jointly (nuisance), recorded with
                # the row, never passed to the engine
                applied = {**{p: theta[p] for p in A.lik["params"]}, **{f"z:{i}": zs[i] for i in A.ids},
                           **{n: float(v[n]) for n in A.lik.get("nuisance") or []}}
                self.records.append({"sample_id": r["sample_id"], "row": r["row"],
                                     "values": {n: applied[n] for n in A.lik["sample"]["names"]}})
            elif kind == "costs":
                mult = {it: float(self.rng.uniform(lo, hi)) for it, (lo, hi) in self.src["costs"]["items"].items()}
                sampled.update({f"cost:{k}": v for k, v in mult.items()})
            elif kind == "emulator":
                noise = self.rng
        if zs is None:
            cz = A.calibrate(theta, shifts)
            zs = {i: v[0] for i, v in cz.items()}
            self.last_clamped = sorted(i for i, v in cz.items() if v[1])   # Codex r2 P1-4: kept per draw
        return theta, zs, noise, mult, sampled


def affordability(costs, budget: float, rule: dict | None = None, level: float = 0.95) -> dict:
    """The fixed plan's cost in each draw against the allocatable budget (F6 S1-1). A cost above budget * (1 + 1e-9), the
    solver's own tolerance, counts as over budget. With a rule (`uncertainty.affordability`), rule_met says whether the share of
    draws over budget is at most rule.max_share_over_budget; without one, rule_met is None (reported, not judged).
    No draws, or a non-finite cost, fails closed."""
    c = np.asarray(costs, float)
    if c.size == 0 or not np.all(np.isfinite(c)):
        raise ValueError("affordability: the fixed plan's cost needs one finite value per draw")
    b = float(budget)
    over = int(np.sum(c > b * (1 + 1e-9)))       # the solver's relative budget tolerance (toolkit/alloc BUDGET_RTOL)
    share = over / c.size
    met = None if not rule else bool(share <= float(rule["max_share_over_budget"]) + 1e-12)
    return {"budget_allocatable": b, "draws": int(c.size), "draws_over_budget": over, "share_over_budget": share,
            "fixed_plan_cost": q(c, level), "per_draw_cost": [float(x) for x in c],
            "rule": dict(rule) if rule else None, "rule_met": met}


RESERVE_TOL, RESERVE_FLOOR, RESERVE_GRID, RESERVE_MAX_SOLVES = 0.005, 0.5, 10, 20


def _over(c, budget: float) -> bool:
    return c > float(budget) * (1 + 1e-9)       # affordability()'s tolerance: the solver's relative one


def reserve_search(draw_costs, solve, budget: float, max_share: float, tol: float = RESERVE_TOL,
                   floor: float = RESERVE_FLOOR, max_solves: int = RESERVE_MAX_SOLVES, grid: int = RESERVE_GRID) -> dict:
    """F6 S1-1b: a plan solved within a budget reserve that meets the affordability rule: its cost exceeds the full
    `budget` in at most `max_share` of the draws. `draw_costs` holds one cost table {unit: {package: cost}} per
    uncertainty draw, so a candidate plan's cost in a draw is a sum; `solve(b)` returns the optimum {choice, objective}
    at budget b (RuntimeError "optimizer status infeasible" counts as not meeting).

    Visits: the unconstrained plan (f = 1); if it fails, a grid of `grid` fractions from `floor` up to 1 (step
    (1 - floor) / grid); then bisection between the highest grid fraction that meets the rule and the grid point above
    it, until that bracket is at most `tol` wide or `max_solves` solves are spent. Every solve is a recorded step.
    Plans at different budgets are not nested, so the share need not fall with f: the result is the BEST VISITED plan
    that meets the rule (highest objective; ties: the larger fraction), not a proven optimum, and "none_visited_meets"
    says only that no visited fraction met the rule, not that none exists. `floor_met` reports the floor candidate
    separately. Returns {feasible, result (unconstrained | best_visited | none_visited_meets), fraction, solution,
    draws_over_budget, share_over_budget, floor_met, converged, steps, ...}. Bad inputs fail closed (ValueError)."""
    tabs = list(draw_costs)
    if not tabs or not all(isinstance(t, dict) and t for t in tabs):
        raise ValueError("reserve search: one cost table per uncertainty draw is required")
    if not all(np.isfinite(float(c)) for t in tabs for row in t.values() for c in row.values()):
        raise ValueError("reserve search: every draw's costs must be finite")
    if not (0 <= float(max_share) < 1):
        raise ValueError("reserve search: max_share_over_budget must be in [0, 1)")
    if not (float(tol) > 0 and 0 < float(floor) <= 1 and int(grid) >= 1 and int(max_solves) >= int(grid) + 2):
        raise ValueError("reserve search: tolerance > 0, floor in (0, 1], a grid of at least 1 and at least grid + 2 "
                         "solves are required")
    nd, steps, sols = len(tabs), [], {}

    def step(f):
        rec = {"fraction": float(f), "budget": float(f) * float(budget)}
        try:
            sol = solve(float(f) * float(budget))
        except RuntimeError as e:
            if str(e).strip() != "optimizer status infeasible":
                raise
            steps.append(dict(rec, objective=None, draws_over_budget=None, share_over_budget=None, meets=False,
                              infeasible=True))
            return False
        ch = sol["choice"]
        over = sum(_over(sum(t[i][ch[i]] for i in t), budget) for t in tabs)
        meets = over / nd <= float(max_share) + 1e-12
        steps.append(dict(rec, objective=float(sol["objective"]), draws_over_budget=int(over),
                          share_over_budget=over / nd, meets=bool(meets)))
        sols[len(steps) - 1] = sol
        return meets

    base = {"draws": nd, "tolerance": float(tol), "floor": float(floor), "grid": int(grid), "max_solves": int(max_solves)}
    if step(1.0):
        s0 = steps[0]
        return dict(base, feasible=True, result="unconstrained", fraction=1.0, solution=sols[0],
                    draws_over_budget=s0["draws_over_budget"], share_over_budget=s0["share_over_budget"],
                    floor_met=None, converged=True, solves=1, steps=steps)
    fl, g = float(floor), int(grid)
    pts = [fl + k * (1.0 - fl) / g for k in range(g)] if fl < 1.0 else [1.0]
    met = [f for f in pts if step(f)]
    floor_met = bool(steps[1]["meets"])
    conv = False
    if met:
        lo = max(met)
        hi = next((f for f in pts if f > lo), 1.0)
        while hi - lo > float(tol) + 1e-12 and len(steps) < int(max_solves):
            mid = (lo + hi) / 2
            if step(mid):
                lo = mid
            else:
                hi = mid
        conv = hi - lo <= float(tol) + 1e-12
    ok_ = [j for j, s in enumerate(steps) if s["meets"]]
    if not ok_:
        return dict(base, feasible=False, result="none_visited_meets", fraction=None, solution=None,
                    draws_over_budget=None, share_over_budget=None, floor_met=floor_met, converged=False,
                    solves=len(steps), steps=steps)
    j = max(ok_, key=lambda k: (steps[k]["objective"], steps[k]["fraction"]))
    return dict(base, feasible=True, result="best_visited", fraction=steps[j]["fraction"], solution=sols[j],
                draws_over_budget=steps[j]["draws_over_budget"], share_over_budget=steps[j]["share_over_budget"],
                floor_met=floor_met, converged=conv, solves=len(steps), steps=steps)


def _acosts():
    from toolkit.alloc import costs
    return costs


def _n_seed(A, draws=None, seed=None):
    sp = A.spec["uncertainty"]
    return int(draws if draws is not None else sp.get("draws", 32)), seed if seed is not None else sp.get("seed", 42)


def _draw_rows(A, D, nd):
    """Each draw's sampled values and evaluation, in draw order (lazily: the emulator error is drawn from the shared
    stream inside evaluate)."""
    for _ in range(nd):
        D.last_clamped = []
        theta, zs, noise, mult, sampled = D.draw()
        clamped = list(D.last_clamped)
        out, ben, cost = A.evaluate(theta, zs, noise=noise, cost_mult=mult)
        yield {"sampled": sampled, "clamped": clamped, "out": out, "ben": ben, "cost": cost,
               "events": list(A.last_events), "floors": list(getattr(A, "last_cost_floors", None) or [])}


def evaluate_draws(A, draws=None, seed=None) -> dict:
    """The uncertainty step's draws evaluated once (F6 S1-1b): the same Drawer, seed and number of draws as run(), so
    run(..., pre=) on the result reads exactly these evaluations."""
    nd, sd = _n_seed(A, draws, seed)
    D = Drawer(A, sd)
    D.prepare(nd)
    return {"draws": nd, "seed": int(sd), "drawer": D, "rows": list(_draw_rows(A, D, nd))}


def run(A, plan, headline_name: str, draws=None, seed=None, pre=None, on_draw=None) -> dict:
    """`on_draw(out, events)`: called with each draw's outcomes and model corrections, precomputed draws included (F6
    S2-5: the burden references' values, and the guard on the validation arm)."""
    sp = A.spec["uncertainty"]
    nd, sd = _n_seed(A, draws, seed)
    scope = A.spec.uncertainty_scope()
    if pre is not None:          # evaluated once beforehand (the reserve search): the same draws, checked
        if pre["draws"] != nd or pre["seed"] != int(sd) or len(pre["rows"]) != nd:
            raise ValueError(f"uncertainty: precomputed draws ({pre['draws']}, seed {pre['seed']}) are not this "
                             f"step's ({nd}, seed {sd})")
        D, rows = pre["drawer"], pre["rows"]
    else:
        D = Drawer(A, sd)
        D.prepare(nd)
        rows = _draw_rows(A, D, nd)
    fixed = {o: [] for o in A.outcomes}
    fixed_cost = []
    opt_vals, same = [], np.zeros(len(A.ids))
    comps_used, outs, samples = set(), [], {}
    corr_any = corr_plan = 0
    clamped_draws = []
    floors = []          # F6 S1-3: each draw's floored per-outcome increments (also from precomputed draws)
    hl_outcome = A.spec["deliverable"]["headline"]["outcome"]
    for d, r in enumerate(rows):
        sampled, out, ben, cost = r["sampled"], r["out"], r["ben"], r["cost"]
        if r["clamped"]:
            clamped_draws.append({"draw": d, "units": len(r["clamped"]), "examples": r["clamped"][:5]})
        corr_any += bool(r["events"])
        if on_draw is not None:
            on_draw(out, r["events"])
        floors.append(r["floors"])
        row = {}
        for o in A.outcomes:
            v = sum(out[i]["y"][A.comp][o] - out[i]["y"][plan[i]][o] for i in A.ids)
            fixed[o].append(v)
            row[f"{o}_averted"] = v
        row[headline_name] = row[f"{hl_outcome}_averted"]
        fixed_cost.append(sum(cost[i][plan[i]] for i in A.ids))
        sol = A.solve(ben, cost)
        A.guard(f"uncertainty draw {d}", [plan, sol["choice"]], r["events"])
        opt_vals.append(sol["objective"])
        row["reoptimised_objective"] = sol["objective"]
        same += np.array([sol["choice"][i] == plan[i] for i in A.ids])
        for i in A.ids:
            comps_used |= set(A.pk[sol["choice"][i]])
            row[f"package:{i}"] = sol["choice"][i]
        outs.append(row)
        for k, v in sampled.items():
            samples.setdefault(k, []).append(v)
    lvl = scope["level"]
    if clamped_draws:
        # Codex r2 P1-4: where a draw's calibration clamps, that draw does not carry the full calibration uncertainty;
        # the interval says so instead of claiming unrestricted calibration coverage.
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import render_claims
        meta = dict(scope["interval"])
        meta["covers"] = ["calibration_partly_clamped" if k == "calibration" else k for k in meta["covers"]]
        scope = dict(scope, interval=meta, covers=meta["covers"], label=render_claims.interval_label(meta))
    prov = None
    if A.method == "posterior" and D.post_draws is not None:
        # v2d §3.3: re-read the canonical sample; every draw's applied values must equal its row (fail closed)
        from toolkit.analysis import calibration as C
        smp = C.sample_of(A.lik)
        found = C.check_provenance(smp, D.records)
        prov = {"sample_id": smp["sample_id"], "draws": len(D.records), "rows": [r["row"] for r in D.records],
                "applied": D.records, "findings": found, "alarms": C.spread_alarm(smp, D.records)}
        if found:
            raise ValueError(f"uncertainty: {len(found)} draws are not rows of the canonical posterior sample "
                             f"(e.g. {found[0]})")
        pd = list(A.lik.get("prior_dominated") or [])
        if pd:
            sys.path.insert(0, os.path.join(ROOT, "scripts"))
            import render_claims
            meta = dict(scope["interval"])
            meta["prior_dominated"] = sorted(set(meta.get("prior_dominated") or []) | set(pd))
            scope = dict(scope, interval=meta, label=render_claims.interval_label(meta))
    if D.jc_draws is not None:
        # the joint calibration's draws: every applied value must be a row of its region sample (fail closed)
        from toolkit.analysis import calibration as C
        smp = A.joint["sample"]
        found = C.check_provenance(smp, D.jc_records)
        if found:
            raise ValueError(f"uncertainty: {len(found)} draws are not rows of the joint calibration's region sample "
                             f"(e.g. {found[0]})")
        prov = dict(prov or {}, joint_calibration={"sample_id": smp["sample_id"], "draws": len(D.jc_records),
                                                   "rows": [r["row"] for r in D.jc_records],
                                                   "alarms": C.spread_alarm(smp, D.jc_records)})
    from toolkit.spec import priors as PRI
    plan_ = PRI.plan(A.spec)
    plan_["emulator_error"] = (getattr(A.model, "accuracy", {}) or {}).get("error_model") if "emulator" in D.src else None
    data = {"draws": nd, "seed": int(sp.get("seed", 42)) if seed is None else int(seed),
            "covers": scope["covers"], "excludes": scope["excludes"], "label": scope["label"],
            "fixed_plan": {o: q(v, lvl) for o, v in fixed.items()},
            "reoptimised_objective": q(opt_vals, lvl),
            "units_same_package_share_median": float(np.median(same / max(nd, 1))) if nd else None,
            "units_same_package_in_90pct_of_draws": int(np.sum(same / max(nd, 1) >= 0.9)) if nd else None,
            "components_in_any_draw_plan": sorted(comps_used), "interval": scope["interval"],
            "calibration_clamped": {"draws": len(clamped_draws), "max_units": max((c["units"] for c in clamped_draws),
                                                                                  default=0),
                                    "per_draw": clamped_draws},
            "model_corrections": {"draws_with_corrections": corr_any, "draws_with_corrections_on_plan": 0},
            "headline": {"name": headline_name, "outcome": hl_outcome}, "draw_plan": plan_,
            "affordability": affordability(fixed_cost, A.budget, sp.get("affordability"), lvl) if nd else None,
            "cost_floors": _acosts().draw_floor_summary(floors),
            "prior_dominated": sorted(set(plan_["prior_dominated"]) | set((A.lik or {}).get("prior_dominated") or []))}
    if getattr(A, "plan_budget", None) is not None:      # F6 S1-1b: the draws re-optimised within the reserve
        data["budget_planned"] = float(A.plan_budget)
    if prov is not None:
        data["draw_provenance"] = prov
    flagged = sorted((getattr(A, "flagged", None) or {}).values(), key=lambda f: str(f["unit"]))
    if flagged:        # calibration.joint.unmatched: flag: these units' values carry no calibrated match
        data["low_confidence_units"] = flagged
    agg = _pu().summarise_outputs(outs, samples, nd, level=scope["level"])   # the signed level (Codex P1-11)
    report = {"producer": PRODUCER, "produced_by": "harness", "n_draws": nd, "n_draws_requested": nd,
              "n_successful": nd - len(agg["errors"]), "n_failed": len(agg["errors"]), "seed": data["seed"],
              "interval_label": scope["label"], "interval": scope["interval"], "covers": scope["covers"],
              "excludes": scope["excludes"],
              "degenerate_outputs": agg["degenerate_outputs"],
              "sampler_varied_parameters": len(agg["varied_params"]),
              "scalar_outputs": agg["scalar_outputs"], "categorical_outputs": agg["categorical_outputs"],
              "parameter_samples": agg["parameter_samples"], "backend": "local",
              "sources_drawn": [x["kind"] for x in plan_["sources"] if x["status"] == "drawn"],
              "sources_not_drawn": [{k: x[k] for k in ("kind", "status", "reason") if k in x} for x in plan_["sources"]
                                    if x["status"] != "drawn"],
              "parameters_drawn": [{k: x.get(k) for k in ("name", "source_kind", "dist", "range", "source")}
                                   for x in plan_["drawn"]],
              "parameters_fixed": [{k: x.get(k) for k in ("name", "kind", "value", "reason")} for x in plan_["fixed"]
                                   if x["status"] == "fixed"],
              "not_exposed": plan_["not_exposed"], "emulator_error": plan_["emulator_error"],
              "affordability": data["affordability"]}
    if flagged:
        report["low_confidence_units"] = [{k: f.get(k) for k in ("unit", "region", "status", "reason", "implausibility")}
                                          for f in flagged]
        report["low_confidence_note"] = (f"{len(flagged)} units did not match the joint calibration and proceed "
                                         "flagged (calibration.joint.unmatched: flag); their outcomes rest on an "
                                         "unmatched calibration")
    return {"data": data, "report": report}


def scalar_report(outputs, values: dict, level: float, label: str, n_draws: int, method: str, **extra) -> dict:
    """uncertainty_report.yaml for a deliverable whose headline outputs are scalars (design note v2d §2.3), in the
    shape the allocation path writes and every reader of that file expects: `scalar_outputs: {name: {mean, median,
    sd, ci_low, ci_high, n}}`, one entry per declared output, in the declared order. `outputs` are the deliverable's
    declared output entries ({name, ...}); `values` maps each name to its draws. A declared output without finite draws
    fails closed (ValueError): the report never omits a headline."""
    a = (1.0 - float(level)) / 2.0
    so = {}
    for o in outputs:
        n = o["name"] if isinstance(o, dict) else str(o)
        v = np.asarray(values.get(n, []), float)
        if v.size == 0 or not np.all(np.isfinite(v)):
            raise ValueError(f"uncertainty report: output {n} has no draws or non-finite draws")
        so[n] = {"mean": float(np.mean(v)), "median": float(np.median(v)), "sd": float(np.std(v, ddof=1))
                 if v.size > 1 else 0.0, "ci_low": float(np.quantile(v, a)), "ci_high": float(np.quantile(v, 1 - a)),
                 "n": int(v.size)}
        for k in ("metric", "window", "scale"):
            if isinstance(o, dict) and o.get(k) is not None:
                so[n][k] = o[k]
    return dict({"producer": PRODUCER, "produced_by": "harness", "n_draws": int(n_draws),
                 "n_successful": int(n_draws), "n_failed": 0, "interval_label": label, "level": float(level),
                 "method": method, "backend": "local", "scalar_outputs": so}, **extra)


def write_report(run_dir, report) -> str:
    p = os.path.join(run_dir, "uncertainty_report.yaml")
    from toolkit.analysis.artifacts import plain
    from toolkit.analysis.artifacts import _safe_io
    _safe_io().write_nofollow(run_dir, "uncertainty_report.yaml", yaml.safe_dump(plain(report), sort_keys=False).encode(),
                              atomic=True)
    return p


def _self_test() -> int:
    """The shared scalar writer (scalar_report); the allocation path's run() is exercised by run.py --self-test."""
    import tempfile
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)

    rng = np.random.default_rng(1)
    outs = [{"name": "b", "metric": "m2", "window": "w", "scale": "ratio"}, {"name": "a", "metric": "m1"}]
    vals = {"a": rng.normal(1.0, 0.1, 4000), "b": rng.normal(5.0, 1.0, 4000)}
    try:
        rep = scalar_report(outs, vals, 0.9, "90% UI over calibration", 4000, "posterior")
    except Exception as e:  # noqa: BLE001 -- a missing writer is a failing case
        rep = {"error": f"{type(e).__name__}: {e}"}
    so = rep.get("scalar_outputs") or {}
    ok(list(so) == ["b", "a"] and all(set(v) >= {"mean", "median", "sd", "ci_low", "ci_high", "n"} for v in so.values()),
       f"U1 scalar_report: one entry per declared output, in the declared order, with mean, median, sd and interval "
       f"({rep.get('error') or list(so)})")
    if so:
        lo, hi = np.quantile(vals["a"], [0.05, 0.95])
        ok(abs(so["a"]["ci_low"] - lo) < 1e-12 and abs(so["a"]["ci_high"] - hi) < 1e-12 and so["b"]["metric"] == "m2",
           "U2 the interval is the declared level's central interval; the output's metric, window and scale are kept")
        ok(rep["interval_label"] == "90% UI over calibration" and rep["n_draws"] == 4000,
           "U3 the report carries the label and the number of draws")
        for label, v in (("no draws", {"a": [], "b": vals["b"]}),
                         ("a non-finite draw", {"a": [1.0, float("nan")], "b": vals["b"]})):
            try:
                scalar_report(outs, v, 0.9, "x", 2, "posterior")
                ok(False, f"U4 an output with {label} fails closed")
            except ValueError:
                ok(True, f"U4 an output with {label} fails closed")
        with tempfile.TemporaryDirectory() as d:
            p = write_report(os.path.realpath(d), rep)
            ok((yaml.safe_load(open(p)) or {}).get("scalar_outputs", {}).get("a", {}).get("n") == 4000,
               "U5 write_report publishes uncertainty_report.yaml, which reads back")
    # F6 S1-1: the fixed plan's cost across the draws against the allocatable budget
    aff = globals().get("affordability")
    ok(callable(aff), "U6 affordability() exists")
    if callable(aff):
        r = aff([90.0, 110.0, 120.0, 95.0], 100.0, None, 0.9)
        ok(r["draws"] == 4 and r["draws_over_budget"] == 2 and abs(r["share_over_budget"] - 0.5) < 1e-12
           and r["rule_met"] is None and r["budget_allocatable"] == 100.0,
           f"U7 two of four draws over budget, share 0.5; no rule declared: rule_met is None ({r})")
        fc = r.get("fixed_plan_cost") or {}
        ok(fc.get("level") == 0.9 and fc.get("ci_low") <= fc.get("median") <= fc.get("ci_high"),
           "U8 the fixed plan's cost quantiles at the declared level")
        ok(aff([90.0, 110.0, 120.0, 95.0], 100.0, {"max_share_over_budget": 0.1}, 0.95)["rule_met"] is False,
           "U9 share 0.5 against a rule of at most 0.1: the rule is not met")
        ok(aff([90.0, 110.0, 120.0, 95.0], 100.0, {"max_share_over_budget": 0.5}, 0.95)["rule_met"] is True,
           "U10 share 0.5 against a rule of at most 0.5: met")
        ok(aff([100.0 * (1 + 1e-12)], 100.0, None, 0.95)["draws_over_budget"] == 0,
           "U11 a cost equal to the budget up to rounding is not over budget")
        ok(aff([2e-10], 1e-10, None, 0.95)["draws_over_budget"] == 1,
           "U11b the tolerance is relative to the budget, as the solver's (no absolute floor): 2e-10 is over 1e-10")
        for label, v in (("no draws", []), ("a non-finite cost", [90.0, float("nan")])):
            try:
                aff(v, 100.0, None, 0.95)
                ok(False, f"U12 {label} fails closed")
            except ValueError:
                ok(True, f"U12 {label} fails closed")
    _self_test_reserve(ok)
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def _self_test_reserve(ok):
    """F6 S1-1b: the reserve search on synthetic cost tables. Four units, each either nothing (cost 0) or one
    package costing 10 x the draw's multiplier, benefits 4, 3, 2, 1; the exact optimum at budget b funds the
    floor(b / 10) best units. Budget 40; multipliers 0.8 ... 2.0: the unconstrained plan (4 units) is over in 5 of 8
    draws; 3 units are over in 2 of 8 (multipliers 1.5 and 2.0); 2 units in none."""
    rs = globals().get("reserve_search")
    ok(callable(rs), "U13 reserve_search() exists")
    if not callable(rs):
        return
    mults = [0.8, 0.9, 1.0, 1.1, 1.2, 1.3, 1.5, 2.0]
    ben = {"u1": 4.0, "u2": 3.0, "u3": 2.0, "u4": 1.0}
    tables = [{u: {"n": 0.0, "y": 10.0 * m} for u in ben} for m in mults]
    calls = []

    def solve(b):
        calls.append(b)
        k = int(b / 10.0 + 1e-9)
        top = sorted(ben, key=lambda u: -ben[u])[:k]
        return {"choice": {u: ("y" if u in top else "n") for u in ben}, "objective": sum(ben[u] for u in top)}

    r = rs(tables, solve, 40.0, 0.25, tol=0.005, floor=0.5, max_solves=20, grid=10)
    st = r.get("steps") or []
    ok(r.get("feasible") is True and st and st[0]["fraction"] == 1.0 and st[0]["meets"] is False
       and st[0]["draws_over_budget"] == 5,
       f"U14 the first step is the unconstrained plan (fraction 1), over budget in 5 of 8 draws: {st[:1]}")
    ok([round(x["fraction"], 6) for x in st[1:11]] == [round(0.5 + 0.05 * k, 6) for k in range(10)]
       and r.get("floor_met") is True,
       f"U14b a coarse grid over [floor, 1) is probed before bisecting (Codex S1-1b P2): "
       f"{[round(x['fraction'], 3) for x in st[1:11]]}")
    ok(1.0 - 0.005 <= r.get("fraction", 0) < 1.0 and sum(v == "y" for v in r["solution"]["choice"].values()) == 3
       and r["draws_over_budget"] == 2 and abs(r["share_over_budget"] - 0.25) < 1e-12
       and r.get("result") == "best_visited",
       f"U15 the best visited fraction meeting the rule, bisected to the tolerance above the grid (3 units, 2 of 8 "
       f"over): {r.get('fraction')}, {r.get('draws_over_budget')}, {r.get('result')}")
    ok(len(calls) == len(st) <= 20 and r.get("converged") is True and abs(r["solution"]["objective"] - 9.0) < 1e-12
       and all(x["meets"] == (x["share_over_budget"] <= 0.25) for x in st)
       and all(abs(b - x["fraction"] * 40.0) < 1e-9 for b, x in zip(calls, st)),
       f"U16 every ILP solve is one recorded step at fraction x budget, within the cap ({len(calls)} solves)")
    r1 = rs(tables, solve, 40.0, 0.7, tol=0.005, floor=0.5, max_solves=20, grid=10)
    ok(r1["fraction"] == 1.0 and len(r1["steps"]) == 1 and r1["feasible"] and r1.get("result") == "unconstrained",
       "U17 an unconstrained plan that meets the rule needs no reserve (fraction 1, one solve)")
    r2 = rs(tables, solve, 40.0, 0.0, tol=0.005, floor=0.8, max_solves=20, grid=10)
    ok(r2["feasible"] is False and r2.get("solution") is None and r2.get("result") == "none_visited_meets"
       and r2.get("floor_met") is False and len(r2["steps"]) == 11,
       f"U18 no visited fraction meets the rule (the unconstrained plan and the grid from the floor): nothing "
       f"returned, and the result says only what was visited ({r2.get('result')}, {len(r2['steps'])} solves)")
    r3 = rs(tables, solve, 40.0, 0.25, tol=0.005, floor=0.5, max_solves=12, grid=10)
    ok(len(r3["steps"]) == 12 and r3["converged"] is False and r3["feasible"] and r3["share_over_budget"] <= 0.25,
       "U19 the solve cap stops the bisection unconverged, at a fraction that meets the rule")

    def infeasible_low(b):
        if b < 30:
            raise RuntimeError("optimizer status infeasible")
        return solve(b)
    r4 = rs(tables, infeasible_low, 40.0, 0.25, tol=0.005, floor=0.5, max_solves=20, grid=10)
    ok(r4["feasible"] is True and r4["steps"][1].get("infeasible") is True and r4.get("floor_met") is False
       and r4["fraction"] >= 0.95,
       "U20 the floor candidate failing (the solver proves it infeasible) is not the end: a higher visited fraction "
       "that meets the rule is kept")

    def non_nested(b):          # Codex S1-1b P2: plans that are not nested in the budget
        f = b / 40.0
        k, obj = (1, 1.0) if f < 0.7 else (3, 3.0) if 0.85 <= f < 0.95 else (4, 4.0)
        top = sorted(ben, key=lambda u: -ben[u])[:k]
        return {"choice": {u: ("y" if u in top else "n") for u in ben}, "objective": obj}
    r5 = rs(tables, non_nested, 40.0, 0.25, tol=0.005, floor=0.5, max_solves=20, grid=10)
    ok(r5["feasible"] and abs(r5["solution"]["objective"] - 3.0) < 1e-12 and 0.9 <= r5["fraction"] < 0.95,
       f"U23 a non-nested problem: the grid finds the better plan a plain bisection misses (objective "
       f"{r5['solution']['objective']} at {r5['fraction']})")
    for label, args in (("no draws", ([], solve, 40.0, 0.25)), ("a non-finite cost",
                        ([{u: {"n": 0.0, "y": float("nan")} for u in ben}], solve, 40.0, 0.25)),
                        ("a share outside [0, 1)", (tables, solve, 40.0, 1.0))):
        try:
            rs(*args)
            ok(False, f"U21 {label} fails closed")
        except ValueError:
            ok(True, f"U21 {label} fails closed")
    for kw in ({"tol": 0.0}, {"floor": 0.0}, {"floor": 1.5}, {"max_solves": 1}, {"max_solves": 5, "grid": 10},
               {"grid": 0}):
        try:
            rs(tables, solve, 40.0, 0.25, **kw)
            ok(False, f"U22 search settings {kw} are refused")
        except ValueError:
            ok(True, f"U22 search settings {kw} are refused")



if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    print(__doc__)
