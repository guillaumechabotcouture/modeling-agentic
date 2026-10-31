#!/usr/bin/env python3
"""Generator for eval question `measles_campaign` (synthetic; Phase 50 α).

Generating model (the answer key; never shipped to agents):
- 12 districts; each single-year birth cohort is 3.0% of the population; everyone
  aged 15+ in 2026 is immune; children aged 0 in 2026 are all susceptible.
- Cohort immunity from routine vaccination: v = 1 - (1 - 0.85 c1)(1 - 0.90 c2), with
  c1, c2 the cohort's true MCV1 and MCV2 coverage (MCV2 only from age 2).
- PLANTED immunity gap: in districts M03, M07 and M10 the cohorts born 2020-2022
  (aged 4-6 in 2026) had true MCV1 coverage 0.40 and MCV2 0.25, while the
  administrative data report the district's usual coverage. Only the 2025
  serosurvey (150 children per age band per district) shows it.
- Campaign options per district: none; A = all children aged 1-4 years; B = all
  children aged 1-9 years. A campaign reaches 85% of targeted children and seroconverts
  93% of susceptibles reached. Cost US$1.10 per targeted child plus US$25,000 per district.
  Budget US$800,000.
- Outcome: one importation per district; final size of a homogeneous-mixing SIR,
  cases = N s z with z = 1 - exp(-R0 s z), s the all-age susceptible fraction;
  R0 = 15 (the question says 12-18). Objective: expected cases averted.

Truth: exact optimum, per-district cases averted by each option, the naive optimum
from administrative coverage (the trap) and the plan from serosurvey estimates.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _common import Rng, acceptable_packages, final_size, mckp, plan_cost, plan_value, write_csv, write_truth  # noqa: E402

QUESTION = "measles_campaign"
SEED = 20261031
HERE = os.path.dirname(os.path.abspath(__file__))
INPUTS = os.path.join(os.path.dirname(os.path.dirname(HERE)), "data", "evals", QUESTION)   # shipped
TRUTH = os.path.join(HERE, "_truth")   # never shipped
YEAR = 2026
COHORT_SHARE = 0.03
R0_TRUE = 15.0
E1, E2 = 0.85, 0.90
REACH, SEROCONV = 0.85, 0.93
COST_PER_CHILD, COST_FIXED = 1.10, 25000.0
BUDGET = 800_000.0
PACKAGES = ["none", "A_1to4", "B_1to9"]
TARGET_AGES = {"none": (), "A_1to4": range(1, 5), "B_1to9": range(1, 10)}
GAP_DISTRICTS = ("M03", "M07", "M10")
GAP_BIRTH_YEARS = (2020, 2021, 2022)
GAP_C1, GAP_C2 = 0.40, 0.25
SERO_N = 150
SERO_BANDS = [(1, 4), (5, 9), (10, 14)]   # age in 2025
TIE_TOL = 0.015

# name, population, usual MCV1 coverage (true and reported outside the gap), MCV2 coverage
DISTRICTS = [
    ("M01", 420000, 0.91, 0.78), ("M02", 260000, 0.86, 0.70), ("M03", 380000, 0.90, 0.76),
    ("M04", 150000, 0.83, 0.62), ("M05", 510000, 0.80, 0.60), ("M06", 220000, 0.92, 0.80),
    ("M07", 300000, 0.89, 0.74), ("M08", 640000, 0.88, 0.72), ("M09", 180000, 0.84, 0.66),
    ("M10", 240000, 0.91, 0.77), ("M11", 330000, 0.87, 0.71), ("M12", 120000, 0.82, 0.64),
]


def true_cov(name, c1, c2, birth_year):
    if name in GAP_DISTRICTS and birth_year in GAP_BIRTH_YEARS:
        return GAP_C1, GAP_C2
    return c1, c2


def immunity(c1, c2, age):
    if age <= 0:
        return 0.0
    return 1 - (1 - E1 * c1) * (1 - (E2 * c2 if age >= 2 else 0.0))


def susceptible_by_age(imm: dict) -> dict:
    """imm[age] (0..14 in 2026) -> susceptible fraction by age."""
    return {a: 1 - imm[a] for a in range(15)}


def outcome(pop, sus: dict, pkg: str, r0: float) -> float:
    """Expected cases after one importation with campaign `pkg`."""
    s_age = dict(sus)
    for a in TARGET_AGES[pkg]:
        s_age[a] *= 1 - REACH * SEROCONV
    s = COHORT_SHARE * sum(s_age.values())   # adults 15+ immune
    return pop * s * final_size(r0 * s)


def pkg_cost(pop, pkg):
    if pkg == "none":
        return 0.0
    return COST_FIXED + COST_PER_CHILD * pop * COHORT_SHARE * len(TARGET_AGES[pkg])


def tables(sus_by_district: dict, r0: float):
    benefit, cost, feas = {}, {}, {}
    for name, pop, *_ in DISTRICTS:
        base = outcome(pop, sus_by_district[name], "none", r0)
        feas[name] = list(PACKAGES)
        benefit[name] = {p: base - outcome(pop, sus_by_district[name], p, r0) for p in PACKAGES}
        cost[name] = {p: pkg_cost(pop, p) for p in PACKAGES}
    return benefit, cost, feas


def generate(inputs_dir: str = INPUTS, truth_dir: str = TRUTH, seed: int = SEED, strict: bool = True) -> dict:
    rng = Rng(seed)
    admin_rows, sero_rows = [], []
    sus_true, sus_admin, sus_sero = {}, {}, {}
    for name, pop, c1, c2 in DISTRICTS:
        imm_true, imm_admin = {}, {}
        for a in range(15):
            by = YEAR - a
            tc1, tc2 = true_cov(name, c1, c2, by)
            imm_true[a] = immunity(tc1, tc2, a)
            r1 = min(0.99, max(0.3, c1 + rng.normal(0, 0.02)))
            r2 = min(r1, max(0.2, c2 + rng.normal(0, 0.02)))
            if 1 <= a <= 14:   # cohorts born 2012-2025 have a reported coverage
                admin_rows.append([name, by, round(r1, 3), round(r2, 3) if a >= 2 else ""])
            imm_admin[a] = immunity(r1, r2, a)
        sus_true[name] = susceptible_by_age(imm_true)
        sus_admin[name] = susceptible_by_age(imm_admin)
        # 2025 serosurvey: age in 2025 = age in 2026 - 1
        est = dict(imm_admin)
        for lo, hi in SERO_BANDS:
            ages26 = [a + 1 for a in range(lo, hi + 1)]
            p = sum(imm_true[a] if a < 15 else 1.0 for a in ages26) / len(ages26)
            k = rng.binomial(SERO_N, p)
            sero_rows.append([name, f"{lo}-{hi}", SERO_N, k])
            for a in ages26:
                if a < 15:
                    est[a] = k / SERO_N
        sus_sero[name] = susceptible_by_age(est)

    inp = inputs_dir
    write_csv(os.path.join(inp, "districts.csv"), ["district", "population_2026"], [[n, p] for n, p, *_ in DISTRICTS])
    write_csv(os.path.join(inp, "routine_coverage_admin.csv"), ["district", "birth_year", "mcv1_coverage", "mcv2_coverage"],
              admin_rows)
    write_csv(os.path.join(inp, "serosurvey_2025.csv"), ["district", "age_band_years_2025", "n_tested", "n_seropositive"],
              sero_rows)

    benefit, cost, feas = tables(sus_true, R0_TRUE)
    opt = mckp(benefit, cost, feas, BUDGET)
    acc = acceptable_packages(benefit, cost, feas, BUDGET, opt, TIE_TOL)
    nb, _, _ = tables(sus_admin, R0_TRUE)
    naive = mckp(nb, cost, feas, BUDGET)
    db, _, _ = tables(sus_sero, R0_TRUE)
    data_opt = mckp(db, cost, feas, BUDGET)
    naive_true = plan_value(naive["choice"], benefit)
    n_naive = sum(naive["choice"][u] != opt["choice"][u] for u in opt["choice"])
    n_data = sum(data_opt["choice"][u] not in acc["acceptable"][u] for u in opt["choice"])
    problems = []
    if n_naive < 2 or naive_true > 0.95 * opt["objective"]:
        problems.append(f"trap too weak: naive differs in {n_naive} units, ratio {naive_true / opt['objective']:.3f}")
    if n_data > 1:
        problems.append(f"serosurvey plan differs from the optimum in {n_data} units")
    if problems and strict:
        raise SystemExit(f"{QUESTION} generator (seed {seed}): " + "; ".join(problems))

    truth = {
        "question": QUESTION, "seed": seed, "generator_version": 1,
        "allocation": {
            "kind": "categorical", "sense": "max", "budget": BUDGET, "units": [d for d, *_ in DISTRICTS],
            "packages": PACKAGES, "benefit": benefit, "cost": cost, "feasible": feas,
            "optimum": opt, "acceptable": acc["acceptable"], "tie_tol": TIE_TOL,
            "naive_optima": {"admin_coverage": {"choice": naive["choice"], "true_objective": naive_true,
                                                "assumption": "immunity from administrative coverage"}},
            "data_optimum": {"choice": data_opt["choice"], "true_objective": plan_value(data_opt["choice"], benefit),
                             "units_outside_acceptable": n_data},
            "benefit_unit": "expected measles cases averted after one importation",
        },
        "headlines": {"total_cases_averted": {"basis": "own_plan", "value_of_optimum": opt["objective"]}},
        "parameters": {"r0": R0_TRUE, "gap_districts": list(GAP_DISTRICTS), "gap_birth_years": list(GAP_BIRTH_YEARS),
                       "susceptible_fraction_all_ages": {
                           n: COHORT_SHARE * sum(sus_true[n].values()) for n, *_ in DISTRICTS}},
        "planted": ["immunity gap in cohorts born 2020-2022 in M03, M07, M10 (aged 4-6 in 2026), "
                    "invisible in administrative coverage"],
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
    print("naive:  ", al["naive_optima"]["admin_coverage"]["choice"], round(al["naive_optima"]["admin_coverage"]["true_objective"]))
    print("data:   ", al["data_optimum"])
    print("s:      ", {k: round(v, 4) for k, v in t["parameters"]["susceptible_fraction_all_ages"].items()})
