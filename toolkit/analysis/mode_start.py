#!/usr/bin/env python3
"""Adaptive Metropolis started at the posterior mode, with one proposal per chain (hb3 triage fix 2a).

    python3 toolkit/analysis/mode_start.py --self-test

calibration.sample's Metropolis branch (`_metropolis`) starts its chains from prior draws and adapts one proposal
covariance on the history of all chains together. When the posterior is far narrower than the prior, a chain that
starts far away inflates that shared covariance, every chain's proposals become too large for the posterior, and
the chains stop moving at different places: the sampler refuses on R-hat and ESS although the posterior is unimodal
and well identified. This module samples the same Problem another way:

1. Modes. The 2^6 scrambled Sobol points over the prior (as observed_targets._scan_start) are scored; the best
   `MODE_STARTS` with a finite likelihood are each moved to the posterior mode in z = Phi^-1(F(x)) coordinates by
   L-BFGS-B, with central-difference gradients evaluated as one batch per step. An optimum is verified when every
   point of its last gradient had a finite likelihood. At each mode, the Laplace covariance comes from a
   finite-difference Hessian (its diagonal only above HESS_FULL_MAX parameters), its eigenvalues floored at 1 (the
   prior's precision in z: the covariance is never wider than the prior). Optima of one basin (within one Laplace
   sd of each other) are one mode. A mode is dropped only when both its peak density and its Laplace mass are far
   below the best one's (DENSITY_GAP and MODE_MASS_GAP): a spike or a wide basin of equal height is kept. Every kept
   mode gets at least one chain (more chains than the threshold when there are more modes), so modes that compete
   are each sampled and R-hat compares them.
2. Starts. Each chain starts at its mode plus DISPERSION times a draw from the Laplace covariance. The starts are
   overdispersed, so R-hat still compares chains that began apart.
3. Adaptation per chain. Each chain's proposal starts from its mode's Laplace covariance and adapts on that chain's
   own warm-up history, shrunk towards the Laplace covariance, with its own scale tuned to an acceptance of 0.234.
   The proposals are fixed after warm-up, as in calibration._metropolis.
4. One retry. A refusal on R-hat or ESS (code posterior_mcmc) is retried once from new starts drawn with another
   random stream. A second refusal is final and names both attempts.

The convergence floors (R-hat, bulk ESS, chains, warm-up, draws, doublings) are calibration's thresholds,
unchanged. Up to `max_importance_params` parameters, calibration.sample (importance sampling) is used as it is.
When no scan point has a finite likelihood, calibration.sample is used too (its prior-draw start, as before).
The output has calibration.sample's shape. calibration.py is not changed: its bytes are bound to the compartmental
coverage pack (toolkit/engines/compartmental/benchmarks/compat_proof.py CP8), whose calibrator is
calibration.posterior, and that path does not come here. Nothing here names a disease.
"""
from __future__ import annotations

import math
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

SCAN_LOG2 = 6                     # the start scan: 2^6 scrambled Sobol points over the prior (one batch)
MODE_STARTS = 8                   # the best scan points moved to a mode
MODE_MASS_GAP = math.log(1000.0)  # with DENSITY_GAP: a mode far below the best in both is dropped
DENSITY_GAP = 20.0                # nats of peak log density
HESS_FULL_MAX = 40                # above this many parameters the Hessian is its diagonal (2d + 1 points)
DISPERSION = 2.0                  # starts: mode + DISPERSION x a Laplace draw (overdispersed)
GRAD_H = 1e-3                     # central-difference step of the gradient, in z
HESS_H = 2e-3                     # finite-difference step of the Hessian, in z
MAXITER = 200                     # L-BFGS-B iterations per mode search
SHRINK_POINTS = 10                # per-chain covariance: shrunk towards the Laplace one as SHRINK_POINTS x d points
RETRY_STREAMS = (17, 19)          # the random streams of the first attempt and of the retry


def _C():
    from toolkit.analysis import calibration as C
    return C


def uses_metropolis(P, **override) -> bool:
    """True when calibration.sample would take its Metropolis branch for this Problem (more parameters than
    `max_importance_params`)."""
    th = dict(_C().thresholds(), **override)
    return len(P.names) > int(th["max_importance_params"])


def _neg_log_post(P, Z):
    """-(log-likelihood + log prior) in z coordinates, +inf where the likelihood is not finite."""
    Z = np.atleast_2d(np.asarray(Z, float))
    ll, _ = P.evaluate(P.x_of_z(Z))
    f = -(ll + _C()._logphi(Z))
    return np.where(np.isfinite(f), f, np.inf)


def _optimise(P, z0, maxiter: int = MAXITER, h: float = GRAD_H):
    """(z, f, verified): z0 moved to a minimum of the negative log posterior by L-BFGS-B, one batch of 2d + 1 points
    per gradient. A point with a non-finite neighbour gives no gradient (the search stops there), so the optimum is
    verified only when the search converged and every point of the gradient at it had a finite likelihood."""
    from scipy.optimize import minimize
    z0 = np.asarray(z0, float)
    d = len(z0)
    offsets = np.vstack([np.zeros(d), h * np.eye(d), -h * np.eye(d)])

    def fg(z):
        f = _neg_log_post(P, z[None, :] + offsets)
        if not np.isfinite(f[0]):
            return 1e300, np.zeros(d), False
        if not np.all(np.isfinite(f)):
            return float(f[0]), np.zeros(d), False
        return float(f[0]), (f[1:d + 1] - f[d + 1:]) / (2.0 * h), True
    f0 = fg(z0)[0]
    try:
        r = minimize(lambda z: fg(z)[:2], z0, jac=True, method="L-BFGS-B", options={"maxiter": int(maxiter)})
        if np.all(np.isfinite(r.x)) and np.isfinite(r.fun) and r.fun <= f0:
            x = np.asarray(r.x, float)
            return x, float(r.fun), bool(r.success) and fg(x)[2]
    except (ValueError, FloatingPointError, np.linalg.LinAlgError):
        pass
    return z0, float(f0), False


def _hessian(P, z, h: float = HESS_H):
    """Central finite-difference Hessian of the negative log posterior at z, or None when a point has no finite
    likelihood. The off-diagonal terms are evaluated one row of pairs at a time (bounded memory); above HESS_FULL_MAX
    parameters only the diagonal (2d + 1 points)."""
    z = np.asarray(z, float)
    d = len(z)
    E = h * np.eye(d)
    f = _neg_log_post(P, np.vstack([z, z + E, z - E]))
    if not np.all(np.isfinite(f)):
        return None
    f0 = f[0]
    H = np.diag((f[1:d + 1] - 2.0 * f0 + f[d + 1:]) / h ** 2)
    if d > HESS_FULL_MAX:
        return H
    for i in range(d - 1):
        ks = np.arange(i + 1, d)
        pts = [z + si * E[i] + sk * E[ks] for si, sk in ((1.0, 1.0), (1.0, -1.0), (-1.0, 1.0), (-1.0, -1.0))]
        g = _neg_log_post(P, np.vstack(pts)).reshape(4, len(ks))
        if not np.all(np.isfinite(g)):
            return None
        H[i, ks] = H[ks, i] = (g[0] - g[1] - g[2] + g[3]) / (4.0 * h * h)
    return H


def laplace(H, d: int):
    """(covariance, log det of the precision): the Laplace covariance in z from a Hessian, its eigenvalues floored at
    1 (the prior's precision in z). No Hessian: the prior's covariance (the identity)."""
    if H is None or not np.all(np.isfinite(H)):
        return np.eye(d), 0.0
    w, V = np.linalg.eigh((H + H.T) / 2.0)
    w = np.maximum(w, 1.0)
    return (V / w) @ V.T, float(np.sum(np.log(w)))


def _select(found: list, d: int) -> tuple:
    """(kept, dropped): the optima grouped into modes (an optimum within one Laplace sd, in the Mahalanobis sense, of
    a better one is that mode), the verified ones ranked first; a mode is dropped only when both its peak density and
    its Laplace mass are far below the best's (DENSITY_GAP, MODE_MASS_GAP). Heaviest first."""
    pool = [m for m in found if m["converged"]] or list(found)
    pool = sorted(pool, key=lambda m: m["neg_log_post"])
    modes = []
    for m in pool:
        same = False
        for r in modes:
            dz = m["z"] - r["z"]
            try:
                same = float(dz @ np.linalg.solve(r["cov"], dz)) < 1.0
            except np.linalg.LinAlgError:
                same = bool(np.allclose(m["z"], r["z"]))
            if same:
                r["optima"] += 1
                break
        if not same:
            modes.append(dict(m, optima=1))
    if not modes:
        return [], 0
    fb = min(m["neg_log_post"] for m in modes)
    mb = max(m["log_mass"] for m in modes)
    kept = [m for m in modes if not (m["neg_log_post"] - fb > DENSITY_GAP and mb - m["log_mass"] > MODE_MASS_GAP)]
    return sorted(kept, key=lambda m: -m["log_mass"]), len(modes) - len(kept)


def find_modes(P, starts: int = MODE_STARTS, after_scan=None) -> dict:
    """{modes, dropped, scanned, scan_finite}: the posterior modes reached from the best scan points (_select), each
    with its z, negative log posterior, Laplace covariance, log mass (-f - log det(H) / 2) and number of optima.
    `after_scan()` is called once the scan is evaluated and before any optimisation: the scan explores the prior, the
    optimisation and the Hessian are part of the fit."""
    from scipy.stats import norm, qmc
    d = len(P.names)
    U = qmc.Sobol(d, scramble=True, seed=11).random_base2(SCAN_LOG2)
    Z0 = norm.ppf(np.clip(U, 1e-9, 1 - 1e-9))
    f = _neg_log_post(P, Z0)
    if after_scan is not None:
        after_scan()
    order = [int(i) for i in np.argsort(f, kind="stable") if np.isfinite(f[i])][:int(starts)]
    found = []
    for i in order:
        z, fz, conv = _optimise(P, Z0[i])
        cov, logdet = laplace(_hessian(P, z), d)
        found.append({"z": z, "neg_log_post": fz, "converged": conv, "cov": cov,
                      "log_mass": float(-fz - 0.5 * logdet), "scan_point": i})
    kept, dropped = _select(found, d)
    return {"modes": kept, "dropped": dropped, "optima": len(found), "scanned": int(len(Z0)),
            "scan_finite": int(np.sum(np.isfinite(f)))}


def _chol(S, d):
    """The Cholesky factor of S as computed; a scale-relative jitter only when it does not factor."""
    S = (S + S.T) / 2.0
    try:
        return np.linalg.cholesky(S)
    except np.linalg.LinAlgError:
        return np.linalg.cholesky(S + 1e-10 * max(float(np.trace(S)) / d, 1e-300) * np.eye(d))


def _chains(P, rng, th, modes) -> dict:
    """One adaptive Metropolis attempt from overdispersed starts around the modes, one proposal per chain; the
    sampling phase and its diagnostics as calibration._metropolis. CalibrationRefused(posterior_mcmc) below the
    floors after the allowed doublings."""
    C = _C()
    d = len(P.names)
    K = max(int(th["chains"]), len(modes))          # every kept mode gets at least one chain
    own = [modes[c % len(modes)] for c in range(K)]
    cov0 = np.stack([m["cov"] for m in own])
    L0 = np.stack([_chol(cov0[c], d) for c in range(K)])
    centre = np.stack([m["z"] for m in own])
    Z = centre + DISPERSION * np.einsum("cij,cj->ci", L0, rng.standard_normal((K, d)))
    ll, ax = P.evaluate(P.x_of_z(Z))
    shrink = 1.0
    for _ in range(20):                           # a start without a finite likelihood moves closer to its mode
        bad = ~np.isfinite(ll)
        if not bad.any():
            break
        shrink *= 0.5
        Z[bad] = centre[bad] + DISPERSION * shrink * np.einsum("cij,cj->ci", L0[bad],
                                                                rng.standard_normal((int(bad.sum()), d)))
        ll2, ax2 = P.evaluate(P.x_of_z(Z[bad]))
        ll[bad] = ll2
        for k in ax:
            ax[k][bad] = ax2[k]
    if not np.all(np.isfinite(ll)):
        raise C.CalibrationRefused("posterior_mcmc", "no finite starting point near the posterior mode for every chain")
    lp = ll + C._logphi(Z)
    scale = np.full(K, 2.38 ** 2 / d)
    S = cov0.copy()
    L = np.stack([_chol(scale[c] * S[c], d) for c in range(K)])

    def step(Z, ll, lp, ax, L):
        Zp = Z + np.einsum("cij,cj->ci", L, rng.standard_normal((K, d)))
        llp, axp = P.evaluate(P.x_of_z(Zp))
        lpp = llp + C._logphi(Zp)
        a = np.log(rng.random(K)) < (lpp - lp)
        Z = np.where(a[:, None], Zp, Z)
        ll, lp = np.where(a, llp, ll), np.where(a, lpp, lp)
        ax = {k: np.where(a, axp[k], ax[k]) for k in ax}
        return Z, ll, lp, ax, a
    W = int(th["warmup"])
    hist, acc_w = [], []
    for t in range(W):
        Z, ll, lp, ax, a = step(Z, ll, lp, ax, L)
        hist.append(Z.copy())
        acc_w.append(a.astype(float))
        if t >= 300 and t % 100 == 0:
            Hc = np.stack(hist[len(hist) // 2:], axis=1)          # (K, n, d): each chain's own recent history
            n = Hc.shape[1]
            rate = np.mean(np.stack(acc_w[-100:]), axis=0)        # (K,)
            w0 = SHRINK_POINTS * d
            for c in range(K):
                Sc = np.cov(Hc[c].T).reshape(d, d)
                S[c] = (n * Sc + w0 * cov0[c]) / (n + w0)
            scale = scale * np.exp(rate - 0.234)
            L = np.stack([_chol(scale[c] * S[c], d) for c in range(K)])
    N = int(th["mcmc_draws"])
    draws_z, draws_ll, draws_ax = [], [], []
    doublings, acc = 0, []
    target = N
    while True:
        while len(draws_z) < target:
            Z, ll, lp, ax, a = step(Z, ll, lp, ax, L)
            draws_z.append(Z.copy())
            draws_ll.append(ll.copy())
            draws_ax.append({k: v.copy() for k, v in ax.items()})
            acc.append(a.mean())
        ch = np.stack(draws_z, axis=1)                             # (K, N, d)
        rhat, essb = C.mcmc_diagnostics(ch)
        if (rhat <= th["rhat_max"] and essb >= th["ess_bulk_min"]) or doublings >= int(th["mcmc_max_doublings"]):
            break
        doublings += 1
        target *= 2
    if not (rhat <= th["rhat_max"] and essb >= th["ess_bulk_min"]):
        raise C.CalibrationRefused("posterior_mcmc", f"R-hat {rhat:.4f} (max {th['rhat_max']}) and bulk ESS "
                                                     f"{essb:.0f} (min {th['ess_bulk_min']}) after {doublings} doublings")
    Zall = ch.reshape(-1, d)
    llall = np.stack(draws_ll, axis=1).reshape(-1)
    auxall = {k: np.stack([x[k] for x in draws_ax], axis=1).reshape(-1) for k in (draws_ax[0] if draws_ax else {})}
    M = min(int(th["draws_kept"]), len(Zall))
    idx = np.unique(np.linspace(0, len(Zall) - 1, M).round().astype(int))
    X = P.x_of_z(Zall[idx])
    best = int(np.argmax(llall))
    return {"method": "adaptive_metropolis", "rows": X, "aux_rows": {k: v[idx] for k, v in auxall.items()},
            "ll_rows": llall[idx], "best": P.x_of_z(Zall[best:best + 1])[0], "best_ll": float(llall[best]),
            "diagnostics": {"chains": K, "warmup": W, "draws_per_chain": ch.shape[1], "rhat_max": rhat,
                            "ess_bulk_min": essb, "acceptance": float(np.mean(acc)), "doublings": doublings}}


def _text(e) -> str:
    s = str(e)
    return s[len(e.code) + 2:] if s.startswith(f"{e.code}: ") else s


def sample(P, seed: int = 0, modes: dict | None = None, **override) -> dict:
    """The canonical joint posterior sample of a Problem, in calibration.sample's shape: importance sampling up to
    `max_importance_params` (calibration.sample itself); above, adaptive Metropolis started at the posterior modes
    (`modes`: find_modes' result, computed here when not given), one proposal per chain, retried once from new starts
    before it refuses. Deterministic for a seed; the floors are calibration's thresholds."""
    C = _C()
    th = dict(C.thresholds(), **override)
    if not P.names:
        raise ValueError("posterior: nothing to calibrate")
    if not uses_metropolis(P, **override):
        return C.sample(P, seed=seed, **override)
    found = modes if modes is not None else find_modes(P)
    if not found.get("modes"):
        return C.sample(P, seed=seed, **override)       # no finite scan point: the prior-draw start, as before
    first, out = None, None
    for attempt, stream in enumerate(RETRY_STREAMS, 1):
        rng = np.random.default_rng([int(seed), int(stream)])
        try:
            out = _chains(P, rng, th, found["modes"])
            break
        except C.CalibrationRefused as e:
            if e.code != "posterior_mcmc":
                raise
            if attempt == len(RETRY_STREAMS):
                raise C.CalibrationRefused("posterior_mcmc", f"{_text(e)} (mode start, per-chain adaptation; retried "
                                                             f"once from new starts: the first attempt gave {first})")
            first = _text(e)
    out["diagnostics"].update(start="posterior_mode", adaptation="per_chain", attempt=attempt,
                              modes_kept=len(found["modes"]), modes_dropped=int(found.get("dropped", 0)),
                              optima=int(found.get("optima", 0)),
                              **({"first_attempt": first} if first else {}))
    out.update(names=list(P.names), seed=int(seed), sample_id=C._sample_id(P.names, out["rows"]),
               evaluations=P.n_evals, priors={n: P.priors[n].describe() for n in P.names})
    return out


# --------------------------------------------------------------------------- self-test
def _toy(d=6, s=1e-3, mu=None):
    """A unimodal posterior far narrower than its uniform priors (posterior sd `s` against a prior sd of 0.29): the
    case where prior-draw starts and one pooled proposal leave the chains stranded."""
    C = _C()
    names = [f"p{j}" for j in range(d)]
    mu = np.asarray(mu if mu is not None else [0.62, 0.35, 0.70, 0.45, 0.55, 0.30][:d], float)
    pri = {n: C.Prior.from_decl(n, {"dist": "uniform", "range": [0.0, 1.0], "source": "toy"}) for n in names}

    def ll(X):
        A = np.stack([np.asarray(X[n], float) for n in names], axis=1)
        return -0.5 * np.sum(((A - mu) / s) ** 2, axis=1), {}
    return C.Problem(names, pri, ll), mu


def _self_test() -> int:
    from scipy.stats import norm
    C = _C()
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)
    fast = {"warmup": 1000, "mcmc_draws": 2000, "mcmc_max_doublings": 2}
    s = 1e-3
    # MS1: prior-draw starts with a pooled proposal (calibration.sample) fail on the toy: a refusal or a wrong mean
    P0, mu = _toy(s=s)
    try:
        r0 = C.sample(P0, seed=0, **fast)
        off = float(np.max(np.abs(r0["rows"].mean(axis=0) - mu)) / s)
        old = f"converged with a mean {off:.1f} sd off"
        old_failed = off > 5.0
    except C.CalibrationRefused as e:
        old, old_failed = str(e)[:120], e.code == "posterior_mcmc"
    ok(old_failed, f"MS1 the prior-draw start with one pooled proposal fails on a posterior 300x narrower than its "
                   f"prior ({old})")
    # MS2: the mode start with per-chain adaptation converges on the same toy, seed and floors
    P1, _ = _toy(s=s)
    try:
        r1 = sample(P1, seed=0, **fast)
        dg = r1["diagnostics"]
        m1, sd1 = r1["rows"].mean(axis=0), r1["rows"].std(axis=0)
        good = (r1["method"] == "adaptive_metropolis" and dg["start"] == "posterior_mode"
                and dg["adaptation"] == "per_chain" and dg["rhat_max"] <= 1.01 and dg["ess_bulk_min"] >= 400
                and float(np.max(np.abs(m1 - mu))) < 0.3 * s and np.all((sd1 > 0.7 * s) & (sd1 < 1.4 * s))
                and r1["names"] == P1.names and len(r1["sample_id"]) == 64)
        msg = (f"R-hat {dg['rhat_max']:.4f}, ESS {dg['ess_bulk_min']:.0f}, attempt {dg['attempt']}, mean off "
               f"{float(np.max(np.abs(m1 - mu))) / s:.2f} sd, sd ratio {np.round(sd1 / s, 2).tolist()}")
    except C.CalibrationRefused as e:
        good, msg = False, str(e)[:200]
    ok(good, f"MS2 the mode start with per-chain adaptation converges on the same toy and floors ({msg})")
    # MS3: the modes and their Laplace covariance (delta method: sd_x = sd_z x phi(z) at the mode)
    P2, _ = _toy(s=s)
    fm = find_modes(P2)
    zs = np.stack([m["z"] for m in fm["modes"]]) if fm["modes"] else np.zeros((0, 6))
    xs = norm.cdf(zs)
    sdx = (np.sqrt(np.diag(fm["modes"][0]["cov"])) * norm.pdf(fm["modes"][0]["z"])) if fm["modes"] else np.zeros(6)
    ok(fm["modes"] and np.all(np.abs(xs - mu) < 0.2 * s) and np.all((sdx > 0.8 * s) & (sdx < 1.25 * s))
       and fm["scan_finite"] == 2 ** SCAN_LOG2,
       f"MS3 every mode reached is the posterior's, and the Laplace sd matches it ({len(fm['modes'])} kept, "
       f"{fm['dropped']} dropped, sd ratio {np.round(sdx / s, 3).tolist()})")
    # MS4: a refused first attempt is retried once from new starts; a second refusal is final and names both
    real = globals()["_chains"]
    calls = []

    def once(P, rng, th, modes):
        calls.append(1)
        if len(calls) == 1:
            raise C.CalibrationRefused("posterior_mcmc", "R-hat 1.5000 (planted)")
        return real(P, rng, th, modes)
    globals()["_chains"] = once
    try:
        r4 = sample(P2, seed=0, modes=fm, **fast)
        dg4 = r4["diagnostics"]
        ok(len(calls) == 2 and dg4["attempt"] == 2 and "planted" in dg4.get("first_attempt", ""),
           f"MS4 a refused attempt is retried once from new starts ({len(calls)} attempts, {dg4.get('first_attempt')})")

        def never(P, rng, th, modes):
            raise C.CalibrationRefused("posterior_mcmc", "R-hat 1.4000 (planted)")
        globals()["_chains"] = never
        try:
            sample(P2, seed=0, modes=fm, **fast)
            ok(False, "MS4b two refused attempts refuse the calibration")
        except C.CalibrationRefused as e:
            ok(e.code == "posterior_mcmc" and str(e).count("planted") == 2 and "retried once" in str(e),
               f"MS4b two refused attempts refuse, naming both ({str(e)[:160]})")
    finally:
        globals()["_chains"] = real
    # MS5: up to max_importance_params the importance sampler is used as it is; no finite scan point falls back
    Pq, muq = _toy(d=2, s=0.05, mu=[0.4, 0.6])
    rq = sample(Pq, seed=0)
    ok(rq["method"] == "importance" and not uses_metropolis(Pq) and uses_metropolis(P2),
       f"MS5 up to max_importance_params calibration.sample's importance sampler is used ({rq['method']})")
    Pn = C.Problem(["a", "b", "c", "d", "e"], {n: C.Prior.from_decl(n, {"dist": "uniform", "range": [0, 1],
                                                                       "source": "toy"}) for n in "abcde"},
                   lambda X: (np.full(len(X["a"]), -np.inf), {}))
    fn = find_modes(Pn)
    ok(fn["modes"] == [] and fn["scan_finite"] == 0, "MS5b no finite scan point: no mode (sample falls back to "
                                                     "calibration.sample's prior start)")
    # MS6 (Codex r1 P1-1, P1-2): optima of one basin are one mode; a spike or an equal-height wide basin is kept; only a
    # mode far below in both peak density and Laplace mass is dropped; unverified optima rank after verified ones
    I6 = np.eye(6)

    def opt(z0, f, cov, conv=True):
        return {"z": np.full(6, float(z0)), "neg_log_post": f, "converged": conv, "cov": cov,
                "log_mass": float(-f - 0.5 * np.linalg.slogdet(np.linalg.inv(cov))[1]), "scan_point": 0}
    a_ = [opt(-1.0, 0.0, 0.01 * I6) for _ in range(4)]
    b_ = [opt(1.0, 0.7, 0.01 * I6) for _ in range(4)]
    spike = opt(2.0, 0.0, 1e-6 * I6)
    stall = opt(-2.0, 50.0, I6)
    kept, dropped = _select(a_ + b_ + [spike, stall], 6)
    ok(len(kept) == 3 and dropped == 1 and [m["optima"] for m in kept].count(4) == 2
       and any(np.allclose(m["z"], 2.0) for m in kept) and not any(np.allclose(m["z"], -2.0) for m in kept),
       f"MS6 duplicate optima are one mode; a spike of equal height is kept; a stalled point far below is dropped "
       f"({[(float(m['z'][0]), m['optima']) for m in kept]}, {dropped} dropped)")
    kept_u, _ = _select([opt(1.0, 0.0, 0.01 * I6, conv=False), opt(-1.0, 0.5, 0.01 * I6)], 6)
    ok(len(kept_u) == 1 and np.allclose(kept_u[0]["z"], -1.0),
       "MS6b an unverified optimum is not ranked beside a verified one")
    # MS7: every kept mode gets at least one chain, also when there are more modes than the chains threshold
    Pb, _ = _toy(s=0.02)
    six = [{"z": np.full(6, 0.1 * j), "cov": 1e-4 * I6} for j in range(6)]
    seen7 = []
    orig_eval = Pb.evaluate

    def spy(X):
        seen7.append(len(X))
        return orig_eval(X)
    Pb.evaluate = spy
    try:
        _chains(Pb, np.random.default_rng(0), dict(C.thresholds(), warmup=1, mcmc_draws=4, mcmc_max_doublings=0),
                six)
        ok(False, "MS7 a planted far-apart start refuses")
    except C.CalibrationRefused:
        ok(seen7 and seen7[0] == 6, f"MS7 six modes start six chains (more than the threshold's 4): {seen7[:1]}")
    # MS8 (Codex r1 P2-12): a valid narrow covariance factors as computed, with no absolute jitter
    Lt = _chol(1e-16 * I6, 6)
    ok(np.allclose(np.diag(Lt), 1e-8, rtol=1e-6), f"MS8 a covariance of 1e-16 factors with no jitter ({Lt[0, 0]:.3g})")
    # MS9 (Codex r1 P2-13): above HESS_FULL_MAX parameters the Hessian is its diagonal (2d + 1 points)
    Pd, _ = _toy(d=HESS_FULL_MAX + 1, s=0.05, mu=[0.5] * (HESS_FULL_MAX + 1))
    n0 = Pd.n_evals
    Hd = _hessian(Pd, np.zeros(HESS_FULL_MAX + 1))
    ok(Hd is not None and Pd.n_evals - n0 == 2 * (HESS_FULL_MAX + 1) + 1 and np.count_nonzero(Hd - np.diag(np.diag(Hd))) == 0,
       f"MS9 a {HESS_FULL_MAX + 1}-parameter Hessian is its diagonal ({Pd.n_evals - n0} points)")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    print(__doc__)
