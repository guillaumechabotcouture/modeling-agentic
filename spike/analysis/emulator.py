#!/usr/bin/env python3
"""Gaussian-process emulators over (calibrated attribute, fitted engine parameters, unit
features) for every package, with held-out accuracy (analysis engine, architecture-v2 §3.4).

Emulated per population group P (spec decision_units.populations, each a set of engine age
groups) and design point:
  base[P][o]      log per-capita outcome o under the comparator package
  red[P][o][p]    proportional reduction 1 - y_p / y_comparator for package p (CRN contrast)
  treated[p]      per-capita treated cases (all ages), for per-outcome costs
  cal             logit of the calibration metric (comparator, calibration window)
  eir             log annual EIR in the calibration window (diagnostic)
One kernel is fitted per (P, p) on the primary outcome and reused for the other outcomes
(hyperparameters fixed), which keeps fitting cheap.
"""
from __future__ import annotations

import json
import math
import os
import warnings

import numpy as np


def _gp(X, y, kernel=None):
    from sklearn.gaussian_process import GaussianProcessRegressor
    from sklearn.gaussian_process.kernels import ConstantKernel, Matern, WhiteKernel
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        if kernel is None:
            k = ConstantKernel(1.0, (1e-3, 1e3)) * Matern(length_scale=np.ones(X.shape[1]), length_scale_bounds=(0.05, 50),
                                                         nu=2.5) + WhiteKernel(1e-3, (1e-6, 1e-1))
            gp = GaussianProcessRegressor(k, normalize_y=True, n_restarts_optimizer=1, random_state=0)
        else:
            gp = GaussianProcessRegressor(kernel, normalize_y=True, optimizer=None)
        gp.fit(X, y)
    return gp


def _logit(p):
    p = np.clip(p, 1e-4, 1 - 1e-4)
    return np.log(p / (1 - p))


class Emulator:
    def __init__(self, spec: dict, design: dict, results: dict, units_by_id: dict):
        self.spec = spec
        self.features = list((spec.get("emulator") or {}).get("features") or [])
        self.fitted_names = design["fitted_names"]
        self.packages = design["packages"]
        self.comp = spec["comparator"]["package"]
        self.pops = spec["decision_units"]["populations"]
        self.outcomes = list(spec.get("outcomes_reported") or [spec["objective"]["outcome"]])
        self.primary = spec["objective"]["outcome"]
        if self.primary in self.outcomes:
            self.outcomes.remove(self.primary)
        self.outcomes = [self.primary] + self.outcomes
        pts = design["points"]
        missing = [f"{p['point']}|{k}" for p in pts for k in self.packages if f"{p['point']}|{k}" not in results]
        self.partial = bool(os.environ.get("SPIKE_PARTIAL")) and bool(missing)
        if self.partial:   # test-only: keep complete design points; recorded in analysis.json
            pts = [p for p in pts if all(f"{p['point']}|{k}" in results for k in self.packages)]
            missing = []
        if missing:
            raise ValueError(f"{len(missing)} engine results missing (e.g. {missing[:3]}): the analysis fails closed")
        self.lo = None
        rows, Y = [], {}
        for p in pts:
            u = units_by_id[str(p["unit_id"])]
            rows.append(self._x(p["z"], p["theta"], u))
        X = np.asarray(rows, float)
        self.lo, self.hi = X.min(0), X.max(0)
        self.hold = np.array([bool(p["holdout"]) for p in pts])
        self.X = self._scale(X)
        self.T = {}
        get = lambda i, k: results[f"{pts[i]['point']}|{k}"]  # noqa: E731
        n = len(pts)
        base = {}
        for P, pm in self.pops.items():
            for o in self.outcomes:
                base[(P, o)] = np.array([self._group(get(i, self.comp), o, pm) for i in range(n)])
                self.T[("base", P, o)] = np.log(np.maximum(base[(P, o)], 1e-9))
                for k in self.packages:
                    if k == self.comp:
                        continue
                    y = np.array([self._group(get(i, k), o, pm) for i in range(n)])
                    self.T[("red", P, o, k)] = 1 - y / np.maximum(base[(P, o)], 1e-12)
        for k in self.packages:
            self.T[("treated", k)] = np.array([get(i, k)["cost_drivers"].get("treated_all", 0.0) for i in range(n)])
        mt = spec["calibration"]["metric"]
        self.T[("cal",)] = _logit(np.array([get(i, self.comp)["windows"]["cal"][mt] for i in range(n)]))
        self.T[("eir",)] = np.log(np.maximum([get(i, self.comp)["windows"]["cal"].get("eir_annual", 1.0) for i in range(n)], 1e-6))
        self.gps = {}
        self.accuracy = {}

    def _x(self, z, theta, u):
        return [z] + [float(theta[nm]) for nm in self.fitted_names] + [float(u[f]) for f in self.features]

    def _scale(self, X):
        return (X - self.lo) / np.where(self.hi > self.lo, self.hi - self.lo, 1.0)

    @staticmethod
    def _group(res, o, pm):
        sh = res["meta"]["age_shares"]
        gs = pm["age_groups"]
        w = sum(sh[g] for g in gs)
        return sum(res["per_capita"][o][g] * sh[g] for g in gs) / w if w > 0 else 0.0

    def fit(self, log=print):
        tr = ~self.hold
        Xt = self.X[tr]
        t0 = __import__("time").time()
        self.gps[("cal",)] = _gp(Xt, self.T[("cal",)][tr])
        self.gps[("eir",)] = _gp(Xt, self.T[("eir",)][tr], self.gps[("cal",)].kernel_)
        for P in self.pops:
            g0 = _gp(Xt, self.T[("base", P, self.primary)][tr])
            self.gps[("base", P, self.primary)] = g0
            for o in self.outcomes[1:]:
                self.gps[("base", P, o)] = _gp(Xt, self.T[("base", P, o)][tr], g0.kernel_)
            for k in self.packages:
                if k == self.comp:
                    continue
                g = _gp(Xt, self.T[("red", P, self.primary, k)][tr])
                self.gps[("red", P, self.primary, k)] = g
                for o in self.outcomes[1:]:
                    self.gps[("red", P, o, k)] = _gp(Xt, self.T[("red", P, o, k)][tr], g.kernel_)
        for k in self.packages:
            self.gps[("treated", k)] = _gp(Xt, self.T[("treated", k)][tr])
        log(f"emulator: {len(self.gps)} GPs fitted on {tr.sum()} points in {__import__('time').time() - t0:.0f}s")
        self._accuracy()

    def _accuracy(self):
        ho = self.hold
        if ho.sum() < 2:
            self.accuracy = {"holdout_points": int(ho.sum())}
            return
        Xh = self.X[ho]
        acc = {"holdout_points": int(ho.sum())}
        y, yh = self.T[("cal",)][ho], self.gps[("cal",)].predict(Xh)
        acc["cal_logit_rmse"] = float(np.sqrt(np.mean((y - yh) ** 2)))
        # DALYs averted per capita, total over groups weighted by the design's own shares (relative error)
        errs, r2 = [], []
        for k in self.packages:
            if k == self.comp:
                continue
            for P in self.pops:
                key = ("red", P, self.primary, k)
                t, p = self.T[key][ho], self.gps[key].predict(Xh)
                ss = np.sum((t - t.mean()) ** 2)
                r2.append(1 - np.sum((t - p) ** 2) / ss if ss > 0 else float("nan"))
                errs.append(np.mean(np.abs(t - p)))
        acc["reduction_r2_median"] = float(np.nanmedian(r2))
        acc["reduction_r2_p10"] = float(np.nanpercentile(r2, 10))
        acc["reduction_mae_median"] = float(np.median(errs))
        tb, pb = self.T[("base", next(iter(self.pops)), self.primary)][ho], self.gps[("base", next(iter(self.pops)), self.primary)].predict(Xh)
        acc["base_log_rmse"] = float(np.sqrt(np.mean((tb - pb) ** 2)))
        self.accuracy = acc

    # ---------------------------------------------------------------- prediction
    def predict_cal(self, Z, theta, urow):
        X = self._scale(np.asarray([self._x(z, theta, urow) for z in Z], float))
        return self.gps[("cal",)].predict(X)

    def predict_units(self, zs, theta, rows):
        """Batched over units: [{"y": {(P, o, package): per-capita outcome}, "treated": {package: per capita},
        "eir": annual EIR}] in the order of `rows`."""
        X = self._scale(np.asarray([self._x(z, theta, u) for z, u in zip(zs, rows)], float))
        n = len(rows)
        outs = [{"y": {}, "treated": {}} for _ in range(n)]
        for P in self.pops:
            for o in self.outcomes:
                b = np.exp(self.gps[("base", P, o)].predict(X))
                for j in range(n):
                    outs[j]["y"][(P, o, self.comp)] = float(b[j])
                for k in self.packages:
                    if k != self.comp:
                        r = np.clip(self.gps[("red", P, o, k)].predict(X), -0.5, 1.0)
                        for j in range(n):
                            outs[j]["y"][(P, o, k)] = float(b[j] * (1 - r[j]))
        for k in self.packages:
            t = np.maximum(self.gps[("treated", k)].predict(X), 0.0)
            for j in range(n):
                outs[j]["treated"][k] = float(t[j])
        e = np.exp(self.gps[("eir",)].predict(X))
        for j in range(n):
            outs[j]["eir"] = float(e[j])
        return outs


def load_results(path: str) -> dict:
    out = {}
    for line in open(path):
        r = json.loads(line)
        if r.get("ok"):
            out[r["id"]] = r["result"]
    return out


def load(out_dir: str, results_path: str | None = None):
    d = json.load(open(os.path.join(out_dir, "design.json")))
    return d, load_results(results_path or os.path.join(out_dir, "results.jsonl"))
