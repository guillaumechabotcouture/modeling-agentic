#!/usr/bin/env python3
"""The analysis engine (architecture-v2 §3.4): every number from the spec, the engine and the run's data.

    python3 toolkit/analysis/run.py <run_dir> [--spec decision_spec.yaml] [--draws N] [--unsigned]

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
    reserve_search visits budget fractions (a grid from the floor, then bisection) and keeps the best visited plan whose
    cost exceeds the allocatable budget in at most max_share_over_budget of those draws (not a proven optimum). No
    visited fraction meeting the rule: AnalysisBlocked (fail closed).
    Returns {solution, reserve (the point artifact's record), pre (the evaluated draws, for UQ.run)}."""
    rule = A.spec["uncertainty"]["affordability"]
    pre = UQ.evaluate_draws(A, draws, seed)
    fl = UQ.RESERVE_FLOOR if floor is None else float(floor)
    rs = UQ.reserve_search([r["cost"] for r in pre["rows"]], lambda b: A.solve(ben0, cost0, budget=b), A.budget,
                           float(rule["max_share_over_budget"]), floor=fl)
    st = rs["steps"]
    if not rs["feasible"]:
        raise AnalysisBlocked("affordability", [dict(s, kind="reserve_step") for s in st], reason=(
            f"no visited fraction meets the affordability rule (at most {rule['max_share_over_budget']} of "
            f"{pre['draws']} draws over budget): {len(st)} plans solved, at the full budget and at {rs['grid']} "
            f"fractions from the floor of {fl:g}; the floor candidate "
            + ("was proved infeasible by the solver" if st[1].get("infeasible") else
               f"is over budget in {st[1]['draws_over_budget']} draws")
            + ". This is not a proof that no plan within a reserve meets the rule"))
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
               "affordable": {"objective": float(sol["objective"]), "cost": plan_cost(sol), "plan": dict(sol["choice"]),
                              "draws_over_budget": rs["draws_over_budget"], "share_over_budget": rs["share_over_budget"]},
               "value_given_up": given, "value_given_up_pct": 100.0 * given / obj_u if obj_u > 0 else None,
               "search": {k: rs[k] for k in ("result", "floor_met", "grid", "tolerance", "floor", "max_solves", "solves",
                                             "converged", "steps")}}
    return {"solution": sol, "reserve": reserve, "pre": pre}


def _stop_where(e) -> str | None:
    """The step of a stop the toolkit declares besides AnalysisBlocked (a refused calibration, calibration input,
    burden anchor or engine batch; a solver defect), else None: any other error is a crash and is not recorded (once
    the run's store exists, no manifest is left; before that, nothing in the run dir is touched)."""
    from toolkit.alloc.optimize import OptimizerError
    from toolkit.analysis.burden import AnchorError
    from toolkit.calibration.history_matching import CalibrationInputError
    for cls, where in ((C.CalibrationRefused, "calibration"), (CalibrationInputError, "calibration"),
                       (AnchorError, "burden_anchor"), (OptimizerError, "optimizer"), (Bt.BatchRefused, "batch")):
        if isinstance(e, cls):
            return where
    return None


def run(run_dir: str, spec_rel: str = "decision_spec.yaml", draws=None,
        unsigned: bool = False, log=print, compute_kw: dict | None = None) -> dict:
    """The analysis. A step whose model corrections bear on a plan stops it: analysis/blocked.yaml records where and
    which, and the manifest is published with status blocked (never complete). A stop the toolkit declares by its
    own exception (`_stop_where`) is recorded the same way, with the exception's name and code, and raised again."""
    holder = {}
    try:
        return _run(run_dir, spec_rel, draws, unsigned, log, compute_kw, holder)
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
    except Exception as e:  # noqa: BLE001 -- recorded when it is a declared stop, and always raised again
        S, where = holder.get("S"), _stop_where(e)
        if S is not None and where is not None:
            try:
                S.write("blocked", {"reason": str(e)[:4000], "where": where, "corrections": [], "n_on_plan": 0,
                                    "exception": type(e).__name__, "code": getattr(e, "code", None),
                                    # only when the exception assessed it: absent is not "none at a bound"
                                    **({"at_bound": list(e.at_bound)} if getattr(e, "at_bound", None) is not None
                                       else {})}, PRODUCER,
                        [holder["calibration"]] if holder.get("calibration") else [])
                S.finalize("blocked", f"{where}: {type(e).__name__}: {e}"[:2000])
            except Exception as w:  # noqa: BLE001 -- the stop itself is what the caller must see
                try:
                    log(f"  the stop could not be recorded ({type(w).__name__}: {w}); no manifest is published")
                except Exception:  # noqa: BLE001 -- nor does a failing log replace it
                    pass
        raise


def _run(run_dir, spec_rel, draws, unsigned, log, compute_kw, holder) -> dict:
    t0 = time.time()
    compute_kw = dict(compute_kw or {})
    fan_out = compute_kw.pop("draw_fanout", None) is True       # only the draw fan-out reads it, never compute.py
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
    from toolkit.engines import hooks as _hooks_
    post = _hooks_.external_posterior_input(spec)        # uncertainty.net_effect_posterior: read once, its signed sha256 checked
    info = dict(_engine_info(eng), **({"net_effect_posterior_sha256": post["sha256"]} if post else {}))
    S = Store(run_dir, spec.sha256, st == "signed", info, spec.settings_sha256, spec.engine_choice_sha256)
    holder["S"] = S
    from toolkit.analysis import draw_fanout as DFO_
    from toolkit.analysis.artifacts import _safe_io as _sio
    _sio().remove_nofollow(run_dir, DFO_.REL)    # an earlier analysis's fan-out record never stands for this one
    from toolkit.engines.benchmark_sources import load_evidence
    stale = load_evidence(eng).problems          # S1-2b: never an analysis on evidence the snapshot does not back
    if stale:
        raise AnalysisBlocked("fitted_values", [], reason="; ".join(stale))
    ne = _hooks_.external_posterior_sample(spec, eng)   # f6-posterior-ranges: loaded, verified; its hull sets its columns' ranges
    if ne is not None:                      # before any artifact: the manifest and every artifact record where from
        info["net_effect_posterior_ranges"] = _hooks_.external_posterior_range_record(ne)
        # v2-posterior-point: each posterior column's point value is the fit's verified chosen point
        info["point_source"], info["posterior_point"] = _hooks_.external_posterior_point_source(ne), dict(ne["point"])
    card = eng.describe()
    arts = {}
    # Codex r2 P1-7: the files the engine reads (benchmark results, fitted values) are inputs of what uses them
    from toolkit.spec import priors as PRI
    eng_files = [f for f in (getattr(eng, "results_path", None), getattr(eng, "fitted_path", None),
                             PRI.priors_path(eng)) if f and os.path.exists(f)] + ([(post["path"], post["sha256"])] if post else [])
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
        from toolkit.analysis import row_reuse as RR     # F6 run 2: stored rows by job_sha, engine, adapter
        b = RR.run(run_dir, jobs, eng, "analysis/batch/results.jsonl", log=log, **kw)
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
                                [os.path.join(run_dir, "analysis", "batch", "design.json"), batch_rows] + data_in)
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
    # direct evaluation: the uncertainty draws' engine runs fan out over the compute workers when the run has them
    # (MA_COMPUTE_SCHEDULER) or the caller asks (draw_fanout: true); toolkit/analysis/draw_fanout.py
    A.compute = dict(compute_kw or {}, run_dir=run_dir, **({"draw_fanout": True} if fan_out else {}))
    if spec["analysis"].get("workers"):         # the same precedence as the other compute stages
        A.compute.setdefault("max_workers", int(spec["analysis"]["workers"]))
    from toolkit.analysis import draw_fanout as DFO
    if jc is not None:
        A.set_joint(jc)
        A.flagged = {f["unit"]: f for f in jc["unmatched_decision"]["flagged"]}
        from toolkit.analysis import conflicts as CF
        A.conflict_tests = CF.tests(jc["unmatched_decision"].get("conflicts"))
    from toolkit.analysis import variants as V
    V.apply_headline(A)     # F6 S1-6: `headline: <variant>`: every solve from here carries the variant's rule
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
    from toolkit.analysis import burden as BV
    anc = BV.anchor_step(A, out0, A.last_events) if BV.section(spec.raw) is not None else None
    if anc is not None:     # F6 S2-5d: absolute burden at the external level; every later evaluation is anchored
        out0, ben0, cost0 = A.evaluate(A.theta0, z0)
        arts["burden_anchor"] = S.write("burden_anchor", anc, BV.PRODUCER, [arts["calibration"], BV.csv_path(spec)])
        log("burden anchor: " + ", ".join(f"{r['id']} x{r['factor']:.3g}" for r in anc["rows"]))
    events0 = list(A.last_events)
    A.point_events = events0       # the point tables' corrections, which decision tests on them guard (#246)
    from toolkit.alloc import costs as acosts
    floors0 = acosts.floor_report(A.last_cost_floors, A.alloc)     # F6 S1-3: reported, never read as a real cost
    sol = A.solve(ben0, cost0)
    reserve = pre = None
    if (spec["uncertainty"].get("affordability") or {}).get("enforce"):
        # F6 S1-1b: the headline plan is the one solved within the reserve that meets the affordability rule
        ap = affordable_plan(A, ben0, cost0, sol, draws=draws)
        sol, reserve, pre = ap["solution"], ap["reserve"], ap["pre"]
        A.set_reserve(reserve["fraction"])
        log(f"affordability: plan within {reserve['fraction']:.4f} of the budget ({reserve['reserve_usd']:,.0f} held "
            f"back), {reserve['value_given_up_pct'] or 0:.2f}% of the objective given up")
    plan = plan_pt = sol["choice"]
    A.guard("point plan", [plan] + ([reserve["unconstrained"]["plan"]] if reserve else []), events0)  # Codex P1-6
    from toolkit.analysis import plan_rule as PR
    evp = None
    if PR.rule(spec.raw) == "expected_value":
        # hb1 fix 11: the plan with the best mean over the uncertainty draws, which the uncertainty step then reuses
        pre = UQ.evaluate_draws(A, draws)
        evp = PR.expected_value_plan(A, pre["rows"], plan_pt, cost0, point_events=events0)
        plan = evp["solution"]["choice"]
        A.guard("expected-value plan", [plan], events0)
        log(f"expected-value plan: {evp['record']['n_units_differ']} of {len(A.ids)} units differ from the point plan")
    on_plan = []
    point = A.summary(plan, out0, cost0)
    from toolkit.engines.identity import engine_identity
    # Codex r3: the analysis's full engine identity (code, params, config), on both evaluation paths, so later steps
    # (the trajectory step) can be checked against it without batch rows
    point.update(engine_identity=engine_identity(eng))
    point.update(plan=plan, n_units=len(A.ids),
                 # 1C-c (D3): the status and the relative gap it was proven within, with the threads and time limit
                 solver=_solver_record(evp["solution"] if evp else sol),
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
    point["cost_floors"] = floors0
    if evp is not None:      # hb1 fix 11: the rule, the point-value plan and its solve, and each unit's mean
        point["plan_rule"] = dict(evp["record"], point_solver=_solver_record(sol))
        for i in A.ids:
            point["per_unit"][i]["averted_mean"] = evp["record"]["per_unit_mean"][i]
    if getattr(A, "adjust", None):    # F6 run 4 R3: the base outcome adjustments every evaluation applied
        from toolkit.analysis import outcome_adjust as OA
        point["outcome_adjustments"] = OA.record(A.adjust)
    if reserve is not None:
        point["reserve"] = reserve
    if getattr(A, "headline_variant", None):     # F6 S1-6: the headline plan is this variant's
        point["headline_variant"] = A.headline_variant
    arts["point"] = S.write("point", point, "toolkit/analysis/core.py", [arts["calibration"]]
                            + ([arts["burden_anchor"]] if anc else []))
    bnd = None
    if model.kind == "emulator":      # F6 S2-4b: the emulator's order near package switches, against engine runs
        from toolkit.analysis import boundary as BD
        bnd = BD.run(A, plan, z0, out0, ben0, cost0, design["horizon"], run_dir, compute_kw=kw, log=log)
        arts["emulator_boundary"] = S.write("emulator_boundary", bnd, BD.PRODUCER, [arts["point"]] + (
            [os.path.join(run_dir, BD.OUT_REL)] if bnd["units"] else []))
    log(f"point: {point['averted'][A.primary]:,.0f} {A.primary} averted, cost {point['cost_allocated']:,.0f}, "
        f"mix {point['mix']}")
    # ---- uncertainty
    bcol = BV.Collector(A) if BV.section(spec.raw) is not None else None      # F6 S2-5: references' values per draw
    bcol = bcol if bcol is None or bcol.refs else None          # S2-5d: anchor rows alone validate nothing
    # F6 S2-6: each fixed plan's outcome and cost in every draw; each solved variant's too (its cost against the budget)
    fcol = V.Draws(A, V.solved_plans(A, ben0, cost0)) if V.config(spec.raw).get("variants") else None
    from toolkit.spec import cea as CEASPEC
    from toolkit.analysis import cea as CEA
    ccol = CEA.Collector(A) if CEASPEC.has_cea(spec.raw) else None
    hooks = [h.add for h in (bcol, fcol) if h is not None]
    from toolkit.analysis import value_of_information as VOI
    nd_ = UQ._n_seed(A, draws)[0]
    vcol = VOI.Collector(A) if VOI.expected(spec.raw) and nd_ >= VOI.thresholds()["min_draws"] else None

    def _on_draw(out, ev, cost, sampled=None):
        for h in hooks:
            h(out, ev, cost)
        if ccol is not None:
            ccol.add(out, ev, cost, sampled)

    u = UQ.run(A, plan, spec["deliverable"]["headline"]["name"], draws=draws, pre=pre,
               on_draw=_on_draw if (hooks or ccol is not None) else None,
               on_solve=vcol.add if vcol is not None else None)
    arts["uncertainty"] = S.write("uncertainty", u["data"], UQ.PRODUCER, [arts["point"]] + eng_files)
    S.deliverable(UQ.write_report(run_dir, u["report"]), UQ.PRODUCER, "uncertainty_report")
    log(f"UQ: {u['data']['fixed_plan'][A.primary]} ({u['data']['label']})")
    cea_res = None
    if ccol is not None:
        cea_res = CEA.run_cea(A, out0, cost0, collector=ccol, plan=plan,
                              uq_level=u["data"]["interval"].get("level", 0.95))
        cea_in = [arts["point"], arts["uncertainty"]]
        lt_p = ((spec.raw.get("cost_effectiveness") or {}).get("health_measure") or {}).get("life_table")
        lt_csv = lt_p.get("csv") if isinstance(lt_p, dict) else (lt_p if isinstance(lt_p, str) else None)
        if isinstance(lt_csv, str) and lt_csv:
            cea_in.append(spec.resolve(lt_csv))
        arts["cost_effectiveness"] = S.write("cost_effectiveness", cea_res, CEA.PRODUCER, cea_in)
        log(f"cost_effectiveness: perspectives={list(cea_res['perspectives'])}")
    voi = None
    if VOI.expected(spec.raw):        # F6: which drawn input is worth better data, from the same draws
        voi = VOI.run(A, vcol, plan, dict(fcol.plans) if fcol is not None else None, u["report"],
                      level=u["data"]["interval"].get("level", 0.95), seed=u["data"]["seed"]) if vcol is not None \
            else VOI.too_few(nd_, u["report"], A)
        arts["value_of_information"] = S.write("value_of_information", voi, VOI.PRODUCER, [arts["uncertainty"]])
        log(f"value of information: {voi['status']}, EVPI {(voi['evpi'] or {}).get('value')}")
    bvd = None
    if bcol is not None:        # F6 S2-5: the continuation arm's burden against external estimates
        bvd = BV.run(A, out0, z0, point, bcol, level=u["data"]["interval"].get("level", 0.95), events0=events0)
        arts["burden_validation"] = S.write("burden_validation", bvd, BV.PRODUCER,
                                            [arts["point"], arts["uncertainty"], BV.csv_path(spec)])
        log(f"burden validation: {bvd['summary']}")
    # ---- stability
    stab = ST.run(A, z0, out0, ben0, cost0, plan_pt)    # re-solved at the point values: the point-value plan
    arts["stability"] = S.write("stability", stab, ST.PRODUCER, [arts["point"], arts["calibration"]])
    if stab.get("folds"):
        import yaml
        rp = os.path.join(run_dir, "models", "allocation_robustness.yaml")
        from toolkit.analysis.artifacts import _safe_io
        _safe_io().write_nofollow(run_dir, "models/allocation_robustness.yaml",
                                  yaml.safe_dump(plain(ST.robustness_yaml(stab)), sort_keys=False).encode(), atomic=True)
        S.deliverable(rp, ST.PRODUCER, "allocation_robustness")
    # ---- plan variants (F6 S1-6), on the point tables within the headline plan's budget
    pv = V.run(A, ben0, cost0, plan, events0, fcol, u["data"]["interval"], out0)   # guarded by the point corrections
    if pv is not None:
        arts["plan_variants"] = S.write("plan_variants", pv, V.PRODUCER, [arts["point"]])
    # ---- sensitivity
    sens = SE.run(A, z0, ben0, cost0, plan_pt, sol["objective"])
    arts["sensitivity"] = S.write("sensitivity", sens, SE.PRODUCER, [arts["point"]])
    # ---- decision tests
    dts = DT.run_all(A, z0, out0, ben0, cost0, plan_pt, u["data"], run_dir)
    if jc is not None:      # owner decision R1 (F6 run 1): a declared calibration miss stands only with its test run
        from toolkit.analysis import joint_calibration as JC
        dp = JC.declared_test_problems(jc["unmatched_decision"], dts)
        if dp:
            raise AnalysisBlocked("calibration", [{"unit": "*", "package": "*", "kind": "declared_miss_untested"}],
                                  reason="; ".join(dp))
    # hb1 fix 11: under deliverable.plan: expected_value each test re-solves at the point values and compares with
    # the point-value plan, which the artifact names (the owner finding says so: contract.decision_test_findings)
    arts["decision_tests"] = S.write("decision_tests", {**({"reference_plan": "point_plan"} if evp is not None else {}),
                                                        "tests": [
        {"test_id": x["test_id"], "path": x["path"], "status": x["status"],
         "verdict": ("NOT_RUN" if x["status"] == "not_run" else "CHANGES_PLAN" if x["decision_changes"] else "NO_CHANGE"),
         # 1C-c follow-up: the verdict words are unchanged (tools/run_console reads exactly these
         # three); a verdict that cannot be settled within the solver's accepted gap is marked beside it
         "within_solver_gap": bool(x.get("within_solver_gap")),
         "can_move_plan": x["can_move_plan"], "threshold_pct": 100.0 * x["share_threshold"],
         "share_pct": None if x["share_units_changed"] is None else 100.0 * x["share_units_changed"],
         # F6 S1-5: what moves beside the share, and which criterion changed the plan
         **{k: (None if x.get(f) is None else 100.0 * x[f]) for k, f in (
             ("regret_pct", "regret_share"), ("regret_threshold_pct", "regret_threshold"),
             ("dollars_moved_pct", "dollars_moved_share"), ("dollars_threshold_pct", "dollars_threshold"),
             ("dollars_reallocated_pct", "dollars_reallocated_share"), ("dollars_share_pct", "dollars_share"),
             ("population_changed_pct", "population_changed_share"))},
         **{k: x.get(k) for k in ("regret", "objective_gap", "point_plan_feasible", "dollars_moved",
                                  "dollars_reallocated", "population_changed", "triggered_by",
                                  "units_changed_by_group", "dollars_by_component")},
         "statement": x["statement"], "detail": {k: x[k] for k in ("engine_arm_or", "engine_interaction",
                                                    "rejected_values", "reachable_interaction", "unit_attribute",
                                                    "burden_scale", "outcome_ratio", "with")
                                    if k in x}} for x in dts]}, DT.PRODUCER,
        [arts["point"], arts["uncertainty"]] + eng_files
        + sorted({os.path.join(run_dir, f) for x in dts for f in (x.get("arm_inputs") or {})})
        # F6: a unit_attribute test's set_from table is an input (each modified table's sha256 is in its record)
        + DT.artifact_inputs(dts, run_dir))
    for x in dts:
        if x["path"]:
            S.deliverable(os.path.join(run_dir, x["path"]), DT.PRODUCER, "decision_test")
    conf = (jc or {}).get("unmatched_decision", {}).get("conflicts") if jc is not None else None
    if conf is not None:      # W0-F: each held-out target, why, its tests' verdicts; who decides (empty: none)
        from toolkit.analysis import conflicts as CF
        crec = CF.record(conf, dts, refit=CF.refit_estimate(jc))
        arts["conflicts"] = S.write("conflicts", crec, CF.PRODUCER, [arts["joint_calibration"], arts["decision_tests"]])
        log(f"conflicts: {len(crec['declared'])} target(s) held out and tested; {len(crec['needs_owner'])} need the owner")
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
        # plan's quantity in that group (a cost output) and the outcome averted in it (mean over draws under
        # deliverable.plan: expected_value)
        gc = bg["columns"]
        w.writerow([gc["group"], gc["quantity"], gc["value"], cols["unit"], cols["package"]])
        pgm = (evp["record"].get("per_unit_group_mean") or {}) if evp is not None else None
        pgq = (evp["record"].get("per_unit_group_quantity") or {}) if evp is not None else None
        for i in A.ids:
            yg = out0[i]["y_group"]
            for g, qname in bg["quantity"].items():
                qv = float(pgq[i][g]) if pgq is not None else float(out0[i]["cost"][plan[i]][qname])
                vv = float(pgm[i][g][hl_o]) if pgm is not None else (
                    yg[A.comp][hl_o].get(g, 0.0) - yg[plan[i]][hl_o].get(g, 0.0))
                w.writerow([g, round(qv, 4), round(vv, 4), i, plan[i]])
    else:
        fl = getattr(A, "flagged", {}) or {}
        w.writerow([cols["unit"], cols["package"], cols["value"], "cost_usd", "region"]
                   + (["calibration_flag"] if fl else []))
        for i in A.ids:
            val = point["per_unit"][i]["averted_mean" if evp is not None else "averted"][hl_o]   # hb1 fix 11
            w.writerow([i, plan[i], round(val, 4), round(cost0[i][plan[i]], 2),
                        A.by_id[i].get(reg, "") if reg else ""] + ([fl[i]["reason"] if i in fl else ""] if fl else []))
    from toolkit.analysis.artifacts import _safe_io
    _safe_io().publish(dp, buf.getvalue(), run_dir)
    S.deliverable(dp, PRODUCER, "allocation")
    # ---- figures: the decision numbers they need (no engine runs), then every figure from the saved artifacts
    # direct evaluation: the uncertainty step's per-draw plan sums are stored and read, not recomputed by re-running the
    # engine for every draw (hb1 flu R2); the emulator path replays its cheap draws as before
    fd_stored, fd_in = None, []
    if model.kind == "direct" and u["data"].get("draws"):
        from toolkit.analysis.artifacts import _safe_io
        import hashlib
        raw = json.dumps(plain(FD.stored_record(u["plan_draws"]))).encode()
        fd_stored = json.loads(raw)
        _safe_io().write_nofollow(run_dir, FD.STORED_REL, raw, atomic=True)
        # Codex r1 P1-2: the hash of the bytes the figure step reads, not of whatever the file holds when recorded
        fd_in = [(os.path.join(run_dir, FD.STORED_REL), hashlib.sha256(raw).hexdigest())]
    fdat = FD.compute(A, plan, out0, ben0, cost0, u["data"], z0, log=log, events=events0, stored=fd_stored)
    if getattr(A, "draw_fanout", None):         # the smoke marker: the draws fanned out (backend, tasks, workers)
        from toolkit.analysis.artifacts import _safe_io
        _safe_io().write_nofollow(run_dir, DFO.REL, json.dumps(plain(A.draw_fanout), indent=1).encode(), atomic=True)
    arts["figure_data"] = S.write("figure_data", fdat, FD.PRODUCER, [arts["point"], arts["uncertainty"]] + fd_in)
    rend = FG.render(run_dir)
    figs = rend["figures"]
    arts["figures"] = S.write("figures", {"figures": figs, "not_produced": rend["not_produced"],
                                          "tiers": rend["tiers"], "groups": rend["groups"],
                                          "overviews": rend.get("overviews") or {}}, FG.PRODUCER,
                              sorted({s for f in figs for s in f["sources"]}))
    for fgd in figs:
        S.deliverable(os.path.join(run_dir, fgd["path"]), FG.PRODUCER, "figure")
    # ---- claims and results.md
    c = CL.write(A, run_dir, arts, point, u["data"], stab, sens, dts, cal,
                 dict(model.accuracy, **({"boundary_ranking": bnd} if bnd else {})), pv=pv, burden=bvd, anchor=anc,
                 voi=voi, cea=cea_res)
    arts["claims"] = S.write("claims", c, CL.PRODUCER, [arts[k] for k in ("point", "uncertainty", "stability",
                                                                          "decision_tests", "calibration")]
                             + ([arts["plan_variants"]] if pv is not None else [])
                             + ([arts["value_of_information"]] if voi is not None else [])
                             + ([arts["cost_effectiveness"]] if cea_res is not None else [])
                             + ([arts["burden_validation"]] if bvd else [])
                             + ([arts["burden_anchor"]] if anc else []))
    for rel, role in ((c["spec"], "claims_spec"), (c["ledger"], "claims_ledger"), (c["results"], "results")):
        S.deliverable(os.path.join(run_dir, rel), CL.PRODUCER, role)
    S.manifest["wall_s"] = round(time.time() - t0, 1)
    S.manifest["solver"] = A.solver_report()       # 1C-c: settings, and the counts contract.py's gate check reads
    S.manifest["expected"] = {"kinds": list(EXPECTED_KINDS)
                              + (["emulator", "emulator_boundary"] if model.kind == "emulator" else [])
                              + (["burden_validation"] if bvd else []) + (["burden_anchor"] if anc else [])
                              + (["value_of_information"] if voi is not None else [])
                              + (["cost_effectiveness"] if cea_res is not None else []),
                              "roles": list(EXPECTED_ROLES),
                              "decision_tests": {x["test_id"]: x["path"] for x in dts}}
    S.finalize("complete")
    return {"artifacts": arts, "point": point, "uncertainty": u["data"], "stability": stab, "sensitivity": sens,
            "decision_tests": dts, "calibration": cal, "emulator_accuracy": model.accuracy, "wall_s": S.manifest["wall_s"],
            "_A": A, "_model": model}


def _solver_record(sol: dict) -> dict:
    """point.solver: the status and the relative gap it was proven within, with the threads and time limit (1C-c)."""
    return {**{k: sol[k] for k in ("status", "method", "objective", "cost")},
            **{k: sol.get(k) for k in ("gap_rel", "threads", "time_limit_s", "solver_status")}}


# --------------------------------------------------------------------------- self-test
def _toy_pack() -> dict:
    """The toy engine's benchmark pack: T2 is its interaction benchmark."""
    return {"engine": "toolkit.engines.contract:ToyEngine", "seeds": [1],
            "horizon": {"start": 0, "stop": 3, "window": [0, 3], "windows": {"w": [1, 2]}},
            "settings": {"s": {"unit": {"x": 0.0}}},
            "arms": {"none": {"setting": "s", "package": {}}, "a": {"setting": "s", "package": {"a": 1}},
                     "b": {"setting": "s", "package": {"b": 1}}, "ab": {"setting": "s", "package": {"a": 1, "b": 1}}},
            "benchmarks": [{"id": "T2", "metric": "interaction", "target": 1.0, "band": [0.8, 1.25],
                            "band_override": {"band": [0.8, 1.25], "level": 0.95, "reason": "self-test toy: no published source"},
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
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir")
    ap.add_argument("--spec", default="decision_spec.yaml")
    ap.add_argument("--draws", type=int, default=None)
    ap.add_argument("--unsigned", action="store_true", help="test runs only: every artifact records spec_signed false")
    a = ap.parse_args(argv)
    R = run(a.run_dir, a.spec, a.draws, a.unsigned)
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
