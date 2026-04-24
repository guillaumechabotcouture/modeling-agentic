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
  | `unidentified` (SE > 50% OR profile loss flat over >5× the CI range).

The "unidentified / ridge-trapped" status is the diagnostic we want: a
parameter whose value doesn't actually affect the fit. This is the γ
(recovery rate) issue from the malaria structural probe: profile loss was
essentially flat across γ ∈ [0.1, 100] yr⁻¹.

The modeler supplies a manifest at `{run_dir}/models/identifiability.yaml`:

```yaml
loss_fn: "models/outcome_fn.py::loss"   # importable callable: loss(params) -> float
                                          # same module + scheme as outcome_fn.py
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


def fisher_diagonal(loss_fn: Callable[[dict], float],
                    point: dict[str, float],
                    step_frac: float = 0.01) -> dict[str, Optional[float]]:
    """Approximate Fisher information diagonal via central differences on
    the loss (treating loss as 2·NLL, i.e., deviance-like).

    Returns {name: se} where se = 1 / sqrt(0.5 * d²loss/dp²) on a
    quadratic approximation. Returns None for parameters where the
    second derivative is non-positive (not a minimum, so SE ill-defined)."""
    f0 = loss_fn(point)
    out = {}
    for name, p0 in point.items():
        if abs(p0) < 1e-12:
            h = max(step_frac, 1e-6)
        else:
            h = max(abs(p0) * step_frac, 1e-6)
        plus = dict(point); plus[name] = p0 + h
        minus = dict(point); minus[name] = p0 - h
        fp = loss_fn(plus)
        fm = loss_fn(minus)
        d2 = (fp - 2 * f0 + fm) / (h * h)
        # For loss = NLL (or deviance), d²/dp² ≈ 1/σ²; we interpret loss as
        # (relative) log-likelihood and report SE = 1/sqrt(d²loss/dp²).
        # If loss is NLL, this is the textbook Fisher SE.
        if d2 <= 1e-12:
            out[name] = None
        else:
            out[name] = 1.0 / math.sqrt(d2)
    return out


def profile_likelihood(loss_fn: Callable[[dict], float],
                       point: dict[str, float],
                       param: dict,
                       n_points: int = 20) -> dict:
    """Scan the loss function while varying one parameter from lower_bound
    to upper_bound, holding others at their point estimates. Returns a
    list of (param_value, loss) pairs plus a 95% CI range where the
    profile loss stays within PROFILE_CI_THRESHOLD of the minimum."""
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

    curve = []
    for v in grid:
        p = dict(point); p[name] = v
        try:
            loss = loss_fn(p)
        except Exception as e:
            loss = float("inf")
        curve.append((v, loss))

    loss_values = [l for _, l in curve if math.isfinite(l)]
    if not loss_values:
        return {"curve": curve, "ci_low": None, "ci_high": None,
                "flat_ratio": float("nan")}
    min_loss = min(loss_values)
    threshold = min_loss + PROFILE_CI_THRESHOLD

    # Find the widest contiguous range where loss ≤ threshold.
    within = [v for v, l in curve if math.isfinite(l) and l <= threshold]
    ci_low = min(within) if within else None
    ci_high = max(within) if within else None

    # Flat-ridge metric: max loss in the scan - min loss, expressed as
    # fraction of the expected likelihood "curvature" over the range.
    # If the full scan spans ~1.92 units total, the parameter is hardly
    # constrained at all → ridge-trapped.
    total_range = max(loss_values) - min_loss
    flat_ratio = total_range / PROFILE_CI_THRESHOLD if total_range > 0 else 0

    return {
        "curve": curve,
        "min_loss": min_loss,
        "ci_low": ci_low,
        "ci_high": ci_high,
        "flat_ratio": flat_ratio,
    }


def classify(name: str, point_estimate: float, fisher_se: Optional[float],
             profile: dict) -> tuple[str, str]:
    """Classify the parameter's identifiability status and write one line
    explaining why. Returns (status, explanation)."""
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
            f"(see the identifiability skill — to be added)."
        )
    with open(manifest_path) as f:
        manifest = yaml.safe_load(f) or {}

    loss_spec = manifest.get("loss_fn")
    if not loss_spec:
        raise ValueError("manifest missing `loss_fn:` (path::fn_name)")
    loss_fn = load_loss_fn(run_dir, loss_spec)

    params_manifest = manifest.get("parameters", [])
    if not params_manifest:
        raise ValueError("manifest missing `parameters:` list")

    # Build point dict for Fisher calc.
    point = {p["name"]: float(p["point_estimate"]) for p in params_manifest}

    fisher = fisher_diagonal(loss_fn, point)

    param_results = {}
    status_counts = {"well_identified": 0, "weakly_identified": 0,
                     "unidentified": 0}
    for p in params_manifest:
        name = p["name"]
        pe = float(p["point_estimate"])
        prof = profile_likelihood(loss_fn, point, p)
        status, explanation = classify(name, pe, fisher.get(name), prof)
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
                                   if prof.get("flat_ratio") is not None else None),
            "status": status,
            "explanation": explanation,
        }

    return {
        "n_parameters": len(params_manifest),
        "parameters": param_results,
        "status_counts": status_counts,
        "verdict": ("CLEAN"
                    if status_counts.get("unidentified", 0) == 0
                    and status_counts.get("weakly_identified", 0) == 0
                    else ("UNIDENTIFIED_PARAMETERS"
                          if status_counts.get("unidentified", 0) > 0
                          else "WEAKLY_IDENTIFIED_PARAMETERS")),
    }


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
                "    return (a - 1.0) ** 2 * 50 + (b - 2.0) ** 2 * 80\n"
            )
        with open(os.path.join(d, "models", "identifiability.yaml"), "w") as f:
            f.write(
                "loss_fn: models/loss.py::loss\n"
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
