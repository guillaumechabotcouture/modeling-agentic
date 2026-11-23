#!/usr/bin/env python3
"""The numbers the decision figures need beyond the other artifacts (design: docs/design/v2-figures.md §4).

    python3 toolkit/analysis/figure_data.py --self-test

`compute(A, plan, out0, ben0, cost0, uq)` uses the run's own Analysis (its emulator or engine, and the toolkit/alloc
solver through `Analysis.solve`); it runs no engine and has no optimiser of its own. It returns the payload of
`analysis/figure_data.yaml`:

  frontier             the plan re-solved at fractions of the declared envelope (fixed costs held), and the marginal
                       cost per outcome averted at the envelope (central difference over the 0.9-1.1 points)
  dollars              the plan's dollars by component and by region, the fixed costs and the envelope
  by_package           per package in the plan: units, cost, outcome averted, cost per outcome averted, and its
                       interval over the uncertainty draws, replayed with uncertainty.Drawer and the spec's seed (the
                       replay must reproduce the stored fixed-plan interval, else the interval is withheld). On direct
                       evaluation the uncertainty step's own per-draw sums (STORED_REL) are read instead of re-running
                       the engine for every draw; the first REPLAY_CHECK_DRAWS draws are re-evaluated and must give
                       the stored values, every draw's package sums must add up to its total, and the totals must
                       give the stored interval, else the interval is withheld as before
  component_increment  per component: outcome averted and cost added in the units that get it (each unit's package
                       against the same package without the component, where that package exists)
  burden               per unit: the comparator's primary outcome per person, and the population
  calibration          per unit: the modelled calibration metric at the fitted attribute, where the calibration
                       module offers it
  schedule             per region: the delivery events of the engine's schedule hook (toolkit/engines/hooks.py), or
                       the reason it is not available

`rebuild(run_dir)` reconstructs the Analysis of a finished emulator-path run from its stored batch rows (no engine
runs), and refuses unless the rows' engine identity matches this code's engine and the rebuilt plan equals the stored
one. Nothing here names a disease, component or unit.
"""
from __future__ import annotations

import json
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

PRODUCER = "toolkit/analysis/figure_data.py"
FRACTIONS = (0.25, 0.5, 0.75, 0.9, 1.0, 1.1, 1.25, 1.5)
REPLAY_RTOL = 1e-6
STORED_REL = "analysis/uncertainty_draws.json"      # direct evaluation: the uncertainty step's per-draw plan sums
STORED_SCHEMA = "analysis.uncertainty_draws/1"
REPLAY_CHECK_DRAWS = 2      # stored draws re-evaluated by the figure step (the first ones: the draws are sequential)


class RebuildRefused(RuntimeError):
    pass


def _fixed(spec) -> dict:
    return {k: float(v) for k, v in ((spec["constraints"].get("fixed_costs_usd") or {}).items())}


def plan_within_gap(A, sol, plan, ben, cost) -> bool:
    """1C-c: a stored plan that differs from the re-solved one is the same result when it meets the constraints at the
    budget the plan is solved within and its objective is within the solver's stated gap of the re-solved optimum.
    Both are proven within gap g of the optimum's gain G (over each unit's least-benefit option after pruning: the
    solver's `gain`), so they differ by at most g / (1 - g) of the re-solved gain. Without that gain in `sol`, no
    difference is allowed (Codex r2 P1)."""
    from toolkit.alloc import optimize as aopt
    budget = A.budget if getattr(A, "plan_budget", None) is None else A.plan_budget
    try:      # the allocator's own check (Codex r1 P1-8); a plan it cannot read is not within the gap
        if aopt.check(plan, ben, cost, A.feas, budget):
            return False
        short = float(sol["objective"]) - sum(float(ben[i][plan[i]]) for i in A.ids)
    except (KeyError, TypeError, ValueError):
        return False
    g, gain = float(sol.get("gap_rel") or 0.0), abs(float(sol.get("gain") or 0.0))
    return short <= (g / (1.0 - g) + 1e-9) * gain      # the rounding allowance scales with the gain (P1-7)


def frontier(A, ben0, cost0, out0, fractions=FRACTIONS, events=None) -> dict:
    """The plan re-solved at fractions of the envelope (fixed costs held). A plan that model corrections bear on is
    excluded with the reason (the analysis guard, applied to every frontier plan); an envelope the solver proves
    infeasible is recorded separately, and one the solver could not solve within the stated gap is recorded as not
    solved with its status (1C-c, decision D3: the frontier is exploratory). The marginal is on outcome averted (the
    primary outcome, undiscounted); `objective` is the solver's (discounted) objective."""
    env = float(A.spec["constraints"]["envelope_usd"])
    fixed = sum(_fixed(A.spec).values())
    rows, skipped, infeasible, excluded, not_solved = [], [], [], [], []
    from toolkit.analysis.core import auxiliary
    A.spec.packages()
    bound = getattr(A.spec, "prune_budget", None)      # packages dropped above this budget (within_envelope)
    for f in fractions:
        b = f * env - fixed
        if b <= 0:
            skipped.append({"fraction": f, "reason": "the fixed costs exceed this envelope"})
            continue
        if bound is not None and b > bound * (1 + 1e-12):
            skipped.append({"fraction": f, "reason": "above the declared envelope, and the packages were generated "
                            "within it (options.packages.within_envelope): a plan there could use a dropped package"})
            continue
        try:
            with auxiliary(A, "frontier"):
                sol = A.solve(ben0, cost0, budget=A.planned(b))      # F6 S1-1b: the same reserve fraction
        except RuntimeError as e:
            if str(e).strip() == "optimizer status infeasible":
                infeasible.append({"fraction": f, "envelope_usd": f * env, "reason": "the solver proves no "
                                   "allocation feasible at this envelope"})
                continue
            if getattr(e, "status", None) is None:
                raise
            not_solved.append({"fraction": f, "envelope_usd": f * env, "status": e.status, "reason": "the solver "
                               "returned no plan proven within the stated gap at this envelope"})
            continue
        hits = A.plan_corrections(list(events or []), sol["choice"])
        if hits:
            excluded.append({"fraction": f, "envelope_usd": f * env, "reason": f"{len(hits)} model corrections "
                             "(extrapolation, clips, floors) bear on this plan", "examples": hits[:3]})
            continue
        s = A.summary(sol["choice"], out0, cost0)
        rows.append({"fraction": f, "envelope_usd": f * env, "budget_allocatable": b,
                     **({"budget_planned": A.planned(b)} if getattr(A, "reserve_fraction", None) is not None else {}),
                     "objective": sol["objective"],
                     "cost": s["cost_allocated"], "averted": s["averted"], "packages": s["packages"], "mix": s["mix"]})
    by = {r["fraction"]: r for r in rows}
    marg = None
    if 0.9 in by and 1.1 in by:
        d_out = by[1.1]["averted"][A.primary] - by[0.9]["averted"][A.primary]
        d_usd = by[1.1]["cost"] - by[0.9]["cost"]
        marg = {"from_fraction": 0.9, "to_fraction": 1.1, "outcome": A.primary,
                "outcome_per_usd": d_out / d_usd if d_usd > 0 else None,
                "usd_per_outcome": d_usd / d_out if d_out > 0 else None}
    return {"envelope_usd": env, "fixed_costs_usd": fixed, "points": rows, "skipped": skipped,
            "infeasible": infeasible, "excluded": excluded, "not_solved": not_solved, "marginal": marg,
            "discount_factor": A.dfac}


def dollars(A, plan, out0) -> dict:
    from toolkit.alloc import costs as acosts
    units = [A.by_id[i] for i in A.ids]
    fn = lambda u, p: out0[str(u[A.spec.uid])]["cost"][p]  # noqa: E731
    cm = acosts.cost_matrix(A.alloc, units, outcome=fn)
    item_comp = {it["id"]: it["component"] for it in A.spec["options"]["cost_items"]}
    comps = list(A.spec["options"]["components"])
    by_c = {c: 0.0 for c in comps}
    by_r = {}
    for i in A.ids:
        r = str(A.by_id[i].get(A.region, "")) if A.region else ""
        row = by_r.setdefault(r, {c: 0.0 for c in comps})
        for it, v in cm[i][plan[i]]["items"].items():
            by_c[item_comp[it]] += v
            row[item_comp[it]] += v
    return {"envelope_usd": float(A.spec["constraints"]["envelope_usd"]), "fixed_costs_usd": _fixed(A.spec),
            "budget_allocatable": float(A.budget), "by_component": by_c, "by_region": by_r,
            "allocated_usd": sum(by_c.values())}


def _package_sums(A, plan, out, cost) -> dict:
    s = {}
    for i in A.ids:
        k = plan[i]
        e = s.setdefault(k, {"units": 0, "cost": 0.0, "averted": 0.0, "averted_abs": 0.0})
        v = out[i]["y"][A.comp][A.primary] - out[i]["y"][k][A.primary]
        e["units"] += 1
        e["cost"] += cost[i][k]
        e["averted"] += v
        e["averted_abs"] += abs(v)       # the units' magnitudes: the round-off bound of the stored-draw check
    return s


def _close(a, b) -> bool:
    return abs(float(a) - float(b)) <= REPLAY_RTOL * max(abs(float(b)), 1.0)


def _finite(x) -> bool:
    """A real number a float holds finitely (an int too large for a float, or inf/nan, is not)."""
    import math
    if not isinstance(x, (int, float)) or isinstance(x, bool):
        return False
    try:
        return math.isfinite(float(x))
    except OverflowError:
        return False


def _well_formed(per_draw, totals) -> bool:
    """Every stored draw is {package: {units: positive int, cost: finite number >= 0, averted: finite number}} and
    every total a finite number (Codex r2 P1-3, P1-4)."""
    return isinstance(per_draw, list) and isinstance(totals, list) and all(_finite(t) for t in totals) and all(
        isinstance(d, dict) and all(isinstance(e, dict) and isinstance(e.get("units"), int)
                                    and not isinstance(e.get("units"), bool) and e["units"] > 0
                                    and _finite(e.get("cost")) and float(e["cost"]) >= 0.0
                                    and _finite(e.get("averted")) and _finite(e.get("averted_abs"))
                                    and float(e["averted_abs"]) >= 0.0 for e in d.values()) for d in per_draw)


def _sums_match(per: dict, total, key: str = "averted") -> bool:
    """The package subtotals of one draw add up to its total. The total and the subtotals are sums of the same per-unit
    values in different orders, so they may differ by summation round-off: at most about n x eps x the sum of the
    absolute values over n units (Codex r2 P1-1: a bound on round-off, not a relative tolerance of the largest
    subtotal, which let cancelling subtotals hide a real error), plus the replay tolerance on the total."""
    import math
    try:
        vals = [float(e[key]) for e in per.values()]
        # Codex r3 P1-1: the magnitudes of the unit contributions (cancellation can happen inside a package too);
        # costs are non-negative, so their subtotals are their magnitudes
        mags = [abs(float(e["averted_abs"] if key == "averted" else e[key])) for e in per.values()]
        n = max(1, sum(int(e["units"]) for e in per.values()))
        err = abs(math.fsum(vals) - float(total))
        big = max(mags + [1.0])
        rel = math.fsum(m / big for m in mags)          # Codex r3 P1-2: scaled, so the bound does not overflow
        bound = REPLAY_RTOL * max(abs(float(total)), 1.0) + 4.0 * n * 2.0 ** -52 * rel * big
        return math.isfinite(err) and err <= bound
    except (OverflowError, ValueError, TypeError, KeyError):
        return False


def _interval(tot, lvl) -> tuple:
    """(quantile summary, None) of the stored totals, or ({}, why) when it cannot be computed or a statistic is not
    finite (Codex r3 P1-3: an overflowing mean is a warning in numpy, not an exception)."""
    import math
    import warnings
    from toolkit.analysis.core import q
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            got = q(tot, lvl)
    except (ValueError, OverflowError) as e:
        return {}, f"the stored totals give no interval ({e})"
    if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in got.values()):
        return {}, "the stored totals give a non-finite statistic"
    return got, None


def _same_sums(a: dict, b: dict) -> bool:
    return set(a) == set(b) and all(a[k]["units"] == b[k]["units"] and _close(a[k]["cost"], b[k]["cost"])
                                    and _close(a[k]["averted"], b[k]["averted"]) for k in a)


def stored_record(plan_draws: dict) -> dict:
    """The sidecar (STORED_REL) from uncertainty.run()'s `plan_draws`."""
    return dict({"schema": STORED_SCHEMA, "producer": "toolkit/analysis/uncertainty.py"}, **plan_draws)


def _replay_stored(A, plan, uq, stored, log=None) -> dict:
    """replay() from the uncertainty step's stored per-draw sums, checked three ways (any failure: not reproduced, with
    the reason): every draw's package sums add up to its total; the first REPLAY_CHECK_DRAWS draws, re-evaluated with
    the same Drawer and seed, give the stored values; the stored totals give the stored fixed-plan interval."""
    from toolkit.analysis import uncertainty as UQ
    from toolkit.analysis.core import q
    nd, seed = int(uq["draws"]), int(uq["seed"])
    lvl = float((uq.get("interval") or {}).get("level", 0.95))
    want = (uq.get("fixed_plan") or {}).get(A.primary) or {}
    tot, per = list(stored.get("primary_averted") or []), list(stored.get("per_draw") or [])
    k = min(REPLAY_CHECK_DRAWS, nd)
    costs = (uq.get("affordability") or {}).get("per_draw_cost") or []
    why, checked = None, 0
    if stored.get("schema") != STORED_SCHEMA or stored.get("draws") != nd or stored.get("seed") != seed \
            or len(tot) != nd or len(per) != nd:
        why = "the stored draws are not this uncertainty step's (schema, number of draws or seed differ)"
    elif stored.get("primary") != A.primary or {str(i): v for i, v in (stored.get("plan") or {}).items()} \
            != {i: plan[i] for i in A.ids}:
        why = "the stored draws are for another plan or primary outcome"
    elif not _well_formed(per, tot):
        why = ("the stored draws are malformed (a package entry lacks units, cost or averted, a number is not finite, "
               "or a cost is negative)")
    elif len(costs) != nd:
        why = "the uncertainty record has no per-draw plan cost to check the stored costs against"
    else:
        # Codex r1 P1-1: every draw's package costs add up to the plan cost the uncertainty step recorded for it
        # (affordability.per_draw_cost), and its package outcomes to its stored total
        bad = [d for d in range(nd) if not _sums_match(per[d], tot[d])]
        badc = [d for d in range(nd) if not _sums_match(per[d], costs[d], key="cost")]
        want_units = {}
        for i in A.ids:
            want_units[plan[i]] = want_units.get(plan[i], 0) + 1
        # Codex r2 P1-2: each draw holds exactly the plan's packages, each with the plan's unit count
        badu = [d for d in range(nd) if {k: e["units"] for k, e in per[d].items()} != want_units]
        if bad:
            why = f"{len(bad)} stored draws' package sums do not add up to their total (e.g. draw {bad[0]})"
        elif badc:
            why = (f"{len(badc)} stored draws' package costs do not add up to the plan cost the uncertainty step recorded "
                   f"(e.g. draw {badc[0]})")
        elif badu:
            why = f"{len(badu)} stored draws do not hold the plan's packages with their units (e.g. draw {badu[0]})"
    if why is None:
        D = UQ.Drawer(A, seed)
        D.prepare(nd)
        P = UQ.Progress(log, "figure data: re-evaluating stored draws", k)
        for d in range(k):
            D.last_clamped = []
            theta, zs, noise, mult, _s = D.draw()
            out, _ben, cost = A.evaluate(theta, zs, noise=noise, cost_mult=mult, sampled=_s)
            t = sum(out[i]["y"][A.comp][A.primary] - out[i]["y"][plan[i]][A.primary] for i in A.ids)
            if not (_close(t, tot[d]) and _same_sums(_package_sums(A, plan, out, cost), per[d])):
                why = f"draw {d}, re-evaluated, does not give the stored values"
                checked += 1
                break
            checked += 1
            P.step()
    got = {}
    if why is None:
        got, why = _interval(tot, lvl)
    if why is None and not (want and all(_close(got[x], want[x]) for x in ("median", "ci_low", "ci_high"))):
        why = "the stored draws do not give the stored fixed-plan interval"
    return {"draws": nd, "seed": seed, "level": lvl, "reproduced": why is None, "replayed": got, "stored": want,
            "per_draw": per, "method": "stored_draws", "checked_draws": checked, **({"reason": why} if why else {})}


def replay(A, plan, uq: dict, stored: dict | None = None, log=None) -> dict:
    """Per-package sums over the uncertainty step's draws (same Drawer, seed and call order); `reproduced` says
    whether the replay gives the stored fixed-plan interval of the primary outcome. With `stored` (the uncertainty
    step's per-draw sums, STORED_REL) they are read and checked instead (_replay_stored)."""
    if stored is not None:
        return _replay_stored(A, plan, uq, stored, log=log)
    from toolkit.analysis import uncertainty as UQ
    from toolkit.analysis.core import q
    nd, seed = int(uq["draws"]), int(uq["seed"])
    D = UQ.Drawer(A, seed)
    D.prepare(nd)
    tot, per = [], []
    P = UQ.Progress(log, "figure data: replaying draws", nd)
    for _ in range(nd):
        D.last_clamped = []
        theta, zs, noise, mult, _s = D.draw()
        out, _ben, cost = A.evaluate(theta, zs, noise=noise, cost_mult=mult, sampled=_s)
        tot.append(sum(out[i]["y"][A.comp][A.primary] - out[i]["y"][plan[i]][A.primary] for i in A.ids))
        per.append(_package_sums(A, plan, out, cost))
        P.step()
    lvl = float((uq.get("interval") or {}).get("level", 0.95))
    got, want = q(tot, lvl), (uq.get("fixed_plan") or {}).get(A.primary) or {}
    same = bool(want) and all(abs(got[k] - float(want[k])) <= REPLAY_RTOL * max(abs(float(want[k])), 1.0)
                              for k in ("median", "ci_low", "ci_high"))
    return {"draws": nd, "seed": seed, "level": lvl, "reproduced": same, "replayed": got, "stored": want,
            "per_draw": per}


def by_package(A, plan, out0, cost0, rep: dict | None) -> dict:
    """Per package in the plan: units, cost, outcome averted and cost per outcome averted (None when nothing is
    averted: the figure shows such packages as harmful or undefined), with its interval over the replayed draws.
    The interval is withheld, with the reason and the share of draws, when the replay did not reproduce or some
    draws avert nothing."""
    from toolkit.analysis.core import q
    pt = _package_sums(A, plan, out0, cost0)
    out = {}
    for k, e in pt.items():
        row = dict(e, components=sorted(A.pk[k]),
                   usd_per_outcome=e["cost"] / e["averted"] if e["averted"] > 0 else None)
        if rep and rep.get("reproduced"):
            ds = [d[k] for d in rep["per_draw"] if k in d]
            bad = [d for d in ds if not d["averted"] > 0]
            if len(ds) != rep["draws"]:
                row["usd_per_outcome_interval"] = None
                row["interval_withheld"] = {"reason": "the package is missing from some draws",
                                            "share_draws_nonpositive": None}
            elif bad:
                row["usd_per_outcome_interval"] = None
                row["interval_withheld"] = {"reason": "the package averts nothing (or harms) in some draws, where cost "
                                            "per outcome averted is undefined",
                                            "share_draws_nonpositive": len(bad) / len(ds)}
            else:
                row["usd_per_outcome_interval"] = q([d["cost"] / d["averted"] for d in ds], rep["level"])
        elif rep is not None:
            row["usd_per_outcome_interval"] = None
            row["interval_withheld"] = {"reason": "the draw replay did not reproduce the stored interval",
                                        "share_draws_nonpositive": None}
        out[k] = row
    return out


def component_increment(A, plan, ben0, cost0, events=None) -> dict:
    """Per component, the outcome averted and cost added in the units that get it, against the same package without
    it. A unit whose counterfactual package model corrections bear on is left out and counted."""
    by_set = {cs: k for k, cs in A.pk.items()}
    ev = list(events or [])
    res = {}
    for c in A.spec["options"]["components"]:
        r = {"units": 0, "units_without_counterpart": 0, "units_with_corrections": 0, "averted": 0.0, "cost": 0.0}
        for i in A.ids:
            cs = A.pk[plan[i]]
            if c not in cs:
                continue
            alt = by_set.get(frozenset(set(cs) - {c}))
            if alt is None:
                r["units_without_counterpart"] += 1
                continue
            if A.plan_corrections(ev, {i: alt}):
                r["units_with_corrections"] += 1
                continue
            r["units"] += 1
            r["averted"] += (ben0[i][plan[i]] - ben0[i][alt]) / max(A.dfac, 1e-12)
            r["cost"] += cost0[i][plan[i]] - cost0[i][alt]
        r["outcome_per_usd"] = r["averted"] / r["cost"] if r["cost"] > 0 else None
        res[c] = r
    return res


def burden(A, out0) -> dict:
    pops = A.spec["decision_units"]["populations"]
    res = {}
    for i in A.ids:
        n = sum(float(A.by_id[i][pm["column"]]) for pm in pops.values())
        res[i] = {"population": n, "per_person": out0[i]["y"][A.comp][A.primary] / n if n > 0 else None,
                  "comparator_total": out0[i]["y"][A.comp][A.primary]}
    return res


def calibration_modelled(A, z0) -> dict | None:
    from toolkit.analysis import calibration as C
    fn = getattr(C, "modelled_targets", None)
    if fn is None or A.method != "emulator_inversion":
        return None
    return {"metric": A.spec["calibration"].get("metric"), "units": fn(A.spec, A.model, A.theta0, z0)}


def schedule(A, plan, z0) -> dict:
    """Per region, the engine's delivery events (hooks.schedule) in the comparator arm (history and routine events,
    every arm) and in each unit's plan arm (decision events), with unit counts."""
    from toolkit.analysis.batch import calibrated_value
    from toolkit.engines import hooks
    from toolkit.engines.contract import Package, UnitSpec
    eng = A.eng
    if not hooks.supports(eng, "schedule"):
        return {"status": "not_offered", "reason": str(hooks._unsupported("schedule", eng))}
    h = A.spec.horizon({})
    cal_attr = A.spec["calibration"].get("attribute")
    regions = {}
    for i in A.ids:
        u = A.by_id[i]
        a = dict(A.spec.unit_attributes(u))
        if cal_attr and z0.get(i) is not None:
            a[cal_attr] = calibrated_value(A.spec, z0[i])
        us = UnitSpec(i, a)
        r = str(u.get(A.region, "")) if A.region else ""
        ev = [e for e in hooks.schedule(eng, A.theta0, us, Package(A.comp, A.spec.engine_package(A.pk[A.comp])), h)
              if e["kind"] != "decision"]
        ev += [e for e in hooks.schedule(eng, A.theta0, us, Package(plan[i], A.spec.engine_package(A.pk[plan[i]])), h)
               if e["kind"] == "decision"]
        reg = regions.setdefault(r, {"units": 0, "events": {}})
        reg["units"] += 1
        for e in ev:
            key = json.dumps([e["component"], e["label"], e["kind"], round(e["t0"], 4),
                              None if e["t1"] is None else round(e["t1"], 4)])
            reg["events"][key] = reg["events"].get(key, 0) + 1
    out = {}
    for r, reg in regions.items():
        out[r] = {"units": reg["units"], "events": [dict(zip(("component", "label", "kind", "t0", "t1"), json.loads(k)),
                                                         units=n) for k, n in sorted(reg["events"].items())]}
    d = h.delivery if getattr(h, "delivery", None) is not None else h.window
    return {"status": "ok", "delivery": [float(x) for x in d], "window": [float(x) for x in h.window],
            "start": float(h.start), "regions": out}


def compute(A, plan, out0, ben0, cost0, uq: dict | None, z0: dict | None = None, log=print, events=None,
            stored: dict | None = None) -> dict:
    """`events`: the model corrections of the point evaluation (out0); every plan the figures show is guarded with
    them. With events=None the rebuild's own (A.point_events) are used. `stored`: the uncertainty step's per-draw sums
    (STORED_REL), read and checked instead of replaying every draw (direct evaluation)."""
    ev = list(events if events is not None else (getattr(A, "point_events", None) or []))
    rep = None
    if uq and uq.get("draws"):
        rep = replay(A, plan, uq, stored=stored, log=log)
        if not rep["reproduced"]:
            log(f"figure_data: the draw replay did not reproduce the stored interval ({rep.get('reason') or ''} "
                f"{rep['replayed']} vs {rep['stored']}); package intervals withheld")
    try:
        sch = schedule(A, plan, z0 or {})
    except Exception as e:  # noqa: BLE001 - a malformed schedule is recorded, not fatal for the other figures
        sch = {"status": "invalid", "reason": f"{type(e).__name__}: {e}"}
    out = {"primary": A.primary, "comparator": A.comp, "region": A.region,
           "frontier": frontier(A, ben0, cost0, out0, events=ev), "dollars": dollars(A, plan, out0),
           "by_package": by_package(A, plan, out0, cost0, rep),
           "replay": {k: rep[k] for k in ("draws", "seed", "level", "reproduced", "replayed", "stored", "method",
                                          "checked_draws", "reason") if k in rep} if rep
           else {"reproduced": False, "reason": "no uncertainty draws"},
           "component_increment": component_increment(A, plan, ben0, cost0, events=ev), "burden": burden(A, out0),
           "calibration": calibration_modelled(A, z0 or {}), "schedule": sch}
    if getattr(A, "rebuild_info", None):
        out["rebuild"] = A.rebuild_info
    return out


def derived_fields(spec_raw: dict) -> list[str]:
    """The job inputs computed by a transcendental function, so a platform's libm can move their last bits: the
    calibrated attribute on the log scale (batch.calibrated_value: exp(z)). Every other input is source-bound (copied
    from the units table, the spec or the design) and must match exactly."""
    cal = (spec_raw or {}).get("calibration") or {}
    if cal.get("attribute") and cal.get("scale", "log") == "log":
        return [f"unit.{cal['attribute']}"]
    return []


ULP_BOUND = 4          # a stored row's job may differ from the rebuilt one by at most this many ulps in one input


# --------------------------------------------------------------------------- offline rebuild
def _canon(x):
    return json.loads(json.dumps(x, sort_keys=True, default=str))


def _float_leaves(x, path=()):
    if isinstance(x, dict):
        for k, v in x.items():
            yield from _float_leaves(v, path + (k,))
    elif isinstance(x, list):
        for k, v in enumerate(x):
            yield from _float_leaves(v, path + (k,))
    elif isinstance(x, float):
        yield path, x


def ulp_match(job: dict, stored_sha: str, job_sha, bound: int = ULP_BOUND, fields=()):
    """The single derived input of `job` (a dotted path in `fields`, e.g. unit.<attribute>) that, moved by at most
    `bound` ulps, reproduces `stored_sha`: (dotted path, ulps), or None. Exactly one candidate must match; inputs not
    in `fields` are never moved."""
    hits = []
    allowed = set(fields or ())
    for path, v in _float_leaves({k: job[k] for k in ("unit", "params") if k in job}):
        if ".".join(str(p) for p in path) not in allowed:
            continue
        for k in [x for n in range(1, bound + 1) for x in (n, -n)]:
            w = v
            for _ in range(abs(k)):
                w = float(np.nextafter(w, np.inf if k > 0 else -np.inf))
            jj = json.loads(json.dumps(job))
            tgt = jj
            for p in path[:-1]:
                tgt = tgt[p]
            tgt[path[-1]] = w
            if job_sha(jj) == stored_sha:
                hits.append((".".join(str(p) for p in path), k))
    return hits[0] if len(hits) == 1 else None


def rebuild(run_dir: str, log=print):
    """(A, plan, out0, ben0, cost0, z0) of a finished emulator-path run, from its stored batch (no engine runs).

    Fail closed: the stored design and every stored batch file must have the hashes batch.yaml recorded; every row
    must carry this code's engine identity; a row whose job hash differs from the rebuilt job is taken only when one
    derived input (derived_fields: exp of the calibrated attribute) moved by at most ULP_BOUND ulps reproduces its
    stored hash, recorded in A.rebuild_info; every source-bound input must match exactly; the rebuilt plan must equal the stored plan. A.point_events
    holds the point evaluation's model corrections."""
    import yaml
    from toolkit.analysis import batch as Bt
    from toolkit.analysis.artifacts import sha256
    from toolkit.analysis.core import Analysis
    from toolkit.analysis.model import EmulatorModel
    from toolkit.engines.contract import engine_identity
    from toolkit.spec import schema
    run_dir = os.path.abspath(run_dir)
    spec = schema.load(os.path.join(run_dir, "decision_spec.yaml"))
    if spec["analysis"]["evaluation"] != "emulator":
        raise RebuildRefused("the run evaluates the engine directly; a rebuild would run the engine")
    eng = spec.engine()
    rows = os.path.join(run_dir, "analysis", "batch", "results.jsonl")
    if not os.path.exists(rows):
        raise RebuildRefused("no stored batch rows (analysis/batch/results.jsonl)")
    bdoc = yaml.safe_load(open(os.path.join(run_dir, "analysis", "batch.yaml")))
    for rel, h in (bdoc.get("inputs") or {}).items():
        if rel.startswith("analysis/batch/") and sha256(os.path.join(run_dir, rel)) != h:
            raise RebuildRefused(f"{rel} is not the file batch.yaml recorded (hash differs): the stored batch changed "
                                 "after the analysis")
    ident = engine_identity(eng)
    design, jobs = Bt.design(spec)
    stored = json.load(open(os.path.join(run_dir, "analysis", "batch", "design.json")))
    if _canon(stored) != _canon(design):
        raise RebuildRefused("the stored batch design differs from the spec's design (a shipped batch?)")
    res, by_ulp, other = {}, [], []
    dfields = derived_fields(spec.raw)
    with open(rows) as f:
        for line in f:
            r = json.loads(line)
            if not r.get("ok") or not isinstance(r.get("i"), int) or r["i"] >= len(jobs):
                continue
            body = r.get("result") or {}
            pv = body.get("provenance") or {}
            job = jobs[r["i"]]
            if pv.get("engine") != ident:
                pe = pv.get("engine") or {}
                raise RebuildRefused(f"row {r['i']} ({job['id']}) was computed by engine {pe.get('name')} "
                                     f"v{pe.get('version')} (code {str(pe.get('code_sha256', ''))[:12]}); this code's "
                                     f"engine is v{ident['version']} (code {ident['code_sha256'][:12]}): re-render with "
                                     "the run's own code")
            if pv.get("job_sha") != Bt.job_sha(job):
                m = ulp_match(job, pv.get("job_sha"), Bt.job_sha, fields=dfields)
                if m is None:
                    other.append(job["id"])
                    continue
                by_ulp.append({"job": job["id"], "field": m[0], "ulps": m[1]})
            res[job["id"]] = body["result"]
    if other:
        raise RebuildRefused(f"{len(other)} rows' inputs differ from the rebuilt jobs by more than one derived input "
                             f"({', '.join(dfields) or 'none declared'}) within {ULP_BOUND} ulps (e.g. {other[:3]})")
    if len(res) != len(jobs):
        raise RebuildRefused(f"{len(jobs) - len(res)} jobs have no stored row")
    if by_ulp:
        log(f"rebuild: {len(by_ulp)} rows match their rebuilt job within {ULP_BOUND} ulps in one input "
            f"(e.g. {by_ulp[0]})")
    model = EmulatorModel(spec, design, res, log=log).fit()
    A = Analysis(spec, model, log=log)
    from toolkit.analysis import burden as BV
    BV.restore_anchor(A, run_dir)        # F6 S2-5d review r1 P2-9: the run's anchor, as its evaluations had it
    A.fit_calibration()
    cal0 = A.calibrate(A.theta0)
    z0 = {i: v[0] for i, v in cal0.items()}
    out0, ben0, cost0 = A.evaluate(A.theta0, z0)
    A.point_events = list(A.last_events)
    point = yaml.safe_load(open(os.path.join(run_dir, "analysis", "point.yaml")))["data"]
    if ((spec["uncertainty"].get("affordability") or {}).get("enforce")):
        # F6 S1-1b: an enforced run's plan was solved within its reserve; restore it before solving
        rv = point.get("reserve")
        f = rv.get("fraction") if isinstance(rv, dict) else None
        if not (isinstance(f, (int, float)) and not isinstance(f, bool) and 0 < f <= 1
                and abs(float(rv.get("budget_planned", -1)) - f * A.budget) <= 1e-9 * max(1.0, A.budget)):
            raise RebuildRefused("the spec enforces the affordability rule but point.yaml has no consistent reserve")
        A.set_reserve(f)
    from toolkit.analysis import variants as V
    if point.get("headline_variant") != V.headline_id(spec.raw):
        raise RebuildRefused(f"point.yaml's headline_variant {point.get('headline_variant')!r} is not the spec's "
                             f"plan_variants.headline {V.headline_id(spec.raw)!r}")
    V.apply_headline(A)          # F6 S1-6: a variant headline's rule, as the run solved it
    sol = A.solve(ben0, cost0)
    plan = {str(k): v for k, v in point["plan"].items()}
    diff = [i for i in A.ids if sol["choice"][i] != plan.get(i)]
    if diff and not (set(plan) == set(A.ids) and plan_within_gap(A, sol, plan, ben0, cost0)):
        raise RebuildRefused(f"the rebuilt plan differs from the stored plan in {len(diff)} units, by more than the "
                             "solver's stated gap")
    A.rebuild_info = {"rows": len(res), "by_ulp": by_ulp, "ulp_bound": ULP_BOUND,
                      "fields": sorted({x["field"] for x in by_ulp}), "plan_reproduced": True,
                      "units_differing_within_gap": len(diff)}
    return A, plan, out0, ben0, cost0, z0


# --------------------------------------------------------------------------- self-test
def _self_test() -> int:
    import copy
    import tempfile
    import yaml
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)

    from toolkit.analysis import run as RUN
    from toolkit.spec import schema, signoff
    q = lambda *a, **k: None  # noqa: E731
    with tempfile.TemporaryDirectory() as d:
        bench, v_ok, v_bad = RUN._toy_bench(d)
        schema.toy_spec(d, n=24, bench=bench, dt_values=(v_ok, v_bad))
        signoff.sign(d, "lead", note="self-test")
        R = RUN.run(d, log=q, compute_kw={"max_workers": 2})
        fd = yaml.safe_load(open(os.path.join(d, "analysis", "figure_data.yaml")))["data"]
        fr = fd["frontier"]["points"]
        objs = [p["objective"] for p in fr]
        ok(len(fr) >= 6 and all(b >= a - 1e-9 for a, b in zip(objs, objs[1:])),
           f"FD1 the frontier re-solves the plan at {len(fr)} envelopes and never loses value as the budget grows")
        at1 = next(p for p in fr if p["fraction"] == 1.0)
        ok(abs(at1["objective"] - R["point"]["solver"]["objective"]) <= 1e-6 * abs(at1["objective"]),
           "FD2 at the declared envelope the frontier equals the plan's objective")
        ok(all(p["cost"] <= p["budget_allocatable"] * (1 + 1e-9) for p in fr), "FD3 every frontier plan is within its budget")
        dl = fd["dollars"]
        ok(abs(dl["allocated_usd"] - R["point"]["cost_allocated"]) <= 1e-6 * R["point"]["cost_allocated"]
           and abs(sum(sum(r.values()) for r in dl["by_region"].values()) - dl["allocated_usd"]) <= 1e-6 * dl["allocated_usd"],
           "FD4 dollars by component and by region add up to the plan's cost")
        bp = fd["by_package"]
        ok(sum(v["units"] for v in bp.values()) == 24
           and abs(sum(v["cost"] for v in bp.values()) - R["point"]["cost_allocated"]) <= 1e-6 * R["point"]["cost_allocated"],
           "FD5 per-package units and costs add up to the plan")
        ok(fd["replay"]["reproduced"] is True and all(v.get("usd_per_outcome_interval") for v in bp.values()
                                                      if v["averted"] > 0 and v["units"] > 0),
           f"FD6 the draw replay reproduces the stored interval, so package intervals are given: {fd['replay']}")
        ok("method" not in fd["replay"] and not os.path.exists(os.path.join(d, STORED_REL)),
           "FD19 an emulator run keeps the full replay: no stored-draws sidecar, figure_data's replay record unchanged")
        ok(all(v["per_person"] is not None and v["per_person"] >= 0 for v in fd["burden"].values()),
           "FD7 burden per person for every unit")
        ok(fd["schedule"]["status"] == "not_offered" and "schedule()" in fd["schedule"]["reason"],
           f"FD8 an engine without the schedule hook is recorded as not offered: {fd['schedule']}")
        ok(set(fd["component_increment"]) == {"a", "b"}, "FD9 component increments for every component")
        # rebuild from the stored batch reproduces the plan
        A, plan, out0, ben0, cost0, z0 = rebuild(d, log=q)
        ok(plan == {str(k): v for k, v in R["point"]["plan"].items()}, "FD10 the offline rebuild reproduces the stored plan")
        fd2 = compute(A, plan, out0, ben0, cost0, R["uncertainty"], z0, log=q)
        ok(abs(fd2["frontier"]["points"][-1]["objective"] - fr[-1]["objective"]) <= 1e-6 * abs(fr[-1]["objective"]),
           "FD11 the rebuilt frontier equals the harness's")
        # ---- Codex r1 P1-2: rows are taken only when their own inputs are proven to match
        import hashlib
        import numpy as np
        from toolkit.analysis import batch as Bt
        rows = os.path.join(d, "analysis", "batch", "results.jsonl")
        bpath = os.path.join(d, "analysis", "batch.yaml")
        keep_rows, keep_b = open(rows).read(), open(bpath).read()

        def rewrite(fn):
            out = []
            for n, line in enumerate(keep_rows.splitlines()):
                r = json.loads(line)
                fn(n, r)
                out.append(json.dumps(r))
            open(rows, "w").write("\n".join(out) + "\n")
            b = yaml.safe_load(keep_b)            # as if the batch had been written so on its own platform
            b["inputs"]["analysis/batch/results.jsonl"] = hashlib.sha256(open(rows, "rb").read()).hexdigest()
            yaml.safe_dump(b, open(bpath, "w"), sort_keys=False)
        _d, jobs = Bt.design(A.spec)
        j5 = json.loads(json.dumps(jobs[5]))
        leaf = sorted(k for k, v in j5["params"].items() if isinstance(v, float))[0]
        j5["params"][leaf] = float(np.nextafter(j5["params"][leaf], np.inf))
        rewrite(lambda n, r: r["result"]["provenance"].update(job_sha=Bt.job_sha(j5)) if n == 5 else None)
        try:            # Codex r2 P1-2: a parameter is source-bound (the design's own value); 1 ulp off is refused
            rebuild(d, log=q)
            ok(False, "FD14 a row whose source-bound parameter differs by 1 ulp is refused")
        except RebuildRefused as e:
            ok("within" in str(e), f"FD14 a row whose source-bound parameter differs by 1 ulp is refused: {e}")
        # the tolerance applies only to the derived field (the calibrated attribute on the log scale: exp(z))
        jx = json.loads(json.dumps(jobs[5]))
        jx["unit"]["x"] = float(np.nextafter(float(jx["unit"]["x"]), np.inf))
        ok(ulp_match(jobs[5], Bt.job_sha(jx), Bt.job_sha, fields=["unit.x"]) == ("unit.x", 1)
           and ulp_match(jobs[5], Bt.job_sha(j5), Bt.job_sha, fields=["unit.x"]) is None,
           "FD19 the ulp tolerance covers only the declared derived fields")
        lg = copy.deepcopy(A.spec.raw)
        lg["calibration"]["scale"] = "log"
        ok(derived_fields(lg) == ["unit.x"] and derived_fields(A.spec.raw) == [],
           f"FD19b the derived field is the calibrated attribute on the log scale only: {derived_fields(lg)}")
        rewrite(lambda n, r: r["result"]["provenance"].update(job_sha="0" * 64) if n == 5 else None)
        try:
            rebuild(d, log=q)
            ok(False, "FD14b a row whose inputs cannot be matched within the bound is refused")
        except RebuildRefused as e:
            ok("within" in str(e), f"FD14b a row whose inputs cannot be matched within the bound is refused: {e}")
        rewrite(lambda n, r: r["result"]["provenance"]["engine"].update(version="0") if n == 7 else None)
        try:
            rebuild(d, log=q)
            ok(False, "FD14c a later row from another engine identity is refused")
        except RebuildRefused as e:
            ok("engine" in str(e), f"FD14c a later row from another engine identity is refused: {e}")
        open(rows, "w").write(keep_rows)
        b = yaml.safe_load(keep_b)
        b["inputs"]["analysis/batch/results.jsonl"] = "f" * 64
        yaml.safe_dump(b, open(bpath, "w"), sort_keys=False)
        try:
            rebuild(d, log=q)
            ok(False, "FD14d batch rows that are not the ones the analysis recorded are refused")
        except RebuildRefused as e:
            ok("batch.yaml" in str(e), f"FD14d batch rows that are not the ones the analysis recorded are refused: {e}")
        open(bpath, "w").write(keep_b)
        # ---- Codex r1 P1-3: frontier and component plans are guarded against model corrections
        star = [{"unit": A.ids[0], "package": "*", "kind": "clip"}]
        fr_g = frontier(A, ben0, cost0, out0, events=star)
        ok(fr_g["points"] == [] and len(fr_g["excluded"]) == len(FRACTIONS)
           and all("correction" in x["reason"] for x in fr_g["excluded"]),
           "FD15 frontier plans that model corrections bear on are excluded with the reason")
        cpk = {i: plan[i] for i in A.ids}
        by_set = {cs: k for k, cs in A.pk.items()}
        unit_c = next(i for i in A.ids if len(A.pk[plan[i]]) >= 1)
        c0 = sorted(A.pk[plan[unit_c]])[0]
        alt = by_set[frozenset(set(A.pk[plan[unit_c]]) - {c0})]
        ci = component_increment(A, cpk, ben0, cost0, events=[{"unit": unit_c, "package": alt, "kind": "clip"}])
        ok(ci[c0].get("units_with_corrections") == 1,
           f"FD15b a component increment whose counterfactual package has corrections is excluded: {ci[c0]}")
        # ---- Codex r1 P1-4: an infeasible exploratory envelope is recorded; a genuine solver failure propagates
        orig = A.solve

        def infeasible_low(ben, cost, budget=None, feas=None):
            if budget is not None and budget < 0.6 * A.budget:
                raise RuntimeError("optimizer status infeasible")
            return orig(ben, cost, budget, feas)
        A.solve = infeasible_low
        fr_i = frontier(A, ben0, cost0, out0)
        ok([x["fraction"] for x in fr_i.get("infeasible", [])] == [0.25, 0.5]
           and all(p["fraction"] >= 0.75 for p in fr_i["points"]),
           f"FD16 infeasible exploratory envelopes are recorded separately: {fr_i.get('infeasible')}")

        def broken(ben, cost, budget=None, feas=None):
            from toolkit.analysis import core as CO
            raise CO.SolveFailed("no_feasible_found")
        A.solve = broken
        # 1C-c (D3) reverses the earlier FD16b (such a failure propagated, Codex r1 P1-4): the frontier is exploratory,
        # so the envelope is recorded as not solved with the solver's status, apart from the proven-infeasible ones
        try:
            fr_b = frontier(A, ben0, cost0, out0)
            ns = fr_b.get("not_solved") or []
            ok(fr_b["points"] == [] and fr_b["infeasible"] == [] and len(ns) > 0
               and all(x["status"] == "no_feasible_found" for x in ns),
               f"FD16b a solver failure other than infeasibility is recorded per envelope as not solved: {ns[:1]}")
        except RuntimeError as e:
            ok(False, f"FD16b a solver failure other than infeasibility is recorded, not raised ({e})")
        A.solve = orig
        # 1C-c: a rebuilt plan that differs from the stored one is accepted only when the stored plan is feasible and
        # within the solver's stated gap of the re-solved optimum
        pwg = globals().get("plan_within_gap")
        sol_ = A.solve(ben0, cost0)
        u_ = next(i for i in A.ids if plan[i] != A.comp)
        alt = next(k for k in A.pk if k not in (plan[u_], A.comp) and A.feas["feasible"][u_][k])
        ben_t = {i: dict(v) for i, v in ben0.items()}
        cost_t = {i: dict(v) for i, v in cost0.items()}
        ben_t[u_][alt], cost_t[u_][alt] = ben0[u_][plan[u_]], cost0[u_][plan[u_]]      # a tie: either is optimal
        rich = {i: max(cost0[i], key=cost0[i].get) for i in A.ids}
        ok(pwg is not None and pwg(A, sol_, dict(plan, **{u_: alt}), ben_t, cost_t) is True
           and pwg(A, sol_, dict(plan, **{u_: A.comp}), ben0, cost0) is False and pwg(A, sol_, rich, ben0, cost0) is False,
           "FD10b a stored plan tied with the rebuilt optimum is within the gap; a worse one, or one over the budget, "
           "is not")
        # Codex r1 P1-7, P1-8: the allowance scales with the gain, and a plan the allocator's own check rejects (an
        # unknown package) is not within the gap, without an exception
        tiny = {i: {k: (1e-13 if k == plan[i] and i == u_ else 0.0) for k in ben0[i]} for i in A.ids}
        sol_t = dict(sol_, objective=1e-13, baseline=0.0, gain=1e-13)
        ok(pwg(A, sol_t, dict(plan, **{u_: A.comp}), tiny, cost0) is False
           and pwg(A, sol_, dict(plan, **{u_: "no_such_package"}), ben0, cost0) is False,
           "FD10c a plan that loses a tiny gain entirely is not within the gap; an unknown package is refused, not raised")
        # Codex r2 P1: the allowance is on the gain the solver optimised, not on the objective above the pre-pruning
        # baseline (a large benefit every plan gets must not widen it)
        u0 = next(i for i in A.ids if i != u_)
        big = {i: {k: (1e6 if i == u0 else 1.0 if (i == u_ and k == plan[u_]) else 0.0) for k in ben0[i]} for i in A.ids}
        sol_b = dict(sol_, objective=1e6 + 1.0, baseline=0.0, gain=1.0)
        ok(pwg(A, sol_b, dict(plan, **{u_: A.comp}), big, cost0) is False and pwg(A, sol_b, dict(plan), big, cost0) is True
           and pwg(A, {k: v for k, v in sol_b.items() if k != "gain"}, dict(plan, **{u_: A.comp}), big, cost0) is False,
           "FD10d a plan that gives up the whole optimised gain is not within the gap, whatever every plan gets anyway; "
           "without the solver's gain no difference is allowed")
        # ---- Codex r1 P2-8: the frontier reports outcome averted, not the (possibly discounted) objective
        half = {i: {k: 0.5 * v for k, v in ben0[i].items()} for i in A.ids}
        fr_h = frontier(A, half, cost0, out0)
        pts = {p["fraction"]: p for p in fr_h["points"]}
        want = ((pts[1.1]["averted"][A.primary] - pts[0.9]["averted"][A.primary])
                / (pts[1.1]["cost"] - pts[0.9]["cost"]))
        ok(abs(fr_h["marginal"]["outcome_per_usd"] - want) <= 1e-9 * abs(want),
           "FD18 the marginal is computed on outcome averted, not on the discounted objective")
        # ---- Codex r1 P2-12: package intervals withheld with a reason and the share of draws
        k0 = next(k for k in set(plan.values()) if k != A.comp)
        rep = {"reproduced": True, "draws": 4, "level": 0.95, "per_draw": [
            {k0: {"units": 1, "cost": 10.0, "averted": a}} for a in (5.0, 4.0, -1.0, 6.0)]}
        bp = by_package(A, plan, out0, cost0, rep)
        w = bp[k0].get("interval_withheld") or {}
        ok(bp[k0].get("usd_per_outcome_interval") is None and w.get("share_draws_nonpositive") == 0.25
           and w.get("reason"), f"FD17 a withheld package interval says why and in what share of draws: {w}")
        # a stale engine identity is refused
        rows = os.path.join(d, "analysis", "batch", "results.jsonl")
        txt = open(rows).read().replace('"version": "1"', '"version": "0"', 1)
        open(rows, "w").write(txt)
        b = yaml.safe_load(keep_b)            # the batch as its analysis recorded it, so the identity check is reached
        b["inputs"]["analysis/batch/results.jsonl"] = hashlib.sha256(open(rows, "rb").read()).hexdigest()
        yaml.safe_dump(b, open(bpath, "w"), sort_keys=False)
        try:
            rebuild(d, log=q)
            ok(False, "FD12 a batch from another engine identity is refused")
        except RebuildRefused as e:
            ok("re-render with the run's own code" in str(e), f"FD12 a batch from another engine identity is refused: {e}")
        # schedule hook: a toy engine that offers it
        from toolkit.engines import hooks
        A.eng = hooks.ToyScheduleEngine()
        sch = schedule(A, plan, z0)
        n_dec = sum(e["units"] for r in sch["regions"].values() for e in r["events"] if e["kind"] == "decision")
        ok(sch["status"] == "ok" and n_dec == sum(len(A.pk[plan[i]]) for i in A.ids),
           f"FD13 with the hook, decision events are counted per region for the units whose package has them ({n_dec})")
    try:
        _self_test_stored_draws(ok, q)
    except Exception as e:  # noqa: BLE001 -- a missing feature is a failing case
        ok(False, f"FD20-FD26 the figure step reads the stored draws on direct evaluation ({type(e).__name__}: {e})")
    print("OK: all self-test cases passed." if not fails else f"FAILED: {len(fails)} case(s)")
    return 1 if fails else 0


def _self_test_stored_draws(ok, q):
    """hb1 flu R2: on direct evaluation the figure step re-ran every uncertainty draw's engine evaluations (200 draws,
    killed at the 24 h backstop) after the uncertainty step had computed them. The uncertainty step now stores the
    per-draw package sums of the plan (STORED_REL) and the figure step reads them, re-evaluating only the first
    REPLAY_CHECK_DRAWS draws to check them; `reproduced` stays a check."""
    import copy
    import tempfile
    import yaml
    from toolkit.analysis import run as RUN
    from toolkit.spec import signoff
    with tempfile.TemporaryDirectory() as d:
        RUN._direct_toy(d)
        signoff.sign(d, "lead")
        R = RUN.run(d, log=q)
        doc = yaml.safe_load(open(os.path.join(d, "analysis", "figure_data.yaml")))
        rp = doc["data"]["replay"]
        uq = R["uncertainty"]
        nd = int(uq["draws"])
        k = min(REPLAY_CHECK_DRAWS, nd)
        ok(rp.get("method") == "stored_draws" and rp.get("checked_draws") == k and rp.get("reproduced") is True,
           f"FD20 a direct run's figure step reads the stored draws and checks {k} of them: {rp}")
        side = os.path.join(d, STORED_REL)
        st = json.load(open(side)) if os.path.exists(side) else {}
        ok(st.get("draws") == nd and len(st.get("per_draw") or []) == nd and len(st.get("primary_averted") or []) == nd
           and STORED_REL in (doc.get("inputs") or {}),
           f"FD21 the uncertainty step stores every draw's plan sums in {STORED_REL}, an input of figure_data")
        ukeys = set(yaml.safe_load(open(os.path.join(d, "analysis", "uncertainty.yaml")))["data"])
        ok(not ukeys & {"per_draw", "primary_averted", "plan_draws"},
           f"FD22 analysis/uncertainty.yaml is unchanged: the per-draw sums live in the sidecar ({sorted(ukeys)})")
        A, model = R["_A"], R["_model"]
        plan = {str(i): v for i, v in R["point"]["plan"].items()}
        z0 = {i: (R["calibration"].get("units") or {}).get(i, {}).get("z") for i in A.ids}
        n0 = model.runs
        out0, ben0, cost0 = A.evaluate(A.theta0, z0)
        per_eval = model.runs - n0
        n1 = model.runs
        fd = compute(A, plan, out0, ben0, cost0, uq, z0, log=q, stored=st)
        ok(model.runs - n1 == k * per_eval and fd["replay"]["reproduced"] is True,
           f"FD23 the figure step runs the engine for the {k} checked draws only ({model.runs - n1} runs, "
           f"{per_eval} per draw)")
        bad = copy.deepcopy(st)
        bad["primary_averted"][0] *= 1.01
        for e in bad["per_draw"][0].values():
            e["averted"] *= 1.01
        r1 = replay(A, plan, uq, stored=bad)
        ok(r1["reproduced"] is False and r1.get("reason"),
           f"FD24 a stored draw a fresh evaluation does not give is caught: not reproduced ({r1.get('reason')})")
        r2 = replay(A, plan, dict(uq, seed=int(uq["seed"]) + 1), stored=st)
        r3 = replay(A, {i: A.comp for i in A.ids}, uq, stored=st)
        ok(r2["reproduced"] is False and r3["reproduced"] is False,
           f"FD25 stored draws of another seed or another plan are not used ({r2.get('reason')}; {r3.get('reason')})")
        bad = copy.deepcopy(st)          # Codex r1 P3-10: the last total and its package sum moved together
        bad["primary_averted"][-1] += 1e6
        next(iter(bad["per_draw"][-1].values()))["averted"] += 1e6
        r4 = replay(A, plan, uq, stored=bad)
        bad = copy.deepcopy(st)
        e = next(iter(bad["per_draw"][-1].values()))
        e["averted"] += 1.0
        r5 = replay(A, plan, uq, stored=bad)
        ok(nd > k and r4["reproduced"] is False and "interval" in (r4.get("reason") or "")
           and r5["reproduced"] is False and "add up" in (r5.get("reason") or ""),
           f"FD26 an unchecked draw is still checked: its total against the stored interval, its package sums against "
           f"its total ({r4.get('reason')}; {r5.get('reason')})")
        # Codex r1 P1-1: an unchecked draw's package cost is checked against the draw's fixed-plan cost
        bad = copy.deepcopy(st)
        next(iter(bad["per_draw"][-1].values()))["cost"] *= 1000.0
        r6 = replay(A, plan, uq, stored=bad)
        ok(r6["reproduced"] is False and "cost" in (r6.get("reason") or ""),
           f"FD27 a stored package cost that is not the draw's recorded plan cost is caught ({r6.get('reason')})")
        # Codex r1 P2-3: a malformed row withholds the intervals with a reason; it does not abort the figures
        bad = copy.deepcopy(st)
        del next(iter(bad["per_draw"][-1].values()))["averted"]
        try:
            r7 = replay(A, plan, uq, stored=bad)
            ok(r7["reproduced"] is False and "malformed" in (r7.get("reason") or ""),
               f"FD28 a malformed stored draw is not reproduced, with the reason ({r7.get('reason')})")
        except Exception as e:  # noqa: BLE001 -- the failing case
            ok(False, f"FD28 a malformed stored draw is not reproduced, with the reason ({type(e).__name__}: {e})")
        # Codex r2 P1-2: a package's rows moved to another package (totals unchanged) is caught
        bad = copy.deepcopy(st)
        last = bad["per_draw"][-1]
        k1 = next(iter(last))
        k2 = next(k for k in A.pk if k not in last)
        last[k2] = last.pop(k1)
        r8 = replay(A, plan, uq, stored=bad)
        ok(r8["reproduced"] is False and "units" in (r8.get("reason") or ""),
           f"FD32 a draw whose package rows do not match the plan's packages and unit counts is caught "
           f"({r8.get('reason')})")
        # Codex r2 P1-3: a negative package cost (the draw's total cost unchanged) is malformed
        bad = copy.deepcopy(st)
        ks = list(bad["per_draw"][-1])
        r9 = None
        if len(ks) >= 2:
            e1, e2 = bad["per_draw"][-1][ks[0]], bad["per_draw"][-1][ks[1]]
            e2["cost"] += e1["cost"] + 3.0
            e1["cost"] = -3.0
            r9 = replay(A, plan, uq, stored=bad)
        ok(r9 is not None and r9["reproduced"] is False,
           f"FD33 a negative package cost is not reproduced ({len(ks)} packages; {(r9 or {}).get('reason')})")
        # Codex r2 P1-4: non-finite or unrepresentable numbers withhold the intervals; they never abort the figures
        outs = []
        for label, mut in (("an infinite total", lambda b: b["primary_averted"].__setitem__(-1, float("inf"))),
                           ("a cost of 10**400", lambda b: next(iter(b["per_draw"][-1].values())).__setitem__(
                               "cost", 10 ** 400)),
                           ("subtotals that overflow", lambda b: [e.__setitem__("averted", 1e308)
                                                                 for e in b["per_draw"][-1].values()])):
            bad = copy.deepcopy(st)
            mut(bad)
            try:
                r = replay(A, plan, uq, stored=bad)
                outs.append((label, r["reproduced"] is False))
            except Exception as e:  # noqa: BLE001 -- the failing case
                outs.append((label, f"{type(e).__name__}: {e}"))
        ok(all(v is True for _l, v in outs), f"FD34 non-finite or unrepresentable stored numbers are refused ({outs})")
        # Codex r3 P1-3: totals whose mean overflows give no interval statistics, never an infinite one
        import math as _m
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            got_, why_ = _interval([1.0] * 198 + [1e308] * 2, 0.95)
        ok(why_ is not None or all(_m.isfinite(v) for v in got_.values()),
           f"FD38 a replayed statistic is never infinite ({why_}; {got_})")
        # Codex r1 P3-9: checked_draws counts the evaluations actually run
        ok(r2.get("checked_draws") == 0, f"FD29 refused stored draws report no checked draw ({r2.get('checked_draws')})")
    # Codex r1 P2-4: package subtotals of benefits and harms that cancel still add up to the draw's total
    big, n = 1000000.0, 1000
    per = {"a": {"units": n, "cost": 1.0, "averted": 0.0}, "b": {"units": n, "cost": 1.0, "averted": 0.0}}
    tot = 0.0
    for _ in range(n):
        per["a"]["averted"] += big
        per["b"]["averted"] += -999999.9999999
        tot += big
        tot += -999999.9999999
    per["a"]["averted_abs"], per["b"]["averted_abs"] = abs(per["a"]["averted"]), abs(per["b"]["averted"])
    ok(_sums_match(per, tot), "FD30 package subtotals that cancel are compared at their own scale")
    # Codex r2 P1-1: the cancellation allowance is a summation round-off bound, not the replay tolerance of the
    # largest subtotal: three exact integers off by 900 are caught
    tri = {"a": {"units": 1, "cost": 0.0, "averted": 1e9, "averted_abs": 1e9},
           "b": {"units": 1, "cost": 0.0, "averted": -1e9, "averted_abs": 1e9},
           "c": {"units": 1, "cost": 0.0, "averted": 901.0, "averted_abs": 901.0}}
    ok(not _sums_match(tri, 1.0), "FD35 subtotals that cancel do not hide an error far above their round-off")
    # Codex r3 P1-1: cancellation inside a package; the bound uses the units' magnitudes (averted_abs), recorded
    # by _package_sums, not the already cancelled subtotals
    units = [("A", 1e16), ("B", -1e16), ("A", 1.0), ("A", 1.0), ("A", 2e6 - 1e16), ("B", 1e16 - 2e6)]
    tot_, sub = 0.0, {}
    for k, v in units:
        tot_ += v
        e = sub.setdefault(k, {"units": 0, "cost": 0.0, "averted": 0.0, "averted_abs": 0.0})
        e["units"] += 1
        e["averted"] += v
        e["averted_abs"] += abs(v)
    ok(_sums_match(sub, tot_), f"FD36 a valid draw whose unit contributions cancel inside a package matches ({tot_})")
    # Codex r3 P1-2: an exact sum of huge values does not overflow the bound
    huge = {"a": {"units": 1, "cost": 0.0, "averted": 1e308, "averted_abs": 1e308},
            "b": {"units": 1, "cost": 0.0, "averted": -1e308, "averted_abs": 1e308}}
    ok(_sums_match(huge, 0.0), "FD37 an exact sum of huge subtotals matches; computing the bound does not overflow")
    # Codex r1 P1-2: figure_data records the hash of the sidecar bytes it used, whatever the file holds afterwards
    import hashlib
    with tempfile.TemporaryDirectory() as d:
        RUN._direct_toy(d)
        signoff.sign(d, "lead")
        used, orig = {}, RUN.FD.compute

        def swapping(*a, **kw):
            used["sha"] = hashlib.sha256(json.dumps(kw.get("stored")).encode()).hexdigest()
            open(os.path.join(d, STORED_REL), "w").write("{}")      # replaced while the figure step reads
            return orig(*a, **kw)
        RUN.FD.compute = swapping
        try:
            RUN.run(d, log=q)
        finally:
            RUN.FD.compute = orig
        rec = (yaml.safe_load(open(os.path.join(d, "analysis", "figure_data.yaml"))).get("inputs") or {}).get(STORED_REL)
        ok(rec == used.get("sha"), "FD31 figure_data records the sha256 of the sidecar bytes the figure step used")


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    print(__doc__)
