#!/usr/bin/env python3
"""Generator for eval question `tb_acf` (synthetic; Phase 50 α).

Generating model (the answer key; never shipped to agents):
- 10 districts, each with a target population of adults in high-risk settings,
  an undiagnosed bacteriologically positive TB prevalence p_d and a subclinical
  fraction f_d (prevalent TB without symptoms).
- One screening round reaches 70% of the target population.
  SYM (symptom screen, Xpert for presumptive): sensitivity (1 - f_d) x 0.90 x 0.85.
  CXR (chest X-ray for everyone, Xpert if abnormal): sensitivity 0.90 x 0.85.
  Cases detected = 0.70 x N_d x p_d x sensitivity.
- PLANTED yield differences: f_d is 0.75 in T02, T05 and T08 (mining and dense urban
  settings) against 0.30-0.40 elsewhere, so symptom screening finds few cases there
  and the CXR/SYM yield ratio is about 2.5x the other districts'. A modeller who pools
  the yield ratio across districts (or uses the national reference yields) misallocates.
- ACF records 2022-2025: in each district and year 5,000 people were screened with
  each algorithm; detections ~ Binomial(5000, p_d x sensitivity) (prevalence stable).
- Costs per person screened: SYM US$2.50, CXR US$7.00. Budget US$1.0 M.

Truth: exact optimum, per-district detections for each package, the naive optimum
with a pooled CXR/SYM ratio (the trap) and the plan from the pilot estimates.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _common import Rng, acceptable_packages, mckp, plan_cost, plan_value, write_csv, write_truth  # noqa: E402

QUESTION = "tb_acf"
SEED = 20261101
HERE = os.path.dirname(os.path.abspath(__file__))
INPUTS = os.path.join(os.path.dirname(os.path.dirname(HERE)), "data", "evals", QUESTION)   # shipped
TRUTH = os.path.join(HERE, "_truth")   # never shipped
PACKAGES = ["none", "SYM", "CXR"]
REACH = 0.70
SENS_SYMPTOM, SENS_CXR, SENS_XPERT = 0.90, 0.90, 0.85
COST = {"none": 0.0, "SYM": 2.50, "CXR": 7.00}
BUDGET = 1_000_000.0
ACF_YEARS = (2022, 2023, 2024, 2025)
PILOT_N = 5000   # people screened per algorithm per district per year
TIE_TOL = 0.01
HIGH_F = ("T02", "T05", "T08")

# name, target population, undiagnosed prevalence, subclinical fraction
DISTRICTS = [
    ("T01", 60000, 0.0080, 0.35), ("T02", 85000, 0.0110, 0.75), ("T03", 40000, 0.0060, 0.30),
    ("T04", 70000, 0.0045, 0.40), ("T05", 55000, 0.0095, 0.75), ("T06", 90000, 0.0035, 0.35),
    ("T07", 35000, 0.0120, 0.30), ("T08", 75000, 0.0070, 0.75), ("T09", 50000, 0.0050, 0.35),
    ("T10", 65000, 0.0030, 0.40),
]


def sens(f: float, pkg: str) -> float:
    if pkg == "SYM":
        return (1 - f) * SENS_SYMPTOM * SENS_XPERT
    if pkg == "CXR":
        return SENS_CXR * SENS_XPERT
    return 0.0


def tables(yield_per_screened: dict):
    """yield_per_screened[d][pkg] = cases detected per person screened."""
    benefit, cost, feas = {}, {}, {}
    for name, n, *_ in DISTRICTS:
        feas[name] = list(PACKAGES)
        benefit[name] = {p: REACH * n * yield_per_screened[name][p] for p in PACKAGES}
        cost[name] = {p: REACH * n * COST[p] for p in PACKAGES}
    return benefit, cost, feas


def generate(inputs_dir: str = INPUTS, truth_dir: str = TRUTH, seed: int = SEED, strict: bool = True) -> dict:
    rng = Rng(seed)
    true_y, pilot_y, pilot_rows = {}, {}, []
    for name, n, p, f in DISTRICTS:
        true_y[name] = {k: p * sens(f, k) for k in PACKAGES}
        pilot_y[name] = {"none": 0.0}
        for k in ("SYM", "CXR"):
            dets = 0
            for y in ACF_YEARS:
                det = rng.binomial(PILOT_N, true_y[name][k])
                pilot_rows.append([name, y, k, PILOT_N, det])
                dets += det
            pilot_y[name][k] = dets / (PILOT_N * len(ACF_YEARS))
    # trap: one pooled CXR/SYM yield ratio for every district (from the pilot totals)
    tot = {k: sum(r[4] for r in pilot_rows if r[2] == k) for k in ("SYM", "CXR")}
    pooled_ratio = tot["SYM"] / tot["CXR"]
    naive_y = {d: {"none": 0.0, "CXR": pilot_y[d]["CXR"], "SYM": pilot_y[d]["CXR"] * pooled_ratio} for d in pilot_y}

    inp = inputs_dir
    write_csv(os.path.join(inp, "districts.csv"), ["district", "target_population_high_risk_adults"],
              [[d, n] for d, n, *_ in DISTRICTS])
    write_csv(os.path.join(inp, "acf_records_2022_2025.csv"),
              ["district", "year", "algorithm", "n_screened", "n_bact_confirmed_detected"], pilot_rows)
    write_csv(os.path.join(inp, "algorithm_costs.csv"), ["algorithm", "usd_per_person_screened", "description"],
              [["SYM", COST["SYM"], "symptom screen; Xpert MTB/RIF Ultra for presumptive TB"],
               ["CXR", COST["CXR"], "chest X-ray for everyone screened; Xpert if abnormal"]])
    write_csv(os.path.join(inp, "national_reference_yields.csv"), ["algorithm", "cases_detected_per_1000_screened", "source"],
              [[k, round(1000 * tot[k] / (PILOT_N * len(ACF_YEARS) * len(DISTRICTS)), 2),
                "national average of the 2022-2025 ACF rounds"] for k in ("SYM", "CXR")])

    benefit, cost, feas = tables(true_y)
    opt = mckp(benefit, cost, feas, BUDGET)
    acc = acceptable_packages(benefit, cost, feas, BUDGET, opt, TIE_TOL)
    nb, _, _ = tables(naive_y)
    naive = mckp(nb, cost, feas, BUDGET)
    db, _, _ = tables(pilot_y)
    data_opt = mckp(db, cost, feas, BUDGET)
    naive_true = plan_value(naive["choice"], benefit)
    n_naive = sum(naive["choice"][u] not in acc["acceptable"][u] for u in opt["choice"])
    n_data = sum(data_opt["choice"][u] not in acc["acceptable"][u] for u in opt["choice"])
    problems = []
    if n_naive < 2 or naive_true > 0.97 * opt["objective"]:
        problems.append(f"trap too weak: naive differs in {n_naive} units, ratio {naive_true / opt['objective']:.3f}")
    if n_data > 1:
        problems.append(f"pilot plan differs from the optimum in {n_data} units")
    if problems and strict:
        raise SystemExit(f"{QUESTION} generator (seed {seed}): " + "; ".join(problems))

    truth = {
        "question": QUESTION, "seed": seed, "generator_version": 1,
        "allocation": {
            "kind": "categorical", "sense": "max", "budget": BUDGET, "units": [d for d, *_ in DISTRICTS],
            "packages": PACKAGES, "benefit": benefit, "cost": cost, "feasible": feas,
            "optimum": opt, "acceptable": acc["acceptable"], "tie_tol": TIE_TOL,
            "naive_optima": {"pooled_yield_ratio": {"choice": naive["choice"], "true_objective": naive_true,
                                                    "assumption": f"one CXR/SYM yield ratio ({pooled_ratio:.3f}) everywhere"}},
            "data_optimum": {"choice": data_opt["choice"], "true_objective": plan_value(data_opt["choice"], benefit),
                             "units_outside_acceptable": n_data},
            "benefit_unit": "bacteriologically confirmed TB cases detected in one screening round",
        },
        "headlines": {"total_cases_detected": {"basis": "own_plan", "value_of_optimum": opt["objective"]}},
        "parameters": {"prevalence": {d: p for d, _, p, _ in DISTRICTS},
                       "subclinical_fraction": {d: f for d, *_, f in DISTRICTS}, "high_subclinical": list(HIGH_F)},
        "planted": ["subclinical fraction 0.75 in T02, T05, T08: symptom screening yield much lower there"],
        "optimum_cost": plan_cost(opt["choice"], cost),
    }
    write_truth(truth_dir, truth, inputs_dir)
    return truth


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--inputs", default=INPUTS)
    ap.add_argument("--truth", default=TRUTH)
    ap.add_argument("--seed", type=int, default=SEED)
    a = ap.parse_args()
    t = generate(a.inputs, a.truth, a.seed)
    al = t["allocation"]
    print("optimum:", al["optimum"]["choice"], round(al["optimum"]["objective"]), "cost", round(t["optimum_cost"]))
    print("naive:  ", al["naive_optima"]["pooled_yield_ratio"])
    print("data:   ", al["data_optimum"])
