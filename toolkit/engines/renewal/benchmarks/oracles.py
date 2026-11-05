"""Oracles and probes of the renewal engine's benchmark pack (test only; never exposed to runs).

Oracles are written independently of toolkit/engines/renewal/engine.py: closed forms, fixed points, and a
second, loop-by-loop implementation of the declared renewal process. Probes receive the pack's engine factory
(make_engine(config_name)) and return one number the pack compares with a band or an oracle.
"""
from __future__ import annotations

import math

import numpy as np


# ------------------------------------------------------------------------------------- closed forms and identities
def oracle_wallinga_lipsitch(w, r: float, dt_years: float) -> float:
    """R = 1 / sum_s w_s exp(-r s dt) for a growth rate r per year and w on lags 1..L of dt years (Wallinga &
    Lipsitch 2007, Proc R Soc B 274:599)."""
    w = np.asarray(w, float)
    s = np.arange(1, len(w) + 1) * float(dt_years)
    return float(1.0 / np.sum(w * np.exp(-float(r) * s)))


def oracle_final_size(r0: float, s0: float = 1.0, seed: float = 1e-9) -> float:
    """Infections after the seed until extinction, as a share of N, for a closed population that starts with s0
    susceptible (the seed included in s0): S_inf = S_start exp(-R0 (seed + S_start - S_inf)), S_start = s0 - seed
    (Kermack & McKendrick 1927; Diekmann, Heesterbeek & Britton 2013, ch. 1). Solved by bisection."""
    r0, s0, seed = float(r0), float(s0), float(seed)
    sst = s0 - seed
    f = lambda x: x - sst * math.exp(-r0 * (seed + sst - x))  # noqa: E731
    lo, hi = 0.0, sst
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if f(mid) > 0:
            hi = mid
        else:
            lo = mid
    # the root below S_start (the trivial root S_inf = S_start exists only at seed = 0)
    return float(sst - 0.5 * (lo + hi))


def oracle_linear_renewal_attack(r0: float, w, seed: float = 1e-9, steps: int = 20000) -> float:
    """NEGATIVE CONTROL: the linear renewal form I_t = S_{t-1} R0 sum_s w_s I_{t-s}, capped at S. It does not satisfy
    the final-size relation (with w_1 = 1 and R0 = 2 it ends near 0.8687, against 0.7968)."""
    w = list(map(float, w))
    L = len(w)
    hist = [0.0] * (L - 1) + [float(seed)]
    S = 1.0 - seed
    tot = 0.0
    for _ in range(steps):
        lam = r0 * sum(w[k] * hist[-1 - k] for k in range(L))
        new = min(S, S * lam)
        S -= new
        tot += new
        hist.append(new)
        if new < 1e-16 and S * r0 < 1:
            break
    return float(tot)


def oracle_product(a, b) -> float:
    return float(a) * float(b)


def oracle_difference(a, b) -> float:
    return float(a) - float(b)


def renewal_reference(r0, w, s0, seed, n, c=None):
    """A second implementation of the declared process, loop by loop in plain Python (not the engine's code):
    returns (incidence per step as shares of N, S after the last step)."""
    w = [float(x) for x in w]
    c = [1.0] * n if c is None else [float(x) for x in c]
    hist = [float(seed)]
    S = float(s0) - float(seed)
    inc = []
    for t in range(n):
        lam = 0.0
        for k, wk in enumerate(w, start=1):
            if len(hist) - k >= 0:
                lam += wk * hist[len(hist) - k]
        lam *= float(r0) * c[t]
        new = S * (1.0 - math.exp(-lam))
        S -= new
        hist.append(new)
        inc.append(new)
    return inc, S


def oracle_reference_attack(r0: float, w, s0: float, seed: float, n: int) -> float:
    return float(sum(renewal_reference(r0, w, s0, seed, int(n))[0]))


# ------------------------------------------------------------------------------------------------------- probes
def _engine_run(eng, r0, unit, n_steps, extra=None):
    from toolkit.engines.contract import Horizon, Package, UnitSpec
    stop = n_steps * eng.dt_years
    h = Horizon(0.0, stop, (0.0, stop), {"all": [0.0, stop]})
    return eng.run(dict({"r0": float(r0)}, **(extra or {})), UnitSpec("probe", dict(unit)), Package("none", {}), h, 1)


def probe_reference_max_rel(make_engine, config: str, r0: float, s0: float, seed: float, n: int) -> float:
    """RW7: the largest relative difference, step by step, between the engine's incidence and the reference
    implementation's."""
    eng = make_engine(config)
    o = _engine_run(eng, r0, {"N": 1.0, "s0": s0, "seed": seed}, int(n))
    ref, _ = renewal_reference(r0, list(eng.w), s0, seed, int(n))
    a, b = np.asarray(o.meta["incidence"]), np.asarray(ref)
    return float(np.max(np.abs(a - b) / np.maximum(np.abs(b), 1e-300)))


def probe_scale_invariance(make_engine, config: str, r0: float, s0: float, seed: float, n: int) -> float:
    """RW4: counts from (R0, s0, N) against (R0 s0, 1, s0 N): the largest relative difference. Zero means the case
    series alone cannot separate s0 (and with it the attack rate) from R0, while R0 s_end is the same."""
    eng = make_engine(config)
    N = 1.0e6
    a = _engine_run(eng, r0, {"N": N, "s0": s0, "seed": seed}, int(n))
    b = _engine_run(eng, r0 * s0, {"N": N * s0, "s0": 1.0, "seed": seed / s0}, int(n))
    ca = np.asarray(a.meta["incidence"]) * N
    cb = np.asarray(b.meta["incidence"]) * N * s0
    return float(max(np.max(np.abs(ca / cb - 1.0)),
                     abs(a.windows["all"]["reff_end"] / b.windows["all"]["reff_end"] - 1.0)))


def _cori_case(eng, r_before, r_after, n, switch, N):
    from toolkit.engines.contract import Horizon, Package, UnitSpec
    stop = n * eng.dt_years
    h = Horizon(0.0, stop, (0.0, stop), {})
    o = eng.run({"r0": float(r_before), "transmissibility:1": float(r_after) / float(r_before)},
                UnitSpec("x", {"N": N, "s0": 1.0, "seed": 300.0 / N}), Package("none", {}), h, 1)
    return np.asarray(o.meta["incidence"]) * N, np.asarray(o.meta["rt"])


def probe_cori_recovery(make_engine, config: str, r_before: float, r_after: float, n: int, switch: int,
                        tau: int = 3) -> float:
    """RW5a: on the engine's own expected series with a known piecewise R_t (negligible depletion), the Cori
    estimator's worst relative error, away from the switch (where the window straddles it)."""
    from toolkit.engines.renewal.engine import cori_rt
    eng = make_engine(config)
    inf, rt = _cori_case(eng, r_before, r_after, int(n), int(switch), 1e10)
    cr = cori_rt(inf, list(eng.w), tau=int(tau))
    L = len(eng.w)
    errs = [abs(m / rt[t] - 1.0) for t, m in zip(cr["t"], cr["mean"])
            if t >= 2 * L and not int(switch) - 1 <= t <= int(switch) + L + int(tau)]
    return float(max(errs))


def probe_cori_coverage(make_engine, config: str, r_before: float, r_after: float, n: int, switch: int,
                        reporting: float, replicates: int, seed: int, tau: int = 3) -> float:
    """RW5b: Poisson-reported series (reporting fraction `reporting`) from the engine's expected infections with a
    known piecewise R_t; the share of Cori 95% intervals (all replicates, steps away from the switch) that cover the
    true R_t. The reporting fraction cancels in the estimator."""
    from toolkit.engines.renewal.engine import cori_rt
    eng = make_engine(config)
    inf, rt = _cori_case(eng, r_before, r_after, int(n), int(switch), 2e5)
    rng = np.random.default_rng(int(seed))
    L = len(eng.w)
    hit = tot = 0
    for _ in range(int(replicates)):
        y = rng.poisson(float(reporting) * inf)
        cr = cori_rt(y, list(eng.w), tau=int(tau))
        for t, lo, hi in zip(cr["t"], cr["lo"], cr["hi"]):
            if t >= 2 * L and not int(switch) - 1 <= t <= int(switch) + L + int(tau):
                tot += 1
                hit += int(lo <= rt[t] <= hi)
    return hit / tot


def probe_refit_r0(make_engine, config: str, r0: float, s0: float, reporting: float, seed: float, n: int,
                   N: float = 1.5e6, what: str = "r0") -> float:
    """RW6: expected weekly reported counts from the reference implementation (R0, s0 known, reporting fraction),
    refitted with the engine by Poisson maximum likelihood over (log R0, logit reporting, log seed); returns the
    recovered `what` (r0 | reporting | reff_end)."""
    from scipy.optimize import minimize
    from scipy.special import expit, logit
    eng = make_engine(config)
    inc, _ = renewal_reference(r0, list(eng.w), s0, seed, int(n))
    y = float(reporting) * np.asarray(inc) * N

    def nll(x):
        try:
            o = _engine_run(eng, math.exp(x[0]), {"N": N, "s0": s0, "seed": float(math.exp(x[2]))}, int(n))
        except ValueError:
            return 1e30
        mu = expit(x[1]) * np.asarray(o.meta["incidence"]) * N + 1e-300
        return float(np.sum(mu - y * np.log(mu)))
    best = None
    for x0 in ([math.log(1.2), logit(0.1), math.log(1e-5)], [math.log(3.0), logit(0.02), math.log(1e-4)]):
        res = minimize(nll, x0, method="Nelder-Mead", options={"xatol": 1e-10, "fatol": 1e-12, "maxiter": 20000,
                                                               "maxfev": 20000})
        if best is None or res.fun < best.fun:
            best = res
    r0_hat = math.exp(best.x[0])
    if what == "reporting":
        return float(expit(best.x[1]))
    if what == "reff_end":
        o = _engine_run(eng, r0_hat, {"N": N, "s0": s0, "seed": float(math.exp(best.x[2]))}, int(n))
        return float(o.windows["all"]["reff_end"])
    return float(r0_hat)


def oracle_reference_reff_end(r0: float, w, s0: float, seed: float, n: int) -> float:
    _, S = renewal_reference(r0, w, s0, seed, int(n))
    return float(r0) * float(S)
