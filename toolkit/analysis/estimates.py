#!/usr/bin/env python3
"""The analysis of an `estimates` deliverable: quantities with intervals, no allocation (design note v2d §2.3).

    python3 toolkit/analysis/estimates.py --self-test

Called by toolkit/analysis/run.py when the spec's deliverable kind is `estimates` (toolkit/spec/estimates.py). Every
number comes from the spec, the engine and the run's data:

1. The posterior problem. Its parameters are the spec's `calibration.priors` (engine parameters, or unit attributes
   the spec does not bind to data) and each observation source's own parameters (bias mechanisms, a declared
   dispersion), each with its declared prior. One engine run_batch() evaluates many points. The engine's incidence
   (`meta["incidence"]`, shares of N per step) times the population is the latent incidence; each source sees it
   through its measurement model: a declared reporting delay (convolution over steps), its bias mechanisms
   (toolkit/analysis/observation.py, e.g. under_ascertainment) and its noise (negbin or poisson). No source is the
   truth: every bias parameter is a prior-bounded mechanism.
2. The canonical joint sample (toolkit/analysis/calibration.py: importance sampling or adaptive Metropolis, fail
   closed below its floors), identifiability classes, and the posterior mode.
3. Uncertainty draws: rows of the canonical sample (with provenance), the engine re-run at each row's applied values;
   each declared output is an engine window metric. uncertainty_report.yaml (scalar_outputs) is written by the
   shared writer (toolkit/analysis/uncertainty.py).
4. What the data determine: each output's interval under the prior alone against the posterior (contraction).
5. The threshold rule (yes / no / undetermined) and an automatic prior_range test for every parameter the data do not
   determine: the decision recomputed with that parameter at each end of its declared range.
6. A posterior predictive check of every target, figures, the claims ledger and results.md.

Nothing here names a disease; the engine, the spec and the data carry the specifics.
"""
from __future__ import annotations

import json
import math
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from toolkit.analysis import calibration as C  # noqa: E402
from toolkit.engines.contract import Horizon, Package, UnitSpec  # noqa: E402
from toolkit.spec.estimates import statements as _statements  # noqa: E402

PRODUCER = "toolkit/analysis/estimates.py"
EXPECTED_KINDS = ("engine", "calibration", "uncertainty", "estimates", "figures", "claims")
EXPECTED_ROLES = ("uncertainty_report", "claims_spec", "claims_ledger", "results")
PRIOR_DRAWS = 4000
PPC_DRAWS = 400


def thresholds() -> dict:
    """thresholds.yaml `estimates`: dependence_max (an output that moves by more than this share of its median across
    an undetermined parameter's posterior range is set by that parameter's declared range) and end_loglik_drop_max
    (an end of a declared range whose best fit is worse by more than this log-likelihood is rejected by the data)."""
    import yaml
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "thresholds.yaml")) as f:
        return {k: (dict(v) if isinstance(v, dict) else float(v)) for k, v in yaml.safe_load(f)["estimates"].items()}


def _csv(path):
    import csv
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


class EstimatesProblem:
    """The posterior Problem of an estimates spec on its engine (see the module docstring)."""

    def __init__(self, spec, eng, fixed: dict | None = None):
        from toolkit.analysis import observation as ob
        self.spec, self.eng = spec, eng
        raw = spec.raw
        self.card = eng.describe()
        self.pnames = {p.name for p in eng.parameters()}
        cal = raw["calibration"]
        row = spec.units()[0]
        self.base_unit = spec.unit_attributes(row)
        pops = (raw["decision_units"].get("populations") or {})
        self.population = (sum(float(row[p["column"]]) for p in pops.values()) if pops
                           else float(self.base_unit.get("N")))
        h = raw["horizon"]
        self.windows = {k: tuple(float(x) for x in v) for k, v in raw["deliverable"]["windows"].items()}
        self.horizon = Horizon(float(h["start"]), float(h["stop"]), (float(h["start"]), float(h["stop"])),
                               dict(self.windows))
        self.outputs = list(raw["deliverable"]["outputs"])
        srcs = raw["observations"]["sources"]
        self.decls = {s["id"]: s for s in srcs}
        self.sources = {s["id"]: ob.Source(s) for s in srcs}
        self.fixed = {k: float(v) for k, v in (fixed or {}).items()}
        pri = {}
        for n, d in (cal.get("priors") or {}).items():
            pri[n] = C.Prior.from_decl(n, d)
        for s in self.sources.values():
            for n, d in s.priors().items():
                pri[n] = C.Prior.from_decl(n, d)
            disp = (s.noise or {}).get("dispersion")
            if isinstance(disp, dict):
                pri[disp["param"]] = C.Prior.from_decl(disp["param"], dict(disp["prior"], source=disp.get("source")))
        self.all_priors = dict(pri)
        names = [n for n in pri if n not in self.fixed]
        t = cal["targets"]
        rows = _csv(spec.resolve(t["csv"]))
        self.target = {"source": t["source"], "steps": np.array([int(float(r[t["step"]])) for r in rows]),
                       "y": np.array([float(r[t["observed"]]) for r in rows])}
        n_steps = self._steps()
        if self.target["steps"].max() > n_steps:
            raise ValueError(f"calibration.targets: step {int(self.target['steps'].max())} is beyond the horizon's "
                             f"{n_steps} engine steps")
        self.failed = 0
        self.problem = C.Problem(names, {n: pri[n] for n in names}, self.loglik, chunk=4096)

    # ---- engine evaluation
    def _steps(self) -> int:
        """The engine's number of steps over the horizon (its `n_steps(horizon)` when it declares one)."""
        f = getattr(self.eng, "n_steps", None)
        if not callable(f):
            raise ValueError(f"engine {self.card.name}: an estimates spec needs n_steps(horizon) to place its "
                             "targets on the engine's step grid")
        return int(f(self.horizon))

    def _vals(self, X: dict) -> dict:
        n = len(next(iter(X.values()))) if X else 1
        out = {k: np.asarray(v, float) for k, v in X.items()}
        for k, v in self.fixed.items():
            out[k] = np.full(n, v)
        return out

    def _engine_params(self, V: dict, i: int) -> dict:
        return {k: float(v[i]) for k, v in V.items() if k in self.pnames}

    def _unit(self, V: dict, i: int) -> UnitSpec:
        a = dict(self.base_unit)
        a.update({k: float(v[i]) for k, v in V.items() if k in self.card.unit_attributes})
        return UnitSpec("population", a)

    def run(self, V: dict, n: int) -> list:
        params = [self._engine_params(V, i) for i in range(n)]
        units = [self._unit(V, i) for i in range(n)]
        return self._run(params, units)

    def _run(self, params, units):
        """run_batch with refused members isolated (their likelihood is -inf, counted): a batch the engine refuses
        is split until each refused member stands alone."""
        try:
            return self.eng.run_batch(params, units, [Package("none", {})] * len(params), self.horizon, 1)
        except (ValueError, RuntimeError):
            if len(params) == 1:
                self.failed += 1
                return [None]
            h = len(params) // 2
            return self._run(params[:h], units[:h]) + self._run(params[h:], units[h:])

    def expected(self, outs, V: dict) -> np.ndarray:
        """Expected observations (members x targets) through the target source's measurement model."""
        sid = self.target["source"]
        src, decl = self.sources[sid], self.decls[sid]
        n = len(outs)
        T = max(len(o.meta["incidence"]) for o in outs if o is not None) if any(o is not None for o in outs) else 0
        inc = np.full((n, T), np.nan)
        for i, o in enumerate(outs):
            if o is not None:
                inc[i] = np.asarray(o.meta["incidence"], float) * self.population
        pmf = (decl.get("delay") or {}).get("pmf")
        if pmf:
            k = np.asarray(pmf, float)
            lag = np.zeros_like(inc)
            for j, pj in enumerate(k):
                lag[:, j:] += pj * inc[:, :T - j] if j else pj * inc
            inc = lag
        vals = {p: np.asarray(V[p], float)[:, None] for p in src.params() if p in V}
        mu = src.expected(np.where(np.isfinite(inc), inc, 0.0), vals) if src.bias else inc
        mu = np.where(np.isfinite(inc), mu, np.nan)
        return mu[:, self.target["steps"] - 1]

    def _dispersion(self, V, n):
        nz = self.sources[self.target["source"]].noise
        d = nz.get("dispersion")
        if nz["model"] != "negbin":
            return None
        if isinstance(d, dict):
            return np.asarray(V[d["param"]], float)[:, None]
        return np.full((n, 1), float(d))

    def loglik(self, X):
        from toolkit.analysis import observation as ob
        n = len(next(iter(X.values())))
        V = self._vals(X)
        outs = self.run(V, n)
        ll = np.full(n, -np.inf)
        aux = {o["name"]: np.full(n, np.nan) for o in self.outputs}
        ok = np.array([o is not None for o in outs])
        if not ok.any():
            return ll, aux
        mu = self.expected(outs, V)
        y = self.target["y"]
        model = self.sources[self.target["source"]].noise["model"]
        kw = {"dispersion": self._dispersion(V, n)} if model == "negbin" else {}
        mu_ok = np.where(np.isfinite(mu), np.maximum(mu, 0.0), 0.0)
        with np.errstate(divide="ignore", invalid="ignore"):
            lr = ob.loglik(model, y[None, :], mu=mu_ok, **kw)
        ll = np.where(ok, np.sum(lr, axis=1), -np.inf)
        for i, o in enumerate(outs):
            if o is None:
                continue
            for d in self.outputs:
                aux[d["name"]][i] = float(o.windows[d["window"]][d["metric"]])
        return ll, aux

    def predictive(self, X, rng, n_rep=1):
        """Posterior predictive draws of the target series (members x targets) and their expected values."""
        n = len(next(iter(X.values())))
        V = self._vals(X)
        outs = self.run(V, n)
        mu = self.expected(outs, V)
        model = self.sources[self.target["source"]].noise["model"]
        if model == "negbin":
            k = self._dispersion(V, n)
            lam = rng.gamma(k, mu / k)
            return rng.poisson(lam), mu, outs
        return rng.poisson(mu), mu, outs


def _decision(dec, v: np.ndarray) -> dict:
    thr, prob = float(dec["threshold"]), float(dec["probability"])
    p = float(np.mean(v > thr)) if dec["direction"] == "above" else float(np.mean(v < thr))
    verdict = "yes" if p >= prob else "no" if p <= 1.0 - prob else "undetermined"
    return {"output": dec["output"], "threshold": thr, "direction": dec["direction"], "probability_required": prob,
            "probability": p, "verdict": verdict, "statement": _statements(dec)[verdict]}


def _sample(P: C.Problem, seed: int) -> dict:
    """The canonical sample; problems evaluated with run_batch() use importance sampling up to the
    run_batch dimension threshold (as calibration.posterior does)."""
    mip = C.thresholds()["max_importance_params_run_batch"]
    refused = []
    # the calibration defaults, then once with the larger budget of thresholds.yaml estimates.importance_retry (a
    # curved ridge can leave importance sampling below its ESS floor), then adaptive Metropolis (its own floors:
    # R-hat and bulk ESS). Every stage fails closed; the floors are never lowered.
    for over in ({}, thresholds()["importance_retry"], {"max_importance_params": 0}):
        try:
            out = C.sample(P, seed=int(seed), **dict({"max_importance_params": mip}, **over))
        except C.CalibrationRefused as e:
            if e.code not in ("posterior_ess", "posterior_mcmc"):
                raise
            refused.append(str(e))
            continue
        if refused:
            out["diagnostics"] = dict(out["diagnostics"], earlier_attempts_refused=refused)
        return out
    raise C.CalibrationRefused("posterior_ess", "; ".join(refused))


def _rows_dict(names, rows):
    rows = np.asarray(rows, float)
    return {n: rows[:, j] for j, n in enumerate(names)}


def analyse(spec, eng, seed: int = 0, n_draws: int | None = None, prior_range=True, log=print) -> dict:
    """Every number of the estimates analysis (no files written): calibration, draws, outputs, decision, tests.
    `prior_range`: True runs the automatic test of every undetermined parameter (the run's setting); False none; a
    collection of names only those (self-tests)."""
    raw = spec.raw
    u = raw["uncertainty"]
    lvl = float(u.get("level", 0.95))
    seed = int(u.get("seed", seed))
    E = EstimatesProblem(spec, eng)
    P = E.problem
    s = _sample(P, seed)
    if np.any(~np.isfinite(s["ll_rows"])):
        raise C.CalibrationRefused("posterior_ess", "a canonical draw has no finite likelihood")
    cls = C.classify(P, s)
    xh, conv, msg = C._map_point(P, s["best"])
    smp = {"sample_id": s["sample_id"], "names": s["names"], "rows": np.asarray(s["rows"], float)}
    nd = int(n_draws or u.get("draws", 1000))
    dr = C.draws(smp, nd, seed)
    X = {n: np.array([d["values"][n] for d in dr]) for n in smp["names"]}
    ll, aux = E.loglik(X)                     # the engine re-run at each draw's applied values
    if E.failed or not np.all(np.isfinite(ll)):
        raise C.CalibrationRefused("posterior_engine_failure", f"the engine refused {E.failed} draws")
    vals = {o["name"]: aux[o["name"]] for o in E.outputs}
    dec = raw["deliverable"].get("decision")
    decision = _decision(dec, vals[dec["output"]]) if dec else None
    # automatic prior_range tests: every parameter the data do not determine, at each end of its declared range: the
    # posterior of the others re-sampled, the outputs and the decision recomputed
    th = thresholds()
    tests = []
    for t in C.prior_range_tests(cls, P) if prior_range else []:
        if prior_range is not True and t["parameter"] not in prior_range:     # a self-test's subset
            continue
        res = {"id": t["id"], "parameter": t["parameter"], "class": t["class"], "values": t["values"],
               "source": t["source"], "status": "run", "ends": []}
        for v in t["values"]:
            try:
                Ef = EstimatesProblem(spec, eng, fixed={t["parameter"]: v})
                sf = _sample(Ef.problem, seed)
            except C.CalibrationRefused as e:
                res["ends"].append({"value": v, "refused": str(e)})
                continue
            dr_f = C.draws({"sample_id": sf["sample_id"], "names": sf["names"], "rows": np.asarray(sf["rows"])},
                           min(nd, 1000), seed)
            Xf = {n: np.array([d["values"][n] for d in dr_f]) for n in sf["names"]}
            _, auxf = Ef.loglik(Xf)
            drop = float(s["best_ll"] - sf["best_ll"])
            end = {"value": v, "median": {o["name"]: float(np.median(auxf[o["name"]])) for o in E.outputs},
                   "interval": {o["name"]: C.interval(auxf[o["name"]], lvl) for o in E.outputs},
                   "loglik_drop": drop, "rejected_by_data": drop > th["end_loglik_drop_max"]}
            if dec:
                df = _decision(dec, auxf[dec["output"]])
                end.update(verdict=df["verdict"], probability=df["probability"])
            res["ends"].append(end)
        if any("refused" in e for e in res["ends"]):
            res["status"] = "not_assessable"
        if dec and res["status"] == "run":
            # an end the data reject (its best fit is worse by more than the threshold) cannot change the decision
            kept = [e for e in res["ends"] if not e["rejected_by_data"]]
            res["decision_changes"] = any(e["verdict"] != decision["verdict"] for e in kept)
            res["ends_rejected_by_data"] = [e["value"] for e in res["ends"] if e["rejected_by_data"]]
        tests.append(res)
    # what the data determine, per output: the prior alone against the posterior (contraction on the output's own
    # scale), and how much each undetermined parameter moves it across the parameter's own posterior range (the
    # marginal slope in the canonical draws times the parameter's central interval, relative to the output's median:
    # `dependence`). Above thresholds.yaml estimates.dependence_max the output is set by that declared range.
    rng = np.random.default_rng([seed, 17])
    Xp = P.x_of_z(rng.standard_normal((PRIOR_DRAWS, len(P.names))))
    _, auxp = E.loglik(_rows_dict(P.names, Xp))
    undetermined = [t["parameter"] for t in C.prior_range_tests(cls, P)]
    determined = {}
    for o in E.outputs:
        n = o["name"]
        vp, vq = auxp[n][np.isfinite(auxp[n])], vals[n]
        pos = bool(np.all(vp > 0) and np.all(vq > 0))
        f = np.log if pos else (lambda x: x)
        c = float(1.0 - np.var(f(vq)) / np.var(f(vp))) if np.var(f(vp)) > 0 else 0.0
        lo_q, hi_q = C.interval(vq, lvl)
        scale = max(abs(float(np.median(vq))), 1e-300)
        dep = {}
        for p in undetermined:
            x = P.priors[p].natural(X[p])
            vx = float(np.var(x))
            slope = float(np.cov(x, vq)[0, 1] / vx) if vx > 0 else 0.0
            span = float(np.diff(C.interval(x, lvl))[0])
            dep[p] = abs(slope) * span / scale
        dmax = th["dependence_max"]
        on = sorted(k for k, v in dep.items() if v > dmax)
        determined[n] = {"prior_interval": C.interval(vp, lvl), "posterior_interval": [lo_q, hi_q],
                         "contraction": c, "scale": "log" if pos else "linear", "dependence": dep,
                         "depends_on": on, "class": "set_by_declared_ranges" if on else "determined_by_data"}
    # posterior predictive check of the target series
    yrep, mu, outs = E.predictive({n: v[:PPC_DRAWS] for n, v in X.items()}, rng)
    lo, hi = np.quantile(yrep, (1 - lvl) / 2, axis=0), np.quantile(yrep, 1 - (1 - lvl) / 2, axis=0)
    y = E.target["y"]
    ppc = {"steps": E.target["steps"].tolist(), "observed": y.tolist(), "median": np.median(yrep, axis=0).tolist(),
           "lo": lo.tolist(), "hi": hi.tolist(), "coverage": float(np.mean((y >= lo) & (y <= hi))), "level": lvl,
           "draws": int(len(yrep))}
    rt = None
    if outs and outs[0] is not None and "rt" in outs[0].meta:
        R = np.array([o.meta["rt"] for o in outs])
        rt = {"t": list(outs[0].meta.get("t") or range(R.shape[1])), "median": np.median(R, axis=0).tolist(),
              "lo": np.quantile(R, (1 - lvl) / 2, axis=0).tolist(),
              "hi": np.quantile(R, 1 - (1 - lvl) / 2, axis=0).tolist()}
    post = {n: {"median": float(np.median(smp["rows"][:, j])), "interval": C.interval(smp["rows"][:, j], lvl),
                "prior": P.priors[n].describe()} for j, n in enumerate(smp["names"])}
    return {"E": E, "P": P, "sample": s, "classes": cls, "map": dict(zip(P.names, (float(v) for v in xh))),
            "converged": conv, "message": msg, "draws": dr, "values": vals, "level": lvl, "determined": determined,
            "decision": decision, "tests": tests, "ppc": ppc, "rt": rt, "posterior": post, "seed": seed}


# ------------------------------------------------------------------------------------- the run's artifacts
def _label(spec, res) -> tuple[str, dict]:
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import render_claims
    u = spec.raw["uncertainty"]
    covers = [x["kind"] for x in u.get("sources") or []]
    exc = [{"kind": e["kind"], "reason": e.get("reason", "")} for e in u.get("excluded") or [] if "kind" in e]
    meta = {"level": res["level"], "covers": covers, "excludes": [e["kind"] for e in exc]}
    pd = C.label_parts(res["classes"], res["P"])["prior_dominated"]
    if pd:
        meta["prior_dominated"] = pd
    return render_claims.interval_label(meta), {"covers": covers, "excludes": exc, "interval": meta}


def _fig(run_dir, name, draw, sources) -> dict:
    """One harness figure: PNG and SVG with the provenance sidecar the contract checks (toolkit/analysis/figures.py)."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from toolkit.analysis import figure_style as FS, figures as FG
    t = FS.setup("light")
    fig, ax = plt.subplots(figsize=(7.5, 4.2))
    draw(fig, ax, t)
    out = os.path.join(run_dir, "figures")
    os.makedirs(out, exist_ok=True)
    files = FG._save(fig, out, name, run_dir, sources, True, t)
    return {"id": name, "path": os.path.relpath(files["png"], run_dir), "svg": os.path.relpath(files["svg"], run_dir),
            "sources": list(sources), "tier": "decision" if name.startswith("estimates_") else "diagnostics"}


def figures(run_dir, spec, res, est_rel) -> list:
    from toolkit.analysis import figure_style as FS
    figs = []
    ppc = res["ppc"]

    def ppc_draw(fig, ax, t):
        x = np.asarray(ppc["steps"])
        ax.fill_between(x, ppc["lo"], ppc["hi"], color=t["cat"][0], alpha=0.25, lw=0,
                        label=f"{ppc['level'] * 100:g}% posterior predictive interval")
        ax.plot(x, ppc["median"], color=t["cat"][0], lw=1.6, label="posterior predictive median")
        ax.plot(x, ppc["observed"], "o", ms=3, color=t["ink"], label="observed")
        ax.set_xlabel("step of the horizon (target series)")
        ax.set_ylabel("observed count per step")
        FS.style(ax, t, grid="y")
        ax.legend(frameon=False, fontsize=8)
        FS.title(fig, "Fit to the target series", t, sub=f"coverage of the predictive interval "
                 f"{ppc['coverage'] * 100:.0f}% of steps")
    figs.append(_fig(run_dir, "diagnostics_ppc", ppc_draw, [est_rel]))
    if res["rt"]:
        rt = res["rt"]

        def rt_draw(fig, ax, t):
            x = np.asarray(rt["t"], float)
            ax.fill_between(x, rt["lo"], rt["hi"], color=t["cat"][1], alpha=0.25, lw=0,
                            label=f"{res['level'] * 100:g}% interval")
            ax.plot(x, rt["median"], color=t["cat"][1], lw=1.6, label="median")
            ax.axhline(1.0, color=t["muted"], lw=0.8, ls="--")
            ax.set_xlabel("time (model years)")
            ax.set_ylabel("instantaneous reproduction number R_t")
            FS.style(ax, t, grid="y")
            ax.legend(frameon=False, fontsize=8)
            FS.title(fig, "Reproduction number over the horizon", t, sub="engine R_t at the posterior draws")
        figs.append(_fig(run_dir, "diagnostics_rt", rt_draw, [est_rel]))
    for o in spec.raw["deliverable"]["outputs"]:
        n = o["name"]
        dd = res["determined"][n]
        v = res["values"][n]

        def out_draw(fig, ax, t, n=n, dd=dd, v=v):
            ax.hist(v, bins=40, color=t["cat"][0], alpha=0.85)
            for q in dd["posterior_interval"]:
                ax.axvline(q, color=t["ink"], lw=0.8, ls="--")
            ax.set_xlabel(n)
            ax.set_ylabel("posterior draws")
            FS.style(ax, t, grid="y")
            FS.title(fig, f"{n}: posterior draws", t,
                     sub=f"{res['level'] * 100:g}% interval dashed; prior-only interval "
                         f"{FS.fmt(dd['prior_interval'][0])} to {FS.fmt(dd['prior_interval'][1])}")
        figs.append(_fig(run_dir, f"estimates_{n}", out_draw, [est_rel]))
    return figs


def output_interval(meta: dict, depends_on) -> dict:
    """One output's interval metadata: the run's (level, covers, excludes) with the parameters whose declared ranges
    set that output (not every prior-dominated parameter of the calibration)."""
    m = {k: v for k, v in meta.items() if k != "prior_dominated"}
    if depends_on:
        m["prior_dominated"] = list(depends_on)
    return m


def output_label(meta: dict, depends_on) -> str:
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import render_claims
    return render_claims.interval_label(output_interval(meta, depends_on))


def _c(cid, kind, art, field, units="", **kw):
    return {"id": cid, "claim_kind": kind, "units": units, "source_artifact": art, "source_field": field,
            "produced_by": "harness", **kw}


def claims(run_dir, spec, res, arts, interval) -> dict:
    import yaml
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import build_claims_ledger as B
    import render_claims as R
    from toolkit.analysis.artifacts import _safe_io
    est = arts["estimates"]
    cl = [_c("uq.draws", "count", arts["uncertainty"], "data.draws", "draws")]
    for o in spec.raw["deliverable"]["outputs"]:
        n = o["name"]
        cl += [_c(f"{n}.median", "scalar", est, f"data.outputs.{n}.median", o["scale"],
                  interval=output_interval(interval, res["determined"][n]["depends_on"])),
               _c(f"{n}.prior_lo", "scalar", est, f"data.determined.{n}.prior_interval[0]", o["scale"]),
               _c(f"{n}.prior_hi", "scalar", est, f"data.determined.{n}.prior_interval[1]", o["scale"]),
               _c(f"{n}.contraction", "scalar", est, f"data.determined.{n}.contraction"),
               _c(f"{n}.determined", "label", est, f"data.determined.{n}.class")]
    if res["decision"]:
        cl += [_c("decision.verdict", "label", est, "data.decision.verdict"),
               _c("decision.probability", "scalar", est, "data.decision.probability", "probability")]
    for j, n in enumerate(res["sample"]["names"]):
        cl += [_c(f"param.{n}.median", "scalar", est, f"data.parameters.{n}.median"),
               _c(f"param.{n}.class", "label", arts["calibration"], f"data.identifiability.classes.{n}")]
    cl.append(_c("ppc.coverage", "scalar", est, "data.posterior_predictive.coverage", "share"))
    spec_doc = {"producer": PRODUCER, "produced_by": "harness", "claims": cl}
    _safe_io().write_nofollow(run_dir, "models/claims_spec.yaml",
                              yaml.safe_dump(spec_doc, sort_keys=False, width=120).encode(), atomic=True)
    doc, errs = B.write(run_dir)
    if errs or doc is None or doc.get("unresolved"):
        raise RuntimeError(f"claims ledger: {errs or doc.get('unresolved')}")
    led = R.load_claims(os.path.join(run_dir, "models", "claims_ledger.yaml"))
    text, unresolved = R.render_text(results_template(spec, res), led)
    if unresolved:
        raise RuntimeError(f"results.md: unresolved claims {unresolved}")
    _safe_io().write_nofollow(run_dir, "results.md", text.encode("utf-8"), atomic=True)
    return {"n_claims": len(led), "unresolved": [], "ledger": "models/claims_ledger.yaml",
            "spec": "models/claims_spec.yaml", "results": "results.md"}


def _f(x):
    return "n/a" if x is None else f"{x:.3g}"


def results_template(spec, res) -> str:
    raw = spec.raw
    L = [f"# Results: {raw['question'].strip()}", "",
         "Rendered by the harness (toolkit/analysis/estimates.py) from its artifacts; no number below was typed by an "
         "agent.", "", "## Estimates", "",
         f"Over [CLAIM:uq.draws] draws of the joint posterior ({res['label']}). Each output's interval names the "
         "declared ranges that set it.", "",
         "| Output | Window metric | Median and interval | Interval under the priors alone | What the data determine |",
         "|---|---|---|---|---|"]
    for o in raw["deliverable"]["outputs"]:
        n = o["name"]
        dep = res["determined"][n].get("depends_on") or []
        what = (f"set by the declared range of {', '.join(dep)}" if dep else "determined by the data")
        L.append(f"| {n} | {o['metric']} over {o['window']} | [CLAIM:{n}.median] | [CLAIM:{n}.prior_lo] to "
                 f"[CLAIM:{n}.prior_hi] | {what} |")
    if res["decision"]:
        d = res["decision"]
        L += ["", "## Decision rule", "",
              f"Rule: {d['output']} {d['direction']} {d['threshold']:g} with probability at least "
              f"{d['probability_required']:g}. Probability over the draws: [CLAIM:decision.probability]. Verdict: "
              f"[CLAIM:decision.verdict]: {d['statement']}.", ""]
    L += ["", "## What the data do and do not determine", "",
          "Classes from the calibration (toolkit/analysis/calibration.py classify: the likelihood's curvature against "
          "each prior, and the posterior's contraction):", "", "| Parameter | Posterior median | Class | Prior |",
          "|---|---|---|---|"]
    for n in res["sample"]["names"]:
        pr = res["posterior"][n]["prior"]
        L.append(f"| {n} | [CLAIM:param.{n}.median] | [CLAIM:param.{n}.class] | {pr['dist']} "
                 f"{pr.get('range', '')} ({pr.get('source', '')}) |")
    ridges = res["classes"].get("ridges") or []
    if ridges:
        L += ["", "Directions the data leave flat (ridges): " + "; ".join(" with ".join(r["params"]) for r in ridges)
              + "."]
    if res["tests"]:
        L += ["", "### Decision with each undetermined parameter at the ends of its declared range", "",
              "Each end is re-fitted with the parameter held there. An end whose best fit is worse than the full fit's "
              f"by more than {thresholds()['end_loglik_drop_max']:g} in log-likelihood is rejected by the data and "
              "cannot change the decision.", "",
              "| Test | Parameter | End | Verdict | Rejected by the data |", "|---|---|---|---|---|"]
        for t in res["tests"]:
            for e in t.get("ends") or []:
                L.append(f"| {t['id']} | {t['parameter']} | {_f(e['value'])} | "
                         f"{e.get('verdict', 'refused: ' + str(e.get('refused', ''))[:60])} | "
                         f"{'yes' if e.get('rejected_by_data') else 'no'} |")
            L.append(f"| {t['id']} | | | decision changes: {t.get('decision_changes', 'not assessable')} | |")
    L += ["", "## Fit", "", "Posterior predictive check of the target series: [CLAIM:ppc.coverage] of observed steps "
          "inside the predictive interval.", ""]
    return "\n".join(L) + "\n"


def run(run_dir, spec, S, eng, arts: dict, data_in: list, eng_files: list, draws=None, log=print) -> dict:
    """The estimates path of toolkit/analysis/run.py: writes every artifact and deliverable, finalizes the manifest."""
    import time
    from toolkit.analysis import uncertainty as UQ
    t0 = time.time()
    res = analyse(spec, eng, n_draws=draws, log=log)
    lab, scope = _label(spec, res)
    res["label"] = lab
    s, cls = res["sample"], res["classes"]
    cal = {"method": "posterior", "units": {}, "names": list(s["names"]),
           "diagnostics": dict({"converged": res["converged"], "message": res["message"],
                                "n_targets": int(len(res["E"].target["y"])), "n_parameters": len(s["names"]),
                                "sampler": s["method"], "at_bound": []}, **s["diagnostics"]),
           "identifiability": {"method": cls["method"], "classes": cls["classes"],
                               "likelihood_classes": cls["likelihood_classes"], "contraction": cls["contraction"],
                               "ridges": cls["ridges"],
                               "status": ("well_identified" if all(v == "identified" for v in cls["classes"].values())
                                          else "partly_or_prior_dominated")},
           "sample": {"sample_id": s["sample_id"], "names": s["names"], "rows": np.asarray(s["rows"]).tolist(),
                      "method": s["method"], "seed": s["seed"]},
           "priors": {n: res["P"].priors[n].describe() for n in s["names"]}, "map": res["map"],
           "prior_range_tests": [t["id"] for t in res["tests"]],
           "prior_dominated": C.label_parts(cls, res["P"])["prior_dominated"]}
    arts["calibration"] = S.write("calibration", cal, C.PRODUCER, data_in)
    outs = {o["name"]: UQ.scalar_report([o], res["values"], res["level"], lab, len(res["draws"]),
                                        "posterior")["scalar_outputs"][o["name"]]
            for o in spec.raw["deliverable"]["outputs"]}
    for n, v in outs.items():        # each output's own label: the declared ranges that set it, not every one
        v["label"] = output_label(scope["interval"], res["determined"][n]["depends_on"])
    unc = {"draws": len(res["draws"]), "seed": res["seed"], "covers": scope["covers"], "excludes": scope["excludes"],
           "label": lab, "interval": scope["interval"],
           "fixed_plan": {"not_applicable": "estimates deliverable: no allocation plan"},
           "outputs": outs, "prior_dominated": cal["prior_dominated"],
           "draw_provenance": {"sample_id": s["sample_id"], "applied": res["draws"]}}
    arts["uncertainty"] = S.write("uncertainty", unc, UQ.PRODUCER, [arts["calibration"]] + eng_files)
    rep = UQ.scalar_report(spec.raw["deliverable"]["outputs"], res["values"], res["level"], lab, len(res["draws"]),
                           "posterior: rows of the canonical joint sample, the engine re-run at each",
                           covers=scope["covers"], excludes=scope["excludes"], interval=scope["interval"],
                           seed=res["seed"])
    for n, v in rep["scalar_outputs"].items():
        v["label"] = outs[n]["label"]
    S.deliverable(UQ.write_report(run_dir, rep), UQ.PRODUCER, "uncertainty_report")
    est = {"outputs": outs, "windows": {k: list(v) for k, v in res["E"].windows.items()},
           "decision": res["decision"] or {"not_applicable": "the spec declares no threshold rule"},
           "determined": res["determined"], "decision_tests": res["tests"], "posterior_predictive": res["ppc"],
           "rt": res["rt"], "parameters": res["posterior"], "population": res["E"].population,
           "label": lab, "level": res["level"]}
    arts["estimates"] = S.write("estimates", est, PRODUCER, [arts["calibration"], arts["uncertainty"]] + data_in)
    figs = figures(run_dir, spec, res, arts["estimates"])
    arts["figures"] = S.write("figures", {"figures": figs, "not_produced": [], "tiers": {}, "groups": {},
                                          "overviews": {}}, PRODUCER, [arts["estimates"]])
    for f in figs:
        S.deliverable(os.path.join(run_dir, f["path"]), PRODUCER, "figure")
    c = claims(run_dir, spec, res, arts, scope["interval"])
    arts["claims"] = S.write("claims", c, PRODUCER, [arts["estimates"], arts["uncertainty"], arts["calibration"]])
    for rel, role in ((c["spec"], "claims_spec"), (c["ledger"], "claims_ledger"), (c["results"], "results")):
        S.deliverable(os.path.join(run_dir, rel), PRODUCER, role)
    S.manifest["wall_s"] = round(time.time() - t0, 1)
    S.manifest["deliverable_kind"] = "estimates"
    S.manifest["expected"] = {"kinds": list(EXPECTED_KINDS), "roles": list(EXPECTED_ROLES), "decision_tests": {}}
    S.finalize("complete")
    log(f"estimates: " + ", ".join(f"{n} {v['median']:.4g} [{v['ci_low']:.4g}, {v['ci_high']:.4g}]"
                                   for n, v in outs.items()) + (f"; decision {res['decision']['verdict']}"
                                                                if res["decision"] else ""))
    return {"artifacts": arts, "estimates": est, "uncertainty": unc, "calibration": cal,
            "wall_s": S.manifest["wall_s"]}


def headline(run_dir: str) -> dict:
    """The numbers the impact ledger records before and after a fix: each output's median and interval."""
    from toolkit.analysis.artifacts import read
    d = read(run_dir, "analysis/estimates.yaml")["data"]
    out = {}
    for n, v in d["outputs"].items():
        out.update({f"{n}_median": float(v["median"]), f"{n}_lo": float(v["ci_low"]), f"{n}_hi": float(v["ci_high"])})
    if isinstance(d.get("decision"), dict) and "verdict" in d["decision"]:
        out["decision_verdict"] = d["decision"]["verdict"]
    return out


# --------------------------------------------------------------------------- self-test
def _synthetic(r0=1.8, rho=0.1, n=30, N=1e5, seed_share=3e-4, gi=(0.5, 0.5), k=50.0, rs=3):
    """A synthetic weekly series from the reference implementation in the renewal pack (not the engine's code)."""
    from toolkit.engines.renewal.benchmarks.oracles import renewal_reference
    inc, _ = renewal_reference(r0, list(gi), 1.0, seed_share, n)
    rng = np.random.default_rng(rs)
    mu = rho * np.asarray(inc) * N
    return [int(x) for x in rng.poisson(rng.gamma(k, mu / k))]


def _self_test() -> int:
    import tempfile
    import yaml
    from toolkit.analysis import contract
    from toolkit.analysis.run import run as run_analysis
    from toolkit.spec import estimates as ES, schema
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)

    rows = _synthetic()
    with tempfile.TemporaryDirectory() as d:
        d = os.path.realpath(d)          # run paths are recorded relative to the run dir: no symlinked temp root
        sp = ES.toy_dir(d, rows=rows)
        ok(schema.validate(schema.load(sp)) == [], "AE0 the toy estimates spec validates")
        r = run_analysis(d, unsigned=True, log=lambda *a: None)
        rep = yaml.safe_load(open(os.path.join(d, "uncertainty_report.yaml")))
        so = rep.get("scalar_outputs") or {}
        ok(set(so) == {"reff_next", "attack"} and all(so[k]["ci_low"] <= so[k]["median"] <= so[k]["ci_high"]
                                                     for k in so),
           f"AE1 uncertainty_report.yaml carries scalar_outputs for every declared output: {sorted(so)}")
        from toolkit.engines.renewal.benchmarks.oracles import renewal_reference
        inc, S_end = renewal_reference(1.8, [0.5, 0.5], 1.0, 3e-4, len(rows))
        true_reff, true_ar = 1.8 * S_end, sum(inc)
        ok(so["reff_next"]["ci_low"] <= true_reff <= so["reff_next"]["ci_high"],
           f"AE2 the reff interval covers the generator's value {true_reff:.3f}: "
           f"[{so['reff_next']['ci_low']:.3f}, {so['reff_next']['ci_high']:.3f}]")
        ok(so["attack"]["ci_low"] <= true_ar <= so["attack"]["ci_high"],
           f"AE3 the attack-rate interval covers the generator's value {true_ar:.3f}: "
           f"[{so['attack']['ci_low']:.3f}, {so['attack']['ci_high']:.3f}]")
        est = yaml.safe_load(open(os.path.join(d, "analysis", "estimates.yaml")))["data"]
        ok(est["decision"]["verdict"] in ("yes", "no", "undetermined") and est["decision"]["statement"],
           f"AE4 the threshold rule gives a verdict and its statement: {est['decision']['verdict']}")
        f = contract.gate_findings(d)
        kinds = sorted({x["kind"] for x in f})
        ok(kinds == ["analysis_contract_unsigned"], f"AE5 the contract checks pass on the estimates run (only the "
                                                    f"unsigned test-run finding): {kinds} {[x['claim'] for x in f][:2]}")
        txt = open(os.path.join(d, "results.md")).read()
        ok("[CLAIM:" not in txt and "What the data do and do not determine" in txt,
           "AE6 results.md is rendered with every claim resolved")
        man = json.load(open(os.path.join(d, "analysis", "manifest.json")))
        ok(man.get("deliverable_kind") == "estimates" and "allocation" not in {e["role"] for e in
                                                                               man["deliverables"].values()},
           "AE7 the manifest records the estimates kind and no allocation deliverable")
        hl = headline(d)
        ok("reff_next_median" in hl and "attack_hi" in hl, f"AE8 headline() reads the outputs: {sorted(hl)[:3]}")
        # draws are rows of the canonical sample: altering one applied value fails the contract check
        unc_p = os.path.join(d, "analysis", "uncertainty.yaml")
        doc = yaml.safe_load(open(unc_p))
        doc["data"]["draw_provenance"]["applied"][0]["values"]["r0"] *= 1.01
        from toolkit.analysis.contract import _posterior_provenance
        cal = yaml.safe_load(open(os.path.join(d, "analysis", "calibration.yaml")))["data"]
        ok(bool(_posterior_provenance("analysis/calibration.yaml", cal, doc["data"])),
           "AE9 a draw that is not a row of the canonical sample fails the provenance check")
        # the reporting fraction and s0 trade off: with s0 unknown, reporting is not determined
    with tempfile.TemporaryDirectory() as d:
        d = os.path.realpath(d)
        sp = ES.toy_dir(d, rows=rows)
        raw = yaml.safe_load(open(sp))
        raw["calibration"]["priors"]["s0"] = {"dist": "uniform", "range": [0.3, 1.0], "source": "toy: no serosurvey"}
        yaml.safe_dump(raw, open(sp, "w"), sort_keys=False)
        s2 = schema.load(sp)          # the analysis alone, with the automatic test of s0 only (self-test time)
        res = analyse(s2, s2.engine(), n_draws=400, prior_range=["s0"], log=lambda *a: None)
        est = {"determined": res["determined"], "decision_tests": res["tests"]}
        cls = res["classes"]["classes"]
        ok(cls.get("s0") == "prior_dominated" and cls.get("reporting") in ("prior_dominated", "partly_identified"),
           f"AE10 with s0 unknown, s0 and the reporting fraction are not determined by the series: {cls}")
        dd = est["determined"]
        ok(dd["attack"]["class"] == "set_by_declared_ranges" and "s0" in dd["attack"]["depends_on"]
           and dd["reff_next"]["class"] == "determined_by_data",
           f"AE11 with s0 unknown the attack rate is set by s0's declared range and reff is determined by the data "
           f"(attack {dd['attack']['class']} {dd['attack']['dependence']}; reff {dd['reff_next']['class']} "
           f"{dd['reff_next']['dependence']})")
        ok(any(t["parameter"] == "s0" and t["status"] == "run" and len(t["ends"]) == 2 for t in est["decision_tests"]),
           "AE12 an automatic prior_range test runs for s0 at both ends of its range")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    print(__doc__)
