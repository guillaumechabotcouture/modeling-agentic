#!/usr/bin/env python3
"""Identifiability analysis: Fisher-information diagonal + profile-likelihood scan.

Library consumed by `toolkit/analysis/calibration.py` and `toolkit/engines/built/calibrators.py`:
- `hessian(loss_fn, point, bounds, step_frac)`
- `fisher_analysis(loss_fn, point, bounds, step_frac)`
- `profile_likelihood(loss_fn, point, param, n_points, nuisance_bounds, method, maxiter)`
- `classify(name, point_estimate, fisher_se, profile, ridge)`

Usage:
    python3 scripts/identifiability.py --self-test
"""

from __future__ import annotations

import math
import sys
from typing import Callable, Optional


# Chi-squared 95% CI for 1 DOF: 3.84. Profile-likelihood CI uses
# -2 * (logL(profile) - logL(max)) < 3.84, equivalent to loss increase < 1.92.
PROFILE_CI_THRESHOLD = 1.92

# Ridge detection in relative coordinates: a Hessian eigen-direction
# whose implied relative SE exceeds this is unconstrained (same 50% cut
# classify() uses for single parameters). Eigenvalues below
# RIDGE_EIG_RTOL·λ_max are treated as numerically zero.
RIDGE_REL_SE = 0.50
RIDGE_EIG_RTOL = 1e-8
RIDGE_LOADING_MIN = 0.30
PROFILE_MAXITER = 100


def _step(p0: float, bounds: Optional[tuple[float, float]],
          step_frac: float) -> float:
    """Finite-difference step scaled to |p| with a floor tied to the
    plausible range, so parameters on very different scales get
    comparable relative resolution."""
    floor = 1e-6
    if bounds is not None and bounds[1] > bounds[0]:
        floor = max(floor, 1e-4 * (bounds[1] - bounds[0]))
    return max(abs(p0) * step_frac, floor)


def hessian(loss_fn: Callable[[dict], float], point: dict[str, float],
            bounds: Optional[dict[str, tuple[float, float]]] = None,
            step_frac: float = 0.01):
    """Full central-difference Hessian of loss_fn at `point`.
    Returns (names, H as numpy array). Costs 2k + 2k(k−1) evaluations."""
    import numpy as np
    names = list(point)
    k = len(names)
    bounds = bounds or {}
    h = [_step(point[n], bounds.get(n), step_frac) for n in names]
    f0 = loss_fn(point)

    def ev(deltas):
        q = dict(point)
        for i, d in deltas:
            q[names[i]] = point[names[i]] + d
        return loss_fn(q)

    H = np.zeros((k, k))
    for i in range(k):
        fp = ev([(i, h[i])]); fm = ev([(i, -h[i])])
        H[i, i] = (fp - 2 * f0 + fm) / (h[i] * h[i])
    for i in range(k):
        for j in range(i + 1, k):
            fpp = ev([(i, h[i]), (j, h[j])]); fpm = ev([(i, h[i]), (j, -h[j])])
            fmp = ev([(i, -h[i]), (j, h[j])]); fmm = ev([(i, -h[i]), (j, -h[j])])
            H[i, j] = H[j, i] = (fpp - fpm - fmp + fmm) / (4 * h[i] * h[j])
    return names, H


def fisher_analysis(loss_fn: Callable[[dict], float], point: dict[str, float],
                    bounds: Optional[dict[str, tuple[float, float]]] = None,
                    step_frac: float = 0.01) -> dict:
    """Phase 21 δ: SE from sqrt(diag(F⁻¹)) on the full Hessian of the NLL,
    plus ridge detection from the eigen-decomposition in relative
    coordinates (θ_i / s_i, s_i = |θ_i| or the bound range when θ_i≈0).

    Returns {se: {name: se|None}, ridges: [ {combination, rel_se,
    eigenvalue, params} ], ridge_params: set, condition_number}."""
    import numpy as np
    names, H = hessian(loss_fn, point, bounds, step_frac)
    bounds = bounds or {}
    scale = []
    for n in names:
        v = abs(point[n])
        if v < 1e-12:
            b = bounds.get(n)
            v = (b[1] - b[0]) if b and b[1] > b[0] else 1.0
        scale.append(v)
    D = np.diag(scale)
    Hs = D @ H @ D
    Hs = 0.5 * (Hs + Hs.T)
    if not np.all(np.isfinite(Hs)):
        return {"se": {n: None for n in names}, "ridges": [],
                "ridge_params": set(names), "condition_number": None,
                "error": "non-finite Hessian entries"}
    evals, evecs = np.linalg.eigh(Hs)
    lam_max = float(max(abs(evals).max(), 1e-300))
    ridges = []
    ridge_params: set[str] = set()
    for idx, lam in enumerate(evals):
        lam = float(lam)
        null = lam <= RIDGE_EIG_RTOL * lam_max
        rel_se_dir = (float("inf") if lam <= 0 else 1.0 / math.sqrt(lam))
        if null or rel_se_dir > RIDGE_REL_SE:
            v = evecs[:, idx]
            loaded = [(names[i], float(v[i])) for i in range(len(names))
                      if abs(v[i]) >= RIDGE_LOADING_MIN]
            if not loaded:
                continue
            combo = " + ".join(f"{c:+.2f}·{n}/{scale[names.index(n)]:.3g}"
                               for n, c in loaded)
            ridges.append({
                "combination": combo,
                "params": [n for n, _ in loaded],
                "eigenvalue_relative": lam,
                "rel_se_along_direction": (None if not math.isfinite(rel_se_dir)
                                           else round(rel_se_dir, 4)),
                "null_direction": bool(null),
            })
            ridge_params.update(n for n, _ in loaded)
    pos = evals[evals > RIDGE_EIG_RTOL * lam_max]
    cond = (float(pos.max() / pos.min()) if len(pos) == len(evals) and len(pos)
            else float("inf"))
    # Pseudo-inverse over the positive (identified) eigen-directions only,
    # in scaled coordinates, then back to native units. Parameters loading
    # on a null direction have infinite SE → reported as None.
    keep = evals > RIDGE_EIG_RTOL * lam_max
    cov_s = (evecs[:, keep] / evals[keep]) @ evecs[:, keep].T
    null_params = {n for r in ridges if r["null_direction"] for n in r["params"]}
    se = {}
    for i, n in enumerate(names):
        var = float(cov_s[i, i]) * scale[i] ** 2
        se[n] = (math.sqrt(var) if var > 0 and n not in null_params
                 else None)
    return {"se": se, "ridges": ridges, "ridge_params": ridge_params,
            "condition_number": cond}


def profile_likelihood(loss_fn: Callable[[dict], float],
                       point: dict[str, float],
                       param: dict,
                       n_points: int = 20,
                       nuisance_bounds: Optional[dict[str, tuple[float, float]]] = None,
                       method: str = "full",
                       maxiter: int = PROFILE_MAXITER) -> dict:
    """Profile the (NLL-scaled) loss over one parameter's plausible range.

    Phase 21 δ: with method="full" (default) the nuisance parameters are
    re-optimized at every grid point (bounded L-BFGS-B, warm-started from
    the previous grid point, maxiter-capped), so a parameter whose effect
    another parameter can absorb shows a flat profile. method="conditional"
    keeps the pre-21 slice (others held at point estimates) and is reported
    as weaker evidence. Returns the curve plus the 95% CI range where the
    profile stays within PROFILE_CI_THRESHOLD of its minimum."""
    name = param["name"]
    lo = float(param["lower_bound"])
    hi = float(param["upper_bound"])
    n = int(param.get("profile_n_points", n_points))
    if n < 5:
        n = 5

    # Geometric spacing if param spans > 1 decade, else linear.
    if lo > 0 and hi / lo > 10:
        log_lo = math.log(lo); log_hi = math.log(hi)
        grid = [math.exp(log_lo + (log_hi - log_lo) * i / (n - 1))
                for i in range(n)]
    else:
        grid = [lo + (hi - lo) * i / (n - 1) for i in range(n)]

    others = [k for k in point if k != name]
    nuisance_bounds = nuisance_bounds or {}
    do_reopt = method == "full" and others
    if do_reopt:
        from scipy.optimize import minimize
        bnds = [nuisance_bounds.get(k, (None, None)) for k in others]

    def _safe(p):
        try:
            v = loss_fn(p)
        except Exception:
            return float("inf")
        return v if math.isfinite(v) else float("inf")

    curve = []
    # Warm-start outward from the point estimate in both directions so the
    # nuisance solution tracks a continuous ridge.
    order = sorted(range(len(grid)), key=lambda i: abs(grid[i] - point[name]))
    start_idx = order[0]
    results = [None] * len(grid)
    for direction in (range(start_idx, len(grid)), range(start_idx - 1, -1, -1)):
        x0 = [point[k] for k in others]
        for i in direction:
            v = grid[i]
            if not do_reopt:
                p = dict(point); p[name] = v
                results[i] = _safe(p)
                continue

            def f(x, v=v):
                p = dict(point); p[name] = v
                p.update(zip(others, (float(xi) for xi in x)))
                val = _safe(p)
                return val if math.isfinite(val) else 1e300

            try:
                r = minimize(f, x0, method="L-BFGS-B", bounds=bnds,
                             options={"maxiter": maxiter})
                val = float(r.fun)
                # Never report worse than the conditional slice.
                p = dict(point); p[name] = v
                val = min(val, _safe(p))
                x0 = list(r.x)
            except Exception:
                p = dict(point); p[name] = v
                val = _safe(p)
            results[i] = val if val < 1e299 else float("inf")
    curve = list(zip(grid, results))

    loss_values = [l for _, l in curve if math.isfinite(l)]
    if not loss_values:
        return {"curve": curve, "ci_low": None, "ci_high": None,
                "flat_ratio": float("nan"), "method": method}
    min_loss = min(loss_values)
    threshold = min_loss + PROFILE_CI_THRESHOLD

    within = [v for v, l in curve if math.isfinite(l) and l <= threshold]
    ci_low = min(within) if within else None
    ci_high = max(within) if within else None

    # Flat-ridge metric: if the full scan spans < 1.92 NLL units, the
    # parameter is hardly constrained at all → ridge-trapped.
    total_range = max(loss_values) - min_loss
    flat_ratio = total_range / PROFILE_CI_THRESHOLD if total_range > 0 else 0

    return {
        "curve": curve,
        "min_loss": min_loss,
        "ci_low": ci_low,
        "ci_high": ci_high,
        "flat_ratio": flat_ratio,
        "method": "full" if do_reopt else "conditional",
    }


def classify(name: str, point_estimate: float, fisher_se: Optional[float],
             profile: dict,
             ridge: Optional[dict] = None) -> tuple[str, str]:
    """Classify the parameter's identifiability status and write one line
    explaining why. Returns (status, explanation). `ridge` (Phase 21 δ)
    is the weak/null Hessian eigen-direction this parameter loads on."""
    if ridge is not None:
        return ("unidentified",
                "loads on an unconstrained parameter combination "
                f"({ridge['combination']}); the data constrain the "
                "combination's complement, not this parameter on its own.")
    if profile.get("flat_ratio") is not None and profile["flat_ratio"] < 1.0:
        # The full scan over the parameter's plausible range doesn't
        # move loss even by the CI threshold. Ridge-trapped.
        return ("unidentified",
                f"profile loss spans only {profile['flat_ratio']:.2f}× "
                "the 95% CI threshold across the full plausible range — "
                "the parameter is on a flat ridge and doesn't affect the fit.")

    if fisher_se is None:
        return ("unidentified",
                "Fisher information diagonal is non-positive at the point "
                "estimate — either not a local minimum or the loss is flat.")

    if abs(point_estimate) < 1e-12:
        rel_se = fisher_se  # arbitrary; just use absolute SE
    else:
        rel_se = fisher_se / abs(point_estimate)

    if rel_se < 0.10:
        return ("well_identified",
                f"relative SE {rel_se:.1%}; profile CI "
                f"[{profile.get('ci_low', '?')}, {profile.get('ci_high', '?')}]")
    elif rel_se < 0.50:
        return ("weakly_identified",
                f"relative SE {rel_se:.1%} — parameter is constrained but "
                f"broadly; consider tighter priors or more data.")
    else:
        return ("unidentified",
                f"relative SE {rel_se:.1%} — the data hardly constrains this "
                f"parameter; effectively free.")


def _run_self_test() -> int:
    failures = []

    def ok(cond, label):
        if not cond:
            failures.append(label)

    # Case A: sharp quadratic in beta, flat in ridge
    def loss_a(p):
        return (p["beta"] - 0.5) ** 2 * 2000 + 0.0 * p["ridge"]

    pt_a = {"beta": 0.5, "ridge": 0.5}
    bnds_a = {"beta": (0.0, 1.0), "ridge": (0.0, 1.0)}
    names_a, H_a = hessian(loss_a, pt_a, bnds_a)
    ok(names_a == ["beta", "ridge"] and H_a.shape == (2, 2) and H_a[0, 0] > 3000, f"A: hessian {H_a}")
    fa_a = fisher_analysis(loss_a, pt_a, bnds_a)
    ridge_a = {n: next((r for r in fa_a["ridges"] if n in r["params"]), None) for n in pt_a}
    prof_beta = profile_likelihood(loss_a, pt_a, {"name": "beta", "lower_bound": 0.0, "upper_bound": 1.0, "profile_n_points": 15}, nuisance_bounds=bnds_a)
    prof_ridge = profile_likelihood(loss_a, pt_a, {"name": "ridge", "lower_bound": 0.0, "upper_bound": 1.0, "profile_n_points": 15}, nuisance_bounds=bnds_a)
    st_beta, _ = classify("beta", 0.5, fa_a["se"]["beta"], prof_beta, ridge_a["beta"])
    st_ridge, _ = classify("ridge", 0.5, fa_a["se"]["ridge"], prof_ridge, ridge_a["ridge"])
    ok(st_beta == "well_identified" and st_ridge == "unidentified",
       f"A: expected beta well_identified & ridge unidentified; got {st_beta}, {st_ridge}")

    # Case B: all well-identified
    def loss_b(p):
        return (p["a"] - 1.0) ** 2 * 5000 + (p["b"] - 2.0) ** 2 * 8000

    pt_b = {"a": 1.0, "b": 2.0}
    bnds_b = {"a": (0.5, 1.5), "b": (1.5, 2.5)}
    fa_b = fisher_analysis(loss_b, pt_b, bnds_b)
    ok(not fa_b["ridges"] and not fa_b["ridge_params"], f"B: no ridges expected; got {fa_b}")
    for nm, (lo, hi) in bnds_b.items():
        prof = profile_likelihood(loss_b, pt_b, {"name": nm, "lower_bound": lo, "upper_bound": hi, "profile_n_points": 9}, nuisance_bounds=bnds_b)
        st, _ = classify(nm, pt_b[nm], fa_b["se"][nm], prof)
        ok(st == "well_identified", f"B: {nm} expected well_identified; got {st}")

    # Case E: ratio-only loss (only beta/gamma is identifiable)
    def loss_e(p):
        return 500 * (p["beta"] / p["gamma"] - 2.0) ** 2

    pt_e = {"beta": 0.4, "gamma": 0.2}
    bnds_e = {"beta": (0.05, 2.0), "gamma": (0.05, 2.0)}
    fa_e = fisher_analysis(loss_e, pt_e, bnds_e)
    ok(any(set(r["params"]) == {"beta", "gamma"} for r in fa_e["ridges"]),
       f"E: ridge should name beta and gamma; got {fa_e['ridges']}")
    ridge_e = {n: next((r for r in fa_e["ridges"] if n in r["params"]), None) for n in pt_e}
    for nm, (lo, hi) in bnds_e.items():
        prof = profile_likelihood(loss_e, pt_e, {"name": nm, "lower_bound": lo, "upper_bound": hi, "profile_n_points": 9}, nuisance_bounds=bnds_e)
        st, _ = classify(nm, pt_e[nm], fa_e["se"][nm], prof, ridge_e[nm])
        ok(st == "unidentified", f"E: {nm} expected unidentified; got {st}")

    # Case F: sum-only loss — full re-optimized profile is flat, conditional slice is not
    def loss_f(p):
        return 10000 * (p["a"] + p["b"] - 2.0) ** 2

    pt_f = {"a": 1.0, "b": 1.0}
    bnds_f = {"a": (0.0, 2.0), "b": (0.0, 2.0)}
    for nm, (lo, hi) in bnds_f.items():
        prof_full = profile_likelihood(loss_f, pt_f, {"name": nm, "lower_bound": lo, "upper_bound": hi, "profile_n_points": 9}, nuisance_bounds=bnds_f, method="full")
        prof_cond = profile_likelihood(loss_f, pt_f, {"name": nm, "lower_bound": lo, "upper_bound": hi, "profile_n_points": 9}, nuisance_bounds=bnds_f, method="conditional")
        ok(prof_full["flat_ratio"] < 1.0 and prof_full["method"] == "full",
           f"F: full profile flat_ratio < 1.0; got {prof_full}")
        ok(prof_cond["flat_ratio"] > 10.0 and prof_cond["method"] == "conditional",
           f"F: conditional slice flat_ratio > 10; got {prof_cond}")

    # Case G: classify() boundary statuses
    st_w, _ = classify("w", 1.0, 0.2, {"flat_ratio": 5.0})
    st_none, _ = classify("u", 1.0, None, {"flat_ratio": 5.0})
    st_big, _ = classify("b", 1.0, 0.8, {"flat_ratio": 5.0})
    ok(st_w == "weakly_identified" and st_none == "unidentified" and st_big == "unidentified",
       f"G: classify boundaries {(st_w, st_none, st_big)}")

    if failures:
        print(f"FAIL: {len(failures)} case(s)", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    print("OK: all self-test cases passed.", file=sys.stderr)
    return 0


def main() -> int:
    if "--self-test" in sys.argv[1:]:
        return _run_self_test()
    print("ERROR: identifiability.py is a library module; use --self-test", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
