#!/usr/bin/env python3
"""Generator for eval question `dengue_outbreak` (synthetic; Phase 50 α).

The question where the data cannot identify a key quantity. Generating model (the
answer key; never shipped to agents):
- A city of 1.2 M; one DENV-2 outbreak in 2025. Discrete-time renewal SIR with a
  weekly generation-interval distribution w = (0.15, 0.35, 0.30, 0.20) for lags 1-4
  weeks (mean 2.55 weeks; given to the agent): inf_t = S_t (1 - exp(-R0 sum_k w_k
  inf_{t-k} / N)), S_{t+1} = S_t - inf_t, 200 infections in week 1. R0 = 3.6 and a
  pre-outbreak susceptible fraction s0 = 0.55 (so R_eff at the start = 1.98).
- Reported cases_t ~ Poisson(rho inf_t exp(e)), e ~ N(0, 0.15), rho = 0.04 (4% of
  infections are reported).
- PLANTED non-identifiability: the weekly curve's shape fixes R0 s0 and its height
  fixes rho s0, so R_eff for 2026 (= R0 s0 (1 - z), about 0.41) IS identified, but the
  2025 attack rate (s0 z, about 0.44) and the remaining susceptible fraction are NOT:
  they depend on rho (or s0), which nothing in the data pins down. No serosurvey is
  supplied. A modeller who assumes a typical reporting ratio (say 1 in 10) gets an
  attack rate of about 18% against the true 44%.

Truth: R_eff 2026, the attack rate, the range of attack rates consistent with the
data for reporting fractions 1/30 to 1/3, and the fitted R_eff 2026 from the data alone.
"""
from __future__ import annotations

import argparse
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _common import Rng, final_size, write_csv, write_truth  # noqa: E402

QUESTION = "dengue_outbreak"
SEED = 20261103
HERE = os.path.dirname(os.path.abspath(__file__))
INPUTS = os.path.join(os.path.dirname(os.path.dirname(HERE)), "data", "evals", QUESTION)   # shipped
TRUTH = os.path.join(HERE, "_truth")   # never shipped
N = 1_200_000
R0, S0, RHO = 3.6, 0.55, 0.04
SEED_INF = 200
GI = (0.15, 0.35, 0.30, 0.20)
WEEKS = 52
NOISE_SD = 0.15
RHO_RANGE = (1 / 30, 1 / 3)


def simulate(r_eff0: float, seed_inf: float, weeks: int = WEEKS, n_sus: float = N * S0) -> list[float]:
    """New infections per week. Depends on R0 and s0 only through R0 s0 when
    expressed per susceptible (n_sus scales the curve)."""
    beta = r_eff0 / n_sus   # R0 / N = R_eff0 / (N s0)
    s, hist, out = n_sus - seed_inf, [seed_inf], [seed_inf]
    for _ in range(weeks - 1):
        force = sum(w * hist[-1 - k] for k, w in enumerate(GI) if k < len(hist))
        inf = s * (1 - math.exp(-beta * force))
        s -= inf
        hist.append(inf)
        out.append(inf)
    return out


def fit_shape(cases: list[int]) -> dict:
    """Grid least squares on log1p cases over R_eff0 and seed (the scale is free)."""
    best = None
    for k in range(110, 300):
        r = k / 100
        for seed in (25, 50, 100, 150, 200, 300, 400, 600):
            curve = simulate(r, seed)
            # best scale for this shape (least squares on counts), then log-residuals
            num = sum(c * x for c, x in zip(cases, curve))
            den = sum(x * x for x in curve) or 1
            sc = num / den
            loss = sum((math.log1p(c) - math.log1p(sc * x)) ** 2 for c, x in zip(cases, curve))
            if best is None or loss < best[0]:
                best = (loss, r, seed, sc)
    _, r, seed, sc = best
    depleted = sum(simulate(r, seed)) / (N * S0)
    return {"r_eff0": r, "seed": seed, "scale_rho_s0_N": sc, "reff_2026": r * (1 - depleted)}


def generate(inputs_dir: str = INPUTS, truth_dir: str = TRUTH, seed: int = SEED, strict: bool = True) -> dict:
    rng = Rng(seed)
    inf = simulate(R0 * S0, SEED_INF)
    cases = [rng.poisson(RHO * x * math.exp(rng.normal(0, NOISE_SD))) for x in inf]
    total_inf = sum(inf)
    attack = total_inf / N
    s_end = S0 - attack
    reff_2026 = R0 * s_end
    reported = sum(cases)
    lo, hi = reported / (RHO_RANGE[1] * N), min(reported / (RHO_RANGE[0] * N), final_size(R0 * S0))
    fit = fit_shape(cases)

    inp = inputs_dir
    write_csv(os.path.join(inp, "city.csv"), ["city", "population_2025", "serotype_2025"], [["Synthetic City", N, "DENV-2"]])
    write_csv(os.path.join(inp, "generation_interval.csv"), ["lag_weeks", "probability"],
              [[k + 1, w] for k, w in enumerate(GI)])
    write_csv(os.path.join(inp, "weekly_reported_cases_2025.csv"), ["epi_week", "reported_dengue_cases"],
              [[w + 1, c] for w, c in enumerate(cases)])

    problems = []
    if abs(fit["reff_2026"] - reff_2026) > 0.1 * reff_2026:
        problems.append(f"R_eff 2026 not identified from the curve: fit {fit['reff_2026']:.3f} vs {reff_2026:.3f}")
    if not (lo < attack < hi) or hi / lo < 3:
        problems.append(f"attack-rate range [{lo:.3f}, {hi:.3f}] does not bracket {attack:.3f} widely")
    if problems and strict:
        raise SystemExit(f"{QUESTION} generator (seed {seed}): " + "; ".join(problems))

    truth = {
        "question": QUESTION, "seed": seed, "generator_version": 1,
        "headlines": {
            "reff_2026": {"basis": "fixed", "value": reff_2026, "reportable": True},
            "attack_rate_2025": {"basis": "fixed", "value": attack, "reportable": False,
                                 "consistent_range": [lo, hi],
                                 "naive_value": reported / (0.1 * N)},
        },
        "parameters": {"r0": R0, "s0": S0, "rho": RHO, "r_eff0": R0 * S0, "total_infections": total_inf,
                       "total_reported": reported, "fit_from_data": fit},
        "planted": ["case data identify R0*s0 and rho*s0 only: R_eff 2026 identified, 2025 attack rate not"],
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
    print({k: v for k, v in t["headlines"].items()})
    print(t["parameters"])
