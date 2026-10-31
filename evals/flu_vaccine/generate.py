#!/usr/bin/env python3
"""Generator for eval question `flu_vaccine` (synthetic; Phase 50 α).

Generating model (the answer key; never shipped to agents):
- One population of 5 M in five age groups (0-4, 5-17, 18-49, 50-64, 65+) mixing
  by a contact matrix (given to the agent). Age-structured SIR final size with
  all-or-nothing vaccine protection:
      A_i = s_i (1 - VE_i v_i) (1 - exp(-q sum_j C_ij A_j)),
  A_i the fraction of group i infected, s_i the pre-season susceptible fraction,
  v_i the fraction vaccinated, C_ij daily contacts of an i person with j people,
  q the transmissibility per contact (true q gives last season's attack rates).
- Hospitalisations = sum_i N_i A_i h_i. VE against infection by age 0.60, 0.65,
  0.60, 0.50, 0.35; hospitalisations per infection 0.004, 0.0008, 0.0015, 0.006, 0.03.
- PLANTED indirect protection: school-age children drive transmission, so doses given
  to 5-17 year-olds avert more hospitalisations in older adults than the same doses
  given to the 65+ directly. A direct-protection-only calculation sends the doses
  to the 65+.
- Data: last season's pre-season (susceptible titre) and post-season (seroconversion)
  serosurveys, 400 people per group each, and hospitalisations by age.
- Decision: 10 blocks of 75,000 doses across the five groups (at most 70% of a group
  vaccinated, whole blocks), minimising expected hospitalisations next season
  (same transmissibility and pre-season susceptibility as last season).

Truth: the exact optimum (all 10-block splits enumerated), the hospitalisations averted
by every split (value grid), the direct-only optimum (the trap) and the optimum from
the serosurvey estimates.
"""
from __future__ import annotations

import argparse
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _common import Rng, compositions, write_csv, write_truth  # noqa: E402

QUESTION = "flu_vaccine"
SEED = 20261102
HERE = os.path.dirname(os.path.abspath(__file__))
INPUTS = os.path.join(os.path.dirname(os.path.dirname(HERE)), "data", "evals", QUESTION)   # shipped
TRUTH = os.path.join(HERE, "_truth")   # never shipped
GROUPS = ["0-4", "5-17", "18-49", "50-64", "65+"]
POP_TOTAL = 5_000_000
SHARE = [0.07, 0.18, 0.43, 0.18, 0.14]
N = [int(POP_TOTAL * s) for s in SHARE]
# mean daily contacts reported by a person in row group with people in column group (before symmetrising)
M = [[2.0, 1.5, 3.0, 0.8, 0.4],
     [0.6, 8.0, 3.5, 1.0, 0.5],
     [0.5, 1.5, 6.0, 1.8, 0.7],
     [0.3, 1.0, 3.5, 2.5, 1.0],
     [0.2, 0.6, 1.8, 1.2, 1.8]]
S0 = [0.90, 0.75, 0.70, 0.65, 0.60]
VE = [0.60, 0.65, 0.60, 0.50, 0.35]
H = [0.004, 0.0008, 0.0015, 0.006, 0.03]
Q_TRUE = 0.165   # R_eff about 1.28 with the pre-season susceptibility; last-season attack 23%
BLOCKS, BLOCK_SIZE, UPTAKE_CAP = 10, 75_000, 0.70
SERO_N = 400
TIE_TOL = 0.005


def contact_matrix():
    """Reciprocal contacts: N_i C_ij = N_j C_ji."""
    k = len(GROUPS)
    return [[(N[i] * M[i][j] + N[j] * M[j][i]) / 2 / N[i] for j in range(k)] for i in range(k)]


def final_size(q, s, c, v=None, iters=5000):
    k = len(s)
    v = v or [0.0] * k
    a = [0.5 * s[i] for i in range(k)]
    for _ in range(iters):
        new = [s[i] * (1 - VE[i] * v[i]) * (1 - math.exp(-q * sum(c[i][j] * a[j] for j in range(k)))) for i in range(k)]
        if max(abs(x - y) for x, y in zip(new, a)) < 1e-13:
            return new
        a = new
    return a


def hospitalisations(q, s, c, h, blocks):
    v = [b * BLOCK_SIZE / N[i] for i, b in enumerate(blocks)]
    a = final_size(q, s, c, v)
    return sum(N[i] * a[i] * h[i] for i in range(len(s)))


def spectral_radius(q, s, c, iters=500):
    """Dominant eigenvalue of the next-generation matrix q diag(s) C (power iteration)."""
    k = len(s)
    x = [1.0] * k
    lam = 0.0
    for _ in range(iters):
        y = [q * s[i] * sum(c[i][j] * x[j] for j in range(k)) for i in range(k)]
        lam = max(y)
        x = [v / lam for v in y]
    return lam


def caps():
    return [int(UPTAKE_CAP * n // BLOCK_SIZE) for n in N]


def grid(q, s, c, h):
    base = hospitalisations(q, s, c, h, [0] * len(GROUPS))
    return {",".join(map(str, b)): base - hospitalisations(q, s, c, h, list(b))
            for b in compositions(BLOCKS, len(GROUPS), caps())}


def best(g: dict) -> str:
    return max(sorted(g), key=lambda k: g[k])


def fit_q(s, attack, c):
    """Least squares of the final-size equations in q (golden-section search)."""
    def loss(q):
        a = final_size(q, s, c)
        return sum((x - y) ** 2 for x, y in zip(a, attack))
    lo, hi = 0.01, 0.3
    gr = (math.sqrt(5) - 1) / 2
    for _ in range(80):
        m1, m2 = hi - gr * (hi - lo), lo + gr * (hi - lo)
        if loss(m1) < loss(m2):
            hi = m2
        else:
            lo = m1
    return (lo + hi) / 2


def generate(inputs_dir: str = INPUTS, truth_dir: str = TRUTH, seed: int = SEED, strict: bool = True) -> dict:
    rng = Rng(seed)
    c = contact_matrix()
    a_last = final_size(Q_TRUE, S0, c)
    sero_rows, hosp_rows = [], []
    s_hat, a_hat, h_hat = [], [], []
    for i, g in enumerate(GROUPS):
        k_sus = rng.binomial(SERO_N, S0[i])
        k_conv = rng.binomial(SERO_N, a_last[i])
        hosp = rng.poisson(N[i] * a_last[i] * H[i])
        sero_rows.append([g, SERO_N, k_sus, SERO_N, k_conv])
        hosp_rows.append([g, N[i], hosp])
        s_hat.append(k_sus / SERO_N)
        a_hat.append(k_conv / SERO_N)
        h_hat.append(hosp / (N[i] * a_hat[-1]))
    q_hat = fit_q(s_hat, a_hat, c)

    inp = inputs_dir
    write_csv(os.path.join(inp, "population.csv"), ["age_group", "population"], [[g, N[i]] for i, g in enumerate(GROUPS)])
    write_csv(os.path.join(inp, "contact_matrix.csv"), ["age_group"] + GROUPS,
              [[g] + [round(c[i][j], 4) for j in range(len(GROUPS))] for i, g in enumerate(GROUPS)])
    write_csv(os.path.join(inp, "serosurvey_last_season.csv"),
              ["age_group", "n_preseason", "n_preseason_susceptible_titre", "n_paired", "n_seroconverted"], sero_rows)
    write_csv(os.path.join(inp, "hospitalisations_last_season.csv"), ["age_group", "population", "influenza_hospitalisations"],
              hosp_rows)
    write_csv(os.path.join(inp, "vaccine_effectiveness.csv"), ["age_group", "ve_against_infection", "source"],
              [[g, VE[i], "test-negative studies, assumed known"] for i, g in enumerate(GROUPS)])

    vg = grid(Q_TRUE, S0, c, H)
    opt = best(vg)
    acceptable = sorted(k for k, v in vg.items() if v >= vg[opt] * (1 - TIE_TOL))
    # trap: direct protection only (no change in transmission)
    direct = {k: sum(int(b) * BLOCK_SIZE * VE[i] * a_last[i] * H[i] for i, b in enumerate(k.split(","))) for k in vg}
    naive = best(direct)
    dg = grid(q_hat, s_hat, c, h_hat)
    data_opt = best(dg)

    def l1(x, y):
        return sum(abs(int(p) - int(r)) for p, r in zip(x.split(","), y.split(",")))
    problems = []
    if l1(naive, opt) < 4 or vg[naive] > 0.95 * vg[opt]:
        problems.append(f"trap too weak: direct-only {naive} vs {opt}, ratio {vg[naive] / vg[opt]:.3f}")
    if min(l1(data_opt, x) for x in acceptable) > 2 or vg[data_opt] < 0.97 * vg[opt]:
        problems.append(f"data optimum {data_opt} far from {opt} (ratio {vg[data_opt] / vg[opt]:.3f})")
    if problems and strict:
        raise SystemExit(f"{QUESTION} generator (seed {seed}): " + "; ".join(problems))

    truth = {
        "question": QUESTION, "seed": seed, "generator_version": 1,
        "allocation": {
            "kind": "vector", "sense": "max", "units": GROUPS, "total_blocks": BLOCKS, "block_size": BLOCK_SIZE,
            "caps_blocks": caps(), "value_grid": vg, "optimum": {"blocks": opt, "objective": vg[opt]},
            "acceptable": acceptable, "tie_tol": TIE_TOL,
            "naive_optima": {"direct_protection_only": {"blocks": naive, "true_objective": vg[naive],
                                                        "assumption": "vaccination does not change transmission"}},
            "data_optimum": {"blocks": data_opt, "true_objective": vg[data_opt]},
            "benefit_unit": "influenza hospitalisations averted next season",
        },
        "headlines": {"hospitalisations_averted": {"basis": "own_plan", "value_of_optimum": vg[opt]}},
        "parameters": {"q": Q_TRUE, "q_hat": q_hat, "s0": S0, "attack_last_season": a_last,
                       "r0": spectral_radius(Q_TRUE, [1.0] * len(GROUPS), c),
                       "r_eff_preseason": spectral_radius(Q_TRUE, S0, c), "hosp_per_infection": H, "ve": VE},
        "planted": ["indirect protection: doses to 5-17 avert more hospitalisations than doses to 65+"],
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
    print("optimum:", al["optimum"], "acceptable:", al["acceptable"])
    print("naive:  ", al["naive_optima"]["direct_protection_only"])
    print("data:   ", al["data_optimum"], "q", Q_TRUE, "q_hat", round(t["parameters"]["q_hat"], 4))
    print("attack last season:", [round(x, 3) for x in t["parameters"]["attack_last_season"]])
