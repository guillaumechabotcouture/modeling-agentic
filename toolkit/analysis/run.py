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
from toolkit.analysis.artifacts import Store, plain, sha256  # noqa: E402
from toolkit.analysis.core import Analysis  # noqa: E402
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


def run(run_dir: str, spec_rel: str = "decision_spec.yaml", results: str | None = None, draws=None,
        unsigned: bool = False, log=print, compute_kw: dict | None = None) -> dict:
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
    S = Store(run_dir, spec.sha256, st == "signed", _engine_info(eng), spec.settings_sha256)
    card = eng.describe()
    arts = {}
    arts["engine"] = S.write("engine", {"card": card.to_dict(), "parameters": [vars(p) for p in eng.parameters()]},
                             PRODUCER, [spec_path])
    data_in = [spec.resolve(spec["decision_units"]["csv"])]
    if spec["calibration"].get("method") == "likelihood":
        data_in.append(spec.resolve(spec["calibration"]["targets"]["csv"]))
    # ---- outcome model
    if spec["analysis"]["evaluation"] == "emulator":
        design, jobs = Bt.design(spec)
        os.makedirs(os.path.join(run_dir, "analysis", "batch"), exist_ok=True)
        with open(os.path.join(run_dir, "analysis", "batch", "design.json"), "w") as f:
            json.dump(design, f, indent=1)
        if results:
            res = Bt.load_results(results, jobs)
            bsum = {"source": "stored results", "path": results, "sha256": sha256(results), "n": len(res)}
        else:
            b = Bt.run(run_dir, jobs, **(compute_kw or {}))
            res, bsum = b["results"], dict(b["summary"], path=os.path.relpath(b["path"], run_dir))
        arts["batch"] = S.write("batch", {"evaluation": "emulator", "points": len(design["points"]), "jobs": len(jobs),
                                          "packages": design["packages"], "fitted_names": design["fitted_names"],
                                          "summary": bsum}, Bt.PRODUCER,
                                [os.path.join(run_dir, "analysis", "batch", "design.json")] + data_in)
        model = EmulatorModel(spec, design, res, log=log).fit()
        arts["emulator"] = S.write("emulator", {"accuracy": model.accuracy, "fit_seconds": model.fit_seconds,
                                                "features": model.features}, "toolkit/analysis/model.py",
                                   [os.path.join(run_dir, "analysis", "batch", "design.json")])
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
    # ---- point and optimum
    out0, ben0, cost0 = A.evaluate(A.theta0, z0)
    sol = A.solve(ben0, cost0)
    plan = sol["choice"]
    point = A.summary(plan, out0, cost0)
    point.update(plan=plan, n_units=len(A.ids), solver={k: sol[k] for k in ("status", "method", "objective", "cost")},
                 interactions=interactions(A, out0), rule=decision_rule(A, out0, plan),
                 diagnostics={n: {"max": max(out0[i]["diag"][n] for i in A.ids),
                                  "median": sorted(out0[i]["diag"][n] for i in A.ids)[len(A.ids) // 2]}
                              for n in (spec["analysis"].get("diagnostics") or {})},
                 per_unit={i: {"package": plan[i], "benefit": ben0[i][plan[i]], "cost": cost0[i][plan[i]],
                               "averted": {o: out0[i]["y"][A.comp][o] - out0[i]["y"][plan[i]][o] for o in A.outcomes},
                               "z": z0[i]} for i in A.ids})
    arts["point"] = S.write("point", point, "toolkit/analysis/core.py", [arts["calibration"]])
    log(f"point: {point['averted'][A.primary]:,.0f} {A.primary} averted, cost {point['cost_allocated']:,.0f}, "
        f"mix {point['mix']}")
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
    # ---- uncertainty
    u = UQ.run(A, plan, d["headline"]["name"], draws=draws)
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
        {"test_id": x["test_id"], "path": x["path"], "verdict": "CHANGES_PLAN" if x["decision_changes"] else "NO_CHANGE",
         "can_move_plan": x["can_move_plan"], "share_pct": 100.0 * x["share_units_changed"], "statement": x["statement"]}
        for x in dts]}, DT.PRODUCER, [arts["point"], arts["uncertainty"]])
    for x in dts:
        S.deliverable(os.path.join(run_dir, x["path"]), DT.PRODUCER, "decision_test")
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
    S.save()
    return {"artifacts": arts, "point": point, "uncertainty": u["data"], "stability": stab, "sensitivity": sens,
            "decision_tests": dts, "calibration": cal, "emulator_accuracy": model.accuracy, "wall_s": S.manifest["wall_s"]}


# --------------------------------------------------------------------------- self-test
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
        p = schema.toy_spec(d, n=24)
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
        r1 = dt["results"][0]
        res = yaml.safe_load(open(os.path.join(d, dt["path"])))
        ok(abs(r1["increment_scale"] - math.log(0.7) / math.log(0.8)) < 1e-9 and res["produced_by"] == "harness"
           and isinstance(res["decision_changes"], bool) and res["recommendation"]["with"],
           f"R7 interaction rescale (the spike's DT1) writes a Phase 49 result file, produced_by harness")
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
        ok("total_dalys_averted" in rep["scalar_outputs"] and rep["interval_label"] == R["uncertainty"]["label"],
           "R10 uncertainty_report.yaml carries the headline output and the interval label")
        txt = open(os.path.join(d, "results.md")).read()
        ok("[CLAIM:" not in txt and R["uncertainty"]["label"] in txt, "R11 results.md is fully rendered from the ledger")
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
        # lesson 8: a test that only lowers unchosen benefits says so up front
        A = type("A", (), {})()
        ok(DT._lowers_only_unchosen({"u": {"p": 1.0, "q": 2.0}}, {"u": {"p": 0.5, "q": 2.0}}, {"u": "q"})
           and not DT._lowers_only_unchosen({"u": {"p": 1.0, "q": 2.0}}, {"u": {"p": 3.0, "q": 2.0}}, {"u": "q"}),
           "R15 lesson 8: lowering only unchosen packages' benefits is recognised as unable to move the plan")
    with tempfile.TemporaryDirectory() as d:
        # ---------------- direct path, likelihood calibration (a cheap engine fitted to a panel)
        _direct_toy(d)
        signoff.sign(d, "lead")
        R = run(d, log=q)
        th = R["calibration"]["theta"]
        ok(abs(th["lrr:a"] - math.log(0.6)) < 0.05 and abs(th["lrr:a+b"] - math.log(0.5)) < 0.05,
           f"R16 likelihood calibration recovers the planted package effects ({th})")
        ident = R["calibration"]["identifiability"]["parameters"]
        ok(all(v["status"] in ("well_identified", "weakly_identified", "unidentified") for v in ident.values())
           and ident["lrr:a"]["status"] == "well_identified", f"R17 identifiability from scripts/identifiability.py: "
           f"{ {k: v['status'] for k, v in ident.items()} }")
        inter = {(r["a"], r["b"]): r for r in R["point"]["interactions"]}
        ok(abs(inter[("a", "b")]["interaction_ratio"] - 0.5 / (0.6 * 0.7)) < 0.05,
           f"R18 the interaction ratio is computed from the point outcomes ({inter[('a', 'b')]['interaction_ratio']:.3f})")
        ok(R["stability"]["n_folds"] == 3 and R["uncertainty"]["covers"] == ["calibration"],
           "R19 likelihood path: refit-based stability and calibration-covariance uncertainty")
        gr = next(x for x in R["decision_tests"] if x["type"] == "group_rule")
        ok(gr["can_move_plan"] and gr["recommendation"]["with"], "R20 a group-rule decision test re-solves the plan")
        from toolkit.analysis import contract
        ok(contract.gate_findings(d) == [], f"R21 contract checks pass on the direct path: {contract.gate_findings(d)}")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


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
    ap.add_argument("--results", default=None, help="stored batch results (spike format or compute.py rows)")
    ap.add_argument("--draws", type=int, default=None)
    ap.add_argument("--unsigned", action="store_true", help="test runs only: every artifact records spec_signed false")
    a = ap.parse_args(argv)
    R = run(a.run_dir, a.spec, a.results, a.draws, a.unsigned)
    print(json.dumps({"point": {k: R["point"][k] for k in ("averted", "cost_allocated", "mix")},
                      "uncertainty": R["uncertainty"]["fixed_plan"], "label": R["uncertainty"]["label"],
                      "stability": {k: R["stability"].get(k) for k in ("verdict", "value_verdict")},
                      "wall_s": R["wall_s"]}, indent=1, default=float))
    return 0


if __name__ == "__main__":
    sys.exit(main())
