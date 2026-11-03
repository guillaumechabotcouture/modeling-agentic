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


def frontier(A, ben0, cost0, out0, fractions=FRACTIONS) -> dict:
    env = float(A.spec["constraints"]["envelope_usd"])
    fixed = sum(_fixed(A.spec).values())
    rows, skipped = [], []
    for f in fractions:
        b = f * env - fixed
        if b <= 0:
            skipped.append({"fraction": f, "reason": "the fixed costs exceed this envelope"})
            continue
        sol = A.solve(ben0, cost0, budget=b)
        s = A.summary(sol["choice"], out0, cost0)
        rows.append({"fraction": f, "envelope_usd": f * env, "budget_allocatable": b, "objective": sol["objective"],
                     "cost": s["cost_allocated"], "averted": s["averted"], "packages": s["packages"], "mix": s["mix"]})
    by = {r["fraction"]: r for r in rows}
    marg = None
    if 0.9 in by and 1.1 in by:
        d_obj = by[1.1]["objective"] - by[0.9]["objective"]
        d_usd = by[1.1]["cost"] - by[0.9]["cost"]
        marg = {"from_fraction": 0.9, "to_fraction": 1.1, "outcome_per_usd": d_obj / d_usd if d_usd > 0 else None,
                "usd_per_outcome": d_usd / d_obj if d_obj > 0 else None}
    return {"envelope_usd": env, "fixed_costs_usd": fixed, "points": rows, "skipped": skipped, "marginal": marg}


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
        out, _ben, cost = A.evaluate(theta, zs, noise=noise, cost_mult=mult)
        tot.append(sum(out[i]["y"][A.comp][A.primary] - out[i]["y"][plan[i]][A.primary] for i in A.ids))
        per.append(_package_sums(A, plan, out, cost))
    lvl = float((uq.get("interval") or {}).get("level", 0.95))
    got, want = q(tot, lvl), (uq.get("fixed_plan") or {}).get(A.primary) or {}
    same = bool(want) and all(abs(got[k] - float(want[k])) <= REPLAY_RTOL * max(abs(float(want[k])), 1.0)
                              for k in ("median", "ci_low", "ci_high"))
    return {"draws": nd, "seed": seed, "level": lvl, "reproduced": same, "replayed": got, "stored": want,
            "per_draw": per}


def by_package(A, plan, out0, cost0, rep: dict | None) -> dict:
    from toolkit.analysis.core import q
    pt = _package_sums(A, plan, out0, cost0)
    out = {}
    for k, e in pt.items():
        row = dict(e, components=sorted(A.pk[k]),
                   usd_per_outcome=e["cost"] / e["averted"] if e["averted"] > 0 else None)
        if rep and rep["reproduced"]:
            ratios = [d[k]["cost"] / d[k]["averted"] for d in rep["per_draw"] if k in d and d[k]["averted"] > 0]
            row["usd_per_outcome_interval"] = q(ratios, rep["level"]) if len(ratios) == rep["draws"] else None
        out[k] = row
    return out


def component_increment(A, plan, ben0, cost0) -> dict:
    by_set = {cs: k for k, cs in A.pk.items()}
    res = {}
    for c in A.spec["options"]["components"]:
        r = {"units": 0, "units_without_counterpart": 0, "averted": 0.0, "cost": 0.0}
        for i in A.ids:
            cs = A.pk[plan[i]]
            if c not in cs:
                continue
            alt = by_set.get(frozenset(set(cs) - {c}))
            if alt is None:
                r["units_without_counterpart"] += 1
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
        return {"status": "not_offered", "reason": str(hooks.HookUnsupported("schedule", eng))}
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


def compute(A, plan, out0, ben0, cost0, uq: dict | None, z0: dict | None = None, log=print) -> dict:
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
    return {"primary": A.primary, "comparator": A.comp, "region": A.region,
            "frontier": frontier(A, ben0, cost0, out0), "dollars": dollars(A, plan, out0),
            "by_package": by_package(A, plan, out0, cost0, rep),
            "replay": {k: rep[k] for k in ("draws", "seed", "level", "reproduced", "replayed", "stored")} if rep
            else {"reproduced": False, "reason": "no uncertainty draws"},
            "component_increment": component_increment(A, plan, ben0, cost0), "burden": burden(A, out0),
            "calibration": calibration_modelled(A, z0 or {}), "schedule": sch}


# --------------------------------------------------------------------------- offline rebuild
def _canon(x):
    return json.loads(json.dumps(x, sort_keys=True, default=str))


def rebuild(run_dir: str, log=print):
    """(A, plan, out0, ben0, cost0, z0) of a finished emulator-path run, from its stored batch (no engine runs)."""
    import yaml
    from toolkit.analysis import batch as Bt
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
    with open(rows) as f:
        first = json.loads(f.readline())
    stored_ident = ((first.get("result") or {}).get("provenance") or {}).get("engine")
    if stored_ident != engine_identity(eng):
        raise RebuildRefused(f"the batch was computed by engine {stored_ident and stored_ident.get('name')} "
                             f"v{stored_ident and stored_ident.get('version')} (code "
                             f"{(stored_ident or {}).get('code_sha256', '')[:12]}); this code's engine is "
                             f"v{engine_identity(eng)['version']} (code {engine_identity(eng)['code_sha256'][:12]}): "
                             "re-render with the run's own code")
    design, jobs = Bt.design(spec)
    stored = json.load(open(os.path.join(run_dir, "analysis", "batch", "design.json")))
    if _canon(stored) != _canon(design):
        raise RebuildRefused("the stored batch design differs from the spec's design (a shipped batch?)")
    res, probs = Bt.load_results(rows, jobs, eng)
    accepted_by_index = []
    if probs and all(p.endswith("inputs differ from the current job") for p in probs):
        # The run's own analysis verified every row on the computing platform (batch.yaml rejected_rows 0, manifest
        # complete); a job rebuilt here can differ from it in the last bit of a derived float (libm differs across
        # platforms, as batch.adopt notes), which changes its hash. Such rows are taken by their index, and the
        # rebuilt plan must still equal the stored plan below.
        man = json.load(open(os.path.join(run_dir, "analysis", "manifest.json")))
        bsum = (yaml.safe_load(open(os.path.join(run_dir, "analysis", "batch.yaml")))["data"].get("summary") or {})
        if man.get("status") != "complete" or bsum.get("rejected_rows") != 0:
            raise RebuildRefused(f"{len(probs)} batch rows lack matching provenance and the run's analysis did not "
                                 "verify them")
        with open(rows) as f:
            for line in f:
                r = json.loads(line)
                jid = jobs[r["i"]]["id"]
                if jid not in res and r.get("ok"):
                    res[jid] = r["result"]["result"]
                    accepted_by_index.append(jid)
        probs = []
    if probs or len(res) != len(jobs):
        raise RebuildRefused(f"{len(probs)} batch rows lack matching provenance, {len(jobs) - len(res)} jobs missing")
    if accepted_by_index:
        log(f"rebuild: {len(accepted_by_index)} rows taken by index (job hash differs on this platform), e.g. "
            f"{accepted_by_index[:2]}")
    model = EmulatorModel(spec, design, res, log=log).fit()
    A = Analysis(spec, model, log=log)
    A.fit_calibration()
    cal0 = A.calibrate(A.theta0)
    z0 = {i: v[0] for i, v in cal0.items()}
    out0, ben0, cost0 = A.evaluate(A.theta0, z0)
    sol = A.solve(ben0, cost0)
    point = yaml.safe_load(open(os.path.join(run_dir, "analysis", "point.yaml")))["data"]
    plan = {str(k): v for k, v in point["plan"].items()}
    diff = [i for i in A.ids if sol["choice"][i] != plan.get(i)]
    if diff:
        raise RebuildRefused(f"the rebuilt plan differs from the stored plan in {len(diff)} units")
    return A, plan, out0, ben0, cost0, z0


# --------------------------------------------------------------------------- self-test
def _self_test() -> int:
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
        A2, plan2, *_ = rebuild(d, log=q)
        info = getattr(A2, "rebuild_info", {}) or {}
        ok(plan2 == plan and [(x["job"], x["field"], x["ulps"]) for x in info.get("by_ulp", [])]
           == [(jobs[5]["id"], f"params.{leaf}", 1)],
           f"FD14 a row whose job differs from the rebuilt job by 1 ulp in one input is taken, and recorded: {info}")
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
        unit_c = next(i for i in A.ids if len(A.pk[plan[i]]) == 2)
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
