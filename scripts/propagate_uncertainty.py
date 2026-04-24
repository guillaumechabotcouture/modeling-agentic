#!/usr/bin/env python3
"""
Propagate parameter uncertainty from the effect-size registry through an
outcome function.

Reads the `## Parameter Registry` section of `{run_dir}/citations.md`
(Commit A), samples N draws from each registered parameter's prior (using
`scripts/effect_size_registry.py::sample_prior`), runs the modeler-provided
`outcome_fn` (from `{run_dir}/models/outcome_fn.py`) once per draw, and
emits `{run_dir}/uncertainty_report.yaml` with per-output credible
intervals.

The modeler must expose `outcome_fn` as a callable:

    def outcome_fn(params: dict) -> dict:
        # params is a dict of {parameter_name: sampled_value}
        # returns a dict of {output_name: scalar_or_str}
        # scalar outputs aggregate into CIs; str outputs aggregate into
        # stability distributions (e.g., "which package per archetype")

If the full model is too slow for direct 100+ draws, the modeler is
expected to build a surrogate (see the `uncertainty-quantification` skill)
and expose it as `outcome_fn`. This script does not care whether
`outcome_fn` wraps the full ABM or an emulator — it just requires that
the function is deterministic given its inputs and returns a dict.

Output shape (uncertainty_report.yaml):

    n_draws: 200
    scalar_outputs:
      total_dalys_averted:
        mean: 7.41e6
        ci_low: 5.45e6
        ci_high: 9.66e6
        n: 200
      ...
    categorical_outputs:  # e.g., which package per archetype
      package_A1:
        counts:
          itn_pbo_80: 193
          itn_dual_80: 7
        dominant: itn_pbo_80
        dominance: 0.965
    parameter_samples:    # what values were drawn
      irs_odds_ratio:
        mean: 0.352
        ci_low: 0.278
        ci_high: 0.441

Usage:
    python3 scripts/propagate_uncertainty.py <run_dir> \\
        [--n-draws 200] [--repo-root .] [--seed 42] \\
        [--outcome-fn models/outcome_fn.py::outcome_fn]

Exit:
    0   report written
    1   outcome_fn missing or raised on sample
    2   registry error
    3   no parameters registered (nothing to propagate)
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import random
import sys
import traceback
from collections import Counter
from statistics import mean
from typing import Any, Callable, Optional

try:
    import yaml
except ImportError:
    print("ERROR: pyyaml not installed.", file=sys.stderr)
    sys.exit(2)


def _import_sibling_module(name: str):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    return __import__(name)


def _propagate_cloud(
        run_dir: str, params_list: list, outcome_fn_spec: Optional[str],
        pool_name: str, vm_size: str, max_nodes: int,
        use_low_priority: bool, budget_usd_cap: float,
        verbose: bool = False) -> list:
    """Submit each params dict as a task to an Azure Batch pool.

    Packages `{run_dir}/models/` as a tarball so workers can import the
    user's outcome_fn module. Returns a list of outcome_fn outputs (or
    {'_error': ...} for tasks that raised).

    Assumes outcome_fn is importable at module scope in the models/ dir.
    For default spec `models/outcome_fn.py::outcome_fn`, the qualified
    name pickle records is `outcome_fn.outcome_fn`, which the worker
    resolves after adding `./models/` to sys.path.
    """
    cloud = _import_sibling_module("cloud_batch")

    models_dir = os.path.join(run_dir, "models")
    if not os.path.isdir(models_dir):
        raise FileNotFoundError(
            f"No models/ directory at {models_dir}. Cloud UQ requires a "
            f"models/ directory containing outcome_fn.py (the code the "
            f"worker imports).")

    # Resolve outcome_fn locally so we have a picklable handle. The module
    # must be at the qualified name the worker will resolve (typically the
    # basename of the outcome_fn file).
    fn = load_outcome_fn(run_dir, outcome_fn_spec)

    runner = cloud.BatchRunner()
    try:
        runner._require_config()
    except RuntimeError as e:
        raise RuntimeError(
            f"Cloud propagation requires AZ_* env vars: {e}. Set them in "
            f"the repo .env or export before running."
        )

    # Provision the pool if absent. Always use dedicated nodes unless the
    # caller explicitly sets low-priority (Free Trial subs have 0 quota).
    runner.ensure_pool(
        pool_name=pool_name,
        vm_size=vm_size,
        max_nodes=max_nodes,
        use_low_priority=use_low_priority,
        auto_scale=False,
        dedicated_nodes=(0 if use_low_priority else max_nodes),
    )

    print(f"  cloud: submitting {len(params_list)} tasks to pool "
          f"'{pool_name}' ({vm_size}, "
          f"{'low-priority' if use_low_priority else 'dedicated'} × "
          f"{max_nodes})", file=sys.stderr)
    job_id = runner.submit_function_tasks(
        pool_name=pool_name,
        fn=fn,
        args_list=params_list,
        pip_deps=[],  # workers only need stdlib + whatever's in the
                      # container image; outcome_fn's deps must be importable
                      # from the tarballed models/ directory.
        budget_usd_cap=budget_usd_cap,
        avg_task_seconds=30.0,
        models_dir=models_dir,
    )
    print(f"  cloud: job {job_id} submitted, waiting...", file=sys.stderr)
    raw_results = runner.wait_and_collect(job_id, timeout_minutes=120)
    print(f"  cloud: {len(raw_results)} results collected", file=sys.stderr)

    # Unpack: each raw result is {'ok': bool, 'result'/'error': ...}
    outs = []
    for i, r in enumerate(raw_results):
        if r is None:
            outs.append({"_error": {"draw": i, "exception": "NoResult",
                                    "message": "task produced no output blob"}})
        elif r.get("ok"):
            outs.append(r["result"])
        else:
            outs.append({"_error": {
                "draw": i,
                "exception": "WorkerError",
                "message": r.get("error", "")[:200],
            }})
    return outs


# ---------------------------------------------------------------------------
# Outcome function loading
# ---------------------------------------------------------------------------

def load_outcome_fn(run_dir: str, spec: Optional[str]) -> Callable[[dict], dict]:
    """Load the modeler-provided outcome_fn.

    spec: "path/to/file.py::function_name" or "path/to/file.py" (assumes
    function_name = "outcome_fn"). Defaults to "models/outcome_fn.py::outcome_fn"
    under run_dir.
    """
    if spec is None:
        spec = "models/outcome_fn.py::outcome_fn"

    if "::" in spec:
        rel_path, fn_name = spec.split("::", 1)
    else:
        rel_path = spec
        fn_name = "outcome_fn"

    path = os.path.join(run_dir, rel_path)
    if not os.path.exists(path):
        # Also check repo-relative.
        if os.path.exists(rel_path):
            path = rel_path
        else:
            raise FileNotFoundError(
                f"outcome_fn source not found at {path} or {rel_path}. "
                f"The modeler must expose a deterministic function "
                f"`{fn_name}(params: dict) -> dict` in {rel_path}. See "
                f"the `uncertainty-quantification` skill for the contract."
            )

    # Use the file's stem as the module name. Pickle stores functions by
    # qualified name (<module>.<function>); the worker extracts the models/
    # tarball into ./models/ + adds it to sys.path, so when pickle tries to
    # resolve <stem>.<fn_name>, `import <stem>` finds <stem>.py in models/.
    # This keeps names consistent between local and cloud.
    mod_name = os.path.splitext(os.path.basename(path))[0]
    mod_dir = os.path.dirname(os.path.abspath(path))
    if mod_dir not in sys.path:
        sys.path.insert(0, mod_dir)
    # If module already imported in this process (e.g. earlier run_dir),
    # re-import fresh to pick up any changes.
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    module_spec = importlib.util.spec_from_file_location(mod_name, path)
    if module_spec is None or module_spec.loader is None:
        raise ImportError(f"could not load {path}")
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[mod_name] = module   # CRITICAL for pickle to see the module
    module_spec.loader.exec_module(module)

    if not hasattr(module, fn_name):
        raise AttributeError(f"{path} has no `{fn_name}` attribute")
    fn = getattr(module, fn_name)
    if not callable(fn):
        raise TypeError(f"{path}:{fn_name} is not callable")
    return fn


# ---------------------------------------------------------------------------
# Propagation
# ---------------------------------------------------------------------------

def propagate(run_dir: str, n_draws: int = 200, seed: int = 42,
              outcome_fn_spec: Optional[str] = None,
              verbose: bool = False,
              cloud: bool = False,
              cloud_pool_name: str = "uq-pool",
              cloud_vm_size: str = "Standard_A2_v2",   # Free Trial-safe
              cloud_max_nodes: int = 2,                 # 2 × A2_v2 = 4 vCPUs
              cloud_use_low_priority: bool = False,
              cloud_budget_usd_cap: float = 5.0) -> dict:
    """Sample N draws from registered priors, run outcome_fn, aggregate.

    If `cloud=True`, each draw runs as an Azure Batch task on a pool of
    `cloud_max_nodes` nodes of `cloud_vm_size`. The `{run_dir}/models/`
    directory is tarballed and uploaded so the worker can import outcome_fn.
    The outcome_fn must be importable by a stable qualified name
    (e.g. `outcome_fn.outcome_fn` when loaded from `models/outcome_fn.py`),
    which is the default contract.

    Use `cloud_use_low_priority=True` when your subscription has
    lowPriorityCoreQuota > 0 (not Free Trial). Default False uses dedicated
    nodes, which always work within Free Trial's 4-vCPU quota.
    """
    registry_module = _import_sibling_module("effect_size_registry")

    citations_path = os.path.join(run_dir, "citations.md")
    registry = registry_module.load_priors(citations_path)
    params = registry.get("parameters", [])
    if not params:
        raise ValueError(
            "No parameters registered in citations.md `## Parameter Registry` "
            "section. UQ propagation requires at least one registered "
            "parameter with CI bounds. See the `effect-size-priors` skill."
        )

    rng = random.Random(seed)
    # Pre-sample all N draws per parameter. Independent priors — later
    # enhancement would support correlated priors (e.g., IRS and ITN
    # efficacy share a pyrethroid-mortality backbone).
    per_param_samples: dict[str, list[float]] = {}
    for p in params:
        per_param_samples[p["name"]] = registry_module.sample_prior(
            p, n_draws, rng
        )

    # Prepare the per-draw params list (same for local and cloud).
    params_list = [
        {name: per_param_samples[name][i] for name in per_param_samples}
        for i in range(n_draws)
    ]

    scalar_results: dict[str, list[float]] = {}
    categorical_results: dict[str, list[str]] = {}
    errors: list[dict] = []

    if cloud:
        cloud_results = _propagate_cloud(
            run_dir, params_list, outcome_fn_spec,
            pool_name=cloud_pool_name, vm_size=cloud_vm_size,
            max_nodes=cloud_max_nodes,
            use_low_priority=cloud_use_low_priority,
            budget_usd_cap=cloud_budget_usd_cap,
            verbose=verbose,
        )
        outs = cloud_results
    else:
        outcome_fn = load_outcome_fn(run_dir, outcome_fn_spec)
        outs = []
        for i, p in enumerate(params_list):
            try:
                outs.append(outcome_fn(p))
            except Exception as e:
                outs.append({"_error": {
                    "draw": i,
                    "exception": type(e).__name__,
                    "message": str(e)[:200],
                }})
            if verbose and (i + 1) % 50 == 0:
                print(f"  [{i+1}/{n_draws}] draws complete", file=sys.stderr)

    for i, out in enumerate(outs):
        if isinstance(out, dict) and "_error" in out:
            errors.append(out["_error"])
            continue
        if not isinstance(out, dict):
            errors.append({"draw": i, "exception": "TypeError",
                           "message": f"outcome_fn must return dict, "
                                      f"got {type(out).__name__}"})
            continue
        for key, val in out.items():
            if isinstance(val, (int, float)):
                scalar_results.setdefault(key, []).append(float(val))
            elif isinstance(val, str):
                categorical_results.setdefault(key, []).append(val)
            # Silently ignore other types (lists, dicts) — outcome_fn
            # contract requires scalar-or-string outputs.

    if errors and len(errors) / max(1, n_draws) > 0.2:
        raise RuntimeError(
            f"outcome_fn errors on >20% of draws "
            f"({len(errors)}/{n_draws}). Last error: "
            f"{errors[-1]}. Aborting."
        )

    # Aggregate scalars.
    scalar_summary = {}
    for key, vals in scalar_results.items():
        if not vals:
            continue
        vals_sorted = sorted(vals)
        n = len(vals_sorted)
        scalar_summary[key] = {
            "mean": float(mean(vals)),
            "median": float(vals_sorted[n // 2]),
            "ci_low": float(vals_sorted[int(0.025 * n)]),
            "ci_high": float(vals_sorted[min(n - 1, int(0.975 * n))]),
            "n": n,
        }

    # Aggregate categoricals.
    categorical_summary = {}
    for key, vals in categorical_results.items():
        counts = Counter(vals)
        dominant, dominant_n = counts.most_common(1)[0]
        categorical_summary[key] = {
            "counts": dict(counts),
            "dominant": dominant,
            "dominance": dominant_n / len(vals),
            "n": len(vals),
        }

    # Parameter sample summary.
    parameter_samples = {}
    for name, samples in per_param_samples.items():
        samples_sorted = sorted(samples)
        n = len(samples_sorted)
        parameter_samples[name] = {
            "mean": float(mean(samples)),
            "ci_low": float(samples_sorted[int(0.025 * n)]),
            "ci_high": float(samples_sorted[min(n - 1, int(0.975 * n))]),
        }

    return {
        "n_draws": n_draws,
        "seed": seed,
        "n_errors": len(errors),
        "errors": errors[:5],  # truncate
        "scalar_outputs": scalar_summary,
        "categorical_outputs": categorical_summary,
        "parameter_samples": parameter_samples,
    }


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

def _run_self_test() -> int:
    import tempfile
    failures: list[str] = []

    def ok(cond: bool, label: str) -> None:
        if not cond:
            failures.append(label)

    with tempfile.TemporaryDirectory() as run_dir:
        os.makedirs(os.path.join(run_dir, "models"))

        # Write a minimal outcome_fn.
        outcome_fn_src = """
def outcome_fn(params):
    # simple linear combination so we can check CI widths analytically
    x = params['irs_effect']
    y = params['itn_effect']
    dalys_averted = 1e6 * (1 - x) + 2e6 * (1 - y)
    # Categorical: which intervention wins
    if x < y:
        chosen = 'irs'
    else:
        chosen = 'itn'
    return {
        'dalys_averted': dalys_averted,
        'chosen': chosen,
    }
"""
        with open(os.path.join(run_dir, "models", "outcome_fn.py"), "w") as f:
            f.write(outcome_fn_src)

        # Write a registry with two parameters.
        citations = """# Citations

## [C1] Source

## Parameter Registry

```yaml
parameters:
  - name: irs_effect
    value: 0.35
    ci_low: 0.27
    ci_high: 0.44
    kind: relative_risk
    source: C1
    applies_to: IRS effect
    code_refs: []

  - name: itn_effect
    value: 0.50
    ci_low: 0.42
    ci_high: 0.59
    kind: relative_risk
    source: C1
    applies_to: ITN effect
    code_refs: []
```
"""
        with open(os.path.join(run_dir, "citations.md"), "w") as f:
            f.write(citations)

        # Run.
        report = propagate(run_dir, n_draws=200, seed=42)

        ok(report["n_draws"] == 200, "n_draws=200")
        ok(report["n_errors"] == 0, f"no errors; got {report['n_errors']}")
        ok("dalys_averted" in report["scalar_outputs"], "scalar present")

        ci = report["scalar_outputs"]["dalys_averted"]
        # Point estimate with irs=0.35, itn=0.5: 1e6*0.65 + 2e6*0.5 = 1.65e6.
        ok(abs(ci["mean"] - 1.65e6) / 1.65e6 < 0.05,
           f"mean near 1.65e6; got {ci['mean']:.3e}")
        # CI should be nontrivially wide (>20% of mean).
        width = ci["ci_high"] - ci["ci_low"]
        ok(width / ci["mean"] > 0.10,
           f"CI width {width:.3e} > 10% of mean")

        ok("chosen" in report["categorical_outputs"],
           "categorical present")
        cat = report["categorical_outputs"]["chosen"]
        ok(cat["dominance"] > 0.5, f"dominance > 0.5; got {cat['dominance']}")

        ok("irs_effect" in report["parameter_samples"],
           "param samples recorded")

    if failures:
        print(f"FAIL: {len(failures)} case(s)", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    print("OK: all self-test cases passed.", file=sys.stderr)
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("run_dir", nargs="?", help="Run directory")
    p.add_argument("--n-draws", type=int, default=200,
                   help="Number of posterior draws (default 200)")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--outcome-fn", default=None,
                   help="Path::fn_name (default models/outcome_fn.py::outcome_fn)")
    p.add_argument("--output", default=None,
                   help="Output path (default <run_dir>/uncertainty_report.yaml)")
    p.add_argument("--verbose", "-v", action="store_true")
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--json", action="store_true",
                   help="Emit report to stdout as JSON (in addition to YAML file)")
    p.add_argument("--cloud", action="store_true",
                   help="Run draws as Azure Batch tasks (distributed). "
                        "Requires AZ_* env vars and provisioned Batch/Storage "
                        "accounts. See the cloud-compute skill.")
    p.add_argument("--cloud-pool-name", default="uq-pool",
                   help="Batch pool to use (created on first run)")
    p.add_argument("--cloud-vm-size", default="Standard_A2_v2",
                   help="VM size per node. Default Standard_A2_v2 (2 vCPUs, "
                        "A-series) fits Free Trial. Use Standard_D4s_v5 or "
                        "larger on Pay-As-You-Go for heavier workloads.")
    p.add_argument("--cloud-max-nodes", type=int, default=2,
                   help="Max concurrent nodes (default 2 × A2_v2 = 4 vCPUs, "
                        "which fits Free Trial A-family quota)")
    p.add_argument("--cloud-low-priority", action="store_true",
                   help="Use low-priority/spot nodes. Default: dedicated. "
                        "Requires lowPriorityCoreQuota > 0 (not Free Trial).")
    p.add_argument("--cloud-budget-usd", type=float, default=5.0,
                   help="Budget cap; refuses to submit if estimate exceeds this")
    args = p.parse_args()

    if args.self_test:
        return _run_self_test()

    if not args.run_dir:
        p.error("run_dir required (or use --self-test)")
    if not os.path.isdir(args.run_dir):
        print(f"ERROR: {args.run_dir} is not a directory", file=sys.stderr)
        return 2

    try:
        report = propagate(args.run_dir, n_draws=args.n_draws,
                           seed=args.seed,
                           outcome_fn_spec=args.outcome_fn,
                           verbose=args.verbose,
                           cloud=args.cloud,
                           cloud_pool_name=args.cloud_pool_name,
                           cloud_vm_size=args.cloud_vm_size,
                           cloud_max_nodes=args.cloud_max_nodes,
                           cloud_use_low_priority=args.cloud_low_priority,
                           cloud_budget_usd_cap=args.cloud_budget_usd)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 3
    except (FileNotFoundError, ImportError, AttributeError, TypeError) as e:
        print(f"ERROR loading outcome_fn: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"ERROR during propagation: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 1

    output_path = args.output or os.path.join(args.run_dir,
                                               "uncertainty_report.yaml")
    with open(output_path, "w") as f:
        yaml.safe_dump(report, f, default_flow_style=False, sort_keys=False)

    print(f"uncertainty_report.yaml written: {output_path}", file=sys.stderr)
    print(f"  n_draws: {report['n_draws']}, errors: {report['n_errors']}",
          file=sys.stderr)
    for name, s in report["scalar_outputs"].items():
        print(f"  {name}: mean={s['mean']:.3g} "
              f"CI [{s['ci_low']:.3g}, {s['ci_high']:.3g}]", file=sys.stderr)
    for name, c in report["categorical_outputs"].items():
        print(f"  {name}: {c['dominant']} ({c['dominance']:.1%})",
              file=sys.stderr)

    if args.json:
        print(json.dumps(report, indent=2, default=str))

    return 0


if __name__ == "__main__":
    sys.exit(main())
