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
