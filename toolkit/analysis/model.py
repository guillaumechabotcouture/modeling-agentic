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

import os
import sys

import numpy as np

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
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


def _driver(res, name, where):
    """A mapped cost driver: present and finite, or the analysis fails closed (Codex P1-7)."""
    v = (res.get("cost_drivers") or {}).get(name)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise ValueError(f"{where}: cost driver {name!r} is missing or not finite ({v!r}); every cost driver the spec "
                         "maps (analysis.cost_outputs) must be an engine output")
    return float(v)


def _metric(res, window, metric, where):
    v = ((res.get("windows") or {}).get(window) or {}).get(metric)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise ValueError(f"{where}: window metric {window}.{metric} is missing or not finite ({v!r})")
    return float(v)


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
        self.prior_names = list(design.get("prior_names") or [])
        from toolkit.spec import priors as PRI
        want = PRI.drawn(spec)
        miss = sorted(set(want) - set(self.prior_names))
        if miss:
            raise ValueError(f"emulator design: the uncertainty draws vary {miss}, which are not inputs of this design; "
                             "they must enter the design (rebuild the batch from toolkit/analysis/batch.py design(), "
                             "which spans every prior's declared range), or be excluded by name in the signed spec")
        extra = sorted(set(self.prior_names) - set(want))
        if extra:
            raise ValueError(f"emulator design: its prior inputs {extra} are not drawn by this spec; rebuild the batch "
                             "from toolkit/analysis/batch.py design() so the design and the draws agree")
        self.param_names = list(self.fitted_names) + self.prior_names
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
        # Codex P1-6: the design domain. The calibrated attribute and the fitted parameters span their declared
        # design box (calibration.range, engine-card ranges); unit features span what the design points contain.
        self.input_names = ["z"] + self.param_names + list(self.features)
        fr = {q.name: (q.lo, q.hi) for q in spec.engine().parameters() if q.kind == "fitted"}
        fr.update({k: (p.lo, p.hi) for k, p in want.items()})
        zr = spec["calibration"]["range"]
        npar = len(self.param_names)
        self.dom_lo = np.array([min(zr[0], self.lo[0])] + [min(fr[nm][0], self.lo[1 + j]) for j, nm in
                                                          enumerate(self.param_names)] + list(self.lo[1 + npar:]))
        self.dom_hi = np.array([max(zr[1], self.hi[0])] + [max(fr[nm][1], self.hi[1 + j]) for j, nm in
                                                          enumerate(self.param_names)] + list(self.hi[1 + npar:]))
        self.last_events = []
        self.hold = np.array([bool(p["holdout"]) for p in pts])
        self.X = self._scale(X)
        self.T = {}
        get = lambda i, k: results[f"{pts[i]['point']}|{k}"]  # noqa: E731
        n = len(pts)
        self.train_corrections = {}   # Codex P1-6: every floor applied to the training targets, counted
        for P, pm in self.pops.items():
            for o in self.outcomes:
                base = np.array([_group(get(i, self.comp), o, pm) for i in range(n)])
                if np.any(base < 1e-9):
                    self.train_corrections[f"base_floor:{P}:{o}"] = int(np.sum(base < 1e-9))
                self.T[("base", P, o)] = np.log(np.maximum(base, 1e-9))
                for k in self.packages:
                    if k != self.comp:
                        y = np.array([_group(get(i, k), o, pm) for i in range(n)])
                        self.T[("red", P, o, k)] = 1 - y / np.maximum(base, 1e-12)
        for name, m in self.cost_outputs.items():
            for k in self.packages:
                self.T[("cost", name, k)] = np.array([_driver(get(i, k), m["cost_driver"], f"design point {i}, {k}")
                                                      for i in range(n)])
        cal = spec["calibration"]
        cm = np.array([_metric(get(i, self.comp), CAL_WINDOW, cal["metric"], f"design point {i}") for i in range(n)])
        if np.any((cm < 1e-4) | (cm > 1 - 1e-4)):
            self.train_corrections["cal_logit_clip"] = int(np.sum((cm < 1e-4) | (cm > 1 - 1e-4)))
        self.T[("cal",)] = _logit(cm)
        for name, d in self.diagnostics.items():
            w = d.get("window", CAL_WINDOW)
            v = np.array([_metric(get(i, self.comp), w, d["metric"], f"design point {i}") for i in range(n)])
            if np.any(v < 1e-6):
                self.train_corrections[f"diag_floor:{name}"] = int(np.sum(v < 1e-6))
            self.T[("diag", name)] = np.log(np.maximum(v, 1e-6))
        self.gps, self.accuracy = {}, {}
        self.error = None
        self.design_points = n

    def _x(self, z, theta, u):
        return [z] + [float(theta[nm]) for nm in self.param_names] + [float(u[f]) for f in self.features]

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
        cols, res = [], []
        for k in self.packages:
            if k == self.comp:
                continue
            for P in self.pops:
                key = ("red", P, self.primary, k)
                t, p = self.T[key][ho], self.gps[key].predict(Xh)
                cols.append((P, k))
                res.append(t - p)
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
        if cols and ho.sum() >= EmulatorError.MIN_HOLDOUT:
            self.error = EmulatorError(cols, np.array(res).T)
            acc["error_model"] = self.error.summary()
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
        """`noise`: an rng for the emulator-error source: one joint draw of the reduction errors of every unit
        (EmulatorError: a common component plus a per-unit one, from the held-out residuals), applied to every
        outcome of a (population group, package)."""
        rows = [self.by_id[i] for i in ids]
        E = None
        if noise is not None:
            if self.error is None:
                raise ValueError("emulator error: fewer than "
                                 f"{EmulatorError.MIN_HOLDOUT} held-out points, so the error cannot be drawn")
            E = self.error.sample(noise, len(ids))
        Xr = np.asarray([self._x(zs[i], theta, u) for i, u in zip(ids, rows)], float)
        X = self._scale(Xr)
        n = len(ids)
        ev = []                       # Codex P1-6: extrapolation and every clip, per unit (and package)
        tol = 1e-9 * np.maximum(self.dom_hi - self.dom_lo, 1.0)
        outside = (Xr < self.dom_lo - tol) | (Xr > self.dom_hi + tol)
        for j in np.flatnonzero(outside.any(1)):
            ev.append({"unit": ids[j], "package": "*", "kind": "extrapolation",
                       "inputs": [self.input_names[c] for c in np.flatnonzero(outside[j])]})
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
                    if E is not None:
                        r = r + E[:, self.error.col[(P, k)]]
                    for j in np.flatnonzero((r < -0.5) | (r > 1.0)):
                        ev.append({"unit": ids[j], "package": k, "kind": "reduction_clip", "outcome": o, "group": P,
                                   "value": float(r[j])})
                    r = np.clip(r, -0.5, 1.0)
                    for j in range(n):
                        y[j]["y"][k][o] = y[j]["y"][k].get(o, 0.0) + float(b[j] * (1 - r[j])) * popv[j]
        tot = np.array([sum(float(u[pm["column"]]) for pm in self.pops.values()) for u in rows])
        for name in self.cost_outputs:
            for k in self.packages:
                t = self.gps[("cost", name, k)].predict(X)
                for j in np.flatnonzero(t < 0):
                    ev.append({"unit": ids[j], "package": k, "kind": "cost_output_floor", "output": name,
                               "value": float(t[j])})
                t = np.maximum(t, 0.0)
                for j in range(n):
                    y[j]["cost"][k][name] = float(t[j]) * tot[j]
        for name in self.diagnostics:
            e = np.exp(self.gps[("diag", name)].predict(X))
            for j in range(n):
                y[j]["diag"][name] = float(e[j])
        self.last_events = ev
        return dict(zip(ids, y))


class EmulatorError:
    """The emulator-error source, from the held-out residuals of the reductions (one column per population group and
    package, one row per held-out point; held-out points are real units at design values).

    A unit's error is split into the error of the population mean, which a sum over units does not average away, and
    the unit's deviation from it, which it does:
        common  = m + D' g / sqrt(H (H - 1))      g ~ N(0, I_H): one per draw, shared by every unit
        unit j  = D' g_j / sqrt(H - 1)            g_j ~ N(0, I_H): one per unit
    m is the mean held-out residual (signed, per column) and D the residuals minus m: the common part is the held-out
    estimate of the population-mean error, N(m, S/H), so an observed bias keeps its sign and its pattern across
    packages (Codex r1 P1-2), and the draws' centre moves by it; the point estimate does not. Both parts are
    combinations of the observed residual vectors, so they keep the residuals' correlation across packages and
    population groups. Independent per-unit errors at the held-out RMSE (the earlier model) make the error of an
    n-unit total shrink as 1/sqrt(n); here it does not fall below the common part. Not captured: correlation at an
    intermediate level (units of one region more alike than units at large), which H residuals cannot estimate."""
    MIN_HOLDOUT = 3

    def __init__(self, cols: list, R):
        self.cols = list(cols)
        self.col = {c: j for j, c in enumerate(self.cols)}
        self.R = np.asarray(R, float)
        self.H = self.R.shape[0]
        self.m = self.R.mean(0)
        self.D = self.R - self.m

    def sample(self, rng, n: int):
        """(n units x columns) errors for one draw."""
        H = self.H
        common = self.m + self.D.T @ rng.normal(size=H) / math.sqrt(H * (H - 1))
        return common[None, :] + rng.normal(size=(n, H)) @ self.D / math.sqrt(H - 1)

    def summary(self) -> dict:
        H = self.H
        s = self.R.std(0, ddof=1)
        rmse = np.sqrt(np.mean(self.R ** 2, 0))
        sd_c = s / math.sqrt(H)
        tstat = np.abs(self.m) / np.maximum(s / math.sqrt(H), 1e-300)
        live = self.R[:, s > 0]
        corr = None
        if live.shape[1] > 1:
            C = np.corrcoef(live.T)
            corr = float(np.median(C[~np.eye(C.shape[0], dtype=bool)]))
        return {"model": "common (population-mean, centred on the signed mean residual) plus per-unit components from "
                         "the held-out residual vectors",
                "holdout_points": H, "columns": len(self.cols),
                "residual_mean_median": float(np.median(self.m)), "residual_mean_abs_median": float(np.median(np.abs(self.m))),
                "residual_sd_median": float(np.median(s)), "rmse_median": float(np.median(rmse)),
                "sd_common_median": float(np.median(sd_c)), "sd_unit_median": float(np.median(s)),
                "mean_residual_abs_t_median": float(np.median(tstat)),
                "share_columns_mean_residual_beyond_2se": float(np.mean(tstat > 2)),
                "cross_column_residual_corr_median": corr,
                "not_captured": "correlation at an intermediate level (units of one region more alike than units at "
                                "large), which the held-out residuals cannot estimate"}


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
        self.last_events, self.train_corrections = [], {}

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
                row["cost"][k] = {n: _driver({"cost_drivers": r.cost_drivers}, m["cost_driver"], f"unit {i}, {k}") * tot
                                  for n, m in self.cost_outputs.items()}
                if k == self.comp:
                    for n, d in self.diagnostics.items():
                        row["diag"][n] = float((r.windows.get(d.get("window", "")) or {}).get(d["metric"], math.nan))
            out[i] = row
        return out


def _self_test() -> int:
    """python3 toolkit/analysis/model.py --self-test (the emulator-error model; the emulators are tested in run.py)."""
    fails = []

    def ok(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    def sd_of_mean(err, n, draws=3000, seed=1, old=None):
        rng = np.random.default_rng(seed)
        if old is not None:          # the earlier model: independent per unit at the held-out RMSE
            return float(np.std([rng.normal(0, old, size=n).mean() for _ in range(draws)]))
        return float(np.std([err.sample(rng, n)[:, 0].mean() for _ in range(draws)]))

    # ME1 the error of an n-unit mean converges to the population-mean error (sd s/sqrt(H)), not to zero
    R = np.random.default_rng(11).normal(0, 0.04, size=(12, 2))
    e = EmulatorError([("all", "a"), ("all", "b")], R)
    w10, w1000 = sd_of_mean(e, 10), sd_of_mean(e, 1000)
    sH = R[:, 0].std(ddof=1) / math.sqrt(12)
    o10, o1000 = sd_of_mean(None, 10, old=0.04), sd_of_mean(None, 1000, old=0.04)
    ok(w10 / w1000 < 2.0 and abs(w1000 / sH - 1) < 0.1 and o10 / o1000 > 8,
       f"ME1 the error of a total converges to the population-mean error, not to zero as 1/sqrt(n) "
       f"({w10:.4f} at 10 units, {w1000:.4f} at 1000; the independent model shrinks {o10 / o1000:.1f}x)")
    # ME2 zero-mean independent residuals: the per-unit part averages out, the population-mean part (sd s/sqrt(H)) stays
    rng = np.random.default_rng(3)
    R2 = rng.normal(0, 0.05, size=(400, 1))
    e2 = EmulatorError([("all", "a")], R2)
    s, m, H = R2.std(ddof=1), R2.mean(), 400
    want = math.sqrt(s ** 2 / H + s ** 2 / 1000)      # the signed mean m is a shift, not a spread
    got = sd_of_mean(e2, 1000, draws=3000)
    ok(abs(got / want - 1) < 0.1 and got > 1.2 * sd_of_mean(None, 1000, old=float(np.sqrt(np.mean(R2 ** 2)))),
       f"ME2 independent residuals: a total's error has the per-unit part averaged and the mean's error kept "
       f"({got:.5f}, theory {want:.5f})")
    # ME3 the residuals' cross-package correlation is kept
    R3 = np.column_stack([R2[:12, 0], R2[:12, 0]])
    x = EmulatorError([("all", "a"), ("all", "b")], R3).sample(np.random.default_rng(5), 50)
    ok(np.allclose(x[:, 0], x[:, 1]), "ME3 two packages with the same held-out residuals get the same error draw")
    # ME5 Codex r1 P1-2: opposite-signed held-out biases keep their sign (package b is over-predicted relative to a)
    x0 = np.random.default_rng(2).normal(0, 0.002, size=20)
    e5 = EmulatorError([("all", "a"), ("all", "b")], np.column_stack([0.04 + x0, -0.04 + x0]))
    g = np.random.default_rng(6)
    dd = np.array([e5.sample(g, 200).mean(0) for _ in range(400)])
    ok(np.all(dd[:, 0] - dd[:, 1] > 0.07) and np.corrcoef(dd.T)[0, 1] > 0.5,
       f"ME5 signed residual means are kept: the difference stays near 0.08 and the errors stay positively correlated "
       f"({np.min(dd[:, 0] - dd[:, 1]):.4f}, corr {np.corrcoef(dd.T)[0, 1]:.2f})")
    sm = e2.summary()
    ok(sm["holdout_points"] == 400 and sm["sd_common_median"] < sm["sd_unit_median"]
       and "not_captured" in sm, f"ME4 the error model is summarised for the artifact ({sm['sd_common_median']:.4f})")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    print(__doc__)
