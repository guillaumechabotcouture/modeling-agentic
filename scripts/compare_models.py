#!/usr/bin/env python3
"""
Multi-structural model comparison: RMSE, LOO-CV RMSE, AIC, BIC, and the
degenerate-fit detector.

The modeler is required to produce ≥3 candidate model structures (null,
simple, full) and supply a comparison manifest at
`{run_dir}/models/model_comparison.yaml` with fitted results. This tool
reads that manifest, computes information criteria, performs LOO-CV when
possible, and emits `{run_dir}/model_comparison_formal.yaml` with the
gate-relevant fields.

Crucially: it flags DEGENERATE FIT — the "AIC lies" pattern that
masquerades as a great calibration. If training RMSE << LOO-CV RMSE
(ratio > 3×) AND k >= n/2, AIC will prefer the saturated model but LOO
reveals it doesn't generalize. This is the malaria-run failure mode:
22 free EIRs fit to 22 zone targets gives train-RMSE ≈ 0 but LOO-RMSE
equals the null model's.

Input manifest schema (`models/model_comparison.yaml`, written by modeler):

```yaml
n_targets: 22              # number of data points being fit
targets: [0.23, 0.31, ...]  # observed y values, length == n_targets
models:
  - name: null
    k_params: 1
    predictions: [0.28, 0.28, ...]        # training predictions, length == n_targets
    loo_predictions: [0.27, 0.29, ...]    # leave-one-out predictions; OPTIONAL
    description: "Global mean predictor"
  - name: simple
    k_params: 4
    predictions: [...]
    loo_predictions: [...]
    description: "Pooled logistic regression on 4 covariates"
  - name: full
    k_params: 23
    predictions: [...]
    loo_predictions: [...]
    description: "SEIR compartmental surrogate for the archetype ABM"
```

If `loo_predictions` is omitted, this tool skips LOO-CV for that model
(only training-RMSE + AIC/BIC reported). Modeler is strongly encouraged
to compute LOO — it's the main check for degenerate fit.

Output (`model_comparison_formal.yaml`):

```yaml
n_targets: 22
models:
  null:   {k: 1,  rmse_train: 0.146, rmse_loo: 0.153, aic: -20.3, bic: -19.2}
  simple: {k: 4,  rmse_train: 0.087, rmse_loo: 0.100, aic: -37.0, bic: -32.7}
  full:   {k: 23, rmse_train: 1e-17, rmse_loo: 0.153, aic: -499.4, bic: -474.3}
preferred_by_aic: full
preferred_by_loo: simple
aic_vs_loo_divergence: true        # preferred differs
degenerate_fit:
  flagged: true
  model: full
  reason: "k_params (23) >= n_targets (22) / 2; train RMSE is 1e-17
           while LOO RMSE is 0.153 — saturated fit masquerading as AIC winner"
verdict: DEGENERATE_FIT_DETECTED   # CLEAN | DEGENERATE_FIT_DETECTED | INSUFFICIENT_STRUCTURES
```

Usage:
    python3 scripts/compare_models.py <run_dir> [--json] [--self-test]
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from typing import Any

try:
    import yaml
except ImportError:
    print("ERROR: pyyaml not installed.", file=sys.stderr)
    sys.exit(2)


def rmse(observed: list[float], predicted: list[float]) -> float:
    if len(observed) != len(predicted):
        raise ValueError(f"length mismatch: {len(observed)} vs {len(predicted)}")
    if not observed:
        return 0.0
    sse = sum((o - p) ** 2 for o, p in zip(observed, predicted))
    return math.sqrt(sse / len(observed))


def aic_bic(rmse_value: float, k: int, n: int) -> tuple[float, float]:
    """AIC = 2k + n·ln(SSE/n); BIC = k·ln(n) + n·ln(SSE/n). For RMSE-based
    formulation, n·ln(RMSE²) = n·ln(SSE/n). Uses a small-number floor to
    avoid log(0) when RMSE is ~0 from saturated fit."""
    sigma2 = max(rmse_value ** 2, 1e-30)
    log_lik = -0.5 * n * (math.log(2 * math.pi) + math.log(sigma2) + 1)
    aic = 2 * k - 2 * log_lik
    bic = k * math.log(n) - 2 * log_lik
    return aic, bic


def check_degenerate(model: dict) -> dict:
    """A model is degenerately fit if k_params >= n_targets/2 AND
    train RMSE is much smaller than LOO RMSE (>3× gap)."""
    name = model["name"]
    n = model["_n_targets"]
    k = model["k_params"]
    train = model["rmse_train"]
    loo = model.get("rmse_loo")

    if loo is None:
        return {"flagged": False, "reason": "LOO not provided"}

    saturated = k >= n / 2
    if train < 1e-10:
        train_near_zero = True
    else:
        train_near_zero = False
    gap_ratio = float("inf") if train < 1e-10 else loo / train
    train_much_smaller = gap_ratio > 3

    if saturated and (train_much_smaller or train_near_zero):
        return {
            "flagged": True,
            "model": name,
            "reason": (f"k_params ({k}) >= n_targets ({n}) / 2; train RMSE is "
                       f"{train:.3g} while LOO RMSE is {loo:.3g}" +
                       (f" — {gap_ratio:.1f}× gap"
                        if gap_ratio < float("inf") else
                        " — essentially saturated fit")
                       + " — AIC will prefer this model but LOO reveals it "
                       "doesn't generalize. Classic 'AIC lies' pattern: the "
                       "model is effectively interpolating the training points."),
        }
    if train_near_zero:
        return {
            "flagged": True,
            "model": name,
            "reason": (f"train RMSE is {train:.3g} (essentially zero) — "
                       "suspicious regardless of k. Check for trivial "
                       "overfitting (e.g., one free parameter per target)."),
        }
    return {"flagged": False, "reason": None}


def compare(manifest: dict) -> dict:
    """Build the comparison report from a modeler-provided manifest."""
    n_targets = manifest.get("n_targets")
    targets = manifest.get("targets")
    models = manifest.get("models", [])

    if n_targets is None or targets is None:
        raise ValueError("manifest must have 'n_targets' and 'targets'")
    if len(targets) != n_targets:
        raise ValueError(
            f"targets length {len(targets)} != n_targets {n_targets}"
        )
    if len(models) < 3:
        return {
            "n_targets": n_targets,
            "models": {m["name"]: {"error": "fewer than 3 models supplied"}
                       for m in models},
            "verdict": "INSUFFICIENT_STRUCTURES",
            "reason": (f"Modeler supplied {len(models)} candidate structures; "
                       "this tool requires ≥3 (null, simple, full). See the "
                       "multi-structural-comparison skill."),
        }

    results: dict[str, dict] = {}
    for m in models:
        name = m["name"]
        k = int(m["k_params"])
        preds = m.get("predictions", [])
        loo_preds = m.get("loo_predictions")
        if len(preds) != n_targets:
            raise ValueError(f"model {name}: predictions length "
                             f"{len(preds)} != n_targets {n_targets}")
        rmse_train = rmse(targets, preds)
        aic, bic = aic_bic(rmse_train, k, n_targets)
        entry = {
            "k": k,
            "rmse_train": round(rmse_train, 6),
            "aic": round(aic, 3),
            "bic": round(bic, 3),
            "description": m.get("description", ""),
        }
        if loo_preds is not None:
            if len(loo_preds) != n_targets:
                raise ValueError(f"model {name}: loo_predictions length "
                                 f"{len(loo_preds)} != n_targets {n_targets}")
            entry["rmse_loo"] = round(rmse(targets, loo_preds), 6)
        else:
            entry["rmse_loo"] = None
        # Attach n_targets to the model dict for the degeneracy check.
        entry["_n_targets"] = n_targets
        entry["_name"] = name
        results[name] = entry

    # Find preferred models.
    def get_with_loo(d, key):
        return d.get(key) if d.get("rmse_loo") is not None else None

    preferred_by_aic = min(results, key=lambda n: results[n]["aic"])
    loo_available = {n: m for n, m in results.items()
                     if m.get("rmse_loo") is not None}
    preferred_by_loo = None
    if loo_available:
        preferred_by_loo = min(loo_available,
                               key=lambda n: loo_available[n]["rmse_loo"])
    divergence = (preferred_by_loo is not None
                  and preferred_by_aic != preferred_by_loo)

    # Degenerate-fit scan.
    degenerate = {"flagged": False, "reason": None}
    for name, entry in results.items():
        check = check_degenerate({"name": name, "k_params": entry["k"],
                                  "rmse_train": entry["rmse_train"],
                                  "rmse_loo": entry.get("rmse_loo"),
                                  "_n_targets": n_targets})
        if check["flagged"]:
            degenerate = check
            break

    # Clean the internal fields out of the output.
    for entry in results.values():
        entry.pop("_n_targets", None)
        entry.pop("_name", None)

    verdict = ("DEGENERATE_FIT_DETECTED" if degenerate["flagged"] else "CLEAN")

    return {
        "n_targets": n_targets,
        "models": results,
        "preferred_by_aic": preferred_by_aic,
        "preferred_by_loo": preferred_by_loo,
        "aic_vs_loo_divergence": divergence,
        "degenerate_fit": degenerate,
        "verdict": verdict,
    }


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

def _run_self_test() -> int:
    import tempfile
    import random
    failures = []
    def ok(cond, label):
        if not cond:
            failures.append(label)

    rng = random.Random(0)

    # Case A: three candidates, clean (simple preferred).
    targets = [rng.uniform(0.1, 0.5) for _ in range(22)]
    noise = lambda s: [t + rng.gauss(0, s) for t in targets]

    manifest_a = {
        "n_targets": 22,
        "targets": targets,
        "models": [
            {
                "name": "null",
                "k_params": 1,
                "predictions": [sum(targets) / len(targets)] * 22,
                "loo_predictions": [sum(targets) / len(targets)] * 22,
                "description": "Mean",
            },
            {
                "name": "simple",
                "k_params": 4,
                "predictions": noise(0.05),
                "loo_predictions": noise(0.06),
                "description": "4-coef regression",
            },
            {
                "name": "full",
                "k_params": 10,
                "predictions": noise(0.03),
                "loo_predictions": noise(0.07),
                "description": "10-param ABM surrogate",
            },
        ],
    }
    report = compare(manifest_a)
    ok(report["verdict"] == "CLEAN",
       f"A: CLEAN expected; got {report['verdict']}")
    ok(not report["degenerate_fit"]["flagged"],
       "A: no degeneracy")
    ok(set(report["models"].keys()) == {"null", "simple", "full"},
       f"A: 3 models; got {sorted(report['models'])}")

    # Case B: saturated fit.
    manifest_b = {
        "n_targets": 22,
        "targets": targets,
        "models": [
            manifest_a["models"][0],  # null
            manifest_a["models"][1],  # simple
            {
                "name": "saturated",
                "k_params": 22,
                "predictions": targets[:],  # perfect fit
                "loo_predictions": [sum(targets) / len(targets)] * 22,  # LOO falls back to mean
                "description": "One parameter per target",
            },
        ],
    }
    report_b = compare(manifest_b)
    ok(report_b["verdict"] == "DEGENERATE_FIT_DETECTED",
       f"B: DEGENERATE expected; got {report_b['verdict']}")
    ok(report_b["degenerate_fit"]["model"] == "saturated",
       f"B: flagged model should be 'saturated'; got {report_b['degenerate_fit']}")
    ok(report_b["aic_vs_loo_divergence"],
       "B: AIC preferred saturated, LOO preferred simple")

    # Case C: insufficient structures.
    manifest_c = {
        "n_targets": 22,
        "targets": targets,
        "models": [manifest_a["models"][0], manifest_a["models"][1]],  # only 2
    }
    report_c = compare(manifest_c)
    ok(report_c["verdict"] == "INSUFFICIENT_STRUCTURES",
       f"C: INSUFFICIENT expected; got {report_c['verdict']}")

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
    p.add_argument("--output", default=None,
                   help="Output path (default <run_dir>/model_comparison_formal.yaml)")
    args = p.parse_args()

    if args.self_test:
        return _run_self_test()
    if not args.run_dir:
        p.error("run_dir required")

    manifest_path = os.path.join(args.run_dir, "models", "model_comparison.yaml")
    if not os.path.exists(manifest_path):
        print(f"ERROR: {manifest_path} not found. The modeler must produce "
              f"models/model_comparison.yaml with ≥3 candidate structures. "
              f"See the multi-structural-comparison skill.", file=sys.stderr)
        return 2

    with open(manifest_path) as f:
        manifest = yaml.safe_load(f)

    try:
        report = compare(manifest)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    output_path = args.output or os.path.join(
        args.run_dir, "model_comparison_formal.yaml")
    with open(output_path, "w") as f:
        yaml.safe_dump(report, f, default_flow_style=False, sort_keys=False)

    print(f"model_comparison_formal.yaml written: {output_path}", file=sys.stderr)
    print(f"  verdict: {report['verdict']}", file=sys.stderr)
    print(f"  preferred_by_aic: {report.get('preferred_by_aic')}", file=sys.stderr)
    print(f"  preferred_by_loo: {report.get('preferred_by_loo')}", file=sys.stderr)
    if report.get("degenerate_fit", {}).get("flagged"):
        print(f"  DEGENERATE FIT: {report['degenerate_fit']['reason']}",
              file=sys.stderr)
    for name, m in report["models"].items():
        loo = f"{m['rmse_loo']:.4f}" if m.get("rmse_loo") is not None else "N/A"
        print(f"  {name:10s} k={m['k']:3d} "
              f"RMSE_train={m['rmse_train']:.4f} "
              f"RMSE_LOO={loo} "
              f"AIC={m['aic']:.1f}", file=sys.stderr)

    if args.json:
        print(json.dumps(report, indent=2))

    return 0 if report["verdict"] == "CLEAN" else 1


if __name__ == "__main__":
    sys.exit(main())
