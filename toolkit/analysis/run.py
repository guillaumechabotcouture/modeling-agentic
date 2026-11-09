#!/usr/bin/env python3
"""The analysis engine (architecture-v2 §3.4): every number from the spec, the engine and the run's data.

    python3 toolkit/analysis/run.py <run_dir> [--spec decision_spec.yaml] [--results <batch results.jsonl>]
                                    [--shipped <shipped batch dir>] [--draws N] [--unsigned]
    python3 toolkit/analysis/run.py --self-test         # every step on the toy engine, both paths

Steps (design: docs/design/v2-spec-and-analysis.md §2): validate the spec and check it is
signed; the engine card; the engine batch and the emulator (emulator path) or the engine itself
(direct path); calibration with diagnostics and identifiability; point outcomes and the
toolkit/alloc optimum; uncertainty over the declared sources; leave-one-group-out stability;
one-at-a-time sensitivity; decision tests (Phase 49 result files); figures; the claims ledger
and results.md; the deliverable CSV. Each writes a typed artifact under <run_dir>/analysis/
(toolkit/analysis/artifacts.py), recorded in analysis/manifest.json. A missing input, an
unsigned spec or a solver that does not prove optimality stops the run (fail closed).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from toolkit.analysis import (batch as Bt, calibration as C, claims as CL, decision_tests as DT,  # noqa: E402
                              figure_data as FD,
                              figures as FG, sensitivity as SE, stability as ST, uncertainty as UQ)
from toolkit.analysis.artifacts import EXPECTED_KINDS, EXPECTED_ROLES, Store, plain, sha256  # noqa: E402
from toolkit.analysis.core import Analysis, AnalysisBlocked  # noqa: E402
from toolkit.analysis.model import DirectModel, EmulatorModel  # noqa: E402
from toolkit.spec import schema, signoff  # noqa: E402

PRODUCER = "toolkit/analysis/run.py"


def _engine_info(eng) -> dict:
    card = eng.describe()
    blob = json.dumps(card.to_dict(), sort_keys=True, default=str).encode()
    import hashlib
    return {"name": card.name, "version": card.version, "card_sha256": hashlib.sha256(blob).hexdigest()}


def interactions(A, out) -> list[dict]:
    """For each pair of components with packages {a}, {b} and {a, b}: outcome ratios to the comparator over all
    units, and the interaction ratio r_ab / (r_a r_b)."""
    comps = list(A.spec["options"]["components"])
    by_set = {cs: k for k, cs in A.pk.items()}
    tot = lambda k: sum(out[i]["y"][k][A.primary] for i in A.ids)  # noqa: E731
    base = tot(A.comp)
    rows = []
    for x in range(len(comps)):
        for y in range(x + 1, len(comps)):
            a, b = comps[x], comps[y]
            ka, kb, kab = (by_set.get(frozenset(s)) for s in ({a}, {b}, {a, b}))
            if None in (ka, kb, kab) or base <= 0:
                continue
            ra, rb, rab = tot(ka) / base, tot(kb) / base, tot(kab) / base
            rows.append({"a": a, "b": b, "ratio_a": ra, "ratio_b": rb, "ratio_ab": rab,
                         "interaction_ratio": rab / (ra * rb) if ra * rb > 0 else None})
    return rows


def decision_rule(A, out, plan) -> list[dict]:
    """Units by package, with the range of the comparator outcome per person among them."""
    pops = A.spec["decision_units"]["populations"]
    burden = {}
    for i in A.ids:
        n = sum(float(A.by_id[i][pm["column"]]) for pm in pops.values())
        burden[i] = out[i]["y"][A.comp][A.primary] / n if n > 0 else 0.0
    rows = {}
    for i in A.ids:
        rows.setdefault(plan[i], []).append(i)
    return [{"package": k, "n_units": len(v), "units": sorted(v, key=lambda i: -burden[i]),
             "burden_min": min(burden[i] for i in v), "burden_max": max(burden[i] for i in v)}
            for k, v in sorted(rows.items(), key=lambda kv: -max(burden[i] for i in kv[1]))]




def affordable_plan(A, ben0, cost0, sol0, draws=None, seed=None, floor=None) -> dict:
    """F6 S1-1b: with `uncertainty.affordability.enforce`, the plan solved within a budget reserve. The uncertainty
    step's draws are evaluated once (same Drawer, seed and number of draws), and toolkit/analysis/uncertainty.py
    reserve_search bisects on the budget fraction for the largest one whose plan exceeds the allocatable budget in at
    most max_share_over_budget of those draws. No fraction at or above the floor: AnalysisBlocked (fail closed).
    Returns {solution, reserve (the point artifact's record), pre (the evaluated draws, for UQ.run)}."""
    rule = A.spec["uncertainty"]["affordability"]
    pre = UQ.evaluate_draws(A, draws, seed)
    fl = UQ.RESERVE_FLOOR if floor is None else float(floor)
    rs = UQ.reserve_search([r["cost"] for r in pre["rows"]], lambda b: A.solve(ben0, cost0, budget=b), A.budget,
                           float(rule["max_share_over_budget"]), floor=fl)
    st = rs["steps"]
    if not rs["feasible"]:
        raise AnalysisBlocked("affordability", [dict(s, kind="reserve_step") for s in st], reason=(
            f"no plan solved at or above the floor of {fl:g} x the allocatable budget meets the affordability rule "
            f"(at most {rule['max_share_over_budget']} of {pre['draws']} draws over budget): the plan at the floor is "
            f"over in {st[-1]['draws_over_budget']} draws"))
    u0, f = st[0], rs["fraction"]
    obj_u, obj_a = float(sol0["objective"]), float(rs["solution"]["objective"])
    sol = rs["solution"] if f < 1.0 else sol0
    plan_cost = lambda s: float(sum(cost0[i][s["choice"][i]] for i in A.ids))  # noqa: E731
    given = obj_u - obj_a if f < 1.0 else 0.0
    reserve = {"rule": dict(rule), "outcome": A.primary, "fraction": f, "budget_allocatable": float(A.budget),
               "budget_planned": f * float(A.budget), "reserve_usd": (1.0 - f) * float(A.budget),
               "draws": pre["draws"], "seed": pre["seed"],
               "unconstrained": {"objective": obj_u, "cost": plan_cost(sol0), "plan": dict(sol0["choice"]),
                                 "draws_over_budget": u0["draws_over_budget"], "share_over_budget": u0["share_over_budget"]},
               "affordable": {"objective": float(sol["objective"]), "cost": plan_cost(sol),
                              "draws_over_budget": rs["draws_over_budget"], "share_over_budget": rs["share_over_budget"]},
               "value_given_up": given, "value_given_up_pct": 100.0 * given / obj_u if obj_u > 0 else None,
               "search": {k: rs[k] for k in ("tolerance", "floor", "max_solves", "solves", "converged", "steps")}}
    return {"solution": sol, "reserve": reserve, "pre": pre}


def run(run_dir: str, spec_rel: str = "decision_spec.yaml", adopt: dict | None = None, draws=None,
        unsigned: bool = False, log=print, compute_kw: dict | None = None) -> dict:
    """The analysis. A step whose model corrections bear on a plan stops it: analysis/blocked.yaml records where and
    which, and the manifest is published with status blocked (never complete)."""
    holder = {}
    try:
        return _run(run_dir, spec_rel, adopt, draws, unsigned, log, compute_kw, holder)
    except AnalysisBlocked as e:
        S = holder.get("S")
        if S is not None:
            why = e.reason or f"model corrections bear on a plan at: {e.where}"
            S.write("blocked", {"reason": why, "where": e.where,
                                "corrections": e.hits[:200], "n_on_plan": len(e.hits)}, PRODUCER,
                    [holder["calibration"]] if holder.get("calibration") else [])
            S.finalize("blocked", f"{e.where}: {e.reason}" if e.reason else
                       f"{e.where}: {len(e.hits)} model corrections bear on the plan")
        raise


def _run(run_dir, spec_rel, adopt, draws, unsigned, log, compute_kw, holder) -> dict:
    t0 = time.time()
    run_dir = os.path.abspath(run_dir)
    spec_path = os.path.join(run_dir, spec_rel)
    spec = schema.load(spec_path)
    errs = schema.validate(spec)
    if errs:
        raise ValueError("decision spec invalid:\n  " + "\n  ".join(errs))
    st = signoff.status(run_dir) if spec_rel == signoff.SPEC else "not the run's decision_spec.yaml"
    if st != "signed" and not unsigned:
        raise PermissionError(f"decision spec is not signed (status: {st}); sign it (toolkit/spec/signoff.py) or pass "
                              "--unsigned for a test run")
    eng = spec.engine()
    S = Store(run_dir, spec.sha256, st == "signed", _engine_info(eng), spec.settings_sha256,
              spec.engine_choice_sha256)
    holder["S"] = S
    card = eng.describe()
    arts = {}
    # Codex r2 P1-7: the files the engine reads (benchmark results, fitted values) are inputs of what uses them
    from toolkit.spec import priors as PRI
    eng_files = [f for f in (getattr(eng, "results_path", None), getattr(eng, "fitted_path", None),
                             PRI.priors_path(eng)) if f and os.path.exists(f)]
    from toolkit.spec import engine_parameters as PV
    eng_data = {"card": card.to_dict(), "parameters": [vars(p) for p in eng.parameters()]}
    svr = PV.rows(spec, eng)            # spec-declared engine parameter values, with the card values they replace
    if svr:
        eng_data["spec_values"] = {r["name"]: {k: v for k, v in r.items() if k != "name"} for r in svr}
    arts["engine"] = S.write("engine", eng_data, PRODUCER, [spec_path] + eng_files)
    data_in = [spec.resolve(spec["decision_units"]["csv"])]
    if spec["calibration"].get("method") in ("likelihood", "posterior"):
        data_in.append(spec.resolve(spec["calibration"]["targets"]["csv"]))
    from toolkit.spec import estimates as ES_SPEC
    if ES_SPEC.is_estimates(spec.raw):     # no allocation units: quantities with intervals (v2d §2.3)
        from toolkit.analysis import estimates as ES
        data_in.append(spec.resolve(spec["dynamics"]["generation_interval"]["csv"]))    # Codex r1-9
        return ES.run(run_dir, spec, S, eng, arts, data_in, eng_files, draws=draws, log=log)
    # ---- joint calibration (before the outcome batch: units no compatible value matches stop the run here)
    jc = None
    if spec["calibration"].get("joint") is not None:
        from toolkit.analysis import joint_calibration as JC
        jkw = dict(compute_kw or {})
        if spec["analysis"].get("workers"):
            jkw.setdefault("max_workers", int(spec["analysis"]["workers"]))
        jc = JC.run(spec, run_dir, jkw, log)
        # drawn here so a blocked run has them; listed in the joint artifact. A complete run's figure step redraws
        # them from the artifact into figures/ with provenance sidecars and lists them in analysis/figures.yaml
        jc["figures"] = JC.render_figures(jc, run_dir)
        arts["joint_calibration"] = S.write("joint_calibration", plain(JC.artifact(jc)), JC.PRODUCER,
                                            [spec_path] + data_in + JC.inputs(spec) + eng_files)
        holder["calibration"] = arts["joint_calibration"]
        log(JC.summary(jc))
        if jc["blocked"] and not jc["unmatched_decision"]["proceed"]:
            raise AnalysisBlocked("calibration", JC.block_hits(jc), reason=JC.block_reason(jc))
        if jc["unmatched_decision"]["flagged"]:      # owner option calibration.joint.unmatched: flag (never silent)
            log(f"joint calibration: {len(jc['unmatched_decision']['flagged'])} units proceed flagged low-confidence")
    # ---- outcome model
    if spec["analysis"]["evaluation"] == "emulator":
        design, jobs = Bt.design(spec)
        from toolkit.analysis.artifacts import _safe_io
        _safe_io().write_nofollow(run_dir, "analysis/batch/design.json", json.dumps(design, indent=1).encode(),
                                  atomic=True)
        kw = dict(compute_kw or {})
        if spec["analysis"].get("workers"):
            kw.setdefault("max_workers", int(spec["analysis"]["workers"]))
        shipped = (adopt or {}).get("shipped")
        adopt_in = []
        if shipped:
            # a batch computed elsewhere (VM shards) with its exact inputs: rows are checked against the shipped
            # jobs, and the analysis uses the shipped design (batch.load_shipped)
            from toolkit.engines.contract import engine_identity
            design, jobs, res, info = Bt.load_shipped(shipped, spec, design, jobs, eng)
            _safe_io().write_nofollow(run_dir, "analysis/batch/design.json", json.dumps(design, indent=1).encode(),
                                      atomic=True)
            _safe_io().write_nofollow(run_dir, "analysis/batch/results.jsonl", "".join(
                json.dumps({"i": n, "ok": True, "result": {"result": res[j["id"]], "provenance": {
                    "job_sha": Bt.job_sha(j), "engine": engine_identity(eng), "producer": Bt.PRODUCER,
                    "shipped_from": info}}}) + "\n" for n, j in enumerate(jobs)).encode(), atomic=True)
            res, probs = Bt.load_results(os.path.join(run_dir, "analysis", "batch", "results.jsonl"), jobs, eng)
            bsum = {"source": "shipped batch", "shipped": info, "n": len(res), "rejected_rows": len(probs)}
            adopt_in = [os.path.join(os.path.abspath(shipped), n) for n in
                        [Bt.SHIP_MANIFEST] + list(Bt.SHIP_FILES.values())]
        elif adopt:
            adopt_in = [adopt["results"], adopt["jobs"], adopt["design"]]
            # a batch computed before results carried provenance: adopted only after a full-input match and a re-run
            # sample that reproduces it (Codex P1-3)
            ad = Bt.adopt(run_dir, adopt["results"], adopt["jobs"], adopt["design"], jobs, design, eng,
                          sample=int(adopt.get("sample") or Bt.min_adopt_sample(len(jobs))),
                          workers=int(kw.get("max_workers", 2)))
            res, probs = Bt.load_results(ad["path"], jobs, eng)
            bsum = {"source": "adopted stored batch", "adoption": ad, "n": len(res), "rejected_rows": len(probs)}
        else:
            b = Bt.run(run_dir, jobs, eng, **kw)
            res, probs = b["results"], b["problems"]
            bsum = dict(b["summary"], path=os.path.relpath(b["path"], run_dir), rejected_rows=len(probs))
        batch_rows = os.path.join(run_dir, "analysis", "batch", "results.jsonl")
        if probs:
            raise ValueError(f"{len(probs)} batch results lack matching provenance (e.g. {probs[:3]}): the analysis fails "
                             "closed")
        arts["batch"] = S.write("batch", {"evaluation": "emulator", "points": len(design["points"]), "jobs": len(jobs),
                                          "packages": design["packages"], "fitted_names": design["fitted_names"],
                                          "prior_names": design.get("prior_names", []),
                                          "summary": bsum}, Bt.PRODUCER,
                                [os.path.join(run_dir, "analysis", "batch", "design.json"), batch_rows] + data_in
                                + adopt_in)
        model = EmulatorModel(spec, design, res, log=log).fit()
        arts["emulator"] = S.write("emulator", {"accuracy": model.accuracy, "fit_seconds": model.fit_seconds,
                                                "features": model.features, "training_corrections":
                                                    model.train_corrections,
                                                "domain": {"inputs": model.input_names, "lo": model.dom_lo,
                                                           "hi": model.dom_hi}}, "toolkit/analysis/model.py",
                                   [os.path.join(run_dir, "analysis", "batch", "design.json"), batch_rows])
    else:
        model = DirectModel(spec, log=log)
        arts["batch"] = S.write("batch", {"evaluation": "direct", "note": "the engine runs per unit, package and draw"},
                                Bt.PRODUCER, data_in)
    A = Analysis(spec, model, log=log)
    if jc is not None:
        A.set_joint(jc)
        A.flagged = {f["unit"]: f for f in jc["unmatched_decision"]["flagged"]}
    # ---- calibration
    A.fit_calibration()
    cal0 = A.calibrate(A.theta0)
    z0 = {i: v[0] for i, v in cal0.items()}
    if A.method == "emulator_inversion":
        cal = C.inversion_report(spec, model, A.theta0, cal0)
        if jc is not None:
            cal["joint"] = {"artifact": os.path.relpath(arts["joint_calibration"], run_dir), "shared": A.joint["shared"],
                            "best": A.joint["best"], "ranges": A.joint["ranges"],
                            "sample_id": A.joint["sample"]["sample_id"]}
    elif A.method == "likelihood":
        cal = {k: A.lik[k] for k in ("method", "diagnostics", "identifiability", "theta", "names", "cov")}
        cal["units"] = {i: {"z": z0[i]} for i in A.ids}
    elif A.method == "posterior":
        cal = {k: A.lik[k] for k in ("method", "diagnostics", "identifiability", "theta", "names", "sample", "priors",
                                     "prior_range_tests", "prior_dominated")}
        cal["units"] = {i: {"z": z0[i]} for i in A.ids}
    else:
        cal = {"method": "none", "units": {}, "diagnostics": {}, "identifiability": {"status": "not_applicable"}}
    arts["calibration"] = S.write("calibration", cal, C.PRODUCER,
                                  data_in + ([arts["joint_calibration"]] if jc is not None else []))
    holder["calibration"] = arts["calibration"]
    if A.method == "emulator_inversion":
        # F5 (2026-11-05): units whose target lies outside what the model reaches at the point values are clamped at
        # a bound of calibration.range; they stop the run here, before the point plan and the uncertainty draws
        # (thresholds: toolkit/calibration/thresholds.yaml blocking.clamped_units_max)
        from toolkit.analysis.core import clamped_hits, clamped_units_max
        hits = clamped_hits(cal)
        if len(hits) > clamped_units_max():
            raise AnalysisBlocked("calibration", hits, reason=f"{len(hits)} units' calibration targets lie outside "
                                  "what the model reaches inside calibration.range (clamped at a bound)")
    # ---- point and optimum
    out0, ben0, cost0 = A.evaluate(A.theta0, z0)
    events0 = list(A.last_events)
    sol = A.solve(ben0, cost0)
    reserve = pre = None
    if (spec["uncertainty"].get("affordability") or {}).get("enforce"):
        # F6 S1-1b: the headline plan is the one solved within the reserve that meets the affordability rule
        ap = affordable_plan(A, ben0, cost0, sol, draws=draws)
        sol, reserve, pre = ap["solution"], ap["reserve"], ap["pre"]
        A.plan_budget = reserve["budget_planned"]
        log(f"affordability: plan within {reserve['fraction']:.4f} of the budget ({reserve['reserve_usd']:,.0f} held "
            f"back), {reserve['value_given_up_pct'] or 0:.2f}% of the objective given up")
    plan = sol["choice"]
    A.guard("point plan", [plan] + ([reserve["unconstrained"]["plan"]] if reserve else []), events0)  # Codex P1-6
    on_plan = []
    point = A.summary(plan, out0, cost0)
    from toolkit.engines.contract import engine_identity
    # Codex r3: the analysis's full engine identity (code, params, config), on both evaluation paths, so later steps
    # (the trajectory step) can be checked against it without batch rows
    point.update(engine_identity=engine_identity(eng))
    point.update(plan=plan, n_units=len(A.ids), solver={k: sol[k] for k in ("status", "method", "objective", "cost")},
                 interactions=interactions(A, out0), rule=decision_rule(A, out0, plan),
                 model_corrections={"n": len(events0), "on_plan": on_plan, "kinds": sorted({e["kind"] for e in events0}),
                                    "examples": events0[:20]},
                 diagnostics={n: {"max": max(out0[i]["diag"][n] for i in A.ids),
                                  "median": sorted(out0[i]["diag"][n] for i in A.ids)[len(A.ids) // 2]}
                              for n in (spec["analysis"].get("diagnostics") or {})},
                 per_unit={i: {"package": plan[i], "benefit": ben0[i][plan[i]], "cost": cost0[i][plan[i]],
                               "averted": {o: out0[i]["y"][A.comp][o] - out0[i]["y"][plan[i]][o] for o in A.outcomes},
                               "z": z0[i], **({"calibration_flag": A.flagged[i]["reason"],
                                               "calibration_status": A.flagged[i]["status"]}
                                              if i in getattr(A, "flagged", {}) else {})} for i in A.ids})
    if reserve is not None:
        point["reserve"] = reserve
    arts["point"] = S.write("point", point, "toolkit/analysis/core.py", [arts["calibration"]])
    log(f"point: {point['averted'][A.primary]:,.0f} {A.primary} averted, cost {point['cost_allocated']:,.0f}, "
        f"mix {point['mix']}")
    # ---- uncertainty
    u = UQ.run(A, plan, spec["deliverable"]["headline"]["name"], draws=draws, pre=pre)
    arts["uncertainty"] = S.write("uncertainty", u["data"], UQ.PRODUCER, [arts["point"]] + eng_files)
    S.deliverable(UQ.write_report(run_dir, u["report"]), UQ.PRODUCER, "uncertainty_report")
    log(f"UQ: {u['data']['fixed_plan'][A.primary]} ({u['data']['label']})")
    # ---- stability
    stab = ST.run(A, z0, out0, ben0, cost0, plan)
    arts["stability"] = S.write("stability", stab, ST.PRODUCER, [arts["point"], arts["calibration"]])
    if stab.get("folds"):
        import yaml
        rp = os.path.join(run_dir, "models", "allocation_robustness.yaml")
        from toolkit.analysis.artifacts import _safe_io
        _safe_io().write_nofollow(run_dir, "models/allocation_robustness.yaml",
                                  yaml.safe_dump(plain(ST.robustness_yaml(stab)), sort_keys=False).encode(), atomic=True)
        S.deliverable(rp, ST.PRODUCER, "allocation_robustness")
    # ---- sensitivity
    sens = SE.run(A, z0, ben0, cost0, plan, sol["objective"])
    arts["sensitivity"] = S.write("sensitivity", sens, SE.PRODUCER, [arts["point"]])
    # ---- decision tests
    dts = DT.run_all(A, z0, out0, ben0, cost0, plan, u["data"], run_dir)
    arts["decision_tests"] = S.write("decision_tests", {"tests": [
        {"test_id": x["test_id"], "path": x["path"], "status": x["status"],
         "verdict": ("NOT_RUN" if x["status"] == "not_run" else "CHANGES_PLAN" if x["decision_changes"] else "NO_CHANGE"),
         "can_move_plan": x["can_move_plan"], "threshold_pct": 100.0 * x["share_threshold"],
         "share_pct": None if x["share_units_changed"] is None else 100.0 * x["share_units_changed"],
         "statement": x["statement"], "detail": {k: x[k] for k in ("engine_arm_or", "engine_interaction",
                                                    "rejected_values", "reachable_interaction") if k in x}} for x in dts]}, DT.PRODUCER,
        [arts["point"], arts["uncertainty"]] + eng_files
        + sorted({os.path.join(run_dir, f) for x in dts for f in (x.get("arm_inputs") or {})}))
    for x in dts:
        if x["path"]:
            S.deliverable(os.path.join(run_dir, x["path"]), DT.PRODUCER, "decision_test")
    # ---- deliverable CSV
    d = spec["deliverable"]
    cols = d["columns"]
    reg = spec["decision_units"].get("region")
    dp = os.path.join(run_dir, d["allocation_file"])
    import io as _io
    buf = _io.StringIO(newline="")
    w = csv.writer(buf)
    hl_o = d["headline"]["outcome"]
    bg = d.get("by_group")
    if bg:
        # a joint package within a unit (e.g. dose blocks over groups): one row per (unit, engine group) with the
        # plan's quantity in that group (a cost output) and the outcome averted in it
        gc = bg["columns"]
        w.writerow([gc["group"], gc["quantity"], gc["value"], cols["unit"], cols["package"]])
        for i in A.ids:
            yg = out0[i]["y_group"]
            for g, qname in bg["quantity"].items():
                w.writerow([g, round(float(out0[i]["cost"][plan[i]][qname]), 4),
                            round(yg[A.comp][hl_o].get(g, 0.0) - yg[plan[i]][hl_o].get(g, 0.0), 4), i, plan[i]])
    else:
        fl = getattr(A, "flagged", {}) or {}
        w.writerow([cols["unit"], cols["package"], cols["value"], "cost_usd", "region"]
                   + (["calibration_flag"] if fl else []))
        for i in A.ids:
            w.writerow([i, plan[i], round(point["per_unit"][i]["averted"][hl_o], 4), round(cost0[i][plan[i]], 2),
                        A.by_id[i].get(reg, "") if reg else ""] + ([fl[i]["reason"] if i in fl else ""] if fl else []))
    from toolkit.analysis.artifacts import _safe_io
    _safe_io().publish(dp, buf.getvalue(), run_dir)
    S.deliverable(dp, PRODUCER, "allocation")
    # ---- figures: the decision numbers they need (no engine runs), then every figure from the saved artifacts
    fdat = FD.compute(A, plan, out0, ben0, cost0, u["data"], z0, log=log, events=events0)
    arts["figure_data"] = S.write("figure_data", fdat, FD.PRODUCER, [arts["point"], arts["uncertainty"]])
    rend = FG.render(run_dir)
    figs = rend["figures"]
    arts["figures"] = S.write("figures", {"figures": figs, "not_produced": rend["not_produced"],
                                          "tiers": rend["tiers"], "groups": rend["groups"],
                                          "overviews": rend.get("overviews") or {}}, FG.PRODUCER,
                              sorted({s for f in figs for s in f["sources"]}))
    for fgd in figs:
        S.deliverable(os.path.join(run_dir, fgd["path"]), FG.PRODUCER, "figure")
    # ---- claims and results.md
    c = CL.write(A, run_dir, arts, point, u["data"], stab, sens, dts, cal, model.accuracy)
    arts["claims"] = S.write("claims", c, CL.PRODUCER, [arts[k] for k in ("point", "uncertainty", "stability",
                                                                          "decision_tests", "calibration")])
    for rel, role in ((c["spec"], "claims_spec"), (c["ledger"], "claims_ledger"), (c["results"], "results")):
        S.deliverable(os.path.join(run_dir, rel), CL.PRODUCER, role)
    S.manifest["wall_s"] = round(time.time() - t0, 1)
    S.manifest["expected"] = {"kinds": list(EXPECTED_KINDS) + (["emulator"] if model.kind == "emulator" else []),
                              "roles": list(EXPECTED_ROLES),
                              "decision_tests": {x["test_id"]: x["path"] for x in dts}}
    S.finalize("complete")
    return {"artifacts": arts, "point": point, "uncertainty": u["data"], "stability": stab, "sensitivity": sens,
            "decision_tests": dts, "calibration": cal, "emulator_accuracy": model.accuracy, "wall_s": S.manifest["wall_s"],
            "_A": A, "_model": model}


# --------------------------------------------------------------------------- self-test
def _toy_pack() -> dict:
    """The toy engine's benchmark pack: T2 is its interaction benchmark."""
    return {"engine": "toolkit.engines.contract:ToyEngine", "seeds": [1],
            "horizon": {"start": 0, "stop": 3, "window": [0, 3], "windows": {"w": [1, 2]}},
            "settings": {"s": {"unit": {"x": 0.0}}},
            "arms": {"none": {"setting": "s", "package": {}}, "a": {"setting": "s", "package": {"a": 1}},
                     "b": {"setting": "s", "package": {"b": 1}}, "ab": {"setting": "s", "package": {"a": 1, "b": 1}}},
            "benchmarks": [{"id": "T2", "metric": "interaction", "target": 1.0, "band": [0.8, 1.25],
                            "estimand": {"type": "interaction_or", "a11": "ab", "a10": "a", "a01": "b", "a00": "none",
                                         "value": "windows.w.prev"}}]}


def _toy_bench(d):
    """Stamped benchmark results for the toy engine (toolkit/engines/benchmark.py): T2 is its interaction benchmark."""
    import yaml
    from toolkit.engines import benchmark as BM
    from toolkit.engines.contract import ToyEngine
    path = os.path.join(d, "toy_bench.yaml")
    eng = ToyEngine(results_path=path)
    pack = _toy_pack()
    res = BM.run_pack(pack, eng, BM.Runner(pack, eng, 1), {})
    yaml.safe_dump(BM.stamp(res, eng), open(path, "w"), sort_keys=False)
    b = res["benchmarks"][0]
    arm = {k: math.exp(sum(math.log(x[k]) for x in b["arm_or"]) / len(b["arm_or"])) for k in ("a11", "a10", "a01")}
    den = math.log(arm["a11"] / arm["a10"])
    v_ok = math.exp(0.5 * den) / arm["a01"]          # the interaction at half the increment: reachable
    v_bad = 2.0 / arm["a01"]                           # above what removing the increment reaches: unreachable
    return path, v_ok, v_bad


import math  # noqa: E402  (used by _toy_bench at module level)


def _self_test() -> int:
    import copy
    import math
    import tempfile
    import yaml
    fails = []

    def ok(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    q = lambda *a, **k: None  # noqa: E731
    with tempfile.TemporaryDirectory() as d:
        # ---------------- emulator path, emulator-inversion calibration (the spike's chain)
        bench, v_ok, v_bad = _toy_bench(d)
        p = schema.toy_spec(d, n=24, bench=bench, dt_values=(v_ok, v_bad))
        try:
            run(d, log=q)
            ok(False, "R1 an unsigned spec is refused")
        except PermissionError:
            ok(True, "R1 an unsigned spec is refused")
        signoff.sign(d, "lead", note="self-test")
        R = run(d, log=q, compute_kw={"max_workers": 2})
        acc = R["emulator_accuracy"]
        ok(acc["reduction_r2_median"] > 0.9 or acc["reduction_mae_median"] < 0.01,
           f"R2 emulator held-out accuracy on the toy engine ({acc})")
        spec = schema.load(p)
        errs = []
        for i, v in R["calibration"]["units"].items():
            t = float(next(u for u in spec.units() if u["id"] == i)["tgt"])
            errs.append(abs(v["z"] - math.log(-math.log(1 - t) / 0.5) / 0.8))
        ok(max(errs) < 0.1, f"R3 calibration by emulator inversion recovers the toy attribute (max error {max(errs):.3f})")
        pt = R["point"]
        ok(pt["solver"]["status"] == "optimal" and pt["cost_allocated"] <= 9000 * (1 + 1e-9),
           "R4 the plan is a proven optimum within the budget (toolkit/alloc ILP)")
        from toolkit.engines.contract import engine_identity as _eid
        ok(pt.get("engine_identity") == _eid(spec.engine()),
           "R4b the point artifact records the analysis's full engine identity (emulator path; Codex r3 P1)")
        fp = R["uncertainty"]["fixed_plan"]["dalys"]
        ok(fp["ci_low"] <= fp["median"] <= fp["ci_high"] and "not covered" in R["uncertainty"]["label"],
           f"R5 the interval is computed and labelled with what it covers ({R['uncertainty']['label']})")
        af = R["uncertainty"].get("affordability") or {}
        ok(af.get("draws") == R["uncertainty"]["draws"] and 0.0 <= af.get("share_over_budget", -1) <= 1.0
           and (af.get("fixed_plan_cost") or {}).get("median") is not None
           and abs(af.get("budget_allocatable", 0) - 9000) < 1e-6,
           f"R5b the fixed plan's cost is recorded in every draw and its share over budget reported (F6 S1-1): {af}")
        rep0 = yaml.safe_load(open(os.path.join(d, "uncertainty_report.yaml")))
        ok("fixed_plan_cost" not in (rep0.get("scalar_outputs") or {}) and rep0.get("affordability") == af,
           "R5c one cost summary: the report carries affordability, not a second scalar summary (Codex S1-1 P2)")
        cs = {c["id"]: c for c in (yaml.safe_load(open(os.path.join(d, CL.SPEC_REL))) or {}).get("claims", [])}
        ok("uq.share_over_budget" in cs and (cs.get("uq.fixed_plan_cost.median") or {}).get("interval")
           == R["uncertainty"]["interval"],
           "R5d claims: the share over budget, and cost quantiles labelled with the interval (Codex S1-1 P2)")
        # Codex S1-1 P2: costs that move in each draw (a drawn cost multiplier), checked draw by draw against an
        # independent replay of the same draws
        import numpy as np
        from toolkit.analysis import uncertainty as U
        A5 = R["_A"]
        saved = copy.deepcopy(A5.spec.raw["uncertainty"])
        try:
            A5.spec.raw["uncertainty"]["sources"] = list(saved["sources"]) + [{"kind": "costs",
                                                                                "items": {"ca": [0.5, 2.0]}}]
            A5.spec.raw["uncertainty"]["excluded"] = [x for x in saved.get("excluded") or [] if x.get("kind") != "costs"]
            A5.spec.raw["uncertainty"]["affordability"] = {"max_share_over_budget": 0.2}
            u5 = U.run(A5, R["point"]["plan"], "total_dalys_averted", draws=8, seed=3)["data"]["affordability"]
            D5 = U.Drawer(A5, 3)
            D5.prepare(8)
            want = []
            for _ in range(8):
                th, zs, noise, mult, _s = D5.draw()
                _o, _b, c5 = A5.evaluate(th, zs, noise=noise, cost_mult=mult)
                want.append(sum(c5[i][R["point"]["plan"][i]] for i in A5.ids))
            n_over = sum(c > A5.budget * (1 + 1e-9) for c in want)
            fq = u5.get("fixed_plan_cost") or {}
            ok(abs(fq.get("median", -1) - float(np.median(want))) <= 1e-9 * max(want)
               and abs(fq.get("ci_high", -1) - float(np.percentile(want, 97.5))) <= 1e-9 * max(want),
               "R5f the cost quantiles are the replayed draws' own quantiles")
            ok(u5.get("per_draw_cost") == want and len(set(round(c, 6) for c in want)) > 1
               and u5["draws_over_budget"] == n_over and u5["rule_met"] == (n_over / 8 <= 0.2)
               and u5["budget_allocatable"] == A5.budget,
               f"R5e with drawn cost multipliers each draw's cost equals an independent replay, and the rule is judged "
               f"on those draws ({u5.get('draws_over_budget')} of 8 over)")
            _self_test_reserve(ok, A5, R, u5)
        finally:
            A5.spec.raw["uncertainty"] = saved
            A5.plan_budget = None
        stb = R["stability"]
        ok(stb["n_folds"] == 4 and stb["value_verdict"] in ("ROBUST", "FRAGILE", "UNSTABLE")
           and "total_benefit_loss_pct_worst_fold" in stb["metrics"],
           "R6 leave-one-group-out runs every fold and reports value lost beside package identity (lesson 5)")
        dt = R["decision_tests"][0]
        res = yaml.safe_load(open(os.path.join(d, dt["path"])))
        r1 = dt["results"][0]
        ok(abs(r1["achieved_interaction"] - v_ok) <= 1e-9 * v_ok and [x["value"] for x in dt["rejected_values"]] == [v_bad]
           and res["produced_by"] == "harness" and isinstance(res["decision_changes"], bool)
           and "verified benchmark results" in res["arm_source"],
           "R7 interaction rescale: arms from verified benchmark results, the achieved interaction equals the requested "
           "one, an unreachable value is rejected (Codex P1-12, P1-13)")
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import mechanism_manifest as MM
        if hasattr(MM, "load_test_result"):
            got, probs = MM.load_test_result(d, dt["path"])
            ok(got is not None and not probs, f"R8 the result file passes the Phase 49 loader: {probs}")
        else:
            ok(set(res) >= {"finding", "varied", "criterion", "decision_changes", "recommendation"},
               "R8 the result file has every Phase 49 field (loader not on this branch)")
        rows = list(csv.DictReader(open(os.path.join(d, "allocation_optimized.csv"))))
        ok(len(rows) == 24 and set(rows[0]) >= {"unit", "package", "dalys_averted"}, "R9 the deliverable CSV is written")
        rep = yaml.safe_load(open(os.path.join(d, "uncertainty_report.yaml")))
        ok("total_dalys_averted" in rep["scalar_outputs"] and rep["interval_label"] == R["uncertainty"]["label"]
           and rep["interval"] == R["uncertainty"]["interval"],
           "R10 uncertainty_report.yaml carries the headline output and the structured interval")
        txt = open(os.path.join(d, "results.md")).read()
        ok("[CLAIM:" not in txt and R["uncertainty"]["label"] in txt, "R11 results.md is fully rendered from the ledger")
        dsec = txt[txt.index("## Decision tests"):]
        ok(all(not ch.isdigit() for ch in dt["statement"]) and "[CLAIM" not in dsec,
           "R11b decision-test statements are words; their numbers are claims (Codex P1-4)")
        ok(all(os.path.exists(os.path.join(d, f["path"]) + ".provenance.json") for f in
               yaml.safe_load(open(os.path.join(d, "analysis", "figures.yaml")))["data"]["figures"]),
           "R12 every figure has a provenance sidecar")
        from toolkit.analysis import contract
        # figure sources relative to the run dir: a run whose outputs moved off the VM (/tmp/...) still checks
        figs = yaml.safe_load(open(os.path.join(d, "analysis", "figures.yaml")))["data"]["figures"]
        rels = [all(not os.path.isabs(s) for s in json.load(open(os.path.join(d, f["path"]) + ".provenance.json"))
                    ["source_hashes"]) for f in figs]
        moved = d + "_moved"
        os.rename(d, moved)
        try:
            fk = [x["claim"] for x in contract.gate_findings(moved) if x["kind"] == "analysis_contract_figure"]
        finally:
            os.rename(moved, d)
        ok(figs and all(rels) and fk == [],
           f"R12b figure sources are recorded relative to the run dir, and a moved run dir checks clean: {fk[:2]}")
        ok(contract.gate_findings(d) == [], f"R13 the contract checks pass on a harness run: {contract.gate_findings(d)}")
        rel = os.path.join("analysis", "point.yaml")
        with open(os.path.join(d, rel), "a") as f:
            f.write("# hand edit\n")
        kinds = {x["kind"] for x in contract.gate_findings(d)}
        ok("analysis_contract_tampered" in kinds, f"R14 a hand-edited artifact is caught: {kinds}")
        ok(DT._lowers_only_unchosen({"u": {"p": 1.0, "q": 2.0}}, {"u": {"p": 0.5, "q": 2.0}}, {"u": "q"})
           and not DT._lowers_only_unchosen({"u": {"p": 1.0, "q": 2.0}}, {"u": {"p": 3.0, "q": 2.0}}, {"u": "q"}),
           "R15 lesson 8: lowering only unchosen packages' benefits is recognised as unable to move the plan")
        # Codex P1-6: extrapolation outside the design domain and clips are recorded and bear on the plan
        A, M = R["_A"], R["_model"]
        i0 = A.ids[0]
        far = {i: (5.0 if i == i0 else R["calibration"]["units"][i]["z"]) for i in A.ids}
        A.evaluate(A.theta0, far)
        ev = A.last_events
        ok(any(e["kind"] == "extrapolation" and e["unit"] == i0 for e in ev)
           and A.plan_corrections(ev, R["point"]["plan"]),
           "R16 a unit outside the design domain is recorded as extrapolation and bears on the plan (Codex P1-6)")
        # Codex P1-3: stored results without provenance are refused; an adopted batch must match and reproduce
        design, jobs = Bt.design(spec)
        stored = os.path.join(d, "stored.jsonl")
        sj = os.path.join(d, "stored_jobs.jsonl")
        sd = os.path.join(d, "stored_design.json")
        with open(os.path.join(d, "analysis", "batch", "results.jsonl")) as f:
            rows_c = [json.loads(x) for x in f if x.strip()]
        res_by = {jobs[r["i"]]["id"]: r["result"]["result"] for r in rows_c if r.get("ok")}
        with open(stored, "w") as f:
            for j in jobs:
                f.write(json.dumps({"id": j["id"], "ok": True, "result": res_by[j["id"]]}) + "\n")
        with open(sj, "w") as f:
            for j in jobs:
                f.write(json.dumps({k: j[k] for k in ("id",) + Bt.CORE}) + "\n")
        json.dump({"spec_engine": design["spec_engine"], "horizon": design["horizon"], "cal_window": design["cal_window"]},
                  open(sd, "w"))
        got, probs = Bt.load_results(stored, jobs, spec.engine())
        ok(not got and probs and "no provenance" in probs[0], "R17 stored results without provenance are refused (P1-3)")
        ad = Bt.adopt(d, stored, sj, sd, jobs, design, spec.engine(), sample=4, workers=2)
        got, probs = Bt.load_results(ad["path"], jobs, spec.engine())
        ok(len(got) == len(jobs) and not probs and ad["rerun_reproduced"] == 4,
           "R18 a stored batch is adopted after a full-input match and a re-run sample that reproduces it")
        bad_jobs = [json.loads(x) for x in open(sj)]
        bad_jobs[1]["seed"] += 1
        with open(sj, "w") as f:
            f.write("".join(json.dumps(x) + "\n" for x in bad_jobs))
        try:
            Bt.adopt(d, stored, sj, sd, jobs, design, spec.engine(), sample=4, workers=2)
            ok(False, "R19 a stored job whose inputs differ is not adopted")
        except ValueError as e:
            ok("inputs differ" in str(e), "R19 a stored job whose inputs differ is not adopted")
        bad_jobs[1]["seed"] -= 1
        with open(sj, "w") as f:
            f.write("".join(json.dumps(x) + "\n" for x in bad_jobs))
        lines = open(stored).read().splitlines()
        first = json.loads(lines[0])
        first["result"]["per_capita"]["cases"]["all"] *= 1.5
        lines[0] = json.dumps(first)
        open(stored, "w").write("\n".join(lines) + "\n")
        try:
            Bt.adopt(d, stored, sj, sd, jobs, design, spec.engine(), sample=len(jobs), workers=2)
            ok(False, "R20 a stored result the current engine does not reproduce is not adopted")
        except ValueError as e:
            ok("does not reproduce" in str(e), "R20 a stored result the current engine does not reproduce is not adopted")
        # a batch shipped from another platform with its exact inputs (the Nigeria re-run's shards): the analysis
        # uses the shipped jobs and design, and records where they were computed
        try:
            sh = os.path.join(d, "shipped")
            Bt.ship(sh, spec, design, jobs, [{"id": j["id"], "ok": True, "result": Bt.run_job(j)} for j in jobs],
                    platform=dict(Bt.platform_info(), system="OtherOS"), root=d)
            run(d, log=q, adopt={"shipped": sh}, compute_kw={"max_workers": 2}, draws=2)
            bdoc = yaml.safe_load(open(os.path.join(d, "analysis", "batch.yaml")))
            sm = bdoc["data"]["summary"]
            ok(sm.get("source") == "shipped batch" and sm["shipped"]["cross_platform"] is True
               and sm["n"] == len(jobs) and contract.gate_findings(d) == []
               and any(k.endswith("shipped/jobs.jsonl") for k in bdoc["inputs"]),
               f"R20b a batch shipped from another platform runs the analysis on its own inputs: {sm.get('source')}")
        except Exception as e:      # noqa: BLE001 -- a failing case, not a crash of the self-test
            ok(False, f"R20b a batch shipped from another platform runs the analysis ({type(e).__name__}: {e})")
        from toolkit.analysis.model import _driver
        try:
            _driver({"cost_drivers": {"x": 1.0}}, "treated", "t")
            ok(False, "R21 a missing cost driver fails closed (Codex P1-7)")
        except ValueError:
            ok(True, "R21 a missing cost driver fails closed (Codex P1-7)")
        run(d, log=q, compute_kw={"max_workers": 2}, draws=2)
        ok(contract.gate_findings(d) == [], "R22 a rerun publishes a fresh complete manifest")
        orig = EmulatorModel.predict

        def clipped(self, zs, theta, ids, noise=None):
            out = orig(self, zs, theta, ids, noise)
            self.last_events = self.last_events + [{"unit": ids[0], "package": "*", "kind": "reduction_clip"}]
            return out
        EmulatorModel.predict = clipped
        try:
            run(d, log=q, compute_kw={"max_workers": 2}, draws=2)
            ok(False, "R23 a correction bearing on the plan blocks the run (Codex P1-6)")
        except AnalysisBlocked:
            ok({x["kind"] for x in contract.gate_findings(d)} == {"analysis_contract_blocked"}
               and os.path.exists(os.path.join(d, "analysis", "blocked.yaml")),
               "R23 a correction bearing on the plan blocks the run before anything is published (Codex P1-6)")
        finally:
            EmulatorModel.predict = orig
        _self_test_round2(ok, d, q)
        _self_test_reserve_e2e(ok, d, q)
    with tempfile.TemporaryDirectory() as d:
        # ---------------- direct path, likelihood calibration (a cheap engine fitted to a panel)
        _direct_toy(d)
        signoff.sign(d, "lead")
        R = run(d, log=q)
        th = R["calibration"]["theta"]
        ok(abs(th["lrr:a"] - math.log(0.6)) < 0.05 and abs(th["lrr:a+b"] - math.log(0.5)) < 0.05,
           f"D1 likelihood calibration recovers the planted package effects ({th})")
        from toolkit.engines.contract import engine_identity as _eid
        ok((R["point"].get("engine_identity") or {}).get("code_sha256")
           == _eid(schema.load(os.path.join(d, "decision_spec.yaml")).engine())["code_sha256"],
           "D1b the point artifact records the analysis's full engine identity (direct path; Codex r3 P1)")
        ident = R["calibration"]["identifiability"]["parameters"]
        ok(all(v["status"] in ("well_identified", "weakly_identified", "unidentified") for v in ident.values())
           and ident["lrr:a"]["status"] == "well_identified", f"D2 identifiability from scripts/identifiability.py: "
           f"{ {k: v['status'] for k, v in ident.items()} }")
        inter = {(r["a"], r["b"]): r for r in R["point"]["interactions"]}
        ok(abs(inter[("a", "b")]["interaction_ratio"] - 0.5 / (0.6 * 0.7)) < 0.05,
           f"D3 the interaction ratio is computed from the point outcomes ({inter[('a', 'b')]['interaction_ratio']:.3f})")
        ok(R["stability"]["n_folds"] == 3 and R["uncertainty"]["covers"] == ["calibration"],
           "D4 likelihood path: refit-based stability and calibration-covariance uncertainty")
        gr = next(x for x in R["decision_tests"] if x["type"] == "group_rule")
        ok(gr["can_move_plan"] and gr["recommendation"]["with"], "D5 a group-rule decision test re-solves the plan")
        from toolkit.analysis import contract
        ok(contract.gate_findings(d) == [], f"D6 contract checks pass on the direct path: {contract.gate_findings(d)}")
    _self_test_priors(ok, q)
    _self_test_posterior(ok, q)
    _self_test_joint(ok, q)
    _self_test_stability_flagged(ok)
    # AR-RW1 (run console review round 4, P1-2): artifacts and the manifest never follow a link or a planted .tmp
    import tempfile as _tf
    with _tf.TemporaryDirectory() as d, _tf.TemporaryDirectory() as out:
        open(os.path.join(out, "v"), "w").write("untouched")
        os.makedirs(os.path.join(d, "analysis"))
        os.symlink(os.path.join(out, "v"), os.path.join(d, "analysis", "point.yaml"))
        st_ = Store(d, None, False, {})
        os.symlink(os.path.join(out, "v"), os.path.join(d, "analysis", "manifest.json.tmp"))
        from toolkit.analysis.artifacts import KINDS as _KINDS
        for fn in (lambda: st_.write("point", {k: 0 for k in _KINDS["point"]}, "t"),
                   lambda: st_.finalize("blocked", "t")):
            try:
                fn()
            except (ValueError, OSError):
                pass
        ok(open(os.path.join(out, "v")).read() == "untouched",
           "AR-RW1 an analysis artifact and the manifest are not written through a link or a planted .tmp")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def _self_test_posterior(ok, q):
    """v2d β: the direct toy panel calibrated by `posterior` (declared priors, joint posterior sample)."""
    import tempfile
    import yaml
    from toolkit.analysis import contract
    with tempfile.TemporaryDirectory() as d:
        _direct_toy(d)
        sp = os.path.join(d, "decision_spec.yaml")
        spec = yaml.safe_load(open(sp))
        lrr = {"dist": "uniform", "range": [-2.0, 1.0], "source": "toy: a wide declared range"}
        spec["calibration"].update(method="posterior", priors={
            "lrr:a": lrr, "lrr:b": lrr, "lrr:a+b": lrr,
            "log_rate": {"dist": "uniform", "range": [-8.0, 2.0], "source": "toy: the calibration range"}},
            observation={"model": "poisson"})
        yaml.safe_dump(spec, open(sp, "w"), sort_keys=False)
        signoff.sign(d, "lead")
        try:
            R = run(d, log=q)
        except Exception as e:      # noqa: BLE001 -- a failing case, not a crash of the self-test
            ok(False, f"AN-b7 a posterior-calibrated allocation runs ({type(e).__name__}: {e})")
            return
        st = R["stability"]
        ok(st.get("n_folds") == 3 and st.get("calibration_method") == "posterior" and st.get("method") != "skipped",
           f"AN-b7 a posterior-calibrated allocation runs its stability folds instead of reporting 'nothing "
           f"calibrated' ({st.get('method')}, {st.get('n_folds')} folds)")
        cal = R["calibration"]
        th = cal.get("theta") or {}
        ok(cal.get("method") == "posterior" and abs(th.get("lrr:a", 9) - math.log(0.6)) < 0.1
           and set((cal.get("identifiability") or {}).get("classes", {})) >= {"lrr:a", "z:u0"},
           f"AN-b7b the posterior recovers the planted effects and classes every estimated name ({th})")
        u = R["uncertainty"]
        ok(u.get("draw_provenance", {}).get("findings") == [] and u.get("draw_provenance", {}).get("draws") == 8
           and "calibration" in u["covers"],
           f"AN-b7c uncertainty draws are rows of the canonical posterior sample (provenance recorded and checked): "
           f"{u.get('draw_provenance', {}).get('findings')}")
        ok(contract.gate_findings(d) == [], f"AN-b7d contract checks pass on the posterior path: "
                                            f"{contract.gate_findings(d)}")


def _self_test_joint(ok, q):
    """Joint calibration in ANALYZE (toolkit/analysis/joint_calibration.py): it runs before the outcome batch, an
    unmatched unit stops the run there (no batch), a matched one feeds the region into the draws; a clamped emulator
    inversion stops at the calibration step."""
    import tempfile
    import yaml
    from toolkit.analysis import joint_calibration as JC
    from toolkit.analysis import contract
    with tempfile.TemporaryDirectory() as d:
        JC._toy_joint(d, unreachable="5", group=False)
        signoff.sign(d, "lead", note="self-test")
        try:
            run(d, log=q, compute_kw={"max_workers": 2})
            ok(False, "JR1 a unit the joint calibration cannot match blocks the run")
        except AnalysisBlocked as e:
            b = yaml.safe_load(open(os.path.join(d, "analysis", "blocked.yaml")))["data"]
            ok(e.where == "calibration" and any(h["unit"] == "5" for h in e.hits) and "joint calibration" in b["reason"]
               and not os.path.exists(os.path.join(d, "analysis", "batch", "results.jsonl"))
               and os.path.exists(os.path.join(d, "analysis", "joint_calibration.yaml")),
               "JR1 a unit the joint calibration cannot match stops the run at the calibration step, before the "
               "outcome batch (blocked.yaml names it; the joint calibration artifact is written)")
    with tempfile.TemporaryDirectory() as d:     # owner decision 2026-11-06: flagged, never silently
        sp = JC._toy_joint(d, unreachable="5", group=False)
        raw = yaml.safe_load(open(sp))
        raw["calibration"]["joint"]["unmatched"] = {"mode": "flag", "max_units": 1, "default_reason": "toy reason"}
        # a narrow prior on the toy's (unidentified) base keeps the best point off the prior's edge, so the stability
        # folds' predicted unit values stay inside the emulator's design (a toy property, not part of the flag)
        raw["uncertainty"]["priors"]["base"]["range"] = [0.45, 0.55]
        yaml.safe_dump(raw, open(sp, "w"), sort_keys=False)
        signoff.sign(d, "lead", note="self-test")
        try:
            R = run(d, log=q, compute_kw={"max_workers": 2})
            rows = list(csv.DictReader(open(os.path.join(d, "allocation_optimized.csv"))))
            rep_ = yaml.safe_load(open(os.path.join(d, "uncertainty_report.yaml")))
            jc_ = yaml.safe_load(open(os.path.join(d, "analysis", "joint_calibration.yaml")))["data"]
            fl = {r["unit"]: r["calibration_flag"] for r in rows}
            ok(fl.get("5") == "toy reason" and all(v == "" for k, v in fl.items() if k != "5")
               and [x["unit"] for x in rep_.get("low_confidence_units") or []] == ["5"]
               and [x["unit"] for x in jc_["unmatched_decision"]["flagged"]] == ["5"]
               and R["point"]["per_unit"]["5"].get("calibration_flag") == "toy reason"
               and "unmatched" in (rep_.get("low_confidence_note") or ""),
               "JR8 with unmatched: flag, an unmatched unit proceeds into the batch and the allocation, flagged "
               "low-confidence with its reason in the allocation file, the point, the uncertainty report and label")
        except AnalysisBlocked as e:
            ok(False, f"JR8 flag mode proceeds (blocked: {e})")
    with tempfile.TemporaryDirectory() as d:
        JC._toy_joint(d)
        signoff.sign(d, "lead", note="self-test")
        R = run(d, log=q, compute_kw={"max_workers": 2})
        A = R["_A"]
        jp = R["uncertainty"].get("draw_provenance", {}).get("joint_calibration") or {}
        rg = A.joint["ranges"]["base"]
        ok(jp.get("draws") == 6 and R["calibration"].get("joint", {}).get("shared") == ["base"]
           and abs(A.theta0["base"] - A.joint["best"]["base"]) < 1e-12,
           f"JR2 a matched joint calibration: the point uses the region's best value and the draws are region rows "
           f"(provenance {jp})")
        prt = [t for t in R["decision_tests"] if t.get("test_id") == "prior_range_base"]
        from toolkit.analysis import decision_tests as DT_
        pt_ = [t for t in DT_.prior_range_tests(A) if t["parameter"] == "base"]
        ok(len(prt) == 1 and prt[0].get("status") != "not_run" and len(pt_) == 1 and pt_[0]["values"] == rg
           and pt_[0].get("joint"),
           f"JR3 the jointly calibrated parameter's prior_range test runs at its region's ends ({pt_ and pt_[0]['values']}"
           f" vs {rg})")
        jz = yaml.safe_load(open(os.path.join(d, "analysis", "joint_calibration.yaml")))["data"]["best"]["z"]
        pz = {i: v["z"] for i, v in R["point"]["per_unit"].items()}
        smp = A.joint["sample"]
        ok(pz == {i: float(v) for i, v in jz.items()} and any(n.startswith("z:") for n in smp["names"])
           and jp.get("draws") == 6,
           "JR6 (Codex r1 P1-2) the point uses the joint calibration's unit values and the draws its full rows (shared "
           "values and unit values together), not a re-inversion of one target")
        figs_ = yaml.safe_load(open(os.path.join(d, "analysis", "figures.yaml")))["data"]
        names_ = {f["figure"] for f in figs_["figures"]}
        ok({"calibration_unit_fit", "calibration_waves"} <= names_
           and all(f["tier"] == "data_model" for f in figs_["figures"] if f["figure"].startswith("calibration_"))
           and os.path.exists(os.path.join(d, "figures", "joint_calibration", "calibration_unit_fit.png")),
           f"JR7 the calibration figures are drawn at the calibration step and listed in the figure manifest (data and "
           f"model tier): {sorted(n for n in names_ if n.startswith('calibration_'))}")
        ok(rg[0] >= 0.3 - 1e-9 and rg[1] <= 0.7 + 1e-9 and contract.gate_findings(d) == [],
           f"JR4 the region lies inside the prior and the contract checks pass ({contract.gate_findings(d)})")
    with tempfile.TemporaryDirectory() as d:
        p = schema.toy_spec(d, n=24)
        rows = list(csv.DictReader(open(os.path.join(d, "units.csv"))))
        rows[3]["tgt"] = "0.9999"
        with open(os.path.join(d, "units.csv"), "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        signoff.sign(d, "lead", note="self-test")
        try:
            run(d, log=q, compute_kw={"max_workers": 2})
            ok(False, "JR5 a clamped emulator inversion blocks at the calibration step")
        except AnalysisBlocked as e:
            ok(e.where == "calibration" and [h["unit"] for h in e.hits] == [rows[3]["id"]]
               and not os.path.exists(os.path.join(d, "analysis", "point.yaml")),
               f"JR5 a clamped emulator inversion blocks at the calibration step, before the point plan ({e.hits})")
    _ = p


def _self_test_stability_flagged(ok):
    """Codex r6 P1-3, P2: a flagged unit informs neither the held-out prediction nor the covariate scaling; a fold
    without enough unflagged training units is reported not_run, and the metrics are computed without it."""
    from toolkit.analysis import stability as SB

    class _A:
        method, theta0 = "emulator_inversion", {}

        def __init__(self, ids, xs, flagged):
            self.ids = list(ids)
            self.by_id = {i: {"g": f"g{n % 3}", "x": xs[i]} for n, i in enumerate(ids)}
            self.spec = {"analysis": {"stability": {"holdout_group": "g", "covariates": ["x"]}}}
            self.flagged, self.seen = flagged, {}

        def evaluate(self, theta, zh, hid):
            self.seen.update({i: zh[i] for i in hid})
            return {}, {i: [1.0 + zh[i], 0.5] for i in hid}, {i: [1.0, 2.0] for i in hid}

        def solve(self, ben, cost):
            return {"choice": {i: 0 for i in self.ids}}

        def guard(self, *a):
            pass
    ids = [f"u{k}" for k in range(12)]
    xs = {i: float(k) for k, i in enumerate(ids)}
    z0 = {i: 0.1 * k for k, i in enumerate(ids)}
    ben = {i: [1.0, 0.5] for i in ids}
    cost = {i: [1.0, 2.0] for i in ids}
    plan = {i: 0 for i in ids}
    xs_out = dict(xs, u11=1000.0)
    with_flag = _A(ids, xs_out, {"u11": {"reason": "toy"}})
    SB.run(with_flag, dict(z0, u11=9.0), plan, ben, cost, plan)
    keep = [i for i in ids if i != "u11"]
    without = _A(keep, xs, {})
    without.by_id = {i: with_flag.by_id[i] for i in keep}
    SB.run(without, z0, plan, ben, cost, plan)
    common = [i for i in without.seen if i in with_flag.seen]
    ok(common and all(abs(with_flag.seen[i] - without.seen[i]) < 1e-9 for i in common),
       "JR9a a flagged unit with an extreme covariate leaves every held-out prediction unchanged")
    allf = _A(ids, xs, {i: {"reason": "toy"} for i in ids})
    try:
        r = SB.run(allf, z0, plan, ben, cost, plan)
        ok(r["n_folds"] == 0 and len(r["folds_not_run"]) == 3 and r["value_verdict"] == "NOT_RUN"
           and r["verdict"] == "NOT_RUN",
           f"JR9b folds without unflagged training units are not_run and the metrics skip them ({r['verdict']})")
    except Exception as e:  # noqa: BLE001
        ok(False, f"JR9b folds without unflagged training units are not_run ({type(e).__name__}: {e})")


def _self_test_priors(ok, q):
    """v2 priors on the emulator path: an engine-card prior (a DALY weight) and a scenario parameter with a spec range
    enter the design, are drawn, get prior_range tests and are named in the label; correlated emulator error."""
    import tempfile
    import yaml
    from toolkit.analysis import contract
    from toolkit.spec import priors as PRI
    with tempfile.TemporaryDirectory() as d:
        pp = PRI.toy_priors_file(d)
        p = schema.toy_spec(d, n=24)
        r = yaml.safe_load(open(p))
        r["engine"] = {"ref": "toolkit.spec.priors:ToyPriorEngine", "config": {"priors_path": pp}}
        r["uncertainty"]["sources"].append({"kind": "emulator"})
        r["uncertainty"]["excluded"] = [{"kind": "costs", "reason": "toy"}]
        r["uncertainty"]["priors"] = {"synergy": {"dist": "uniform", "range": [-0.05, 0.1], "source": "toy spec range"}}
        r["analysis"]["decision_tests"] = []
        yaml.safe_dump(r, open(p, "w"), sort_keys=False)
        signoff.sign(d, "lead", note="self-test")
        R = run(d, log=q, compute_kw={"max_workers": 2})
        M = R["_model"]
        design = json.load(open(os.path.join(d, "analysis", "batch", "design.json")))
        ok(design["prior_names"] == ["dw", "synergy"] and M.input_names[:5] == ["z", "eff_a", "eff_b", "dw", "synergy"]
           and all(0.005 <= pt["theta"]["dw"] <= 0.02 for pt in design["points"]),
           f"RP1 drawn priors enter the emulator design over their declared ranges: {M.input_names}")
        u = R["uncertainty"]
        ok("priors" in u["covers"] and "set by declared ranges, not by data: dw, synergy" in u["label"]
           and "held fixed: 2 engine parameters" in u["label"], f"RP2 the label names the priors and what is fixed: "
                                                                f"{u['label']}")
        rep = yaml.safe_load(open(os.path.join(d, "uncertainty_report.yaml")))
        dws = rep["parameter_samples"]["dw"]
        ok(set(rep["sources_drawn"]) == {"engine_parameters", "calibration", "emulator", "priors"}
           and [x["kind"] for x in rep["sources_not_drawn"]] == ["costs"]
           and {x["name"] for x in rep["parameters_fixed"]} == {"base", "k"}
           and all(x["reason"] for x in rep["parameters_fixed"]) and 0.005 <= dws["ci_low"] < dws["ci_high"] <= 0.02,
           f"RP3 the report lists every source and every fixed parameter with why, and the priors vary: {dws}")
        em = rep["emulator_error"]
        ok(em and em["holdout_points"] >= 3 and em["sd_common_median"] > 0 and "population-mean" in em["model"],
           f"RP4 the emulator-error model and its held-out evidence are in the report: {em and em['sd_common_median']}")
        prt = {x["test_id"]: x for x in R["decision_tests"]}
        t = prt.get("prior_range_dw")
        ok(set(prt) == {"prior_range_dw", "prior_range_synergy"} and t["automatic"] and t["status"] == "run"
           and [x["value"] for x in t["results"]] == [0.005, 0.02]
           and t["results"][0]["point_plan_objective_under_test"] < t["results"][1]["point_plan_objective_under_test"]
           and os.path.exists(os.path.join(d, t["path"])),
           "RP5 every drawn prior gets an automatic prior_range test; the DALY-weight ends move the objective")
        ok(contract.gate_findings(d) == [], f"RP6 the contract checks pass with priors: {contract.gate_findings(d)}")
        from toolkit.analysis import decision_tests as DTm
        A = R["_A"]
        z0 = {i: v["z"] for i, v in R["calibration"]["units"].items()}
        out0, ben0, cost0 = A.evaluate(A.theta0, z0)
        plan0 = R["point"]["plan"]
        orig_cal = A.calibrate
        A.calibrate = lambda th, shifts=None, ids=None: {i: (z, True) for i, (z, _c) in orig_cal(th, shifts, ids).items()}
        try:
            rec = DTm.run_one(A, DTm.prior_range_tests(A)[0], z0, ben0, cost0, plan0, R["uncertainty"], out0)
        finally:
            A.calibrate = orig_cal
        ok(all(x.get("calibration_clamped_units") == len(A.ids) for x in rec["results"])
           and "calibration clamped" in rec["statement"],
           f"RP8 an end whose calibration clamps is flagged in the result and its statement (Codex r1 P1-3): "
           f"{rec['statement']}")
        err = M.error
        npk = len(M.packages) - 1
        ok(err is not None and len(err.cols) == len(M.pops) * len(M.outcomes) * npk
           and {c[1] for c in err.cols} == set(M.outcomes),
           f"RP9 each outcome gets its own held-out residual column, so one outcome's bias is not applied to another "
           f"(Codex r2 P1): {err and len(err.cols)} columns")
        design.pop("prior_names")
        try:
            EmulatorModel(schema.load(p), design, {}, log=q)
            ok(False, "RP7 a design that lacks a drawn prior is refused, naming it")
        except ValueError as e:
            ok("['dw', 'synergy']" in str(e) and "must enter the design" in str(e),
               f"RP7 a design that lacks a drawn prior is refused, naming it: {e}")
        # a parameter_value test outside the design box: before this, the emulator extrapolated and the guard blocked
        # the whole analysis; now the value is refused (not_run, with why) and the run completes
        rec = DTm.run_one(A, {"id": "DTx", "type": "parameter_value", "parameter": "dw", "values": [0.001],
                              "text": "t"}, z0, ben0, cost0, plan0, R["uncertainty"], out0)
        ok(rec["status"] == "not_run" and "outside the emulator design" in rec["reason"],
           f"RP11 a parameter_value test value outside the emulator design box is refused, not extrapolated: {rec}")
    _self_test_test_ranges(ok, q)


def _self_test_test_ranges(ok, q):
    """A parameter_value decision test on a drawn prior at a value outside its declared range: the emulator design box
    spans the test's values too (the draws stay inside the declared range), so the test is evaluated inside the design
    and the run completes."""
    import tempfile
    import yaml
    from toolkit.spec import priors as PRI
    with tempfile.TemporaryDirectory() as d:
        pp = PRI.toy_priors_file(d)
        p = schema.toy_spec(d, n=24)
        r = yaml.safe_load(open(p))
        r["engine"] = {"ref": "toolkit.spec.priors:ToyPriorEngine", "config": {"priors_path": pp}}
        r["uncertainty"]["sources"].append({"kind": "emulator"})
        r["uncertainty"]["excluded"] = [{"kind": "costs", "reason": "toy"}]
        r["analysis"]["decision_tests"] = [{"id": "DTw", "type": "parameter_value", "parameter": "dw",
                                            "values": [0.002], "text": "weaker weight, below the prior range"}]
        yaml.safe_dump(r, open(p, "w"), sort_keys=False)
        signoff.sign(d, "lead", note="self-test")
        try:
            R = run(d, log=q, compute_kw={"max_workers": 2})
        except Exception as e:  # noqa: BLE001
            ok(False, f"RP10 a parameter_value test below a prior's range runs inside the design: {type(e).__name__}: "
                      f"{str(e)[:200]}")
            return
        design = json.load(open(os.path.join(d, "analysis", "batch", "design.json")))
        dws = [pt["theta"]["dw"] for pt in design["points"]]
        ok(design.get("design_ranges", {}).get("dw") == [0.002, 0.02] and min(dws) < 0.005 and max(dws) <= 0.02,
           f"RP10 the design box of dw spans the test value and the prior range: {design.get('design_ranges')}, "
           f"min {min(dws):.4f}")
        t = {x["test_id"]: x for x in R["decision_tests"]}["DTw"]
        rep = yaml.safe_load(open(os.path.join(d, "uncertainty_report.yaml")))
        s = rep["parameter_samples"]["dw"]
        ok(t["status"] == "run" and [x["value"] for x in t["results"]] == [0.002]
           and 0.005 <= s["ci_low"] < s["ci_high"] <= 0.02,
           f"RP12 the test runs at 0.002 and the draws stay inside the declared prior range: {t['status']}, {s}")


def _self_test_reserve(ok, A, R, u_unc):
    """F6 S1-1b on the emulator-path analysis, with a drawn cost multiplier (costs vary by draw): the unconstrained
    plan fails the rule; the enforced plan, solved within a reserve, meets it on the same draws, and the contract
    check finds nothing; the reserve and the value given up are reported; an unreachable rule blocks."""
    import tempfile
    import yaml
    from toolkit.analysis import contract, uncertainty as U
    plan0 = R["point"]["plan"]
    z0 = {i: v["z"] for i, v in R["point"]["per_unit"].items()}
    _o, ben0, cost0 = A.evaluate(A.theta0, z0)
    sol0 = A.solve(ben0, cost0)
    fn = globals().get("affordable_plan")
    ok(callable(fn) and sol0["choice"] == plan0, "R5g affordable_plan() exists; the point plan re-solves unchanged")
    if not callable(fn):
        return
    rule = {"max_share_over_budget": 0.2, "enforce": True}
    A.spec.raw["uncertainty"]["affordability"] = rule
    ok(u_unc["rule_met"] is False, f"R5h the unconstrained plan fails the rule on these draws "
                                   f"({u_unc['draws_over_budget']} of 8 over, at most 0.2)")
    res = fn(A, ben0, cost0, sol0, draws=8, seed=3)
    rv, plan1 = res["reserve"], res["solution"]["choice"]
    A.plan_budget = rv["budget_planned"]
    u1 = U.run(A, plan1, "total_dalys_averted", draws=8, seed=3, pre=res["pre"])["data"]
    af1 = u1["affordability"]
    with tempfile.TemporaryDirectory() as t:
        open(os.path.join(t, contract.SPEC), "w").write(yaml.safe_dump({"uncertainty": {"affordability": rule}}))
        f_aff = contract._affordability(t, {"draws": 8, "affordability": af1})
        f_unc = contract._affordability(t, {"draws": 8, "affordability": dict(u_unc, rule=rule)})
    ok(af1["rule_met"] is True and f_aff == [] and f_unc and af1["draws_over_budget"] == rv["affordable"]["draws_over_budget"]
       and rv["unconstrained"]["draws_over_budget"] == u_unc["draws_over_budget"],
       f"R5i the enforced plan meets the rule on the uncertainty step's own draws and the contract check finds nothing; "
       f"the unconstrained plan is a finding ({af1['draws_over_budget']} of 8 over; {f_aff[:1]})")
    ok(0.5 <= rv["fraction"] < 1.0 and abs(rv["reserve_usd"] - (1 - rv["fraction"]) * A.budget) < 1e-6
       and abs(rv["value_given_up"] - (rv["unconstrained"]["objective"] - rv["affordable"]["objective"])) < 1e-9
       and rv["value_given_up"] >= 0 and abs(rv["value_given_up_pct"] - 100 * rv["value_given_up"]
                                             / rv["unconstrained"]["objective"]) < 1e-9
       and rv["search"]["steps"][0]["fraction"] == 1.0 and rv["search"]["tolerance"] > 0,
       f"R5j the reserve (fraction {rv['fraction']:.4f}, {rv['reserve_usd']:,.0f} USD held back) and the value given "
       f"up ({rv['value_given_up_pct']:.2f}%) are reported with the search steps")
    A.spec.raw["uncertainty"]["affordability"] = {"max_share_over_budget": 0.0, "enforce": True}
    A.spec.raw["uncertainty"]["sources"][-1] = {"kind": "costs", "items": {"ca": [1.9, 2.0]}}
    try:
        fn(A, ben0, cost0, sol0, draws=8, seed=3, floor=0.95)
        ok(False, "R5k a rule no plan at or above the floor meets blocks the analysis")
    except AnalysisBlocked as e:
        ok(e.where == "affordability" and "floor" in (e.reason or ""),
           f"R5k a rule no plan at or above the floor meets blocks the analysis: {e.reason}")


def _self_test_reserve_e2e(ok, d, q):
    """F6 S1-1b end to end: a signed spec that enforces the rule; point.yaml, claims and api.headline carry the reserve,
    and the contract checks pass."""
    import yaml
    from toolkit.analysis import api, contract

    def mut(r):
        r["uncertainty"]["sources"] = list(r["uncertainty"]["sources"]) + [{"kind": "costs", "items": {"ca": [0.5, 2.0]}}]
        r["uncertainty"]["excluded"] = [x for x in r["uncertainty"].get("excluded") or [] if x.get("kind") != "costs"]
        r["uncertainty"]["affordability"] = {"max_share_over_budget": 0.2, "enforce": True}
    try:
        _resign(d, mut)
        R = run(d, log=q, compute_kw={"max_workers": 2}, draws=8)
    except Exception as e:  # noqa: BLE001
        ok(False, f"R33 an enforced affordability rule runs end to end: {type(e).__name__}: {e}")
        return
    pt = yaml.safe_load(open(os.path.join(d, "analysis", "point.yaml")))["data"]
    rv = pt.get("reserve") or {}
    af = R["uncertainty"]["affordability"]
    ok(rv.get("fraction") is not None and pt["plan"] == R["point"]["plan"] and af["rule_met"] is True
       and abs(pt["solver"]["objective"] - rv["affordable"]["objective"]) < 1e-9,
       f"R33 the headline plan is the affordable plan, and it meets the rule: {af['draws_over_budget']} of 8 over")
    cs = {c["id"] for c in (yaml.safe_load(open(os.path.join(d, CL.SPEC_REL))) or {}).get("claims", [])}
    hl = api.headline(d)
    ok({"reserve.fraction", "reserve.usd", "reserve.value_given_up", "reserve.value_given_up_pct"} <= cs
       and {"reserve_fraction", "reserve_usd", "value_given_up", "value_given_up_pct"} <= set(hl),
       f"R34 claims and api.headline carry the reserve and the value given up: {sorted(k for k in hl if 'reserve' in k)}")
    ok(contract.gate_findings(d) == [], f"R35 the contract checks pass on an enforced run: {contract.gate_findings(d)}")


def _resign(d, mutate):
    """Edit the run's signed spec (a new owner decision): drop the old sign-off and sign again."""
    import shutil
    import yaml
    p = os.path.join(d, "decision_spec.yaml")
    raw = yaml.safe_load(open(p))
    mutate(raw)
    yaml.safe_dump(raw, open(p, "w"), sort_keys=False)
    for f in ("decision_spec.signoff.json",):
        if os.path.exists(os.path.join(d, f)):
            os.remove(os.path.join(d, f))
    shutil.rmtree(os.path.join(d, "decision_brief.signed"), ignore_errors=True)
    signoff.sign(d, "lead", note="self-test re-sign")


def _self_test_round2(ok, d, q):
    """Codex round 2 cases, on the emulator-path run directory of the main self-test."""
    import math
    import yaml
    from toolkit.analysis import contract
    # r2 P1-1: a real settings file must not break the run
    with open(os.path.join(d, "analysis_settings.yaml"), "w") as f:   # the settings file exists before sign-off
        yaml.safe_dump({"uncertainty": {"draws": 3}}, f)
    _resign(d, lambda r: r["uncertainty"].pop("draws"))
    try:
        R = run(d, log=q, compute_kw={"max_workers": 2})
        ok(R["uncertainty"]["draws"] == 3 and contract.gate_findings(d) == [],
           f"R24 a run with a real analysis_settings.yaml completes and certifies (r2 P1-1): {contract.gate_findings(d)}")
    except Exception as e:  # noqa: BLE001
        ok(False, f"R24 a run with a real analysis_settings.yaml completes (r2 P1-1): {type(e).__name__}: {e}")
        return
    A = R["_A"]
    spec = A.spec
    # r2 P1-7: consumed files are inputs; replacing or deleting them invalidates the certificate
    rp = os.path.join(d, "analysis", "batch", "results.jsonl")
    keep = open(rp).read()
    open(rp, "w").write(keep + "\n")
    stale = [x for x in contract.gate_findings(d) if x["kind"] == "analysis_contract_stale" and "results.jsonl" in x["claim"]]
    open(rp, "w").write(keep)
    bench = A.eng.results_path
    bkeep = open(bench).read()
    open(bench, "a").write("# edited\n")
    stale_b = [x for x in contract.gate_findings(d) if x["kind"] == "analysis_contract_stale"
               and os.path.basename(bench) in x["claim"]]
    open(bench, "w").write(bkeep)
    ok(stale and stale_b and contract.gate_findings(d) == [],
       "R25 replacing the batch results or the benchmark results invalidates the certificate (r2 P1-7)")
    # r2 P1-2: adoption needs a real re-run sample
    design, jobs = Bt.design(spec)
    stored, sj, sd = (os.path.join(d, n) for n in ("r2_stored.jsonl", "r2_jobs.jsonl", "r2_design.json"))
    rows_c = [json.loads(x) for x in open(rp) if x.strip()]
    by = {jobs[r["i"]]["id"]: r["result"]["result"] for r in rows_c if r.get("ok")}
    with open(stored, "w") as f:
        f.write("".join(json.dumps({"id": j["id"], "ok": True, "result": by[j["id"]]}) + "\n" for j in jobs))
    with open(sj, "w") as f:
        f.write("".join(json.dumps({k: j[k] for k in ("id",) + Bt.CORE}) + "\n" for j in jobs))
    json.dump({"spec_engine": design["spec_engine"], "horizon": design["horizon"], "cal_window": design["cal_window"]},
              open(sd, "w"))
    refused = []
    for n in (0, 1):
        try:
            Bt.adopt(d, stored, sj, sd, jobs, design, spec.engine(), sample=n, workers=2)
        except ValueError as e:
            refused.append("sample" in str(e))
    ok(refused == [True, True], f"R26 adoption with no or too small a re-run sample is refused (r2 P1-2): {refused}")
    # r2 P1-5: the interaction is rescaled on one scale for the benchmark target and the allocation outcomes
    out0, ben0, cost0 = A.evaluate(A.theta0, {i: v[0] for i, v in A.calibrate(A.theta0).items()})
    plan = R["point"]["plan"]
    dt = spec["analysis"]["decision_tests"][0]
    try:
        r = DT._interaction(A, dt, ben0, cost0, plan, out0)
        comp, wa = set(dt["components"]), set(dt["with_any"])
        worst = 0.0
        for (v, ben, _c, _b, _f) in r["cases"]:
            g = r["extra"][v]["increment_scale"]
            for i in A.ids:
                y0 = out0[i]["y"][A.comp][A.primary]
                for k, cs in A.pk.items():
                    if comp <= cs and cs & wa:
                        base = next(n for n, c2 in A.pk.items() if c2 == cs - comp)
                        yb, yk = out0[i]["y"][base][A.primary], out0[i]["y"][k][A.primary]
                        yk_new = y0 - ben[i][k] / A.dfac
                        worst = max(worst, abs(yk_new / yb - (yk / yb) ** g))
        ok(r["cases"] and worst < 1e-9, f"R27 the allocation outcomes are rescaled like the benchmark odds ratio "
                                        f"(log scale; r2 P1-5): worst {worst:.2e}")
    except Exception as e:  # noqa: BLE001
        ok(False, f"R27 the interaction rescale is one transformation (r2 P1-5): {type(e).__name__}: {e}")
    # r2 P1-3: corrections in any later evaluation block publication when they touch a plan
    orig = EmulatorModel.predict
    calls = [0]

    def late(self, zs, theta, ids, noise=None):
        o = orig(self, zs, theta, ids, noise)
        calls[0] += 1
        if calls[0] > 1:          # every evaluation after the point one (draws, stability, sensitivity, tests)
            self.last_events = self.last_events + [{"unit": ids[0], "package": "*", "kind": "reduction_clip"}]
        return o
    EmulatorModel.predict = late
    try:
        run(d, log=q, compute_kw={"max_workers": 2})
        ok(False, "R28 a correction in an uncertainty draw that touches the plan blocks publication (r2 P1-3)")
    except AnalysisBlocked as e:
        ok("uncertainty" in str(e) and {x["kind"] for x in contract.gate_findings(d)} == {"analysis_contract_blocked"},
           f"R28 a correction in an uncertainty draw that touches the plan blocks publication (r2 P1-3): {e}")
    finally:
        EmulatorModel.predict = orig
    calls[0] = 0

    def stab_only(self, zs, theta, ids, noise=None):
        o = orig(self, zs, theta, ids, noise)
        if len(ids) < len(A.ids):  # only the stability folds evaluate a subset of units
            self.last_events = self.last_events + [{"unit": ids[0], "package": "*", "kind": "reduction_clip"}]
        return o
    EmulatorModel.predict = stab_only
    try:
        run(d, log=q, compute_kw={"max_workers": 2})
        ok(False, "R29 a correction in a stability fold that touches its plan blocks publication (r2 P1-3)")
    except AnalysisBlocked as e:
        ok("stability" in str(e), f"R29 a correction in a stability fold that touches its plan blocks publication: {e}")
    finally:
        EmulatorModel.predict = orig
    ends = {k: (lo, hi) for k, (lo, hi, _) in A.fr.items()}

    def sens_only(self, zs, theta, ids, noise=None):
        o = orig(self, zs, theta, ids, noise)
        if any(float(theta.get(k, 1e300)) in e for k, e in ends.items()):   # only sensitivity sets a range end
            self.last_events = self.last_events + [{"unit": ids[0], "package": "*", "kind": "cost_output_floor"}]
        return o
    EmulatorModel.predict = sens_only
    try:
        run(d, log=q, compute_kw={"max_workers": 2})
        ok(False, "R31 a correction in a sensitivity evaluation that touches a plan blocks publication (r2 P1-3)")
    except AnalysisBlocked as e:
        ok("sensitivity" in str(e), f"R31 a correction in a sensitivity evaluation blocks publication: {e.where}")
    finally:
        EmulatorModel.predict = orig
    # r2 P1-4: clamped calibrations in draws are recorded and narrow the interval's claim
    _resign(d, lambda r: r["calibration"]["target_uncertainty"].update(region_sd_logit=4.0))
    R = run(d, log=q, compute_kw={"max_workers": 2})
    u = R["uncertainty"]
    cc = u.get("calibration_clamped") or {}
    ok(cc.get("draws", 0) > 0 and "calibration" not in u["interval"]["covers"]
       and "calibration_partly_clamped" in u["interval"]["covers"] and "partly clamped" in u["label"],
       f"R30 draws whose calibration clamps are recorded and the interval does not claim full calibration coverage "
       f"(r2 P1-4): {cc} {u['label']}")
    # engine: auto: the engine comes from the harness's engine_choice.json; its hash certifies every artifact
    chosen = yaml.safe_load(open(os.path.join(d, "decision_spec.yaml")))["engine"]
    _resign(d, lambda r: r.update(engine={"auto": True, "candidates": [chosen["ref"]]}))
    cp = os.path.join(d, schema.ENGINE_CHOICE)
    json.dump({"engine": chosen, "why": "self-test: the only candidate",
               "candidates": [{"ref": chosen["ref"], "eligible": True, "why_not": ""}], "cross_check": {}}, open(cp, "w"))
    run(d, log=q, compute_kw={"max_workers": 2}, draws=2)
    pt = yaml.safe_load(open(os.path.join(d, "analysis", "point.yaml")))
    clean = contract.gate_findings(d) == [] and pt["engine_choice_sha256"] == schema.sha256_file(cp)
    doc = json.load(open(cp))
    doc["why"] = "edited"
    json.dump(doc, open(cp, "w"))
    st = [x for x in contract.gate_findings(d) if x["kind"] == "analysis_contract_stale"]
    ok(clean and st, f"R32 engine: auto runs on the harness's engine choice, and a changed choice makes the analysis "
                     f"stale ({len(st)} findings)")
    del math


def _direct_toy(d):
    """A panel of 6 units x 6 years under planted package rate ratios, and a spec for the package-rate engine."""
    import math
    import numpy as np
    import yaml
    rng = np.random.default_rng(4)
    rr = {"none": 1.0, "a": 0.6, "b": 0.7, "a+b": 0.5}
    with open(os.path.join(d, "units.csv"), "w") as f:
        f.write("id,zone,pop,elig\n")
        for i in range(6):
            f.write(f"u{i},z{i % 3},{10000 + 1000 * i},{i % 2}\n")
    with open(os.path.join(d, "history.csv"), "w") as f:
        f.write("unit,year,pop,package,cases\n")
        for i in range(6):
            base = 0.2 * (1 + 0.3 * i)
            for y in range(6):
                pkg = list(rr)[(i + y) % 4]
                mu = base * rr[pkg] * (10000 + 1000 * i)
                f.write(f"u{i},{2015 + y},{10000 + 1000 * i},{pkg},{rng.poisson(mu)}\n")
    spec = {
        "schema_version": 1, "question": "Which package per unit?", "decision_maker": "programme manager",
        "decision_date": 2026,
        "engine": {"ref": "toolkit.engines.empirical.engine:PackageRateEngine",
                   "config": {"components": ["a", "b"], "outcome": "cases"}},
        "horizon": {"start": 2015, "stop": 2024},
        "decision_units": {"csv": "units.csv", "id": "id",
                           "populations": {"all": {"column": "pop", "age_groups": ["all"]}}, "engine_attributes": {}},
        "options": {"populations": {"total": "pop"}, "components": {"a": {}, "b": {}}, "packages": {"generate": True},
                    "engine_map": {"a": {"a": 1}, "b": {"b": 1}},
                    "prices": {"base_year": "2024", "index": {"2024": 1.0}},
                    "cost_items": [{"id": "ca", "component": "a", "value": 1.0, "price_year": "2024",
                                    "price_rule": "as_published", "basis": "per_person", "includes": ["x"]},
                                   {"id": "cb", "component": "b", "value": 0.8, "price_year": "2024",
                                    "price_rule": "as_published", "basis": "per_person", "includes": ["y"]}]},
        "objective": {"outcome": "cases", "horizon": [2021, 2024], "discounting": "none"},
        "constraints": {"envelope_usd": 60000,
                        "group_rules": [{"id": "zone_a", "components": ["a"], "by": "zone", "rule": "same_choice"}]},
        "comparator": {"package": "none", "text": "no package"},
        "outcomes_reported": ["cases"],
        "calibration": {"method": "likelihood", "attribute": "log_rate", "scale": "linear", "range": [-8, 2],
                        "parameters": ["lrr:a", "lrr:b", "lrr:a+b"],
                        "targets": {"csv": "history.csv", "unit": "unit", "start": "year", "stop": "year_end",
                                    "package": "package", "observed": "cases", "exposure": "pop",
                                    "package_map": {}}},
        "uncertainty": {"draws": 8, "seed": 3, "sources": [{"kind": "calibration"}],
                        "excluded": [{"kind": "engine_parameters", "reason": "the engine has no fitted parameters"},
                                     {"kind": "costs", "reason": "toy"}, {"kind": "emulator", "reason": "direct"}]},
        "analysis": {"evaluation": "direct", "stability": {"holdout_group": "zone"},
                     "sensitivity": {"parameters": "fitted"},
                     "decision_tests": [{"id": "DT1", "type": "group_rule", "rule": "zone_a",
                                         "text": "Does the zone rule change the plan?"},
                                        {"id": "DT2", "type": "envelope", "values": [40000, 80000],
                                         "text": "Does a smaller or larger envelope change the plan?"}]},
        "deliverable": {"kind": "package_per_unit", "allocation_file": "allocation_optimized.csv",
                        "columns": {"unit": "unit", "package": "package", "value": "cases_averted"},
                        "headline": {"name": "total_cases_averted", "outcome": "cases"}},
    }
    # one-year periods: add the stop column
    rows = list(csv.DictReader(open(os.path.join(d, "history.csv"))))
    with open(os.path.join(d, "history.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]) + ["year_end"])
        w.writeheader()
        for r in rows:
            w.writerow(dict(r, year_end=int(r["year"]) + 1))
    del math
    with open(os.path.join(d, "decision_spec.yaml"), "w") as f:
        yaml.safe_dump(spec, f, sort_keys=False)


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--self-test"]:
        return _self_test()
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir")
    ap.add_argument("--spec", default="decision_spec.yaml")
    ap.add_argument("--adopt", nargs=3, metavar=("RESULTS", "JOBS", "DESIGN"), default=None,
                    help="adopt a stored batch computed before results carried provenance (verified, then re-run sample)")
    ap.add_argument("--shipped", metavar="DIR", default=None,
                    help="use a batch shipped from where it was computed (toolkit/analysis/batch.py ship/merge_shipped)")
    ap.add_argument("--draws", type=int, default=None)
    ap.add_argument("--unsigned", action="store_true", help="test runs only: every artifact records spec_signed false")
    a = ap.parse_args(argv)
    ad = dict(zip(("results", "jobs", "design"), a.adopt)) if a.adopt else None
    if a.shipped:
        if ad:
            ap.error("--shipped and --adopt are alternatives")
        ad = {"shipped": a.shipped}
    R = run(a.run_dir, a.spec, ad, a.draws, a.unsigned)
    if "estimates" in R:                  # deliverable kind estimates: its outputs (toolkit/analysis/estimates.py)
        print(json.dumps({"outputs": R["estimates"]["outputs"], "decision": R["estimates"]["decision"],
                          "label": R["uncertainty"]["label"], "wall_s": R["wall_s"]}, indent=1, default=float))
        return 0
    print(json.dumps({"point": {k: R["point"][k] for k in ("averted", "cost_allocated", "mix")},
                      "uncertainty": R["uncertainty"]["fixed_plan"], "label": R["uncertainty"]["label"],
                      "stability": {k: R["stability"].get(k) for k in ("verdict", "value_verdict")},
                      "wall_s": R["wall_s"]}, indent=1, default=float))
    return 0


if __name__ == "__main__":
    sys.exit(main())
