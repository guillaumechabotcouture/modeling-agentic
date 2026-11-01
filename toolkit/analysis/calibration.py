"""Calibration to targets, with diagnostics and identifiability (design §2).

emulator_inversion (the spike): per unit, the calibrated attribute that reproduces the
unit's target on the emulator's calibration metric (logit scale), on a grid over
`calibration.range`, assuming the metric increases with the attribute (made monotone by a
running maximum). Units whose target lies outside the grid's range are clamped and
counted. Identifiability: one attribute per target; the local slope of the metric in the
attribute is the diagnostic, and a slope near zero means the target does not pin the
attribute down.

likelihood: the spec's `calibrated` engine parameters and a per-unit attribute, fitted to
a panel of targets (unit, period, package, observed count, exposure) by Poisson
quasi-likelihood. The dispersion is the Pearson chi-square over its degrees of freedom
(at least 1), the covariance is dispersion x inverse Hessian, and identifiability comes
from scripts/identifiability.py (Fisher information on the full Hessian, ridge detection,
profile likelihood) on the dispersion-scaled negative log-likelihood.
"""
from __future__ import annotations

import csv
import math
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from toolkit.analysis.model import _logit  # noqa: E402
from toolkit.engines.contract import Horizon  # noqa: E402

PRODUCER = "toolkit/analysis/calibration.py"
GRID = 161
FLAT_SLOPE = 0.05          # d logit(metric) / d attribute below this: the target hardly constrains the attribute


def _ident():
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import identifiability
    return identifiability


# --------------------------------------------------------------------------- emulator inversion
def invert(spec, model, theta, shifts=None, ids=None):
    """{unit: (z, clamped)}: the spike's emulator inversion, unchanged."""
    cal = spec["calibration"]
    tc = cal["target_column"]
    region = spec["decision_units"].get("region")
    ids = ids or spec.unit_ids()
    lo, hi = cal["range"]
    zgrid = np.linspace(lo, hi, GRID)
    pred_all = model.cal_grid(ids, theta, zgrid)
    out = {}
    for j, i in enumerate(ids):
        u = model.by_id[i]
        t = float(_logit(np.array([float(u[tc])]))[0])
        if shifts is not None and region:
            t += shifts.get(u[region], 0.0)
        pred = np.maximum.accumulate(pred_all[j])
        clamped = not (pred[0] <= t <= pred[-1])
        out[i] = (float(np.interp(t, pred, zgrid)), clamped)
    return out


def inversion_report(spec, model, theta, cal) -> dict:
    ids = list(cal)
    zs = {i: cal[i][0] for i in ids}
    slope = model.cal_slope(ids, theta, zs)
    flat = [i for i, s in zip(ids, slope) if not abs(s) >= FLAT_SLOPE]
    tc = spec["calibration"]["target_column"]
    pred = {}
    X = model._scale(np.asarray([model._x(zs[i], theta, model.by_id[i]) for i in ids], float))
    p = model.gps[("cal",)].predict(X)
    for i, v in zip(ids, p):
        pred[i] = float(v)
    resid = [pred[i] - float(_logit(np.array([float(model.by_id[i][tc])]))[0]) for i in ids]
    return {
        "method": "emulator_inversion",
        "units": {i: {"z": zs[i], "clamped": bool(cal[i][1]), "slope": float(s)} for i, s in zip(ids, slope)},
        "diagnostics": {"units": len(ids), "clamped_units": int(sum(cal[i][1] for i in ids)),
                        "grid_points": GRID, "range": list(spec["calibration"]["range"]),
                        "target_logit_residual_max": float(np.max(np.abs(resid))),
                        "slope_median": float(np.median(slope)), "slope_min": float(np.min(slope))},
        "identifiability": {"design": "one attribute per unit, one target per unit (exactly determined)",
                            "flat_slope_threshold": FLAT_SLOPE, "units_unidentified": flat,
                            "status": "well_identified" if not flat else "partly_unidentified"},
    }


# --------------------------------------------------------------------------- likelihood
def read_targets(spec) -> list[dict]:
    t = spec["calibration"]["targets"]
    pmap = t.get("package_map") or {}
    with open(spec.resolve(t["csv"]), newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return [{"unit": r[t["unit"]], "t0": float(r[t["start"]]),
             "t1": float(r[t["stop"]]) if "stop" in t else float(r[t["start"]]) + float(t["period"]),
             "package": pmap.get(r[t["package"]], r[t["package"]]), "observed": float(r[t["observed"]]),
             "exposure": float(r[t["exposure"]])} for r in rows]


class Likelihood:
    """Poisson NLL of the panel targets as a function of (calibrated parameters, per-unit attribute)."""

    def __init__(self, spec, model, rows=None, fixed_z=None):
        self.spec, self.model = spec, model
        cal = spec["calibration"]
        self.params = list(cal["parameters"])
        self.outcome = cal["targets"].get("outcome", spec["objective"]["outcome"])
        self.rows = rows if rows is not None else read_targets(spec)
        self.fixed_z = dict(fixed_z or {})
        self.units = sorted({r["unit"] for r in self.rows} - set(self.fixed_z), key=str)
        self.names = self.params + [f"z:{u}" for u in self.units]
        h = spec["horizon"]
        self.hz = {(r["t0"], r["t1"]): Horizon(float(h["start"]), float(h["stop"]), (r["t0"], r["t1"]), {})
                   for r in self.rows}
        self.pspec = {p.name: p for p in model.eng.parameters()}
        self.zrange = list(cal["range"])

    def split(self, x: dict):
        theta = {p: x[p] for p in self.params}
        zs = dict(self.fixed_z)
        zs.update({u: x[f"z:{u}"] for u in self.units})
        return theta, zs

    def expected(self, x: dict) -> np.ndarray:
        theta, zs = self.split(x)
        out = []
        for r in self.rows:
            o = self.model.run(theta, r["unit"], zs[r["unit"]], r["package"], self.hz[(r["t0"], r["t1"])])
            pc = sum(o.per_capita[self.outcome][g] * o.meta.get("age_shares", {}).get(g, 1.0) for g in o.per_capita[self.outcome])
            out.append(pc * r["exposure"])
        return np.asarray(out)

    def nll(self, x: dict) -> float:
        mu = np.maximum(self.expected(x), 1e-300)
        y = np.array([r["observed"] for r in self.rows])
        return float(np.sum(mu - y * np.log(mu)))

    def bounds(self) -> dict:
        b = {}
        for p in self.params:
            ps = self.pspec[p]
            b[p] = (ps.lo if ps.lo is not None else -20.0, ps.hi if ps.hi is not None else 20.0)
        for u in self.units:
            b[f"z:{u}"] = tuple(self.zrange)
        return b

    def start(self) -> dict:
        x = {p: float(self.pspec[p].value) for p in self.params}
        for u in self.units:      # a crude start: the unit's mean observed rate under any package
            ys = [r["observed"] / r["exposure"] for r in self.rows if r["unit"] == u and r["exposure"] > 0]
            x[f"z:{u}"] = float(np.clip(math.log(max(np.mean(ys), 1e-9)), *self.zrange)) if ys else 0.0
        return x


def fit_likelihood(spec, model, rows=None, fixed_z=None, start=None, identifiability=True) -> dict:
    from scipy.optimize import minimize
    L = Likelihood(spec, model, rows, fixed_z)
    names, b = L.names, L.bounds()
    x0 = start or L.start()
    v0 = np.array([x0[n] for n in names])
    scale = max(1.0, abs(L.nll(x0)))
    res = minimize(lambda v: L.nll(dict(zip(names, v))) / scale, v0, method="L-BFGS-B",
                   bounds=[b[n] for n in names], options={"maxiter": 2000, "ftol": 1e-14, "gtol": 1e-10})
    xh = dict(zip(names, res.x))
    mu = L.expected(xh)
    y = np.array([r["observed"] for r in L.rows])
    df = max(1, len(y) - len(names))
    pearson = float(np.sum((y - mu) ** 2 / np.maximum(mu, 1e-300)))
    phi = max(1.0, pearson / df)
    I = _ident()
    qnll = lambda x: L.nll(x) / phi  # noqa: E731 -- quasi-likelihood: the dispersion-scaled NLL
    fa = I.fisher_analysis(qnll, xh, b)
    names_h, H = I.hessian(qnll, xh, b)
    # v2d §3.1 (AN-b3): a ridged fit is refused, never jittered into a positive-definite covariance. The finding tells
    # the planner to reparameterise (a reference package) or to declare priors and use `posterior`.
    null = [r for r in fa.get("ridges") or [] if r.get("null_direction")]
    cond = fa.get("condition_number")
    cmax = float(thresholds()["ridge_condition_max"])
    if null or cond is None or not (cond <= cmax):
        raise CalibrationRefused("calibration_ridge", f"the likelihood has a flat direction (condition number "
                                 f"{cond}, max {cmax:g}; null directions {[r['params'] for r in null][:3]}): "
                                 "reparameterise (a reference package) or declare priors and use method posterior")
    try:
        cov = np.linalg.inv(np.asarray(H))
        cov = (cov + cov.T) / 2
        if np.linalg.eigvalsh(cov).min() <= 0:
            raise CalibrationRefused("calibration_ridge", "the covariance is not positive definite")
    except np.linalg.LinAlgError:
        raise CalibrationRefused("calibration_ridge", "the Hessian is singular") from None
    at_bound = [n for n in names if min(abs(xh[n] - b[n][0]), abs(xh[n] - b[n][1])) < 1e-6 * max(1.0, abs(xh[n]))]
    ident = {}
    if identifiability:
        for p in L.params:
            prof = I.profile_likelihood(qnll, xh, {"name": p, "lower_bound": b[p][0], "upper_bound": b[p][1]},
                                        n_points=12, nuisance_bounds=b, maxiter=200)
            ridge = next((r for r in fa.get("ridges") or [] if p in (r.get("params") or [])), None)
            st, why = I.classify(p, xh[p], (fa.get("se") or {}).get(p), prof, ridge)
            ident[p] = {"status": st, "explanation": why, "estimate": xh[p], "se": (fa.get("se") or {}).get(p),
                        "profile_ci": [prof.get("ci_low"), prof.get("ci_high")]}
    resid = (y - mu) / np.sqrt(np.maximum(mu, 1e-300))
    return {
        "method": "likelihood", "x": xh, "names": names_h, "cov": cov.tolist() if cov is not None else None,
        "params": L.params, "units_fitted": L.units, "fixed_z": L.fixed_z,
        "theta": {p: xh[p] for p in L.params}, "z": {u: xh[f"z:{u}"] for u in L.units},
        "diagnostics": {"converged": bool(res.success), "message": str(res.message), "iterations": int(res.nit),
                        "n_targets": len(y), "n_parameters": len(names), "dispersion": phi,
                        "pearson_chi2": pearson, "df": df, "at_bound": at_bound,
                        "pearson_residual_max": float(np.max(np.abs(resid))),
                        "unsupported_targets": int(np.sum((y > 0) & (mu < 1e-12))),
                        "fit": [{"unit": r["unit"], "t0": r["t0"], "package": r["package"], "observed": r["observed"],
                                 "expected": float(m)} for r, m in zip(L.rows, mu)]},
        "identifiability": {"method": "scripts/identifiability.py (Fisher information, ridges, profile likelihood) "
                                      "on the dispersion-scaled Poisson NLL",
                            "parameters": ident, "ridges": fa.get("ridges"),
                            "condition_number": fa.get("condition_number"),
                            "status": ("well_identified" if ident and all(v["status"] == "well_identified"
                                                                          for v in ident.values())
                                       else "weakly_or_unidentified" if ident else "not_assessed")},
    }


# =========================================================================== v2d β: posterior calibration
class CalibrationRefused(ValueError):
    """A calibration that fails closed, with a finding code (calibration_ridge, posterior_ess, posterior_mcmc, ...)."""

    def __init__(self, code: str, msg: str):
        self.code = code
        super().__init__(f"{code}: {msg}")


def thresholds() -> dict:
    import yaml
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "thresholds.yaml")) as f:
        return dict(yaml.safe_load(f)["calibration"])


PRIOR_DISTS = ("uniform", "loguniform", "beta", "triangular")


class Prior:
    """A bounded prior with a quantile map (the samplers work in z = Phi^-1(F(x)), where every prior is N(0, 1)) and
    its own natural scale (log for a log-uniform prior), on which contraction and the Fisher scaling are measured."""

    def __init__(self, name, dist, lo=None, hi=None, a=None, b=None, mode=None, source=""):
        self.name, self.dist, self.lo, self.hi, self.a, self.b, self.mode = name, dist, lo, hi, a, b, mode
        self.source = source
        if dist == "beta":
            self.lo, self.hi = 0.0, 1.0

    @classmethod
    def from_decl(cls, name, d):
        if not isinstance(d, dict) or d.get("dist") not in PRIOR_DISTS:
            raise ValueError(f"prior {name}: dist must be one of {PRIOR_DISTS} ({d!r})")
        src = str(d.get("source") or "")
        if d["dist"] == "beta":
            a, b = d.get("a"), d.get("b")
            if not all(isinstance(x, (int, float)) and x > 0 for x in (a, b)):
                raise ValueError(f"prior {name}: beta needs a > 0 and b > 0")
            return cls(name, "beta", a=float(a), b=float(b), source=src)
        r = d.get("range")
        if not (isinstance(r, (list, tuple)) and len(r) == 2 and all(isinstance(x, (int, float)) for x in r)
                and math.isfinite(r[0]) and math.isfinite(r[1]) and r[0] < r[1]):
            raise ValueError(f"prior {name}: range [lo, hi] with lo < hi")
        if d["dist"] == "loguniform" and r[0] <= 0:
            raise ValueError(f"prior {name}: a log-uniform range must be positive")
        mode = d.get("mode")
        if d["dist"] == "triangular" and not (isinstance(mode, (int, float)) and r[0] <= mode <= r[1]):
            raise ValueError(f"prior {name}: triangular needs a mode inside the range")
        return cls(name, d["dist"], float(r[0]), float(r[1]), mode=mode, source=src)

    def ppf(self, u):
        from scipy import stats
        u = np.asarray(u, float)
        if self.dist == "uniform":
            return self.lo + (self.hi - self.lo) * u
        if self.dist == "loguniform":
            return np.exp(math.log(self.lo) + (math.log(self.hi) - math.log(self.lo)) * u)
        if self.dist == "beta":
            return stats.beta.ppf(u, self.a, self.b)
        c = (self.mode - self.lo) / (self.hi - self.lo)
        return stats.triang.ppf(u, c, loc=self.lo, scale=self.hi - self.lo)

    def natural(self, x):
        x = np.asarray(x, float)
        return np.log(x) if self.dist == "loguniform" else x

    def from_natural(self, t):
        t = np.asarray(t, float)
        return np.exp(t) if self.dist == "loguniform" else t

    def natural_support(self):
        return (math.log(self.lo), math.log(self.hi)) if self.dist == "loguniform" else (self.lo, self.hi)

    def natural_var(self) -> float:
        if self.dist in ("uniform", "loguniform"):
            lo, hi = self.natural_support()
            return (hi - lo) ** 2 / 12.0
        if self.dist == "beta":
            a, b = self.a, self.b
            return a * b / ((a + b) ** 2 * (a + b + 1))
        lo, hi, c = self.lo, self.hi, self.mode
        return (lo * lo + hi * hi + c * c - lo * hi - lo * c - hi * c) / 18.0

    def interval(self) -> list:
        """The declared range (a beta prior: its central 98%), the values of an automatic prior_range test."""
        if self.dist == "beta":
            return [float(self.ppf(0.01)), float(self.ppf(0.99))]
        return [self.lo, self.hi]

    def describe(self) -> dict:
        d = {"dist": self.dist, "source": self.source}
        d.update({"a": self.a, "b": self.b} if self.dist == "beta" else {"range": [self.lo, self.hi]})
        if self.mode is not None:
            d["mode"] = self.mode
        return d


class Problem:
    """A posterior problem: named parameters with priors, and a vectorised log-likelihood
    `loglik({name: array(n)}) -> (ll array(n), {aux quantity: array(n)})`. `derived` maps a derived quantity's name
    to a function of the parameter arrays (classed like a parameter, e.g. a source's net bias)."""

    def __init__(self, names, priors, loglik_fn, derived=None, chunk=4096):
        self.names = list(names)
        miss = [n for n in self.names if n not in priors]
        if miss:
            raise ValueError(f"no prior for {miss} (no silent defaults: a parameter needs a prior or a fixed value)")
        self.priors = {n: priors[n] for n in self.names}
        self.loglik_fn, self.derived, self.chunk = loglik_fn, dict(derived or {}), int(chunk)
        self.n_evals = 0

    def x_of_z(self, Z):
        from scipy.special import ndtr
        U = np.clip(ndtr(np.asarray(Z, float)), 1e-15, 1 - 1e-15)
        return np.stack([self.priors[n].ppf(U[:, j]) for j, n in enumerate(self.names)], axis=1)

    def evaluate(self, X):
        X = np.atleast_2d(np.asarray(X, float))
        lls, aux = [], {}
        for i in range(0, len(X), self.chunk):
            part = X[i:i + self.chunk]
            ll, ax = self.loglik_fn({n: part[:, j] for j, n in enumerate(self.names)})
            ll = np.asarray(ll, float).reshape(len(part))
            lls.append(np.where(np.isfinite(ll), ll, -np.inf))
            for k, v in (ax or {}).items():
                aux.setdefault(k, []).append(np.asarray(v, float).reshape(len(part)))
        self.n_evals += len(X)
        return np.concatenate(lls), {k: np.concatenate(v) for k, v in aux.items()}


def _logphi(Z):
    return -0.5 * np.sum(Z * Z, axis=1) - 0.5 * Z.shape[1] * math.log(2 * math.pi)


def _mvn_logpdf(Z, m, L):
    """log N(Z; m, L L^T) for a lower-triangular Cholesky factor L."""
    from scipy.linalg import solve_triangular
    d = len(m)
    y = solve_triangular(L, (Z - m).T, lower=True)
    return -0.5 * np.sum(y * y, axis=0) - np.sum(np.log(np.diag(L))) - 0.5 * d * math.log(2 * math.pi)


def _sample_id(names, rows) -> str:
    import hashlib
    h = hashlib.sha256(json_dumps(list(names)).encode())
    h.update(np.ascontiguousarray(np.asarray(rows, float)).tobytes())
    return h.hexdigest()


def json_dumps(x) -> str:
    import json
    return json.dumps(x, sort_keys=True, default=str)


def _importance(P, rng, seed, th):
    from scipy.special import logsumexp
    from scipy.stats import qmc
    d = len(P.names)
    n0 = 2 ** int(th["sobol_log2"])
    U = np.clip(qmc.Sobol(d, scramble=True, seed=int(seed)).random(n0), 1e-12, 1 - 1e-12)
    from scipy.special import ndtri
    Z = ndtri(U)
    X = P.x_of_z(Z)
    ll, aux = P.evaluate(X)
    comps = [("prior", n0, None, None)]
    lp = _logphi(Z)
    stages, ess = 0, 0.0

    def weights():
        logq = []
        for kind, n, m, L in comps:
            lq = lp if kind == "prior" else _mvn_logpdf(Z, m, L)
            logq.append(lq + math.log(n / len(Z)))
        lw = ll + lp - logsumexp(np.stack(logq), axis=0)
        lw = np.where(np.isfinite(lw), lw, -np.inf)
        if not np.isfinite(lw).any():
            return None, 0.0
        w = np.exp(lw - lw.max())
        w /= w.sum()
        return w, float(1.0 / np.sum(w * w))
    w, ess = weights()
    while w is not None and (stages < 1 or ess < 2 * th["ess_floor"]) and stages < int(th["max_stages"]):
        m = w @ Z
        C = (Z - m).T @ ((Z - m) * w[:, None])
        h = 2.0 * (1.0 / n0) ** (1.0 / d)                   # about the first stage's point spacing, in z units
        C = 2.0 * (C + C.T) / 2.0 + h * h * np.eye(d)
        L = np.linalg.cholesky(C)
        ns = 2 ** int(th["stage_log2"])
        Zs = m + rng.standard_normal((ns, d)) @ L.T
        lls, auxs = P.evaluate(P.x_of_z(Zs))
        Z, X = np.vstack([Z, Zs]), np.vstack([X, P.x_of_z(Zs)])
        ll = np.concatenate([ll, lls])
        aux = {k: np.concatenate([aux[k], auxs[k]]) for k in aux}
        lp = _logphi(Z)
        comps.append(("gauss", ns, m, L))
        stages += 1
        w, ess = weights()
    if w is None or ess < th["ess_floor"]:
        raise CalibrationRefused("posterior_ess", f"effective sample size {ess:.0f} below the floor "
                                                  f"{th['ess_floor']} after {stages} adaptive stages")
    M = int(th["draws_kept"])
    pos = (rng.random() + np.arange(M)) / M                     # systematic resampling
    idx = np.minimum(np.searchsorted(np.cumsum(w), pos), len(w) - 1)
    best = int(np.argmax(ll))
    return {"method": "importance", "rows": X[idx], "aux_rows": {k: v[idx] for k, v in aux.items()},
            "ll_rows": ll[idx], "best": X[best], "best_ll": float(ll[best]),
            "diagnostics": {"ess": ess, "points": int(len(Z)), "adaptive_stages": stages,
                            "points_failed": int(np.sum(~np.isfinite(ll))), "sobol_points": n0}}


def _rank_normalise(x):
    from scipy.special import ndtri
    from scipy.stats import rankdata
    r = rankdata(x.ravel()).reshape(x.shape)
    return ndtri((r - 0.375) / (x.size + 0.25))


def _split(x):
    n = x.shape[1] // 2
    return np.vstack([x[:, :n], x[:, n:2 * n]])


def _rhat(x):
    m, n = x.shape
    w = np.mean(np.var(x, axis=1, ddof=1))
    b = n * np.var(np.mean(x, axis=1), ddof=1)
    return float(math.sqrt(((n - 1) / n * w + b / n) / w)) if w > 0 else float("inf")


def _ess(x):
    m, n = x.shape
    f = np.fft.rfft(x - x.mean(axis=1, keepdims=True), n=2 * n, axis=1)
    acov = np.fft.irfft(f * np.conj(f), axis=1)[:, :n] / n
    w = np.mean(acov[:, 0]) * n / (n - 1)
    vp = w * (n - 1) / n + (np.var(np.mean(x, axis=1), ddof=1) if m > 1 else 0.0)
    if vp <= 0:
        return float(m * n)
    rho = 1.0 - (w - np.mean(acov, axis=0)) / vp
    rho[0] = 1.0
    s, prev = 0.0, float("inf")
    for t in range(0, n - 1, 2):
        p = rho[t] + rho[t + 1]
        if p < 0:
            break
        p = min(p, prev)
        s += p
        prev = p
    tau = max(-1.0 + 2.0 * s, 1.0 / math.log10(m * n + 10))
    return float(m * n / tau)


def mcmc_diagnostics(chains) -> tuple:
    """(max rank-normalised split R-hat incl. folded, min bulk ESS) over the parameters of chains (C, N, d)."""
    rh, es = [], []
    for j in range(chains.shape[2]):
        x = _split(chains[:, :, j])
        z = _rank_normalise(x)
        zf = _rank_normalise(np.abs(x - np.median(x)))
        rh.append(max(_rhat(z), _rhat(zf)))
        es.append(_ess(z))
    return max(rh), min(es)


def _metropolis(P, rng, th):
    d, C = len(P.names), int(th["chains"])
    Z = rng.standard_normal((C, d))
    ll, ax = P.evaluate(P.x_of_z(Z))
    for _ in range(50):                           # start every chain where the likelihood is finite
        bad = ~np.isfinite(ll)
        if not bad.any():
            break
        Z[bad] = rng.standard_normal((int(bad.sum()), d))
        ll2, ax2 = P.evaluate(P.x_of_z(Z[bad]))
        ll[bad] = ll2
        for k in ax:
            ax[k][bad] = ax2[k]
    if not np.all(np.isfinite(ll)):
        raise CalibrationRefused("posterior_mcmc", "no finite starting point for every chain")
    lp = ll + _logphi(Z)
    S = np.eye(d) * 0.1
    scale = 2.38 ** 2 / d
    hist = []
    acc_w = []

    def step(Z, ll, lp, ax, L):
        Zp = Z + rng.standard_normal((C, d)) @ L.T
        llp, axp = P.evaluate(P.x_of_z(Zp))
        lpp = llp + _logphi(Zp)
        a = np.log(rng.random(C)) < (lpp - lp)
        Z = np.where(a[:, None], Zp, Z)
        ll, lp = np.where(a, llp, ll), np.where(a, lpp, lp)
        ax = {k: np.where(a, axp[k], ax[k]) for k in ax}
        return Z, ll, lp, ax, a
    W = int(th["warmup"])
    L = np.linalg.cholesky(scale * S)
    for t in range(W):
        Z, ll, lp, ax, a = step(Z, ll, lp, ax, L)
        hist.append(Z.copy())
        acc_w.append(a.mean())
        if t >= 300 and t % 100 == 0:
            H = np.concatenate(hist[len(hist) // 2:])
            S = np.cov(H.T).reshape(d, d) + 1e-8 * np.eye(d)
            rate = float(np.mean(acc_w[-100:]))
            scale *= math.exp(rate - 0.234)
            L = np.linalg.cholesky(scale * S)
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
        ch = np.stack(draws_z, axis=1)                       # (C, N, d)
        rhat, essb = mcmc_diagnostics(ch)
        if (rhat <= th["rhat_max"] and essb >= th["ess_bulk_min"]) or doublings >= int(th["mcmc_max_doublings"]):
            break
        doublings += 1
        target *= 2
    if not (rhat <= th["rhat_max"] and essb >= th["ess_bulk_min"]):
        raise CalibrationRefused("posterior_mcmc", f"R-hat {rhat:.4f} (max {th['rhat_max']}) and bulk ESS "
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
            "diagnostics": {"chains": C, "warmup": W, "draws_per_chain": ch.shape[1], "rhat_max": rhat,
                            "ess_bulk_min": essb, "acceptance": float(np.mean(acc)), "doublings": doublings}}


def sample(P: Problem, seed: int = 0, **override) -> dict:
    """The canonical joint posterior sample of a Problem (v2d §3.1): importance sampling up to
    `max_importance_params` parameters, adaptive Metropolis above; deterministic for a seed; fails closed below the
    ESS / R-hat floors. Rows are joint draws (correlations carried), with an id over names and values."""
    th = dict(thresholds(), **override)
    rng = np.random.default_rng([int(seed), 11])
    d = len(P.names)
    if d == 0:
        raise ValueError("posterior: nothing to calibrate")
    out = _importance(P, rng, seed, th) if d <= int(th["max_importance_params"]) else _metropolis(P, rng, th)
    out.update(names=list(P.names), seed=int(seed), sample_id=_sample_id(P.names, out["rows"]),
               evaluations=P.n_evals, priors={n: P.priors[n].describe() for n in P.names})
    return out


def _derived_values(P, X):
    return {k: np.asarray(f({n: X[:, j] for j, n in enumerate(P.names)}), float) for k, f in P.derived.items()}


def classify(P: Problem, smp: dict, **override) -> dict:
    """Identifiability classes (v2d §3.2): likelihood classes from the Fisher information of the likelihood alone in
    prior-scale units (flat directions, ridge loadings), contraction 1 - var_post/var_prior on each prior's own
    scale, and the combined class: prior_dominated (on_ridge, unidentified, or contraction < 0.1), identified
    (likelihood identified and contraction >= 0.5) or partly_identified. Derived quantities are classed the same way
    (gradient projected on the flat directions; contraction on the log scale when positive)."""
    th = dict(thresholds(), **override)
    names, d = P.names, len(P.names)
    sd = np.array([math.sqrt(P.priors[n].natural_var()) for n in names])
    t0 = np.array([float(P.priors[n].natural(smp["best"][j])) for j, n in enumerate(names)])
    h = 0.01
    for j, n in enumerate(names):                         # keep the stencil inside the prior's support
        lo, hi = P.priors[n].natural_support()
        pad = 2.5 * h * sd[j]
        t0[j] = min(max(t0[j], lo + pad), hi - pad)
    pts = [np.zeros(d)]
    for i in range(d):
        for s in (1, -1):
            e = np.zeros(d)
            e[i] = s * h
            pts.append(e)
    pairs = [(i, k) for i in range(d) for k in range(i + 1, d)]
    for i, k in pairs:
        for si, sk in ((1, 1), (1, -1), (-1, 1), (-1, -1)):
            e = np.zeros(d)
            e[i], e[k] = si * h, sk * h
            pts.append(e)
    S = np.asarray(pts)
    T = t0 + S * sd
    X = np.stack([P.priors[n].from_natural(T[:, j]) for j, n in enumerate(names)], axis=1)
    ll, _ = P.evaluate(X)
    f = -ll
    H = np.zeros((d, d))
    f0 = f[0]
    for i in range(d):
        H[i, i] = (f[1 + 2 * i] - 2 * f0 + f[2 + 2 * i]) / h ** 2
    base = 1 + 2 * d
    for q, (i, k) in enumerate(pairs):
        a, b, c, e = f[base + 4 * q: base + 4 * q + 4]
        H[i, k] = H[k, i] = (a - b - c + e) / (4 * h * h)
    fin = bool(np.all(np.isfinite(H)))
    if not fin:
        H = np.nan_to_num(H, nan=0.0, posinf=0.0, neginf=0.0)
    lam, V = np.linalg.eigh(0.5 * (H + H.T))
    flat = lam < th["flat_eigenvalue"]
    Vf = V[:, flat]
    lmin = th["ridge_loading_min"]
    ridges = []
    for j in np.nonzero(flat)[0]:
        loaded = [names[i] for i in range(d) if abs(V[i, j]) >= lmin]
        if len(loaded) >= 2:
            ridges.append({"params": loaded, "eigenvalue": float(lam[j]),
                           "loadings": {names[i]: round(float(V[i, j]), 3) for i in range(d) if abs(V[i, j]) >= lmin}})
    sub = [names[i] for i in range(d) if Vf.size and float(np.sum(Vf[i, :] ** 2)) >= lmin ** 2]
    if len(sub) >= 2 and Vf.shape[1] > 1:      # several flat directions: also name the whole flat subspace
        ridges.append({"params": sub, "kind": "flat_subspace", "dimension": int(Vf.shape[1])})
    lik, share = {}, {}
    for i, n in enumerate(names):
        sh = float(np.sum(Vf[i, :] ** 2)) if Vf.size else 0.0
        share[n] = sh
        lik[n] = ("unidentified" if H[i, i] < th["flat_eigenvalue"] else "on_ridge" if sh >= lmin ** 2
                  else "identified")
    rows = np.asarray(smp["rows"], float)
    contraction = {}
    for j, n in enumerate(names):
        contraction[n] = float(1.0 - np.var(P.priors[n].natural(rows[:, j])) / P.priors[n].natural_var())
    if P.derived:
        prng = np.random.default_rng(12345)
        U = prng.random((20000, d))
        Xp = np.stack([P.priors[n].ppf(U[:, j]) for j, n in enumerate(names)], axis=1)
        dp, dq = _derived_values(P, Xp), _derived_values(P, rows)
        for k in P.derived:
            pos = bool(np.all(dp[k] > 0))
            nat = (lambda v: np.log(v)) if pos else (lambda v: v)
            vp = float(np.var(nat(dp[k])))
            contraction[k] = float(1.0 - np.var(nat(dq[k])) / vp) if vp > 0 else 0.0
            g = np.zeros(d)                                   # gradient in prior-scale units
            for j in range(d):
                e = np.zeros(d)
                e[j] = h * sd[j]
                tp, tm = t0 + e, t0 - e
                xp = np.array([[P.priors[n].from_natural(tp[i]) for i, n in enumerate(names)]])
                xm = np.array([[P.priors[n].from_natural(tm[i]) for i, n in enumerate(names)]])
                g[j] = (nat(_derived_values(P, xp)[k])[0] - nat(_derived_values(P, xm)[k])[0]) / (2 * h)
            gn = float(g @ g)
            sh = float(np.sum((g @ Vf) ** 2) / gn) if gn > 0 and Vf.size else 0.0
            curv = float(g @ H @ g / gn) if gn > 0 else 0.0
            share[k] = sh
            lik[k] = ("unidentified" if gn == 0 or curv < th["flat_eigenvalue"] else "on_ridge" if sh >= lmin ** 2
                      else "identified")
    classes = {}
    for n in list(names) + list(P.derived):
        c = contraction[n]
        if lik[n] in ("on_ridge", "unidentified") or c < th["contraction_prior_dominated"]:
            classes[n] = "prior_dominated"
        elif lik[n] == "identified" and c >= th["contraction_identified"]:
            classes[n] = "identified"
        else:
            classes[n] = "partly_identified"
    return {"classes": classes, "likelihood_classes": lik, "contraction": contraction, "ridge_share": share,
            "ridges": ridges, "fisher_eigenvalues": [float(x) for x in lam], "fisher_finite": fin,
            "method": "Fisher information of the likelihood alone at the best sample point, in prior-scale units "
                      "(each parameter on its prior's own scale / prior sd); flat eigenvalue < "
                      f"{th['flat_eigenvalue']}, loading >= {lmin}; contraction 1 - var_post/var_prior"}


# ------------------------------------------------------------------ prior-dominated: tests and label (§3.2)
PRIOR_RANGE_PREFIX = "prior_range_"
NOT_DETERMINED = ("prior_dominated", "partly_identified")


def prior_range_tests(cls: dict, P: Problem) -> list:
    """An automatic prior_range decision test for every prior-dominated or partly identified parameter (same shape as
    toolkit/analysis/decision_tests.prior_range_tests)."""
    out = []
    for n in P.names:
        if cls["classes"].get(n) in NOT_DETERMINED:
            pr = P.priors[n]
            out.append({"id": f"{PRIOR_RANGE_PREFIX}{n}", "type": "prior_range", "parameter": n,
                        "values": pr.interval(), "automatic": True, "class": cls["classes"][n],
                        "finding": f"decision_test:{PRIOR_RANGE_PREFIX}{n}",
                        "text": f"Does the plan change when {n} is at either end of its declared range?",
                        "source": pr.source})
    return out


def _fmt(x) -> str:
    return f"{x:.3g}"


def label_parts(cls: dict, P: Problem) -> dict:
    """The names the interval label carries: prior-dominated parameters with their declared ranges."""
    pd = [n for n in P.names if cls["classes"].get(n) == "prior_dominated"]
    parts = [f"{n} ({_fmt(P.priors[n].interval()[0])} to {_fmt(P.priors[n].interval()[1])})" for n in pd]
    lab = ("its width is set mainly by the declared range of " + ", ".join(parts)) if parts else ""
    return {"prior_dominated": pd, "label": lab}


# --------------------------------------------------------------------- draw provenance (§3.3, Codex finding 10)
def draws(smp: dict, n: int, seed: int) -> list:
    """n uncertainty draws, each a row of the canonical sample: {sample_id, row, values}."""
    rng = np.random.default_rng([int(seed), 13])
    M = len(smp["rows"])
    idx = rng.choice(M, size=int(n), replace=int(n) > M)
    return [{"sample_id": smp["sample_id"], "row": int(i),
             "values": {nm: float(smp["rows"][i][j]) for j, nm in enumerate(smp["names"])}} for i in idx]


def check_provenance(smp: dict, recorded: list, rtol: float = 1e-12) -> list:
    """Re-read the canonical sample and confirm every recorded draw's applied values equal its row (fail closed)."""
    out = []
    rows = np.asarray(smp["rows"], float)
    sid = _sample_id(smp["names"], rows)
    if sid != smp.get("sample_id"):
        out.append({"kind": "analysis_contract_uncovered_parameter", "detail": "the canonical sample does not match "
                                                                                 "its recorded id"})
    for k, r in enumerate(recorded or []):
        if r.get("sample_id") != sid:
            out.append({"kind": "analysis_contract_uncovered_parameter", "draw": k,
                        "detail": "the draw cites another sample"})
            continue
        i = r.get("row")
        if not isinstance(i, int) or not 0 <= i < len(rows):
            out.append({"kind": "analysis_contract_uncovered_parameter", "draw": k, "detail": f"row {i!r} not in "
                                                                                               "the sample"})
            continue
        vals = r.get("values") or {}
        for j, nm in enumerate(smp["names"]):
            v = vals.get(nm)
            ref = float(rows[i, j])
            if not isinstance(v, (int, float)) or abs(float(v) - ref) > rtol * max(1.0, abs(ref)):
                out.append({"kind": "analysis_contract_uncovered_parameter", "draw": k, "parameter": nm,
                            "detail": f"applied {v!r}, canonical sample row {i} has {ref!r}"})
    return out


def spread_alarm(smp: dict, recorded: list, ratio: float | None = None) -> list:
    """Missing-variation alarm (not a coverage certificate): realised sd below ratio x the sample's sd."""
    ratio = thresholds()["spread_ratio_alarm"] if ratio is None else ratio
    rows = np.asarray(smp["rows"], float)
    out = []
    if len(recorded or []) < 8:
        return out
    for j, nm in enumerate(smp["names"]):
        s = float(np.std(rows[:, j]))
        r = float(np.std([x["values"].get(nm, np.nan) for x in recorded]))
        if s > 0 and not r >= ratio * s:
            out.append({"kind": "draws_missing_variation", "parameter": nm, "sample_sd": s, "draw_sd": r})
    return out


def interval(v, level=0.95) -> list:
    v = np.asarray(v, float)
    a = (1 - level) / 2
    return [float(np.quantile(v, a)), float(np.quantile(v, 1 - a))]


# --------------------------------------------- the allocation path: `posterior` on a count panel (v2d §3.1)
def panel_problem(spec, model, rows=None, fixed_z=None, fixed_params=None):
    """The posterior Problem of a count panel: the spec's calibrated engine parameters and the per-unit attribute, each
    with its declared prior (`calibration.priors`: one per parameter and one for the attribute, applied per unit),
    under the declared observation model (poisson, or negbin with a declared dispersion)."""
    from toolkit.analysis import observation as ob
    cal = spec["calibration"]
    L = Likelihood(spec, model, rows, fixed_z)
    fixed_params = dict(fixed_params or {})
    pri_decl = cal.get("priors") or {}
    miss = [p for p in L.params + [cal["attribute"]] if p not in pri_decl and p not in fixed_params]
    if miss:
        raise ValueError(f"calibration.priors: no prior for {miss} (no silent defaults)")
    names = [p for p in L.params if p not in fixed_params] + [f"z:{u}" for u in L.units]
    pri = {p: Prior.from_decl(p, pri_decl[p]) for p in L.params if p not in fixed_params}
    pri.update({f"z:{u}": Prior.from_decl(f"z:{u}", pri_decl[cal["attribute"]]) for u in L.units})
    obs = cal.get("observation") or {}
    model_ = obs.get("model")
    if model_ not in ("poisson", "negbin"):
        raise ValueError("calibration.observation.model: poisson | negbin (a count panel)")
    k = obs.get("dispersion") if model_ == "negbin" else None
    y = np.array([r["observed"] for r in L.rows])

    def ll(X):
        n = len(next(iter(X.values())))
        out = np.full(n, -np.inf)
        for i in range(n):
            x = dict(fixed_params, **{nm: float(X[nm][i]) for nm in names})
            mu = np.maximum(L.expected(x), 1e-300)
            out[i] = float(np.sum(ob.loglik(model_, y, mu=mu, **({"dispersion": float(k)} if k else {}))))
        return out, {}
    P = Problem(names, pri, ll, chunk=256)
    return P, L


def _map_point(P, start=None):
    """The posterior mode in z = Phi^-1(F(x)) coordinates (the plan's point calibration; the same estimator in the
    full fit and in every stability fold)."""
    from scipy.optimize import minimize
    from scipy.special import ndtri
    d = len(P.names)
    if start is None:
        z0 = np.zeros(d)
    else:
        u = []
        for j, n in enumerate(P.names):
            pr = P.priors[n]
            lo, hi = pr.natural_support() if pr.dist != "beta" else (0.0, 1.0)
            if pr.dist in ("uniform", "loguniform"):
                u.append((float(pr.natural(start[j])) - lo) / (hi - lo))
            else:
                from scipy import stats
                u.append(float(stats.beta.cdf(start[j], pr.a, pr.b)) if pr.dist == "beta" else 0.5)
        z0 = ndtri(np.clip(u, 1e-9, 1 - 1e-9))
    f = lambda z: -(float(P.evaluate(P.x_of_z(z[None, :]))[0][0]) - 0.5 * float(z @ z))  # noqa: E731
    r = minimize(f, z0, method="L-BFGS-B", options={"maxiter": 500})
    return P.x_of_z(r.x[None, :])[0], bool(r.success), str(r.message)


def fit_posterior_panel(spec, model, rows=None, fixed_z=None, fixed_params=None, seed=None, sample_it=True) -> dict:
    """`posterior` calibration of a count panel: the canonical joint posterior sample (params and every unit
    attribute), identifiability classes, the point (posterior mode) the plan is solved at, automatic prior_range
    tests for prior-dominated parameters and the label's names. With sample_it False only the point (stability folds,
    prior_range tests)."""
    P, L = panel_problem(spec, model, rows, fixed_z, fixed_params)
    s = c = None
    if sample_it:
        sd = spec["calibration"].get("seed", 0) if seed is None else seed
        s = sample(P, seed=int(sd))
        c = classify(P, s)
    xh, conv, msg = _map_point(P, s["best"] if s else None)
    x = dict(fixed_params or {}, **dict(zip(P.names, (float(v) for v in xh))))
    out = {"method": "posterior", "x": x, "names": list(P.names), "params": L.params, "units_fitted": L.units,
           "fixed_z": L.fixed_z, "fixed_params": dict(fixed_params or {}),
           "theta": {p: x[p] for p in L.params}, "z": {u: x[f"z:{u}"] for u in L.units},
           "diagnostics": {"converged": conv, "message": msg, "n_targets": len(L.rows), "n_parameters": len(P.names),
                           "at_bound": [], "observation": (spec["calibration"].get("observation") or {}).get("model")}}
    if s is not None:
        out["sample"] = {"sample_id": s["sample_id"], "names": s["names"], "rows": s["rows"].tolist(),
                         "method": s["method"], "seed": s["seed"]}
        out["diagnostics"].update(sampler=s["method"], **{k: v for k, v in s["diagnostics"].items()})
        pr_tests = [t for t in prior_range_tests(c, P) if not t["parameter"].startswith("z:")]
        lab = label_parts(c, P)
        out["identifiability"] = {"method": c["method"], "classes": c["classes"],
                                  "likelihood_classes": c["likelihood_classes"], "contraction": c["contraction"],
                                  "ridges": c["ridges"],
                                  "status": ("well_identified" if all(v == "identified" for v in c["classes"].values())
                                             else "partly_or_prior_dominated")}
        out["prior_range_tests"] = pr_tests
        out["prior_dominated"] = [n for n in lab["prior_dominated"] if not n.startswith("z:")]
        out["priors"] = {n: P.priors[n].describe() for n in P.names}
    return out


def sample_of(lik: dict) -> dict:
    """The canonical sample of a posterior calibration as check_provenance reads it."""
    s = lik["sample"]
    return {"sample_id": s["sample_id"], "names": s["names"], "rows": np.asarray(s["rows"], float)}


# ------------------------------------------------- the engine-backed posterior (the coverage pack's calibrator)
def _ref(v, data, X=None):
    """Resolve a declaration value: 'data:<key>[.<key>]' reads the data set, 'param:<name>' a sampled parameter (an
    array over rows; a latent vector gives rows x K), lists element-wise, anything else literally."""
    if isinstance(v, str) and v.startswith("data:"):
        cur = data
        for k in v[5:].split("."):
            if not isinstance(cur, dict) or k not in cur:
                raise ValueError(f"{v}: not in the data set")
            cur = cur[k]
        return cur
    if isinstance(v, str) and v.startswith("param:"):
        if X is None or v[6:] not in X:
            raise ValueError(f"{v}: not a calibrated or fixed parameter")
        return X[v[6:]]
    if isinstance(v, list):
        return [_ref(x, data, X) for x in v]
    if isinstance(v, dict):
        return {k: _ref(x, data, X) for k, x in v.items()}
    return v


def _row(v, i):
    """Value of a resolved declaration for row i (arrays over rows are indexed, everything else is shared)."""
    if isinstance(v, np.ndarray):
        return v[i].tolist() if v.ndim > 1 else float(v[i])
    if isinstance(v, list):
        return [_row(x, i) for x in v]
    if isinstance(v, dict):
        return {k: _row(x, i) for k, x in v.items()}
    return v


def _varies(v) -> bool:
    if isinstance(v, np.ndarray):
        return True
    if isinstance(v, list):
        return any(_varies(x) for x in v)
    if isinstance(v, dict):
        return any(_varies(x) for x in v.values())
    return False


def _data_hash(data) -> int:
    import hashlib
    return int(hashlib.sha256(json_dumps(data).encode()).hexdigest()[:8], 16)


STATE_LATENT = ("coverage", "immunity")


class EngineProblem:
    """Builds the posterior Problem of a declared calibration on a configured engine (v2d §3.1, §3.4): parameters with
    priors (global, latent vectors, and every source's bias and frame-gap parameters), observation sources with their
    measurement models, targets, and the quantities reported. The likelihood reads the engine's `initial_state()` when
    every target observes coverage or immunity (no run), else its `run_batch()`."""

    def __init__(self, make_engine, data, config, priors=None, latent=None, fixed=None, engine_params=None,
                 unit=None, sources=None, targets=None, quantities=None, treat_as_truth=None, horizon=None):
        from toolkit.analysis import observation as ob
        self.eng = make_engine(config)
        self.data, self.config = data, config
        self.fixed = {k: float(_ref(v, data)) for k, v in (fixed or {}).items()}
        self.engine_params = dict(engine_params or {})
        self.unit_decl = dict(unit or {})
        self.findings = []
        decls = [_ref(s, data) for s in (sources or [])]
        bad = ob.check_sources(decls)
        if bad:
            raise CalibrationRefused(bad[0][0], "; ".join(m for _, m in bad))
        ids = [d["id"] for d in decls]
        if treat_as_truth is not None and treat_as_truth not in ids:
            raise ValueError(f"treat_as_truth: {treat_as_truth!r} is not a declared source")
        self.sources = {d["id"]: ob.Source(d, as_truth=(d["id"] == treat_as_truth)) for d in decls}
        if treat_as_truth:
            self.findings.append({"kind": "single_source_treated_as_truth", "severity": "HIGH",
                                  "detail": f"{treat_as_truth}: biases and frame gaps dropped (negative control)"})
        self.targets = []
        for t in targets or []:
            if t.get("source") not in self.sources:
                raise CalibrationRefused("observation_source_undeclared", f"target {t!r} names no declared source")
            self.targets.append(dict(t))
        seen = {}
        for t in self.targets:
            seen.setdefault(str(t["latent"]).split(":")[0], set()).add(t["source"])
        for q, ss in seen.items():
            if len(ss) == 1:
                s = self.sources[next(iter(ss))]
                if s.as_truth or (not s.bias and not s.unbiased_reason):
                    self.findings.append({"kind": "single_source_treated_as_truth", "severity": "HIGH",
                                          "detail": f"{q}: observed by {s.id} only, with no bias model"})
        pri = {}
        for n, d in (priors or {}).items():
            if n not in self.fixed:
                pri[n] = Prior.from_decl(n, d)
        self.latent = {}
        for n, d in (latent or {}).items():
            k = len(_ref(d["n"], data)) if not isinstance(d["n"], int) else int(d["n"])
            self.latent[n] = [f"{n}[{i}]" for i in range(k)]
            for m in self.latent[n]:
                pri[m] = Prior.from_decl(m, d["prior"])
        gaps = {}
        for s in self.sources.values():
            for p, d in s.priors().items():
                if p in self.fixed:
                    continue
                if p in pri and pri[p].describe() != Prior.from_decl(p, d).describe():
                    raise ValueError(f"{p}: declared with two different priors")
                pri.setdefault(p, Prior.from_decl(p, d))
            for e in s.excluded:
                gaps[e["param"]] = e
        self.gaps = list(gaps.values())
        names = list(pri)
        derived = {s.net_name(): (lambda X, s=s: s.net_multiplier(self._vals(X)))
                   for s in self.sources.values() if s.n_multiplicative() >= 2}
        self.state_only = (all(str(t["latent"]).split(":")[0] in STATE_LATENT for t in self.targets)
                           and callable(getattr(self.eng, "initial_state", None)))
        self.quantities = dict(quantities or {})
        self.horizon_decl = horizon or {}
        self.problem = Problem(names, pri, self.loglik, derived=derived, chunk=4096)

    # ---- helpers
    def _vals(self, X):
        out = dict(X)
        n = len(next(iter(X.values()))) if X else 1
        for k, v in self.fixed.items():
            out[k] = np.full(n, v)
        for name, cols in self.latent.items():
            out[name] = np.stack([out[c] for c in cols], axis=1)
        return out

    def _horizon(self):
        if self.eng.mode == "windowed":
            hd = self.horizon_decl
            if "weeks" in hd:
                stop = 7.0 * float(_ref(hd["weeks"], self.data))
            else:
                stop = float(_ref(hd.get("steps"), self.data)) * self.eng.dt_days
            return Horizon(0.0, stop, (0.0, stop), {"all": (0.0, stop)})
        big = (self.eng.step_cap + 2) * self.eng.dt_days
        return Horizon(0.0, big, (0.0, big), {"all": (0.0, big)})

    def _units(self, V, n):
        from toolkit.engines.contract import UnitSpec
        res = {k: _ref(v, self.data, V) for k, v in self.unit_decl.items()}
        if not any(_varies(v) for v in res.values()):
            u = UnitSpec("cal", {k: _row(v, 0) for k, v in res.items()})
            return [u] * n
        return [UnitSpec("cal", {k: _row(v, i) for k, v in res.items()}) for i in range(n)]

    def _params(self, V, n):
        res = {k: _ref(v, self.data, V) for k, v in self.engine_params.items()}
        return [{k: _row(v, i) for k, v in res.items()} for i in range(n)]

    def _run(self, params, units, packages):
        """run_batch with fail-closed members isolated: a batch the engine refuses is split until each refused member
        stands alone; those members get no outputs (their likelihood is -inf, counted)."""
        pk = packages if isinstance(packages, list) else [packages] * len(params)
        try:
            return self.eng.run_batch(params, units, pk, self._horizon(), 1)
        except RuntimeError:
            if len(params) == 1:
                return [None]
            h = len(params) // 2
            return self._run(params[:h], units[:h], pk[:h]) + self._run(params[h:], units[h:], pk[h:])

    SUB_BATCH = 512

    def _run_sorted(self, params, units, packages):
        """run_batch over sub-batches of similar members (sorted by their parameter values): in to_extinction mode a
        batch runs until its slowest member converges, so near-critical members are kept together."""
        n = len(params)
        pk = packages if isinstance(packages, list) else [packages] * n
        key = lambda i: tuple(sorted((k, v) for k, v in params[i].items() if isinstance(v, (int, float))))  # noqa
        order = sorted(range(n), key=key)
        out = [None] * n
        for a in range(0, n, self.SUB_BATCH):
            idx = order[a:a + self.SUB_BATCH]
            res = self._run([params[i] for i in idx], [units[i] for i in idx], [pk[i] for i in idx])
            for i, o in zip(idx, res):
                out[i] = o
        return out

    def evaluate_state(self, V, n):
        """Per row: frame-population state (s_start per group, sizes) from initial_state or run_batch outputs."""
        from toolkit.engines.contract import Package
        params, units = self._params(V, n), self._units(V, n)
        if self.state_only:
            sts = [self.eng.initial_state(p, u, Package("none", {})) for p, u in zip(params, units)]
            return [{"s_start": np.asarray(s["s_start"]), "N": np.asarray(s["N"]), "unit": u.attributes}
                    for s, u in zip(sts, units)], None
        outs = self._run_sorted(params, units, Package("none", {}))
        st = []
        for o, u in zip(outs, units):
            if o is None:
                st.append(None)
                continue
            st.append({"s_start": np.asarray(o.meta["s_start"]), "N": np.asarray(u.attributes["N"], float),
                       "unit": u.attributes, "out": o})
        return st, outs

    def _years(self, t, n):
        y = t.get("years")
        if not y:
            return None
        sy = float(_ref(y["survey_year"], self.data))
        by = np.asarray(_ref(y["birth_year"], self.data), float)
        return np.maximum(sy - by - float(y.get("age_at_vaccination", 0)), 0.0)

    def _target_ll(self, t, lat, vals, i):
        from toolkit.analysis import observation as ob
        s = self.sources[t["source"]]
        v1 = {k: (v[i] if isinstance(v, np.ndarray) and v.ndim == 1 else v) for k, v in vals.items()}
        e = s.expected(lat, v1, years=self._years(t, None))
        y = np.asarray(_ref(t["observed"], self.data), float)
        nz = s.noise
        m = nz["model"]
        if m in ("negbin", "poisson"):
            mu = e
            kw = {"dispersion": float(_ref(nz["dispersion"], self.data))} if m == "negbin" else {}
            return float(np.sum(ob.loglik(m, y, mu=np.maximum(mu, 1e-300), **kw)))
        if m == "lognormal":
            return float(np.sum(ob.loglik(m, y, mu=e, sd=float(_ref(nz["sd"], self.data)))))
        n = np.asarray(_ref(t["n"], self.data), float) * np.ones_like(y)
        p = np.clip(e, 0.0, 1.0)
        if m == "binomial":
            return float(np.sum(ob.loglik(m, y, n=n, p=p)))
        return float(np.sum(ob.loglik(m, y, n=n, p=p, design_effect=float(_ref(s.design_effect, self.data)))))

    def _latent(self, t, st, V, i):
        q = str(t["latent"])
        if q == "incidence":
            o = st["out"]
            inc = np.asarray(o.meta["incidence"], float) * float(np.sum(st["N"]))
            k = int(t.get("aggregate", 1))         # e.g. daily steps reported weekly
            if k > 1:
                if len(inc) % k:
                    raise ValueError(f"target aggregate {k}: {len(inc)} steps is not a whole number of periods")
                inc = inc.reshape(-1, k).sum(axis=1)
            return inc
        if q == "immunity":
            return 1.0 - st["s_start"]
        if q.startswith("coverage:"):
            return np.asarray(st["unit"][q], float)
        if q == "attack_rate":
            return np.asarray([st["out"].windows["all"][f"attack_rate:{g}"] for g in self.eng.groups], float)
        raise ValueError(f"target latent {q!r} not in incidence | immunity | coverage:<dose> | attack_rate")

    def unit_susceptible(self, st, vals, i):
        imm = 1.0 - st["s_start"]
        for e in self.gaps:
            phi = float(e["share"])
            rel = float(vals[e["param"]][i])
            imm = (1.0 - phi) * imm + phi * np.minimum(rel * imm, 1.0)
        w = st["N"] / np.sum(st["N"])
        return float(np.sum(w * (1.0 - imm)))

    def loglik(self, X):
        n = len(next(iter(X.values())))
        V = self._vals(X)
        sts, _ = self.evaluate_state(V, n)
        ll = np.full(n, -np.inf)
        aux = {k: np.full(n, np.nan) for k in self._aux_keys()}
        for i, st in enumerate(sts):
            if st is None:
                continue
            tot = 0.0
            for t in self.targets:
                tot += self._target_ll(t, self._latent(t, st, V, i), V, i)
            ll[i] = tot
            for k in aux:
                aux[k][i] = self._aux(k, st, V, i)
        return ll, aux

    def _aux_keys(self):
        keys = []
        for q, d in self.quantities.items():
            if isinstance(d, str):
                keys.append(q)
            elif isinstance(d, dict) and d.get("scenario"):
                keys += [f"_s_end:{g}" for g in self.eng.groups]
        return list(dict.fromkeys(keys))

    def _aux(self, k, st, V, i):
        if k.startswith("_s_end:"):
            g = k.split(":", 1)[1]
            j = self.eng.groups.index(g)
            return float(st["s_start"][j] - st["out"].windows["all"][f"attack_rate:{g}"])
        d = self.quantities[k]
        if d.startswith("window:"):
            return float(st["out"].windows["all"][d[7:]])
        if d == "unit_susceptible_share":
            return self.unit_susceptible(st, V, i)
        raise ValueError(f"quantity {k}: {d!r} not in window:<metric> | unit_susceptible_share | a scenario")

    def scenario(self, q, d, smp):
        """A quantity that needs a further engine run per draw: the unit after the calibrated epidemic (each group's
        susceptible share at its end), with the package against the comparator; `averted_count` = sum over groups
        of (comparator - package) per-capita outcome x group size."""
        from toolkit.engines.contract import Package, UnitSpec
        if d.get("scenario") != "after_calibrated_epidemic":
            raise ValueError(f"quantity {q}: scenario {d.get('scenario')!r} not in after_calibrated_epidemic")
        rows = np.asarray(smp["rows"])
        V = self._vals({n: rows[:, j] for j, n in enumerate(smp["names"])})
        n = len(rows)
        units = self._units(V, n)
        params = self._params(V, n)
        s_end = np.stack([smp["aux_rows"][f"_s_end:{g}"] for g in self.eng.groups], axis=1)
        u2 = [UnitSpec("next", dict(u.attributes, s0=[float(x) for x in s_end[i]])) for i, u in enumerate(units)]
        res = {}
        for tag, pk in (("with", d["package"]), ("without", d["comparator"])):
            outs = self._run_sorted(params, u2, [Package(tag, dict(pk))] * n)
            res[tag] = outs
        val = np.full(n, np.nan)
        o = d["outcome"]
        for i in range(n):
            a, b = res["without"][i], res["with"][i]
            if a is None or b is None:
                continue
            N = np.asarray(u2[i].attributes["N"], float)
            val[i] = float(sum((a.per_capita[o][g] - b.per_capita[o][g]) * N[j] for j, g in enumerate(self.eng.groups)))
        return val


def posterior(make_engine, data, config, level=0.95, seed=0, **decl) -> dict:
    """The harness `posterior` calibration of a declared problem on a configured engine (the coverage pack's
    calibrator, v2d §5): the canonical joint posterior sample, the intervals of the declared quantities from its rows,
    identifiability classes, automatic prior_range tests and the label's prior-dominated names. Fails closed."""
    E = EngineProblem(make_engine, data, config, **decl)
    P = E.problem
    s = sample(P, seed=(int(seed) * 7919 + _data_hash(data)) % (2 ** 31))
    if np.any(~np.isfinite(s["ll_rows"])):
        raise CalibrationRefused("posterior_ess", "a canonical draw has no finite likelihood")
    c = classify(P, s)
    iv = {}
    for q, d in E.quantities.items():
        v = E.scenario(q, d, s) if isinstance(d, dict) else s["aux_rows"][q]
        if not np.all(np.isfinite(v)):
            raise CalibrationRefused("posterior_quantity", f"{q}: non-finite in {int(np.sum(~np.isfinite(v)))} draws")
        iv[q] = interval(v, level)
    lab = label_parts(c, P)
    return {"intervals": iv, "classes": c["classes"], "likelihood_classes": c["likelihood_classes"],
            "contraction": c["contraction"], "ridges": c["ridges"], "sample_id": s["sample_id"], "method": s["method"],
            "diagnostics": s["diagnostics"], "evaluations": s["evaluations"], "level": level,
            "prior_range_tests": [t["id"] for t in prior_range_tests(c, P)], "label": lab["label"],
            "prior_dominated": lab["prior_dominated"], "fixed": E.fixed, "findings": E.findings,
            "evaluation": "initial_state" if E.state_only else "run_batch"}


# --------------------------------------------------------------------------- self-test (v2d β)
def _self_test() -> int:
    import tempfile
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)

    def safe(fn, msg):
        try:
            fn()
        except Exception as e:          # a missing function is a failing case, not a crash of the whole self-test
            ok(False, f"{msg} ({type(e).__name__}: {e})")

    def normal_problem(d, sd=0.05, mu=None, lo=-1.0, hi=1.0):
        mu = np.linspace(-0.3, 0.3, d) if mu is None else np.asarray(mu, float)
        names = [f"x{i}" for i in range(d)]
        pri = {n: Prior.from_decl(n, {"dist": "uniform", "range": [lo, hi], "source": "test"}) for n in names}

        def ll(X):
            v = np.stack([X[n] for n in names], axis=1)
            return -0.5 * np.sum(((v - mu) / sd) ** 2, axis=1), {"sum": v.sum(axis=1)}
        return Problem(names, pri, ll), mu

    def cb1():
        P, mu = normal_problem(2)
        s = sample(P, seed=3)
        m = np.mean(s["rows"], axis=0)
        sdv = np.std(s["rows"], axis=0)
        ok(s["method"] == "importance" and s["diagnostics"]["ess"] >= 1000 and np.max(np.abs(m - mu)) < 0.01
           and np.max(np.abs(sdv / 0.05 - 1)) < 0.08,
           f"CB1 importance sampling (<= 4 parameters) recovers a Gaussian posterior: mean {m}, sd {sdv}, "
           f"ESS {s['diagnostics'].get('ess')}")
        s2 = sample(P, seed=3)
        s3 = sample(P, seed=4)
        ok(s2["sample_id"] == s["sample_id"] and s3["sample_id"] != s["sample_id"],
           "CB2 deterministic seeding: the same seed gives the same canonical sample (same id); another seed does not")
        try:
            sample(P, seed=3, ess_floor=10 ** 9)
            ok(False, "CB3 an ESS below the floor fails closed")
        except CalibrationRefused as e:
            ok(e.code == "posterior_ess", f"CB3 an ESS below the floor fails closed ({e.code})")
    safe(cb1, "CB1-3 importance sampler")

    def cb4():
        P, mu = normal_problem(6, sd=0.1)
        s = sample(P, seed=5)
        m = np.mean(s["rows"], axis=0)
        sdv = np.std(s["rows"], axis=0)
        dg = s["diagnostics"]
        ok(s["method"] == "adaptive_metropolis" and dg["chains"] == 4 and dg["rhat_max"] <= 1.01
           and dg["ess_bulk_min"] >= 400 and np.max(np.abs(m - mu)) < 0.02 and np.max(np.abs(sdv / 0.1 - 1)) < 0.12,
           f"CB4 adaptive Metropolis (> 4 parameters): 4 chains, R-hat {dg.get('rhat_max')}, bulk ESS "
           f"{dg.get('ess_bulk_min')}, mean error {np.max(np.abs(m - mu)):.3f}, sd ratio {sdv / 0.1}")
        try:
            sample(P, seed=5, ess_bulk_min=10 ** 9)
            ok(False, "CB5 MCMC below its ESS floor fails closed")
        except CalibrationRefused as e:
            ok(e.code == "posterior_mcmc", f"CB5 MCMC below its ESS floor fails closed ({e.code})")
    safe(cb4, "CB4-5 adaptive Metropolis")

    def anb8():
        names = ["a", "b"]
        pri = {n: Prior.from_decl(n, {"dist": "uniform", "range": [0.9, 1.1], "source": "test"}) for n in names}
        P = Problem(names, pri, lambda X: (-0.5 * ((np.log(X["a"] * X["b"]) - math.log(1.1)) / 0.005) ** 2, {}))
        s = sample(P, seed=1)
        c = classify(P, s)
        ok(c["contraction"]["a"] > 0.5 and c["likelihood_classes"] == {"a": "on_ridge", "b": "on_ridge"}
           and c["classes"] == {"a": "prior_dominated", "b": "prior_dominated"}
           and any(set(r["params"]) == {"a", "b"} for r in c["ridges"]),
           f"AN-b8 a product-only-identified pair under narrow priors is on_ridge, not identified, although its "
           f"marginals contract ({c.get('contraction')}, {c.get('likelihood_classes')})")
        P2, _ = normal_problem(2)
        c2 = classify(P2, sample(P2, seed=1))
        ok(set(c2["classes"].values()) == {"identified"}, f"CB6 well-identified parameters are identified: {c2['classes']}")
        pri3 = dict(pri, c=Prior.from_decl("c", {"dist": "loguniform", "range": [0.1, 10.0], "source": "test"}))
        P3 = Problem(["a", "b", "c"], pri3, lambda X: (-0.5 * ((X["a"] - 1.0) / 0.01) ** 2, {}))
        c3 = classify(P3, sample(P3, seed=2))
        ok(c3["likelihood_classes"]["c"] == "unidentified" and c3["classes"]["c"] == "prior_dominated"
           and abs(c3["contraction"]["c"]) < 0.1 and c3["classes"]["a"] == "identified",
           f"CB7 a parameter the data do not see is unidentified and prior-dominated (contraction on its prior's own "
           f"log scale {c3['contraction'].get('c'):.3f})")
    safe(anb8, "AN-b8 / CB6-7 identifiability classes")

    def anb9():
        P, mu = normal_problem(2)
        s = sample(P, seed=3)
        rec = draws(s, 50, seed=9)
        ok(check_provenance(s, rec) == [], "AN-b9a draws taken as rows of the canonical sample pass the provenance check")
        mean = np.mean(s["rows"], axis=0)
        half = [dict(r, values={n: float(mean[j] + 0.5 * (r["values"][n] - mean[j])) for j, n in enumerate(s["names"])})
                for r in rec]
        f = check_provenance(s, half)
        ok(bool(f) and all(x["kind"] == "analysis_contract_uncovered_parameter" for x in f),
           f"AN-b9 draws at half the posterior sd fail the provenance check ({len(f)} findings)")
        other = [dict(r, sample_id="0" * 64) for r in rec]
        ok(check_provenance(s, other) != [], "AN-b9b draws citing another sample fail the provenance check")
    safe(anb9, "AN-b9 draw provenance")

    def anb10():
        from toolkit.analysis import observation as ob
        srcs = ob.toy_sources()
        srcs[1]["bias"] = {"none": "a reference survey with a validated assay (toy)"}
        srcs[0]["frame"]["excluded"] = []
        srcs[1]["frame"]["excluded"] = []
        A, S = ob.Source(srcs[0]), ob.Source(srcs[1])
        rng = np.random.default_rng(2)
        x_true, om, de = 0.7, 0.12, 0.05
        adm = A.expected(np.array([x_true]), {"omega": om, "delta": de})[0] * math.exp(rng.normal(0, 0.03))
        npos = int(rng.binomial(400, x_true))
        pri = {"x": Prior.from_decl("x", {"dist": "uniform", "range": [0.3, 0.95], "source": "test"})}
        pri.update({k: Prior.from_decl(k, v) for k, v in A.priors().items()})

        def ll(X):
            vals = {k: X[k] for k in ("omega", "delta")}
            e_adm = A.expected(X["x"], vals)
            out = ob.loglik("lognormal", np.full_like(X["x"], adm), mu=e_adm, sd=0.03)
            out = out + ob.loglik("binomial", np.full_like(X["x"], npos), n=400, p=S.expected(X["x"], {}))
            return out, {"x": X["x"]}
        P = Problem(["x", "omega", "delta"], pri, ll, derived={A.net_name(): lambda X: A.net_multiplier(X)})
        s = sample(P, seed=4)
        c = classify(P, s)
        tests = prior_range_tests(c, P)
        lab = label_parts(c, P)
        ok(c["classes"]["omega"] == "prior_dominated" and c["classes"]["x"] in ("identified", "partly_identified")
           and c["classes"]["net_bias:admin"] == "identified"
           and any(t["id"] == "prior_range_omega" and t["values"] == [0.0, 0.25] for t in tests)
           and "omega" in lab["prior_dominated"] and "omega" in lab["label"],
           f"AN-b10 a bias parameter seen by one source only is prior_dominated, gets a prior_range test and is named "
           f"in the label ({c.get('classes')}; tests {[t['id'] for t in tests]}; label {lab.get('label')!r})")
    safe(anb10, "AN-b10 one-source bias parameter")

    def anb3():
        from toolkit.analysis import run as R
        from toolkit.analysis.model import DirectModel
        from toolkit.spec.schema import load
        with tempfile.TemporaryDirectory() as d:
            R._direct_toy(d)
            spec = load(os.path.join(d, "decision_spec.yaml"))
            m = DirectModel(spec, log=lambda *a: None)
            m.fit()
            rows = [r for r in read_targets(spec) if r["package"] != "none"]
            try:
                fit_likelihood(spec, m, rows=rows)
                ok(False, "AN-b3 a ridge panel (no reference package) stops with calibration_ridge (today: jittered MVN)")
            except CalibrationRefused as e:
                ok(e.code == "calibration_ridge", f"AN-b3 a ridge panel stops with calibration_ridge ({e})")
            full = fit_likelihood(spec, m)
            ok(full["cov"] is not None, "AN-b3b the full panel (with the reference package) still fits")
    safe(anb3, "AN-b3 ridge refusal")

    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    print(__doc__)
