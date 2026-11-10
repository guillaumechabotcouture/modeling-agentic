"""Outcome models: one interface over an emulator or the engine itself (design §2).

    model.predict(zs, theta, ids) -> {unit: {"y": {package: {outcome: total}},
                                             "cost": {package: {cost output: total}},
                                             "diag": {name: value},
                                             "burden": {arm: {outcome: total over the model window}}}}

`burden` only when validation.burden declares a model window (F6 S2-5b, toolkit/analysis/burden.py): the arm's
engine window metric for each mapped outcome over that window, times the unit's population.

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
from toolkit.analysis import burden as BV
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


def group_sizes(res, pops: dict, unit: dict) -> dict:
    """{engine group: people} with the weighting the aggregate outcomes use: within each population band of
    decision_units.populations, the engine's age shares renormalised and multiplied by the band's population column,
    summed over bands. So per-group counts always add up to the unit's aggregate outcome."""
    sh = res["meta"]["age_shares"]
    out = {}
    for pm in pops.values():
        gs = pm["age_groups"]
        w = sum(sh[g] for g in gs)
        for g in gs:
            out[g] = out.get(g, 0.0) + (sh[g] / w * float(unit[pm["column"]]) if w > 0 else 0.0)
    return out


class EmulatorModel:
    kind = "emulator"

    def __init__(self, spec, design: dict, results: dict, log=print):
        self.spec, self.log = spec, log
        an = spec["analysis"]
        from toolkit.analysis import features as FT       # F6 S2-4: unit columns or encoded engine attributes
        self.features = FT.declared(spec.raw)
        self.feature_names = [FT.name(f) for f in self.features]
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
        # held spec values a parameter_value test varies (toolkit/spec/engine_parameters.py): design inputs too
        from toolkit.spec import engine_parameters as PV
        self.spec_names = list(design.get("spec_value_names") or [])
        want_s = list(PV.tested_boxes(spec))
        if self.spec_names != want_s:
            raise ValueError(f"emulator design: its spec-value inputs {self.spec_names} are not the held spec values "
                             f"this spec's decision tests vary {want_s}; rebuild the batch from "
                             "toolkit/analysis/batch.py design()")
        self.param_names = list(self.fitted_names) + self.prior_names + self.spec_names
        self.packages = design["packages"]
        self.comp = spec.comparator
        self.pops = spec["decision_units"]["populations"]
        self.outcomes = spec.outcomes()
        self.primary = self.outcomes[0]
        self.by_id = {str(u[spec.uid]): u for u in spec.units()}
        self.fv = {i: FT.vector(spec, u) for i, u in self.by_id.items()}
        pts = design["points"]
        missing = [f"{p['point']}|{k}" for p in pts for k in self.packages if f"{p['point']}|{k}" not in results]
        if missing:
            raise ValueError(f"{len(missing)} engine results missing (e.g. {missing[:3]}): the analysis fails closed")
        self.lo = None
        X = np.asarray([self._x(p["z"], p["theta"], self.by_id[str(p["unit_id"])]) for p in pts], float)
        self.lo, self.hi = X.min(0), X.max(0)
        # Codex P1-6: the design domain. The calibrated attribute and the fitted parameters span their declared
        # design box (calibration.range, engine-card ranges); unit features span what the design points contain.
        self.input_names = ["z"] + self.param_names + self.feature_names
        from toolkit.analysis.batch import fitted_ranges          # S1-2b: through the evidence snapshot
        fr = {k: (v[0], v[1]) for k, v in fitted_ranges(spec.engine()).items()}
        fr.update({k: (p.lo, p.hi) for k, p in want.items()})
        fr.update({k: tuple(v) for k, v in (design.get("spec_value_ranges") or {}).items()})
        fr.update({k: tuple(v) for k, v in (design.get("design_ranges") or {}).items() if k in fr})   # test values
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
        self.bw = BV.model_window(spec)       # F6 S2-5b: the arm's window metrics over the burden model window
        self.bw_keys = [(o, P, m) for o, mp in (self.bw or {}).get("metrics", {}).items() for P, m in mp.items()]
        for o, P, m in self.bw_keys:
            v = np.array([_metric(get(i, self.bw["arm"]), BV.WINDOW, m, f"design point {i}") for i in range(n)])
            if np.any(v < 1e-9):
                self.train_corrections[f"burden_floor:{o}:{P}"] = int(np.sum(v < 1e-9))
            self.T[("bw", o, P)] = np.log(np.maximum(v, 1e-9))
        self.gps, self.accuracy = {}, {}
        self.error, self.error_floors = None, {}
        self.design_points = n

    def _x(self, z, theta, u):
        return [z] + [float(theta[nm]) for nm in self.param_names] + self.fv[str(u[self.spec.uid])]

    def _scale(self, X):
        return (X - self.lo) / np.where(self.hi > self.lo, self.hi - self.lo, 1.0)

    def fit(self):
        tr = ~self.hold
        Xt = self.X[tr]
        t0 = time.time()
        self.gps[("cal",)] = _gp(Xt, self.T[("cal",)][tr])
        for name in self.diagnostics:
            self.gps[("diag", name)] = _gp(Xt, self.T[("diag", name)][tr], self.gps[("cal",)].kernel_)
        for o, P, _m in self.bw_keys:
            self.gps[("bw", o, P)] = _gp(Xt, self.T[("bw", o, P)][tr], self.gps[("cal",)].kernel_)
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
                for o in self.outcomes:          # Codex r2 P1: one residual column per outcome
                    ko = ("red", P, o, k)
                    cols.append((P, o, k))
                    res.append(self.T[ko][ho] - self.gps[ko].predict(Xh))
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
        for o, P, _m in self.bw_keys:     # Codex S2-5b r1 P2: the window values' error, jointly
            cols.append(("bw", o, P))
            res.append(self.T[("bw", o, P)][ho] - self.gps[("bw", o, P)].predict(Xh))
        for P in self.pops:    # F6 S2-4: the comparator burden and every cost output too, from the same points
            for o in self.outcomes:
                cols.append(("emu", "base", P, o))    # four parts: never a reduction's or a window's key
                res.append(self.T[("base", P, o)][ho] - self.gps[("base", P, o)].predict(Xh))
        for name in self.cost_outputs:
            for k in self.packages:
                cols.append(("emu", "cost", name, k))
                res.append(self.T[("cost", name, k)][ho] - self.gps[("cost", name, k)].predict(Xh))
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
        """`noise`: an rng for the emulator-error source: one joint draw of the errors of every unit (EmulatorError:
        a common component plus a per-unit one, from the held-out residuals), one column per emulated quantity the
        decision uses: the reduction per (population group, outcome, package), the comparator burden per (population
        group, outcome), each cost output per package (F6 S2-4), and the burden-window values."""
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
                lb = self.gps[("base", P, o)].predict(X)
                b = np.exp(lb + E[:, self.error.col[("emu", "base", P, o)]] if E is not None else lb)
                for j in range(n):
                    y[j]["y"][self.comp][o] = y[j]["y"][self.comp].get(o, 0.0) + float(b[j]) * popv[j]
                for k in self.packages:
                    if k == self.comp:
                        continue
                    r = self.gps[("red", P, o, k)].predict(X)
                    if E is not None:
                        r = r + E[:, self.error.col[(P, o, k)]]
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
                if E is not None:     # F6 S2-4: a drawn error below zero is floored and counted, not a correction
                    t = t + E[:, self.error.col[("emu", "cost", name, k)]]
                    nf = int(np.sum(t < 0))
                    if nf:
                        fl = self.error_floors.setdefault(name, {})
                        fl[k] = fl.get(k, 0) + nf
                t = np.maximum(t, 0.0)
                for j in range(n):
                    y[j]["cost"][k][name] = float(t[j]) * tot[j]
        for name in self.diagnostics:
            e = np.exp(self.gps[("diag", name)].predict(X))
            for j in range(n):
                y[j]["diag"][name] = float(e[j])
        for o, P, _m in self.bw_keys:     # per population group, times its population (Codex S2-5b r2 P1)
            if P not in self.pops:          # by_population(): a prediction for some groups only (Codex r3 P1)
                continue
            e = self.gps[("bw", o, P)].predict(X)
            e = np.exp(e + E[:, self.error.col[("bw", o, P)]] if E is not None else e)
            col = self.pops[P]["column"]
            for j in range(n):
                b = y[j].setdefault("burden", {}).setdefault(self.bw["arm"], {})
                b[o] = b.get(o, 0.0) + float(e[j]) * float(rows[j][col])
        self.last_events = ev
        return dict(zip(ids, y))


class EmulatorError:
    """The emulator-error source, from the held-out residuals of every emulated quantity the decision uses (one row per
    held-out point; held-out points are real units at design values): one column per reduction (population group,
    outcome, package), comparator burden (log scale, per population group and outcome), cost output (per capita, per
    package; F6 S2-4) and burden-window value. All columns are drawn from the same residual rows, so a unit's comparator,
    reduction and cost errors keep the correlation they show at the held-out points (the assumption: that correlation
    is the one at the units' own inputs).

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
        if len(self.col) != len(self.cols):
            raise ValueError("emulator error: two residual columns share a key (a population group named bw whose "
                             "package and outcome names match a burden-window column?)")
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
        def kind(c):
            return {"base": "comparator", "cost": "cost"}[c[1]] if len(c) == 4 else \
                "burden_window" if c[0] == "bw" else "reduction"
        by_q = {}
        for c in self.cols:
            by_q[kind(c)] = by_q.get(kind(c), 0) + 1
        pair = [np.corrcoef(self.R[:, self.col[c]], self.R[:, self.col[b]])[0, 1] for c in self.cols
                for b in [("emu", "base", c[0], c[1])] if kind(c) == "reduction" and b in self.col
                and s[self.col[c]] > 0 and s[self.col[b]] > 0]
        return {"model": "common (population-mean, centred on the signed mean residual) plus per-unit components from "
                         "the held-out residual vectors",
                "columns_by_quantity": by_q,
                "correlation": "every column is drawn from the same held-out residual rows, so a unit's comparator, "
                               "reduction and cost errors keep their held-out correlation",
                "comparator_reduction_corr_median": float(np.median(pair)) if pair else None,
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
        self.bw = BV.model_window(spec)       # F6 S2-5b: only the burden model window is added (none: unchanged)
        self.horizon = spec.horizon({BV.WINDOW: self.bw["window"]} if self.bw else None)
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

    BATCH = 1024        # members per run_batch call (engines that declare batch_equals_run)

    def _runs(self, theta, ids, zs):
        """{(unit, package): Outcomes}. An engine that declares `batch_equals_run` (its run_batch equals run pair by
        pair, a contract self-test of that engine) evaluates every (unit, package) pair in run_batch calls of up to
        BATCH members; any other engine runs pair by pair, as before."""
        pairs = [(i, k) for i in ids for k in self.packages]
        if not (getattr(self.eng, "batch_equals_run", False) and callable(getattr(self.eng, "run_batch", None))):
            return {(i, k): self.run(theta, i, zs.get(i) if zs else None, k) for i, k in pairs}
        out = {}
        for a in range(0, len(pairs), self.BATCH):
            part = pairs[a:a + self.BATCH]
            units = [self.unit_spec(i, zs.get(i) if zs else None) for i, _ in part]
            pks = [Package(k, self.spec.engine_package(self.pk[k])) for _, k in part]
            self.runs += len(part)
            for p, r in zip(part, self.eng.run_batch([dict(theta)] * len(part), units, pks, self.horizon, self.seed)):
                out[p] = r
        return out

    def predict(self, zs, theta, ids, noise=None):
        out = {}
        runs = self._runs(theta, ids, zs)
        for i in ids:
            u = self.by_id[i]
            row = {"y": {}, "cost": {}, "diag": {}, "y_group": {}}
            tot = sum(float(u[pm["column"]]) for pm in self.pops.values())
            for k in self.packages:
                r = runs[(i, k)]
                res = {"per_capita": r.per_capita, "meta": r.meta}
                row["y"][k] = {o: sum(_group(res, o, pm) * float(u[pm["column"]]) for pm in self.pops.values())
                               for o in self.outcomes}
                # per engine group (deliverables by group), weighted as the aggregate is (group_sizes)
                gsz = group_sizes(res, self.pops, u)
                row["y_group"][k] = {o: {g: float((r.per_capita.get(o) or {}).get(g, 0.0)) * n
                                         for g, n in gsz.items()} for o in self.outcomes}
                row["cost"][k] = {n: _driver({"cost_drivers": r.cost_drivers}, m["cost_driver"], f"unit {i}, {k}") * tot
                                  for n, m in self.cost_outputs.items()}
                if self.bw and k == self.bw["arm"]:
                    row["burden"] = {k: {o: sum(_metric({"windows": r.windows}, BV.WINDOW, mp[P], f"unit {i}, {k}")
                                                 * float(u[pm["column"]]) for P, pm in self.pops.items())
                                         for o, mp in self.bw["metrics"].items()}}
                if k == self.comp:
                    for n, d in self.diagnostics.items():
                        row["diag"][n] = float((r.windows.get(d.get("window", "")) or {}).get(d["metric"], math.nan))
            out[i] = row
        return out


def _self_test_emulator(ok):
    """F6 S2-4 on the toy engine: an encoded attribute is an emulator input, the design and job hashes do not move with
    features, and the drawn error reaches the comparator burden and the cost outputs."""
    import tempfile
    import yaml
    from toolkit.analysis import batch as Bt
    from toolkit.spec import schema
    with tempfile.TemporaryDirectory() as d:
        p = schema.toy_spec(d, n=12)
        lines = open(os.path.join(d, "units.csv")).read().splitlines()
        open(os.path.join(d, "units.csv"), "w").write("\n".join([lines[0] + ",yrs"] + [
            ln + "," + " ".join(str(2015 + k) for k in range(i % 4)) if i % 4 else ln + ",none"
            for i, ln in enumerate(lines[1:])]) + "\n")
        r = yaml.safe_load(open(p))
        r["decision_units"]["engine_attributes"] = {"group": {"column": "yrs", "split": " ", "empty": "none"}}
        r["analysis"]["emulator"]["points"] = 40
        r["analysis"]["cost_outputs"] = {"doses": {"cost_driver": "a_doses"}}
        yaml.safe_dump(r, open(p, "w"), sort_keys=False)
        sp0 = schema.load(p)
        d0, j0 = Bt.design(sp0)
        r["analysis"]["emulator"]["features"] = ["cov", {"attribute": "group", "encoding": "count"},
                                                 {"attribute": "group", "encoding": "last", "empty": 2010}]
        yaml.safe_dump(r, open(p, "w"), sort_keys=False)
        sp = schema.load(p)
        ok(schema.validate(sp) == [], f"EM1 a spec with attribute features validates: {schema.validate(sp)}")
        d1, j1 = Bt.design(sp)
        ok(d1 == d0 and [Bt.job_sha(j) for j in j1] == [Bt.job_sha(j) for j in j0],
           "EM2 features change neither the design nor any batch job hash, so a stored batch is reused")
        res = {j["id"]: Bt.run_job(j)["result"] for j in j1}
        M = EmulatorModel(sp, d1, res, log=lambda *a: None).fit()
        u3 = next(i for i, u in M.by_id.items() if u["yrs"] == "2015 2016 2017")
        ok(M.input_names[-3:] == ["cov", "group.count", "group.last"] and M.fv[u3][1:] == [3.0, 2017.0]
           and M.X.shape[1] == len(M.input_names), f"EM3 the encoded attributes are emulator inputs: {M.input_names}")
        cols = set(M.error.cols)
        ok({("emu", "base", "all", o) for o in M.outcomes} <= cols and {("emu", "cost", "doses", k) for k in M.packages} <= cols,
           "EM4 the error model has comparator-burden and cost-output columns")
        ids = list(M.by_id)[:4]
        zs = {i: 0.2 for i in ids}
        th = {nm: float(np.mean([pt["theta"][nm] for pt in d1["points"]])) for nm in M.param_names}
        y0 = M.predict(zs, th, ids)
        # the toy's dose counts do not depend on the inputs (zero held-out error): residual rows with spread instead
        M.error = EmulatorError(M.error.cols, np.random.default_rng(3).normal(0, 0.1, size=(6, len(M.error.cols))))
        ya, yb = (M.predict(zs, th, ids, noise=np.random.default_rng(s)) for s in (1, 2))
        i0, pk = ids[0], "a"
        ok(ya[i0]["y"][M.comp]["dalys"] != yb[i0]["y"][M.comp]["dalys"] != y0[i0]["y"][M.comp]["dalys"]
           and ya[i0]["cost"][pk]["doses"] != yb[i0]["cost"][pk]["doses"]
           and M.predict(zs, th, ids) == y0,
           "EM5 drawn emulator error moves the comparator burden and the cost outputs; without noise nothing moves")
        R5 = np.zeros((6, len(M.error.cols)))
        R5[:, M.error.col[("emu", "cost", "doses", M.comp)]] = -1.0          # the comparator gives no doses: pushed below 0
        M.error, M.error_floors = EmulatorError(M.error.cols, R5), {}
        y5 = M.predict(zs, th, ids, noise=np.random.default_rng(1))
        ok(all(y5[i]["cost"][M.comp]["doses"] == 0.0 for i in ids) and M.error_floors == {"doses": {M.comp: 4}}
           and not [e for e in M.last_events if e["kind"] == "cost_output_floor"],
           f"EM6 a drawn cost error below zero is floored and counted, not a model correction: {M.error_floors}")
        # Codex r1 P1-1: a population group named like a quantity, with a cost output named like an outcome
        r["decision_units"]["populations"] = {"cost": r["decision_units"]["populations"]["all"]}
        r["options"]["populations"] = {"total": "pop"}
        r["analysis"]["cost_outputs"] = {"dalys": {"cost_driver": "a_doses"}}
        yaml.safe_dump(r, open(p, "w"), sort_keys=False)
        sp7 = schema.load(p)
        try:
            M7 = EmulatorModel(sp7, d1, res, log=lambda *a: None).fit()
            ok(len(set(M7.error.cols)) == len(M7.error.cols), "EM7 quantity keys cannot collide with group names")
        except ValueError as e:
            ok(False, f"EM7 quantity keys cannot collide with group names ({e})")


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
    # ME6 F6 S2-4: a comparator column and a reduction column drawn from the same residual rows keep their correlation
    rng6 = np.random.default_rng(8)
    g6 = rng6.normal(0, 0.05, size=40)
    R6 = np.column_stack([g6, -0.8 * g6 + rng6.normal(0, 0.01, size=40), rng6.normal(0, 0.05, size=40)])
    e6 = EmulatorError([("emu", "base", "all", "a"), ("all", "a", "k"), ("emu", "cost", "c", "k")], R6)
    x6 = e6.sample(np.random.default_rng(9), 4000)
    want6 = np.corrcoef(R6.T)
    got6 = np.corrcoef(x6.T)
    ok(abs(got6[0, 1] - want6[0, 1]) < 0.05 and abs(got6[0, 2] - want6[0, 2]) < 0.1 and got6[0, 1] < -0.9
       and e6.summary()["columns_by_quantity"] == {"comparator": 1, "reduction": 1, "cost": 1}
       and e6.summary()["comparator_reduction_corr_median"] < -0.9,
       f"ME6 comparator, reduction and cost errors keep their held-out correlation ({got6[0, 1]:.3f} vs "
       f"{want6[0, 1]:.3f}; {got6[0, 2]:.3f} vs {want6[0, 2]:.3f})")
    # ME7 adding columns leaves the draws of the existing ones unchanged (the same random numbers per draw)
    a7 = EmulatorError([("all", "a", "k")], R6[:, 1:2]).sample(np.random.default_rng(4), 30)
    b7 = e6.sample(np.random.default_rng(4), 30)
    ok(np.allclose(a7[:, 0], b7[:, 1], rtol=1e-12, atol=1e-15), "ME7 the reduction errors are drawn as before when columns are added")
    try:
        EmulatorError([("a", "b", "c"), ("a", "b", "c")], R6[:, :2])
        ok(False, "ME9 two residual columns with one key are refused")
    except ValueError:
        ok(True, "ME9 two residual columns with one key are refused")
    _self_test_emulator(ok)
    # MD1-MD2: the direct model batches an engine that declares batch_equals_run, with the same outcomes
    import tempfile
    from toolkit.analysis import observed_targets as OT
    with tempfile.TemporaryDirectory() as d:
        sp = OT._toy(d, attr=True)
        m = DirectModel(sp)
        th = {"r0": 12.0}
        zs = {"u1": 0.9, "u2": 1.1}
        a = m.predict(zs, th, ["u1", "u2"])
        n_batched = m.runs
        m.eng.batch_equals_run = False            # instance attribute: the pair-by-pair path
        b = m.predict(zs, th, ["u1", "u2"])
        rel = max(abs(a[i]["y"][k][o] - b[i]["y"][k][o]) / max(abs(b[i]["y"][k][o]), 1e-300)
                  for i in a for k in a[i]["y"] for o in a[i]["y"][k])
        ok(rel < 1e-12 and all(a[i]["cost"] == b[i]["cost"] and a[i]["y_group"] == b[i]["y_group"] for i in a),
           f"MD1 batched direct evaluation equals pair-by-pair runs (worst rel {rel:.1e})")
        yg = a["u1"]["y_group"]["none"]["infections"]
        ok(n_batched == 4 and abs(sum(yg.values()) - a["u1"]["y"]["none"]["infections"]) < 1e-6,
           "MD2 the per-group outcomes sum to the unit total (one row per engine group)")
    # MD3 (Codex r1 P1-8): per-group counts use the aggregate's weighting when population bands differ from the
    # engine's own shares
    res = {"meta": {"age_shares": {"a": 0.5, "b": 0.3, "c": 0.2}}, "per_capita": {"o": {"a": 0.1, "b": 0.2, "c": 0.4}}}
    pops = {"kids": {"column": "pk", "age_groups": ["a"]}, "adults": {"column": "pa", "age_groups": ["b", "c"]}}
    urow = {"pk": 100.0, "pa": 200.0}
    gs = group_sizes(res, pops, urow)
    agg = sum(_group(res, "o", pm) * urow[pm["column"]] for pm in pops.values())
    ok(gs == {"a": 100.0, "b": 120.0, "c": 80.0} and abs(sum(res["per_capita"]["o"][g] * n for g, n in gs.items())
                                                        - agg) < 1e-12,
       f"MD3 group sizes follow the population bands, so group counts add up to the aggregate ({gs})")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    print(__doc__)
