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

from toolkit.analysis.model import _logit
from toolkit.engines.contract import Horizon

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
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
    try:
        cov = np.linalg.inv(np.asarray(H))
        cov = (cov + cov.T) / 2
        ev = np.linalg.eigvalsh(cov)
        if ev.min() <= 0:
            cov = cov + (abs(ev.min()) + 1e-12) * np.eye(len(names_h))
    except np.linalg.LinAlgError:
        cov = None
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
