#!/usr/bin/env python3
"""Test oracles and probes for the compartmental engine's benchmark pack (design note v2d §1.1, §5).

    python3 toolkit/engines/compartmental/benchmarks/oracles.py --self-test

Everything here is a TEST ORACLE: closed forms (Lambert W final size, the herd-immunity identity,
Wallinga-Lipsitch, branching-process extinction), the multigroup final-size fixed point and a
multigroup SIR ODE (`scipy.integrate.solve_ivp`). None of it is an engine and none of it is exposed
to runs (owner decision 2026-11-01): the pack compares the Starsim engine with these.

Two kinds of function, both called by the benchmark runner (toolkit/engines/benchmark.py):
- `oracle_*(**args)`: a reference value computed independently of the engine;
- `probe_*(make_engine, **args)`: a value read from the engine (or from the harness observation
  forward maps) that is not an `Outcomes` path, e.g. `initial_state()` or `run_batch()`.
  `make_engine(config_name)` returns the pack's configured engine.
- `generate_*` / the SB cases: synthetic-data generators for the coverage pack (`coverage.yaml`),
  independent of the engine and of the observation code under test.

Nothing here names a disease.
"""
from __future__ import annotations

import math
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ------------------------------------------------------------------------------ closed forms
def oracle_final_size_lambert(r0: float, s: float = 1.0, seed: float = 0.0) -> float:
    """Single-group final size, seeds included, whole-population denominator.

    S_inf = (s - seed) exp(-R0 z) and z = s - S_inf (fractions of N), so
    z = s + W0(-R0 (s - seed) exp(-R0 s)) / R0 (principal branch). With s = 1 and seed -> 0 this is
    Kermack-McKendrick's z = 1 - exp(-R0 z)."""
    from scipy.special import lambertw
    r0, s, seed = float(r0), float(s), float(seed)
    arg = -r0 * (s - seed) * math.exp(-r0 * s)
    return float(s + lambertw(arg, 0).real / r0)


def oracle_herd_immunity(r0: float, coverage: float, efficacy: float, denominator: str = "population") -> float:
    """All-or-nothing vaccination of a share v with efficacy e (seed -> 0): s = 1 - v e,
    x = 1 - exp(-R0 s x) is the attack rate among those left susceptible, z = s x the whole-population
    attack rate; both are zero when v e >= 1 - 1/R0 (the herd-immunity threshold)."""
    s = 1.0 - float(coverage) * float(efficacy)
    if s * float(r0) <= 1.0:
        return 0.0
    z = oracle_final_size_lambert(r0, s=s, seed=0.0)
    return float(z if denominator == "population" else z / s)


def oracle_seed_bound(r0: float, seed: float) -> float:
    """Threshold theorem: for R0 < 1 the whole chain from a seed has expected size seed / (1 - R0)."""
    return float(seed) / (1.0 - float(r0))


def oracle_wallinga_lipsitch(w, r: float, dt_days: float = 1.0) -> float:
    """R = 1 / sum_s w_s exp(-r s dt), w on the step grid (s = 1..L), r per day (Wallinga & Lipsitch 2007)."""
    w = np.asarray(w, float)
    s = np.arange(1, len(w) + 1) * float(dt_days)
    return float(1.0 / np.sum(w * np.exp(-float(r) * s)))


def oracle_branching_single(r0: float, offspring: str = "poisson") -> float:
    """Probability of a major outbreak from one case, one group. Poisson: 1 - p = exp(-R0 p);
    geometric: p = 1 - 1/R0. Zero at R0 <= 1."""
    r0 = float(r0)
    if r0 <= 1.0:
        return 0.0
    if offspring == "geometric":
        return 1.0 - 1.0 / r0
    from scipy.optimize import brentq
    return float(brentq(lambda p: 1.0 - p - math.exp(-r0 * p), 1e-12, 1.0))


def oracle_constant(value: float) -> float:
    return float(value)


def oracle_minimum(values) -> float:
    return float(min(float(v) for v in values))


def oracle_difference(a, b) -> float:
    return float(a) - float(b)


def oracle_product(a, b) -> float:
    return float(a) * float(b)


def oracle_ratio(a, b) -> float:
    return float(a) / float(b)


def oracle_cosine_forcing_r(r0: float, amplitude: float, phase: float, period: float, t: float) -> float:
    """R0 f(t) with the declared cosine forcing f(t) = 1 + a cos(2 pi (t - phase) / period)."""
    return float(r0) * (1.0 + float(amplitude) * math.cos(2.0 * math.pi * (float(t) - float(phase)) / float(period)))


# ------------------------------------------------------------------------- multigroup final size
def _contact(C, N, structure="matrix", c=None):
    C = np.asarray(C, float) if C is not None else None
    N = np.asarray(N, float)
    if structure == "homogeneous":
        return float(c) * np.tile(N / N.sum(), (len(N), 1))
    return C


def oracle_final_size_multigroup(q: float, C, N, s, seed, per_group: bool = True):
    """Multigroup final size (seeds included): z_i = s_i - (s_i - e_i) exp(-q sum_j C_ij z_j), with z, s, e
    fractions of N_i and e the seed. Solved from z = e upward (the minimal solution, i.e. only what the
    seeds can reach), then polished by Newton. Returns per-group z (list) or the whole-population share."""
    from scipy.optimize import root
    C = np.asarray(C, float)
    N = np.asarray(N, float)
    s = np.asarray(s, float)
    e = np.asarray(seed, float)
    z = e.copy()
    for _ in range(200000):
        zn = s - (s - e) * np.exp(-q * C @ z)
        if np.max(np.abs(zn - z)) < 1e-15:
            z = zn
            break
        z = zn
    sol = root(lambda x: x - (s - (s - e) * np.exp(-q * C @ x)), z, method="hybr", tol=1e-15)
    if sol.success and np.max(np.abs(sol.x - z)) < 1e-6:
        z = sol.x
    return [float(v) for v in z] if per_group else float(np.dot(z, N) / N.sum())


def oracle_final_size_rank_one(q: float, a, b, N, s, seed, per_group: bool = True):
    """Rank-one (proportionate) mixing C_ij = a_i b_j: the multigroup relation reduces to the scalar
    theta = sum_j b_j (s_j - (s_j - e_j) exp(-q a_j theta)), then z_i = s_i - (s_i - e_i) exp(-q a_i theta)
    (Ma & Earn 2006; Andreasen 2011). The smallest root above zero is the seeded epidemic."""
    from scipy.optimize import brentq
    a, b, N, s, e = (np.asarray(x, float) for x in (a, b, N, s, seed))
    g = lambda th: float(np.sum(b * (s - (s - e) * np.exp(-q * a * th))) - th)  # noqa: E731
    hi = float(np.sum(b * s)) + 1e-12
    grid = np.linspace(0.0, hi, 200001)
    vals = (b[None, :] * (s - (s - e) * np.exp(-q * a[None, :] * grid[:, None]))).sum(axis=1) - grid
    cross = np.nonzero((vals[:-1] > 0) & (vals[1:] <= 0))[0]
    if not len(cross):
        raise ValueError("rank-one oracle: no root")
    k = int(cross[0])
    th = brentq(g, grid[k], grid[k + 1], xtol=1e-16, rtol=1e-15, maxiter=500)
    z = s - (s - e) * np.exp(-q * a * th)
    return [float(v) for v in z] if per_group else float(np.dot(z, N) / N.sum())


# ------------------------------------------------------------------------------- reference ODE
def ode_sir(q: float, C, N, s, seed, gamma: float, t_end: float, t_eval=None, rtol=1e-10, atol=1e-14,
            forcing=None):
    """Multigroup SIR ODE in fractions of each group: ds_i/dt = -beta f(t) s_i sum_j C_ij i_j,
    di_i/dt = beta f(t) s_i sum_j C_ij i_j - gamma i_i, with beta = q gamma (q per contact over the
    infectious period). Seeds start infectious: s(0) = s - e, i(0) = e. Returns (t, s(t), i(t), cum(t))
    with cum the cumulative infections (seeds included) per group."""
    from scipy.integrate import solve_ivp
    C = np.asarray(C, float)
    s0 = np.asarray(s, float) - np.asarray(seed, float)
    i0 = np.asarray(seed, float)
    G = len(s0)
    beta = q * gamma
    f = forcing or (lambda t: 1.0)

    def rhs(t, y):
        sv, iv = y[:G], y[G:2 * G]
        inf = beta * f(t) * sv * (C @ iv)
        return np.concatenate([-inf, inf - gamma * iv, inf])
    y0 = np.concatenate([s0, i0, i0])
    sol = solve_ivp(rhs, (0.0, float(t_end)), y0, method="DOP853", rtol=rtol, atol=atol, t_eval=t_eval,
                    dense_output=t_eval is None)
    if not sol.success:
        raise RuntimeError(f"reference ODE failed: {sol.message}")
    return sol.t, sol.y[:G], sol.y[G:2 * G], sol.y[2 * G:]


def oracle_ode_final_size(q: float, C, N, s, seed, gamma: float = 0.2, per_group: bool = True,
                          t_max: float = 1e5):
    """The reference ODE run to extinction (prevalence below 1e-14 in every group), final size per group."""
    t_end = 200.0 / gamma
    while True:
        t, sv, iv, cum = ode_sir(q, C, N, s, seed, gamma, t_end)
        if np.max(iv[:, -1]) < 1e-14 or t_end >= t_max:
            break
        t_end *= 2
    z = cum[:, -1]
    N = np.asarray(N, float)
    return [float(v) for v in z] if per_group else float(np.dot(z, N) / N.sum())


def oracle_ode_growth_r0(q: float, C, N, s, gamma: float = 0.2, seed_scale: float = 1e-20,
                         t1: float = None, t2: float = None) -> float:
    """R0 consistent with the reference ODE's early growth: for SIR with an exponential infectious period,
    R = 1 + r / gamma (Wallinga-Lipsitch for an exponential generation interval), r from the log-slope of
    total prevalence between t1 and t2 in the linear phase (seeds tiny, transients gone)."""
    C = np.asarray(C, float)
    G = len(N)
    seed = np.full(G, seed_scale)
    rho = float(np.max(np.linalg.eigvals(np.diag(np.asarray(s, float)) @ C).real))
    r_guess = q * gamma * rho - gamma
    t1 = t1 if t1 is not None else 20.0 / max(abs(r_guess), gamma)
    t2 = t2 if t2 is not None else t1 + 10.0 / max(abs(r_guess), gamma)
    t, sv, iv, cum = ode_sir(q, C, N, s, seed, gamma, t2, t_eval=[t1, t2], rtol=1e-12, atol=1e-40)
    tot = iv.sum(axis=0)
    r = math.log(tot[1] / tot[0]) / (t2 - t1)
    return 1.0 + r / gamma


def ode_weekly_incidence(q: float, C, N, s, seed, gamma: float, weeks: int, days_per_week: float = 7.0):
    """Weekly new infections (whole population, fraction of N) from the reference ODE, aggregated to weeks."""
    N = np.asarray(N, float)
    tt = np.arange(weeks + 1) * days_per_week
    t, sv, iv, cum = ode_sir(q, C, N, s, seed, gamma, tt[-1], t_eval=tt)
    tot = (cum * N[:, None]).sum(axis=0) / N.sum()
    return np.diff(tot)


def discretised_exponential_gi(gamma: float, dt_days: float, L: int):
    """Generation interval of SIR (density gamma exp(-gamma tau)) on a grid of step dt: the mass of each
    step (s-1, s] dt, truncated at L steps and renormalised to sum to 1."""
    edges = np.arange(L + 1) * dt_days
    w = np.exp(-gamma * edges[:-1]) - np.exp(-gamma * edges[1:])
    return [float(x) for x in w / w.sum()]


def discretised_exponential_gi_triangle(gamma: float, dt_days: float, L: int):
    """The same density discretised for a renewal model whose infections are binned by step: with both the
    infector's and the infectee's times uniform within their steps, the lag in steps s has weight
    integral (1 - |tau/dt - s|) g(tau) over ((s-1) dt, (s+1) dt); the same-step mass (s = 0) is folded into
    w_1 (the engine's renewal starts at lag 1). The interval form above over-states R by about 6% at
    R0 = 1.8 with a 7-day infectious period on a daily grid (pack run 2, RN4); this form by about 1%."""
    from scipy.integrate import quad
    g = lambda t: gamma * math.exp(-gamma * t)  # noqa: E731
    d = float(dt_days)
    w = [quad(lambda t: (1 - abs(t / d - s)) * g(t), max(s - 1, 0) * d, (s + 1) * d)[0] for s in range(1, L + 1)]
    w[0] += quad(lambda t: (1 - t / d) * g(t), 0.0, d)[0]
    w = np.asarray(w)
    return [float(x) for x in w / w.sum()]


# ------------------------------------------------------------------------- the linear-form control
def oracle_linear_renewal_attack(r0: float, w, seed: float = 1e-9, steps: int = 100000) -> float:
    """The NEGATIVE CONTROL of RN2: the linear renewal form I_t = (S_{t-1}/N) R0 sum_s w_s I_{t-s},
    S_t = S_{t-1} - I_t (Codex review finding 2). It does not satisfy the final-size relation: with
    w_1 = 1 and R0 = 2 it ends near 0.8687 against the exact 0.7968."""
    w = np.asarray(w, float)
    L = len(w)
    hist = np.zeros(L)
    hist[0] = seed
    S, cum = 1.0 - seed, seed
    for _ in range(steps):
        inc = S * r0 * float(np.dot(w, hist))
        inc = min(inc, S)
        S -= inc
        cum += inc
        hist = np.roll(hist, 1)
        hist[0] = inc
        if inc < 1e-16 and np.sum(hist) < 1e-15:
            break
    return float(cum)


def oracle_cohort_susceptible(coverage, seroconversion, dependence: str = "independent") -> float:
    """Hand-computable susceptibility after doses (CI1 reference, written independently of the engine):
    independent: prod_k (1 - c_k e_k); nested: a single propensity u ~ U(0,1), dose k reaches u < c_k,
    so the share left susceptible is the integral over u of prod_{k: u < c_k} (1 - e_k)."""
    c = [float(x) for x in coverage]
    e = [float(x) for x in seroconversion]
    if dependence == "independent":
        out = 1.0
        for ck, ek in zip(c, e):
            out *= 1.0 - ck * ek
        return out
    cuts = sorted(set([0.0, 1.0] + [min(max(x, 0.0), 1.0) for x in c]))
    out = 0.0
    for lo, hi in zip(cuts[:-1], cuts[1:]):
        mid = 0.5 * (lo + hi)
        prod = 1.0
        for ck, ek in zip(c, e):
            if mid < ck:
                prod *= 1.0 - ek
        out += (hi - lo) * prod
    return out


# ------------------------------------------------------------------------------------- probes
def _engine_args(unit, package=None, horizon=None):
    from toolkit.engines.contract import Horizon, Package, UnitSpec
    h = horizon or {"start": 0, "stop": 1000, "window": [0, 1000], "windows": {}}
    return (UnitSpec("probe", dict(unit)), Package("p", dict(package or {})),
            Horizon(float(h["start"]), float(h["stop"]), tuple(h["window"]), dict(h.get("windows") or {})))


def probe_initial_state(make_engine, config: str, unit: dict, field: str = "s_initialiser", group: str = None,
                        package: dict = None, params: dict = None):
    """A value of `initial_state(params, unit, package)`: one group's entry, or the whole vector."""
    eng = make_engine(config)
    u, p, _ = _engine_args(unit, package)
    st = eng.initial_state(params or {}, u, p)
    vec = st[field]
    if group is None:
        return [float(x) for x in vec]
    return float(vec[eng.groups.index(group)])


def probe_initial_state_vs_sim(make_engine, config: str, unit: dict, package: dict = None, params: dict = None,
                               horizon: dict = None) -> float:
    """CI4: max |initial_state().s_start - the sim's susceptible share at its first step| over groups."""
    eng = make_engine(config)
    u, p, h = _engine_args(unit, package, horizon)
    st = eng.initial_state(params or {}, u, p)
    o = eng.run(params or {}, u, p, h, 1)
    return float(np.max(np.abs(np.asarray(st["s_start"]) - np.asarray(o.meta["s_start"]))))


def probe_cohort_shift(make_engine, config: str, unit: dict, shift_years: float = 1.0) -> float:
    """CI2: the largest change in any birth cohort's susceptibility when the decision year moves by
    `shift_years` with the same birth-year-indexed inputs (zero by construction under birth-year indexing)."""
    eng = make_engine(config)
    u0 = dict(unit)
    u1 = dict(unit, decision_year=float(unit["decision_year"]) + shift_years)
    a = probe_initial_state(make_engine, config, u0)
    b = probe_initial_state(make_engine, config, u1)
    del eng
    return float(np.max(np.abs(np.asarray(a) - np.asarray(b))))


def probe_batch_vs_run(make_engine, config: str, unit: dict, param_sets: list, packages: list,
                       horizon: dict, path: str) -> float:
    """Largest relative difference of an output between run_batch() and run(), pair by pair."""
    from toolkit.engines.benchmark import get
    eng = make_engine(config)
    u, _, h = _engine_args(unit, None, horizon)
    from toolkit.engines.contract import Package
    pk = [Package(f"p{i}", dict(x)) for i, x in enumerate(packages)]
    outs = eng.run_batch(param_sets, u, pk, h, 1)
    worst = 0.0
    for prm, p, ob in zip(param_sets, pk, outs):
        o = eng.run(prm, u, p, h, 1)
        a = get(_res(ob), path)
        b = get(_res(o), path)
        worst = max(worst, abs(a - b) / max(abs(b), 1e-300))
    return worst


def _res(o):
    return {"per_capita": o.per_capita, "windows": o.windows, "cost_drivers": o.cost_drivers, "meta": o.meta}


def probe_observation(make_engine, mechanism: str, **args) -> float:
    """OB1: one bias mechanism's forward map (toolkit/analysis/observation.py)."""
    from toolkit.analysis import observation as ob
    return float(ob.forward(mechanism, **args))


def probe_band_positivity(make_engine, immune, sizes, se: float = 1.0, sp: float = 1.0) -> float:
    """CI3: a mixed-cohort survey band's expected positivity from the observation forward map."""
    from toolkit.analysis import observation as ob
    return float(ob.band_positivity(immune, sizes, se=se, sp=sp))


def oracle_weighted_mean(values, weights) -> float:
    v, w = np.asarray(values, float), np.asarray(weights, float)
    return float(np.dot(v, w) / w.sum())


def probe_refit_r0(make_engine, config: str, r0: float, gamma: float, weeks: int, seed: float,
                   start: list = (1.5, -18.0)) -> float:
    """RN4: a weekly series simulated by the reference ODE (single group, fraction of N), refitted with the
    engine in windowed mode by least squares on log incidence over (log R0, log seed); returns R0 recovered."""
    from scipy.optimize import minimize
    eng = make_engine(config)
    y = ode_weekly_incidence(r0, [[1.0]], [1.0], [1.0], [seed], gamma, weeks)
    from toolkit.engines.contract import Horizon, Package, UnitSpec
    dt = eng.dt_days
    h = Horizon(0.0, weeks * 7.0, (0.0, weeks * 7.0), {})
    pk = Package("none", {})
    keep = y > 1e-12

    def sse(x):
        u = UnitSpec("fit", {"N": [1.0], "s0": [1.0], "seed": float(math.exp(x[1]))})
        o = eng.run({"r0": float(math.exp(x[0]))}, u, pk, h, 1)
        inc = np.asarray(o.meta["incidence"], float)        # per step, fraction of N, steps 1..T
        per_week = int(round(7.0 / dt))
        wk = inc[:len(y) * per_week].reshape(len(y), per_week).sum(axis=1)
        return float(np.sum((np.log(np.maximum(wk[keep], 1e-300)) - np.log(y[keep])) ** 2))
    best = None
    for x0 in ([math.log(start[0]), start[1]], [math.log(r0 * 0.8), math.log(seed) + 1.0]):
        res = minimize(sse, x0, method="Nelder-Mead", options={"xatol": 1e-8, "fatol": 1e-12, "maxiter": 4000})
        if best is None or res.fun < best.fun:
            best = res
    return float(math.exp(best.x[0]))


# ---------------------------------------------------------------- coverage-pack generators (coverage.yaml)
# Independent of the engine and of the observation code under test: the renewal cases use the reference ODE,
# the cohort case draws latent coverage and each source's frame and biases directly, and SB5 uses the
# final-size fixed point. Each returns (data the calibration sees, truth the interval must cover).
def _loguniform(rng, lo, hi):
    return float(math.exp(rng.uniform(math.log(lo), math.log(hi))))


def generate_renewal_series(rng, r0_range=(1.3, 2.5), reporting_range=(0.0333, 0.333), gamma=1 / 7.0,
                            weeks=30, population=1.0e6, seed_share=1.0e-5, dispersion=20.0, s0=1.0,
                            s0_range=None, forcing_amplitude_range=None, forcing_phase=0.0, forcing_period=365.0,
                            truncate_weeks=None):
    """SB1/SB2: weekly reported cases from the reference ODE (single group), negative-binomial reporting
    noise around reporting x true infections. `s0_range` makes the initial susceptible share unknown
    (drawn); `forcing_amplitude_range` adds an unknown (drawn) cosine forcing; `truncate_weeks` ends the
    series before depletion. Truth: R0, the attack rate and reff_end over the observed window, reporting."""
    r0 = float(rng.uniform(*r0_range))
    rho = _loguniform(rng, *reporting_range)
    s = float(rng.uniform(*s0_range)) if s0_range else float(s0)
    amp = float(rng.uniform(*forcing_amplitude_range)) if forcing_amplitude_range else 0.0
    f = (lambda t: 1.0 + amp * math.cos(2 * math.pi * (t - forcing_phase) / forcing_period)) if amp else None
    n_obs = int(truncate_weeks or weeks)
    tt = np.arange(n_obs + 1) * 7.0
    t, sv, iv, cum = ode_sir(r0, [[1.0]], [1.0], [s], [seed_share], gamma, tt[-1], t_eval=tt, forcing=f)
    inc = np.diff(cum[0])
    mu = np.maximum(rho * population * inc, 1e-12)
    p = dispersion / (dispersion + mu)
    cases = rng.negative_binomial(dispersion, p)
    s_end = float(sv[0, -1])
    f_end = f(tt[-1]) if f else 1.0
    truth = {"r0": r0, "reporting": rho, "attack_rate": float(cum[0, -1] - cum[0, 0] + seed_share),
             "reff_end": r0 * f_end * s_end, "s0": s, "forcing_amplitude": amp}
    data = {"weekly_cases": [int(x) for x in cases], "population": population, "weeks": n_obs,
            "gamma": gamma, "known": {"s0": None if s0_range else s, "forcing": "none" if not forcing_amplitude_range
                                      else "cosine (amplitude unknown)"}}
    return data, truth


def generate_cohort_sources(rng, n_cohorts=5, coverage_beta=(8.0, 2.0), omega_prior=(0.0, 0.25),
                            delta_prior=(-0.10, 0.25), se=(0.92, 0.98), sp=(0.95, 0.995), wane_rate=(0.0, 0.05),
                            out_of_frame_share=0.06, out_of_frame_rel=(0.5, 1.0), survey_n=300, design_effect=2.1,
                            admin_only=False, seroconversion=0.93, decision_year=2026, survey_year=2025):
    """SB3: latent true coverage of one dose by birth cohort; an administrative source (numerator inflation,
    denominator error, reporting noise) and, unless `admin_only`, a serosurvey of the in-frame population
    (assay Se/Sp, waning by years since vaccination, a frame gap with out-of-frame relative immunity,
    beta-binomial sampling with the survey's design effect). Truth: the unit's susceptible share."""
    years = np.arange(decision_year - n_cohorts, decision_year)
    c = rng.beta(*coverage_beta, n_cohorts)
    omega = float(rng.uniform(*omega_prior))
    delta = float(rng.uniform(*delta_prior))
    admin = np.clip(c * (1 + omega) / (1 + delta) * np.exp(rng.normal(0, 0.03, n_cohorts)), 0, None)
    immune_frame = c * seroconversion
    rel = float(rng.uniform(*out_of_frame_rel))
    immune_unit = (1 - out_of_frame_share) * immune_frame + out_of_frame_share * np.minimum(rel * immune_frame, 1)
    data = {"birth_year": [int(y) for y in years], "admin_coverage": [float(x) for x in admin],
            "decision_year": decision_year, "survey_year": survey_year, "seroconversion": seroconversion}
    if not admin_only:
        se_, sp_ = float(rng.uniform(*se)), float(rng.uniform(*sp))
        wr = float(rng.uniform(*wane_rate))
        age = np.maximum(survey_year - years - 1, 0)
        pos = se_ * immune_frame * np.exp(-wr * age) + (1 - sp_) * (1 - immune_frame * np.exp(-wr * age))
        n_eff = max(int(survey_n / design_effect), 1)
        rho_icc = (design_effect - 1) / max(survey_n - 1, 1)
        a = pos * (1 / max(rho_icc, 1e-9) - 1)
        b = (1 - pos) * (1 / max(rho_icc, 1e-9) - 1)
        pk = rng.beta(np.maximum(a, 1e-6), np.maximum(b, 1e-6))
        data.update(sero_n=survey_n, sero_positive=[int(x) for x in rng.binomial(survey_n, pk)],
                    design_effect=design_effect, out_of_frame_share=out_of_frame_share, n_effective=n_eff)
    truth = {"susceptible_share": float(np.mean(1 - immune_unit)), "omega": omega, "delta": delta,
             "ratio": (1 + omega) / (1 + delta)}
    return data, truth


def generate_group_attack(rng, q_range=(0.4, 1.0), N=(3.0e5, 2.0e5, 1.0e5),
                          C=((2.0, 0.5, 0.2), (0.6, 1.5, 0.4), (0.3, 0.5, 1.0)), survey_n=400,
                          hosp_ratio=(0.002, 0.004, 0.02), plan_coverage=(0.0, 0.5, 0.0), efficacy=0.9):
    """SB5: last season's group attack rates (binomial serosurveys of `survey_n` per group) from the final-size
    fixed point at a drawn q; the decision quantity is hospitalisations averted by a fixed plan (all-or-nothing
    vaccination of a share of each group) next season, computed by the same independent oracle."""
    q = float(rng.uniform(*q_range))
    N, C = np.asarray(N, float), np.asarray(C, float)
    G = len(N)
    seed = np.zeros(G)
    seed[0] = 1e-6
    z = np.asarray(oracle_final_size_multigroup(q, C, N, np.ones(G), seed))
    obs = rng.binomial(survey_n, np.clip(z, 0, 1))
    s_next = 1 - z
    z0 = np.asarray(oracle_final_size_multigroup(q, C, N, s_next, seed))
    s_plan = s_next * (1 - np.asarray(plan_coverage) * efficacy)
    z1 = np.asarray(oracle_final_size_multigroup(q, C, N, s_plan, seed))
    hosp = np.asarray(hosp_ratio)
    averted = float(np.sum((z0 - z1) * N * hosp))
    data = {"N": N.tolist(), "contact": C.tolist(), "survey_n": survey_n, "positive": [int(x) for x in obs],
            "hosp_ratio": list(hosp), "plan_coverage": list(plan_coverage), "efficacy": efficacy}
    return data, {"q": q, "hospitalisations_averted": averted}


# --------------------------------------------------------------------------------- self-test
def _self_test() -> int:
    fails = []

    def ok(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    z2 = oracle_final_size_lambert(2.0)
    ok(abs(z2 - 0.796812130) < 1e-8, f"O1 Lambert W final size at R0 = 2 is 0.7968 ({z2:.9f})")
    ok(abs(z2 - (1 - math.exp(-2 * z2))) < 1e-14, "O2 it solves z = 1 - exp(-R0 z)")
    zs = oracle_final_size_lambert(2.0, seed=1e-6)
    ok(abs((1 - zs) - (1 - 1e-6) * math.exp(-2 * zs)) < 1e-14, "O3 the seeded closed form solves its relation")
    x = oracle_herd_immunity(3.0, 0.5, 1.0, "susceptible")
    zz = oracle_herd_immunity(3.0, 0.5, 1.0, "population")
    ok(abs(x - 0.5828) < 5e-5 and abs(zz - 0.2914) < 5e-5, f"O4 herd-immunity identity: x {x:.4f}, z {zz:.4f}")
    ok(oracle_herd_immunity(3.0, 0.7, 1.0) == 0.0, "O5 zero at v e >= 1 - 1/R0")
    lin = oracle_linear_renewal_attack(2.0, [1.0], seed=1e-9)
    ok(abs(lin - 0.8687) < 5e-4, f"O6 the linear renewal form ends near 0.8687 (got {lin:.4f})")
    ok(abs(oracle_wallinga_lipsitch([1.0], math.log(2.0)) - 2.0) < 1e-12, "O7 Wallinga-Lipsitch with w_1 = 1")
    p = oracle_branching_single(2.0)
    ok(abs(p - z2) < 1e-10 and abs(oracle_branching_single(2.0, "geometric") - 0.5) < 1e-15,
       "O8 Poisson extinction equals the final-size root; geometric gives 1 - 1/R0")
    C = np.array([[2.0, 0.5, 0.1], [0.4, 1.5, 0.3], [0.2, 0.6, 1.0]])
    N = [3.0, 2.0, 1.0]
    zm = oracle_final_size_multigroup(1.2, C, N, [1, 1, 1], [1e-9, 0, 0])
    ok(all(abs(zm[i] - (1 - (1 - [1e-9, 0, 0][i]) * math.exp(-1.2 * float(C[i] @ zm)))) < 1e-13 for i in range(3)),
       "O9 the multigroup fixed point solves its relation")
    a, b = np.array([1.0, 2.0, 0.5]), np.array([0.3, 0.2, 0.5])
    r1 = oracle_final_size_rank_one(1.5, a, b, N, [1, 1, 1], [1e-9, 0, 0])
    m1 = oracle_final_size_multigroup(1.5, np.outer(a, b), N, [1, 1, 1], [1e-9, 0, 0])
    ok(max(abs(u - v) for u, v in zip(r1, m1)) < 1e-10, "O10 the rank-one reduction equals the full fixed point")
    zo = oracle_ode_final_size(1.2, C, N, [1, 1, 1], [1e-6, 0, 0], gamma=0.25)
    zf = oracle_final_size_multigroup(1.2, C, N, [1, 1, 1], [1e-6, 0, 0])
    ok(max(abs(u - v) for u, v in zip(zo, zf)) < 1e-7, f"O11 the ODE run to extinction meets the final-size relation "
       f"({max(abs(u - v) for u, v in zip(zo, zf)):.2e})")
    rg = oracle_ode_growth_r0(1.2, C, N, [1, 1, 1], gamma=0.25)
    rho = float(np.max(np.linalg.eigvals(C).real))
    ok(abs(rg / (1.2 * rho) - 1) < 1e-6, f"O12 the ODE's growth-consistent R0 equals q rho(C) ({rg:.8f} vs {1.2 * rho:.8f})")
    ok(abs(oracle_cohort_susceptible([0.9, 0.6], [0.85, 0.95]) - 0.10105) < 1e-14
       and abs(oracle_cohort_susceptible([0.9, 0.6], [0.85, 0.95], "nested") - 0.1495) < 1e-14,
       "O13 cohort susceptibility, independent and nested, by hand")
    w = discretised_exponential_gi(1 / 7, 7.0, 12)
    ok(abs(sum(w) - 1) < 1e-14 and w[0] > w[1] > 0, "O14 discretised generation interval sums to 1")
    d1, t1 = generate_renewal_series(np.random.default_rng([7, 0]))
    d2, t2 = generate_renewal_series(np.random.default_rng([7, 0]))
    dtr, ttr = generate_renewal_series(np.random.default_rng([7, 0]), truncate_weeks=5)
    ok(d1 == d2 and t1 == t2 and len(d1["weekly_cases"]) == 30 and t1["attack_rate"] > 0.3
       and ttr["attack_rate"] < 0.05 and len(dtr["weekly_cases"]) == 5,
       f"O15 renewal generator: reproducible; a complete epidemic depletes ({t1['attack_rate']:.2f}), a truncated "
       f"one does not ({ttr['attack_rate']:.3f})")
    dc, tc = generate_cohort_sources(np.random.default_rng([3, 1]))
    da, ta = generate_cohort_sources(np.random.default_rng([3, 1]), admin_only=True)
    ok(0 < tc["susceptible_share"] < 1 and "sero_positive" in dc and "sero_positive" not in da
       and abs(tc["ratio"] - (1 + tc["omega"]) / (1 + tc["delta"])) < 1e-15,
       "O16 cohort-sources generator: two overlapping sources, or administrative only")
    dg, tg = generate_group_attack(np.random.default_rng([5, 2]))
    ok(tg["hospitalisations_averted"] > 0 and len(dg["positive"]) == 3, "O17 group-attack generator")
    ok(math.isnan(oracle_minimum([1.0, float("nan"), 1.0])), "O18 (Codex r1-9) the minimum oracle propagates NaN")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    print(__doc__)
