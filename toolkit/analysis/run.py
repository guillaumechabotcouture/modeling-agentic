#!/usr/bin/env python3
"""The analysis engine (architecture-v2 §3.4): every number from the spec, the engine and the run's data.

    python3 toolkit/analysis/run.py <run_dir> [--spec decision_spec.yaml] [--results <batch results.jsonl>]
                                    [--draws N] [--unsigned]
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
            S.write("blocked", {"reason": f"model corrections bear on a plan at: {e.where}", "where": e.where,
                                "corrections": e.hits[:200], "n_on_plan": len(e.hits)}, PRODUCER,
                    [holder["calibration"]] if holder.get("calibration") else [])
            S.finalize("blocked", f"{e.where}: {len(e.hits)} model corrections bear on the plan")
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
    eng_files = [f for f in (getattr(eng, "results_path", None), getattr(eng, "fitted_path", None))
                 if f and os.path.exists(f)]
    arts["engine"] = S.write("engine", {"card": card.to_dict(), "parameters": [vars(p) for p in eng.parameters()]},
                             PRODUCER, [spec_path] + eng_files)
    data_in = [spec.resolve(spec["decision_units"]["csv"])]
    if spec["calibration"].get("method") == "likelihood":
        data_in.append(spec.resolve(spec["calibration"]["targets"]["csv"]))
    # ---- outcome model
    if spec["analysis"]["evaluation"] == "emulator":
        design, jobs = Bt.design(spec)
        os.makedirs(os.path.join(run_dir, "analysis", "batch"), exist_ok=True)
        with open(os.path.join(run_dir, "analysis", "batch", "design.json"), "w") as f:
            json.dump(design, f, indent=1)
        kw = dict(compute_kw or {})
        if spec["analysis"].get("workers"):
            kw.setdefault("max_workers", int(spec["analysis"]["workers"]))
        if adopt:
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
                                          "summary": bsum}, Bt.PRODUCER,
                                [os.path.join(run_dir, "analysis", "batch", "design.json"), batch_rows] + data_in
                                + ([adopt["results"], adopt["jobs"], adopt["design"]] if adopt else []))
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
    # ---- calibration
    A.fit_calibration()
    cal0 = A.calibrate(A.theta0)
    z0 = {i: v[0] for i, v in cal0.items()}
    if A.method == "emulator_inversion":
        cal = C.inversion_report(spec, model, A.theta0, cal0)
    elif A.method == "likelihood":
        cal = {k: A.lik[k] for k in ("method", "diagnostics", "identifiability", "theta", "names", "cov")}
        cal["units"] = {i: {"z": z0[i]} for i in A.ids}
    else:
        cal = {"method": "none", "units": {}, "diagnostics": {}, "identifiability": {"status": "not_applicable"}}
    arts["calibration"] = S.write("calibration", cal, C.PRODUCER, data_in)
    holder["calibration"] = arts["calibration"]
    # ---- point and optimum
    out0, ben0, cost0 = A.evaluate(A.theta0, z0)
    events0 = list(A.last_events)
    sol = A.solve(ben0, cost0)
    plan = sol["choice"]
    A.guard("point plan", [plan], events0)   # Codex P1-6; every later evaluation is guarded too (r2 P1-3)
    on_plan = []
    point = A.summary(plan, out0, cost0)
    point.update(plan=plan, n_units=len(A.ids), solver={k: sol[k] for k in ("status", "method", "objective", "cost")},
                 interactions=interactions(A, out0), rule=decision_rule(A, out0, plan),
                 model_corrections={"n": len(events0), "on_plan": on_plan, "kinds": sorted({e["kind"] for e in events0}),
                                    "examples": events0[:20]},
                 diagnostics={n: {"max": max(out0[i]["diag"][n] for i in A.ids),
                                  "median": sorted(out0[i]["diag"][n] for i in A.ids)[len(A.ids) // 2]}
                              for n in (spec["analysis"].get("diagnostics") or {})},
                 per_unit={i: {"package": plan[i], "benefit": ben0[i][plan[i]], "cost": cost0[i][plan[i]],
                               "averted": {o: out0[i]["y"][A.comp][o] - out0[i]["y"][plan[i]][o] for o in A.outcomes},
                               "z": z0[i]} for i in A.ids})
    arts["point"] = S.write("point", point, "toolkit/analysis/core.py", [arts["calibration"]])
    log(f"point: {point['averted'][A.primary]:,.0f} {A.primary} averted, cost {point['cost_allocated']:,.0f}, "
        f"mix {point['mix']}")
    # ---- uncertainty
    u = UQ.run(A, plan, spec["deliverable"]["headline"]["name"], draws=draws)
    arts["uncertainty"] = S.write("uncertainty", u["data"], UQ.PRODUCER, [arts["point"]])
    S.deliverable(UQ.write_report(run_dir, u["report"]), UQ.PRODUCER, "uncertainty_report")
    log(f"UQ: {u['data']['fixed_plan'][A.primary]} ({u['data']['label']})")
    # ---- stability
    stab = ST.run(A, z0, out0, ben0, cost0, plan)
    arts["stability"] = S.write("stability", stab, ST.PRODUCER, [arts["point"], arts["calibration"]])
    if stab.get("folds"):
        import yaml
        rp = os.path.join(run_dir, "models", "allocation_robustness.yaml")
        os.makedirs(os.path.dirname(rp), exist_ok=True)
        with open(rp, "w") as f:
            yaml.safe_dump(plain(ST.robustness_yaml(stab)), f, sort_keys=False)
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
        [arts["point"], arts["uncertainty"]] + eng_files)
    for x in dts:
        if x["path"]:
            S.deliverable(os.path.join(run_dir, x["path"]), DT.PRODUCER, "decision_test")
    # ---- deliverable CSV
    d = spec["deliverable"]
    cols = d["columns"]
    reg = spec["decision_units"].get("region")
    dp = os.path.join(run_dir, d["allocation_file"])
    with open(dp, "w", newline="") as f:
        w = csv.writer(f)
        hl_o = d["headline"]["outcome"]
        w.writerow([cols["unit"], cols["package"], cols["value"], "cost_usd", "region"])
        for i in A.ids:
            w.writerow([i, plan[i], round(point["per_unit"][i]["averted"][hl_o], 4), round(cost0[i][plan[i]], 2),
                        A.by_id[i].get(reg, "") if reg else ""])
    S.deliverable(dp, PRODUCER, "allocation")
    # ---- figures
    figs = FG.draw(run_dir, point, u["data"], sens, cal, A.primary, arts)
    arts["figures"] = S.write("figures", {"figures": figs}, FG.PRODUCER,
                              [arts["point"], arts["uncertainty"], arts["sensitivity"], arts["calibration"]])
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
def _toy_bench(d):
    """Stamped benchmark results for the toy engine (toolkit/engines/benchmark.py): T2 is its interaction benchmark."""
    import yaml
    from toolkit.engines import benchmark as BM
    from toolkit.engines.contract import ToyEngine
    path = os.path.join(d, "toy_bench.yaml")
    eng = ToyEngine(results_path=path)
    pack = {"engine": "toolkit.engines.contract:ToyEngine", "seeds": [1],
            "horizon": {"start": 0, "stop": 3, "window": [0, 3], "windows": {"w": [1, 2]}},
            "settings": {"s": {"unit": {"x": 0.0}}},
            "arms": {"none": {"setting": "s", "package": {}}, "a": {"setting": "s", "package": {"a": 1}},
                     "b": {"setting": "s", "package": {"b": 1}}, "ab": {"setting": "s", "package": {"a": 1, "b": 1}}},
            "benchmarks": [{"id": "T2", "metric": "interaction", "target": 1.0, "band": [0.8, 1.25],
                            "estimand": {"type": "interaction_or", "a11": "ab", "a10": "a", "a01": "b", "a00": "none",
                                         "value": "windows.w.prev"}}]}
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
        fp = R["uncertainty"]["fixed_plan"]["dalys"]
        ok(fp["ci_low"] <= fp["median"] <= fp["ci_high"] and "not covered" in R["uncertainty"]["label"],
           f"R5 the interval is computed and labelled with what it covers ({R['uncertainty']['label']})")
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
    with tempfile.TemporaryDirectory() as d:
        # ---------------- direct path, likelihood calibration (a cheap engine fitted to a panel)
        _direct_toy(d)
        signoff.sign(d, "lead")
        R = run(d, log=q)
        th = R["calibration"]["theta"]
        ok(abs(th["lrr:a"] - math.log(0.6)) < 0.05 and abs(th["lrr:a+b"] - math.log(0.5)) < 0.05,
           f"D1 likelihood calibration recovers the planted package effects ({th})")
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
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


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
    ap.add_argument("--draws", type=int, default=None)
    ap.add_argument("--unsigned", action="store_true", help="test runs only: every artifact records spec_signed false")
    a = ap.parse_args(argv)
    ad = dict(zip(("results", "jobs", "design"), a.adopt)) if a.adopt else None
    R = run(a.run_dir, a.spec, ad, a.draws, a.unsigned)
    print(json.dumps({"point": {k: R["point"][k] for k in ("averted", "cost_allocated", "mix")},
                      "uncertainty": R["uncertainty"]["fixed_plan"], "label": R["uncertainty"]["label"],
                      "stability": {k: R["stability"].get(k) for k in ("verdict", "value_verdict")},
                      "wall_s": R["wall_s"]}, indent=1, default=float))
    return 0


if __name__ == "__main__":
    sys.exit(main())
