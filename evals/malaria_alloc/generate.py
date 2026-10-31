#!/usr/bin/env python3
"""Generator for eval question `malaria_alloc` (synthetic; Phase 50 α).

Generating model (all of it is the answer key; never shipped to agents):
- 10 districts, each with a baseline all-age clinical incidence I0_d (cases per
  person-year with no ITN, IRS or SMC) and a population.
- Each package multiplies all-age incidence by a rate ratio RR_p: ITN 0.60, IRS 0.65,
  SMC 0.85 (seasonal districts only; under-5s in the high season), ITN+SMC 0.51 and
  ITN+IRS+SMC 0.4675 (SMC multiplicative with vector control), and ITN+IRS 0.55.
  PLANTED: ITN+IRS is redundant; a multiplicative model gives 0.39. IRS adds about 8%
  on top of nets, not 35%.
- Programme history 2015-2024: each district-year had one package (drawn at random),
  recorded cases ~ Poisson(pop x I0_d x RR_p x exp(e)), e ~ N(0, 0.10).
- Plan 2026-2028 (3 years); counterfactual: no ITN, IRS or SMC (the 2022 nets have
  expired). Objective: clinical cases averted. Costs over the plan per person in the
  district: ITN 5.40, IRS 12.00 (4.00 a year), SMC 2.16; budget US$18 M, which does not
  cover nets everywhere plus SMC, so IRS on top of nets competes with nets elsewhere.

Truth: the exact optimum of that model, the cases averted by any plan (per-unit
table), and the naive optimum a modeller gets by multiplying ITN and IRS effects
(the trap). The generator checks that the data identify the optimum (Poisson MLE
from the history gives the same plan) and that the trap changes the plan.

    python3 evals/malaria_alloc/generate.py [--inputs DIR] [--truth DIR] [--seed N]
"""
from __future__ import annotations

import argparse
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _common import Rng, acceptable_packages, mckp, plan_cost, plan_value, write_csv, write_truth  # noqa: E402

QUESTION = "malaria_alloc"
SEED = 20261030
HERE = os.path.dirname(os.path.abspath(__file__))
INPUTS = os.path.join(os.path.dirname(os.path.dirname(HERE)), "data", "evals", QUESTION)   # shipped
TRUTH = os.path.join(HERE, "_truth")   # never shipped

# name, population, baseline incidence (cases/person-year, no ITN/IRS/SMC), seasonal
DISTRICTS = [
    ("D01", 420000, 0.62, True), ("D02", 310000, 0.51, True), ("D03", 250000, 0.44, False),
    ("D04", 180000, 0.36, True), ("D05", 520000, 0.30, False), ("D06", 390000, 0.24, True),
    ("D07", 280000, 0.18, False), ("D08", 150000, 0.12, True), ("D09", 600000, 0.08, False),
    ("D10", 230000, 0.05, False),
]
# Packages per district for 2026-2028 (the 2022 campaign nets have expired: no ITN is the counterfactual).
PACKAGES = ["none", "ITN", "IRS", "ITN+IRS", "SMC", "ITN+SMC", "ITN+IRS+SMC"]
HISTORY_PACKAGES = PACKAGES
RR = {"none": 1.0, "ITN": 0.60, "IRS": 0.65, "ITN+IRS": 0.55, "SMC": 0.85, "ITN+SMC": 0.51,
      "ITN+IRS+SMC": 0.4675}
UNIT_COST = {"ITN": 5.40, "IRS": 12.00, "SMC": 2.16}   # US$ per person in the district, whole plan
YEARS_PLAN = 3
BUDGET = 18_000_000.0
HISTORY_YEARS = list(range(2015, 2025))
NOISE_SD = 0.10
TIE_TOL = 0.002


def feasible(seasonal: bool, packages=PACKAGES) -> list[str]:
    return [p for p in packages if seasonal or "SMC" not in p]


def pkg_cost(pop: int, p: str) -> float:
    return 0.0 if p == "none" else pop * sum(UNIT_COST[c] for c in p.split("+"))


def tables(i0: dict, rr: dict):
    benefit, cost, feas = {}, {}, {}
    for name, pop, _, seas in DISTRICTS:
        feas[name] = feasible(seas)
        benefit[name] = {p: pop * i0[name] * (1 - rr[p]) * YEARS_PLAN for p in feas[name]}
        cost[name] = {p: pkg_cost(pop, p) for p in feas[name]}
    return benefit, cost, feas


def mle(history) -> tuple[dict, dict]:
    """Poisson MLE of cases = pop x a_d x r_p (district and package effects, saturated in
    packages) by iterative proportional fitting; r_none = 1."""
    a = {d: 0.2 for d, *_ in DISTRICTS}
    r = {p: 1.0 for p in HISTORY_PACKAGES}
    for _ in range(500):
        for d in a:
            rows = [h for h in history if h[0] == d]
            a[d] = sum(h[4] for h in rows) / sum(h[2] * r[h[3]] for h in rows)
        for p in HISTORY_PACKAGES:
            rows = [h for h in history if h[3] == p]
            if rows:
                r[p] = sum(h[4] for h in rows) / sum(h[2] * a[h[0]] for h in rows)
        k = r["none"]
        r = {p: v / k for p, v in r.items()}
        a = {d: v * k for d, v in a.items()}
    return a, r


def generate(inputs_dir: str = INPUTS, truth_dir: str = TRUTH, seed: int = SEED, strict: bool = True) -> dict:
    rng = Rng(seed)
    i0 = {d: inc for d, _, inc, _ in DISTRICTS}
    history = []   # (district, year, population, package, cases)
    for name, pop, inc, seas in DISTRICTS:
        for y in HISTORY_YEARS:
            opts = feasible(seas, HISTORY_PACKAGES)
            p = rng.choice(opts)
            lam = pop * inc * RR[p] * math.exp(rng.normal(0, NOISE_SD))
            history.append((name, y, pop, p, rng.poisson(lam)))

    inp = inputs_dir
    write_csv(os.path.join(inp, "districts.csv"), ["district", "population", "smc_eligible_seasonal"],
              [[n, pop, seas] for n, pop, _, seas in DISTRICTS])
    write_csv(os.path.join(inp, "programme_history.csv"), ["district", "year", "population", "package", "recorded_cases"],
              [list(h) for h in history])
    write_csv(os.path.join(inp, "unit_costs.csv"), ["intervention", "usd_per_person_covered_whole_plan", "note"],
              [["ITN", UNIT_COST["ITN"], "one mass campaign covering 2026-2028"],
               ["IRS", UNIT_COST["IRS"], "annual spraying 2026-2028 (US$4.00 per person per year)"],
               ["SMC", UNIT_COST["SMC"], "4 monthly rounds per year; children 3-59 months are 16% of the population; "
                "US$4.50 per child per season"]])

    benefit, cost, feas = tables(i0, RR)
    opt = mckp(benefit, cost, feas, BUDGET)
    acc = acceptable_packages(benefit, cost, feas, BUDGET, opt, TIE_TOL)

    rr_naive = dict(RR, **{"ITN+IRS": RR["ITN"] * RR["IRS"], "ITN+IRS+SMC": RR["ITN"] * RR["IRS"] * RR["SMC"]})
    nb, _, _ = tables(i0, rr_naive)
    naive = mckp(nb, cost, feas, BUDGET)
    naive_true = plan_value(naive["choice"], benefit)

    a_hat, r_hat = mle(history)
    db, _, _ = tables(a_hat, r_hat)
    data_opt = mckp(db, cost, feas, BUDGET)

    n_naive_diff = sum(naive["choice"][u] != opt["choice"][u] for u in opt["choice"])
    n_data_diff = sum(data_opt["choice"][u] not in acc["acceptable"][u] for u in opt["choice"])
    problems = []
    if n_naive_diff < 2 or naive_true > opt["objective"] * 0.99:
        problems.append(f"trap too weak: naive plan differs in {n_naive_diff} units, value ratio "
                        f"{naive_true / opt['objective']:.4f}")
    if n_data_diff > 1:
        problems.append(f"data do not identify the optimum: MLE plan differs in {n_data_diff} units")
    if problems and strict:
        raise SystemExit(f"{QUESTION} generator (seed {seed}): " + "; ".join(problems))

    truth = {
        "question": QUESTION, "seed": seed, "generator_version": 1,
        "allocation": {
            "kind": "categorical", "sense": "max", "budget": BUDGET, "units": [d for d, *_ in DISTRICTS],
            "packages": PACKAGES, "benefit": benefit, "cost": cost, "feasible": feas,
            "optimum": opt, "acceptable": acc["acceptable"], "tie_tol": TIE_TOL,
            "naive_optima": {"multiplicative_itn_irs": {
                "choice": naive["choice"], "true_objective": naive_true,
                "assumption": "ITN+IRS rate ratio = RR_ITN x RR_IRS"}},
            "data_optimum": {"choice": data_opt["choice"], "true_objective": plan_value(data_opt["choice"], benefit),
                             "units_outside_acceptable": n_data_diff},
            "benefit_unit": "clinical cases averted 2026-2028",
        },
        "headlines": {"total_cases_averted": {"basis": "own_plan", "value_of_optimum": opt["objective"]}},
        "parameters": {"baseline_incidence": i0, "rate_ratios": RR, "noise_sd": NOISE_SD,
                       "mle_rate_ratios": r_hat},
        "planted": ["ITN+IRS rate ratio 0.55 against 0.39 if multiplicative (IRS adds ~8% on top of nets)"],
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
    print("naive:  ", al["naive_optima"]["multiplicative_itn_irs"]["choice"],
          round(al["naive_optima"]["multiplicative_itn_irs"]["true_objective"]))
    print("data:   ", al["data_optimum"])
    print("mle RR: ", {k: round(v, 3) for k, v in t["parameters"]["mle_rate_ratios"].items()})
