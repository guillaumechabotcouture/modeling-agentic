#!/usr/bin/env python3
"""Test oracle library for agent-built engine admission (docs/design/agent-built-engines.md §4.3).

    python3 toolkit/engines/built/oracles.py --self-test

Module name ends in `oracles`, as `toolkit/import_guard.py`'s oracle policy requires. Everything
here is a test oracle (closed forms, fixed points, ODE solutions) — never an engine and never
exposed to production analysis. Re-exports the existing closed forms from the compartmental and
renewal oracle modules and adds the generic reductions listed in `requested.KNOWN_ORACLES`:
SIS endemic prevalence, constant-rate stable age distribution, constant-hazard survival, and
multigroup SEIR ODE. Nothing here names a disease.
"""
from __future__ import annotations

import math
import os
import sys
from collections.abc import Mapping

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from toolkit.engines.compartmental.benchmarks import oracles as _CO  # noqa: E402
from toolkit.engines.renewal.benchmarks import oracles as _RO  # noqa: E402


def oracle_final_size_lambert(r0: float, s: float = 1.0, seed: float = 0.0) -> float:
    """Kermack-McKendrick final attack rate via principal Lambert W branch."""
    return float(_CO.oracle_final_size_lambert(float(r0), s=float(s), seed=float(seed)))


def oracle_herd_immunity(r0: float, coverage: float | None = None, efficacy: float = 1.0,
                         denominator: str = "population") -> float:
    """When only `r0` is given: critical immunization threshold max(0, 1 - 1/R0).
    When `coverage` is given: post-immunization attack rate via compartmental oracle."""
    r0_f = float(r0)
    if coverage is None:
        return max(0.0, 1.0 - 1.0 / r0_f) if r0_f > 1.0 else 0.0
    return float(_CO.oracle_herd_immunity(r0_f, float(coverage), float(efficacy), denominator=denominator))


def oracle_wallinga_lipsitch(r0: float, gi, *, dt_days: float = 1.0, return_r: bool = True) -> float:
    """Wallinga-Lipsitch relation between R0 and exponential growth rate r.
    If `gi` is a positive scalar (mean exponential generation time), returns r = (R0 - 1) / gi
    (or R0 = 1 + r * gi when `return_r=False` and `r0` is interpreted as r).
    If `gi` is a sequence of discrete lag weights w_1..w_L, solves 1 / sum_s w_s exp(-r s dt) = R0
    for r via root-finding."""
    r0_f = float(r0)
    if isinstance(gi, (list, tuple, np.ndarray)):
        w = np.asarray(gi, dtype=float)
        w = w / np.sum(w)
        s = np.arange(1, len(w) + 1, dtype=float) * float(dt_days)
        if abs(r0_f - 1.0) < 1e-12:
            return 0.0
        from scipy.optimize import brentq
        fn = lambda r: float(1.0 / np.sum(w * np.exp(-r * s)) - r0_f)  # noqa: E731
        lo, hi = -2.0 / max(float(dt_days), 1e-6), 5.0 / max(float(dt_days), 1e-6)
        while fn(lo) > 0:
            lo *= 2.0
        while fn(hi) < 0:
            hi *= 2.0
        return float(brentq(fn, lo, hi, xtol=1e-12))
    gi_f = float(gi)
    if gi_f <= 0:
        raise ValueError("wallinga_lipsitch: gi must be positive")
    return float((r0_f - 1.0) / gi_f) if return_r else float(1.0 + r0_f * gi_f)


def oracle_branching_single(r0: float, offspring: str = "poisson") -> float:
    """Probability of a major outbreak from a single introduction."""
    return float(_CO.oracle_branching_single(float(r0), offspring=offspring))


def oracle_seir_growth_r0(r: float, latent_days: float, infectious_days: float) -> float:
    """R0 = (1 + r * L) * (1 + r * D) for SEIR with exponential latent/infectious periods."""
    return float(_CO.oracle_seir_growth_r0(float(r), float(latent_days), float(infectious_days)))


def oracle_final_size_multigroup(r0: float, contact, s0, *, N=None, seed=None, per_group: bool = False):
    """Multigroup final attack rate calibrated to next-generation spectral radius R0."""
    C = np.asarray(contact, dtype=float)
    s_vec = np.asarray(s0, dtype=float)
    G = len(s_vec)
    n_vec = np.ones(G, dtype=float) if N is None else np.asarray(N, dtype=float)
    e_vec = np.full(G, 1e-9, dtype=float) if seed is None else np.asarray(seed, dtype=float)
    rho = float(np.max(np.abs(np.linalg.eigvals(C))))
    q = float(r0) / max(rho, 1e-15)
    return _CO.oracle_final_size_multigroup(q, C, n_vec, s_vec, e_vec, per_group=per_group)


def oracle_ode_final_size(r0: float, s0: float, *, seed: float = 1e-9) -> float:
    """Final attack rate for a single-group SIR ODE starting from susceptible fraction s0."""
    return float(_RO.oracle_final_size(float(r0), s0=float(s0), seed=float(seed)))


def oracle_cohort_susceptible(coverage, seroconversion, dependence: str = "independent") -> float:
    """Residual susceptible fraction after sequential dose coverage and seroconversion."""
    return float(_CO.oracle_cohort_susceptible(coverage, seroconversion, dependence=dependence))


def oracle_sis_endemic(r0: float) -> float:
    """SIS endemic equilibrium prevalence: max(0, 1 - 1/R0)."""
    r0_f = float(r0)
    return max(0.0, 1.0 - 1.0 / r0_f) if r0_f > 1.0 else 0.0


def oracle_stable_age_dist(mortality, *, widths=None) -> list[float]:
    """Stationary age distribution of a constant-rate life table across age bands."""
    m = np.asarray(mortality, dtype=float)
    if m.ndim != 1 or len(m) == 0 or np.any(m < 0) or not np.all(np.isfinite(m)):
        raise ValueError("stable_age_dist: mortality must be a non-empty non-negative finite vector")
    w = np.ones_like(m) if widths is None else np.asarray(widths, dtype=float)
    if len(w) != len(m) or np.any(w <= 0):
        raise ValueError("stable_age_dist: widths must be positive with same length as mortality")
    # Constant hazard m_i over band width w_i: person-years L_i = S_{i-1} * (1 - exp(-m_i w_i)) / m_i (or w_i if m_i==0)
    surv = 1.0
    py = []
    for mi, wi in zip(m, w):
        if mi > 1e-12:
            li = surv * (1.0 - math.exp(-mi * wi)) / mi
            surv *= math.exp(-mi * wi)
        else:
            li = surv * wi
        py.append(li)
    arr = np.asarray(py, dtype=float)
    tot = float(np.sum(arr))
    return [float(x / tot) for x in arr]


def oracle_constant_hazard_survival(hazard: float, dt: float) -> float:
    """Survival probability exp(-hazard * dt) under constant hazard."""
    h, d = float(hazard), float(dt)
    if h < 0 or d < 0:
        raise ValueError("constant_hazard_survival: hazard and dt must be non-negative")
    return float(math.exp(-h * d))


def oracle_seir_multigroup_ode(r0: float, contact, s0, *, N=None, seed=None, sigma: float = 0.25,
                               gamma: float = 0.2, t_end: float = 1000.0, per_group: bool = False):
    """Multigroup SEIR ODE cumulative attack rate via scipy.integrate.solve_ivp."""
    from scipy.integrate import solve_ivp
    C = np.asarray(contact, dtype=float)
    s_vec = np.asarray(s0, dtype=float)
    G = len(s_vec)
    n_vec = np.ones(G, dtype=float) if N is None else np.asarray(N, dtype=float)
    e_vec = np.full(G, 1e-6, dtype=float) if seed is None else np.asarray(seed, dtype=float)
    rho = float(np.max(np.abs(np.linalg.eigvals(C))))
    beta = (float(r0) / max(rho, 1e-15)) * float(gamma)
    s_init = np.maximum(s_vec - e_vec, 0.0)
    e_init = np.zeros(G, dtype=float)
    i_init = np.minimum(e_vec, s_vec)
    cum_init = i_init.copy()
    y0 = np.concatenate([s_init, e_init, i_init, cum_init])

    def rhs(_t, y):
        sv, ev, iv = y[:G], y[G:2 * G], y[2 * G:3 * G]
        inf = beta * sv * (C @ iv)
        prog = float(sigma) * ev
        rec = float(gamma) * iv
        return np.concatenate([-inf, inf - prog, prog - rec, inf])

    sol = solve_ivp(rhs, (0.0, float(t_end)), y0, method="DOP853", rtol=1e-9, atol=1e-12)
    if not sol.success:
        raise RuntimeError(f"seir_multigroup_ode failed: {sol.message}")
    z = sol.y[3 * G:, -1]
    return [float(v) for v in z] if per_group else float(np.dot(z, n_vec) / np.sum(n_vec))


ORACLE_REGISTRY = {
    "final_size_lambert": oracle_final_size_lambert,
    "herd_immunity": oracle_herd_immunity,
    "wallinga_lipsitch": oracle_wallinga_lipsitch,
    "branching_single": oracle_branching_single,
    "seir_growth_r0": oracle_seir_growth_r0,
    "final_size_multigroup": oracle_final_size_multigroup,
    "ode_final_size": oracle_ode_final_size,
    "cohort_susceptible": oracle_cohort_susceptible,
    "sis_endemic": oracle_sis_endemic,
    "stable_age_dist": oracle_stable_age_dist,
    "constant_hazard_survival": oracle_constant_hazard_survival,
    "seir_multigroup_ode": oracle_seir_multigroup_ode,
}


def call_oracle(name: str, args: Mapping):
    """Dispatch a declared reduction oracle by name (fail closed on unknown oracle)."""
    fn = ORACLE_REGISTRY.get(str(name))
    if fn is None:
        raise KeyError(f"unknown built-engine oracle {name!r}")
    return fn(**dict(args or {}))


def _self_test() -> int:
    from toolkit.engines.built import requested as BREQ
    fails: list[str] = []

    def ok(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    ok(set(ORACLE_REGISTRY) == set(BREQ.KNOWN_ORACLES),
       f"OR1 oracle registry matches requested.KNOWN_ORACLES exactly ({len(ORACLE_REGISTRY)} oracles)")
    # Classic analytic identities
    z2 = call_oracle("final_size_lambert", {"r0": 2.0})
    ok(abs(z2 - (1.0 - math.exp(-2.0 * z2))) < 1e-12 and abs(z2 - 0.79681213002002) < 1e-8,
       f"OR2 final_size_lambert(2.0) satisfies z = 1 - exp(-2z) ({z2:.8f})")
    ok(abs(call_oracle("herd_immunity", {"r0": 4.0}) - 0.75) < 1e-12
       and abs(call_oracle("sis_endemic", {"r0": 2.5}) - 0.6) < 1e-12
       and abs(call_oracle("constant_hazard_survival", {"hazard": 0.2, "dt": 5.0}) - math.exp(-1.0)) < 1e-12,
       "OR3 herd_immunity, sis_endemic, and constant_hazard_survival match exact formulas")
    ok(abs(call_oracle("wallinga_lipsitch", {"r0": 2.0, "gi": 5.0}) - 0.2) < 1e-12
       and abs(call_oracle("seir_growth_r0", {"r": 0.1, "latent_days": 3.0, "infectious_days": 5.0}) - 1.95) < 1e-12,
       "OR4 wallinga_lipsitch and seir_growth_r0 match closed forms")
    ok(abs(call_oracle("branching_single", {"r0": 2.0}) - z2) < 1e-8
       and abs(call_oracle("cohort_susceptible", {"coverage": [0.5, 0.5], "seroconversion": [0.8, 0.8]}) - 0.36) < 1e-12,
       "OR5 branching_single (Poisson) matches final size and cohort_susceptible matches product")
    sad = call_oracle("stable_age_dist", {"mortality": [0.0, 0.0, 0.0]})
    ok(len(sad) == 3 and all(abs(x - 1.0 / 3.0) < 1e-12 for x in sad),
       f"OR6 stable_age_dist with zero mortality is uniform ({sad})")
    z_ode = call_oracle("ode_final_size", {"r0": 2.0, "s0": 1.0})
    z_mg = call_oracle("final_size_multigroup", {"r0": 2.0, "contact": [[0.5, 0.5], [0.5, 0.5]], "s0": [1.0, 1.0]})
    z_seir = call_oracle("seir_multigroup_ode", {"r0": 2.0, "contact": [[0.5, 0.5], [0.5, 0.5]], "s0": [1.0, 1.0]})
    ok(abs(z_ode - z2) < 1e-6 and abs(z_mg - z2) < 1e-6 and abs(z_seir - z2) < 1e-4,
       f"OR7 ode_final_size ({z_ode:.6f}), final_size_multigroup ({z_mg:.6f}), and seir_multigroup_ode ({z_seir:.6f}) agree")

    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    print(__doc__)
