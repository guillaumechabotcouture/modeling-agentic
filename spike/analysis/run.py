#!/usr/bin/env python3
"""Analysis engine (architecture-v2 §3.4): every number from the spec, the engine batch and
harness code. Engine-agnostic: disease specifics live in the engine and the spec.

    python3 spike/analysis/run.py <decision_spec.yaml> <out_dir> [--results <results.jsonl>] [--draws N]
    python3 spike/analysis/run.py --self-test        # the whole chain on the toy engine

Steps:
 1. emulator: Gaussian processes over the batch, with held-out accuracy;
 2. calibration: per unit, the calibrated attribute that reproduces the unit's target
    (emulator inversion on a grid; clamped units are counted, not hidden);
 3. point outcomes and benefits (comparator minus package) for every unit and package;
    costs from toolkit/alloc (per-outcome items use emulated outputs);
 4. optimisation with toolkit/alloc (ILP, proven optimum);
 5. uncertainty: draws of the fitted engine parameters within their benchmark ranges and
    of a per-region common-mode calibration-target error; the point plan is evaluated in
    every draw (headline interval) and re-optimised (package stability);
 6. leave-one-group-out allocation stability (the held-out group's calibration targets
    are replaced by a covariate regression fitted on the other units);
 7. decision tests (interaction_rescale): rescale the increment of a component given
    another so the benchmark-setting interaction equals the trial value, and re-solve.
Writes <out_dir>/analysis.json, allocation.csv and tables.md.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
import time

import numpy as np
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from spike.analysis import spec as S, batch as B  # noqa: E402
from spike.analysis.emulator import Emulator, load as load_batch, _logit  # noqa: E402
from toolkit.alloc import costs as acosts, packages as apackages, optimize as aopt  # noqa: E402


class Analysis:
    def __init__(self, spec, out_dir, results_path=None, log=print):
        self.spec, self.out, self.log = spec, out_dir, log
        self.design, res = load_batch(out_dir, results_path)
        self.units = S.read_units(spec)
        self.uid = spec["decision_units"]["id"]
        self.by_id = {str(u[self.uid]): u for u in self.units}
        self.ids = [str(u[self.uid]) for u in self.units]
        self.em = Emulator(spec, self.design, res, self.by_id)
        self.em.fit(log)
        self.eng = S.load_engine(spec["engine"])
        self.fr = B.fitted_ranges(self.eng)
        self.theta0 = {k: v[2] for k, v in self.fr.items()}
        self.opts = spec["options"]
        self.pk = acosts.packages(self.opts)
        self.comp = spec["comparator"]["package"]
        self.feas = apackages.feasibility(self.opts, self.units)
        c = spec["constraints"]
        self.budget = float(c["envelope_usd"]) - sum((c.get("fixed_costs_usd") or {}).values())
        self.pops = spec["decision_units"]["populations"]
        lo, hi = spec["calibration"]["log_range"]
        self.zgrid = np.linspace(lo, hi, 161)
        self.region = spec["decision_units"].get("region")

    # ------------------------------------------------------------ calibration
    def calibrate(self, theta, shifts=None, ids=None):
        tc = self.spec["calibration"]["target_column"]
        ids = ids or self.ids
        G = len(self.zgrid)
        X = []
        for i in ids:
            X += [self.em._x(z, theta, self.by_id[i]) for z in self.zgrid]
        pred_all = self.em.gps[("cal",)].predict(self.em._scale(np.asarray(X, float))).reshape(len(ids), G)
        out = {}
        for j, i in enumerate(ids):
            u = self.by_id[i]
            t = float(_logit(np.array([float(u[tc])]))[0])
            if shifts is not None and self.region:
                t += shifts.get(u[self.region], 0.0)
            pred = np.maximum.accumulate(pred_all[j])   # monotone in transmission
            clamped = not (pred[0] <= t <= pred[-1])
            out[i] = (float(np.interp(t, pred, self.zgrid)), clamped)
        return out

    # ------------------------------------------------------------ outcomes
    def outcomes(self, theta, zs, ids=None):
        o = self.spec["objective"]["outcome"]
        res = {}
        ids = ids or self.ids
        preds = self.em.predict_units([zs[i] for i in ids], theta, [self.by_id[i] for i in ids])
        for i, pr in zip(ids, preds):
            u = self.by_id[i]
            row = {"eir": pr["eir"]}
            for k in self.pk:
                vals = {}
                for oc in self.em.outcomes:
                    vals[oc] = sum(pr["y"][(P, oc, k)] * float(u[pm["column"]]) for P, pm in self.pops.items())
                tot = sum(float(u[pm["column"]]) for pm in self.pops.values())
                vals["treated"] = pr["treated"][k] * tot
                row[k] = vals
            res[i] = row
        self._primary = o
        return res

    def benefit(self, out):
        o = self.spec["objective"]["outcome"]
        return {i: {k: out[i][self.comp][o] - out[i][k][o] for k in self.pk} for i in out}

    def costs(self, out):
        fn = lambda u, p: {"treated": out[str(u[self.uid])][p]["treated"]}  # noqa: E731
        return acosts.cost_matrix(self.opts, self.units, outcome=fn)

    def solve(self, ben, cost):
        r = aopt.solve(ben, cost, self.feas, self.budget, method="ilp", time_limit=120)
        if r["status"] != "optimal":
            raise RuntimeError(f"optimizer status {r['status']}")
        return r

    # ------------------------------------------------------------ steps
    def run(self, draws=None, seed=None):
        t0 = time.time()
        spec = self.spec
        A = {"emulator_accuracy": self.em.accuracy, "budget_allocatable": self.budget,
             "partial_batch_test_only": self.em.partial, "design_points_used": int(len(self.em.hold))}
        cal = self.calibrate(self.theta0)
        z0 = {i: v[0] for i, v in cal.items()}
        out0 = self.outcomes(self.theta0, z0)
        ben0, cost0 = self.benefit(out0), self.costs(out0)
        sol = self.solve(ben0, cost0)
        plan = sol["choice"]
        o = spec["objective"]["outcome"]
        A["calibration"] = {"clamped_units": sum(v[1] for v in cal.values()), "units": len(cal),
                            "max_eir_annual": max(out0[i]["eir"] for i in self.ids),
                            "median_eir_annual": float(np.median([out0[i]["eir"] for i in self.ids]))}
        A["point"] = self._summ(plan, out0, cost0)
        A["point"]["solver"] = {k: sol[k] for k in ("status", "method", "objective", "cost", "seconds")}
        self.log(f"point: {A['point']['averted'][o]:,.0f} {o} averted, cost {sol['cost']:,.0f}, mix {A['point']['mix']}")
        # uncertainty
        nd = int(draws if draws is not None else spec["uncertainty"].get("draws", 32))
        rng = np.random.default_rng(int(seed if seed is not None else spec["uncertainty"].get("seed", 42)))
        sd = float((spec["calibration"].get("target_uncertainty") or {}).get("common_state_sd_logit", 0.0))
        regions = sorted({u[self.region] for u in self.units}) if self.region else []
        fixed_vals, opt_vals, same_pkg = {oc: [] for oc in self.em.outcomes}, [], np.zeros(len(self.ids))
        for d in range(nd):
            th = {k: float(rng.uniform(v[0], v[1])) for k, v in self.fr.items()}
            sh = {r: float(rng.normal(0, sd)) for r in regions}
            cz = self.calibrate(th, sh)
            od = self.outcomes(th, {i: v[0] for i, v in cz.items()})
            for oc in self.em.outcomes:
                fixed_vals[oc].append(sum(od[i][self.comp][oc] - od[i][plan[i]][oc] for i in self.ids))
            bd, cd = self.benefit(od), self.costs(od)
            sd_ = self.solve(bd, cd)
            opt_vals.append(sd_["objective"])
            same_pkg += np.array([sd_["choice"][i] == plan[i] for i in self.ids])
        A["uncertainty"] = {"draws": nd, "fixed_plan": {oc: _q(v) for oc, v in fixed_vals.items()},
                            "reoptimised_objective": _q(opt_vals),
                            "units_same_package_share_median": float(np.median(same_pkg / max(nd, 1))) if nd else None,
                            "units_same_package_in_90pct_of_draws": int(np.sum(same_pkg / max(nd, 1) >= 0.9)) if nd else None}
        self.log(f"UQ: {A['uncertainty']['fixed_plan'][o]}")
        A["loo"] = self.loo(z0, out0, ben0, cost0, plan)
        A["decision_tests"] = [self.dt(dt, out0, ben0, cost0, plan) for dt in spec.get("decision_tests") or []]
        A["wall_s"] = round(time.time() - t0, 1)
        self._write(A, plan, out0, cost0, z0, ben0)
        return A

    def _summ(self, plan, out, cost):
        o = self.spec["objective"]["outcome"]
        averted = {oc: sum(out[i][self.comp][oc] - out[i][plan[i]][oc] for i in self.ids) for oc in self.em.outcomes}
        base = {oc: sum(out[i][self.comp][oc] for i in self.ids) for oc in self.em.outcomes}
        mix = {}
        for i in self.ids:
            for c in self.pk[plan[i]]:
                mix[c] = mix.get(c, 0) + 1
        mix["no_package"] = sum(plan[i] == self.comp for i in self.ids)
        spent = sum(cost[i][plan[i]]["total"] for i in self.ids)
        return {"averted": averted, "comparator_total": base, "mix": mix, "cost_allocated": spent,
                "usd_per_" + o: spent / averted[o] if averted[o] > 0 else None}

    def loo(self, z0, out0, ben0, cost0, plan):
        g = self.spec["decision_units"].get("holdout_group")
        cov = self.spec["calibration"].get("loo_covariates") or []
        if not g or not cov:
            return {"skipped": "no holdout_group or loo_covariates in the spec"}
        Xall = np.array([[float(self.by_id[i][c]) for c in cov] for i in self.ids])
        mu, sdv = Xall.mean(0), Xall.std(0) + 1e-12
        Xs = np.column_stack([np.ones(len(self.ids)), (Xall - mu) / sdv])
        y = np.array([z0[i] for i in self.ids])
        groups = sorted({self.by_id[i][g] for i in self.ids}, key=str)
        folds = []
        for gv in groups:
            m = np.array([self.by_id[i][g] == gv for i in self.ids])
            if m.all():
                continue
            lam = 1e-3 * np.eye(Xs.shape[1])
            beta = np.linalg.solve(Xs[~m].T @ Xs[~m] + lam, Xs[~m].T @ y[~m])
            zh = dict(z0)
            hid = [i for i, k in zip(self.ids, m) if k]
            for i, k in zip(self.ids, m):
                if k:
                    zh[i] = float(Xs[self.ids.index(i)] @ beta)
            oh = self.outcomes(self.theta0, zh, hid)
            out_f = dict(out0)
            out_f.update(oh)
            ben_f = self.benefit(out_f)
            cost_f = self.costs(out_f)
            sol = self.solve(ben_f, cost_f)
            pf = sol["choice"]
            c_full = [cost0[i][plan[i]]["total"] for i in hid]
            c_fold = [cost0[i][pf[i]]["total"] for i in hid]
            rho = _spearman(c_full, c_fold) if len(hid) >= 3 else None
            tot_full = sum(ben0[i][plan[i]] for i in self.ids)
            tot_fold = sum(ben0[i][pf[i]] for i in self.ids)   # the fold plan, judged with full-calibration benefits
            b_full = sum(ben0[i][plan[i]] for i in hid)
            b_fold = sum(ben0[i][pf[i]] for i in hid)
            folds.append({"group": gv, "n_units": len(hid), "rank_corr": rho,
                          "package_concordance_pct": 100.0 * np.mean([plan[i] == pf[i] for i in hid]),
                          "benefit_gap_pct": 100.0 * (b_full - b_fold) / b_full if b_full > 0 else 0.0,
                          "total_benefit_loss_pct": 100.0 * (tot_full - tot_fold) / tot_full if tot_full > 0 else 0.0,
                          "z_shift_mean": float(np.mean([zh[i] - z0[i] for i in hid]))})
        big = [f for f in folds if f["n_units"] >= 5 and f["rank_corr"] is not None and not math.isnan(f["rank_corr"])]
        res = {"holdout_method": f"leave-one-{g}-out", "n_folds": len(folds), "folds": folds,
               "metrics": {"rank_correlation_mean": _mean([f["rank_corr"] for f in big]),
                           "rank_correlation_worst_fold": min((f["rank_corr"] for f in big), default=None),
                           "folds_with_rank_corr": len(big),
                           "package_concordance_pct_mean": _mean([f["package_concordance_pct"] for f in folds]),
                           "package_concordance_pct_worst_fold": min(f["package_concordance_pct"] for f in folds),
                           "package_concordance_pct_worst_fold_ge5": min((f["package_concordance_pct"] for f in big), default=None),
                           "total_benefit_loss_pct_mean": _mean([f["total_benefit_loss_pct"] for f in folds]),
                           "total_benefit_loss_pct_worst_fold": max(f["total_benefit_loss_pct"] for f in folds),
                           "benefit_gap_pct_mean": _mean([f["benefit_gap_pct"] for f in folds]),
                           "benefit_gap_pct_worst_fold": max(f["benefit_gap_pct"] for f in folds)}}
        m_ = res["metrics"]
        worst = m_["rank_correlation_worst_fold"]
        bands = [("ROBUST" if worst is None or worst >= 0.7 else "FRAGILE" if worst >= 0.4 else "UNSTABLE"),
                 ("ROBUST" if m_["benefit_gap_pct_worst_fold"] <= 15 else "FRAGILE" if m_["benefit_gap_pct_worst_fold"] <= 30 else "UNSTABLE"),
                 ("ROBUST" if m_["package_concordance_pct_worst_fold"] >= 80 else "FRAGILE" if m_["package_concordance_pct_worst_fold"] >= 60 else "UNSTABLE")]
        res["verdict"] = "UNSTABLE" if "UNSTABLE" in bands else ("FRAGILE" if "FRAGILE" in bands else "ROBUST")
        return res

    def dt(self, dt, out0, ben0, cost0, plan):
        rec = {"id": dt["id"], "type": dt["type"], "text": dt.get("text", "")}
        arm = dt.get("engine_arm_or")
        if arm is None:
            rows = {b["id"]: b for b in (self.eng.describe().benchmarks or [])}
            b = rows.get(dt.get("benchmark"))
            if not b or not b.get("arm_or"):
                rec["status"] = f"NOT RUN: benchmark {dt.get('benchmark')} with arm ORs not in the engine card"
                return rec
            arm = {k: float(np.exp(np.mean([np.log(x[k]) for x in b["arm_or"]]))) for k in ("a11", "a10", "a01")}
        rec["engine_arm_or"] = arm
        eng_int = arm["a11"] / (arm["a10"] * arm["a01"])
        rec["engine_interaction"] = eng_int
        comp, with_any = set(dt["components"]), set(dt["with_any"])
        rec["results"] = []
        for v in dt["values"]:
            num = math.log(v * arm["a01"])
            den = math.log(arm["a11"] / arm["a10"])
            gfac = float(np.clip(num / den, 0.0, 5.0)) if den < 0 else 5.0
            ben = {}
            for i in self.ids:
                ben[i] = {}
                for k, cs in self.pk.items():
                    if comp <= cs and cs & with_any:
                        base = next(n for n, c2 in self.pk.items() if c2 == cs - comp)
                        ben[i][k] = ben0[i][base] + gfac * (ben0[i][k] - ben0[i][base])
                    else:
                        ben[i][k] = ben0[i][k]
            sol = self.solve(ben, cost0)
            ch = sol["choice"]
            n_comp = sum(bool(self.pk[ch[i]] & comp) for i in self.ids)
            rec["results"].append({"trial_value": v, "increment_scale": gfac, "units_changed": sum(ch[i] != plan[i] for i in self.ids),
                                   "units_with_component": n_comp,
                                   "units_with_component_point_plan": sum(bool(self.pk[plan[i]] & comp) for i in self.ids),
                                   "objective": sol["objective"],
                                   "point_plan_objective_under_test": sum(ben[i][plan[i]] for i in self.ids)})
        rec["plan_changes"] = any(r["units_changed"] > 0 for r in rec["results"])
        return rec

    def _write(self, A, plan, out0, cost0, z0, ben0):
        os.makedirs(self.out, exist_ok=True)
        json.dump(A, open(os.path.join(self.out, "analysis.json"), "w"), indent=1, default=float)
        o = self.spec["objective"]["outcome"]
        with open(os.path.join(self.out, "allocation.csv"), "w", newline="") as f:
            w = csv.writer(f)
            w.writerow([self.uid, "region", "package", "cost_usd", f"{o}_averted", "z_calibrated", "eir_annual"])
            for i in self.ids:
                w.writerow([i, self.by_id[i].get(self.region, ""), plan[i], round(cost0[i][plan[i]]["total"], 2),
                            round(ben0[i][plan[i]], 2), round(z0[i], 4), round(out0[i]["eir"], 2)])


def _q(v):
    v = np.asarray(v, float)
    if not len(v):
        return None
    return {"median": float(np.median(v)), "lo95": float(np.percentile(v, 2.5)), "hi95": float(np.percentile(v, 97.5)),
            "mean": float(v.mean())}


def _mean(v):
    v = [x for x in v if x is not None and not (isinstance(x, float) and math.isnan(x))]
    return float(np.mean(v)) if v else None


def _spearman(a, b):
    from scipy.stats import spearmanr
    if np.std(a) == 0 or np.std(b) == 0:
        return 1.0 if list(a) == list(b) else float("nan")
    return float(spearmanr(a, b).correlation)


def _self_test() -> int:
    import tempfile
    fails = []

    def ok(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    with tempfile.TemporaryDirectory() as d:
        rng = np.random.default_rng(3)
        p = os.path.join(d, "u.csv")
        with open(p, "w") as f:
            f.write("id,region,grp,cov,pop,elig,tgt\n")
            for i in range(24):
                x = rng.uniform(-1.5, 1.5)
                prev = 1 - math.exp(-0.5 * math.exp(0.8 * x))
                f.write(f"{i},{'r' + str(i % 3)},{'g' + str(i % 4)},{x + rng.normal(0, 0.1):.4f},{1000 + 50 * i},{i % 2},{prev:.5f}\n")
        spec = {
            "engine": "toolkit.engines.contract:ToyEngine",
            "decision_units": {"csv": p, "id": "id", "region": "region", "holdout_group": "grp",
                               "populations": {"all": {"column": "pop", "age_groups": ["all"]}},
                               "engine_attributes": {}},
            "calibration": {"attribute": "x", "scale": "linear", "target_column": "tgt", "log_range": [-2, 2],
                            "window": [1, 2], "metric": "prev", "target_uncertainty": {"common_state_sd_logit": 0.05},
                            "loo_covariates": ["cov"]},
            "horizon": {"start": 0, "stop": 3, "window": [0, 3]},
            "emulator": {"points": 60, "holdout_share": 0.15, "seed": 5, "features": []},
            "options": {"units": {"csv": p, "id": "id", "populations": {"total": "pop"}},
                        "components": {"a": {}, "b": {}}, "packages": {"generate": True},
                        "eligibility": {"b": {"column": "elig"}},
                        "engine_map": {"a": {"a": 1}, "b": {"b": 1}},
                        "prices": {"base_year": "2024", "index": {"2024": 1.0}},
                        "cost_items": [{"id": "ca", "component": "a", "value": 1.0, "price_year": "2024",
                                        "price_rule": "as_published", "basis": "per_person", "includes": ["x"]},
                                       {"id": "cb", "component": "b", "value": 0.6, "price_year": "2024",
                                        "price_rule": "as_published", "basis": "per_person", "includes": ["y"]}]},
            "objective": {"outcome": "dalys"}, "outcomes_reported": ["dalys", "cases"],
            "constraints": {"envelope_usd": 20000}, "comparator": {"package": "none"},
            "uncertainty": {"draws": 6, "seed": 1},
            "decision_tests": [{"id": "DT1", "type": "interaction_rescale", "components": ["b"], "with_any": ["a"],
                                "values": [1.0, 3.0], "engine_arm_or": {"a11": 0.4, "a10": 0.5, "a01": 0.7}}],
            "deliverable": "package per unit"}
        ok(S.validate(spec) == [], f"A1 toy spec validates {S.validate(spec)}")
        B.design(spec, d)
        B.run(d, 2)
        an = Analysis(spec, d, log=lambda *a: None)
        acc = an.em.accuracy
        ok(acc["reduction_r2_median"] > 0.9 or acc["reduction_mae_median"] < 0.01,
           f"A2 emulator held-out accuracy on the toy engine ({acc})")
        cal = an.calibrate(an.theta0)
        errs = []
        for i in an.ids:
            u = an.by_id[i]
            t = float(u["tgt"])
            x_true = math.log(-math.log(1 - t) / 0.5) / 0.8
            errs.append(abs(cal[i][0] - x_true))
        ok(max(errs) < 0.1, f"A3 calibration by emulator inversion recovers the toy unit's attribute (max error {max(errs):.3f})")
        A = an.run()
        ok(A["point"]["solver"]["status"] == "optimal" and A["point"]["cost_allocated"] <= 20000 * (1 + 1e-9),
           "A4 the plan is a proven optimum within the budget (toolkit/alloc ILP)")
        fp = A["uncertainty"]["fixed_plan"]["dalys"]
        ok(fp["lo95"] <= A["point"]["averted"]["dalys"] * 1.2 and fp["hi95"] >= fp["lo95"], f"A5 UQ interval computed ({fp})")
        ok(A["loo"]["n_folds"] == 4 and A["loo"]["verdict"] in ("ROBUST", "FRAGILE", "UNSTABLE"), "A6 leave-one-group-out runs every fold")
        dt = A["decision_tests"][0]
        r1 = dt["results"][0]
        ok(abs(r1["increment_scale"] - math.log(0.7) / math.log(0.8)) < 1e-9,
           f"A7 interaction rescale: trial interaction 1.0 scales the increment to the no-interaction value ({r1['increment_scale']:.3f})")
        ok(os.path.exists(os.path.join(d, "allocation.csv")) and os.path.exists(os.path.join(d, "analysis.json")),
           "A8 artifacts written")
        bad = dict(spec)
        os.remove(os.path.join(d, "results.jsonl"))
        open(os.path.join(d, "results.jsonl"), "w").close()
        try:
            Analysis(bad, d, log=lambda *a: None)
            ok(False, "A9 missing engine results fail closed")
        except ValueError:
            ok(True, "A9 missing engine results fail closed")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("spec")
    ap.add_argument("out")
    ap.add_argument("--results", default=None)
    ap.add_argument("--draws", type=int, default=None)
    a = ap.parse_args(argv)
    spec = yaml.safe_load(open(a.spec))
    errs = S.validate(spec)
    if errs:
        print("spec invalid:\n  " + "\n  ".join(errs))
        return 2
    A = Analysis(spec, a.out, a.results).run(a.draws)
    print(json.dumps({k: A[k] for k in ("point", "uncertainty", "calibration", "emulator_accuracy")}, indent=1, default=float)[:3000])
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    sys.exit(main())
