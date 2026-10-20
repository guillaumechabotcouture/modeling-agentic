#!/usr/bin/env python3
"""
Identifiability analysis: Fisher-information diagonal + profile-likelihood
scan.

Given a fitted model's loss function (or negative log-likelihood) and point
estimates, this tool reports for each fitted parameter:

- `fisher_se`: standard error via second-derivative of loss at the optimum
  (local curvature).
- `profile_range_95`: the parameter range for which the profile loss stays
  within 1.92 units of the optimum (equivalent to a 95% profile-likelihood
  CI for a chi-squared / normal likelihood).
- `status`: `well_identified` (rel SE < 10%) | `weakly_identified` (10–50%)
  | `unidentified` (SE > 50% OR profile loss flat over >5× the CI range
  OR the parameter loads on a near-null Hessian eigen-direction).

Phase 21 δ corrections (the pre-21 tool reported CLEAN for a loss that
only depends on β/γ, the exact ridge it exists to catch):

- SE now comes from sqrt(diag(F⁻¹)) on the FULL finite-difference
  Hessian, not 1/sqrt(F_ii). The diagonal-only SE is the conditional
  SE holding every other parameter fixed; it cannot see correlated
  ridges.
- Ridges are detected from the eigen-decomposition of the Hessian in
  scale-free (relative) coordinates. Every parameter that loads on a
  near-null / weak direction is `unidentified`, and the output reports
  the parameter combination that is unconstrained.
- The profile likelihood re-optimizes the nuisance parameters at each
  grid point (bounded L-BFGS-B, warm-started, maxiter-capped). Manifest
  `profile: conditional` opts out (cheap slice); the output then records
  `profile_method: conditional` as weaker evidence.
- The 1.92 threshold is only meaningful on a negative-log-likelihood
  scale, so the manifest must declare `loss_type`:
    nll       loss is −log L (up to a constant)
    deviance  loss is −2 log L (chi2 is an alias)
    sse / mse sum / mean of squared residuals; converted to the
              Gaussian profile NLL (n_obs/2)·log(SSE), which requires
              `n_obs`. This makes verdicts invariant to rescaling the
              loss by a constant.
  A missing/unknown loss_type (or sse/mse without n_obs) yields verdict
  UNKNOWN_LOSS_SCALE, which consumers must treat as failing.

The "unidentified / ridge-trapped" status is the diagnostic we want: a
parameter whose value doesn't actually affect the fit. This is the γ
(recovery rate) issue from the malaria structural probe: profile loss was
essentially flat across γ ∈ [0.1, 100] yr⁻¹.

The modeler supplies a manifest at `{run_dir}/models/identifiability.yaml`:

```yaml
loss_fn: "models/outcome_fn.py::loss"   # importable callable: loss(params) -> float
                                          # same module + scheme as outcome_fn.py
loss_type: sse                 # nll | deviance | chi2 | sse | mse (REQUIRED, Phase 21 δ)
n_obs: 22                      # required for sse / mse
profile: full                  # full (default, re-optimizes nuisance) | conditional
parameters:
  - name: beta
    point_estimate: 0.5
    lower_bound: 0.1
    upper_bound: 2.0
    profile_n_points: 20         # number of points in the profile scan
  - name: gamma
    point_estimate: 0.2
    lower_bound: 0.01
    upper_bound: 2.0
  - ...
```

Output: `{run_dir}/identifiability.yaml` and a summary table on stderr.

Usage:
    python3 scripts/identifiability.py <run_dir> [--json]
    python3 scripts/identifiability.py --self-test

Exit:
    0   all parameters well-identified
    1   any parameter flagged weakly_identified or unidentified
    2   manifest missing or loss_fn failed
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import sys
from typing import Callable, Optional

try:
    import yaml
except ImportError:
    print("ERROR: pyyaml not installed.", file=sys.stderr)
    sys.exit(2)


# Chi-squared 95% CI for 1 DOF: 3.84. Profile-likelihood CI uses
# -2 * (logL(profile) - logL(max)) < 3.84, equivalent to loss increase < 1.92.
PROFILE_CI_THRESHOLD = 1.92

# Phase 21 δ: closed verdict vocabulary. The validator allowlists these;
# any other string in identifiability.yaml means the file was not
# produced by this tool (or was hand-edited) and must fail closed.
VERDICTS = (
    "CLEAN",
    "WEAKLY_IDENTIFIED_PARAMETERS",
    "UNIDENTIFIED_PARAMETERS",
    "UNKNOWN_LOSS_SCALE",
)
PASSING_VERDICTS = ("CLEAN",)
PRODUCER = "scripts/identifiability.py"

LOSS_TYPES_NLL_FACTOR = {"nll": 1.0, "deviance": 0.5, "chi2": 0.5}
LOSS_TYPES_SQUARED = ("sse", "mse")

# Ridge detection in relative coordinates: a Hessian eigen-direction
# whose implied relative SE exceeds this is unconstrained (same 50% cut
# classify() uses for single parameters). Eigenvalues below
# RIDGE_EIG_RTOL·λ_max are treated as numerically zero.
RIDGE_REL_SE = 0.50
RIDGE_EIG_RTOL = 1e-8
RIDGE_LOADING_MIN = 0.30
PROFILE_MAXITER = 100


class LossFunctionReoptimizesError(Exception):
    """Raised when the modeler-supplied loss function appears to internally
    re-optimize all parameters rather than evaluating residuals at the
    EXACT supplied parameter values. The diagnostic: perturbing any
    parameter produces no measurable change in the returned loss, because
    the inner optimizer compensates. Profile-likelihood scans on such a
    function are meaningless — every profile is flat by construction,
    yielding false-positive ridge-trapped verdicts.

    Phase 9 Commit σ: two consecutive malaria runs (1721, 0013) wasted
    a HIGH RIG-001 blocker on this code artifact. The fix is to detect
    re-optimizing loss at load time, before profile_likelihood() is
    called, and tell the modeler explicitly to rewrite calibration_loss
    as a pointwise function (no inner solver/calibrator/fitter)."""


def load_loss_fn(run_dir: str, spec: str) -> Callable[[dict], float]:
    """Load the modeler-provided loss function. spec format matches
    propagate_uncertainty's outcome_fn: 'path/file.py::fn_name'."""
    if "::" in spec:
        rel, fn_name = spec.split("::", 1)
    else:
        rel, fn_name = spec, "loss"
    path = os.path.join(run_dir, rel)
    if not os.path.exists(path) and os.path.exists(rel):
        path = rel
    if not os.path.exists(path):
        raise FileNotFoundError(f"loss_fn source not found at {path}")

    module_spec = importlib.util.spec_from_file_location("_loss_mod", path)
    module = importlib.util.module_from_spec(module_spec)
    mod_dir = os.path.dirname(os.path.abspath(path))
    if mod_dir not in sys.path:
        sys.path.insert(0, mod_dir)
    module_spec.loader.exec_module(module)
    fn = getattr(module, fn_name, None)
    if fn is None or not callable(fn):
        raise AttributeError(f"{path}: no callable {fn_name}")
    return fn


def resolve_loss_scale(manifest: dict) -> tuple[Optional[Callable[[float], float]], Optional[str]]:
    """Phase 21 δ: map the manifest's `loss_type` to a transform that puts
    the loss on a negative-log-likelihood scale (up to an additive
    constant), so PROFILE_CI_THRESHOLD and Fisher SEs mean what they
    say. Returns (transform, None) or (None, reason) when the scale is
    unknown — the caller then reports UNKNOWN_LOSS_SCALE."""
    lt = manifest.get("loss_type")
    if lt is None:
        return None, ("manifest has no `loss_type` (nll | deviance | chi2 | "
                      "sse | mse); the 1.92 profile threshold and Fisher SE "
                      "are only meaningful on a −log L scale")
    lt = str(lt).strip().lower()
    if lt in LOSS_TYPES_NLL_FACTOR:
        k = LOSS_TYPES_NLL_FACTOR[lt]
        return (lambda v, k=k: k * v), None
    if lt in LOSS_TYPES_SQUARED:
        n_obs = manifest.get("n_obs")
        try:
            n_obs = int(n_obs)
        except (TypeError, ValueError):
            n_obs = None
        if not n_obs or n_obs <= 0:
            return None, (f"loss_type {lt!r} requires a positive integer "
                          f"`n_obs` to convert to a Gaussian profile NLL")
        # Gaussian errors with σ profiled out: −log L = (n/2)·log(SSE) + c.
        # mse = SSE/n differs only by a constant inside the log. This form
        # is invariant to multiplying the loss by any constant.
        return (lambda v, n=n_obs: 0.5 * n * math.log(max(v, 1e-300))), None
    return None, (f"loss_type {lt!r} not recognized; use one of "
                  f"{sorted(list(LOSS_TYPES_NLL_FACTOR) + list(LOSS_TYPES_SQUARED))}")


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


def fisher_diagonal(loss_fn: Callable[[dict], float],
                    point: dict[str, float],
                    step_frac: float = 0.01) -> dict[str, Optional[float]]:
    """Conditional SE (1/sqrt(F_ii), all other parameters held fixed).
    Retained for reference only — Phase 21 δ: this ignores correlations
    and must not be used for classification; see fisher_analysis()."""
    f0 = loss_fn(point)
    out = {}
    for name, p0 in point.items():
        h = max(abs(p0) * step_frac, 1e-6)
        plus = dict(point); plus[name] = p0 + h
        minus = dict(point); minus[name] = p0 - h
        d2 = (loss_fn(plus) - 2 * f0 + loss_fn(minus)) / (h * h)
        out[name] = None if d2 <= 1e-12 else 1.0 / math.sqrt(d2)
    return out


def assert_loss_fn_is_pointwise(loss_fn: Callable[[dict], float],
                                point: dict[str, float],
                                eps_frac: float = 0.05,
                                tol: float = 1e-9) -> None:
    """Detect re-optimizing loss functions before profile_likelihood runs.

    A pointwise loss must produce a measurable change when AT LEAST one
    parameter is perturbed by a non-trivial fraction of its point value.
    A re-optimizing loss (e.g., one that internally re-fits all parameters
    via scipy.optimize.minimize before computing the residual) returns
    near-identical values for every perturbation, because the inner solver
    compensates. profile_likelihood() on such a loss yields flat curves
    for every parameter, and classify() then mis-flags every parameter
    as ridge-trapped — RIG-001 false-positive HIGH.

    Raises LossFunctionReoptimizesError if the maximum |Δloss| across
    all single-parameter perturbations is below `tol`. Tolerance is
    deliberately tight (1e-9) so genuine numerical noise from a true
    pointwise function still passes.
    """
    f0 = loss_fn(point)
    max_change = 0.0
    n_evaluated = 0
    for name, p0 in point.items():
        h = max(abs(p0) * eps_frac, 1e-3)
        plus = dict(point); plus[name] = p0 + h
        try:
            f1 = loss_fn(plus)
        except Exception:
            # An exception on perturbation is fine — what we care about
            # is whether ANY perturbation moves the loss. A loss that
            # raises on every perturbation is its own problem and will
            # surface at the profile_likelihood call site.
            continue
        n_evaluated += 1
        delta = abs(f1 - f0) if math.isfinite(f1) and math.isfinite(f0) else 0.0
        if delta > max_change:
            max_change = delta

    if n_evaluated == 0:
        # Every perturbation raised — let profile_likelihood report that;
        # not our concern here.
        return

    if max_change < tol:
        raise LossFunctionReoptimizesError(
            "Loss function returns identical values (max |Δloss| = "
            f"{max_change:g} < {tol:g}) for every parameter perturbation "
            f"of {n_evaluated} parameter(s). This indicates the loss "
            "function is internally re-optimizing all parameters rather "
            "than evaluating residuals at the supplied values. Rewrite "
            "calibration_loss(params: dict) -> float as a POINTWISE "
            "function: compute residuals at the EXACT supplied parameter "
            "values WITHOUT calling any optimizer / calibrator / fitter "
            "inside. If your model has no analytic residual (e.g., loss "
            "is only defined as the output of a calibration loop), "
            "declare scope and skip identifiability analysis — DO NOT "
            "submit a re-optimizing loss, which produces guaranteed "
            "false-positive ridge-trapped verdicts."
        )


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
    warm = {}
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


def analyze(run_dir: str) -> dict:
    manifest_path = os.path.join(run_dir, "models", "identifiability.yaml")
    if not os.path.exists(manifest_path):
        raise FileNotFoundError(
            f"{manifest_path} not found. Modeler must supply the manifest "
            f"(see the identifiability-analysis skill)."
        )
    with open(manifest_path) as f:
        manifest = yaml.safe_load(f) or {}
    if not isinstance(manifest, dict):
        raise ValueError("manifest must be a YAML mapping")

    loss_spec = manifest.get("loss_fn")
    if not loss_spec:
        raise ValueError("manifest missing `loss_fn:` (path::fn_name)")
    raw_loss_fn = load_loss_fn(run_dir, loss_spec)

    params_manifest = manifest.get("parameters", [])
    if not isinstance(params_manifest, list) or not params_manifest:
        raise ValueError("manifest `parameters:` must be a non-empty list")
    for p in params_manifest:
        if not isinstance(p, dict) or "name" not in p or "point_estimate" not in p:
            raise ValueError("each parameter needs `name` and `point_estimate`")

    # Build point dict for Fisher calc.
    point = {p["name"]: float(p["point_estimate"]) for p in params_manifest}
    bounds = {}
    for p in params_manifest:
        if "lower_bound" in p and "upper_bound" in p:
            bounds[p["name"]] = (float(p["lower_bound"]), float(p["upper_bound"]))

    # Phase 9 Commit σ: catch re-optimizing loss before profile_likelihood
    # runs. A loss that internally re-fits compensates for every
    # perturbation, producing flat profiles for every parameter. Surface
    # this as an explicit error — telling the modeler to rewrite — rather
    # than letting it cascade into a fleet of false-positive ridge-trapped
    # verdicts.
    assert_loss_fn_is_pointwise(raw_loss_fn, point)

    # Phase 21 δ: put the loss on an NLL scale. Unknown scale → we still
    # compute diagnostics in raw units (ridge detection in relative
    # coordinates is scale-free) but the verdict is UNKNOWN_LOSS_SCALE.
    transform, scale_err = resolve_loss_scale(manifest)
    if transform is None:
        loss_fn = raw_loss_fn
    else:
        loss_fn = lambda q, _f=raw_loss_fn, _t=transform: _t(_f(q))

    fa = fisher_analysis(loss_fn, point, bounds)
    fisher = fa["se"]
    ridge_for: dict[str, dict] = {}
    for r in fa["ridges"]:
        for n in r["params"]:
            ridge_for.setdefault(n, r)

    profile_method = str(manifest.get("profile", "full")).strip().lower()
    if profile_method not in ("full", "conditional"):
        profile_method = "full"

    param_results = {}
    status_counts = {"well_identified": 0, "weakly_identified": 0,
                     "unidentified": 0}
    methods_used = set()
    for p in params_manifest:
        name = p["name"]
        pe = float(p["point_estimate"])
        if name in bounds:
            prof = profile_likelihood(
                loss_fn, point, p, nuisance_bounds=bounds,
                method=str(p.get("profile", profile_method)).lower())
        else:
            prof = {"ci_low": None, "ci_high": None, "flat_ratio": None,
                    "method": "none"}
        methods_used.add(prof.get("method"))
        status, explanation = classify(name, pe, fisher.get(name), prof,
                                       ridge_for.get(name))
        status_counts[status] = status_counts.get(status, 0) + 1
        param_results[name] = {
            "point_estimate": pe,
            "fisher_se": (round(fisher[name], 6)
                          if fisher.get(name) is not None else None),
            "relative_se": (round(fisher[name] / abs(pe), 4)
                            if fisher.get(name) is not None and abs(pe) > 1e-12
                            else None),
            "profile_ci_low": prof.get("ci_low"),
            "profile_ci_high": prof.get("ci_high"),
            "profile_flat_ratio": (round(prof["flat_ratio"], 3)
                                   if prof.get("flat_ratio") is not None
                                   and math.isfinite(prof["flat_ratio"])
                                   else None),
            "profile_method": prof.get("method"),
            "status": status,
            "explanation": explanation,
        }

    if status_counts.get("unidentified", 0) > 0:
        verdict = "UNIDENTIFIED_PARAMETERS"
    elif scale_err is not None:
        verdict = "UNKNOWN_LOSS_SCALE"
    elif status_counts.get("weakly_identified", 0) > 0:
        verdict = "WEAKLY_IDENTIFIED_PARAMETERS"
    else:
        verdict = "CLEAN"
    assert verdict in VERDICTS

    return {
        "producer": PRODUCER,
        "manifest_sha256": _sha256_file(manifest_path),
        "n_parameters": len(params_manifest),
        "loss_type": manifest.get("loss_type"),
        "loss_scale_error": scale_err,
        "se_method": "sqrt(diag(pinv(Hessian)))",
        "profile_method": ("conditional" if methods_used <= {"conditional", "none"}
                           and "conditional" in methods_used else
                           "full" if methods_used <= {"full", "none"} else "mixed"),
        "evidence_strength": ("conditional_profile"
                              if "conditional" in methods_used else "full"),
        "hessian_condition_number": (None if fa.get("condition_number") is None
                                     or not math.isfinite(fa["condition_number"])
                                     else round(fa["condition_number"], 3)),
        "ridges": fa["ridges"],
        "parameters": param_results,
        "status_counts": status_counts,
        "verdict": verdict,
    }


def _sha256_file(path: str) -> Optional[str]:
    import hashlib
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def _run_self_test() -> int:
    import tempfile
    failures = []
    def ok(cond, label):
        if not cond:
            failures.append(label)

    # Case A: well-identified quadratic loss.
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "models", "loss.py"), "w") as f:
            f.write(
                "def loss(p):\n"
                "    # sharp quadratic in beta, flat in ridge\n"
                "    beta = p['beta']\n"
                "    ridge = p['ridge']\n"
                "    return (beta - 0.5) ** 2 * 2000 + 0.0 * ridge\n"
            )
        with open(os.path.join(d, "models", "identifiability.yaml"), "w") as f:
            f.write(
                "loss_fn: models/loss.py::loss\n"
                "loss_type: nll\n"
                "parameters:\n"
                "  - name: beta\n"
                "    point_estimate: 0.5\n"
                "    lower_bound: 0.0\n"
                "    upper_bound: 1.0\n"
                "    profile_n_points: 15\n"
                "  - name: ridge\n"
                "    point_estimate: 0.5\n"
                "    lower_bound: 0.0\n"
                "    upper_bound: 1.0\n"
                "    profile_n_points: 15\n"
            )
        report = analyze(d)
        ok(report["parameters"]["beta"]["status"] == "well_identified",
           f"A: beta well_identified; got {report['parameters']['beta']['status']}")
        ok(report["parameters"]["ridge"]["status"] == "unidentified",
           f"A: ridge unidentified; got {report['parameters']['ridge']['status']}")
        ok(report["verdict"] == "UNIDENTIFIED_PARAMETERS",
           f"A: verdict; got {report['verdict']}")

    # Case B: all well-identified.
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "models", "loss.py"), "w") as f:
            f.write(
                "def loss(p):\n"
                "    a = p['a']; b = p['b']\n"
                "    return (a - 1.0) ** 2 * 5000 + (b - 2.0) ** 2 * 8000\n"
            )
        with open(os.path.join(d, "models", "identifiability.yaml"), "w") as f:
            f.write(
                "loss_fn: models/loss.py::loss\n"
                "loss_type: nll\n"
                "parameters:\n"
                "  - name: a\n"
                "    point_estimate: 1.0\n"
                "    lower_bound: 0.5\n"
                "    upper_bound: 1.5\n"
                "  - name: b\n"
                "    point_estimate: 2.0\n"
                "    lower_bound: 1.5\n"
                "    upper_bound: 2.5\n"
            )
        report = analyze(d)
        ok(report["verdict"] == "CLEAN",
           f"B: verdict CLEAN; got {report['verdict']}")

    # Case C (Phase 9 Commit σ): re-optimizing loss must be caught at
    # load time. Two consecutive malaria runs (1721 and 0013) wasted a
    # HIGH RIG-001 blocker on this exact pattern: calibration_loss()
    # internally re-fits all parameters, returning a constant minimum
    # value regardless of input. We simulate that here with a constant-
    # returning loss; assert_loss_fn_is_pointwise must raise.
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "models", "loss.py"), "w") as f:
            f.write(
                "def loss(p):\n"
                "    # Mimics a calibration_loss() that re-fits internally:\n"
                "    # always returns the same minimum regardless of inputs.\n"
                "    return 0.42\n"
            )
        with open(os.path.join(d, "models", "identifiability.yaml"), "w") as f:
            f.write(
                "loss_fn: models/loss.py::loss\n"
                "loss_type: nll\n"
                "parameters:\n"
                "  - name: a\n"
                "    point_estimate: 1.0\n"
                "    lower_bound: 0.5\n"
                "    upper_bound: 1.5\n"
                "  - name: b\n"
                "    point_estimate: 2.0\n"
                "    lower_bound: 1.5\n"
                "    upper_bound: 2.5\n"
            )
        raised = False
        try:
            analyze(d)
        except LossFunctionReoptimizesError:
            raised = True
        ok(raised,
           "C: reoptimizing (constant) loss should raise "
           "LossFunctionReoptimizesError; analyze() did not raise")

    # Case D (Phase 9 Commit σ): partial flat-ridge — sharp in one
    # parameter, truly flat in another — must NOT raise. Only ALL-flat
    # losses indicate re-optimization; a genuine ridge in one parameter
    # is exactly what classify() is designed to detect downstream.
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "models", "loss.py"), "w") as f:
            f.write(
                "def loss(p):\n"
                "    # Sharp in beta, flat in ridge — the canonical\n"
                "    # ridge-trapped pattern that profile_likelihood is\n"
                "    # designed to flag downstream.\n"
                "    beta = p['beta']\n"
                "    ridge = p['ridge']\n"
                "    return (beta - 0.5) ** 2 * 2000 + 0.0 * ridge\n"
            )
        with open(os.path.join(d, "models", "identifiability.yaml"), "w") as f:
            f.write(
                "loss_fn: models/loss.py::loss\n"
                "loss_type: nll\n"
                "parameters:\n"
                "  - name: beta\n"
                "    point_estimate: 0.5\n"
                "    lower_bound: 0.0\n"
                "    upper_bound: 1.0\n"
                "  - name: ridge\n"
                "    point_estimate: 0.5\n"
                "    lower_bound: 0.0\n"
                "    upper_bound: 1.0\n"
            )
        # Should NOT raise — the sharp beta dimension proves the loss
        # is pointwise; ridge is genuinely unidentified, which is fine.
        report = analyze(d)
        ok(report["parameters"]["beta"]["status"] == "well_identified",
           f"D: partial-ridge beta should be well_identified; got "
           f"{report['parameters']['beta']['status']}")
        ok(report["parameters"]["ridge"]["status"] == "unidentified",
           f"D: partial-ridge ridge should still classify as unidentified; "
           f"got {report['parameters']['ridge']['status']}")

    # Phase 21 δ helpers.
    def _run(loss_src: str, params: list[tuple], extra: str = "") -> dict:
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "models"))
            with open(os.path.join(d, "models", "loss.py"), "w") as f:
                f.write(loss_src)
            lines = ["loss_fn: models/loss.py::loss"]
            if extra:
                lines.append(extra.rstrip("\n"))
            lines.append("parameters:")
            for name, pe, lo, hi in params:
                lines += [f"  - name: {name}", f"    point_estimate: {pe}",
                          f"    lower_bound: {lo}", f"    upper_bound: {hi}",
                          "    profile_n_points: 9"]
            with open(os.path.join(d, "models", "identifiability.yaml"), "w") as f:
                f.write("\n".join(lines) + "\n")
            return analyze(d)

    # Case E (Phase 21 δ): only the ratio β/γ is identifiable. The pre-21
    # diagonal-Fisher + conditional-slice tool returned CLEAN here.
    rE = _run("def loss(p):\n    return 500 * (p['beta'] / p['gamma'] - 2.0) ** 2\n",
              [("beta", 0.4, 0.05, 2.0), ("gamma", 0.2, 0.05, 2.0)],
              "loss_type: nll")
    ok(rE["verdict"] == "UNIDENTIFIED_PARAMETERS",
       f"E: ratio-only loss must be UNIDENTIFIED; got {rE['verdict']}")
    ok(all(rE["parameters"][n]["status"] == "unidentified"
           for n in ("beta", "gamma")),
       f"E: both ratio params unidentified; got "
       f"{[rE['parameters'][n]['status'] for n in ('beta', 'gamma')]}")
    ok(any(set(r["params"]) == {"beta", "gamma"} for r in rE["ridges"]),
       f"E: ridge should name the beta/gamma combination; got {rE['ridges']}")

    # Case F (Phase 21 δ): sum-only loss — a+b identified, a and b not.
    rF = _run("def loss(p):\n    return 10000 * (p['a'] + p['b'] - 2.0) ** 2\n",
              [("a", 1.0, 0.0, 2.0), ("b", 1.0, 0.0, 2.0)], "loss_type: nll")
    ok(rF["verdict"] == "UNIDENTIFIED_PARAMETERS",
       f"F: sum-only loss must be UNIDENTIFIED; got {rF['verdict']}")
    ok(all(rF["parameters"][n]["profile_flat_ratio"] is not None
           and rF["parameters"][n]["profile_flat_ratio"] < 1.0
           for n in ("a", "b")),
       f"F: re-optimized profiles must be flat; got "
       f"{[rF['parameters'][n]['profile_flat_ratio'] for n in ('a', 'b')]}")

    # Case G (Phase 21 δ): SSE verdicts invariant to rescaling the loss.
    sse_src = ("XS = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9]\n"
               "YS = [1.1, 2.9, 5.2, 6.8, 9.1, 11.0, 12.8, 15.2, 17.1, 18.9]\n"
               "SCALE = {c}\n"
               "def loss(p):\n"
               "    return SCALE * sum((y - (p['a'] + p['b'] * x)) ** 2\n"
               "                       for x, y in zip(XS, YS))\n")
    pg = [("a", 1.04, 0.0, 3.0), ("b", 1.99, 1.0, 3.0)]
    rG1 = _run(sse_src.format(c=1.0), pg, "loss_type: sse\nn_obs: 10")
    rG2 = _run(sse_src.format(c=2000.0), pg, "loss_type: sse\nn_obs: 10")
    ok(rG1["verdict"] == rG2["verdict"],
       f"G: verdict must not depend on loss scale; {rG1['verdict']} vs {rG2['verdict']}")
    ok(all(rG1["parameters"][n]["status"] == rG2["parameters"][n]["status"]
           and abs((rG1["parameters"][n]["relative_se"] or 0)
                   - (rG2["parameters"][n]["relative_se"] or 0)) < 1e-3
           for n in ("a", "b")),
       f"G: per-param status/SE must be scale-invariant; "
       f"{rG1['parameters']} vs {rG2['parameters']}")
    ok(rG1["parameters"]["b"]["status"] == "well_identified",
       f"G: slope well identified; got {rG1['parameters']['b']['status']}")

    # Case H (Phase 21 δ): missing loss_type → UNKNOWN_LOSS_SCALE (fails).
    rH = _run("def loss(p):\n    return (p['a'] - 1) ** 2 * 5000\n",
              [("a", 1.0, 0.5, 1.5)])
    ok(rH["verdict"] == "UNKNOWN_LOSS_SCALE",
       f"H: no loss_type must be UNKNOWN_LOSS_SCALE; got {rH['verdict']}")
    rH2 = _run("def loss(p):\n    return (p['a'] - 1) ** 2 * 5000\n",
               [("a", 1.0, 0.5, 1.5)], "loss_type: sse")
    ok(rH2["verdict"] == "UNKNOWN_LOSS_SCALE" and rH2["loss_scale_error"],
       f"H2: sse without n_obs must be UNKNOWN_LOSS_SCALE; got {rH2['verdict']}")

    # Case I (Phase 21 δ): conditional opt-out is recorded as weaker evidence.
    rI = _run("def loss(p):\n    return (p['a'] - 1) ** 2 * 5000 + (p['b'] - 2) ** 2 * 8000\n",
              [("a", 1.0, 0.5, 1.5), ("b", 2.0, 1.5, 2.5)],
              "loss_type: nll\nprofile: conditional")
    ok(rI["verdict"] == "CLEAN" and rI["evidence_strength"] == "conditional_profile",
       f"I: conditional profile CLEAN with weaker evidence; got "
       f"{rI['verdict']} / {rI['evidence_strength']}")
    ok(set(VERDICTS) >= {rE["verdict"], rG1["verdict"], rH["verdict"], rI["verdict"]}
       and rI["producer"] == PRODUCER,
       "I2: verdicts within closed VERDICTS set and producer stamped")

    if failures:
        print(f"FAIL: {len(failures)} case(s)", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    print("OK: all self-test cases passed.", file=sys.stderr)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("run_dir", nargs="?")
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--output", default=None)
    args = p.parse_args()

    if args.self_test:
        return _run_self_test()
    if not args.run_dir:
        p.error("run_dir required")

    try:
        report = analyze(args.run_dir)
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    except (ValueError, AttributeError, ImportError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        return 2

    out_path = args.output or os.path.join(args.run_dir, "identifiability.yaml")
    with open(out_path, "w") as f:
        yaml.safe_dump(report, f, default_flow_style=False, sort_keys=False)

    print(f"identifiability.yaml written: {out_path}", file=sys.stderr)
    print(f"  verdict: {report['verdict']}", file=sys.stderr)
    for name, r in report["parameters"].items():
        se = r.get("relative_se")
        se_str = f"rel SE {se:.1%}" if se is not None else "rel SE n/a"
        print(f"  {name:15s} {r['status']:20s} {se_str:14s} — {r['explanation']}",
              file=sys.stderr)

    if args.json:
        print(json.dumps(report, indent=2))

    return 0 if report["verdict"] == "CLEAN" else 1


if __name__ == "__main__":
    sys.exit(main())
