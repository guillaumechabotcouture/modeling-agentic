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
      descoping_steps:      # zero in >= 50% of draws: also a rare event
        mean: 0.16
        ci_low: 0
        ci_high: 0
        n: 200
        rare_event: true
        p_nonzero: 0.02
        p_nonzero_se: 0.0099
        mean_if_nonzero: 8.0
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
import contextlib
import importlib.util
import json
import math
import os
import random
import sys
import traceback
from collections import Counter
from statistics import mean, pstdev
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

    # Phase 21 δ: budget guard BEFORE any resource is created, priced for
    # the node type actually requested (dedicated by default).
    avg_task_seconds = 30.0
    cloud.preflight_budget(len(params_list), avg_task_seconds, vm_size,
                           max_nodes, use_low_priority, budget_usd_cap)

    # Provision the pool if absent and guarantee teardown of a pool this
    # invocation created — on success, exception, or timeout. Static
    # dedicated pools bill until deleted, so this is not optional.
    # Always use dedicated nodes unless the caller explicitly sets
    # low-priority (Free Trial subs have 0 quota).
    spec = cloud.PoolSpec(name=pool_name, vm_size=vm_size,
                          max_nodes=max_nodes, auto_delete=True)
    with runner.managed_pool(spec, use_low_priority=use_low_priority,
                             auto_scale=False):
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
            avg_task_seconds=avg_task_seconds,
            models_dir=models_dir,
            low_priority=use_low_priority,
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


@contextlib.contextmanager
def no_cached_bytecode():
    """Run model code from its source, never from a cached .pyc: a same-size
    edit within the cache's timestamp resolution would otherwise execute old
    code while the fingerprint hashes the new source (Codex review of S2)."""
    prev = (sys.dont_write_bytecode, sys.pycache_prefix)
    # A prefix directory that is never created: no cached bytecode is found,
    # and none is written.
    sys.dont_write_bytecode = True
    sys.pycache_prefix = os.path.join(os.path.realpath(os.sep), "nonexistent-ma-pycache", str(os.getpid()))
    try:
        yield
    finally:
        sys.dont_write_bytecode, sys.pycache_prefix = prev


def point_params(registry: dict) -> dict:
    """{name: point value} of the registry's parameters with a finite `value`."""
    import math
    out = {}
    for p in registry.get("parameters") or []:
        try:
            v = float(p.get("value"))
        except (TypeError, ValueError, AttributeError):
            continue
        if math.isfinite(v) and p.get("name"):
            out[str(p["name"])] = v
    return out


def load_outcome_fn_fingerprinted(run_dir: str, spec: Optional[str], probe_params: Optional[dict] = None):
    """(fn, fingerprint). S2: the fingerprint names the function and hashes
    the run modules it uses: those imported when it is loaded and, when
    `probe_params` is given, those imported while it runs once at them (the
    registry point estimates, which scripts/headline_binding.py also uses), so
    lazy imports count. Other models/ files (figure scripts, pipeline steps)
    are not the function's code and do not change the fingerprint. A module
    imported only on a branch the probe call does not take is not hashed.
    Modules already imported from run_dir are dropped first so the imports are
    this run's. Call it, and the function, inside no_cached_bytecode()."""
    import hashlib
    rd = os.path.realpath(run_dir)
    under = lambda f: bool(f) and os.path.realpath(f).startswith(os.path.join(rd, ""))  # noqa: E731
    for k, m in list(sys.modules.items()):
        if under(getattr(m, "__file__", None)):
            sys.modules.pop(k, None)
    before = set(sys.modules)
    fn = load_outcome_fn(run_dir, spec)
    if probe_params is not None:
        try:
            fn(dict(probe_params))
        except Exception:  # noqa: BLE001 — the caller reports a failing function
            pass
    files = {os.path.realpath(sys.modules[k].__file__) for k in set(sys.modules) - before
             if under(getattr(sys.modules[k], "__file__", None))}
    rel, _, name = (spec or "models/outcome_fn.py::outcome_fn").partition("::")
    src = os.path.join(run_dir, rel)
    src = os.path.realpath(src if os.path.exists(src) else rel)   # load_outcome_fn's lookup
    files.add(src)
    sources = {}
    for f in sorted(files):
        with open(f, "rb") as fh:
            sources[os.path.relpath(f, rd)] = hashlib.sha256(fh.read()).hexdigest()
    return fn, {"file": os.path.relpath(src, rd), "function": name or "outcome_fn", "sources": sources}


# ---------------------------------------------------------------------------
# Propagation
# ---------------------------------------------------------------------------

def draw_params(params: list, n_draws: int, seed: int) -> tuple[dict, list]:
    """(per-parameter samples, per-draw params dicts): the registry draws.
    Independent priors, sampled in registry order from one seeded stream, so
    the same registry, n_draws and seed give the same draws (scripts/ask.py
    regenerates a report's draws this way). A later enhancement would
    support correlated priors (e.g. two intervention effects sharing a
    common driver)."""
    registry_module = _import_sibling_module("effect_size_registry")
    rng = random.Random(seed)
    per_param_samples: dict[str, list[float]] = {}
    for p in params:
        per_param_samples[p["name"]] = registry_module.sample_prior(p, n_draws, rng)
    params_list = [{name: per_param_samples[name][i] for name in per_param_samples}
                   for i in range(n_draws)]
    return per_param_samples, params_list


def parameter_summary(per_param_samples: dict) -> dict:
    """The report's `parameter_samples:` block (mean, std, 2.5% / 97.5% order statistics)."""
    out = {}
    for name, samples in per_param_samples.items():
        samples_sorted = sorted(samples)
        n = len(samples_sorted)
        if not n:
            continue
        out[name] = {
            "mean": float(mean(samples)),
            "std": float(pstdev(samples)) if n > 1 else 0.0,
            "ci_low": float(samples_sorted[int(0.025 * n)]),
            "ci_high": float(samples_sorted[min(n - 1, int(0.975 * n))]),
        }
    return out


# Phase 45 α (A12): an output that is exactly zero in at least this share
# of draws is summarised as a rare event. At >= 50% zeros the median is
# zero, and once the non-zero share falls below 2.5% both 95% bounds are
# zero too, so the quantiles describe the zero mass rather than the event
# (attempt 8: mean 0.16 with CI [0, 0]). The faithful summary is the event
# probability, with its sampling error, and the size of the event when it
# occurs. validate_critique_yaml.py keys its rare-event path on the same
# boundary (`_RARE_EVENT_MAX_P_NONZERO`).
RARE_EVENT_ZERO_FRACTION = 0.5


def rare_event_summary(vals: list[float]) -> dict:
    """p_nonzero, its binomial standard error and the conditional mean for
    a zero-inflated output; {} when the output is not a rare event (fewer
    than RARE_EVENT_ZERO_FRACTION zeros, or zero in every draw — the
    latter is already a degenerate constant)."""
    n = len(vals)
    nonzero = [v for v in vals if v != 0.0]
    if not n or not nonzero or (n - len(nonzero)) / n < RARE_EVENT_ZERO_FRACTION:
        return {}
    p = len(nonzero) / n
    return {"rare_event": True,
            "p_nonzero": p,
            "p_nonzero_se": math.sqrt(p * (1.0 - p) / n),
            "mean_if_nonzero": float(mean(nonzero))}


def propagate(run_dir: str, n_draws: int = 200, seed: int = 42,
              outcome_fn_spec: Optional[str] = None,
              verbose: bool = False,
              cloud: bool = False,
              cloud_pool_name: str = "uq-pool",
              cloud_vm_size: str = "Standard_A2_v2",   # Free Trial-safe
              cloud_max_nodes: int = 2,                 # 2 × A2_v2 = 4 vCPUs
              cloud_use_low_priority: bool = False,
              cloud_budget_usd_cap: float = 5.0,
              parallel: bool = False) -> dict:
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

    # S2: record which code produced the report (scripts/headline_binding.py
    # checks it is the function bound to the report's headline).
    try:
        fp_fn, fingerprint = load_outcome_fn_fingerprinted(run_dir, outcome_fn_spec, point_params(registry))
    except Exception as e:  # noqa: BLE001 — the load below raises the usual error
        fp_fn, fingerprint = None, {"error": f"{type(e).__name__}: {str(e)[:200]}"}

    per_param_samples, params_list = draw_params(params, n_draws, seed)

    scalar_results: dict[str, list[float]] = {}
    categorical_results: dict[str, list[str]] = {}
    errors: list[dict] = []

    compute_cloud = False
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
    elif parallel or os.environ.get("MA_COMPUTE_SCHEDULER"):
        # Phase 24 α: fan the draws out through scripts/compute.py (dask on
        # Coiled when the launcher provides a scheduler, else a local pool).
        compute = _import_sibling_module("compute")
        spec = outcome_fn_spec or "models/outcome_fn.py::outcome_fn"
        if "::" not in spec:
            spec += "::outcome_fn"
        # Same lookup as load_outcome_fn: run-relative, then repo-relative.
        src, _, fname = spec.partition("::")
        if not os.path.isabs(src):
            cand = os.path.join(run_dir, src)
            src = os.path.abspath(cand if os.path.exists(cand) or not os.path.exists(src) else src)
        spec = f"{src}::{fname}"
        # Workers receive only the run's models/ and data/ and the repo's
        # scripts/; a source elsewhere runs on this machine instead.
        rd = os.path.realpath(run_dir)
        bundled = any(os.path.realpath(src).startswith(os.path.join(root, ""))
                      for root in (os.path.join(rd, "models"), os.path.join(rd, "data"),
                                   os.path.join(compute.REPO, "scripts")))
        rows, csum = compute.map_fn(run_dir, spec, params_list, backend="auto" if bundled else "local",
                                     purpose="uncertainty propagation")
        compute_cloud = csum.get("backend") == "dask"
        if verbose:
            print(f"  compute: {csum.get('backend')} x{csum.get('workers')}, "
                  f"{csum.get('wall_s')}s wall", file=sys.stderr)
        outs = [r["result"] if r["ok"] else
                {"_error": {"draw": r["i"], "exception": r.get("error", "").split(":")[0],
                            "message": r.get("error", "")[:200]}} for r in rows]
    else:
        outcome_fn = fp_fn or load_outcome_fn(run_dir, outcome_fn_spec)
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

    n_nonfinite: dict[str, int] = {}
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
            if isinstance(val, bool):
                categorical_results.setdefault(key, []).append(str(val))
            elif isinstance(val, (int, float)):
                # Phase 21 δ: NaN/inf must not enter sorted() — NaN
                # comparisons silently corrupt order statistics.
                if math.isfinite(val):
                    scalar_results.setdefault(key, []).append(float(val))
                else:
                    n_nonfinite[key] = n_nonfinite.get(key, 0) + 1
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

    # Parameter sample summary (computed first: the degeneracy diagnostic
    # needs to know whether the sampler actually varied the inputs).
    parameter_samples = parameter_summary(per_param_samples)
    varied_params = [n for n, ps in parameter_samples.items() if ps["std"] > 0]

    # Aggregate scalars.
    scalar_summary = {}
    degenerate_outputs: list[str] = []
    for key, vals in scalar_results.items():
        if not vals:
            continue
        vals_sorted = sorted(vals)
        n = len(vals_sorted)
        entry = {
            "mean": float(mean(vals)),
            "median": float(vals_sorted[n // 2]),
            "ci_low": float(vals_sorted[int(0.025 * n)]),
            "ci_high": float(vals_sorted[min(n - 1, int(0.975 * n))]),
            "n": n,
            "n_nonfinite": n_nonfinite.get(key, 0),
        }
        entry.update(rare_event_summary(vals))
        # Phase 21 δ: a zero-width interval from uncertain inputs is a
        # defect, not a precise answer. Flag it with a diagnostic.
        scale = max(abs(entry["mean"]), 1e-300)
        if n > 1 and (vals_sorted[-1] - vals_sorted[0]) / scale < 1e-12:
            entry["degenerate"] = True
            entry["degenerate_diagnostic"] = (
                "every draw produced the same value although "
                f"{len(varied_params)} sampled parameter(s) varied — "
                "outcome_fn likely ignores its `params` argument (e.g. reads "
                "module-level constants or a cached result)"
                if varied_params else
                "every draw produced the same value AND no sampled parameter "
                "varied — the registry priors have zero width or the sampler "
                "returned constants")
            degenerate_outputs.append(key)
        scalar_summary[key] = entry
    for key, cnt in n_nonfinite.items():
        if key not in scalar_summary:
            scalar_summary[key] = {"n": 0, "n_nonfinite": cnt,
                                   "degenerate": True,
                                   "degenerate_diagnostic":
                                       "all draws were NaN/inf"}
            degenerate_outputs.append(key)

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

    n_failed = len(errors)
    return {
        "producer": "scripts/propagate_uncertainty.py",
        "outcome_fn_fingerprint": fingerprint,
        # Phase 21 δ: effort floors read these, not n_draws alone —
        # n_draws is what was requested; n_successful is what the CIs
        # are built from.
        # Draws on a cluster are cloud execution for the effort floors
        # (cloud_uq_present), whichever service ran them.
        "backend": ("cloud" if cloud or compute_cloud else
                    "compute" if (parallel or os.environ.get("MA_COMPUTE_SCHEDULER")) else "local"),
        "n_draws": n_draws,
        "n_draws_requested": n_draws,
        "n_successful": len(outs) - n_failed,
        "n_failed": n_failed,
        "degenerate_outputs": degenerate_outputs,
        "sampler_varied_parameters": len(varied_params),
        "seed": seed,
        "n_errors": n_failed,
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
        # Phase 21 δ: provenance + success accounting.
        ok(report["backend"] == "local" and report["n_draws_requested"] == 200
           and report["n_successful"] == 200 and report["n_failed"] == 0,
           f"P21-1: backend/n_successful/n_failed recorded; got "
           f"{ {k: report.get(k) for k in ('backend', 'n_successful', 'n_failed')} }")
        ok(report["degenerate_outputs"] == [] and
           report["parameter_samples"]["irs_effect"]["std"] > 0,
           "P21-2: healthy run has no degenerate outputs and nonzero std")
        # P24-1 (Phase 24 α): the compute-service path gives the same answer.
        par = propagate(run_dir, n_draws=200, seed=42, parallel=True)
        ok(par["backend"] == "compute"
           and abs(par["scalar_outputs"]["dalys_averted"]["mean"] - ci["mean"]) < 1e-6
           and par["scalar_outputs"]["dalys_averted"]["ci_low"] == ci["ci_low"],
           f"P24-1: parallel propagation matches serial; got {par['backend']}")
        cwd = os.getcwd()
        try:
            os.chdir(os.path.dirname(run_dir))
            rel = os.path.basename(run_dir)
            par2 = propagate(rel, n_draws=200, seed=42, parallel=True,
                             outcome_fn_spec=os.path.join(rel, "models", "outcome_fn.py") + "::outcome_fn")
        finally:
            os.chdir(cwd)
        ok(par2["n_failed"] == 0 and abs(par2["scalar_outputs"]["dalys_averted"]["mean"] - ci["mean"]) < 1e-6,
           f"P24-2: parallel keeps the repo-relative outcome_fn fallback; got n_failed={par2.get('n_failed')}")

        # P21-3: outcome_fn that ignores params → zero-width CI flagged
        # degenerate with the "ignores params" diagnostic; NaN draws are
        # excluded and counted rather than sorted.
        with open(os.path.join(run_dir, "models", "outcome_fn.py"), "w") as f:
            f.write("CACHED = 1769757.25\n"
                    "_i = [0]\n"
                    "def outcome_fn(params):\n"
                    "    _i[0] += 1\n"
                    "    return {'total_benefit': CACHED,\n"
                    "            'noisy': float('nan') if _i[0] % 10 == 0\n"
                    "                     else params['irs_effect']}\n")
        rep3 = propagate(run_dir, n_draws=100, seed=1)
        tb = rep3["scalar_outputs"]["total_benefit"]
        ok(tb.get("degenerate") is True
           and "ignores its `params`" in tb.get("degenerate_diagnostic", "")
           and "total_benefit" in rep3["degenerate_outputs"],
           f"P21-3: constant output flagged degenerate; got {tb}")
        nz = rep3["scalar_outputs"]["noisy"]
        ok(nz["n"] == 90 and nz["n_nonfinite"] == 10
           and nz["ci_low"] <= nz["median"] <= nz["ci_high"]
           and not nz.get("degenerate"),
           f"P21-4: NaN draws excluded + counted; got {nz}")

        # P45-1 (Phase 45 α, A12): zero-inflated output → p_nonzero, its
        # SE and the conditional mean; a mostly non-zero output and an
        # all-zero output get no rare-event fields.
        z = rare_event_summary([0.0] * 196 + [5.0, 7.0, 9.0, 11.0])
        ok(z.get("rare_event") is True and abs(z["p_nonzero"] - 0.02) < 1e-12
           and abs(z["p_nonzero_se"] - math.sqrt(0.02 * 0.98 / 200)) < 1e-12
           and z["mean_if_nonzero"] == 8.0,
           f"P45-1: rare-event fields; got {z}")
        ok(rare_event_summary([0.0] * 40 + [1.0] * 60) == {}
           and rare_event_summary([0.0] * 50) == {}
           and rare_event_summary([0.0] * 50 + [2.0] * 50).get("p_nonzero") == 0.5,
           "P45-2: <50% zeros or all-zero → no fields; exactly 50% → rare event")
        with open(os.path.join(run_dir, "models", "outcome_fn.py"), "w") as f:
            f.write("def outcome_fn(params):\n"
                    "    x = params['irs_effect']\n"
                    "    return {'cuts': 3.0 if x > 0.40 else 0.0, 'b': x}\n")
        rep4 = propagate(run_dir, n_draws=200, seed=3)
        cu = rep4["scalar_outputs"]["cuts"]
        ok(cu.get("rare_event") is True and 0 < cu["p_nonzero"] < 0.5
           and cu["mean_if_nonzero"] == 3.0
           and "rare_event" not in rep4["scalar_outputs"]["b"]
           and "cuts" not in rep4["degenerate_outputs"],
           f"P45-3: propagate records rare-event fields; got {cu}")

        # P21-5: cloud path against the in-memory Azure fake — budget is
        # rejected before any pool is created; on success the created
        # pool is deleted; on a mid-run failure it is deleted too.
        cloud_mod = _import_sibling_module("cloud_batch")
        with open(os.path.join(run_dir, "models", "outcome_fn.py"), "w") as f:
            f.write(outcome_fn_src)
        orig_runner = cloud_mod.BatchRunner
        try:
            with cloud_mod._FakeAzure() as fa:
                r1 = fa.runner()
                cloud_mod.BatchRunner = lambda: r1
                raised = False
                try:
                    propagate(run_dir, n_draws=50, seed=2, cloud=True,
                              cloud_budget_usd_cap=1e-6)
                except cloud_mod.BudgetExceededError:
                    raised = True
                ok(raised and fa.calls == [],
                   f"P21-5: budget rejection precedes provisioning; calls {fa.calls}")
            with cloud_mod._FakeAzure() as fa:
                r2 = fa.runner()
                cloud_mod.BatchRunner = lambda: r2
                rep5 = propagate(run_dir, n_draws=20, seed=2, cloud=True,
                                 cloud_budget_usd_cap=100.0)
                ok(rep5["backend"] == "cloud" and rep5["n_successful"] == 20
                   and ("pool.delete", "uq-pool") in fa.calls,
                   f"P21-6: cloud success → results + pool deleted; calls {fa.calls}")
            with cloud_mod._FakeAzure(task_mode="hang") as fa:
                runner = fa.runner()
                orig_wait = runner.wait_and_collect
                runner.wait_and_collect = (lambda job_id, timeout_minutes=120:
                                           orig_wait(job_id, timeout_minutes=0.0001,
                                                     poll_seconds=0))
                cloud_mod.BatchRunner = lambda: runner
                timed_out = False
                try:
                    propagate(run_dir, n_draws=5, seed=2, cloud=True,
                              cloud_budget_usd_cap=100.0)
                except TimeoutError:
                    timed_out = True
                ok(timed_out and ("pool.delete", "uq-pool") in fa.calls,
                   f"P21-7: cloud timeout → pool deleted; calls {fa.calls}")
        finally:
            cloud_mod.BatchRunner = orig_runner

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
    p.add_argument("--parallel", action="store_true",
                   help="Fan draws out via scripts/compute.py (automatic when "
                        "MA_COMPUTE_SCHEDULER is set, i.e. on Coiled)")
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
        with no_cached_bytecode():
            report = propagate(args.run_dir, n_draws=args.n_draws,
                               seed=args.seed,
                               outcome_fn_spec=args.outcome_fn,
                               verbose=args.verbose,
                               cloud=args.cloud,
                               cloud_pool_name=args.cloud_pool_name,
                               cloud_vm_size=args.cloud_vm_size,
                               cloud_max_nodes=args.cloud_max_nodes,
                               cloud_use_low_priority=args.cloud_low_priority,
                               cloud_budget_usd_cap=args.cloud_budget_usd,
                               parallel=args.parallel)
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
