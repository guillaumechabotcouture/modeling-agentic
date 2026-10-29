#!/usr/bin/env python3
"""Toolkit, "budget allocation across units" template: optimizer and its benchmark (T1).

    python3 toolkit/alloc/optimize.py <spec.yaml> --benefit <csv> --budget <number> [--units <csv>]
        [--primary ilp|greedy|brute] [--run-dir <dir>] [--objective-name <name>]
    python3 toolkit/alloc/optimize.py --self-test

The CLI reads the spec (costs from toolkit/alloc/costs.py, feasibility and caps
from toolkit/alloc/packages.py) and a benefit table with columns
unit, package, benefit (the run's model output), runs the benchmark and writes
<run-dir>/models/optimization_quality.yaml.

Choose one package per unit to maximise total benefit within a budget and any
caps (a multiple-choice knapsack). E7 attempt 7's `models/optimize.py` had two
defects this module is built to prevent:
- dominance pruning was silently switched off whenever a cap was present,
  because removing a dominated package is only safe if it uses no less of
  every capped resource; here pruning compares cap use too, so it is always
  on and always safe;
- greedy beat the "optimal" ILP at one budget (an unchecked solver status and
  a relative gap), and that was reported as a footnote; here the ILP must
  report a proven optimum (gap 0), every returned allocation is re-checked
  against budget, caps and feasibility, and any method beating an exact one
  stops the benchmark with an error.

API:
    solve(benefit, cost, feasible, budget, caps=None, method="ilp"|"greedy"|"brute")
        benefit[unit][package] -> number; cost[unit][package] -> number or
        {"total": number} (toolkit/alloc/costs.py cost_matrix); feasible is
        feasible[unit][package] -> bool, or the dict returned by
        toolkit/alloc/packages.py feasibility(), whose caps are then used
        unless caps is given (caps=[] drops them explicitly).
        caps: [{"name", "max", "weights": {unit: {package: w}}}].
        Returns {"choice": {unit: package}, "objective", "cost", "status",
        "method", "seconds", "cap_use": {name: use}, ...}; status is
        "optimal" (proven), "feasible" (ILP stopped early), "heuristic",
        "infeasible" (exact methods: no allocation satisfies the constraints)
        or "no_feasible_found" (greedy).
    benchmark(..., primary="ilp", run_dir=None | out=None, sweep_budgets=None)
        runs every applicable method (the ILP if PuLP is installed, greedy,
        brute force if the pruned search space is at most `brute_limit`),
        raises OptimizerError if any method beats an exact one or the primary
        is not proven optimal, and writes models/optimization_quality.yaml in
        the schema scripts/optimization_quality.py checks.

Methods:
- ilp: PuLP + CBC (PuLP is pinned in requirements.txt), binary x[unit,package],
  one package per unit, budget and caps, relative gap 0, time limit; status
  "optimal" only when CBC proves it.
- greedy: the multiple-choice knapsack heuristic: start each unit at its
  cheapest feasible package, take steps along each unit's upper convex hull
  of (cost, benefit) in order of incremental benefit per incremental cost
  while they fit, then take the best affordable single-unit upgrades. Without
  caps, exact - greedy <= the largest single hull-step benefit (the LP bound
  of Sinha and Zoltners 1979), and greedy is exact when the budget falls on a
  hull breakpoint; the benchmark checks and reports that bound. With caps,
  there is no such bound and greedy is a heuristic only.
- brute: exhaustive depth-first search with exact bounds (cost, caps and
  benefit), for small instances and as an independent check of the ILP.
"""
from __future__ import annotations

import argparse
import importlib.util
import math
import os
import sys
import time

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
# Imported as toolkit.alloc.optimize (a run's model code), the siblings come from the
# package, and sys.path is left alone: a run's own models/costs.py must not shadow them,
# nor they it. Run as a script, they are imported from this directory.
if __package__:
    from . import costs, packages
else:
    sys.path.insert(0, HERE)
    import costs  # noqa: E402
    import packages  # noqa: E402

METHOD_NAMES = {"ilp": "ilp_pulp", "greedy": "greedy", "brute": "brute_force"}   # optimization_quality.py vocabulary
EXACT = ("ilp", "brute")
BUDGET_RTOL = 1e-9       # every method allows cost <= budget * (1 + 1e-9)
CHECK_RTOL = 1e-7        # the independent check allows solver rounding up to this
BEAT_RTOL = 1e-7         # a method "beats" another when it is better by more than this (relative)


class OptimizerError(RuntimeError):
    """A solver defect: an exact method beaten, an unproven optimum, or an allocation that violates a constraint."""


def _pulp():
    try:
        import pulp
        return pulp
    except ImportError:
        return None


def _num(x):
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) else None


def _prepare(benefit, cost, feasible, budget, caps):
    """Validate (fail closed) and build per-unit options [(package, benefit, cost, cap weights)]."""
    if isinstance(feasible, dict) and isinstance(feasible.get("feasible"), dict):
        if caps is None:
            caps = feasible.get("caps") or []
        feasible = feasible["feasible"]
    caps = list(caps or [])
    errs = []
    b0 = _num(budget)
    if b0 is None:
        errs.append(f"budget must be a finite number, got {budget!r}")
    for k, m in (("benefit", benefit), ("cost", cost), ("feasible", feasible)):
        if not isinstance(m, dict) or not m:
            errs.append(f"{k}: a non-empty mapping unit -> package -> value is required")
    if errs:
        raise ValueError("; ".join(errs))
    us = list(feasible)
    for k, m in (("benefit", benefit), ("cost", cost)):
        if set(map(str, m)) != set(map(str, us)):
            miss = sorted(set(map(str, us)) - set(map(str, m)))[:5]
            extra = sorted(set(map(str, m)) - set(map(str, us)))[:5]
            errs.append(f"{k}: units differ from feasible (missing {miss}, extra {extra})")
    for i, cp in enumerate(caps):
        if not isinstance(cp, dict) or _num(cp.get("max")) is None or not isinstance(cp.get("weights"), dict):
            errs.append(f"caps[{i}]: needs name, a finite max and weights[unit][package]")
    if errs:
        raise ValueError("; ".join(errs))
    names = [str(cp.get("name") or f"cap{i}") for i, cp in enumerate(caps)]
    opts = {}
    for u in us:
        fu = feasible[u]
        if not isinstance(fu, dict):
            errs.append(f"feasible.{u}: must map package -> bool")
            continue
        bad = [p for p, v in fu.items() if not isinstance(v, bool)]
        if bad:
            errs.append(f"feasible.{u}: values must be booleans ({bad[:3]})")
            continue
        o = []
        for p, v in fu.items():
            if not v:
                continue
            bv = _num((benefit.get(u) or {}).get(p))
            cr = (cost.get(u) or {}).get(p)
            cv = _num(cr.get("total") if isinstance(cr, dict) else cr)
            if bv is None or cv is None:
                errs.append(f"unit {u}, package {p}: feasible but benefit {(benefit.get(u) or {}).get(p)!r} "
                            f"or cost {cr!r} is not a finite number")
                continue
            w = []
            for n, cp in zip(names, caps):
                wv = _num(((cp["weights"].get(u) or {}).get(p)))
                if wv is None or wv < 0:
                    errs.append(f"caps {n}: unit {u}, package {p}: weight must be a number >= 0")
                    wv = 0.0
                w.append(wv)
            o.append((p, bv, cv, tuple(w)))
        if not o:
            errs.append(f"unit {u}: no feasible package")
        opts[u] = o
    if errs:
        raise ValueError("invalid optimization input: " + "; ".join(errs[:20]) + (" ..." if len(errs) > 20 else ""))
    return us, opts, caps, names, b0


def prune(opts: dict) -> tuple[dict, int]:
    """Drop packages dominated within a unit: another costs no more, gives no less benefit and uses no more of
    any capped resource. Safe with caps (unlike E7 attempt 7's pruning, which ignored caps and was therefore
    switched off whenever a cap existed). Exact ties keep the first package."""
    out, n = {}, 0
    for u, o in opts.items():
        keep = []
        for j, (p, b, c, w) in enumerate(o):
            dom = False
            for k, (q, b2, c2, w2) in enumerate(o):
                if k == j or not (c2 <= c and b2 >= b and all(x <= y for x, y in zip(w2, w))):
                    continue
                if c2 < c or b2 > b or any(x < y for x, y in zip(w2, w)) or k < j:
                    dom = True
                    break
            if dom:
                n += 1
            else:
                keep.append((p, b, c, w))
        out[u] = keep
    return out, n


def _scale(*xs) -> float:
    """The magnitude tolerances are relative to (no absolute floor of 1, so tiny objectives or costs are not
    swamped); 1.0 only when every value is zero."""
    m = max((abs(x) for x in xs), default=0.0)
    return m if m > 0 else 1.0


def _cost_scale(opts, b0) -> float:
    """Scale of the budget constraint: the budget and the cheapest possible spend, never an outlier option's
    cost (Codex review 2: a 1e9 option must not loosen a budget of 1)."""
    return _scale(b0, sum(min(abs(x[2]) for x in o) for o in opts.values()))


def _cap_scale(opts, k, mx) -> float:
    return _scale(mx, sum(min(x[3][k] for x in o) for o in opts.values()))


def _limits(opts, caps, b0):
    """(budget limit, [cap limits]) with tolerances relative to each constraint's own limit."""
    return (b0 + BUDGET_RTOL * _cost_scale(opts, b0),
            [float(cp["max"]) + BUDGET_RTOL * _cap_scale(opts, k, float(cp["max"])) for k, cp in enumerate(caps)])


def drop_impossible(opts, blim, cmax) -> tuple[dict, int]:
    """Drop options that cannot be part of any feasible allocation: their own cost (or cap use) plus the
    cheapest choice in every other unit already exceeds the limit."""
    K = len(cmax)
    mc = {u: min(x[2] for x in o) for u, o in opts.items()}
    mw = {u: [min(x[3][k] for x in o) for k in range(K)] for u, o in opts.items()}
    tc, tw = sum(mc.values()), [sum(mw[u][k] for u in opts) for k in range(K)]
    out, n = {}, 0
    for u, o in opts.items():
        keep = [x for x in o if tc - mc[u] + x[2] <= blim
                and all(tw[k] - mw[u][k] + x[3][k] <= cmax[k] for k in range(K))]
        n += len(o) - len(keep)
        out[u] = keep
    return out, n


def _baseline(opts) -> float:
    """The benefit every allocation gets anyway (each unit's least benefit); gains are measured above it."""
    return sum(min(x[1] for x in o) for o in opts.values())


def check(choice, benefit, cost, feasible, budget, caps=None) -> list[str]:
    """Independent check of an allocation: one feasible package per unit, within budget and caps."""
    us, opts, caps, names, b0 = _prepare(benefit, cost, feasible, budget, caps)
    errs = []
    if not isinstance(choice, dict) or set(choice) != set(us):
        return ["the allocation must give exactly one package for every unit"]
    tot, use = 0.0, [0.0] * len(caps)
    for u in us:
        hit = [x for x in opts[u] if x[0] == choice[u]]
        if not hit:
            errs.append(f"unit {u}: package {choice[u]} is not feasible")
            continue
        tot += hit[0][2]
        use = [a + b for a, b in zip(use, hit[0][3])]
    if tot > b0 + CHECK_RTOL * _scale(b0, tot):
        errs.append(f"cost {tot:,.4f} exceeds the budget {b0:,.4f}")
    for k, (n, cp, x) in enumerate(zip(names, caps, use)):
        if x > float(cp["max"]) + CHECK_RTOL * _scale(float(cp["max"]), x):
            errs.append(f"cap {n}: use {x:g} exceeds {cp['max']:g}")
    return errs


def _result(method, us, opts, choice, status, t0, names, **extra):
    r = {"method": method, "status": status, "choice": choice, "objective": None, "cost": None,
         "cap_use": None, "seconds": time.time() - t0, **extra}
    if choice is not None:
        pick = {u: next(x for x in opts[u] if x[0] == choice[u]) for u in us}
        r["objective"] = sum(x[1] for x in pick.values())
        r["cost"] = sum(x[2] for x in pick.values())
        r["cap_use"] = {n: sum(x[3][i] for x in pick.values()) for i, n in enumerate(names)}
    return r


def _ilp(us, opts, caps, names, blim, cmax, time_limit):
    pulp = _pulp()
    if pulp is None:
        raise ImportError("method ilp needs PuLP (requirements.txt pins PuLP==3.3.0)")
    t0 = time.time()
    prob = pulp.LpProblem("alloc", pulp.LpMaximize)
    x = {(i, j): pulp.LpVariable(f"x_{i}_{j}", cat="Binary") for i, u in enumerate(us) for j in range(len(opts[u]))}
    # Coefficients normalised to O(1): CBC's tolerances are absolute, so raw objectives of 1e-9 or 1e9 would
    # let it call a suboptimal allocation optimal (Codex review 1). The objective is each unit's gain over its
    # least-benefit option, so a large benefit every allocation gets anyway (a locked unit) cannot swamp the
    # gains being optimised (Codex review 2); constraints are scaled by their own limit, not by outlier options.
    base = {u: min(y[1] for y in opts[u]) for u in us}
    bs = _scale(*(y[1] - base[u] for u in us for y in opts[u]))
    cs = _cost_scale(opts, blim)
    prob += pulp.lpSum(((opts[u][j][1] - base[u]) / bs) * x[i, j]
                       for i, u in enumerate(us) for j in range(len(opts[u])))
    for i, u in enumerate(us):
        prob += pulp.lpSum(x[i, j] for j in range(len(opts[u]))) == 1
    prob += pulp.lpSum((opts[u][j][2] / cs) * x[i, j]
                       for i, u in enumerate(us) for j in range(len(opts[u]))) <= blim / cs
    for k, cp in enumerate(caps):
        ws = _cap_scale(opts, k, cmax[k])
        terms = [(opts[u][j][3][k] / ws) * x[i, j] for i, u in enumerate(us) for j in range(len(opts[u]))
                 if opts[u][j][3][k]]
        if terms:
            prob += pulp.lpSum(terms) <= cmax[k] / ws
    prob.solve(pulp.PULP_CBC_CMD(msg=False, timeLimit=time_limit, gapRel=0.0, gapAbs=0.0))
    sol = prob.sol_status
    if sol == pulp.LpSolutionInfeasible or prob.status == pulp.LpStatusInfeasible:
        return _result("ilp", us, opts, None, "infeasible", t0, names, solver_status=pulp.LpStatus[prob.status])
    if sol not in (pulp.LpSolutionOptimal, pulp.LpSolutionIntegerFeasible):
        return _result("ilp", us, opts, None, "no_feasible_found", t0, names, solver_status=pulp.LpStatus[prob.status])
    choice = {}
    for i, u in enumerate(us):
        on = [j for j in range(len(opts[u])) if (x[i, j].value() or 0) > 0.5]
        if len(on) != 1:
            raise OptimizerError(f"ilp: unit {u} has {len(on)} packages selected in the solver's solution")
        choice[u] = opts[u][on[0]][0]
    return _result("ilp", us, opts, choice, "optimal" if sol == pulp.LpSolutionOptimal else "feasible", t0, names,
                   solver_status=pulp.LpStatus[prob.status])


def _hull(o, start):
    """Upper concave hull of one unit's (cost, benefit) points from its start package: indices into o."""
    pts = sorted(range(len(o)), key=lambda j: (o[j][2], -o[j][1]))
    hull = [start]
    for j in pts:
        if o[j][2] <= o[start][2] or o[j][1] <= o[hull[-1]][1]:
            continue
        while len(hull) >= 2:
            a, b = hull[-2], hull[-1]
            s1 = (o[b][1] - o[a][1]) / (o[b][2] - o[a][2])
            s2 = (o[j][1] - o[b][1]) / (o[j][2] - o[b][2])
            if s2 >= s1:
                hull.pop()
            else:
                break
        hull.append(j)
    return hull


def _greedy(us, opts, caps, names, blim, cmax):
    t0 = time.time()
    pos = {u: min(range(len(opts[u])), key=lambda j: (opts[u][j][2], -opts[u][j][1], sum(opts[u][j][3])))
           for u in us}
    spent = sum(opts[u][pos[u]][2] for u in us)
    use = [sum(opts[u][pos[u]][3][k] for u in us) for k in range(len(caps))]
    if spent > blim or any(x > m for x, m in zip(use, cmax)):
        return _result("greedy", us, opts, None, "no_feasible_found", t0, names, bound=None)
    hulls = {u: _hull(opts[u], pos[u]) for u in us}
    steps = []
    for ui, u in enumerate(us):
        h = hulls[u]
        for k in range(1, len(h)):
            a, b = opts[u][h[k - 1]], opts[u][h[k]]
            steps.append(((b[1] - a[1]) / (b[2] - a[2]), ui, k, b[1] - a[1]))
    steps.sort(key=lambda s: (-s[0], s[1], s[2]))
    bound = max((s[3] for s in steps), default=0.0)
    at = {u: 0 for u in us}
    for _r, ui, k, _db in steps:
        u = us[ui]
        if at[u] != k - 1:
            continue
        a, b = opts[u][hulls[u][k - 1]], opts[u][hulls[u][k]]
        nu = [x + (wb - wa) for x, wa, wb in zip(use, a[3], b[3])]
        if spent + (b[2] - a[2]) <= blim and all(x <= m for x, m in zip(nu, cmax)):
            at[u], spent, use = k, spent + (b[2] - a[2]), nu
    cur = {u: hulls[u][at[u]] for u in us}
    while True:     # best affordable single-unit change (any feasible package), until none improves
        best = None
        for u in us:
            a = opts[u][cur[u]]
            for j, b in enumerate(opts[u]):
                g = b[1] - a[1]
                if g <= 1e-12 * max(abs(a[1]), abs(b[1])):
                    continue
                nu = [x + (wb - wa) for x, wa, wb in zip(use, a[3], b[3])]
                if spent + (b[2] - a[2]) <= blim and all(x <= m for x, m in zip(nu, cmax)):
                    if best is None or g > best[0]:
                        best = (g, u, j, nu)
        if best is None:
            break
        g, u, j, nu = best
        spent += opts[u][j][2] - opts[u][cur[u]][2]
        cur[u], use = j, nu
    return _result("greedy", us, opts, {u: opts[u][cur[u]][0] for u in us}, "heuristic", t0, names,
                   bound=bound if not caps else None)


def search_space(opts: dict) -> int:
    n = 1
    for o in opts.values():
        n *= max(1, len(o))
    return n


def _brute(us, opts, caps, names, blim, cmax, limit):
    t0 = time.time()
    if search_space(opts) > limit:
        raise ValueError(f"brute: search space {search_space(opts):.3g} exceeds brute_limit {limit:.3g}")
    n, K = len(us), len(caps)
    o = [sorted(opts[u], key=lambda x: -x[1]) for u in us]
    smin_c = [0.0] * (n + 1)
    smax_b = [0.0] * (n + 1)
    smin_w = [[0.0] * K for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        smin_c[i] = smin_c[i + 1] + min(x[2] for x in o[i])
        smax_b[i] = smax_b[i + 1] + max(x[1] for x in o[i])
        smin_w[i] = [smin_w[i + 1][k] + min(x[3][k] for x in o[i]) for k in range(K)]
    best = [-math.inf, None]
    pick = [None] * n

    def dfs(i, c, b, w):
        if c + smin_c[i] > blim or any(w[k] + smin_w[i][k] > cmax[k] for k in range(K)):
            return
        if b + smax_b[i] <= best[0]:
            return
        if i == n:
            best[0], best[1] = b, list(pick)
            return
        for x in o[i]:
            pick[i] = x[0]
            dfs(i + 1, c + x[2], b + x[1], [w[k] + x[3][k] for k in range(K)])

    sys.setrecursionlimit(max(sys.getrecursionlimit(), n + 100))
    dfs(0, 0.0, 0.0, [0.0] * K)
    if best[1] is None:
        return _result("brute", us, opts, None, "infeasible", t0, names)
    return _result("brute", us, opts, dict(zip(us, best[1])), "optimal", t0, names)


def solve(benefit, cost, feasible, budget, caps=None, method="ilp", time_limit=300, brute_limit=2_000_000):
    if method not in METHOD_NAMES:
        raise ValueError(f"method must be one of {', '.join(METHOD_NAMES)}")
    us, opts, caps, names, b0 = _prepare(benefit, cost, feasible, budget, caps)
    base = _baseline(opts)
    opts, npruned = prune(opts)
    blim, cmax = _limits(opts, caps, b0)
    opts, nimp = drop_impossible(opts, blim, cmax)
    if any(not o for o in opts.values()):
        r = _result(method, us, opts, None, "infeasible" if method in EXACT else "no_feasible_found", time.time(),
                    names, bound=None)
    elif method == "ilp":
        r = _ilp(us, opts, caps, names, blim, cmax, time_limit)
    elif method == "greedy":
        r = _greedy(us, opts, caps, names, blim, cmax)
    else:
        r = _brute(us, opts, caps, names, blim, cmax, brute_limit)
    r["n_pruned_dominated"] = npruned
    r["n_impossible"] = nimp
    r["baseline"] = base
    r["search_space"] = search_space(opts)
    if r["choice"] is not None:
        bad = check(r["choice"], benefit, cost, feasible, budget, caps)
        if bad:
            raise OptimizerError(f"{method} returned an allocation that violates its constraints: {'; '.join(bad)}")
    return r


def _better(a: float, b: float, base: float = 0.0) -> bool:
    """a beats b, comparing gains above the common baseline (so a large fixed benefit cannot hide a
    disagreement), with room for the rounding of the baseline itself."""
    ga, gb = a - base, b - base
    return ga > gb + BEAT_RTOL * max(abs(ga), abs(gb)) + 1e-12 * abs(base)


def assert_consistent(results: dict, primary: str) -> None:
    """Fail on solver defects: a primary with no allocation, an exact primary not proven optimal, any method
    beating an exact one, exact methods disagreeing, or greedy outside its stated bound."""
    p = results[primary]
    if p["choice"] is None:
        raise OptimizerError(f"primary {primary} found no feasible allocation (status {p['status']})")
    if primary in EXACT and p["status"] != "optimal":
        raise OptimizerError(f"primary {primary} is not proven optimal (status {p['status']}, "
                             f"solver {p.get('solver_status')}); raise time_limit or use another primary")
    exact = {m: r for m, r in results.items() if r["status"] == "optimal"}
    for m, r in results.items():
        if r["objective"] is None:
            if m in EXACT and r["status"] == "infeasible" and any(x["choice"] is not None for x in results.values()):
                raise OptimizerError(f"{m} reports infeasible but another method found a feasible allocation")
            continue
        for e, re in exact.items():
            if e != m and _better(r["objective"], re["objective"], re.get("baseline", 0.0)):
                raise OptimizerError(f"{m} ({r['objective']:.6g}) beats the exact method {e} "
                                     f"({re['objective']:.6g}): a solver defect, not a footnote")
    g = results.get("greedy")
    if g and g.get("bound") is not None and g["objective"] is not None and exact:
        opt = max(r["objective"] for r in exact.values())
        base = g.get("baseline", 0.0)
        if opt - g["objective"] > g["bound"] + BEAT_RTOL * abs(opt - base) + 1e-12 * abs(base):
            raise OptimizerError(f"greedy is {opt - g['objective']:.6g} below the optimum, outside its bound "
                                 f"{g['bound']:.6g}: a greedy defect")


def _one(benefit, cost, feasible, budget, caps, methods, time_limit, brute_limit):
    return {m: solve(benefit, cost, feasible, budget, caps, m, time_limit, brute_limit) for m in methods}


def benchmark(benefit, cost, feasible, budget, caps=None, primary="ilp", methods=None, run_dir=None, out=None,
              objective_name="benefit", sweep_budgets=None, time_limit=300, brute_limit=2_000_000) -> dict:
    """Run every applicable method, check them against each other, and write the optimizer quality report."""
    us, opts, caps_l, names, b0 = _prepare(benefit, cost, feasible, budget, caps)
    if caps is None:
        caps = caps_l
    popts, npruned = prune(opts)
    skipped = {}
    if methods is None:
        methods = []
        for m in ("ilp", "greedy", "brute"):
            if m == "ilp" and _pulp() is None:
                skipped[m] = "PuLP not installed"
            elif m == "brute" and search_space(popts) > brute_limit:
                skipped[m] = f"search space {search_space(popts):.3g} > brute_limit {brute_limit:.3g}"
            else:
                methods.append(m)
    if primary not in methods:
        raise ValueError(f"primary {primary} is not among the methods run ({methods}; skipped {skipped})")
    if len(methods) < 2:
        raise ValueError(f"a benchmark needs at least two methods; ran {methods}, skipped {skipped}")
    res = _one(benefit, cost, feasible, budget, caps, methods, time_limit, brute_limit)
    assert_consistent(res, primary)
    sweep = []
    for sb in sweep_budgets or []:
        rs = _one(benefit, cost, feasible, sb, caps, methods, time_limit, brute_limit)
        assert_consistent(rs, primary)
        objs = {m: r["objective"] for m, r in rs.items() if r["objective"] is not None}
        bm = max(objs, key=objs.get)
        sweep.append({"budget": float(sb), "primary_objective": objs[primary], "best_method": METHOD_NAMES[bm],
                      "best_objective": objs[bm],
                      "gap_pct": 100.0 * (objs[bm] - objs[primary]) / objs[bm] if objs[bm] > 0 else None,
                      "objectives": {METHOD_NAMES[m]: v for m, v in objs.items()}})
    p = res[primary]
    objs = {m: r["objective"] for m, r in res.items() if r["objective"] is not None}
    best = max(objs.values())
    notes = [f"Written by toolkit/alloc/optimize.py benchmark(); every allocation re-checked against budget, "
             f"caps and feasibility. {len(us)} units, {sum(len(o) for o in opts.values())} feasible unit-package "
             f"pairs, {npruned} dominated pairs pruned (cap-aware)."]
    if res.get("ilp"):
        notes.append(f"ILP: PuLP + CBC, relative gap 0, time limit {time_limit} s, solver status "
                     f"{res['ilp'].get('solver_status')}.")
    g = res.get("greedy")
    if g and g.get("bound") is not None:
        notes.append(f"Greedy without caps: exact - greedy <= the largest hull step benefit, {g['bound']:.6g}.")
    elif g:
        notes.append("Greedy with caps: no a-priori bound; heuristic only.")
    for m, why in skipped.items():
        notes.append(f"{METHOD_NAMES[m]} not run: {why}.")
    notes.append("gap_pct is relative to the total objective; gain_gap_pct is relative to the gain above "
                 "baseline_objective (the benefit every allocation gets, e.g. from locked units).")
    if best <= 0:
        notes.append(f"The best objective is {best:g} (nothing gained at this budget): gap_pct is undefined, and "
                     "scripts/optimization_quality.py reports such a file as MALFORMED.")
    report = {
        "primary_method": METHOD_NAMES[primary],
        "primary_objective": float(p["objective"]),
        "objective_name": objective_name,
        "primary_status": p["status"],
        "budget": float(b0),
        "primary_cost": float(p["cost"]),
        "benchmark_methods": [{"method": METHOD_NAMES[m], "objective": float(r["objective"]),
                               "runtime_sec": round(r["seconds"], 4), "status": r["status"],
                               "cost": float(r["cost"])}
                              for m, r in res.items() if m != primary and r["objective"] is not None],
        "gap_pct": 100.0 * (best - p["objective"]) / best if best > 0 else None,
        "baseline_objective": float(p["baseline"]),
        "gain_gap_pct": (100.0 * (best - p["objective"]) / (best - p["baseline"])
                         if best - p["baseline"] > 0 else None),
        "constraints": [{"name": n, "max": float(cp["max"]), "use": float(p["cap_use"][n])}
                        for n, cp in zip(names, caps)],
        "n_units": len(us),
        "n_pruned_dominated": npruned,
        "notes": " ".join(notes),
    }
    if sweep:
        report["sweep"] = sweep
    path = out or (os.path.join(run_dir, "models", "optimization_quality.yaml") if run_dir else None)
    if path:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w") as f:
            yaml.safe_dump(report, f, sort_keys=False)
    report["results"] = res
    report["path"] = path
    return report


# --------------------------------------------------------------------------- self-test

RR1, RR2 = 0.6, 0.4      # design §3.5: C1 universal (relative risk 0.6), C2 targeted (relative risk 0.4)


def _planted_world():
    """40 units in 4 groups; C1 universal, C2 targeted to an eligible subgroup; costs from toolkit/alloc/costs.py."""
    units = []
    for i in range(40):
        g, k = divmod(i, 10)
        pop = 10000 + 700 * k + 50 * g
        units.append({"id": f"u{i:02d}", "group": f"g{g}", "pop": pop, "elig_pop": round(0.2 * pop),
                      "burden": round([0.05, 0.10, 0.20, 0.40][g] * (1 + 0.013 * k + 0.001 * g), 6),
                      "c2_eligible": "yes" if g >= 2 else "no"})
    units[35]["c2_eligible"] = "no"            # the eligibility trap: a high-burden unit outside C2's subgroup
    spec = {
        "units": {"id": "id", "populations": {"total": "pop", "eligible": "elig_pop"}},
        "components": {"c1": {}, "c2": {"eligible": "eligible"}},
        "packages": {"none": [], "c1": ["c1"], "c2": ["c2"], "c1+c2": ["c1", "c2"]},
        "prices": {"base_year": 2024, "index": {"2015": 237.0, "2024": 313.7}},
        "cost_items": [
            {"id": "c1_delivered", "component": "c1", "value": 2.0, "price_year": 2015, "price_rule": "index",
             "basis": "per_person", "includes": ["commodity", "delivery"]},
            {"id": "c2_all_in", "component": "c2", "value": 1.0, "price_year": 2024, "price_rule": "index",
             "basis": "per_eligible", "times": 4, "includes": ["drug", "delivery"]}],
        "eligibility": {"c2": {"column": "c2_eligible"}},
        "locks": {"u03": {"package": "c1", "reason": "already delivered"},
                  "u37": {"package": "none", "reason": "no access this cycle"}},
    }
    cm = costs.cost_matrix(spec, units)
    cost = {u: {p: v["total"] for p, v in ps.items()} for u, ps in cm.items()}
    benefit = {}
    for u in units:
        rest, el, bu = u["pop"] - u["elig_pop"], u["elig_pop"], u["burden"]
        benefit[u["id"]] = {p: bu * (rest * (1 - (RR1 if "c1" in cs else 1.0))
                                     + el * (1 - (RR1 if "c1" in cs else 1.0) * (RR2 if "c2" in cs else 1.0)))
                            for p, cs in ((p, set(cs)) for p, cs in spec["packages"].items())}
    return spec, units, benefit, cost


def _closed_form(benefit, cost, feas, k_steps):
    """The planted optimum: with separable benefits and the budget at the k-th breakpoint of the sorted
    incremental-ratio sequence, the LP relaxation's optimum is integral, so it is the exact optimum."""
    choice, steps = {}, []
    for u, f in feas.items():
        pts = sorted([(cost[u][p], benefit[u][p], p) for p, v in f.items() if v], key=lambda t: (t[0], -t[1]))
        hull = [pts[0]]
        for c, b, p in pts[1:]:
            if b <= hull[-1][1]:
                continue
            while len(hull) >= 2 and (b - hull[-1][1]) / (c - hull[-1][0]) >= \
                    (hull[-1][1] - hull[-2][1]) / (hull[-1][0] - hull[-2][0]):
                hull.pop()
            hull.append((c, b, p))
        choice[u] = hull[0]
        steps += [((h[1] - g[1]) / (h[0] - g[0]), u, h) for g, h in zip(hull, hull[1:])]
    steps.sort(key=lambda s: -s[0])
    ratios = [s[0] for s in steps]
    assert all(a - b > 1e-12 for a, b in zip(ratios, ratios[1:])), "planted world has tied ratios"
    for _r, u, h in steps[:k_steps]:
        choice[u] = h
    return ({u: h[2] for u, h in choice.items()}, sum(h[1] for h in choice.values()),
            sum(h[0] for h in choice.values()), steps)


def _self_test() -> int:
    import tempfile

    fails = []

    def ok(c, m):
        if not c:
            fails.append(m)

    def raises(fn, exc, text=""):
        try:
            fn()
        except exc as e:
            return text in str(e)
        return False

    spec, units, benefit, cost = _planted_world()
    F = packages.feasibility(spec, units)
    feas = F["feasible"]
    # eligibility trap and locks, before any optimisation
    ok(not feas["u35"]["c2"] and not feas["u35"]["c1+c2"] and feas["u35"]["c1"],
       "TC1 the eligibility trap: C2 is infeasible in the ineligible high-burden unit")
    ok(cost["u35"]["c2"] > 0 and benefit["u35"]["c2"] / cost["u35"]["c2"] > benefit["u20"]["c2"] / cost["u20"]["c2"],
       "TC2 ... and the trap is attractive: C2 there would be costed and look cost-effective")
    ok([p for p, v in feas["u03"].items() if v] == ["c1"] and [p for p, v in feas["u37"].items() if v] == ["none"],
       "TC3 two locked units have only their locked package")
    # the planted optimum, no cap
    want, want_obj, B, steps = _closed_form(benefit, cost, feas, 30)
    ok(abs(sum(cost[u][p] for u, p in want.items()) - B) < 1e-6, "TC4 the budget is at a breakpoint")
    r = solve(benefit, cost, feas, B, method="ilp")
    ok(r["status"] == "optimal" and r["choice"] == want and abs(r["objective"] - want_obj) < 1e-6,
       f"TC5 the ILP recovers the planted allocation exactly: {r['objective']} vs {want_obj}; "
       f"diff {[u for u in want if r['choice'].get(u) != want[u]]}")
    g = solve(benefit, cost, feas, B, method="greedy")
    ok(g["choice"] == want, "TC6 greedy is exact at a breakpoint (no cap)")
    ok(r["choice"]["u03"] == "c1" and r["choice"]["u37"] == "none" and want["u37"] == "none",
       "TC7 locks respected")
    free = solve(benefit, cost, {u: {p: True for p in ps} for u, ps in feas.items()}, B, caps=[], method="ilp")
    ok(free["choice"]["u35"] in ("c2", "c1+c2") and free["choice"]["u37"] != "none",
       "TC8 without the feasibility table the optimum walks into the trap and over the lock (the trap bites)")
    ok(r["choice"]["u35"] in ("none", "c1"), "TC9 ... and with it, it does not")
    # off a breakpoint: greedy within its stated bound
    Bm = B + 0.5 * (steps[30][2][0] - cost[steps[30][1]][want[steps[30][1]]])
    e, g = solve(benefit, cost, feas, Bm, method="ilp"), solve(benefit, cost, feas, Bm, method="greedy")
    ok(e["objective"] >= g["objective"] - 1e-9 and e["objective"] - g["objective"] <= g["bound"] + 1e-9,
       f"TC10 off a breakpoint greedy is within the stated bound: gap {e['objective'] - g['objective']:.4g}, "
       f"bound {g['bound']:.4g}")
    # a binding cap
    n_c2 = sum(p in ("c2", "c1+c2") for p in want.values())
    capspec = {**spec, "caps": {"c2": {"max_units": n_c2 - 3}}}
    Fc = packages.feasibility(capspec, units)
    rc = solve(benefit, cost, Fc, B, method="ilp")
    used = sum(p in ("c2", "c1+c2") for p in rc["choice"].values())
    ok(n_c2 >= 4 and used <= n_c2 - 3 and rc["cap_use"]["c2.max_units"] == used and rc["objective"] < want_obj,
       f"TC11 the cap binds and is respected: {used} of max {n_c2 - 3} (uncapped {n_c2})")
    gc = solve(benefit, cost, Fc, B, method="greedy")
    ok(gc["choice"] is not None and sum(p in ("c2", "c1+c2") for p in gc["choice"].values()) <= n_c2 - 3
       and gc["objective"] <= rc["objective"] + 1e-9, "TC12 greedy respects the cap and does not beat the ILP")
    ok(solve(benefit, cost, Fc, B, caps=[], method="ilp")["objective"] == r["objective"],
       "TC13 caps=[] drops the feasibility table's caps explicitly; caps=None uses them")
    # brute force on a subset confirms the planted optimum and the capped ILP
    sub = ["u03", "u05", "u19", "u22", "u35", "u28", "u33", "u37"]
    sb = {u: benefit[u] for u in sub}
    sc = {u: cost[u] for u in sub}
    sf = {u: feas[u] for u in sub}
    w2, w2obj, B2, _s = _closed_form(sb, sc, sf, 7)
    br, il = solve(sb, sc, sf, B2, method="brute"), solve(sb, sc, sf, B2, method="ilp")
    ok(br["choice"] == w2 == il["choice"] and abs(br["objective"] - w2obj) < 1e-6,
       "TC14 brute force on a subset confirms the closed-form optimum and the ILP")
    n2 = sum("c2" in p for p in w2.values())
    scap = [{"name": "c2.max_units", "max": float(n2 - 1),
             "weights": {u: {p: float("c2" in p) for p in feas[u]} for u in sub}}]
    brc, ilc = solve(sb, sc, sf, B2, caps=scap, method="brute"), solve(sb, sc, sf, B2, caps=scap, method="ilp")
    ok(abs(brc["objective"] - ilc["objective"]) < 1e-6 and brc["objective"] < br["objective"]
       and n2 >= 2 and brc["cap_use"]["c2.max_units"] <= n2 - 1, "TC15 brute force confirms the capped ILP on the subset")
    # E7 attempt 7's defect: pruning that ignores caps; and the ILP must not be beaten
    tb = {"a": {"x": 5.0, "y": 4.0, "none": 0.0}}
    tc = {"a": {"x": 1.0, "y": 2.0, "none": 0.0}}
    tf = {"a": {"x": True, "y": True, "none": True}}
    tcap = [{"name": "x_units", "max": 0, "weights": {"a": {"x": 1.0, "y": 0.0, "none": 0.0}}}]
    for m in ("ilp", "greedy", "brute"):
        ok(solve(tb, tc, tf, 10, caps=tcap, method=m)["choice"] == {"a": "y"},
           f"TC16 ({m}) a package dominated except in its cap use is kept when the dominating one is capped")
    ok(prune({"a": [("x", 5.0, 1.0, ()), ("y", 4.0, 2.0, ())]})[1] == 1, "TC17 ... and pruned when there is no cap")
    fake = {"ilp": {"choice": {"a": "y"}, "objective": 4.0, "status": "optimal"},
            "greedy": {"choice": {"a": "x"}, "objective": 5.0, "status": "heuristic", "bound": None}}
    ok(raises(lambda: assert_consistent(fake, "ilp"), OptimizerError, "beats the exact method"),
       "TC18 a heuristic beating the exact method is an error, not a footnote")
    ok(raises(lambda: assert_consistent({**fake, "ilp": {**fake["ilp"], "objective": 6.0, "status": "feasible"}},
                                        "ilp"), OptimizerError, "not proven optimal"),
       "TC19 an ILP stopped before proving optimality is not reported as the optimum")
    ok(check({u: "c2" for u in feas}, benefit, cost, F, 1e12) != [], "TC20 the check rejects infeasible pairs")
    ok(any("exceeds the budget" in x for x in check(want, benefit, cost, F, B * 0.9)), "TC21 ... and overspending")
    # fail closed on bad input; an infeasible instance
    bad = {**benefit, "u00": {**benefit["u00"], "c1": float("nan")}}
    ok(raises(lambda: solve(bad, cost, F, B), ValueError, "not a finite number"), "TC22 a NaN benefit fails")
    ok(raises(lambda: solve({u: benefit[u] for u in sub}, cost, F, B), ValueError, "units differ"),
       "TC23 mismatched units fail")
    lock_cost = cost["u03"]["c1"]
    for m in ("ilp", "greedy", "brute"):
        rr = solve(sb, sc, sf, lock_cost * 0.5, method=m)
        ok(rr["choice"] is None and rr["status"] in ("infeasible", "no_feasible_found"),
           f"TC24 ({m}) a budget below the locked units' cost is infeasible, not a wrong answer: {rr['status']}")
    # the benchmark writes a report that the repo's checker accepts
    with tempfile.TemporaryDirectory() as d:
        rep = benchmark(benefit, cost, F, B, run_dir=d, objective_name="cases_averted", sweep_budgets=[Bm, 0.5 * B])
        spec_oq = importlib.util.spec_from_file_location(
            "optimization_quality", os.path.join(HERE, "..", "..", "scripts", "optimization_quality.py"))
        oq = importlib.util.module_from_spec(spec_oq)
        spec_oq.loader.exec_module(oq)
        v = oq.validate_optimization_quality(os.path.join(d, "models", "optimization_quality.yaml"))
        ok(v["verdict"] == "CLEAN" and abs(v["gap_pct"] - rep["gap_pct"]) < 1e-9 and rep["gap_pct"] == 0.0,
           f"TC25 the written optimization_quality.yaml passes scripts/optimization_quality.py: {v}")
        ok({b["method"] for b in yaml.safe_load(open(rep["path"]))["benchmark_methods"]} == {"greedy"}
           and "brute_force not run" in rep["notes"], "TC26 brute force is skipped, and says so, on 40 units")
        rg = benchmark(sb, sc, sf, B2, caps=scap, primary="greedy", out=os.path.join(d, "g.yaml"))
        vg = oq.validate_optimization_quality(os.path.join(d, "g.yaml"))
        ok(vg["verdict"] in ("CLEAN", "GAP_TOO_LARGE") and abs(vg["gap_pct"] - rg["gap_pct"]) < 1e-9
           and rg["primary_objective"] <= max(b["objective"] for b in rg["benchmark_methods"]),
           f"TC27 a greedy primary is reported with its true gap to the exact methods: {vg}")
        # Codex review 1: tiny (or huge) objectives must not fall inside a solver's absolute tolerance
        for scale in (1e-9, 1e9):
            tb = {"a": {"none": 0.0, "x": 1.0 * scale}, "b": {"none": 0.0, "x": 2.0 * scale}}
            tc = {"a": {"none": 0.0, "x": 1.0}, "b": {"none": 0.0, "x": 1.0}}
            tf = {u: {"none": True, "x": True} for u in tb}
            rs = {m: solve(tb, tc, tf, 1.0, method=m)["choice"] for m in ("ilp", "greedy", "brute")}
            ok(all(c == {"a": "none", "b": "x"} for c in rs.values()),
               f"TC28 (Codex) objectives of scale {scale:g} are optimised exactly by every method: {rs}")
            rb = benchmark(tb, tc, tf, 1.0)
            ok(rb["gap_pct"] == 0.0, f"TC29 ... and the benchmark agrees at scale {scale:g}")
        # Codex round 2: an outlier option must not loosen the budget or a cap
        tb = {"a": {"none": 0.0, "small": 1.0, "huge": 2.0}}
        tf = {"a": {"none": True, "small": True, "huge": True}}
        for m in ("ilp", "greedy", "brute"):
            r1 = solve(tb, {"a": {"none": 0.0, "small": 2.0, "huge": 1e9}}, tf, 1.0, caps=[], method=m)
            r2 = solve(tb, {"a": {"none": 0.0, "small": 0.0, "huge": 0.0}}, tf, 1.0, method=m,
                       caps=[{"name": "k", "max": 0, "weights": {"a": {"none": 0.0, "small": 1.0, "huge": 1e9}}}])
            ok(r1["choice"] == {"a": "none"} and r2["choice"] == {"a": "none"},
               f"TC31 (Codex r2, {m}) an outlier option's cost or cap use does not widen the tolerance")
        ok(check({"a": "small"}, tb, {"a": {"none": 0.0, "small": 2.0, "huge": 1e9}}, tf, 1.0, caps=[]) != [],
           "TC32 (Codex r2) the independent check rejects the overspend")
        # Codex round 2: a large benefit every allocation gets (a locked unit) must not hide a wrong optimum
        tb = {"L": {"lock": 1e9}, "A": {"none": 0.0, "x": 26.0}, "B": {"none": 0.0, "x": 23.0}}
        tc = {"L": {"lock": 0.0}, "A": {"none": 0.0, "x": 7.0}, "B": {"none": 0.0, "x": 6.0}}
        tf = {u: {p: True for p in ps} for u, ps in tb.items()}
        rs = {m: solve(tb, tc, tf, 8.0, method=m)["choice"] for m in EXACT}   # greedy takes B (higher ratio)
        ok(all(c["A"] == "x" and c["B"] == "none" for c in rs.values()),
           f"TC33 (Codex r2) a large fixed benefit does not swamp the gains being optimised: {rs}")
        ok(_better(1e9 + 26, 1e9 + 23, 1e9), "TC34 ... and a disagreement above a large baseline is detected")
        rb = benchmark(tb, tc, tf, 8.0)
        ok(rb["gain_gap_pct"] == 0.0 and rb["baseline_objective"] == 1e9, "TC35 the gain-relative gap is reported")
        # Codex review 3: a budget that buys nothing gives an undefined gap, said so, and no crash
        rz = benchmark({"a": {"none": 0.0, "x": 1.0}}, {"a": {"none": 0.0, "x": 1.0}},
                       {"a": {"none": True, "x": True}}, 0.0, out=os.path.join(d, "z.yaml"))
        ok(rz["gap_pct"] is None and "gap_pct is undefined" in rz["notes"]
           and oq.validate_optimization_quality(os.path.join(d, "z.yaml"))["verdict"] == "MALFORMED",
           "TC30 (Codex) a zero best objective: gap undefined, noted, and the checker fails closed")
        # Phase 42 ε: a run imports the toolkit as a package beside its own models/costs.py;
        # neither may shadow the other (the bug: the toolkit's `import costs` returning the run's file).
        import subprocess
        decoy = os.path.join(d, "runmodels")
        os.makedirs(decoy)
        with open(os.path.join(decoy, "costs.py"), "w") as f:
            f.write("DECOY = True\n")
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import costs; sys.path.insert(0, sys.argv[2]);"
                "from toolkit.alloc import optimize, packages; import costs as again;"
                "print(getattr(optimize.costs, 'DECOY', False), packages.costs is optimize.costs,"
                " again is costs, optimize.HERE in sys.path, hasattr(optimize.costs, 'cost_matrix'))")
        pr = subprocess.run([sys.executable, "-c", code, decoy, os.path.dirname(os.path.dirname(HERE))],
                            capture_output=True, text=True, timeout=120)
        ok(pr.stdout.split() == ["False", "True", "True", "False", "True"],
           f"TC36 imported as toolkit.alloc beside a run's costs.py, neither shadows the other: "
           f"{pr.stdout.strip()} {pr.stderr.strip()[-300:]}")
    if fails:
        print("SELF-TEST FAILURES:")
        for m in fails:
            print("  -", m)
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("spec", nargs="?")
    ap.add_argument("--units")
    ap.add_argument("--benefit")
    ap.add_argument("--budget", type=float)
    ap.add_argument("--primary", default="ilp", choices=sorted(METHOD_NAMES))
    ap.add_argument("--run-dir")
    ap.add_argument("--objective-name", default="benefit")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)
    if a.self_test:
        return _self_test()
    if not (a.spec and a.benefit and a.budget is not None):
        ap.error("spec, --benefit and --budget are required")
    import csv

    spec = yaml.safe_load(open(a.spec))
    units = costs.load_units(spec, os.path.dirname(os.path.abspath(a.spec)), a.units)
    try:
        F = packages.feasibility(spec, units)
        cm = costs.cost_matrix(spec, units)
        benefit = {}
        with open(a.benefit, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                benefit.setdefault(str(row["unit"]), {})[str(row["package"])] = float(row["benefit"])
        rep = benchmark(benefit, cm, F, a.budget, primary=a.primary, run_dir=a.run_dir,
                        objective_name=a.objective_name)
    except (ValueError, KeyError, OptimizerError) as e:
        print(f"ERROR: {type(e).__name__}: {e}")
        return 1
    print(f"{rep['primary_method']}: {rep['primary_objective']:,.4g} at cost {rep['primary_cost']:,.0f} "
          f"(budget {rep['budget']:,.0f}); gap "
          f"{'n/a' if rep['gap_pct'] is None else format(rep['gap_pct'], '.4g') + '%'}; status {rep['primary_status']}")
    if rep["path"]:
        print(f"wrote {rep['path']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
