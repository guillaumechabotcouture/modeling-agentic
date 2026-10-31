"""Outcome models: one interface over an emulator or the engine itself (design §2).

    model.predict(zs, theta, ids) -> {unit: {"y": {package: {outcome: total}},
                                             "cost": {package: {cost output: total}},
                                             "diag": {name: value}}}

Totals are over the decision window, scaled by the unit's populations (spec
`decision_units.populations`: each population group is a unit column and a set of engine
age groups). `zs[unit]` is the unit's calibrated attribute on the calibration scale, or
None when the spec calibrates nothing. `theta` overrides engine parameters by name.

EmulatorModel: the spike's Gaussian-process emulator (spike/analysis/emulator.py),
engine-agnostic: the cost outputs and diagnostics it emulates are named in the spec
(`analysis.cost_outputs`, `analysis.diagnostics`), not in this code.
DirectModel: runs the engine for every unit and package (cheap engines).
"""
from __future__ import annotations

import math
import time
import warnings

import numpy as np

from toolkit.analysis.batch import CAL_WINDOW, calibrated_value
from toolkit.engines.contract import Package, UnitSpec


def _logit(p):
    p = np.clip(p, 1e-4, 1 - 1e-4)
    return np.log(p / (1 - p))


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


def _group(res, o, pm):
    sh = res["meta"]["age_shares"]
    gs = pm["age_groups"]
    w = sum(sh[g] for g in gs)
    return sum(res["per_capita"][o][g] * sh[g] for g in gs) / w if w > 0 else 0.0


class EmulatorModel:
    kind = "emulator"

    def __init__(self, spec, design: dict, results: dict, log=print):
        self.spec, self.log = spec, log
        an = spec["analysis"]
        self.features = list((an.get("emulator") or {}).get("features") or [])
        self.cost_outputs = dict(an.get("cost_outputs") or {})
        self.diagnostics = dict(an.get("diagnostics") or {})
        self.fitted_names = design["fitted_names"]
        self.packages = design["packages"]
        self.comp = spec.comparator
        self.pops = spec["decision_units"]["populations"]
        self.outcomes = spec.outcomes()
        self.primary = self.outcomes[0]
        self.by_id = {str(u[spec.uid]): u for u in spec.units()}
        pts = design["points"]
        missing = [f"{p['point']}|{k}" for p in pts for k in self.packages if f"{p['point']}|{k}" not in results]
        if missing:
            raise ValueError(f"{len(missing)} engine results missing (e.g. {missing[:3]}): the analysis fails closed")
        self.lo = None
        X = np.asarray([self._x(p["z"], p["theta"], self.by_id[str(p["unit_id"])]) for p in pts], float)
        self.lo, self.hi = X.min(0), X.max(0)
        self.hold = np.array([bool(p["holdout"]) for p in pts])
        self.X = self._scale(X)
        self.T = {}
        get = lambda i, k: results[f"{pts[i]['point']}|{k}"]  # noqa: E731
        n = len(pts)
        for P, pm in self.pops.items():
            for o in self.outcomes:
                base = np.array([_group(get(i, self.comp), o, pm) for i in range(n)])
                self.T[("base", P, o)] = np.log(np.maximum(base, 1e-9))
                for k in self.packages:
                    if k != self.comp:
                        y = np.array([_group(get(i, k), o, pm) for i in range(n)])
                        self.T[("red", P, o, k)] = 1 - y / np.maximum(base, 1e-12)
        for name, m in self.cost_outputs.items():
            for k in self.packages:
                self.T[("cost", name, k)] = np.array([get(i, k)["cost_drivers"].get(m["cost_driver"], 0.0)
                                                      for i in range(n)])
        cal = spec["calibration"]
        self.T[("cal",)] = _logit(np.array([get(i, self.comp)["windows"][CAL_WINDOW][cal["metric"]] for i in range(n)]))
        for name, d in self.diagnostics.items():
            w = d.get("window", CAL_WINDOW)
            self.T[("diag", name)] = np.log(np.maximum([get(i, self.comp)["windows"][w].get(d["metric"], 1.0)
                                                        for i in range(n)], 1e-6))
        self.gps, self.accuracy = {}, {}
        self.design_points = n

    def _x(self, z, theta, u):
        return [z] + [float(theta[nm]) for nm in self.fitted_names] + [float(u[f]) for f in self.features]

    def _scale(self, X):
        return (X - self.lo) / np.where(self.hi > self.lo, self.hi - self.lo, 1.0)

    def fit(self):
        tr = ~self.hold
        Xt = self.X[tr]
        t0 = time.time()
        self.gps[("cal",)] = _gp(Xt, self.T[("cal",)][tr])
        for name in self.diagnostics:
            self.gps[("diag", name)] = _gp(Xt, self.T[("diag", name)][tr], self.gps[("cal",)].kernel_)
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
        for name in self.cost_outputs:
            for k in self.packages:
                self.gps[("cost", name, k)] = _gp(Xt, self.T[("cost", name, k)][tr])
        self.fit_seconds = time.time() - t0
        self.log(f"emulator: {len(self.gps)} GPs fitted on {tr.sum()} points in {self.fit_seconds:.0f}s")
        self._accuracy()
        return self

    def _accuracy(self):
        ho = self.hold
        if ho.sum() < 2:
            self.accuracy = {"holdout_points": int(ho.sum())}
            return
        Xh = self.X[ho]
        acc = {"holdout_points": int(ho.sum()), "training_points": int((~ho).sum()), "gps": len(self.gps)}
        y, yh = self.T[("cal",)][ho], self.gps[("cal",)].predict(Xh)
        acc["cal_logit_rmse"] = float(np.sqrt(np.mean((y - yh) ** 2)))
        errs, r2, self.red_rmse = [], [], {}
        for k in self.packages:
            if k == self.comp:
                continue
            for P in self.pops:
                key = ("red", P, self.primary, k)
                t, p = self.T[key][ho], self.gps[key].predict(Xh)
                ss = np.sum((t - t.mean()) ** 2)
                r2.append(1 - np.sum((t - p) ** 2) / ss if ss > 0 else float("nan"))
                errs.append(np.mean(np.abs(t - p)))
                self.red_rmse[(P, k)] = float(np.sqrt(np.mean((t - p) ** 2)))
        acc["reduction_r2_median"] = float(np.nanmedian(r2))
        acc["reduction_r2_p10"] = float(np.nanpercentile(r2, 10))
        acc["reduction_mae_median"] = float(np.median(errs))
        P0 = next(iter(self.pops))
        tb, pb = self.T[("base", P0, self.primary)][ho], self.gps[("base", P0, self.primary)].predict(Xh)
        acc["base_log_rmse"] = float(np.sqrt(np.mean((tb - pb) ** 2)))
        self.accuracy = acc

    # ---------------------------------------------------------------- prediction
    def cal_grid(self, ids, theta, zgrid):
        X = []
        for i in ids:
            X += [self._x(z, theta, self.by_id[i]) for z in zgrid]
        return self.gps[("cal",)].predict(self._scale(np.asarray(X, float))).reshape(len(ids), len(zgrid))

    def cal_slope(self, ids, theta, zs, h=0.05):
        """d logit(metric) / dz at each unit's calibrated value (identifiability of the inversion)."""
        Xp = self._scale(np.asarray([self._x(zs[i] + h, theta, self.by_id[i]) for i in ids], float))
        Xm = self._scale(np.asarray([self._x(zs[i] - h, theta, self.by_id[i]) for i in ids], float))
        g = self.gps[("cal",)]
        return (g.predict(Xp) - g.predict(Xm)) / (2 * h)

    def predict(self, zs, theta, ids, noise=None):
        """`noise`: an rng for the emulator-error source (reductions perturbed by their held-out RMSE)."""
        rows = [self.by_id[i] for i in ids]
        X = self._scale(np.asarray([self._x(zs[i], theta, u) for i, u in zip(ids, rows)], float))
        n = len(ids)
        y = [{"y": {k: {} for k in self.packages}, "cost": {k: {} for k in self.packages}, "diag": {}} for _ in ids]
        for P, pm in self.pops.items():
            popv = np.array([float(u[pm["column"]]) for u in rows])
            for o in self.outcomes:
                b = np.exp(self.gps[("base", P, o)].predict(X))
                for j in range(n):
                    y[j]["y"][self.comp][o] = y[j]["y"][self.comp].get(o, 0.0) + float(b[j]) * popv[j]
                for k in self.packages:
                    if k == self.comp:
                        continue
                    r = self.gps[("red", P, o, k)].predict(X)
                    if noise is not None:
                        r = r + noise.normal(0.0, self.red_rmse.get((P, k), 0.0), size=n)
                    r = np.clip(r, -0.5, 1.0)
                    for j in range(n):
                        y[j]["y"][k][o] = y[j]["y"][k].get(o, 0.0) + float(b[j] * (1 - r[j])) * popv[j]
        tot = np.array([sum(float(u[pm["column"]]) for pm in self.pops.values()) for u in rows])
        for name in self.cost_outputs:
            for k in self.packages:
                t = np.maximum(self.gps[("cost", name, k)].predict(X), 0.0)
                for j in range(n):
                    y[j]["cost"][k][name] = float(t[j]) * tot[j]
        for name in self.diagnostics:
            e = np.exp(self.gps[("diag", name)].predict(X))
            for j in range(n):
                y[j]["diag"][name] = float(e[j])
        return dict(zip(ids, y))


class DirectModel:
    """The engine itself, per unit and package (for engines cheap enough to run per draw)."""
    kind = "direct"

    def __init__(self, spec, log=print):
        self.spec, self.log = spec, log
        self.eng = spec.engine()
        an = spec["analysis"]
        self.cost_outputs = dict(an.get("cost_outputs") or {})
        self.diagnostics = dict(an.get("diagnostics") or {})
        self.packages = list(spec.packages())
        self.pk = spec.packages()
        self.comp = spec.comparator
        self.pops = spec["decision_units"]["populations"]
        self.outcomes = spec.outcomes()
        self.by_id = {str(u[spec.uid]): u for u in spec.units()}
        self.attrs = {i: spec.unit_attributes(u) for i, u in self.by_id.items()}
        self.cal_attr = spec["calibration"].get("attribute")
        self.horizon = spec.horizon()
        self.fitted_names = [p.name for p in self.eng.parameters() if p.kind == "fitted"]
        self.accuracy = {"direct": True, "note": "outcomes from the engine itself; no emulator error"}
        self.runs = 0
        self.seed = int((an.get("direct") or {}).get("seed", 1))

    def fit(self):
        return self

    def unit_spec(self, i, z):
        a = dict(self.attrs[i])
        if z is not None and self.cal_attr:
            a[self.cal_attr] = calibrated_value(self.spec, z)
        return UnitSpec(i, a)

    def run(self, theta, i, z, package, horizon=None):
        self.runs += 1
        return self.eng.run(theta, self.unit_spec(i, z), Package(package, self.spec.engine_package(self.pk[package])),
                            horizon or self.horizon, self.seed)

    def predict(self, zs, theta, ids, noise=None):
        out = {}
        for i in ids:
            u = self.by_id[i]
            row = {"y": {}, "cost": {}, "diag": {}}
            tot = sum(float(u[pm["column"]]) for pm in self.pops.values())
            for k in self.packages:
                r = self.run(theta, i, zs.get(i) if zs else None, k)
                res = {"per_capita": r.per_capita, "meta": r.meta}
                row["y"][k] = {o: sum(_group(res, o, pm) * float(u[pm["column"]]) for pm in self.pops.values())
                               for o in self.outcomes}
                row["cost"][k] = {n: float(r.cost_drivers.get(m["cost_driver"], 0.0)) * tot
                                  for n, m in self.cost_outputs.items()}
                if k == self.comp:
                    for n, d in self.diagnostics.items():
                        row["diag"][n] = float((r.windows.get(d.get("window", "")) or {}).get(d["metric"], math.nan))
            out[i] = row
        return out
