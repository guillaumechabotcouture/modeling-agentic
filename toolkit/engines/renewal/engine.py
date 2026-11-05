#!/usr/bin/env python3
"""Renewal engine behind the Engine contract: a discrete generation-interval transmission model (Fraser 2007;
Cori et al. 2013) with susceptible depletion, for one closed population.

    python3 toolkit/engines/renewal/engine.py --self-test
    python3 toolkit/engines/renewal/engine.py --card [path]   # writes card.yaml (after the pack is run)

Our own Python code (numpy); no external simulator. One step has the length of the generation interval's grid
(`dt`: one week or one day). With infections I_t as shares of the population N, susceptibles S_t, the declared
generation interval w_1..w_L (lags of 1..L steps, summing to 1) and a transmissibility profile c(t):

    lambda_t = R0 c(t) sum_s w_s I_{t-s},     I_t = S_{t-1} [1 - exp(-lambda_t)],     S_t = S_{t-1} - I_t

This is the bounded exponential-survival form of the renewal equation: it never infects more than the remaining
susceptibles, and for constant c it satisfies the final-size relation log(S_inf / S_0) = -R0 (seed + sum I) exactly
at any step length. In the growth phase it is the linear renewal equation I_t = R sum_s w_s I_{t-s}, so the growth
rate r and R obey R = 1 / sum_s w_s exp(-r s dt) (Wallinga & Lipsitch 2007). The instantaneous reproduction number
is R_t = R0 c(t) S_{t-1}.

The transmissibility profile is c(t) = m_k(t) x prod_c (1 - effect_c [component c active at t]):
  transmissibility  constant (c = 1) | {knots: [t_1, ...]}: pieces [start, t_1), [t_1, t_2), ...; piece 0 is the
                    reference (m_0 = 1, so R0 is the reproduction number of the first piece) and each later piece has
                    a calibrated multiplier `transmissibility:<k>` (time-varying Rt)
  components        {name: {start, stop}}: an intervention multiplying Rt by (1 - effect:<name>) over [start, stop)
                    (decimal years) when the package has it; `effect:<name>` is a scenario parameter

Configuration (constructor keywords; anything else is refused): generation_interval ([w_1..w_L] or {dt, w}),
dt (week | day), transmissibility, components, results_path.

Unit attributes: N (population, > 0), s0 (susceptible share at the start, before the seed, in [0, 1]; default 1),
seed (infections in the step before the horizon starts, as a share of N; default 1e-6). The seed's infections
come out of s0.

Outputs. Per capita: infections over the decision window. Window metrics, for each named window [a, b) (the steps
whose start lies in it): attack_rate (infections in the window / N: new infections only), s_start, s_end,
reff_start (R0 c S at the window's first step), reff_end (R0 c(t_end) s_end, c of the window's last step),
reff_ref (R0 m_0 s_end: the window's end with the reference transmissibility, i.e. "the same transmissibility as at
the start", no component), growth_rate (least-squares slope of log incidence per year), peak_time, r0. Meta: the
incidence and R_t series. Hooks (toolkit/engines/hooks.py): schedule() for the components, run_series() for monthly
infections, R_t and the susceptible share. `cori_rt()` is the Cori et al. (2013) estimator of R_t from a case
series: the time-varying estimate the pack checks against known R_t, and a reporting-invariant diagnostic.

The observation model (reporting fraction, reporting delay, overdispersion) is not part of the engine: each data
source has its own measurement model in the harness (toolkit/analysis/observation.py, toolkit/analysis/estimates.py),
and no source is taken as the truth. Nothing in this file names a disease.
"""
from __future__ import annotations

import json
import math
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from toolkit.engines.contract import (EngineCard, Horizon, Outcomes, Package, ParamSpec, UnitSpec,  # noqa: E402
                                      check_engine, resolve_params)
from toolkit.engines.hooks import HOOKS_CONTRACT, monthly_grid  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PACK = os.path.join(HERE, "benchmarks", "pack.yaml")
RESULTS = os.path.join(HERE, "benchmarks", "results.yaml")
NAME, VERSION = "renewal", "0.1"
DT_YEARS = {"week": 7.0 / 365.25, "day": 1.0 / 365.25}
DEFAULTS = {"s0": 1.0, "seed": 1.0e-6}
R0_RANGE = (0.05, 40.0)
MULT_RANGE = (0.02, 20.0)
PUBLISHED_LABEL = ("validated against analytic identities (final size, Wallinga-Lipsitch, reproduction-number "
                   "identities, scale invariance) and known-R_t recovery; published benchmark RW9 (Cori et al. 2013 "
                   "worked examples) not yet assessed")
WINDOW_METRICS = ("attack_rate", "s_start", "s_end", "reff_start", "reff_end", "reff_ref", "growth_rate",
                  "peak_time", "r0")
SERIES = {"infections": {"label": "new infections", "units": "infections per person-year", "aggregate": "rate",
                         "denominator": "person-year"},
          "reff": {"label": "instantaneous reproduction number R_t", "units": "infections per infection",
                   "aggregate": "mean", "denominator": "person"},
          "susceptible": {"label": "susceptible share", "units": "share", "aggregate": "mean",
                          "denominator": "person"}}


def _num(x, what) -> float:
    if isinstance(x, bool) or not isinstance(x, (int, float, np.floating, np.integer)) or not math.isfinite(float(x)):
        raise ValueError(f"{what}: {x!r} is not a finite number")
    return float(x)


def check_generation_interval(gi, dt: str) -> np.ndarray:
    """w_1..w_L on the dt grid: finite, non-negative, summing to 1; a gap, a negative value or another grid fails
    closed."""
    if isinstance(gi, dict):
        if gi.get("dt") != dt:
            raise ValueError(f"generation interval on the {gi.get('dt')!r} grid, engine dt is {dt!r}")
        gi = gi.get("w")
    if not isinstance(gi, (list, tuple)) or not gi:
        raise ValueError("generation interval: give a non-empty list w_1..w_L (lags of 1..L steps)")
    w = []
    for i, x in enumerate(gi):
        if x is None or isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(float(x)):
            raise ValueError(f"generation interval: gap or non-number at lag {i + 1} ({x!r})")
        if float(x) < 0:
            raise ValueError(f"generation interval: negative value at lag {i + 1} ({x})")
        w.append(float(x))
    if abs(sum(w) - 1.0) > 1e-9:
        raise ValueError(f"generation interval sums to {sum(w)!r}, not 1")
    return np.asarray(w, float)


def simulate(r0, cmat, s0, seed, w):
    """The renewal recursion for a batch: r0 (B,), cmat (B, n) transmissibility per step, s0 (B,), seed (B,) shares,
    w (L,). Returns (incidence (B, n), S after each step (B, n), S before the first step (B,), R_t (B, n))."""
    r0 = np.asarray(r0, float)
    cmat = np.asarray(cmat, float)
    B, n = cmat.shape
    L = len(w)
    hist = np.zeros((B, L + n))
    seed = np.asarray(seed, float)
    hist[:, L - 1] = seed
    S = np.asarray(s0, float) - seed
    S0 = S.copy()
    inc = np.zeros((B, n))
    Sp = np.zeros((B, n))
    rt = np.zeros((B, n))
    wr = w[::-1]                                   # hist[:, L+t-L : L+t] . w reversed = sum_s w_s I_{t-s}
    for t in range(n):
        lam = r0 * cmat[:, t] * (hist[:, t:L + t] @ wr)
        rt[:, t] = r0 * cmat[:, t] * S
        new = S * (-np.expm1(-lam))
        S = S - new
        hist[:, L + t] = new
        inc[:, t] = new
        Sp[:, t] = S
    return inc, Sp, S0, rt


def cori_rt(cases, w, tau: int = 3, a: float = 1.0, b: float = 5.0, level: float = 0.95, start: int | None = None):
    """Cori et al. (2013) R_t over sliding windows of `tau` steps ending at each step t: the gamma posterior
    shape a + sum I_s, rate 1/b + sum Lambda_s with Lambda_s = sum_k w_k I_{s-k} (prior mean a b). Returns
    {t, mean, lo, hi, cv} for t from `start` (default: the first step with a full window after the generation
    interval) on. A constant reporting fraction cancels: the estimate is the same on reported cases."""
    from scipy import stats
    y = np.asarray(cases, float)
    w = np.asarray(w, float)
    L = len(w)
    lam = np.array([sum(w[k - 1] * y[s - k] for k in range(1, L + 1) if s - k >= 0) for s in range(len(y))])
    t0 = L + tau - 1 if start is None else int(start)      # every Lambda in the window has its full history
    out = {"t": [], "mean": [], "lo": [], "hi": [], "cv": []}
    q = (1 - level) / 2
    for t in range(t0, len(y)):
        sl = slice(t - tau + 1, t + 1)
        shape = a + y[sl].sum()
        rate = 1.0 / b + lam[sl].sum()
        out["t"].append(t)
        out["mean"].append(shape / rate)
        out["lo"].append(float(stats.gamma.ppf(q, shape, scale=1 / rate)))
        out["hi"].append(float(stats.gamma.ppf(1 - q, shape, scale=1 / rate)))
        out["cv"].append(1 / math.sqrt(shape))
    return out


class RenewalEngine:
    """The renewal engine (see the module docstring)."""

    hooks_contract = HOOKS_CONTRACT
    validation_label = PUBLISHED_LABEL      # rendered in every report from this engine (toolkit/analysis/estimates.py)
    mode = "windowed"
    groups = ["all"]

    def __init__(self, generation_interval=None, dt: str = "week", transmissibility="constant", components=None,
                 results_path: str | None = RESULTS):
        if dt not in DT_YEARS:
            raise ValueError(f"dt: one of {sorted(DT_YEARS)}")
        self.dt = dt
        self.dt_years = DT_YEARS[dt]
        self.dt_days = self.dt_years * 365.25
        if generation_interval is None:
            raise ValueError("generation_interval: required (the question's distribution on the dt grid)")
        self.w = check_generation_interval(generation_interval, dt)
        self._gi = generation_interval
        if transmissibility == "constant":
            self.knots = []
        elif isinstance(transmissibility, dict) and set(transmissibility) == {"knots"}:
            k = [_num(x, "transmissibility.knots") for x in (transmissibility["knots"] or [])]
            if not k or any(b <= a for a, b in zip(k, k[1:])):
                raise ValueError("transmissibility.knots: a non-empty, strictly increasing list of decimal years")
            self.knots = k
        else:
            raise ValueError("transmissibility: constant | {knots: [t_1, ...]}")
        self._tr = transmissibility
        self.components = {}
        for c, d in (components or {}).items():
            if not isinstance(d, dict) or set(d) != {"start", "stop"}:
                raise ValueError(f"components.{c}: {{start, stop}} (decimal years)")
            a, b = _num(d["start"], f"components.{c}.start"), _num(d["stop"], f"components.{c}.stop")
            if not a < b:
                raise ValueError(f"components.{c}: start < stop")
            self.components[str(c)] = (a, b)
        self.results_path = results_path

    # ------------------------------------------------------------------ card
    def config(self) -> dict:
        return {"generation_interval": {"dt": self.dt, "w": [float(x) for x in self.w]}, "dt": self.dt,
                "transmissibility": self._tr if self.knots else "constant",
                "components": {c: {"start": a, "stop": b} for c, (a, b) in self.components.items()}}

    def describe(self) -> EngineCard:
        bench = []
        if self.results_path and os.path.exists(self.results_path):
            import yaml
            bench = (yaml.safe_load(open(self.results_path)) or {}).get("benchmarks", [])
        mean_gi = float(np.dot(np.arange(1, len(self.w) + 1), self.w))
        return EngineCard(
            name=NAME, version=VERSION,
            mechanisms=[
                "one closed population; no births, deaths, waning immunity or migration within the horizon",
                "discrete renewal equation on the generation interval's grid (Fraser 2007; Cori et al. 2013) with "
                "susceptible depletion in the exponential-survival form (final-size relation exact at any step)",
                f"generation interval declared per question: {len(self.w)} lags of one {self.dt}, mean "
                f"{mean_gi:.3g} {self.dt}s",
                "R_t = R0 c(t) S_t-1: transmissibility piecewise constant between declared knots "
                + (f"({len(self.knots)} knots)" if self.knots else "(constant)"),
                "interventions as multipliers (1 - effect) on R_t over a declared period",
                "deterministic: no random numbers; the seed is recorded only"],
            components={c: [0, 1] for c in self.components} or {"none": [0]},
            unit_attributes={"N": "population (> 0)",
                             "s0": "susceptible share at the start, before the seed [0, 1] (default 1)",
                             "seed": "infections in the step before the start, as a share of N [0, 0.1] "
                                     "(default 1e-6)"},
            outputs={"per_capita": ["infections"], "windows": list(WINDOW_METRICS),
                     "cost_drivers": [f"person_years:{c}" for c in self.components], "series": dict(SERIES)},
            age_groups=["all"],
            valid_ranges={"s0": [0.0, 1.0], "seed": [0.0, 0.1], "r0": list(R0_RANGE)},
            benchmarks=bench,
            notes=["the observation model (reporting fraction, delay, overdispersion) belongs to each data source "
                   "and is the harness's (toolkit/analysis/observation.py), never an engine parameter",
                   "out of range: births, deaths, waning, migration, vector dynamics (absorbed into R0 and c(t)), "
                   "stochastic fade-out, spatial structure",
                   PUBLISHED_LABEL,
                   "configuration: " + json.dumps(self.config(), sort_keys=True)])

    def parameters(self) -> list:
        ps = [ParamSpec("r0", 2.0, "calibrated", units="secondary infections per infection in a fully susceptible "
                        "population, reference transmissibility", lo=R0_RANGE[0], hi=R0_RANGE[1],
                        source="calibrated per run; its prior is declared in the signed spec (calibration.priors), "
                               "published ranges in toolkit/engines/renewal/ranges.yaml")]
        for k in range(1, len(self.knots) + 1):
            ps.append(ParamSpec(f"transmissibility:{k}", 1.0, "calibrated", units="multiplier on R0 (piece "
                                f"{k} against piece 0)", lo=MULT_RANGE[0], hi=MULT_RANGE[1],
                                source="calibrated per run; prior declared in the signed spec"))
        for c in self.components:
            ps.append(ParamSpec(f"effect:{c}", 0.0, "scenario", units="proportional reduction of R_t while active",
                                lo=0.0, hi=1.0, source="scenario: declared per question in the signed spec"))
        return ps

    # ------------------------------------------------------------------ batch inputs
    def n_steps(self, horizon) -> int:
        n = int(math.floor((float(horizon.stop) - float(horizon.start)) / self.dt_years + 1e-9))
        if n < 1:
            raise ValueError(f"horizon [{horizon.start}, {horizon.stop}) is shorter than one {self.dt}")
        return n

    def step_starts(self, horizon) -> np.ndarray:
        return float(horizon.start) + self.dt_years * np.arange(self.n_steps(horizon))

    def _unit(self, unit: UnitSpec) -> tuple:
        a = dict(unit.attributes)
        bad = [k for k in a if k not in ("N", "s0", "seed")]
        if bad:
            raise ValueError(f"unit attributes {bad} are not declared by engine {NAME}")
        if "N" not in a:
            raise ValueError("unit attribute N (population) is required")
        N = a["N"]
        N = N[0] if isinstance(N, (list, tuple)) and len(N) == 1 else N
        N = _num(N, "N")
        s0 = a.get("s0", DEFAULTS["s0"])
        s0 = _num(s0[0] if isinstance(s0, (list, tuple)) and len(s0) == 1 else s0, "s0")
        seed = _num(a.get("seed", DEFAULTS["seed"]), "seed")
        if not N > 0:
            raise ValueError(f"N = {N}: must be > 0")
        if not 0.0 <= s0 <= 1.0:
            raise ValueError(f"s0 = {s0} outside [0, 1]")
        if not 0.0 <= seed <= min(0.1, s0):
            raise ValueError(f"seed = {seed} outside [0, min(0.1, s0)]")
        return N, s0, seed

    def _profile(self, p: dict, package: Package, t: np.ndarray, with_components: bool = True) -> np.ndarray:
        c = np.ones(len(t))
        for k, kt in enumerate(self.knots, start=1):
            c[t >= kt] = p[f"transmissibility:{k}"]
        if with_components:
            for name, v in (package.components or {}).items():
                if name == "none":
                    continue
                if name not in self.components:
                    raise ValueError(f"package component {name!r} is not an engine component")
                if int(v):
                    a, b = self.components[name]
                    c[(t >= a) & (t < b)] *= 1.0 - p[f"effect:{name}"]
        return c

    def _check_params(self, p):
        if not R0_RANGE[0] <= p["r0"] <= R0_RANGE[1]:
            raise ValueError(f"r0 = {p['r0']} outside the valid range {list(R0_RANGE)}")
        for k in range(1, len(self.knots) + 1):
            if not MULT_RANGE[0] <= p[f"transmissibility:{k}"] <= MULT_RANGE[1]:
                raise ValueError(f"transmissibility:{k} outside {list(MULT_RANGE)}")
        for c in self.components:
            if not 0.0 <= p[f"effect:{c}"] <= 1.0:
                raise ValueError(f"effect:{c} outside [0, 1]")

    # ------------------------------------------------------------------ runs
    def run(self, params, unit, package, horizon, seed) -> Outcomes:
        return self.run_batch([params], [unit], [package], horizon, seed)[0]

    def run_batch(self, param_sets, units, packages, horizon, seed) -> list:
        """One vectorised recursion for many (parameters, unit, package) members; equals run() member by member."""
        n_m = len(param_sets)
        units = units if isinstance(units, list) else [units] * n_m
        packages = packages if isinstance(packages, list) else [packages] * n_m
        if not (len(units) == len(packages) == n_m):
            raise ValueError("run_batch: one unit and one package per parameter set")
        t = self.step_starts(horizon)
        P = [resolve_params(self, ps) for ps in param_sets]
        for p in P:
            self._check_params(p)
        Us = [self._unit(u) for u in units]
        cmat = np.stack([self._profile(p, pk, t) for p, pk in zip(P, packages)])
        cref = np.stack([self._profile(p, pk, t, with_components=False) for p, pk in zip(P, packages)])
        r0 = np.array([p["r0"] for p in P])
        s0 = np.array([u[1] for u in Us])
        sd = np.array([u[2] for u in Us])
        inc, Sp, S0, rt = simulate(r0, cmat, s0, sd, self.w)
        out = []
        for i in range(n_m):
            out.append(self._outcomes(i, t, inc[i], Sp[i], S0[i], rt[i], r0[i], cmat[i], cref[i], packages[i],
                                      horizon, Us[i], int(seed)))
        return out

    def _window(self, t, a, b):
        idx = np.nonzero((t >= float(a)) & (t < float(b)))[0]
        return idx

    def _metrics(self, idx, t, inc, Sp, S0, r0, cmat, cref) -> dict:
        if len(idx) == 0:
            raise ValueError("a window contains no step of the horizon")
        i0, i1 = int(idx[0]), int(idx[-1])
        s_start = float(S0 if i0 == 0 else Sp[i0 - 1])
        s_end = float(Sp[i1])
        y = inc[idx]
        pos = y > 0
        if pos.sum() >= 2:
            tt = t[idx][pos]
            gr = float(np.polyfit(tt - tt[0], np.log(y[pos]), 1)[0])
        else:
            gr = 0.0
        return {"attack_rate": float(y.sum()), "s_start": s_start, "s_end": s_end,
                "reff_start": float(r0 * cmat[i0] * s_start), "reff_end": float(r0 * cmat[i1] * s_end),
                "reff_ref": float(r0 * cref[0] * s_end), "growth_rate": gr,
                "peak_time": float(t[idx][int(np.argmax(y))]), "r0": float(r0)}

    def _outcomes(self, i, t, inc, Sp, S0, rt, r0, cmat, cref, package, horizon, u, seed) -> Outcomes:
        widx = self._window(t, *horizon.window)
        if len(widx) == 0:
            raise ValueError("the decision window contains no step of the horizon")
        wins = {name: self._metrics(self._window(t, *w), t, inc, Sp, S0, r0, cmat, cref)
                for name, w in (horizon.windows or {}).items()}
        cost = {}
        w0, w1 = float(horizon.window[0]), float(horizon.window[1])
        for c, (a, b) in self.components.items():
            on = int((package.components or {}).get(c, 0))
            cost[f"person_years:{c}"] = float(on * max(0.0, min(b, w1) - max(a, w0)))
        meta = {"incidence": [float(x) for x in inc], "rt": [float(x) for x in rt], "s": [float(x) for x in Sp],
                "t": [float(x) for x in t], "s_start": [float(S0 + u[2])], "N": u[0], "dt_years": self.dt_years,
                "incidence_before": [float(u[2])],      # infections in the steps before the start, oldest first
                "seed": seed, "age_shares": {"all": 1.0}}
        return Outcomes(per_capita={"infections": {"all": float(inc[widx].sum())}}, windows=wins, cost_drivers=cost,
                        meta=meta)

    # ------------------------------------------------------------------ hooks (toolkit/engines/hooks.py)
    def schedule(self, params, unit, package, horizon) -> list:
        """One decision event per component in the package (the period over which it multiplies R_t)."""
        ev = []
        for c, (a, b) in sorted(self.components.items()):
            if int((package.components or {}).get(c, 0)):
                ev.append({"component": c, "label": "transmission reduction", "kind": "decision", "t0": a, "t1": b,
                           "charged": True})
        return ev

    def run_series(self, params, unit, packages, horizon, seed, metrics, step="month") -> list:
        """Monthly infections (per person-year), mean R_t and mean susceptible share, from the step series by
        overlap: each step [t_k, t_k + dt) spreads its value over the months it overlaps."""
        grid = monthly_grid(horizon)
        end = float(horizon.window[1])
        res = []
        for pk, o in zip(packages, self.run_batch([params] * len(packages), [unit] * len(packages), list(packages),
                                                   horizon, seed)):
            t = np.asarray(o.meta["t"])
            vals = {"infections": np.asarray(o.meta["incidence"]), "reff": np.asarray(o.meta["rt"]),
                    "susceptible": np.asarray(o.meta["s"])}
            out = {}
            for m in metrics:
                v = vals[m]
                row = []
                for k, g0 in enumerate(grid):
                    g1 = min(g0 + 1 / 12, end) if k == len(grid) - 1 else g0 + 1 / 12
                    ov = np.clip(np.minimum(t + self.dt_years, g1) - np.maximum(t, g0), 0.0, None)
                    if SERIES[m]["aggregate"] == "rate":
                        row.append(float(np.sum(v * ov / self.dt_years)) / (g1 - g0))
                    else:
                        row.append(float(np.sum(v * ov) / np.sum(ov)) if np.sum(ov) > 0 else float(v[-1]))
                out[m] = row
            res.append({"package": pk.id, "t": list(grid), "values": out})
        return res


# ------------------------------------------------------------------------------------- card
def write_card(path: str) -> None:
    """card.yaml: configuration vocabulary, capabilities, parameters and the benchmark results it was validated with
    (sha256 of results.yaml)."""
    import hashlib
    import yaml
    from toolkit.engines import benchmark as B
    pack = yaml.safe_load(open(PACK))
    eng = B.load_engine(pack["engine"], dict(pack["configs"][pack["default_config"]], results_path=RESULTS))
    card = eng.describe().to_dict()
    card["configured_as"] = pack["default_config"]
    card["configuration_vocabulary"] = {"dt": list(DT_YEARS), "transmissibility": ["constant", "{knots: [...]}"],
                                        "components": "{name: {start, stop}}"}
    card["capabilities"] = {"base": {"dynamics": ["closed_epidemic_renewal"], "structure": ["single_group"],
                                     "outcome_role": {"infections": "prevented"},
                                     "intervention_action": ["reduce_rate"],
                                     "estimands": ["r0", "reff_start", "reff_end", "reff_ref", "attack_rate",
                                                   "growth_rate", "incidence_series"],
                                     "calibration_data": ["incidence_series"]}}
    card["parameters"] = [vars(p) for p in eng.parameters()]
    card["published_ranges"] = yaml.safe_load(open(os.path.join(HERE, "ranges.yaml")))   # sourced, with spread
    card["validation_label"] = PUBLISHED_LABEL
    if os.path.exists(RESULTS):
        card["results_sha256"] = hashlib.sha256(open(RESULTS, "rb").read()).hexdigest()
    card["benchmark_pack"] = "benchmarks/pack.yaml (oracles: benchmarks/oracles.py, test only)"
    yaml.safe_dump(card, open(path, "w"), sort_keys=False, width=110)


FAST_PACK_IDS = ("RW1a", "RW2a", "RW3a", "RW5a")


def _self_test() -> int:
    from toolkit.engines.hooks import run_series as hook_series, schedule as hook_schedule
    fails = []

    def ok(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    def safe(fn):
        try:
            return fn(), None
        except Exception as e:  # noqa: BLE001 -- a test that raises is a failing test
            return None, f"{type(e).__name__}: {e}"

    w = [0.2, 0.4, 0.25, 0.15]
    e, err = safe(lambda: RenewalEngine(generation_interval=w))
    ok(e is not None, f"K0 the engine builds from a weekly generation interval: {err}")
    if e is None:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    for label, gi in (("sums to 0.9", [0.5, 0.4]), ("negative", [1.2, -0.2]), ("gap", [0.5, None, 0.5]),
                      ("another grid", {"dt": "day", "w": [1.0]})):
        _, err = safe(lambda gi=gi: RenewalEngine(generation_interval=gi))
        ok(err is not None, f"K0b a generation interval that {label} fails closed: {err}")
    _, err = safe(lambda: RenewalEngine(generation_interval=w, colour="blue"))
    ok(err is not None, "K0c an unknown configuration keyword is refused")
    h = Horizon(2025.0, 2026.0, (2025.0, 2026.0), {"y": [2025.0, 2026.0], "early": [2025.0, 2025.5],
                                                     "late": [2025.5, 2026.0]})
    u = UnitSpec("city", {"N": 1.5e6, "s0": 1.0, "seed": 2e-4})
    ok(check_engine(e, u, Package("none", {}), h) == [], f"K1 contract: {check_engine(e, u, Package('none', {}), h)}")
    ok(e.n_steps(h) == 52, f"K1b a one-year horizon on the weekly grid has 52 steps ({e.n_steps(h)})")
    # K2 run_batch equals run pair by pair; K3 identical across seeds
    ps = [{"r0": 1.4}, {"r0": 2.2}, {"r0": 0.8}]
    us = [u, UnitSpec("b", {"N": 1e5, "s0": 0.6, "seed": 1e-3}), u]
    b = e.run_batch(ps, us, [Package("none", {})] * 3, h, 1)
    one = [e.run(p, x, Package("none", {}), h, 1) for p, x in zip(ps, us)]
    ok(all(bb.windows == oo.windows and bb.per_capita == oo.per_capita for bb, oo in zip(b, one)),
       "K2 run_batch() equals run() member by member")
    ok(e.run(ps[0], u, Package("none", {}), h, 1).windows == e.run(ps[0], u, Package("none", {}), h, 99).windows,
       "K3 outputs are identical across seeds (deterministic)")
    # K4 final size (exponential-survival identity) at any step length, with s0 < 1
    from scipy.optimize import brentq
    hl = Horizon(0.0, 30.0, (0.0, 30.0), {"all": [0.0, 30.0]})
    worst = 0.0
    for r0, s0 in ((1.5, 1.0), (2.0, 1.0), (4.0, 1.0), (2.5, 0.7)):
        for dt, gi in (("week", w), ("day", {"dt": "day", "w": [0.1, 0.3, 0.3, 0.2, 0.1]})):
            ee = RenewalEngine(generation_interval=gi, dt=dt)
            o = ee.run({"r0": r0}, UnitSpec("x", {"N": 1e6, "s0": s0, "seed": 1e-9}), Package("none", {}), hl, 1)
            sst = s0 - 1e-9
            sinf = brentq(lambda x: x - sst * math.exp(-r0 * (1e-9 + sst - x)), 0.0, sst)
            worst = max(worst, abs(o.windows["all"]["s_end"] - sinf))
    ok(worst < 1e-8, f"K4 the final-size relation holds at weekly and daily steps, s0 = 1 and 0.7 (worst {worst:.2e})")
    # K5 Wallinga-Lipsitch
    hg = Horizon(0.0, 2.0, (0.0, 2.0), {"g": [0.3, 0.6]})
    worst = 0.0
    for r0 in (0.8, 1.3, 2.5):
        o = e.run({"r0": r0}, UnitSpec("x", {"N": 1.0, "s0": 1.0, "seed": 1e-12}), Package("none", {}), hg, 1)
        r = o.windows["g"]["growth_rate"] * e.dt_years                 # per step
        rw = 1.0 / sum(wk * math.exp(-r * (k + 1)) for k, wk in enumerate(w))
        worst = max(worst, abs(rw / r0 - 1))
    ok(worst < 1e-3, f"K5 R0 = 1 / sum_s w_s exp(-r s) in the growth phase, R0 0.8, 1.3, 2.5 (worst rel {worst:.1e})")
    # K6 reproduction-number identities
    o = e.run({"r0": 1.7}, u, Package("none", {}), h, 1).windows["y"]
    ok(abs(o["reff_end"] - 1.7 * o["s_end"]) < 1e-12 and o["reff_ref"] == o["reff_end"] and o["reff_end"] < 1,
       f"K6 reff_end = R0 S_end/N (unforced), reff_ref equals it, below 1 after a major epidemic ({o['reff_end']:.3f})")
    # K7 scale invariance: (R0, s0, N) and (R0 s0, 1, s0 N) give the same counts
    a1 = e.run({"r0": 3.0}, UnitSpec("x", {"N": 1e6, "s0": 0.5, "seed": 1e-4}), Package("none", {}), h, 1)
    a2 = e.run({"r0": 1.5}, UnitSpec("x", {"N": 5e5, "s0": 1.0, "seed": 2e-4}), Package("none", {}), h, 1)
    c1 = np.asarray(a1.meta["incidence"]) * 1e6
    c2 = np.asarray(a2.meta["incidence"]) * 5e5
    ok(np.max(np.abs(c1 / c2 - 1)) < 1e-10 and abs(a1.windows["y"]["reff_end"] - a2.windows["y"]["reff_end"]) < 1e-12
       and abs(a1.windows["y"]["attack_rate"] / a2.windows["y"]["attack_rate"] - 0.5) < 1e-10,
       "K7 scale invariance: with s0 unknown the counts fix R0 s0 and reff_end, not the attack rate")
    # K8 piecewise transmissibility and a component
    ek = RenewalEngine(generation_interval=w, transmissibility={"knots": [2025.5]},
                       components={"ctl": {"start": 2025.25, "stop": 2025.5}})
    tt = ek.step_starts(h)
    o = ek.run({"r0": 1.5, "transmissibility:1": 0.5, "effect:ctl": 0.4}, u, Package("c", {"ctl": 1}), h, 1)
    c = np.asarray(o.meta["rt"]) / (1.5 * np.concatenate([[1.0 - 2e-4], np.asarray(o.meta["s"])[:-1]]))
    want = np.where(tt >= 2025.5, 0.5, 1.0) * np.where((tt >= 2025.25) & (tt < 2025.5), 0.6, 1.0)
    ok(np.max(np.abs(c - want)) < 1e-12, "K8 R_t follows the piecewise multiplier and the component's (1 - effect)")
    ok(abs(o.windows["y"]["reff_ref"] - 1.5 * o.windows["y"]["s_end"]) < 1e-12,
       "K8b reff_ref uses the reference transmissibility (piece 0, no component)")
    # K9 attack_rate counts window infections only
    oy = e.run({"r0": 1.7}, u, Package("none", {}), h, 1).windows
    ok(abs(oy["late"]["attack_rate"] - (oy["y"]["attack_rate"] - oy["early"]["attack_rate"])) < 1e-12
       and abs(oy["late"]["s_start"] - oy["early"]["s_end"]) < 1e-15,
       "K9 a window's attack rate counts only infections in it")
    # K10 Cori recovers a known R_t; reporting fraction cancels
    he = Horizon(0.0, 40 * e.dt_years, (0.0, 40 * e.dt_years), {})
    eh = RenewalEngine(generation_interval=w, transmissibility={"knots": [20 * e.dt_years - 1e-9]})
    oo = eh.run({"r0": 1.6, "transmissibility:1": 0.5}, UnitSpec("x", {"N": 1e9, "s0": 1.0, "seed": 1e-6}),
                Package("none", {}), he, 1)
    inf = np.asarray(oo.meta["incidence"]) * 1e9
    rt = np.asarray(oo.meta["rt"])
    cr = cori_rt(inf, w, tau=3)
    cr10 = cori_rt(0.1 * inf, w, tau=3)
    errs = [abs(m / rt[t] - 1) for t, m in zip(cr["t"], cr["mean"]) if t >= 8 and not 19 <= t <= 22]
    inv = max(abs(x / y - 1) for x, y in zip(cr["mean"][4:], cr10["mean"][4:]))
    ok(max(errs) < 0.02 and inv < 2e-3,
       f"K10 Cori recovers the known R_t of a simulated series (worst rel {max(errs):.3f}) and, up to its gamma prior, "
       f"does not depend on a constant reporting fraction (worst rel {inv:.1e} at 1/10 reporting)")
    ok(cr["t"][0] == len(w) + 3 - 1, f"K10b (Codex r1-13) Cori's first window is the first with full generation "
                                     f"history (ends at L + tau - 1 = {len(w) + 2}; got {cr['t'][0]})")
    ob = e.run({"r0": 1.7}, u, Package("none", {}), h, 1)
    ok(ob.meta.get("incidence_before") == [2e-4], f"K10c (Codex r1-5) the seed infections before the horizon are in "
                                                 f"the outputs (meta incidence_before {ob.meta.get('incidence_before')})")
    # K11 hooks
    hd = Horizon(2025.0, 2026.0, (2025.0, 2026.0), {})
    sc = hook_schedule(ek, {"effect:ctl": 0.4}, u, Package("c", {"ctl": 1}), hd)
    ok(len(sc) == 1 and sc[0]["kind"] == "decision", f"K11a schedule(): one decision event per component ({sc})")
    sr = hook_series(e, {"r0": 1.7}, u, [Package("none", {})], hd, 1, ["infections", "reff", "susceptible"])
    tot = sum(v / 12 for v in sr[0]["values"]["infections"])
    ok(len(sr[0]["t"]) == 12 and abs(tot - oy["y"]["attack_rate"]) < 1e-9,
       f"K11b run_series(): 12 monthly steps; monthly infections integrate to the attack rate ({tot:.6f})")
    # K12 fail closed
    for label, fn in (("an unknown parameter", lambda: e.run({"zz": 1}, u, Package("none", {}), h, 1)),
                      ("s0 above 1", lambda: e.run({}, UnitSpec("x", {"N": 1e5, "s0": 1.2}), Package("none", {}), h, 1)),
                      ("no N", lambda: e.run({}, UnitSpec("x", {"s0": 1.0}), Package("none", {}), h, 1)),
                      ("r0 outside its range", lambda: e.run({"r0": 99.0}, u, Package("none", {}), h, 1))):
        _, err = safe(fn)
        ok(err is not None, f"K12 {label} fails closed: {err}")
    # K13 the committed benchmark pack: a fast subset re-run, and results stamped for this code
    import yaml
    from toolkit.engines import benchmark as B
    if os.path.exists(PACK):
        pack = yaml.safe_load(open(PACK))
        eng0 = B.load_engine(pack["engine"], pack["configs"][pack["default_config"]])
        ok(B.validate(pack, eng0) == [], f"K13a the pack validates: {B.validate(pack, eng0)[:3]}")
        sub = dict(pack, benchmarks=[x for x in pack["benchmarks"] if x["id"] in FAST_PACK_IDS])
        rows = {r["id"]: r for r in B.run_pack(sub, eng0, B.Runner(sub, eng0, 1, seeds=[1]), {})["benchmarks"]}
        ok(all(rows.get(i, {}).get("status") == "in_band" for i in FAST_PACK_IDS),
           "K13b fast pack subset in band: " + ", ".join(f"{i} {rows.get(i, {}).get('status')}" for i in FAST_PACK_IDS))
        eng = B.load_engine(pack["engine"], dict(pack["configs"][pack["default_config"]], results_path=RESULTS))
        got, why = B.verified_benchmarks(eng)
        ok(why is None and len(got) == len(pack["benchmarks"]), f"K13c results.yaml is stamped for this code: {why}")
        bad = [r["id"] for r in got if r["status"] != "in_band" and r["id"] != "RW9"]
        ok(got and not bad, f"K13d every assessable benchmark in band ({bad}); RW9 not_assessable until transcribed")
    else:
        ok(False, "K13 the benchmark pack exists")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    if sys.argv[1:2] == ["--card"]:
        write_card(sys.argv[2] if len(sys.argv) > 2 else os.path.join(HERE, "card.yaml"))
        sys.exit(0)
    print(__doc__)
