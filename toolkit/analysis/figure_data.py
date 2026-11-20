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
                       replay must reproduce the stored fixed-plan interval, else the interval is withheld)
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


class RebuildRefused(RuntimeError):
    pass


def _fixed(spec) -> dict:
    return {k: float(v) for k, v in ((spec["constraints"].get("fixed_costs_usd") or {}).items())}


def frontier(A, ben0, cost0, out0, fractions=FRACTIONS, events=None) -> dict:
    """The plan re-solved at fractions of the envelope (fixed costs held). A plan that model corrections bear on is
    excluded with the reason (the analysis guard, applied to every frontier plan); an envelope the solver proves
    infeasible is recorded separately; any other solver failure propagates. The marginal is on outcome averted (the
    primary outcome, undiscounted); `objective` is the solver's (discounted) objective."""
    env = float(A.spec["constraints"]["envelope_usd"])
    fixed = sum(_fixed(A.spec).values())
    rows, skipped, infeasible, excluded = [], [], [], []
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
            sol = A.solve(ben0, cost0, budget=A.planned(b))      # F6 S1-1b: the same reserve fraction
        except RuntimeError as e:
            if str(e).strip() == "optimizer status infeasible":
                infeasible.append({"fraction": f, "envelope_usd": f * env, "reason": "the solver proves no "
                                   "allocation feasible at this envelope"})
                continue
            raise
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
            "infeasible": infeasible, "excluded": excluded, "marginal": marg, "discount_factor": A.dfac}


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
        e = s.setdefault(k, {"units": 0, "cost": 0.0, "averted": 0.0})
        e["units"] += 1
        e["cost"] += cost[i][k]
        e["averted"] += out[i]["y"][A.comp][A.primary] - out[i]["y"][k][A.primary]
    return s


def replay(A, plan, uq: dict) -> dict:
    """Per-package sums over the uncertainty step's draws (same Drawer, seed and call order); `reproduced` says
    whether the replay gives the stored fixed-plan interval of the primary outcome."""
    from toolkit.analysis import uncertainty as UQ
    from toolkit.analysis.core import q
    nd, seed = int(uq["draws"]), int(uq["seed"])
    D = UQ.Drawer(A, seed)
    D.prepare(nd)
    tot, per = [], []
    for _ in range(nd):
        D.last_clamped = []
        theta, zs, noise, mult, _s = D.draw()
        out, _ben, cost = A.evaluate(theta, zs, noise=noise, cost_mult=mult, sampled=_s)
        tot.append(sum(out[i]["y"][A.comp][A.primary] - out[i]["y"][plan[i]][A.primary] for i in A.ids))
        per.append(_package_sums(A, plan, out, cost))
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


def compute(A, plan, out0, ben0, cost0, uq: dict | None, z0: dict | None = None, log=print, events=None) -> dict:
    """`events`: the model corrections of the point evaluation (out0); every plan the figures show is guarded with
    them. With events=None the rebuild's own (A.point_events) are used."""
    ev = list(events if events is not None else (getattr(A, "point_events", None) or []))
    rep = None
    if uq and uq.get("draws"):
        rep = replay(A, plan, uq)
        if not rep["reproduced"]:
            log(f"figure_data: the draw replay did not reproduce the stored interval ({rep['replayed']} vs "
                f"{rep['stored']}); package intervals withheld")
    try:
        sch = schedule(A, plan, z0 or {})
    except Exception as e:  # noqa: BLE001 - a malformed schedule is recorded, not fatal for the other figures
        sch = {"status": "invalid", "reason": f"{type(e).__name__}: {e}"}
    out = {"primary": A.primary, "comparator": A.comp, "region": A.region,
           "frontier": frontier(A, ben0, cost0, out0, events=ev), "dollars": dollars(A, plan, out0),
           "by_package": by_package(A, plan, out0, cost0, rep),
           "replay": {k: rep[k] for k in ("draws", "seed", "level", "reproduced", "replayed", "stored")} if rep
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
    if diff:
        raise RebuildRefused(f"the rebuilt plan differs from the stored plan in {len(diff)} units")
    A.rebuild_info = {"rows": len(res), "by_ulp": by_ulp, "ulp_bound": ULP_BOUND,
                      "fields": sorted({x["field"] for x in by_ulp}), "plan_reproduced": True}
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
            raise RuntimeError("optimizer status time_limit")
        A.solve = broken
        try:
            frontier(A, ben0, cost0, out0)
            ok(False, "FD16b a solver failure other than infeasibility propagates")
        except RuntimeError:
            ok(True, "FD16b a solver failure other than infeasibility propagates")
        A.solve = orig
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
    print("OK: all self-test cases passed." if not fails else f"FAILED: {len(fails)} case(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    print(__doc__)
