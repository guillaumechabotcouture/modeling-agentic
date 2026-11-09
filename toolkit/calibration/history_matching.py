#!/usr/bin/env python3
"""Joint calibration by history matching: the region of parameter values compatible with every target.

    python3 toolkit/calibration/history_matching.py --self-test

The problem (disease-agnostic; the harness adapter toolkit/analysis/joint_calibration.py builds it from a spec):
  shared       parameters common to every unit, each with a bounded prior from a sourced range (a literature value
               is a reference point with its spread, not a fixed value)
  unit_param   optionally one parameter per unit (for example a transmission level), on a declared range
  targets      unit    an observed value of one unit (a unit may have several, e.g. several survey years)
               group   an observed value of a weighted aggregate over units (e.g. a survey at a coarser level)
               shared  an observed value that depends on the shared parameters only (e.g. a fit benchmark)
Every target carries a measurement model: sampling variance from its own interval or sample size, a source bias
variance and a model discrepancy variance. No source is ground truth: a target whose total sd is zero is refused.
A source bias shared by the units of a group (bias_key: one block per source and group) is one latent offset per
block, profiled like a unit parameter, not added to each target's variance (F6 S2-3; see _bias_witness). Profiling
is a feasibility rule, not marginal Gaussian implausibility: a target alone may sit up to cutoff (sd_e + sd_bias)
away (each bound at the cutoff), where integrating the offset out gives cutoff sqrt(sd_e^2 + sd_bias^2); it is
stricter on contrasts within a block. A spec may weight each group once in the best point (best_point_weighting).

Method (Vernon et al. 2010; Andrianakis et al. 2015): waves of simulation; emulators of every observable; the
implausibility I = |E[f] - y| / sqrt(V_emulator + V_observation + V_discrepancy) of every target; the
non-implausible region of the shared parameters, with each unit's non-implausible interval of its own parameter.
The per-unit parameter only moves its own unit, so a shared value is compatible with a unit when some value of the
unit's parameter makes every target of that unit non-implausible (profiling, exact on the grid). The region is the
set of shared values compatible with every shared and group target and with every unit that some value can match.
  unmatched    a unit no value in the prior box matches: reported with its best implausibility and the direction of
               the miss (target above or below what the model reaches), never pinned at a bound silently
  conflicting  units each matchable alone but not together with the others: the region is relaxed to the values with
               the fewest such units, and they are reported
Emulators: unit observables use a regression mean over (unit parameter, shared parameters, unit features) plus a
per-unit Gaussian-process residual with a unit random effect, hyperparameters shared across units (fitted by the
summed marginal likelihood); shared observables use one Gaussian process. Each wave holds out a share of its runs and
checks the standardised errors; an emulator whose stated uncertainty is too small fails closed. After the last wave
the engine is run at the best point for every unit (verification). Thresholds: toolkit/calibration/thresholds.yaml.
Nothing here names a disease, a place or an intervention.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

PRODUCER = "toolkit/calibration/history_matching.py"
THRESHOLDS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "thresholds.yaml")
KINDS = ("unit", "group", "shared")
SCALES = ("logit", "log", "identity")
DISTS = ("uniform", "loguniform", "triangular")
_EPS = 1e-4


class CalibrationInputError(ValueError):
    """The problem cannot be calibrated as declared (fail closed): every reason is listed."""

    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("joint calibration refused:\n  " + "\n  ".join(self.problems[:20]))


def thresholds() -> dict:
    import yaml
    with open(THRESHOLDS) as f:
        return yaml.safe_load(f)


# --------------------------------------------------------------------------- scales and measurement model
def tr(x, scale):
    x = np.asarray(x, float)
    if scale == "logit":
        p = np.clip(x, _EPS, 1 - _EPS)
        return np.log(p / (1 - p))
    if scale == "log":
        return np.log(np.maximum(x, 1e-12))
    return x


def inv(y, scale):
    y = np.asarray(y, float)
    if scale == "logit":
        return 1.0 / (1.0 + np.exp(-y))
    if scale == "log":
        return np.exp(y)
    return y


def dinv(y, scale):
    """d inv(y) / dy."""
    p = inv(y, scale)
    if scale == "logit":
        return p * (1 - p)
    if scale == "log":
        return p
    return np.ones_like(np.asarray(y, float))


def in_domain(x, scale) -> bool:
    """x is a finite value the scale can transform without clipping (Codex r1 P2-10)."""
    if isinstance(x, bool) or not isinstance(x, (int, float, np.floating)) or not math.isfinite(float(x)):
        return False
    x = float(x)
    return (0 < x < 1) if scale == "logit" else (x > 0) if scale == "log" else True


def at_edge(x, scale) -> bool:
    """A simulated proportion of exactly 0 or 1 (logit), or exactly 0 (log): kept at the clip and counted."""
    if isinstance(x, bool) or not isinstance(x, (int, float, np.floating)):
        return False
    return (scale == "logit" and float(x) in (0.0, 1.0)) or (scale == "log" and float(x) == 0.0)


def sim_ok(x, scale) -> bool:
    """One rule for fresh and earlier simulated values (Codex r3 P2-9)."""
    return in_domain(x, scale) or at_edge(x, scale)


def sd_from_ci(lo, hi, scale, level=0.95) -> float:
    """The sampling sd on the target's scale from a symmetric-on-that-scale interval at `level`."""
    from scipy.stats import norm
    if not in_domain(lo, scale) or not in_domain(hi, scale):
        raise ValueError(f"interval [{lo}, {hi}] is outside the {scale} scale's domain")
    zq = float(norm.ppf(0.5 + level / 2))
    a, b = float(tr(lo, scale)), float(tr(hi, scale))
    if not (math.isfinite(a) and math.isfinite(b) and b > a):
        raise ValueError(f"interval [{lo}, {hi}] is not increasing and finite on the {scale} scale")
    return (b - a) / (2 * zq)


def sd_from_n(p, n, scale) -> float:
    """The sampling sd of a proportion p from n (effective) observations, on the target's scale (delta method)."""
    p, n = float(p), float(n)
    if not (0 < p < 1 and n > 0):
        raise ValueError(f"proportion {p} with n {n}: need 0 < p < 1 and n > 0")
    v = p * (1 - p) / n
    if scale == "logit":
        return math.sqrt(v) / (p * (1 - p))
    if scale == "log":
        return math.sqrt(v) / p
    return math.sqrt(v)


class Param:
    """A shared parameter: a bounded prior over a sourced range."""

    def __init__(self, name, lo, hi, dist="uniform", mode=None, source=""):
        self.name, self.lo, self.hi, self.dist = str(name), float(lo), float(hi), str(dist)
        self.mode = None if mode is None else float(mode)
        self.source = str(source or "")

    def problems(self) -> list:
        bad = []
        if not (math.isfinite(self.lo) and math.isfinite(self.hi) and self.lo < self.hi):
            bad.append(f"shared {self.name}: range [{self.lo}, {self.hi}] needs lo < hi")
        if self.dist not in DISTS:
            bad.append(f"shared {self.name}: dist {self.dist!r} not in {DISTS}")
        if self.dist == "loguniform" and self.lo <= 0:
            bad.append(f"shared {self.name}: a loguniform range must be positive")
        if self.dist == "triangular" and not (self.mode is not None and self.lo <= self.mode <= self.hi):
            bad.append(f"shared {self.name}: a triangular prior needs its mode inside the range")
        if not self.source.strip():
            bad.append(f"shared {self.name}: no source for its range")
        return bad

    def natural(self, u):
        return self.lo + np.asarray(u, float) * (self.hi - self.lo)

    def pdf(self, x):
        x = np.asarray(x, float)
        if self.dist == "uniform":
            return np.full_like(x, 1.0 / (self.hi - self.lo))
        if self.dist == "loguniform":
            return 1.0 / (np.maximum(x, 1e-300) * math.log(self.hi / self.lo))
        a, b, c = self.lo, self.hi, self.mode
        up = np.where(x < c, 2 * (x - a) / ((b - a) * max(c - a, 1e-300)), 2 * (b - x) / ((b - a) * max(b - c, 1e-300)))
        return np.maximum(up, 0.0)

    def quantiles(self, qs) -> list:
        """Quantiles of the prior (numerical inverse of its cdf on a fine grid)."""
        g = np.linspace(self.lo, self.hi, 4001)
        c = np.concatenate([[0.0], np.cumsum(0.5 * (self.pdf(g[1:]) + self.pdf(g[:-1])) * np.diff(g))])
        return [float(np.interp(q * c[-1], c, g)) for q in qs]

    def describe(self) -> dict:
        return {"range": [self.lo, self.hi], "dist": self.dist, "mode": self.mode, "source": self.source}


class Target:
    """One observed value with its measurement model. `value` is on the natural scale; sd_obs, sd_bias and sd_md are
    on the target's `scale`. `bias_key` (unit targets): the block whose one offset (sd sd_bias) every target of the
    block shares, for example one survey in one group of units (F6 S2-3); without it sd_bias is the target's own."""

    def __init__(self, id, kind, observable, value, scale="identity", sd_obs=0.0, sd_bias=0.0, sd_md=0.0,
                 unit=None, weights=None, source="", depends_on=None, bias_key=None):
        self.id, self.kind, self.observable = str(id), str(kind), str(observable)
        self.value, self.scale = float(value), str(scale)
        self.sd_obs, self.sd_bias, self.sd_md = float(sd_obs), float(sd_bias), float(sd_md)
        self.unit = None if unit is None else str(unit)
        self.weights = None if weights is None else {str(k): float(v) for k, v in weights.items()}
        self.source = str(source or "")
        # a shared target's declared dependence: the shared parameters its setting uses (None: all of them). The
        # simulation still sets every shared value; only the emulator's inputs are restricted.
        self.depends_on = None if depends_on is None else [str(x) for x in depends_on]
        self.bias_key = None if bias_key is None else str(bias_key)

    @property
    def y(self) -> float:
        return float(tr(self.value, self.scale))

    @property
    def var(self) -> float:
        return self.sd_obs ** 2 + self.sd_bias ** 2 + self.sd_md ** 2

    @property
    def var_fit(self) -> float:
        """The variance the target is judged with: a shared offset (bias_key) is profiled, not added."""
        return self.var - (self.sd_bias ** 2 if self.bias_key is not None else 0.0)

    def describe(self) -> dict:
        d = {"id": self.id, "kind": self.kind, "observable": self.observable, "value": self.value,
             "scale": self.scale, "sd_obs": self.sd_obs, "sd_bias": self.sd_bias, "sd_md": self.sd_md,
             "sd_total": math.sqrt(self.var), "source": self.source}
        if self.unit is not None:
            d["unit"] = self.unit
        if self.weights is not None:
            d["n_units"] = len(self.weights)
        if self.bias_key is not None:
            d["bias_key"] = self.bias_key
        return d


class Problem:
    """shared: [Param]; unit_param: {name, lo, hi} or None; units: [ids]; features: {unit: [floats]} (each unit the
    same length; may be empty); targets: [Target]; scales: {observable: scale}. groups: a label per unit (figures and
    the best point's weights)."""

    def __init__(self, shared, unit_param, units, targets, features=None, feature_names=None, groups=None):
        self.shared = list(shared)
        self.unit_param = dict(unit_param) if unit_param else None
        self.units = [str(u) for u in units]
        self.targets = list(targets)
        self.features = {str(k): [float(x) for x in v] for k, v in (features or {}).items()}
        self.feature_names = list(feature_names or [])
        self.groups = {str(k): str(v) for k, v in (groups or {}).items()}      # a label per unit
        self.scales = {}
        for t in self.targets:
            self.scales.setdefault(t.observable, t.scale)

    @property
    def names(self) -> list:
        return [p.name for p in self.shared]

    def unit_targets(self, u) -> list:
        return [t for t in self.targets if t.kind == "unit" and t.unit == u]

    def observables(self, kind) -> list:
        """Observables simulated per unit ('unit': unit and group targets) or per shared point ('shared')."""
        want = ("unit", "group") if kind == "unit" else ("shared",)
        return sorted({t.observable for t in self.targets if t.kind in want})

    def bias_components(self) -> list:
        """[(blocks, units)]: bias blocks linked through units with targets in several of them, profiled jointly."""
        parent = {}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x
        keys = {u: sorted({t.bias_key for t in self.unit_targets(u) if t.bias_key is not None}) for u in self.units}
        for ks in keys.values():
            for k in ks:
                parent.setdefault(k, k)
            for k in ks[1:]:
                parent[find(k)] = find(ks[0])
        comps = {}
        for u, ks in keys.items():
            if ks:
                c = comps.setdefault(find(ks[0]), (set(), []))
                c[0].update(ks)
                c[1].append(u)
        return [(sorted(b), us) for b, us in comps.values()]

    def _bias_problems(self, mx) -> list:
        bad, sds = [], {}
        for t in self.targets:
            if t.bias_key is None:
                continue
            if t.kind != "unit":
                bad.append(f"target {t.id}: bias_key is for unit targets (one offset shared by a group's units)")
            elif not (math.isfinite(t.sd_bias) and t.sd_bias > 0):
                bad.append(f"target {t.id}: bias block {t.bias_key} needs sd_bias > 0")
            else:
                sds.setdefault(t.bias_key, set()).add(round(t.sd_bias, 12))
        bad += [f"bias block {k}: one sd_bias for every target of the block ({sorted(v)})" for k, v in sds.items()
                if len(v) > 1]
        for blocks, us in self.bias_components():
            if len(blocks) > mx:
                bad.append(f"bias blocks {blocks[:4]}: {len(blocks)} blocks linked through units {us[:3]} would be "
                           f"profiled jointly; at most bias_blocks_max {mx}")
        return bad

    def problems(self) -> list:
        bad = []
        th = thresholds()["history_matching"]
        bad += self._bias_problems(int(th["bias_blocks_max"]))
        for p in self.shared:
            bad += p.problems()
        if len(set(self.names)) != len(self.names):
            bad.append("shared: repeated names")
        if self.units and not self.unit_param:
            bad.append("units are listed but no unit parameter is declared")
        if self.unit_param:
            lo, hi = self.unit_param.get("lo"), self.unit_param.get("hi")
            if not (isinstance(lo, (int, float)) and isinstance(hi, (int, float)) and math.isfinite(lo)
                    and math.isfinite(hi) and lo < hi):
                bad.append(f"unit parameter {self.unit_param.get('name')}: range [{lo}, {hi}] needs lo < hi")
            if not self.units:
                bad.append("a unit parameter is declared but there are no units")
        if len(set(self.units)) != len(self.units):
            bad.append("units: repeated ids")
        uset = set(self.units)
        k = {len(v) for v in self.features.values()}
        if self.features and (set(self.features) != uset or len(k) != 1):
            bad.append("features: one vector of the same length per unit")
        if self.features and any(not all(math.isfinite(x) for x in v) for v in self.features.values()):
            bad.append("features: every value must be finite")
        if not self.targets:
            bad.append("no targets")
        ids = [t.id for t in self.targets]
        if len(set(ids)) != len(ids):
            bad.append("targets: repeated ids")
        for t in self.targets:
            w = f"target {t.id}"
            if t.kind not in KINDS:
                bad.append(f"{w}: kind {t.kind!r} not in {KINDS}")
                continue
            if t.scale not in SCALES:
                bad.append(f"{w}: scale {t.scale!r} not in {SCALES}")
            if self.scales.get(t.observable) != t.scale:
                bad.append(f"{w}: observable {t.observable} is used on two scales")
            if not math.isfinite(t.value) or (t.scale == "logit" and not 0 < t.value < 1) or (
                    t.scale == "log" and not t.value > 0):
                bad.append(f"{w}: value {t.value!r} is not a finite value on the {t.scale} scale")
            for nm in ("sd_obs", "sd_bias", "sd_md"):
                v = getattr(t, nm)
                if not (math.isfinite(v) and v >= 0):
                    bad.append(f"{w}: {nm} {v!r} must be finite and >= 0")
            if not math.sqrt(t.var) >= float(th["min_target_sd"]):
                bad.append(f"{w}: no declared error (sampling, source bias or discrepancy sd); a target with none is "
                           "treated as ground truth, which no source is")
            if t.kind == "unit":
                if t.unit not in uset:
                    bad.append(f"{w}: unit {t.unit!r} is not a calibrated unit")
            elif t.kind == "group":
                ws = t.weights or {}
                if not ws or set(ws) - uset or any(not (math.isfinite(v) and v >= 0) for v in ws.values()) or \
                        not sum(ws.values()) > 0:
                    bad.append(f"{w}: group weights must name calibrated units with weights >= 0 and a positive sum")
            elif t.kind == "shared" and t.unit is not None:
                bad.append(f"{w}: a shared target names no unit")
            if t.depends_on is not None and (t.kind != "shared" or not t.depends_on
                                             or set(t.depends_on) - set(self.names)):
                bad.append(f"{w}: depends_on is for a shared target and must list shared parameters")
        deps = {}
        for t in self.targets:
            if t.kind == "shared":
                deps.setdefault(t.observable, set()).add(tuple(t.depends_on) if t.depends_on else None)
        bad += [f"observable {ob}: its targets declare different dependences" for ob, d in deps.items() if len(d) > 1]
        for u in self.units:
            if not self.unit_targets(u):
                bad.append(f"unit {u}: no unit target; its parameter would be set by nothing (refused)")
        if not self.shared and not self.units:
            bad.append("nothing to calibrate: no shared parameters and no units")
        if not self.shared and any(t.kind == "shared" for t in self.targets):
            bad.append("shared targets need shared parameters")
        return bad

    def z_natural(self, s):
        return self.unit_param["lo"] + np.asarray(s, float) * (self.unit_param["hi"] - self.unit_param["lo"])

    def theta_natural(self, U) -> list:
        U = np.atleast_2d(np.asarray(U, float))
        return [{p.name: float(p.natural(row[j])) for j, p in enumerate(self.shared)} for row in U]

    def prior_weight(self, U) -> np.ndarray:
        U = np.atleast_2d(np.asarray(U, float))
        w = np.ones(len(U))
        for j, p in enumerate(self.shared):
            w = w * p.pdf(p.natural(U[:, j]))
        return w


# --------------------------------------------------------------------------- emulators
def _matern52(A, B, ls):
    d = np.sqrt(np.maximum(((A[:, None, :] - B[None, :, :]) / ls) ** 2, 0).sum(-1))
    s5 = math.sqrt(5.0) * d
    return (1 + s5 + 5.0 / 3.0 * d ** 2) * np.exp(-s5)


class UnitEmulator:
    """One observable simulated per unit: regression mean over (unit parameter, shared parameters, unit features)
    plus a per-unit Gaussian-process residual over (unit parameter, shared parameters) with a unit random effect.
    Hyperparameters (random-effect variance, GP variance, length scales, run noise) are shared across units and fitted
    by the summed marginal likelihood. Inputs are on the unit cube; outputs on the observable's scale."""

    def __init__(self, n_shared: int, feats: dict, ridge: float):
        self.p, self.feats, self.ridge = int(n_shared), feats, float(ridge)
        self.fm, self.fs = None, None

    def _f(self, u):
        v = np.asarray(self.feats.get(u, []), float)
        return (v - self.fm) / self.fs if len(v) else v

    def _basis(self, s, T, F):
        s = np.asarray(s, float)[:, None]
        cols = [np.ones_like(s), s, s ** 2]
        if self.p:
            cols += [T, T ** 2, s * T]
        if F.shape[1]:
            cols += [F, F * s]
        return np.hstack(cols)

    def fit(self, units, S, T, y):
        from scipy.optimize import minimize
        units = [str(u) for u in units]
        S, T, y = np.asarray(S, float), np.asarray(T, float).reshape(len(units), self.p), np.asarray(y, float)
        if self.feats:
            M = np.array(list(self.feats.values()), float)
            self.fm, self.fs = M.mean(0), np.where(M.std(0) > 0, M.std(0), 1.0)
        F = np.array([self._f(u) for u in units], float).reshape(len(units), -1)
        B = self._basis(S, T, F)
        self.beta = np.linalg.solve(B.T @ B + self.ridge * len(y) * np.eye(B.shape[1]), B.T @ y)
        r = y - B @ self.beta
        self.vr = float(max(np.var(r), 1e-10))
        X = np.column_stack([S, T])
        by = {}
        for i, u in enumerate(units):
            by.setdefault(u, []).append(i)
        self.X = {u: X[ix] for u, ix in by.items()}
        self.r = {u: r[ix] for u, ix in by.items()}
        groups = {}
        for u, ix in by.items():
            groups.setdefault(len(ix), []).append(u)
        D = X.shape[1]
        vr = self.vr

        def unpack(h):
            return (math.exp(h[0]) * vr, math.exp(h[1]) * vr, np.exp(h[2:2 + D]), math.exp(h[2 + D]) * vr)

        def nll(h):
            sb, sg, ls, tn = unpack(h)
            tot = 0.0
            for n, us in groups.items():
                Xs = np.stack([self.X[u] for u in us])
                Rs = np.stack([self.r[u] for u in us])
                K = sb + sg * np.stack([_matern52(x, x, ls) for x in Xs]) + (tn + 1e-10 * vr) * np.eye(n)[None]
                try:
                    L = np.linalg.cholesky(K)
                except np.linalg.LinAlgError:
                    return 1e30
                a = np.linalg.solve(L, Rs[..., None])[..., 0]
                tot += 0.5 * float((a ** 2).sum()) + float(np.log(np.diagonal(L, axis1=1, axis2=2)).sum()) \
                    + 0.5 * n * len(us) * math.log(2 * math.pi)
            return tot
        h0 = np.r_[math.log(0.3), math.log(0.5), np.log(np.full(D, 0.5)), math.log(0.05)]
        bnds = [(math.log(1e-6), math.log(10.0)), (math.log(1e-6), math.log(10.0))] + \
            [(math.log(0.05), math.log(20.0))] * D + [(math.log(1e-8), math.log(2.0))]
        best = None
        for start in (h0, np.r_[math.log(0.05), math.log(1.0), np.log(np.full(D, 1.5)), math.log(0.01)]):
            res = minimize(nll, start, method="L-BFGS-B", bounds=bnds, options={"maxiter": 200})
            if best is None or res.fun < best.fun:
                best = res
        self.sb, self.sg, self.ls, self.tn = unpack(best.x)
        self.hyper = {"random_effect_var": self.sb, "gp_var": self.sg, "length_scales": self.ls.tolist(),
                      "run_noise_var": self.tn, "residual_var": vr, "nll": float(best.fun)}
        self.L, self.alpha = {}, {}
        for u, x in self.X.items():
            K = self.sb + self.sg * _matern52(x, x, self.ls) + (self.tn + 1e-10 * vr) * np.eye(len(x))
            Lc = np.linalg.cholesky(K)
            self.L[u] = Lc
            self.alpha[u] = np.linalg.solve(Lc.T, np.linalg.solve(Lc, self.r[u]))
        return self

    def predict(self, u, S, T, noise=True):
        """(mean, var) on the observable's scale for unit u at rows (S, T); var includes the run noise if `noise`."""
        S = np.asarray(S, float)
        T = np.asarray(T, float).reshape(len(S), self.p)
        f = self._f(u)
        F = np.broadcast_to(f, (len(S), len(f)))
        m = self._basis(S, T, F) @ self.beta
        v = np.full(len(S), self.sb + self.sg)
        if u in self.X:
            Q = np.column_stack([S, T])
            k = self.sb + self.sg * _matern52(Q, self.X[u], self.ls)
            m = m + k @ self.alpha[u]
            w = np.linalg.solve(self.L[u], k.T)
            v = np.maximum(v - (w ** 2).sum(0), 1e-12)
        if noise:
            v = v + self.tn
        return m, v


class SharedEmulator:
    """One observable that depends on the shared parameters only: a Gaussian process on the unit cube, over the
    columns `cols` (the target's declared dependence; all shared parameters by default)."""

    def __init__(self, cols=None):
        self.cols = None if cols is None else list(cols)

    def _c(self, T):
        T = np.atleast_2d(np.asarray(T, float))
        return T if self.cols is None else T[:, self.cols]

    def fit(self, T, y):
        import warnings
        from sklearn.gaussian_process import GaussianProcessRegressor
        from sklearn.gaussian_process.kernels import ConstantKernel, Matern, WhiteKernel
        T = self._c(T)
        k = ConstantKernel(1.0, (1e-3, 1e3)) * Matern(np.ones(T.shape[1]), (0.05, 50), nu=2.5) + \
            WhiteKernel(1e-3, (1e-8, 1e0))
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            self.gp = GaussianProcessRegressor(k, normalize_y=True, n_restarts_optimizer=2, random_state=0)
            self.gp.fit(T, np.asarray(y, float))
        self.tn = float(self.gp.kernel_.k2.noise_level * self.gp._y_train_std ** 2)
        return self

    def predict(self, T, noise=True):
        m, s = self.gp.predict(self._c(T), return_std=True)
        v = np.maximum(s ** 2, 1e-12)
        if not noise:
            v = np.maximum(v - self.tn, 1e-12)
        return m, v


def standardised_errors(sim, mean, var) -> np.ndarray:
    return (np.asarray(sim, float) - np.asarray(mean, float)) / np.sqrt(np.asarray(var, float))


def validation(errors, th=None) -> dict:
    """The held-out check: more than outside_share_max of |standardised error| beyond std_error_max fails; fewer than
    min_holdout errors cannot pass (the check is not run)."""
    th = th or thresholds()["emulator"]
    e = np.asarray(errors, float)
    n = int(len(e))
    if n < int(th["min_holdout"]) or not np.all(np.isfinite(e)):
        return {"n": n, "status": "not_run" if np.all(np.isfinite(e)) else "failed", "share_outside": None,
                "max_abs": None if not n else float(np.nanmax(np.abs(e)))}
    share = float(np.mean(np.abs(e) > float(th["std_error_max"])))
    return {"n": n, "status": "passed" if share <= float(th["outside_share_max"]) else "failed",
            "share_outside": share, "max_abs": float(np.max(np.abs(e))), "mean": float(np.mean(e)),
            "sd": float(np.std(e))}


# --------------------------------------------------------------------------- the calibration
class Data:
    """Simulated points: unit rows (unit, s, t, obs) and shared rows (t, obs); s and t on the unit cube."""

    def __init__(self):
        self.unit, self.shared = [], []

    def add(self, kind, rows):
        (self.unit if kind == "unit" else self.shared).extend(rows)


def _key(r) -> tuple:
    """A run's inputs (unit, unit parameter, shared values), rounded: repeated inputs share a key."""
    return (r.get("unit"), round(float(r.get("s", 0.0)), 12), tuple(np.round(np.asarray(r["t"], float), 12)))


def _folds(rows, k, rng) -> list:
    """k cross-validation folds of row indices; rows with the same inputs (_key) always share a fold."""
    groups = {}
    for i, r in enumerate(rows):
        groups.setdefault(_key(r), []).append(i)
    keys = list(groups)
    order = rng.permutation(len(keys))
    folds = [[] for _ in range(k)]
    for n, j in enumerate(order):
        folds[n % k].extend(groups[keys[j]])
    return [f for f in folds if f]


def _sobol(d, n, seed):
    from scipy.stats import qmc
    if d == 0:
        return np.zeros((n, 0))
    m = int(math.ceil(math.log2(max(n, 2))))
    return qmc.Sobol(d=d, scramble=True, seed=int(seed)).random_base2(m)[:n]


def _seed(*parts) -> int:
    h = hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()
    return int(h[:8], 16)


class HistoryMatch:
    """Waves of simulation, emulation and implausibility. `simulate(points)` takes a list of
    {"kind": "unit", "unit", "z", "theta", "seed"} or {"kind": "shared", "theta", "seed"} (z and theta on their
    natural scales) and returns one {observable: value} per point (natural scale); any missing or non-finite value
    fails closed."""

    def __init__(self, problem: Problem, simulate, seed: int = 0, log=print, prior=None, record=(), top_up=None,
                 **override):
        """`prior`: earlier runs ({"unit": [{unit, z, theta, seed, obs}], "shared": [{theta, seed, obs}]}, natural
        scales, as a report's "runs") used as data before the first new wave; each must lie inside the declared
        ranges and carry every observable (fail closed). `record`: observables kept with every run and the
        verification but never emulated or judged (for example a diagnostic the report states beside the fit)."""
        bad = problem.problems()
        if bad:
            raise CalibrationInputError(bad)
        self.P, self.simulate, self.seed, self.log = problem, simulate, int(seed), log
        th = thresholds()
        self.th = {**th["history_matching"], **{k: v for k, v in override.items() if k in th["history_matching"]}}
        self.eth = {**th["emulator"], **{k: v for k, v in override.items() if k in th["emulator"]}}
        self.vth = th["verification"]
        self.bth = th["blocking"]
        self.cut = float(self.th["implausibility_cutoff"])
        if self.th.get("best_point_weighting", "per_unit") not in ("per_group", "per_unit"):
            raise CalibrationInputError([f"best_point_weighting {self.th['best_point_weighting']!r}: per_group or "
                                         "per_unit"])
        self.G = int(self.th["z_grid"])
        self.zg = np.linspace(0.0, 1.0, self.G) if problem.unit_param else np.zeros(1)
        self.p = len(problem.shared)
        self.data = Data()
        self.waves = []
        self.boundary = {}
        self.record = [str(x) for x in record or ()]
        self.prior_n = 0
        # top_up: a new wave simulates only the units with fewer runs than this, up to it (for example the units
        # whose inputs changed, so their earlier runs no longer match); None: every unit gets points_per_unit
        if top_up is not None and not (isinstance(top_up, int) and not isinstance(top_up, bool) and top_up >= 1):
            raise CalibrationInputError([f"top_up {top_up!r}: a positive integer"])
        self.top_up = top_up
        self.uobs = problem.observables("unit")
        self.sobs = problem.observables("shared")
        # F6 S2-3: shared offsets, profiled on a grid in sd units over +-cutoff (see _bias_witness)
        self.bcomp = problem.bias_components()
        # Codex r1 P2-2: a group target whose units all lie in one component is checked while its offset is profiled
        self.bgt = [[t for t in problem.targets if t.kind == "group" and set(t.weights) <= set(us)]
                    for _, us in self.bcomp]
        self.bsd = {t.bias_key: t.sd_bias for t in problem.targets if t.kind == "unit" and t.bias_key is not None}
        nb = int(self.th["bias_grid"])
        self.bgrid = np.linspace(-self.cut, self.cut, nb)
        self.bstep = 2 * self.cut / max(nb - 1, 1)
        self.tid = {t.id: t for t in problem.targets}
        mp = int(self.eth["min_points_per_unit"])
        if problem.units and int(self.th["points_per_unit"]) < mp:     # Codex r1 P2-15
            raise CalibrationInputError([f"points_per_unit {self.th['points_per_unit']} is below the emulator's "
                                         f"min_points_per_unit {mp} (toolkit/calibration/thresholds.yaml)"])
        if prior:
            self._load_prior(prior)

    def _load_prior(self, prior):
        P, bad = self.P, []
        lo, hi = (P.unit_param["lo"], P.unit_param["hi"]) if P.unit_param else (0.0, 1.0)
        uset = set(P.units)

        def cube_t(th, w):
            try:
                t = np.array([(float(th[p.name]) - p.lo) / (p.hi - p.lo) for p in P.shared], float)
            except (KeyError, TypeError, ValueError):
                bad.append(f"{w}: shared values {sorted(th or {})} do not name every shared parameter")
                return None
            if not (np.all(np.isfinite(t)) and np.all(t >= -1e-12) and np.all(t <= 1 + 1e-12)):
                bad.append(f"{w}: a shared value lies outside its declared range")
                return None
            return np.clip(t, 0, 1)
        for kind, need in (("unit", self.uobs), ("shared", self.sobs)):
            for n, r in enumerate(prior.get(kind) or []):
                w = f"earlier {kind} run {n}"
                t = cube_t(r.get("theta") or {}, w)
                obs = r.get("obs") or {}
                miss = [ob for ob in need if not sim_ok(obs.get(ob), P.scales[ob])]
                if miss:
                    bad.append(f"{w}: observables {miss} missing or outside their domain")
                if t is None or miss:
                    continue
                keep = {ob: float(obs[ob]) for ob in need}
                for ob in need:              # Codex r4 P3: earlier values at a scale's edge are counted too
                    if at_edge(obs[ob], P.scales[ob]):
                        self.boundary[ob] = self.boundary.get(ob, 0) + 1
                keep.update({ob: float(obs[ob]) for ob in self.record if isinstance(obs.get(ob), (int, float))
                             and not isinstance(obs.get(ob), bool)})          # Codex r3 P2-8: records kept
                row = {"t": t, "seed": r.get("seed"), "obs": keep, "prior": True}
                if kind == "unit":
                    z = r.get("z")
                    if r.get("unit") not in uset or not isinstance(z, (int, float)) or not lo - 1e-9 <= z <= hi + 1e-9:
                        bad.append(f"{w}: unit {r.get('unit')!r} not calibrated or its value {z!r} outside "
                                   f"[{lo}, {hi}]")
                        continue
                    row.update(unit=r["unit"], s=float(np.clip((z - lo) / (hi - lo), 0, 1)))
                if isinstance(r.get("failed_checks"), dict):
                    row["failed_checks"] = r["failed_checks"]
                self.data.add(kind, [row])
        uc = prior.get("unresolved_checks") or {}
        if not (isinstance(uc, dict) and all(isinstance(v, list) and all(isinstance(n, int) and n > 0 for n in v)
                                             for v in uc.values())):
            bad.append("unresolved_checks: {observable: [size of each unresolved failed check]}")
        else:
            self._carried = uc
        if bad:
            raise CalibrationInputError(bad)
        self.prior_n = len(self.data.unit) + len(self.data.shared)

    # ---------------------------------------------------------------- simulation
    def _run(self, kind, rows):
        pts = []
        for r in rows:
            th = self.P.theta_natural(r["t"])[0] if self.p else {}
            pt = {"kind": kind, "theta": th, "seed": r["seed"]}
            if kind == "unit":
                pt.update(unit=r["unit"], z=float(self.P.z_natural(r["s"])))
            pts.append(pt)
        if not pts:
            return []
        out = self.simulate(pts)
        need = self.uobs if kind == "unit" else self.sobs
        if not isinstance(out, list) or len(out) != len(pts):
            raise CalibrationInputError([f"simulate returned {type(out).__name__} of length "
                                         f"{len(out) if hasattr(out, '__len__') else '?'} for {len(pts)} points"])
        bad = []
        for r, o in zip(rows, out):
            for ob in need:
                v = (o or {}).get(ob)
                sc = self.P.scales[ob]
                if at_edge(v, sc):      # a finite engine can simulate exactly 0 or 1: transformed at the clip, counted
                    self.boundary[ob] = self.boundary.get(ob, 0) + 1
                elif not in_domain(v, sc):
                    bad.append(f"simulate: point {r.get('unit', 'shared')} seed {r['seed']}: observable {ob} is "
                               f"missing, not finite or outside the {sc} scale's domain ({v!r})")
            r["obs"] = {ob: float(o[ob]) for ob in list(need) + self.record if ob in (o or {})
                        and isinstance(o[ob], (int, float)) and not isinstance(o[ob], bool)}
            if isinstance((o or {}).get("_ref"), str):
                r["ref"] = o["_ref"]            # where the simulator stored this run (for later reuse)
        if bad:
            raise CalibrationInputError(bad)
        return rows

    def _design(self, wave, state, label=None, units=True):
        n_u = int(self.th["points_per_unit"])
        n_s = int(self.th["shared_points"]) if self.sobs else 0
        urows, srows = [], []
        lab = wave if label is None else label          # the seeds' wave label
        rng = np.random.default_rng([self.seed, lab, 1])
        if wave == 0 or state is None:
            for k, u in enumerate(self.P.units if units else []):
                X = _sobol(1 + self.p, n_u, _seed(self.seed, "unit", u))
                for i, x in enumerate(X):
                    urows.append({"unit": u, "s": float(x[0]), "t": x[1:].copy(),
                                  "seed": _seed(self.seed, lab, u, i)})
            for i, x in enumerate(_sobol(self.p, n_s, _seed(self.seed, "shared", lab))):
                srows.append({"t": x.copy(), "seed": _seed(self.seed, lab, "shared", i)})
            return urows, srows
        C, acc = state["C"], state["region"]
        w = self.P.prior_weight(C[acc]) if len(acc) else None
        have = {}
        for r in self.data.unit:
            have[r["unit"]] = have.get(r["unit"], 0) + 1
        for u in self.P.units:
            if self.top_up is not None:     # Codex r5 P2-6: the minimum applies to the unit's total, not the increment
                target = max(self.top_up, int(self.eth["min_points_per_unit"]))
                n_u = max(target - have.get(u, 0), 0)
                if n_u == 0:
                    continue
            ok = state["units"][u]["ok"]
            rows_ok = [c for c in acc if ok[c].any()] if len(acc) else []
            if rows_ok:
                ww = self.P.prior_weight(C[rows_ok])
                # Codex r1 P1-3: without replacement where the region allows, so a wave repeats no input
                pick = rng.choice(rows_ok, size=n_u, p=ww / ww.sum(), replace=len(rows_ok) < n_u)
                for i, c in enumerate(pick):
                    zs = self.zg[rng.choice(np.flatnonzero(ok[c]))]
                    zs = float(np.clip(zs + rng.uniform(-0.5, 0.5) / max(self.G - 1, 1), 0, 1))
                    urows.append({"unit": u, "s": zs, "t": C[c].copy(), "seed": _seed(self.seed, lab, u, i)})
            else:      # no value in the region matches it: simulate at its own best values, to confirm or refute
                m = state["units"][u]["m"]
                top = np.argsort(m)[:max(3 * n_u, 1)]
                pick = rng.choice(top, size=n_u, replace=len(top) < n_u)
                for i, c in enumerate(pick):
                    urows.append({"unit": u, "s": float(self.zg[state["units"][u]["g"][c]]), "t": C[c].copy(),
                                  "seed": _seed(self.seed, lab, u, i)})
        if n_s:
            src = acc if len(acc) else np.arange(len(C))
            ww = self.P.prior_weight(C[src])
            for i, c in enumerate(rng.choice(src, size=n_s, p=ww / ww.sum(), replace=len(src) < n_s)):
                srows.append({"t": C[c].copy(), "seed": _seed(self.seed, lab, "shared", i)})
        return urows, srows

    # ---------------------------------------------------------------- emulation
    def _fit(self, urows, srows):
        em = {"unit": {}, "shared": {}}
        if urows:
            units = [r["unit"] for r in urows]
            S = np.array([r["s"] for r in urows])
            T = np.array([r["t"] for r in urows]).reshape(len(urows), self.p)
            for ob in self.uobs:
                y = tr([r["obs"][ob] for r in urows], self.P.scales[ob])
                em["unit"][ob] = UnitEmulator(self.p, self.P.features, float(self.eth["ridge"])).fit(units, S, T, y)
        if srows and self.sobs:
            T = np.array([r["t"] for r in srows]).reshape(len(srows), self.p)
            for ob in self.sobs:
                dep = next((t.depends_on for t in self.P.targets if t.observable == ob), None)
                cols = None if dep is None else [self.P.names.index(n) for n in dep]
                em["shared"][ob] = SharedEmulator(cols).fit(T, tr([r["obs"][ob] for r in srows], self.P.scales[ob]))
        return em

    def _errors(self, kind, held, rest):
        """Standardised errors of `held` rows under emulators fitted on `rest` (one list per observable); the errors
        by run key are kept in self._check_errs for the cohort rule (_note_check)."""
        em = self._fit(rest, []) if kind == "unit" else self._fit([], rest)
        out = {}
        for ob in (self.uobs if kind == "unit" else self.sobs):
            e = []
            for r in held:
                if kind == "unit":
                    m, v = em["unit"][ob].predict(r["unit"], [r["s"]], np.atleast_2d(r["t"]))
                else:
                    m, v = em["shared"][ob].predict(np.atleast_2d(r["t"]))
                x = float(standardised_errors(tr(r["obs"][ob], self.P.scales[ob]), m, v)[0])
                e.append(x)
                self._check_errs.setdefault(ob, {})[self._run_key(kind, r)] = x
            out[ob] = e
        return out

    def _validate(self, wave_u, wave_s):
        """Hold out a share of this wave's runs, fit on the rest, check the held-out standardised errors. When that
        share is fewer than min_holdout runs, 5-fold cross-validation over every run so far (so a small design is
        still checked; never skipped)."""
        rng = np.random.default_rng([self.seed, len(self.waves), 2])
        hs, mh = float(self.eth["holdout_share"]), int(self.eth["min_holdout"])
        out = {}
        if not hasattr(self, "_check_errs"):
            self._reset_checks()
        self._check_errs = {}
        for kind, wave, alld in (("unit", wave_u, self.data.unit), ("shared", wave_s, self.data.shared)):
            if not wave:
                continue
            k = int(round(hs * len(wave)))
            errs = {}
            covers_all = False
            if k >= mh:
                ids = set(rng.choice(len(wave), size=k, replace=False).tolist())
                held = [wave[i] for i in sorted(ids)]
                hk = {_key(r) for r in held}       # Codex r1 P1-3: no copy of a held-out input stays in training
                errs = self._errors(kind, held, [r for r in alld if _key(r) not in hk])
                how = f"held out {k} of this wave's {len(wave)} runs (repeated inputs excluded from the fit)"
            else:
                for f in _folds(alld, 5, rng):
                    fk = {_key(alld[i]) for i in f}
                    e = self._errors(kind, [alld[i] for i in f], [r for r in alld if _key(r) not in fk])
                    for ob, v in e.items():
                        errs.setdefault(ob, []).extend(v)
                covers_all = True
                how = (f"5-fold cross-validation over {len(alld)} runs, repeated inputs in one fold (this wave's "
                       f"holdout would be {k} < {mh})")
            for ob, e in errs.items():
                out[ob] = dict(validation(e, self.eth), how=how, covers_all=bool(covers_all))
        return out

    # ---------------------------------------------------------------- implausibility
    def _evaluate(self, em, C):
        """Per unit: implausibility over (candidate, z grid); per shared and group target: over candidates."""
        P, G, N = self.P, len(self.zg), len(C)
        units = {}
        gobs = sorted({t.observable for t in P.targets if t.kind == "group"})
        need_g = {u for t in P.targets if t.kind == "group" for u in t.weights}
        S = np.tile(self.zg, N)
        T = np.repeat(C, G, axis=0)
        bias, alone, bias_F = self._bias_witness(em, C, S, T) if self.bcomp else ({}, {}, [])
        for u in P.units:
            I = np.zeros(N * G)
            sign = np.zeros(N * G)
            tg = P.unit_targets(u)
            for t in tg:
                m, v = em["unit"][t.observable].predict(u, S, T, noise=bool(self.th["include_run_noise"]))
                it = self._impl(t, m, v, np.repeat(bias[t.bias_key], G) if t.bias_key is not None else None)
                big = it > I
                sign = np.where(big, np.sign(m - t.y), sign)
                I = np.maximum(I, it)
            I, sign = I.reshape(N, G), sign.reshape(N, G)
            g = I.argmin(1)
            ok_ = I <= self.cut
            rec = {"I": I, "ok": ok_, "m": I[np.arange(N), g], "g": g, "sign": sign[np.arange(N), g]}
            if u in alone:
                rec["alone"] = alone[u]
            if u in need_g:
                rec.update(self._group_profile(em, u, S, T, ok_, g, C, gobs))
            units[u] = rec
        shared = {}
        for t in P.targets:
            if t.kind == "shared":
                m, v = em["shared"][t.observable].predict(C, noise=bool(self.th["include_run_noise"]))
                shared[t.id] = {"I": np.abs(m - t.y) / np.sqrt(v + t.var), "mean": m}
        if gobs:
            shared.update(self._group_witness(units, N))
        return {"C": C, "units": units, "targets": shared, "bias": bias, "bias_F": bias_F}

    # ---------------------------------------------------------------- shared offsets (F6 S2-3)
    # A survey's error is partly shared by the units of a group (the same design, fieldwork and model of the small-area
    # surface): y_ut = f_ut + b_k + e_ut, with b_k ~ N(0, sd_bias^2) one offset per block k (source x group) and e_ut
    # independent (sampling, discrepancy). Adding sd_bias^2 to every target's own variance instead lets each unit use
    # its own offset, so a group's units pull on one survey anchor as if independent, and within-group contrasts that
    # identify shared parameters are lost. Here b_k is a nuisance parameter profiled like the unit parameter: a
    # candidate is compatible with a block's units when one offset with |b| / sd_bias <= cutoff leaves every unit of
    # the block matchable by its own parameter (Vernon et al. 2010 treat structured discrepancy the same way: a term
    # with a prior inside the implausibility). The witness offset is the most probable such value (smallest |b| / sd);
    # when none exists, the value matching the most units, so the rest are reported as conflicting.
    # Codex r1: (1) a coarse pass over offset cells with half a cell of slack (an outer bound: it never excludes what a
    # continuous offset keeps); (2) the bias_cells most probable feasible cells per candidate are refined on
    # bias_refine points per axis with no slack, and the witness is one common offset at which every unit (and every
    # group target whose units all lie in the component) holds exactly (P2-2, P2-3); (3) the region sample draws the
    # offset among the feasible cells, then the units given it (P1).
    def _impl(self, t, m, v, b=None):
        """|m - (y - b)| / sd: a target judged at its block's offset b (exact; no grid slack)."""
        y = t.y if b is None or t.bias_key is None else t.y - b
        return np.abs(m - y) / np.sqrt(v + t.var_fit)

    def _cells(self, k, fine=False):
        """(points, k) offsets in sd units: the grid's product, row-major (as a (nb,) * k array reshaped)."""
        g = self.bgrid if not fine else np.linspace(-self.cut, self.cut, (len(self.bgrid) - 1) * int(
            self.th["bias_refine"]) + 1)
        return np.array(np.meshgrid(*[g] * k, indexing="ij")).reshape(k, -1).T

    def _comp_preds(self, em, ci, S, T, M):
        """The component's unit-target predictions [(target, mean, sd)] per unit, and its group targets' member
        values and sds on the natural scale, each (M, G)."""
        _, us = self.bcomp[ci]
        noise = bool(self.th["include_run_noise"])
        pl, gp = {}, {}
        for u in us:
            pl[u] = []
            for t in self.P.unit_targets(u):
                m, v = em["unit"][t.observable].predict(u, S, T, noise=noise)
                pl[u].append((t, m.reshape(M, -1), np.sqrt(v.reshape(M, -1) + t.var_fit)))
        for gt in self.bgt[ci]:
            for u in gt.weights:
                m, v = em["unit"][gt.observable].predict(u, S, T, noise=False)
                m, v = m.reshape(M, -1), v.reshape(M, -1)
                gp[(gt.id, u)] = (inv(m, gt.scale).astype(np.float32),
                                  (np.abs(dinv(m, gt.scale)) * np.sqrt(v)).astype(np.float32))
        return pl, gp

    def _check(self, blocks, pl, gp, gts, B, slack, M):
        """Every unit of a component, and its group targets, at offsets B (M or 1, S, k) on the natural scale:
        (all hold (M, S), {unit: matchable (M, S)}, {unit: z mask (M, G)} when S == 1). A group target holds when the
        range its members reach (each over its matching values) comes within the cutoff of it, with the members'
        emulator sds added as perfectly correlated (as _group_terms)."""
        S, G, cut = B.shape[1], len(self.zg), self.cut
        allok, uoks, masks = np.ones((M, S), bool), {}, {}
        agg = {gt.id: [0.0, 0.0, 0.0] for gt in gts}
        for u, lst in pl.items():
            okz = np.ones((M, G, S), bool)
            for t, m, sd in lst:
                if t.bias_key is None:
                    okz &= (np.abs(m - t.y) <= cut * sd)[..., None]
                    continue
                h = 0.5 * self.bstep * t.sd_bias if slack else 0.0
                okz &= np.abs(m[..., None] - t.y + B[:, None, :, blocks.index(t.bias_key)]) - h <= cut * sd[..., None]
            uoks[u] = okz.any(1)
            allok &= uoks[u]
            for gt in gts:
                if u in gt.weights:
                    nat, sdn = gp[(gt.id, u)]
                    w, a = gt.weights[u] / sum(gt.weights.values()), agg[gt.id]
                    a[0] = a[0] + w * np.where(okz, nat[..., None], np.inf).min(1)
                    a[1] = a[1] + w * np.where(okz, nat[..., None], -np.inf).max(1)
                    a[2] = a[2] + w * np.where(okz, sdn[..., None], 0.0).max(1)
            if S == 1:
                masks[u] = okz[..., 0]
        for gt in gts:
            lo, hi, sdg = agg[gt.id]
            fin = np.isfinite(lo) & np.isfinite(hi)
            near = np.clip(gt.y, tr(np.where(fin, lo, 0.5), gt.scale), tr(np.where(fin, hi, 0.5), gt.scale))
            d = 1.0 / np.maximum(np.abs(dinv(near, gt.scale)), 1e-12)
            allok &= fin & (np.abs(gt.y - near) <= cut * np.sqrt(gt.var + (d * sdg) ** 2))
        return allok, uoks, masks

    def _bias_witness(self, em, C, S, T):
        """({block: certified offset per candidate}, {unit: matchable at some offset}, [coarse feasibility (N, cells)
        per component])."""
        N, G, nb, cut = len(C), len(self.zg), len(self.bgrid), self.cut
        L, R = int(self.th["bias_cells"]), int(self.th["bias_refine"])
        offs, alone, Fs = {}, {}, []
        for ci, (blocks, us) in enumerate(self.bcomp):
            k, gts = len(blocks), self.bgt[ci]
            cells, sds = self._cells(k), np.array([self.bsd[b] for b in blocks])
            pl, gp = self._comp_preds(em, ci, S, T, N)
            if gts:
                F, uoks, _ = self._check(blocks, pl, gp, gts, (cells * sds)[None], True, N)
            else:          # fast path: some z matches every target, an OR over the z grid per pair of cells
                F, uoks = np.ones((N, len(cells)), bool), {}
                for u in us:
                    ind, O = np.ones((N, G), bool), [np.ones((N, G, 1), bool) for _ in blocks]
                    for t, m, sd in pl[u]:
                        if t.bias_key is None:
                            ind &= np.abs(m - t.y) <= cut * sd
                            continue
                        j = blocks.index(t.bias_key)
                        O[j] = O[j] & (np.abs(m[..., None] - t.y + self.bgrid * t.sd_bias) - 0.5 * self.bstep *
                                       t.sd_bias <= cut * sd[..., None])
                    O[0] = O[0] & ind[..., None]
                    A = O[0].any(1) if k == 1 else \
                        np.matmul(O[0].transpose(0, 2, 1).astype(np.float32), O[1].astype(np.float32)) > 0.5
                    uoks[u] = np.broadcast_to(A, (N,) + (nb,) * k).reshape(N, -1)
                    F &= uoks[u]
            cnt = sum(uoks.values())
            alone.update({u: a.any(1) for u, a in uoks.items()})
            D = (cells ** 2).sum(1)
            top = np.argsort(-(F * 1e6 + cnt * 1e3 - D[None]), axis=1, kind="stable")[:, :L]
            sub = np.array(np.meshgrid(*[np.linspace(-0.5, 0.5, R) * self.bstep] * k, indexing="ij")).reshape(k, -1).T
            Bs = np.clip((cells[top][:, :, None, :] + sub[None, None]).reshape(N, -1, k), -cut, cut)
            ok, uo, _ = self._check(blocks, pl, gp, gts, Bs * sds, False, N)
            pick = np.argmax(ok * 1e6 + sum(uo.values()) * 1e3 - (Bs ** 2).sum(-1), axis=1)
            for j, bk in enumerate(blocks):
                offs[bk] = Bs[np.arange(N), pick, j] * sds[j]
            Fs.append(F)
        return offs, alone, Fs

    def _offset_draws(self, pick, rng) -> dict:
        """Codex r1 P1: {unit: grid index per draw} for the units of components without group targets. Per draw and
        component: a cell among the candidate's feasible cells (weights: the offset prior's density), a uniform point
        in it, kept when every unit of the component matches there exactly (bias_tries proposals; else the
        witness); then each unit's value uniform over its matching grid values at that offset."""
        st, n, G = self.state, len(pick), len(self.zg)
        S, T = np.tile(self.zg, n), np.repeat(st["C"][pick], G, axis=0)
        tries, out = int(self.th["bias_tries"]), {}
        for ci, (blocks, us) in enumerate(self.bcomp):
            if self.bgt[ci]:
                continue
            k, cells = len(blocks), self._cells(len(blocks))
            sds = np.array([self.bsd[b] for b in blocks])
            w = st["bias_F"][ci][pick] * np.exp(-0.5 * (cells ** 2).sum(1))[None]
            ci_ = np.array([rng.choice(len(cells), tries, p=x / x.sum()) if x.sum() > 0 else np.zeros(tries, int)
                            for x in w])
            Bs = np.clip(cells[ci_] + rng.uniform(-0.5, 0.5, (n, tries, k)) * self.bstep, -self.cut, self.cut)
            wit = np.stack([st["bias"][b][pick] / self.bsd[b] for b in blocks], -1)
            Bs = np.concatenate([Bs, wit[:, None]], 1)
            pl, gp = self._comp_preds(self.em, ci, S, T, n)
            ok, _, _ = self._check(blocks, pl, gp, [], Bs * sds, False, n)
            first = np.where(ok.any(1), ok.argmax(1), tries)
            _, _, masks = self._check(blocks, pl, gp, [], (Bs[np.arange(n), first] * sds)[:, None], False, n)
            for u in us:
                out[u] = (masks[u], [int(rng.choice(np.flatnonzero(masks[u][i]))) if masks[u][i].any() else
                                     int(st["units"][u]["g"][pick[i]]) for i in range(n)])
        return out

    # ---------------------------------------------------------------- group targets (Codex r1 P1-1)
    # A group target constrains its units' parameters jointly. For each candidate, each member unit's non-implausible
    # values of its own parameter (from its unit targets) give a range of the group observable; the unit's value is
    # indexed by a share t in [0, 1] of that range (the grid value closest to lo + t (hi - lo); its best value when it
    # has none). One t per group target is chosen per candidate (the least implausible of `group_t` shares), each unit
    # taking the t of the first group target that contains it, and every group target is then judged at that joint
    # witness. The region sample draws a t per group among the non-implausible ones and keeps the draw only when
    # every group target holds at the joint values.
    def _group_profile(self, em, u, S, T, ok_, g, C, gobs):
        K = int(self.th["group_t"])
        N, G = ok_.shape
        tk = np.linspace(0.0, 1.0, K)
        sel = np.empty((K, N), np.int32)
        gc = {}
        for ob in gobs:
            sc = self.P.scales[ob]
            m, v = em["unit"][ob].predict(u, S, T, noise=False)
            m, v = m.reshape(N, G), v.reshape(N, G)
            nat = inv(m, sc)
            big = np.where(ok_, nat, np.inf)
            lo = np.where(ok_.any(1), big.min(1), nat[np.arange(N), g])
            hi = np.where(ok_.any(1), np.where(ok_, nat, -np.inf).max(1), nat[np.arange(N), g])
            if ob == gobs[0]:
                for k, t in enumerate(tk):
                    want = lo + t * (hi - lo)
                    d = np.where(ok_, np.abs(nat - want[:, None]), np.inf)
                    sel[k] = np.where(ok_.any(1), d.argmin(1), g)
            r = np.arange(N)
            tn = em["unit"][ob].tn if self.th["include_run_noise"] else 0.0
            dv = np.stack([np.abs(dinv(m[r, sel[k]], sc)) for k in range(K)])
            gc[ob] = (np.stack([nat[r, sel[k]] for k in range(K)]).astype(np.float32),
                      (dv * np.sqrt(np.stack([v[r, sel[k]] for k in range(K)]))).astype(np.float32),
                      (dv ** 2 * tn).astype(np.float32))      # Codex r2 P2-6: the member's run noise, natural scale
        return {"sel": sel, "gc": gc}

    def _group_terms(self, t, units, kmap, rows):
        """(I, transformed mean) of group target t at candidates `rows`, each member unit at share index
        kmap[unit] (an array over rows)."""
        sc = t.scale
        wsum = sum(t.weights.values())
        pbar, sdn, vn = np.zeros(len(rows)), np.zeros(len(rows)), np.zeros(len(rows))
        for u, w in t.weights.items():
            nat, sd, nz = units[u]["gc"][t.observable]
            k = kmap[u]
            pbar += w / wsum * nat[k, rows]
            sdn += w / wsum * sd[k, rows]          # emulator errors perfectly correlated: an upper bound on the sd
            vn += (w / wsum) ** 2 * nz[k, rows]    # run noise: independent runs
        yb = tr(pbar, sc)
        d = 1.0 / np.maximum(np.abs(dinv(yb, sc)), 1e-12)
        return np.abs(yb - t.y) / np.sqrt((d * sdn) ** 2 + d ** 2 * vn + t.var), yb

    def _owner(self):
        """{unit: component id}: group targets that share a unit form one component (Codex r2 P1-1), whose members
        move by one common share; a component is named by its first group target."""
        gts = [t for t in self.P.targets if t.kind == "group"]
        parent = {t.id: t.id for t in gts}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x
        first = {}
        for t in gts:
            for u in t.weights:
                if u in first:
                    parent[find(t.id)] = find(first[u])
                else:
                    first[u] = t.id
        return {u: find(g) for u, g in first.items()}

    def _components(self):
        own = self._owner()
        comp = {}
        for t in self.P.targets:
            if t.kind == "group":
                comp.setdefault(own[next(iter(t.weights))], []).append(t)
        return comp

    def _group_witness(self, units, N):
        K = int(self.th["group_t"])
        rows = np.arange(N)
        out = {}
        for cid, gts in self._components().items():
            members = {u for t in gts for u in t.weights}
            # the component's implausibility at each common share: its worst group target
            Ik = np.stack([np.max([self._group_terms(t, units, {u: np.full(N, k) for u in members}, rows)[0]
                                   for t in gts], axis=0) for k in range(K)])
            kstar = Ik.argmin(0)
            kmap = {u: kstar for u in members}
            for t in gts:
                I, yb = self._group_terms(t, units, kmap, rows)
                out[t.id] = {"I": I, "mean": yb, "kstar": kstar, "Ik": Ik}
        return out

    def _merge(self, a, b):
        if a is None:
            return b
        out = {"C": np.vstack([a["C"], b["C"]]), "units": {}, "targets": {},
               "bias": {k: np.concatenate([v, b["bias"][k]]) for k, v in a["bias"].items()},
               "bias_F": [np.concatenate([x, y]) for x, y in zip(a["bias_F"], b["bias_F"])]}
        for u in a["units"]:
            x, y = a["units"][u], b["units"][u]
            r = {k: np.concatenate([x[k], y[k]]) for k in ("I", "ok", "m", "g", "sign", "alone") if k in x}
            if "sel" in x:
                r["sel"] = np.concatenate([x["sel"], y["sel"]], axis=1)
                r["gc"] = {ob: tuple(np.concatenate([x["gc"][ob][i], y["gc"][ob][i]], axis=1) for i in (0, 1, 2))
                           for ob in x["gc"]}
            out["units"][u] = r
        for k in a["targets"]:
            d = {q: np.concatenate([a["targets"][k][q], b["targets"][k][q]]) for q in ("I", "mean")}
            if "kstar" in a["targets"][k]:
                d["kstar"] = np.concatenate([a["targets"][k]["kstar"], b["targets"][k]["kstar"]])
                d["Ik"] = np.concatenate([a["targets"][k]["Ik"], b["targets"][k]["Ik"]], axis=1)
            out["targets"][k] = d
        return out

    def _classify(self, st, n_sobol):
        """The region, matchable / unmatched / conflicting units (see the module docstring)."""
        C = st["C"]
        N = len(C)
        okT = np.ones(N, bool)
        for k, v in st["targets"].items():
            okT &= v["I"] <= self.cut
        um = {u: r["m"] <= self.cut for u, r in st["units"].items()}
        matchable = [u for u in self.P.units if (um[u] & okT).any()]
        # Codex r1 P2-6: a unit its own targets can match somewhere, but never where the shared and group targets
        # hold, is not unreachable: it conflicts with those targets (reported apart, blocks the same way)
        # a unit in a bias block is unmatchable only when no offset in its prior matches it (alone); matchable at some
        # offset but never at its block's witness, it conflicts with the other units of its block
        unmatchable = [u for u in self.P.units if not st["units"][u].get("alone", um[u]).any()]
        shared_conflict = [u for u in self.P.units if u not in matchable and u not in unmatchable]
        if matchable:
            nbad = np.sum([~um[u] for u in matchable], axis=0)
        else:
            nbad = np.zeros(N, int)
        nbad = np.where(okT, nbad, len(self.P.units) + 1)
        region = np.flatnonzero(okT & (nbad == 0))
        relaxed, conflicting = 0, []
        if not len(region) and okT.any():
            relaxed = int(nbad.min())
            region = np.flatnonzero(nbad == relaxed)
            conflicting = sorted({u for u in matchable if not um[u][region].any()}, key=str)
            if not conflicting:      # different candidates miss different units: every unit missed somewhere
                conflicting = sorted({u for u in matchable if (~um[u][region]).any()}, key=str)
        # Codex r5 P1-2: units no value matches do not steer the choice of the best shared point. F6 S2-3: with
        # best_point_weighting per_group (a spec's choice), each group of units counts once (its units' mean): a chosen
        # balancing rule so a large group does not outweigh small ones by repetition, not a likelihood. Each block's
        # offset enters once, as its own prior term (b / sd)^2.
        keep = [u for u in self.P.units if u not in unmatchable]
        wts = self._unit_weights(keep)
        score = sum(wts[u] * st["units"][u]["m"] ** 2 for u in keep) if keep else np.zeros(N)
        score = score + sum((v / self.bsd[k]) ** 2 for k, v in st["bias"].items()) if st.get("bias") else score
        score = score + sum(v["I"] ** 2 for v in st["targets"].values()) if st["targets"] else score
        acc = okT & (nbad == 0)
        share = None
        if n_sobol:      # Codex r1 P2-5: prior mass, not box volume (the Sobol candidates are uniform on the box)
            w = self.P.prior_weight(C[:n_sobol]) if self.p else np.ones(n_sobol)
            share = float(np.sum(w * acc[:n_sobol]) / np.sum(w)) if np.sum(w) > 0 else None
        return {"region": region, "relaxed": relaxed, "matchable": matchable, "unmatchable": unmatchable,
                "shared_conflict": shared_conflict, "conflicting": conflicting, "okT": okT, "score": score,
                "share_of_prior": share, "n_sobol": n_sobol}

    def _unit_weights(self, units) -> dict:
        """{unit: weight in the best point's score}: 1 / (units of its group) with best_point_weighting per_group and
        group labels; 1 per unit otherwise (a unit without a label is its own group)."""
        if self.th.get("best_point_weighting", "per_unit") != "per_group" or not self.P.groups:
            return {u: 1.0 for u in units}
        n = {}
        for u in units:
            g = self.P.groups.get(u) or f"\0{u}"
            n[g] = n.get(g, 0) + 1
        return {u: 1.0 / n[self.P.groups.get(u) or f"\0{u}"] for u in units}

    def _region(self, em):
        n0 = 2 ** int(self.th["candidates_log2"]) if self.p else 1
        C = _sobol(self.p, n0, _seed(self.seed, "candidates", len(self.waves)))
        st = self._evaluate(em, C)
        cl = self._classify(st, n0)
        rng = np.random.default_rng([self.seed, len(self.waves), 3])
        for _ in range(int(self.th["refine_rounds"]) if self.p else 0):
            if len(cl["region"]) >= int(self.th["region_min_candidates"]):
                break
            base = cl["region"] if len(cl["region"]) else np.argsort(cl["score"])[:16]
            src = C[rng.choice(base, size=n0 // 2)] if len(base) else C
            X = np.clip(src + rng.normal(0, float(self.th["refine_sd"]), src.shape), 0, 1)
            st = self._merge(st, self._evaluate(em, X))
            C = st["C"]
            cl = self._classify(st, n0)
        st.update(cl)
        return st

    # ---------------------------------------------------------------- the run
    def run(self, waves=None) -> dict:
        nw = int(waves if waves is not None else self.th["waves"])
        state = None
        valid = {}
        self._reset_checks()                 # Codex r5 P2-8: history and clearing reset together
        self._restore_checks()               # Codex r6 P1-1: unresolved failed checks of the saved runs
        off = 0
        if self.prior_n:      # the earlier runs are wave 0: checked (cross-validation over them), fitted, a region
            pu, ps = list(self.data.unit), list(self.data.shared)
            valid = self._validate(pu, ps)
            for ob, v in valid.items():
                self._note_check(ob, v)
            off = 1000 + self.prior_n          # Codex r3 P2-7: a continuation's own seeds, apart from earlier ones
            if self.sobs and not ps:            # Codex r3 P2-5: no earlier shared runs: the shared design first
                _, srows = self._design(0, None, label=off - 1, units=False)
                srows = self._run("shared", srows)
                self.data.add("shared", srows)
                ps = list(self.data.shared)
                valid.update(self._validate([], srows))
                for ob in self.sobs:
                    self._note_check(ob, valid[ob])
            self.em = self._fit(pu, ps)
            state = self._region(self.em)
            self.waves.append(dict(self._wave_summary(-1, pu, ps, valid, state), prior_runs=len(pu),
                                   prior_shared_runs=len(ps)))
            self.log(f"joint calibration: {len(pu)} earlier unit runs and {len(ps)} shared runs as data; region "
                     f"{len(state['region'])} of {len(state['C'])}")
            off = 1000 + self.prior_n       # new waves: designs from the region, seeds apart from earlier ones
        for w in range(nw):
            urows, srows = self._design(w + off, state)
            self.log(f"joint calibration wave {w + 1}/{nw}: {len(urows)} unit runs, {len(srows)} shared runs")
            urows = self._run("unit", urows)
            srows = self._run("shared", srows)
            self.data.add("unit", urows)
            self.data.add("shared", srows)
            valid = self._validate(urows, srows)
            for ob, v in valid.items():
                self._note_check(ob, v)
            self.em = self._fit(self.data.unit, self.data.shared)
            state = self._region(self.em)
            self.waves.append(self._wave_summary(w, urows, srows, valid, state))
            self.log(f"  region: {len(state['region'])} of {len(state['C'])} candidates; unmatched "
                     f"{len(state['unmatchable'])}; conflicting {len(state['conflicting'])}")
        self.state = state
        return self.report(valid)

    def _reset_checks(self):
        self.valid_hist, self.valid_cleared, self._failed, self._check_errs = {}, {}, {}, {}

    def _note_check(self, ob, v):
        """Record one emulator check. A failed check keeps its runs (its cohort). A later check that covers every
        run so far clears it only when it passes overall and, out of fold, on each failed cohort's runs (Codex r5
        P1-1: a pooled pass alone could dilute the same failures)."""
        if not hasattr(self, "valid_cleared"):
            self._reset_checks()
        h = self.valid_hist.setdefault(ob, [])
        h.append(v["status"])
        errs = (getattr(self, "_check_errs", {}) or {}).get(ob, {})
        if v["status"] != "passed":
            self._failed.setdefault(ob, []).append(set(errs))
        elif v.get("covers_all"):
            # Codex r6 P1-2: every run of the failed check must be in this one; a partial cohort clears nothing
            sub = [validation([errs[k] for k in c], self.eth) if c <= set(errs) else {"status": "incomplete"}
                   for c in self._failed.get(ob, [])]
            if all(x["status"] == "passed" for x in sub):
                self.valid_cleared[ob] = len(h) - 1      # every earlier check is superseded by this one
                self._failed[ob] = []

    @staticmethod
    def _run_key(kind, r):
        return (kind,) + _key(r) + (r.get("seed"),)

    def _restore_checks(self):
        """Codex r6 P1-1: the unresolved failed checks of saved runs (each run's failed_checks tags, and the size of
        each check in unresolved_checks) are failed checks of this run too. A check whose runs are not all
        reused keeps a member no later check can supply, so it never clears (fail closed)."""
        carried = getattr(self, "_carried", None) or {}
        for ob, sizes in carried.items():
            cs = [set() for _ in sizes]
            for kind in ("unit", "shared"):
                for r in getattr(self.data, kind):
                    for c in (r.get("failed_checks") or {}).get(ob, []):
                        if isinstance(c, int) and 0 <= c < len(cs):
                            cs[c].add(self._run_key(kind, r))
            for c, n in enumerate(sizes):
                if len(cs[c]) < int(n):
                    cs[c].add(("missing", ob, c))
            self._failed.setdefault(ob, []).extend(cs)
            self.valid_hist.setdefault(ob, []).append("failed")

    def _cohort_tags(self, kind, r) -> dict:
        k = self._run_key(kind, r)
        return {ob: [c for c, s in enumerate(cs) if k in s] for ob, cs in (getattr(self, "_failed", {}) or {}).items()
                if any(k in s for s in cs)}

    def _inadequate(self) -> dict:
        hist = getattr(self, "valid_hist", {}) or {}
        cl = getattr(self, "valid_cleared", {}) or {}
        return {ob: s for ob, s in hist.items() if any(x != "passed" for x in s[cl.get(ob, -1) + 1:])}

    def _wave_summary(self, w, urows, srows, valid, st):
        return {"wave": w + 1, "unit_runs": len(urows), "shared_runs": len(srows), "validation": valid,
                "candidates": int(len(st["C"])), "accepted": int(len(st["region"])),
                "share_of_prior": st["share_of_prior"], "relaxed": st["relaxed"],
                "unmatched": list(st["unmatchable"]), "conflicting": list(st["conflicting"]),
                "ranges": self._ranges(st)}

    def _ranges(self, st):
        R = st["C"][st["region"]] if len(st["region"]) else np.zeros((0, self.p))
        out = {}
        for j, p in enumerate(self.P.shared):
            out[p.name] = [float(p.natural(R[:, j].min())), float(p.natural(R[:, j].max()))] if len(R) else None
        return out

    def figure_data(self, smp, units, ver) -> dict:
        """What the calibration figures draw (toolkit/calibration/figures.py), on natural scales: a subset of the
        shared-value candidates with their worst implausibility and whether they are in the region; the co-variation
        of each pair inside the region (|Spearman|); the optical depth of each pair (the share of candidates in each
        cell of the pair's prior box that are non-implausible, whatever the other parameters); each target's observed
        interval against the band the region gives it; the region's prior share per wave."""
        st, P = self.state, self.P
        fth = thresholds().get("figures") or {}
        C, R = st["C"], st["region"]
        n0 = st.get("n_sobol") or len(C)
        rng = np.random.default_rng([self.seed, 23])
        keep_u = [u for u in P.units if u not in st["unmatchable"]]
        worst = np.zeros(len(C))
        for u in keep_u:
            worst = np.maximum(worst, st["units"][u]["m"])
        for v in st["targets"].values():
            worst = np.maximum(worst, v["I"])
        acc = np.zeros(len(C), bool)
        acc[R] = True
        nmax = int(fth.get("points_max", 4000))
        idx = np.arange(len(C)) if len(C) <= nmax else np.sort(rng.choice(len(C), nmax, replace=False))
        nat = lambda U: np.column_stack([p.natural(U[:, j]) for j, p in enumerate(P.shared)]) \
            if self.p else np.zeros((len(U), 0))  # noqa: E731
        out = {"names": P.names, "prior": {p.name: dict(p.describe(), point=p.mode) for p in P.shared},
               "cutoff": self.cut,
               "points": {"values": nat(C[idx]).tolist(), "implausibility": worst[idx].tolist(),
                          "accepted": acc[idx].tolist()},
               "waves": [{"wave": w["wave"], "share_of_prior": w["share_of_prior"], "accepted": w["accepted"],
                          "candidates": w["candidates"], "ranges": w["ranges"]} for w in self.waves]}
        A = nat(C[R]) if len(R) else np.zeros((0, self.p))
        pairs = []
        bins = int(fth.get("depth_bins", 12))
        Cs = C[:n0]
        accs = acc[:n0]
        for i in range(self.p):
            for j in range(i):
                rho = None
                if len(A) >= 5 and np.std(A[:, i]) > 0 and np.std(A[:, j]) > 0:
                    from scipy.stats import spearmanr
                    rho = float(spearmanr(A[:, i], A[:, j]).correlation)
                bi = np.minimum((Cs[:, i] * bins).astype(int), bins - 1)
                bj = np.minimum((Cs[:, j] * bins).astype(int), bins - 1)
                tot = np.zeros((bins, bins))
                hit = np.zeros((bins, bins))
                np.add.at(tot, (bi, bj), 1)
                np.add.at(hit, (bi, bj), accs.astype(float))
                depth = np.where(tot > 0, hit / np.maximum(tot, 1), np.nan)
                pairs.append({"x": P.names[j], "y": P.names[i], "abs_spearman": None if rho is None else abs(rho),
                              "spearman": rho, "depth": depth.tolist(), "bins": bins})
        out["pairs"] = pairs
        rows = []
        if smp and P.units:
            S = np.asarray(smp["rows"], float)
            k = min(len(S), int(fth.get("band_rows", 200)))
            S = S[rng.choice(len(S), k, replace=False)]
            Tn = S[:, :self.p]
            Tc = np.column_stack([(Tn[:, j] - p.lo) / (p.hi - p.lo) for j, p in enumerate(P.shared)]) \
                if self.p else np.zeros((k, 0))
            lo_, hi_ = P.unit_param["lo"], P.unit_param["hi"]
            for n, u in enumerate(P.units):
                zc = (S[:, self.p + n] - lo_) / (hi_ - lo_)
                for tg in P.unit_targets(u):
                    m, v = self.em["unit"][tg.observable].predict(u, zc, Tc, noise=False)
                    q = np.percentile(inv(m, tg.scale), [2.5, 50, 97.5])
                    sdo = math.sqrt(tg.sd_obs ** 2 + tg.sd_bias ** 2)
                    sim = None
                    if ver and u in ver["units"]:
                        sim = next((x["simulated"] for x in ver["units"][u]["targets"] if x["target"] == tg.id), None)
                    rows.append({"target": tg.id, "kind": "unit", "unit": u, "observable": tg.observable,
                                 "observed": tg.value, "obs_lo": float(inv(tg.y - 1.96 * sdo, tg.scale)),
                                 "obs_hi": float(inv(tg.y + 1.96 * sdo, tg.scale)), "model_lo": float(q[0]),
                                 "model_mid": float(q[1]), "model_hi": float(q[2]), "verified": sim,
                                 "status": units[u]["status"]})
        for tg in P.targets:
            if tg.kind in ("group", "shared"):
                v = st["targets"][tg.id]
                vals = inv(v["mean"][R], tg.scale) if len(R) else np.array([np.nan])
                q = np.nanpercentile(vals, [2.5, 50, 97.5])
                sdo = math.sqrt(tg.sd_obs ** 2 + tg.sd_bias ** 2)
                sim = (ver or {}).get("targets", {}).get(tg.id, {}).get("simulated")
                rows.append({"target": tg.id, "kind": tg.kind, "unit": None, "observable": tg.observable,
                             "observed": tg.value, "obs_lo": float(inv(tg.y - 1.96 * sdo, tg.scale)),
                             "obs_hi": float(inv(tg.y + 1.96 * sdo, tg.scale)), "model_lo": float(q[0]),
                             "model_mid": float(q[1]), "model_hi": float(q[2]), "verified": sim,
                             "status": "matched" if (ver or {}).get("targets", {}).get(tg.id, {}).get("ok", True)
                             else "verification_failed"})
        out["fit"] = rows
        # per-unit transmission against the shared values (figures): region rows, each unit's best value, the bound
        if smp and P.units:
            S = np.asarray(smp["rows"], float)
            k = min(len(S), int(fth.get("transmission_rows", 120)))
            S = S[np.random.default_rng([self.seed, 29]).choice(len(S), k, replace=False)]
            lo_, hi_ = P.unit_param["lo"], P.unit_param["hi"]
            step = (hi_ - lo_) / max(len(self.zg) - 1, 1)
            zb = {u: units[u]["z_best"] for u in P.units}
            out["transmission"] = {
                "rows": S.tolist(), "n_shared": self.p, "units": P.units, "groups": [P.groups.get(u, "") for u in P.units],
                "unit_param": dict(P.unit_param), "z_best": [zb[u] for u in P.units],
                "at_top": [zb[u] >= hi_ - 0.5 * step for u in P.units],
                "at_bottom": [zb[u] <= lo_ + 0.5 * step for u in P.units],
                "touches_top": ["hi" in (units[u]["region_touches_bound"] or []) for u in P.units],
                "status": [units[u]["status"] for u in P.units]}
        cand_all = nat(C[:n0]) if self.p else np.zeros((0, 0))
        out["design_share"] = {}
        for j, p in enumerate(P.shared):        # the compatible share of the design's candidates, by bin
            e = np.linspace(p.lo, p.hi, 21)
            b = np.clip(np.digitize(cand_all[:, j], e) - 1, 0, 19)
            tot = np.bincount(b, minlength=20)
            hit = np.bincount(b, weights=acc[:n0].astype(float), minlength=20)
            out["design_share"][p.name] = {"edges": e.tolist(),
                                           "share": [float(h / t) if t else None for h, t in zip(hit, tot)]}
        return out

    def compatible(self, theta: dict) -> bool:
        """Is this joint shared value non-implausible for every target and unit (on the final emulators)?"""
        x = np.array([[(float(theta[p.name]) - p.lo) / (p.hi - p.lo) for p in self.P.shared]]).reshape(1, self.p)
        st = self._evaluate(self.em, x)
        return bool(all(r["m"][0] <= self.cut for r in st["units"].values())
                    and all(v["I"][0] <= self.cut for v in st["targets"].values()))

    def _witness_g(self, u, c) -> int:
        """Unit u's grid index at candidate c: its group witness when it belongs to a group target, else its
        least implausible value."""
        st = self.state
        own = self._owner()
        if u in own:
            k = int(st["targets"][own[u]]["kstar"][c])         # the component's targets share one kstar
            return int(st["units"][u]["sel"][k, c])
        return int(st["units"][u]["g"][c])

    def _verify(self, best_c, zbest):
        """Run the engine at the best shared point for every unit (and the shared targets): simulated implausibility.
        Each unit is run at `z_points` values of its own parameter spread over its non-implausible values there (its
        best value first), and judged at the one the engine matches best: the unit parameter is one-dimensional and
        moves only its unit, so this checks the engine itself, not the emulator's choice of grid value."""
        st = self.state
        t = st["C"][best_c]
        k = int(self.vth.get("z_points", 3))
        urows = []
        own = self._owner()
        for u in self.P.units:
            okc = np.flatnonzero(st["units"][u]["ok"][best_c])
            cand = [int(zbest[u])]
            if len(okc) > 1 and u not in own:     # a group member is run at the joint witness only
                cand += [int(okc[0]), int(okc[-1])]       # Codex r1 P2-9: both ends before interior values
                cand += [int(okc[int(round(q * (len(okc) - 1)))]) for q in np.linspace(0, 1, max(k, 2))[1:-1]]
            seen = []
            for g in cand:
                if g not in seen:
                    seen.append(g)
            for n, g in enumerate(seen[:max(k, 1)]):
                urows.append({"unit": u, "s": float(self.zg[g]), "t": t.copy(), "seed": _seed(self.seed, "verify", u, n),
                              "g": g})
        srows = [{"t": t.copy(), "seed": _seed(self.seed, "verify", "shared")}] if self.sobs else []
        urows = self._run("unit", urows)
        srows = self._run("shared", srows)
        out = {"units": {}, "targets": {}}
        byu = {}
        b_at = self._verify_offsets(urows)
        if b_at:
            out["bias"] = b_at
        for u in self.P.units:
            best = None
            for r in (x for x in urows if x["unit"] == u):
                worst, rows = 0.0, []
                for tg in self.P.unit_targets(u):
                    noise = self.em["unit"][tg.observable].tn if self.th["include_run_noise"] else 0.0
                    y = float(tr(r["obs"][tg.observable], tg.scale))
                    i = float(self._impl(tg, y, noise, b_at.get(tg.bias_key)))
                    worst = max(worst, i)
                    rows.append({"target": tg.id, "simulated": r["obs"][tg.observable], "implausibility": round(i, 4)})
                if best is None or worst < best[0]:
                    best = (worst, rows, r)
            byu[u] = best[2]["obs"]
            out["units"][u] = {"implausibility": round(best[0], 4), "ok": best[0] <= self.cut, "targets": best[1],
                               "z": float(self.P.z_natural(best[2]["s"])),
                               "runs": sum(1 for x in urows if x["unit"] == u),
                               "recorded": {k: best[2]["obs"][k] for k in self.record if k in best[2]["obs"]}}
        for tg in self.P.targets:
            if tg.kind == "group":
                ws = sum(tg.weights.values())
                pbar = sum(w / ws * byu[u][tg.observable] for u, w in tg.weights.items())
                # Codex r1 P2-8: the members' run noise, independent runs, through the aggregate
                tn = self.em["unit"][tg.observable].tn if self.th["include_run_noise"] else 0.0
                sdn = math.sqrt(sum((w / ws * float(dinv(tr(byu[u][tg.observable], tg.scale), tg.scale))) ** 2 * tn
                                    for u, w in tg.weights.items()))
                sdy = sdn / max(abs(float(dinv(tr(pbar, tg.scale), tg.scale))), 1e-12)
                i = abs(float(tr(pbar, tg.scale)) - tg.y) / math.sqrt(sdy ** 2 + tg.var)
                out["targets"][tg.id] = {"simulated": pbar, "implausibility": round(i, 4), "ok": i <= self.cut}
            elif tg.kind == "shared":
                v = srows[0]["obs"][tg.observable]
                noise = self.em["shared"][tg.observable].tn if self.th["include_run_noise"] else 0.0
                i = abs(float(tr(v, tg.scale)) - tg.y) / math.sqrt(noise + tg.var)
                out["targets"][tg.id] = {"simulated": v, "implausibility": round(i, 4), "ok": i <= self.cut}
        return out

    def _verify_offsets(self, urows) -> dict:
        """{block: offset} re-profiled on the engine's runs at the best point, as the unit parameter is, and solved
        exactly (Codex r1 P2-3): each run of each unit holds on an interval of each block's offset, so a common offset
        exists exactly when one built from the intervals' ends lies in every unit's set. Per component, among those
        ends and the fine grid (inside +-cutoff sd): the fewest units unmatched (each at its best run), then the
        smallest worst implausibility, then the smallest |b| / sd."""
        out = {}
        for blocks, us in self.bcomp:
            k, sds = len(blocks), np.array([self.bsd[b] for b in blocks])
            axes = [list(self._cells(1, fine=True)[:, 0] * s) for s in sds]
            for r in (x for x in urows if x["unit"] in set(us)):
                for tg in self.P.unit_targets(r["unit"]):
                    if tg.bias_key is not None:
                        noise = self.em["unit"][tg.observable].tn if self.th["include_run_noise"] else 0.0
                        e = (self.cut - 1e-9) * math.sqrt(noise + tg.var_fit)
                        c = tg.y - float(tr(r["obs"][tg.observable], tg.scale))
                        axes[blocks.index(tg.bias_key)] += [c - e, c + e]
            axes = [np.clip(np.unique(a), -self.cut * s, self.cut * s) / s for a, s in zip(axes, sds)]
            combos = np.array(np.meshgrid(*axes, indexing="ij")).reshape(k, -1).T
            W = []
            for u in us:
                best = np.full(len(combos), np.inf)
                for r in (x for x in urows if x["unit"] == u):
                    w = np.zeros(len(combos))
                    for tg in self.P.unit_targets(u):
                        noise = self.em["unit"][tg.observable].tn if self.th["include_run_noise"] else 0.0
                        b = None if tg.bias_key is None else combos[:, blocks.index(tg.bias_key)] * tg.sd_bias
                        w = np.maximum(w, self._impl(tg, float(tr(r["obs"][tg.observable], tg.scale)), noise, b))
                    best = np.minimum(best, w)
                W.append(best)
            W = np.array(W)
            i = int(np.lexsort(((combos ** 2).sum(1), W.max(0), (W > self.cut).sum(0)))[0])
            out.update({bk: float(combos[i, j] * self.bsd[bk]) for j, bk in enumerate(blocks)})
        return out

    def report(self, valid) -> dict:
        P, st = self.P, self.state
        C, R = st["C"], st["region"]
        blocks = []
        # Codex r2 P1-2: a wave whose check failed (or could not run) blocks even when a later wave passes: the later
        # check holds out only the later wave's points, so it cannot clear what the failed emulator excluded
        # a failed check stays until a later check covers every run so far (cross-validation over all of them) and
        # passes, overall and on the failed check's own runs (_note_check)
        inadequate = self._inadequate() if getattr(self, "valid_hist", None) else \
            {ob: [v["status"]] for ob, v in valid.items() if v["status"] != "passed"}
        if inadequate:
            blocks.append({"code": "emulator_inadequate", "detail": {ob: f"wave checks {s}" for ob, s in inadequate.items()}})
        if not len(R):
            blocks.append({"code": "empty_region", "detail": "no candidate satisfies the shared and group targets"})
        unmatched = list(st["unmatchable"])
        conflicting = list(st["shared_conflict"]) + [u for u in st["conflicting"] if u not in st["shared_conflict"]]
        if len(unmatched) + len(conflicting) > int(self.bth["unmatched_units_max"]):
            blocks.append({"code": "unmatched_units", "detail": {"unmatched": unmatched, "conflicting": conflicting,
                                                                 "conflicts_with_shared_targets":
                                                                     list(st["shared_conflict"])}})
        if st["relaxed"]:
            blocks.append({"code": "conflicting_units", "detail": f"the region is relaxed: {st['relaxed']} unit(s) "
                                                                  "unmatched at every accepted value"})
        src = R if len(R) else np.argsort(st["score"])[:1]
        best_c = int(src[np.argmin(st["score"][src])])
        zbest = {u: self._witness_g(u, best_c) for u in P.units}
        best_theta = P.theta_natural(C[best_c])[0] if self.p else {}
        units = {}
        for u in P.units:
            r = st["units"][u]
            okR = r["ok"][R] if len(R) else np.zeros((0, len(self.zg)), bool)
            zset = self.zg[okR.any(0)] if len(okR) else np.array([])
            gb = zbest[u]
            miss = r["m"].argmin()
            d = None
            misses = None
            if u in unmatched:
                gm = int(r["g"][miss])
                top, bot = gm == len(self.zg) - 1, gm == 0
                sg = r["sign"][miss]
                d = ("target_above_reach" if top and sg < 0 else "target_below_reach" if bot and sg > 0
                     else "no_value_fits_every_target")
                misses = []        # each target at the unit's least implausible point: which ones miss, which way
                for tg in P.unit_targets(u):
                    m, v = self.em["unit"][tg.observable].predict(u, [self.zg[gm]], np.atleast_2d(C[miss]),
                                                                  noise=bool(self.th["include_run_noise"]))
                    b = st["bias"][tg.bias_key][miss] if tg.bias_key is not None else None
                    misses.append({"target": tg.id, "observed": tg.value, "modelled": float(inv(m[0], tg.scale)),
                                   "implausibility": round(float(self._impl(tg, m[0], v[0], b)), 3)})
            at_b = [nm for nm, gi in (("lo", 0), ("hi", len(self.zg) - 1)) if len(okR) and okR[:, gi].any()]
            units[u] = {"matched": u not in unmatched and u not in conflicting,
                        "status": "unmatched" if u in unmatched else "conflicts_with_shared_targets"
                        if u in st["shared_conflict"] else "conflicting" if u in conflicting else "matched",
                        "best_implausibility": float(r["m"].min()),
                        "implausibility_at_best": float(r["m"][best_c]),
                        "z_best": float(P.z_natural(self.zg[gb])),
                        "z_interval": [float(P.z_natural(zset.min())), float(P.z_natural(zset.max()))]
                        if len(zset) else None,
                        "region_touches_bound": at_b, "direction": d, "misses": misses}
        ranges = self._ranges(st)
        contraction = {p.name: (None if ranges[p.name] is None else
                                round(1 - (ranges[p.name][1] - ranges[p.name][0]) / (p.hi - p.lo), 4))
                       for p in P.shared}
        ver = None
        if self.vth.get("run") and (P.units or self.sobs):
            ver = self._verify(best_c, zbest)
            for u, v in ver["units"].items():
                units[u]["z_verified"] = v["z"]
                units[u]["z_best"] = v["z"]          # the value the engine itself matches best at the best point
                units[u]["implausibility_verified"] = v["implausibility"]
            bad = [u for u, v in ver["units"].items() if not v["ok"] and units[u]["matched"]]
            badt = [k for k, v in ver["targets"].items() if not v["ok"]]
            for u in bad:
                units[u]["status"] = "verification_failed"
                units[u]["matched"] = False
            if bad or badt:
                blocks.append({"code": "verification_failed", "detail": {"units": bad, "targets": badt}})
        bias = {k: {"offset": float(v[best_c]), "sd": self.bsd[k], "z": round(float(v[best_c]) / self.bsd[k], 4),
                    "units": len({t.unit for t in P.targets if t.bias_key == k}),
                    "offset_verified": ((ver or {}).get("bias") or {}).get(k)}
                for k, v in (st.get("bias") or {}).items()}
        tsum = {}
        for t in P.targets:
            if t.kind in ("shared", "group"):
                v = st["targets"][t.id]
                tsum[t.id] = dict(t.describe(), implausibility_at_best=float(v["I"][best_c]),
                                  modelled_at_best=float(inv(v["mean"][best_c], t.scale)),
                                  non_implausible_share=float(np.mean(v["I"] <= self.cut)))
        smp = self.sample(int(self.th["draws_kept"])) if len(R) else None
        figd = self.figure_data(smp, units, ver)
        # the extreme accepted values (ranges) can span the whole prior while the region's mass moves: compare the
        # region sample's central 95% interval with the prior's (contraction95 = 1 - width ratio)
        if smp:
            Sx = np.asarray(smp["rows"], float)
            for j, p in enumerate(P.shared):
                a, b = np.percentile(Sx[:, j], [2.5, 97.5])
                pa, pb = p.quantiles([0.025, 0.975])
                inter95 = {"region": [float(a), float(b)], "prior": [pa, pb],
                           "median": float(np.median(Sx[:, j])), "point": p.mode,
                           "contraction95": round(1 - (b - a) / (pb - pa), 4) if pb > pa else None}
                self._i95 = getattr(self, "_i95", {})
                self._i95[p.name] = inter95
        runs = {"unit": [dict({"unit": r["unit"], "z": float(P.z_natural(r["s"])), "theta": P.theta_natural(r["t"])[0]
                               if self.p else {}, "seed": r["seed"], "obs": r["obs"]},
                              **({"ref": r["ref"]} if r.get("ref") else {}),
                              **({"failed_checks": t} if (t := self._cohort_tags("unit", r)) else {}))
                         for r in self.data.unit],
                "shared": [dict({"theta": P.theta_natural(r["t"])[0], "seed": r["seed"], "obs": r["obs"]},
                                **({"failed_checks": t} if (t := self._cohort_tags("shared", r)) else {}))
                           for r in self.data.shared]}
        unres = {ob: [len(c) for c in cs] for ob, cs in (getattr(self, "_failed", {}) or {}).items() if cs}
        if unres:       # Codex r6 P1-1: a continuation from these runs keeps the failed checks not yet cleared
            runs["unresolved_checks"] = unres
        return {"method": "history_matching", "producer": PRODUCER, "figure_data": figd, "runs": runs,
                "status": "blocked" if blocks else "matched", "blocked": bool(blocks), "blocks": blocks,
                "cutoff": self.cut,
                "shared": {p.name: p.describe() for p in P.shared},
                "unit_parameter": P.unit_param,
                "region": {"candidates": int(len(C)), "accepted": int(len(R)), "share_of_prior": st["share_of_prior"],
                           "relaxed": st["relaxed"], "ranges": ranges,
                           "prior_ranges": {p.name: [p.lo, p.hi] for p in P.shared}, "contraction": contraction,
                           "interval95": dict(getattr(self, "_i95", {}))},
                "best": {"theta": best_theta, "z": {u: units[u]["z_best"] for u in P.units},
                         "score": float(st["score"][best_c])},
                "units": units, "unmatched": unmatched, "conflicting": conflicting,
                "n_units": len(P.units), "n_matched": sum(1 for u in units.values() if u["matched"]),
                "targets": tsum, "unit_targets": {u: [t.describe() for t in P.unit_targets(u)] for u in P.units},
                "verification": ver, "bias": bias,
                "weighting": {"best_point": self.th.get("best_point_weighting", "per_unit"),
                              "bias_grid": len(self.bgrid)},
                "waves": self.waves, "simulated_at_boundary": dict(self.boundary),
                "emulators": {"unit": {ob: e.hyper for ob, e in self.em["unit"].items()},
                              "shared": {ob: {"kernel": str(e.gp.kernel_)} for ob, e in self.em["shared"].items()}},
                "thresholds": {"history_matching": self.th, "emulator": self.eth, "blocking": self.bth},
                "sample": smp}

    def sample(self, n: int, seed: int | None = None) -> dict:
        """n rows over the region: shared values drawn from the accepted candidates by prior weight, each unit's
        parameter uniform over its non-implausible grid values at that candidate (jittered within a grid step).
        Names: the shared names, then z:<unit>. The id hashes the names and the rows."""
        st, P = self.state, self.P
        R = st["region"]
        rng = np.random.default_rng([self.seed if seed is None else int(seed), 17])
        # Codex r1 P2-4: refinement candidates are a proposal around accepted points, not prior draws; the sample uses
        # the Sobol candidates (uniform on the box, so prior weights are correct) whenever they hold enough
        Rs = R[R < st["n_sobol"]] if st.get("n_sobol") else R
        sobol_only = len(Rs) >= int(self.th["region_min_candidates"])
        R = Rs if sobol_only else R
        w = P.prior_weight(st["C"][R]) if self.p else np.ones(len(R))
        pick = rng.choice(R, size=int(n), p=w / w.sum())
        names = P.names + [f"z:{u}" for u in P.units]
        rows = []
        step = 1.0 / max(len(self.zg) - 1, 1)
        own = self._owner()
        gts = [t for t in P.targets if t.kind == "group"]
        zd = self._offset_draws(pick, rng) if self.bcomp else {}         # Codex r1 P1: offsets drawn first
        for i, c in enumerate(pick):
            row = [float(p.natural(st["C"][c][j])) for j, p in enumerate(P.shared)]
            gsel = self._group_draw(gts, own, int(c), rng) if gts else {}
            for u in P.units:
                if u in gsel:
                    row.append(float(P.z_natural(self.zg[gsel[u]])))
                    continue
                if u in zd:
                    okc, gi = np.flatnonzero(zd[u][0][i]), zd[u][1][i]
                else:
                    okc = np.flatnonzero(st["units"][u]["ok"][c])
                    gi = rng.choice(okc) if len(okc) else int(st["units"][u]["g"][c])
                s = float(np.clip(self.zg[gi] + rng.uniform(-0.5, 0.5) * step, 0, 1)) if len(okc) else self.zg[gi]
                if len(okc):       # stay inside the non-implausible bracket around the chosen grid value
                    lo = self.zg[gi - 1] if gi > 0 and gi - 1 in okc else self.zg[gi]
                    hi = self.zg[gi + 1] if gi + 1 < len(self.zg) and gi + 1 in okc else self.zg[gi]
                    s = float(np.clip(s, lo, hi))
                row.append(float(P.z_natural(s)))
            rows.append(row)
        rows = np.asarray(rows, float).reshape(len(rows), len(names))
        h = hashlib.sha256(json.dumps(names).encode())
        h.update(np.ascontiguousarray(rows).tobytes())
        return {"sample_id": h.hexdigest(), "names": names, "rows": rows.tolist(), "method": "history_matching_region",
                "weighting": ("prior density over the non-implausible region" + ("" if sobol_only else
                              " (including refinement candidates, whose proposal density is not corrected)") +
                              "; a unit's parameter uniform within its non-implausible values; units of a group "
                              "target at a joint draw that holds every group target" +
                              ("; each shared offset drawn among its feasible cells (prior-weighted), the units of "
                               "its block given it" if self.bcomp else ""))}

    def _group_draw(self, gts, own, c, rng) -> dict:
        """{unit: grid index} for the group members at candidate c, kept only when every group target holds at the
        joint values: first each member at its own random share (combinations off the common path, Codex r2 P1-1),
        then a non-implausible common share per component; else the witness."""
        st = self.state
        units = st["units"]
        K = int(self.th["group_t"])
        tries = int(self.th["group_tries"])
        cc = np.array([c])
        holds = lambda km: all(self._group_terms(t, units, km, cc)[0][0] <= self.cut for t in gts)  # noqa: E731
        for _ in range(tries):
            kmap = {u: np.array([int(rng.integers(K))]) for u in own}
            if holds(kmap):
                return {u: int(units[u]["sel"][kmap[u][0], c]) for u in own}
        for _ in range(tries):
            kc = {}
            for t in gts:
                cid = own[next(iter(t.weights))]
                if cid not in kc:
                    feas = np.flatnonzero(st["targets"][t.id]["Ik"][:, c] <= self.cut)
                    kc[cid] = int(rng.choice(feas)) if len(feas) else int(st["targets"][t.id]["kstar"][c])
            kmap = {u: np.array([kc[own[u]]]) for u in own}
            if holds(kmap):
                return {u: int(units[u]["sel"][kc[own[u]], c]) for u in own}
        return {u: self._witness_g(u, c) for u in own}


def calibrate(problem: Problem, simulate, seed: int = 0, waves=None, log=print, prior=None, record=(), top_up=None,
              **override) -> dict:
    return HistoryMatch(problem, simulate, seed, log, prior=prior, record=record, top_up=top_up,
                        **override).run(waves)


# --------------------------------------------------------------------------- self-test
def _expit(x):
    return 1.0 / (1.0 + math.exp(-x))


def _toy(n_units=12, sd=0.1, extra=None, conflict=False, with_group=False, md=0.05):
    """A two-window toy: logit f0 = z + a_u + 0.3 b (before an intervention), logit f1 = f0 - 3 e s_u (after; s_u
    marks units with the intervention). e is identified by units with both windows; b is confounded with z."""
    truth = {"e": 0.6, "b": 0.5}
    units = [f"u{i}" for i in range(n_units)]
    a = {u: -1.0 + 2.0 * i / max(n_units - 1, 1) for i, u in enumerate(units)}
    s = {u: float(i % 2) for i, u in enumerate(units)}
    zt = {u: 0.4 - 0.1 * (i % 3) for i, u in enumerate(units)}

    def f(u, z, th, ob, noise=0.0):
        l0 = z + a[u] + 0.3 * th["b"]
        return _expit(l0 if ob == "w0" else l0 - 3.0 * th["e"] * s[u])

    tg = []
    for u in units:
        tg.append(Target(f"{u}:w1", "unit", "w1", f(u, zt[u], truth, "w1"), "logit", sd_obs=sd, sd_md=md, unit=u,
                         source="toy"))
        if s[u]:
            tg.append(Target(f"{u}:w0", "unit", "w0", f(u, zt[u], truth, "w0"), "logit", sd_obs=sd, sd_md=md, unit=u,
                             source="toy"))
    if conflict:      # two treated units whose windows imply e = 0.15 and e = 1.05 with tight errors
        for u, e in (("u1", 0.15), ("u3", 1.05)):
            for t in tg:
                if t.unit == u:
                    t.value = f(u, zt[u], {"e": e, "b": 0.5}, t.observable)
                    t.sd_obs, t.sd_md = 0.03, 0.0
    if with_group:
        w = {u: 1.0 + i for i, u in enumerate(units)}
        ws = sum(w.values())
        val = sum(w[u] / ws * f(u, zt[u], truth, "w0") for u in units)
        tg.append(Target("grp:w0", "group", "w0", val, "logit", sd_obs=sd, sd_md=md, weights=w, source="toy"))
    if extra:
        tg += extra(f, units)
    P = Problem([Param("e", 0.0, 1.2, "triangular", 0.5, "toy source"), Param("b", 0.0, 1.0, "uniform", None, "toy")],
                {"name": "z", "lo": -2.0, "hi": 2.0}, units, tg, features={u: [a[u]] for u in units},
                feature_names=["a"])
    calls = {"n": 0}

    def sim(pts):
        calls["n"] += len(pts)
        out = []
        for p in pts:
            r = np.random.default_rng(p["seed"])
            out.append({ob: _expit(math.log(f(p["unit"], p["z"], p["theta"], ob) /
                                            (1 - f(p["unit"], p["z"], p["theta"], ob))) + r.normal(0, 0.02))
                        for ob in ("w0", "w1")})
        return out
    return P, sim, truth, zt, calls


def _self_test() -> int:
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)

    def safe(fn, msg):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            ok(False, f"{msg}: raised {type(e).__name__}: {e}")

    q = lambda *a, **k: None  # noqa: E731
    fast = dict(candidates_log2=10, points_per_unit=10)

    def hm1():
        P, sim, truth, zt, calls = _toy()
        h = HistoryMatch(P, sim, 3, q, **fast)
        r = h.run(2)
        ok(h.compatible(truth) and not h.compatible({"e": 1.15, "b": 0.5}),
           "HM1g (Codex r1 P2-14) the planted joint value itself is compatible, a far one is not")
        rg = r["region"]["ranges"]
        ok(r["status"] == "matched" and r["n_matched"] == len(P.units) and not r["unmatched"],
           f"HM1a every unit matched on the toy, nothing blocked ({r['status']}, {r['blocks']})")
        ok(rg["e"][0] <= truth["e"] <= rg["e"][1] and r["region"]["contraction"]["e"] > 0.4,
           f"HM1b the identified shared parameter's region contains the truth and contracts ({rg['e']}, "
           f"{r['region']['contraction']['e']})")
        ok(r["region"]["contraction"]["b"] < 0.2,
           f"HM1c a parameter the data do not inform keeps (nearly) its prior range ({rg['b']})")
        ok(all(r["units"][u]["z_interval"][0] - 0.2 <= zt[u] <= r["units"][u]["z_interval"][1] + 0.2
               for u in P.units), "HM1d each unit's true parameter is inside (or within a grid step of) its interval")
        v = r["validation"] if "validation" in r else r["waves"][-1]["validation"]
        ok(all(x["status"] == "passed" for x in v.values()), f"HM1e the held-out emulator check passes ({v})")
        ok(r["verification"] and all(x["ok"] for x in r["verification"]["units"].values()),
           "HM1f verification: the engine at the best point matches every unit")
        smp = r["sample"]
        R = np.asarray(smp["rows"])
        j = smp["names"].index("e")
        ok(R.shape == (1000, 2 + len(P.units)) and rg["e"][0] - 1e-9 <= R[:, j].min() and R[:, j].max() <= rg["e"][1]
           + 1e-9, "HM8a the region sample has one row per draw, shared values inside the region")
        zs = R[:, smp["names"].index("z:u0")]
        zi = r["units"]["u0"]["z_interval"]
        ok(zi[0] - 0.11 <= zs.min() and zs.max() <= zi[1] + 0.11, "HM8b unit values inside their intervals")
        r2 = calibrate(P, sim, seed=3, waves=2, log=q, **fast)
        ok(r2["sample"]["sample_id"] == smp["sample_id"], "HM8c same seed, same inputs: the same sample id")
    safe(hm1, "HM1")

    def hm2():
        def extra(f, units):
            return [Target("u2:hi", "unit", "w1", 0.995, "logit", sd_obs=0.1, sd_md=0.05, unit="u2", source="toy")]
        P, sim, truth, zt, _ = _toy(extra=extra)
        r = calibrate(P, sim, seed=4, waves=1, log=q, **fast)
        u = r["units"]["u2"]
        ok("u2" in r["unmatched"] and u["status"] == "unmatched" and u["direction"] == "target_above_reach",
           f"HM2a a target above what the model reaches: the unit is unmatched and says so ({u})")
        ok(r["blocked"] and any(b["code"] == "unmatched_units" for b in r["blocks"]),
           "HM2b an unmatched unit blocks (never pinned at a bound silently)")
        ok(r["region"]["accepted"] > 0 and r["n_matched"] == len(P.units) - 1,
           f"HM2c the region is still computed from the other units ({r['region']['accepted']}, {r['n_matched']}, "
           f"{[(k, v['status']) for k, v in r['units'].items() if v['status'] != 'matched']})")
    safe(hm2, "HM2")

    def hm3():
        P, sim, *_ = _toy(conflict=True)
        r = calibrate(P, sim, seed=5, waves=1, log=q, **fast)
        ok(r["blocked"] and r["region"]["relaxed"] >= 1 and set(r["conflicting"]) & {"u1", "u3"},
           f"HM3 units matchable alone but not together: the region is relaxed and they are named "
           f"({r['conflicting']}, relaxed {r['region']['relaxed']})")
    safe(hm3, "HM3")

    def hm4():
        P = Problem([Param("a", 0.0, 1.0, "uniform", None, "toy"), Param("b", 0.0, 1.0, "uniform", None, "toy")], None,
                    [], [Target("B1", "shared", "s1", 2 * 0.55 + 0.35, "identity", sd_obs=0.05, source="toy"),
                         Target("B2", "shared", "s2", 0.55 - 0.35, "identity", sd_obs=0.05, source="toy")])

        def sim(pts):
            return [{"s1": 2 * p["theta"]["a"] + p["theta"]["b"], "s2": p["theta"]["a"] - p["theta"]["b"]}
                    for p in pts]
        r = calibrate(P, sim, seed=6, waves=2, log=q, candidates_log2=11, shared_points=20)
        rg = r["region"]["ranges"]
        ok(r["status"] == "matched" and rg["a"][0] <= 0.55 <= rg["a"][1] and rg["b"][0] <= 0.35 <= rg["b"][1]
           and r["region"]["contraction"]["a"] > 0.5,
           f"HM4 canary with no units: shared targets only; the region contains the truth and contracts ({rg})")
    safe(hm4, "HM4")

    def hm5():
        P, sim, truth, *_ = _toy(with_group=True)
        r = calibrate(P, sim, seed=7, waves=2, log=q, **fast)
        g = r["targets"].get("grp:w0")
        ok(g is not None and g["implausibility_at_best"] <= 3 and r["status"] == "matched"
           and r["region"]["ranges"]["e"][0] <= truth["e"] <= r["region"]["ranges"]["e"][1],
           f"HM5 a group target (a weighted aggregate over units) is matched and reported ({g and g['implausibility_at_best']})")
    safe(hm5, "HM5")

    def hm6():
        P, sim, *_ = _toy()
        P.targets[0].sd_obs = P.targets[0].sd_md = P.targets[0].sd_bias = 0.0
        try:
            HistoryMatch(P, sim, log=q)
            ok(False, "HM6a a target with no declared error is refused")
        except CalibrationInputError as e:
            ok("ground truth" in str(e), "HM6a a target with no declared error is refused (no source is ground truth)")
        Pa, sa, *_ = _toy(sd=0.08)
        Pb, sb, *_ = _toy(sd=0.4)
        ra = calibrate(Pa, sa, seed=8, waves=1, log=q, **fast)
        rb = calibrate(Pb, sb, seed=8, waves=1, log=q, **fast)
        wa = ra["region"]["ranges"]["e"][1] - ra["region"]["ranges"]["e"][0]
        wb = rb["region"]["ranges"]["e"][1] - rb["region"]["ranges"]["e"][0]
        ok(wb > wa, f"HM6b a larger sampling error gives a wider region ({wa:.3f} < {wb:.3f})")
    safe(hm6, "HM6")

    def hm7():
        rng = np.random.default_rng(0)
        good = validation(rng.normal(0, 1, 200))
        bad = validation(rng.normal(0, 4, 200))
        few = validation(rng.normal(0, 1, 3))
        ok(good["status"] == "passed" and bad["status"] == "failed" and few["status"] == "not_run",
           "HM7a held-out check: calibrated errors pass, overconfident ones fail, too few is not a pass")
        P, sim, *_ = _toy()
        real = UnitEmulator.predict

        def overconfident(self, u, S, T, noise=True):      # an emulator that states a hundredth of its variance
            m, v = real(self, u, S, T, noise)
            return m, v / 100.0
        UnitEmulator.predict = overconfident
        try:
            r = calibrate(P, sim, seed=9, waves=1, log=q, **fast)
        finally:
            UnitEmulator.predict = real
        ok(r["blocked"] and any(b["code"] == "emulator_inadequate" for b in r["blocks"]),
           f"HM7b an emulator whose stated uncertainty is too small fails closed ({[b['code'] for b in r['blocks']]})")
    safe(hm7, "HM7")

    def hm9():
        bad_cases = [
            ("non-finite target", lambda P: setattr(P.targets[0], "value", float("nan"))),
            ("group weight on an unknown unit", lambda P: P.targets.append(
                Target("g", "group", "w0", 0.3, "logit", sd_obs=0.1, weights={"nope": 1.0}, source="t"))),
            ("empty shared range", lambda P: setattr(P.shared[0], "hi", P.shared[0].lo)),
            ("shared range without a source", lambda P: setattr(P.shared[1], "source", "")),
            ("a unit with no target", lambda P: (P.units.append("orphan"), P.features.update(orphan=[0.0]))),
        ]
        for why, mut in bad_cases:
            P, sim, *_ = _toy()
            mut(P)
            try:
                HistoryMatch(P, sim, log=q)
                ok(False, f"HM9 refused: {why}")
            except CalibrationInputError:
                ok(True, f"HM9 refused: {why}")
        P, sim, *_ = _toy()

        def nan_sim(pts):
            out = sim(pts)
            out[0]["w1"] = float("nan")
            return out
        try:
            calibrate(P, nan_sim, seed=1, waves=1, log=q, **fast)
            ok(False, "HM9 a non-finite simulated value fails closed")
        except CalibrationInputError:
            ok(True, "HM9 a non-finite simulated value fails closed")
        ok(abs(sd_from_ci(0.2, 0.4, "logit") - (math.log(0.4 / 0.6) - math.log(0.2 / 0.8)) / (2 * 1.959964)) < 1e-6
           and abs(sd_from_n(0.3, 400, "identity") - math.sqrt(0.21 / 400)) < 1e-12,
           "HM10 sampling sd from an interval and from a sample size")
    safe(hm9, "HM9")

    def hm11():          # Codex r1 P1-1: a group target that binds must be profiled with the unit values
        units = ["a", "b"]
        tg = [Target(f"{u}:t", "unit", "x", 0.0, "identity", sd_obs=1.0, unit=u, source="toy") for u in units]
        tg.append(Target("g", "group", "x", 1.0, "identity", sd_obs=0.05, weights={"a": 1.0, "b": 1.0}, source="toy"))
        P = Problem([], {"name": "z", "lo": -2.0, "hi": 2.0}, units, tg)

        def sim(pts):
            return [{"x": p["z"] + 0.001 * ((p["seed"] % 7) - 3)} for p in pts]
        r = calibrate(P, sim, seed=2, waves=2, log=q, points_per_unit=12)
        R = np.asarray(r["sample"]["rows"]) if r["sample"] else np.zeros((0, 2))
        gm = R.mean(1) if len(R) else np.array([9.0])
        ok(r["status"] == "matched" and np.all(np.abs(gm - 1.0) <= 3 * 0.06),
           f"HM11 a binding group target is profiled jointly with the unit values (z = 0 fits each unit alone, only z "
           f"near 1 fits the group): matched, and every sample row satisfies the group ({r['status']}, "
           f"{r['blocks']}, group means {np.round(gm[:5], 3)})")
    safe(hm11, "HM11")

    def hm12():          # Codex r1 P1-3: identical inputs never sit on both sides of a validation split
        rows = [{"unit": "a", "s": 0.5, "t": np.array([0.2]), "seed": i} for i in range(4)] + \
               [{"unit": "a", "s": 0.1 * i, "t": np.array([0.3]), "seed": 10 + i} for i in range(6)]
        folds = _folds(rows, 5, np.random.default_rng(0))
        same = [k for k, f in enumerate(folds) if any(rows[i]["s"] == 0.5 and rows[i]["t"][0] == 0.2 for i in f)]
        ok(len(same) == 1 and sorted(i for f in folds for i in f) == list(range(10)),
           f"HM12a repeated inputs fall in one cross-validation fold ({same})")
        P, sim, *_ = _toy(n_units=3)
        real = UnitEmulator.predict

        def overconfident(self, u, S, T, noise=True):
            m, v = real(self, u, S, T, noise)
            return m, v / 400.0
        UnitEmulator.predict = overconfident
        try:
            r = calibrate(P, sim, seed=9, waves=1, log=q, points_per_unit=6, candidates_log2=8)
        finally:
            UnitEmulator.predict = real
        how = [v.get("how", "") for v in r["waves"][-1]["validation"].values()]
        ok(r["blocked"] and any(b["code"] == "emulator_inadequate" for b in r["blocks"])
           and all("cross-validation" in h for h in how),
           f"HM12b the cross-validation fallback (small design) also catches an overconfident emulator ({how})")
    safe(hm12, "HM12")

    def hm13():          # Codex r1 P2-10 and P2-15: domains and design minimum
        for lo, hi in ((-1, 0.4), (0.2, 1.2)):
            try:
                sd_from_ci(lo, hi, "logit")
                ok(False, f"HM13a an interval outside (0, 1) is refused on the logit scale ({lo}, {hi})")
            except ValueError:
                ok(True, f"HM13a an interval outside (0, 1) is refused on the logit scale ({lo}, {hi})")
        P, sim, *_ = _toy()

        def bad_sim(pts):
            out = sim(pts)
            out[0]["w1"] = 1.5
            return out
        try:
            calibrate(P, bad_sim, seed=1, waves=1, log=q, **fast)
            ok(False, "HM13b a simulated proportion outside (0, 1) fails closed")
        except CalibrationInputError:
            ok(True, "HM13b a simulated proportion outside (0, 1) fails closed")
        try:
            HistoryMatch(P, sim, log=q, points_per_unit=2)
            ok(False, "HM13c fewer runs per unit than min_points_per_unit is refused")
        except CalibrationInputError:
            ok(True, "HM13c fewer runs per unit than min_points_per_unit is refused")
    safe(hm13, "HM13")

    def hm14():          # Codex r2 P1-2: a failed validation is not erased by a later wave that passes
        P, sim, *_ = _toy()
        real = UnitEmulator.predict
        state = {"wave": 0}

        def flaky(self, u, S, T, noise=True):
            m, v = real(self, u, S, T, noise)
            return (m, v / 400.0) if state["wave"] == 0 else (m, v)
        UnitEmulator.predict = flaky
        h = HistoryMatch(P, sim, 9, q, **fast)
        orig = h._wave_summary

        def summ(*a, **k):
            out = orig(*a, **k)
            state["wave"] += 1
            return out
        h._wave_summary = summ
        try:
            r = h.run(2)
        finally:
            UnitEmulator.predict = real
        st = [{ob: v["status"] for ob, v in w["validation"].items()} for w in r["waves"]]
        ok(r["blocked"] and any(b["code"] == "emulator_inadequate" for b in r["blocks"])
           and any(s != "passed" for s in st[0].values()) and all(s == "passed" for s in st[1].values()),
           f"HM14 a wave whose emulator check failed still blocks after a later wave passes ({st})")
    safe(hm14, "HM14")

    def hm15():          # Codex r2 P1-1: two group targets over the same unit, with a value only jointly found
        tg = [Target("a:t", "unit", "x", 0.5, "identity", sd_obs=2.0, unit="a", source="toy"),
              Target("g1", "group", "x", 0.2, "identity", sd_obs=0.11, weights={"a": 1.0}, source="toy"),
              Target("g2", "group", "y", 0.8, "identity", sd_obs=0.11, weights={"a": 1.0}, source="toy")]
        P = Problem([], {"name": "z", "lo": -1.0, "hi": 2.0}, ["a"], tg)

        def sim(pts):
            return [{"x": p["z"] + np.random.default_rng(p["seed"]).normal(0, 0.01),
                     "y": p["z"] + np.random.default_rng(p["seed"] + 1).normal(0, 0.01)}
                    for p in pts]
        r = calibrate(P, sim, seed=4, waves=2, log=q, points_per_unit=12)
        zs = np.asarray(r["sample"]["rows"])[:, 0] if r["sample"] else np.array([9.0])
        ok(r["status"] == "matched" and np.all(np.abs(zs - 0.5) < 0.35),
           f"HM15 overlapping group targets are profiled together: z near 0.5 satisfies both ({r['status']}, "
           f"{r['blocks']}, z {np.round(zs[:4], 3)})")
    safe(hm15, "HM15")

    def hm16():          # a stochastic engine can simulate a proportion of exactly 0 or 1: counted, not refused
        P, sim, *_ = _toy()

        def edge(pts):
            out = sim(pts)
            out[0]["w1"] = 0.0
            return out
        r = calibrate(P, edge, seed=1, waves=1, log=q, **fast)
        ok(r["simulated_at_boundary"].get("w1", 0) >= 1, f"HM16 a simulated proportion at 0 is kept and counted "
                                                         f"({r.get('simulated_at_boundary')})")
    safe(hm16, "HM16")

    def hm17():          # 2026-11-06: earlier runs are data (a wider range, another error setting), not discarded
        P, sim, truth, *_ = _toy()
        r1 = calibrate(P, sim, seed=3, waves=1, log=q, **fast)
        calls = {"n": 0}

        def counted(pts):
            calls["n"] += len(pts)
            return sim(pts)
        P2, *_ = _toy()
        P2.unit_param = dict(P2.unit_param, hi=3.0)          # a raised ceiling: the earlier runs lie inside it
        h = HistoryMatch(P2, counted, 3, q, prior=r1["runs"], **fast)
        r2 = h.run(1)
        ok(len(h.data.unit) == len(r1["runs"]["unit"]) + len(P.units) * fast["points_per_unit"]
           and calls["n"] == len(P.units) * fast["points_per_unit"] + sum(v["runs"] for v in
                                                                         r2["verification"]["units"].values())
           and r2["waves"][0].get("prior_runs") == len(r1["runs"]["unit"]) and r2["status"] == "matched",
           f"HM17a earlier runs are reused as data, only the new wave and the verification simulate ({calls['n']})")
        h0 = HistoryMatch(P2, counted, 3, q, prior=r1["runs"], **fast)
        r0 = h0.run(0)
        ok(r0["region"]["accepted"] > 0 and len(r0["waves"]) == 1, "HM17b zero new waves: the earlier runs alone")
        bad = {"unit": [dict(r1["runs"]["unit"][0], z=9.0)] + r1["runs"]["unit"][1:], "shared": []}
        try:
            HistoryMatch(P2, sim, 3, q, prior=bad, **fast)
            ok(False, "HM17c an earlier run outside the declared range is refused")
        except CalibrationInputError:
            ok(True, "HM17c an earlier run outside the declared range is refused")

    def hm18():          # recorded observables: kept with every run and the verification, never a target
        P, sim, *_ = _toy()

        def rec(pts):
            out = sim(pts)
            for o in out:
                o["extra"] = 7.0
            return out
        r = calibrate(P, rec, seed=3, waves=1, log=q, record=["extra"], **fast)
        ok(all(x["obs"].get("extra") == 7.0 for x in r["runs"]["unit"])
           and all(v.get("recorded", {}).get("extra") == 7.0 for v in r["verification"]["units"].values()),
           "HM18 a recorded observable is kept with every run and the verification")
    safe(hm17, "HM17")
    safe(hm18, "HM18")

    def hm19():          # Codex r3 P2-5/P2-7/P2-8/P2-9: continuation details
        P = Problem([Param("a", 0.0, 1.0, "uniform", None, "toy")], {"name": "z", "lo": -2.0, "hi": 2.0}, ["u"],
                    [Target("u:t", "unit", "x", 0.4, "identity", sd_obs=0.2, unit="u", source="t"),
                     Target("B", "shared", "s", 0.5, "identity", sd_obs=0.05, source="t")])

        def sim(pts):
            return [({"x": p["z"] * 0.1 + p["theta"]["a"] * 0.2, "rec": 1.0} if p["kind"] == "unit" else
                     {"s": p["theta"]["a"] + 0.001 * (p["seed"] % 3)}) for p in pts]
        r1 = calibrate(P, sim, seed=5, waves=1, log=q, record=["rec"], candidates_log2=8, points_per_unit=6,
                       shared_points=12)
        only_units = {"unit": r1["runs"]["unit"], "shared": []}
        try:
            r2 = calibrate(P, sim, seed=5, waves=1, log=q, prior=only_units, record=["rec"], candidates_log2=8,
                           points_per_unit=6, shared_points=12)
            seeds1 = {x["seed"] for x in r1["runs"]["unit"] + r1["runs"]["shared"]}
            new = [x for x in r2["runs"]["unit"] + r2["runs"]["shared"] if x.get("seed") is not None]
            def bare(x):
                return {k: v for k, v in x.items() if k != "failed_checks"}
            old = [bare(x) for x in only_units["unit"]]
            fresh = [x for x in new if bare(x) not in old]
            ok(r2["region"]["accepted"] > 0 and not ({x["seed"] for x in fresh} & seeds1),
               "HM19a earlier unit runs without shared runs: the shared design is simulated first, and new runs "
               "never reuse an earlier seed")
            ok(all("rec" in x["obs"] for x in r2["runs"]["unit"]), "HM19b earlier runs keep their recorded values")
        except Exception as e:  # noqa: BLE001
            ok(False, f"HM19 continuation with shared targets: {type(e).__name__}: {e}")
        bad = {"unit": [dict(r1["runs"]["unit"][0], obs={"x": True})], "shared": []}
        try:
            HistoryMatch(P, sim, 5, q, prior=bad, candidates_log2=8, points_per_unit=6)
            ok(False, "HM19c a boolean observation in an earlier run is refused")
        except CalibrationInputError:
            ok(True, "HM19c a boolean observation in an earlier run is refused")
    safe(hm19, "HM19")

    def hm20():          # a later check covering every run clears a failure only if the failed runs now pass too
        h = HistoryMatch(*_toy()[:2], 1, q, **fast)
        h._reset_checks()
        rng = np.random.default_rng(1)
        k1 = [("a", i) for i in range(32)]
        h._check_errs = {"w0": dict(zip(k1, list(rng.normal(0, 4, 32))))}            # overconfident
        h._note_check("w0", {"status": "failed"})
        h._check_errs = {"w0": {("b", i): float(x) for i, x in enumerate(rng.normal(0, 1, 32))}}
        h._note_check("w0", {"status": "passed", "covers_all": False})
        a = h._inadequate()
        cov = {k: float(x) for k, x in zip(k1, rng.normal(0, 1, 32))}
        cov.update({("b", i): float(x) for i, x in enumerate(rng.normal(0, 1, 32))})
        h._check_errs = {"w0": cov}
        h._note_check("w0", {"status": "passed", "covers_all": True})
        b = h._inadequate()
        ok("w0" in a and "w0" not in b, "HM20 a failure stays after a new-wave pass, and clears after a pass of a check "
                                        "covering every run in which the failed runs pass too")
    safe(hm20, "HM20")

    def hm22():          # Codex r5 P1-1: a covering pass cannot clear a failure by dilution
        h = HistoryMatch(*_toy()[:2], 1, q, **fast)
        h._reset_checks()
        k1 = [("a", i) for i in range(32)]
        e1 = [0.5] * 30 + [5.0, 5.0]                                        # 2 of 32 beyond 3: 6% fails
        h._check_errs = {"w0": dict(zip(k1, e1))}
        h._note_check("w0", {"status": "failed"})
        pooled = dict(zip(k1, e1))                                          # 2 of 64: 3% passes overall
        pooled.update({("b", i): 0.5 for i in range(32)})
        h._check_errs = {"w0": pooled}
        h._note_check("w0", {"status": "passed", "covers_all": True})
        ok("w0" in h._inadequate(), "HM22 the same two failing runs diluted into a larger pass still block")
    safe(hm22, "HM22")

    def hm23():          # Codex r5 P1-2: an unmatched unit does not steer the shared best point
        def extra(f, units):
            return [Target("u2:hi", "unit", "w1", 0.995, "logit", sd_obs=0.1, sd_md=0.05, unit="u2", source="toy")]
        P, sim, *_ = _toy(extra=extra)
        h = HistoryMatch(P, sim, 4, q, **fast)
        r = h.run(1)
        st = h.state
        R = st["region"]
        sc = sum(st["units"][u]["m"] ** 2 for u in P.units if u != "u2") + \
            (sum(v["I"] ** 2 for v in st["targets"].values()) if st["targets"] else 0)
        want = int(R[np.argmin(np.asarray(sc)[R])])
        got = P.theta_natural(st["C"][want])[0]
        ok("u2" in r["unmatched"] and all(abs(r["best"]["theta"][k] - got[k]) < 1e-12 for k in got),
           "HM23 the best shared point is chosen from the matchable units only")
    safe(hm23, "HM23")

    def hm24():          # Codex r6 P1-2: a covering check that lacks some of the failed runs cannot clear them
        h = HistoryMatch(*_toy()[:2], 1, q, **fast)
        h._reset_checks()
        k1 = [("a", i) for i in range(32)]
        h._check_errs = {"w0": dict(zip(k1, [0.5] * 30 + [5.0, 5.0]))}
        h._note_check("w0", {"status": "failed"})
        later = {k: 0.5 for k in k1[:30]}
        later.update({("b", i): 0.5 for i in range(32)})
        h._check_errs = {"w0": later}
        h._note_check("w0", {"status": "passed", "covers_all": True})
        ok("w0" in h._inadequate(), "HM24 a covering pass without every run of the failed check does not clear it")
    safe(hm24, "HM24")

    def hm25():          # Codex r6 P1-1: a continuation from saved runs keeps the unresolved failed checks
        P, sim, *_ = _toy()
        real = UnitEmulator.predict

        def over(self, u, S, T, noise=True):
            m, v = real(self, u, S, T, noise)
            return m, v / 400.0
        UnitEmulator.predict = over
        try:
            r1 = HistoryMatch(P, sim, 9, q, **fast).run(1)
        finally:
            UnitEmulator.predict = real
        carried = r1["runs"].get("unresolved_checks") or {}
        tagged = sum(1 for x in r1["runs"]["unit"] if x.get("failed_checks"))
        h = HistoryMatch(P, sim, 9, q, prior=r1["runs"], **fast)
        h._reset_checks()
        h._restore_checks()
        same = {ob: [len(c) for c in cs] for ob, cs in h._failed.items() if cs}
        ok(r1["blocked"] and carried and tagged and same == carried,
           f"HM25a the saved runs carry each unresolved failed check, restored whole ({carried} vs {same})")
        first = next(j for j, y in enumerate(r1["runs"]["unit"]) if y.get("failed_checks"))
        cut = dict(r1["runs"], unit=[x for i, x in enumerate(r1["runs"]["unit"]) if i != first])
        r3 = HistoryMatch(P, sim, 9, q, prior=cut, **fast).run(0)
        ok(any(b["code"] == "emulator_inadequate" for b in r3["blocks"]),
           "HM25b a continuation missing a run of an unresolved failed check stays blocked")
    safe(hm25, "HM25")

    def hm21():          # 2026-11-06: a top-up wave simulates only the units with too few earlier runs
        P, sim, *_ = _toy()
        r1 = calibrate(P, sim, seed=3, waves=1, log=q, **fast)
        keep = [x for x in r1["runs"]["unit"] if x["unit"] not in ("u0", "u1")]       # their inputs changed
        seen = []

        def counted(pts):
            seen.extend(p.get("unit") for p in pts if p["kind"] == "unit")
            return sim(pts)
        h = HistoryMatch(P, counted, 3, q, prior={"unit": keep, "shared": []}, top_up=8, **fast)
        r2 = h.run(1)
        wave_units = set(seen[:len(seen) - sum(v["runs"] for v in r2["verification"]["units"].values())])
        ok(wave_units == {"u0", "u1"} and r2["n_units"] == len(P.units),
           f"HM21 a top-up wave simulates only the units below the run count ({sorted(wave_units)})")
    safe(hm21, "HM21")

    # F6 S2-3: shared offsets per (source, group), group-weighted best point
    def gtoy(per, f0, f1, obs, sd=0.03, md=0.02, sd_b=0.0, key=None, shared=None, zr=(-2.0, 2.0)):
        """Units in groups (per: {group: n}); unit targets w0, w1 with logit means f0(u, z, th), f1(u, z, th);
        obs(u, g) gives the two observed logits; key(t, g) the bias block of a target (None: independent)."""
        units, grp, tg = [], {}, []
        for g, n in per.items():
            for i in range(n):
                u = f"{g}u{i}"
                units.append(u)
                grp[u] = g
        for u in units:
            for ob, y in zip(("w0", "w1"), obs(u, grp[u])):
                kw = {"bias_key": key(ob, grp[u])} if key else {}
                tg.append(Target(f"{u}:{ob}", "unit", ob, _expit(y), "logit", sd_obs=sd, sd_bias=sd_b, sd_md=md,
                                 unit=u, source="toy", **kw))
        P = Problem(shared or [Param("e", 0.0, 1.2, "uniform", None, "toy")], {"name": "z", "lo": zr[0], "hi": zr[1]},
                    units, tg, features={u: [float(units.index(u) % 2)] for u in units}, groups=grp)

        def sim(pts):
            return [{ob: _expit(fn(p["unit"], p["z"], p["theta"]) + np.random.default_rng(p["seed"]).normal(0, 0.02))
                     for ob, fn in (("w0", f0), ("w1", f1))} for p in pts]
        return P, sim

    def hm26():          # a shared survey offset per group: independent errors give the wrong compatible region
        off = {"g0": (0.35, -0.3), "g1": (-0.3, 0.35), "g2": (0.2, 0.4), "g3": (-0.4, 0.1)}
        tr_ = lambda u: float(int(u[-1]) % 2)  # noqa: E731  -- treated units alternate within each group
        f0 = lambda u, z, th: z  # noqa: E731
        f1 = lambda u, z, th: z - 3.0 * th["e"] * tr_(u)  # noqa: E731
        obs = lambda u, g: (0.2 + off[g][0], 0.2 - 1.8 * tr_(u) + off[g][1])  # noqa: E731  -- truth e = 0.6
        per = {g: 6 for g in off}
        Ps, ss = gtoy(per, f0, f1, obs, sd_b=0.3, key=lambda ob, g: f"{ob}|{g}")
        Pi, si = gtoy(per, f0, f1, obs, sd_b=0.3)
        hs = HistoryMatch(Ps, ss, 11, q, candidates_log2=9, points_per_unit=10)
        rs = hs.run(2)
        hi_ = HistoryMatch(Pi, si, 11, q, candidates_log2=9, points_per_unit=10)
        ri = hi_.run(2)
        ok(rs["status"] == "matched" and hs.compatible({"e": 0.6}) and not hs.compatible({"e": 0.9}),
           f"HM26a one offset per (source, group): the truth is compatible, a value the within-group contrast rules "
           f"out is not ({rs['status']}, {rs['blocks']}, region {rs['region']['ranges']})")
        ok(hi_.compatible({"e": 0.9}) and ri["region"]["contraction"]["e"] + 0.25 < rs["region"]["contraction"]["e"],
           f"HM26b the same bias sd added to each target independently keeps the wrong value and a wider region "
           f"({ri['region']['ranges']} vs {rs['region']['ranges']})")
        b = rs.get("bias") or {}
        ok(set(b) == {f"{ob}|{g}" for ob in ("w0", "w1") for g in off}
           and all(abs(v["z"]) <= rs["cutoff"] + 1e-9 and v["sd"] == 0.3 for v in b.values()),
           f"HM26c the report gives each block's offset at the best point, inside its prior ({b})")
    safe(hm26, "HM26")

    def hm27():          # the best point weights each group once, not each of its units
        f0 = lambda u, z, th: z  # noqa: E731
        f1 = lambda u, z, th: z + th["e"]  # noqa: E731
        obs = lambda u, g: (0.1, 0.1 + (0.3 if g == "A" else 0.7))  # noqa: E731
        P, sim = gtoy({"A": 10, "B": 2}, f0, f1, obs, sd=0.15, md=0.0, zr=(-1.0, 1.5),
                      shared=[Param("e", 0.0, 1.0, "uniform", None, "toy")])
        r = calibrate(P, sim, seed=5, waves=2, log=q, candidates_log2=9, points_per_unit=20,
                      best_point_weighting="per_group")
        e = r["best"]["theta"]["e"]
        ok(r["status"] == "matched" and abs(e - 0.5) < 0.07,
           f"HM27a per_group: a 10-unit group and a 2-unit group that prefer 0.3 and 0.7: the best point sits between "
           f"them (0.5), not at the large group's 0.37 ({e:.3f}, {r['status']})")
        r = calibrate(P, sim, seed=5, waves=2, log=q, candidates_log2=9, points_per_unit=20)
        e = r["best"]["theta"]["e"]
        ok(r["weighting"]["best_point"] == "per_unit" and abs(e - 0.367) < 0.07,
           f"HM27b Codex r1 P2-6: without the option the best point keeps the per-unit rule ({e:.3f})")
    safe(hm27, "HM27")

    def hm29():          # refusals of the new declarations
        f = lambda u, z, th: z  # noqa: E731
        obs = lambda u, g: (0.0, 0.0)  # noqa: E731
        cases = []

        def c1(P):           # a bias block without a bias sd
            for t in P.targets:
                t.bias_key, t.sd_bias = f"{t.observable}|x", 0.0
        cases.append(("a bias block with no bias sd", c1))

        def c2(P):           # two sds in one block
            for n, t in enumerate(P.targets):
                t.bias_key, t.sd_bias = "one", 0.1 + 0.1 * (n % 2)
        cases.append(("one block with two bias sds", c2))

        def c3(P):
            P.targets.append(Target("gg", "group", "w0", 0.5, "logit", sd_obs=0.1, sd_bias=0.1,
                                    weights={P.units[0]: 1.0}, source="t", bias_key="k"))
        cases.append(("a bias block on a group target", c3))

        def c6(P):
            u = P.units[0]
            for n, t in enumerate(P.unit_targets(u)):
                t.bias_key, t.sd_bias = f"k{n}", 0.1
            P.targets += [Target(f"{u}:x{n}", "unit", "w0", 0.5, "logit", sd_obs=0.1, sd_bias=0.1, unit=u, source="t",
                                 bias_key=f"x{n}") for n in range(3)]
        cases.append(("more bias blocks linked by units than bias_blocks_max", c6))
        for why, mut in cases:
            P, sim = gtoy({"g0": 3}, f, f, obs)
            mut(P)
            try:
                HistoryMatch(P, sim, log=q, points_per_unit=6)
                ok(False, f"HM29 refused: {why}")
            except (CalibrationInputError, TypeError, AttributeError) as e:
                ok(isinstance(e, CalibrationInputError), f"HM29 refused: {why}")
    safe(hm29, "HM29")

    def hm30():          # one block per unit (one survey shared by the group, the other the unit's own error)
        f0 = lambda u, z, th: z  # noqa: E731
        f1 = lambda u, z, th: z + th["e"]  # noqa: E731
        obs = lambda u, g: (0.1 + (0.4 if g == "g0" else -0.3), 0.7)  # noqa: E731
        P, sim = gtoy({"g0": 5, "g1": 5}, f0, f1, obs, sd_b=0.3, key=lambda ob, g: f"{ob}|{g}" if ob == "w0" else None)
        r = calibrate(P, sim, seed=2, waves=1, log=q, candidates_log2=8, points_per_unit=20)
        b = r.get("bias") or {}
        ok(r["status"] == "matched" and set(b) == {"w0|g0", "w0|g1"} and all(abs(v["z"]) <= 3 for v in b.values())
           and all(t.bias_key is None for t in P.targets if t.observable == "w1"),
           f"HM30 one block per unit: matched, one offset per group inside its prior ({r['status']}, {b})")
    safe(hm30, "HM30")

    def one_group(ys, sd=0.01, sd_b=0.2, zr=(0.0, 1.0), slope=1.0, extra=()):
        units = [f"u{i}" for i in range(len(ys))]
        tg = [Target(f"{u}:x", "unit", "x", y, "identity", sd_obs=sd, sd_bias=sd_b, unit=u, source="toy",
                     bias_key="s|g") for u, y in zip(units, ys)] + list(extra)
        P = Problem([], {"name": "z", "lo": zr[0], "hi": zr[1]}, units, tg, groups={u: "g" for u in units})

        def sim(pts):
            return [{"x": slope * p["z"] + 1e-4 * np.random.default_rng(p["seed"]).normal()} for p in pts]
        return P, sim

    def hm31():          # Codex r1 P1: the region sample draws the group's offset, then each unit given it
        P, sim = one_group([0.5] * 4)
        r = calibrate(P, sim, seed=3, waves=1, log=q, points_per_unit=20)
        R = np.asarray(r["sample"]["rows"]) if r["sample"] else np.full((2, 4), 0.5)
        ok(r["status"] == "matched" and R[:, 0].min() < 0.2 and R[:, 0].max() > 0.8,
           f"HM31a every unit value the offset's prior allows is drawn (support [{R[:, 0].min():.2f}, "
           f"{R[:, 0].max():.2f}], every z in [0, 1] is compatible)")
        ratio = float(np.var(R.mean(1)) / max(np.mean(np.var(R, 0)), 1e-12))
        ok(ratio > 0.8, f"HM31b the units move together with their shared offset: the variance of the group mean is "
                        f"close to a unit's (ratio {ratio:.2f}; independent draws would give 0.25)")
    safe(hm31, "HM31")

    def hm32():          # Codex r1 P2-2: a group target is checked while the offset is profiled
        agg = Target("agg", "group", "x", 0.7, "identity", sd_obs=0.03, weights={f"u{i}": 1.0 for i in range(3)},
                     source="toy")
        P, sim = one_group([0.5] * 3, zr=(0.0, 1.5), extra=[agg])
        r = calibrate(P, sim, seed=4, waves=1, log=q, points_per_unit=20)
        z = list((r.get("best") or {}).get("z", {}).values())
        ok(r["status"] == "matched" and z and all(abs(x - 0.7) < 0.08 for x in z),
           f"HM32 a unit survey at 0.5 and an aggregate at 0.7: an offset near -0.2 fits both, and the aggregate is "
           f"checked before an offset is chosen ({r['status']}, {r['blocks']}, z {np.round(z, 3)})")
    safe(hm32, "HM32")

    def hm33():          # Codex r1 P2-3: no common offset exists; grid slack must not certify one
        P, sim = one_group([0.14, -0.14], sd=0.001, sd_b=1.0, zr=(-1.0, 1.0), slope=0.001)
        r = calibrate(P, sim, seed=5, waves=1, log=q, points_per_unit=20)
        ver = (r.get("verification") or {}).get("units") or {}
        ok(r["blocked"] and set(r["conflicting"] + r["unmatched"]) & {"u0", "u1"},
           f"HM33a two units whose offsets must be 0.14 and -0.14 conflict ({r['status']}, {r['conflicting']})")
        ok(ver and not all(v["ok"] for v in ver.values()),
           f"HM33b the engine verification does not certify a common offset either ({ver})")
    safe(hm33, "HM33")

    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    print(__doc__)
