#!/usr/bin/env python3
"""
Phase 6 Commit θ — optimizer-quality benchmark.

When the modeler produces an allocation (decision_rule.md exists), they
must also benchmark their primary optimizer against at least one
alternative (ILP via PuLP, simulated annealing, random-restart greedy,
brute force when feasible). The benchmark file lives at
`{run_dir}/models/optimization_quality.yaml` and has the schema:

    primary_method: greedy | ilp_pulp | simulated_annealing | random_restart_K | brute_force
    primary_objective: <number — cases averted, DALYs averted, etc.>
    objective_name: cases_averted_per_year   # or dalys_averted_per_year
    benchmark_methods:
      - method: ilp_pulp
        objective: 5489102
        runtime_sec: 312
        notes: "PuLP w/ CBC solver, default settings"
      - method: random_restart_50
        objective: 5398754
        runtime_sec: 45
    gap_pct: 1.49             # 100*(best_objective - primary_objective) / best_objective
    notes: |
      Brute force not feasible (8 packages × 22 archetypes = 8^22).
      ILP via PuLP+CBC near-optimal in 5 min.

This script validates the file's structure, recomputes gap_pct, and
emits a JSON report. It is invoked at STAGE 5b (rigor) by the lead.

Usage:
    python3 scripts/optimization_quality.py <run_dir>
    python3 scripts/optimization_quality.py --self-test
"""

from __future__ import annotations

import argparse
import json
import os
import sys

try:
    import yaml
except ImportError:
    print("ERROR: pyyaml not installed", file=sys.stderr)
    sys.exit(2)


VALID_METHODS = {
    "greedy", "marginal_greedy", "ilp_pulp", "ilp_cbc", "ilp_gurobi",
    "simulated_annealing", "random_restart", "brute_force",
    "genetic_algorithm", "branch_and_bound",
    # Reference baselines (not optimizers per se, but legitimate
    # benchmarks): uniform allocation, proportional-to-population,
    # status quo, no-allocation. Modelers often compare against these.
    "uniform", "uniform_allocation", "proportional", "status_quo",
    "current_allocation", "no_allocation", "baseline",
}
# Prefix-matched: anything starting with these. Includes "uniform_*"
# (e.g., "uniform_llin80"), "random_restart_K", "simulated_annealing_T*",
# "ilp_*" variants.
VALID_PREFIXES = {
    "random_restart", "simulated_annealing", "ilp",
    "uniform", "proportional",
}

DEFAULT_GAP_THRESHOLD_PCT = 10.0  # MEDIUM blocker if exceeded


def _normalize_method(name: str) -> str:
    """Match `name` against VALID_METHODS or VALID_PREFIXES. Returns the
    matched canonical prefix or the input unchanged if recognized.
    Returns "" if not recognized."""
    if not isinstance(name, str):
        return ""
    name_l = name.strip().lower()
    if name_l in VALID_METHODS:
        return name_l
    for prefix in VALID_PREFIXES:
        if name_l.startswith(prefix):
            return prefix
    return ""


def validate_optimization_quality(yaml_path: str) -> dict:
    """Load and validate the optimization_quality.yaml file. Returns
    {"verdict": "CLEAN"|"GAP_TOO_LARGE"|"NO_BENCHMARK"|"MALFORMED",
     "gap_pct": float|None, "errors": [str], "primary": dict, "best": dict}.
    """
    if not os.path.exists(yaml_path):
        return {"verdict": "MISSING", "gap_pct": None, "errors": [
            f"{yaml_path} does not exist"], "primary": None, "best": None}

    try:
        with open(yaml_path) as f:
            data = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        return {"verdict": "MALFORMED", "gap_pct": None,
                "errors": [f"YAML parse error: {e}"],
                "primary": None, "best": None}

    errors: list[str] = []
    primary_method = data.get("primary_method")
    if not _normalize_method(primary_method or ""):
        errors.append(
            f"primary_method {primary_method!r} not recognized. "
            f"Must be one of {sorted(VALID_METHODS)} or start with "
            f"{sorted(VALID_PREFIXES)}."
        )
    primary_obj = data.get("primary_objective")
    if not isinstance(primary_obj, (int, float)):
        errors.append(
            f"primary_objective must be numeric, got {type(primary_obj).__name__}"
        )

    benchmarks = data.get("benchmark_methods") or []
    if not isinstance(benchmarks, list) or not benchmarks:
        return {"verdict": "NO_BENCHMARK", "gap_pct": None,
                "errors": errors + ["benchmark_methods must be a non-empty list"],
                "primary": {"method": primary_method, "objective": primary_obj},
                "best": None}

    valid_benchmarks = []
    for i, b in enumerate(benchmarks):
        if not isinstance(b, dict):
            errors.append(f"benchmark_methods[{i}] must be a mapping")
            continue
        b_method = b.get("method")
        b_obj = b.get("objective")
        if not _normalize_method(b_method or ""):
            errors.append(
                f"benchmark_methods[{i}].method {b_method!r} not recognized")
            continue
        if not isinstance(b_obj, (int, float)):
            errors.append(
                f"benchmark_methods[{i}].objective must be numeric, "
                f"got {type(b_obj).__name__}")
            continue
        valid_benchmarks.append({
            "method": b_method,
            "objective": float(b_obj),
            "runtime_sec": b.get("runtime_sec"),
        })

    if errors:
        return {"verdict": "MALFORMED", "gap_pct": None, "errors": errors,
                "primary": {"method": primary_method, "objective": primary_obj},
                "best": None}

    # Compute gap. Best objective across primary + benchmarks.
    all_objs = [(primary_method, float(primary_obj))] + [
        (b["method"], b["objective"]) for b in valid_benchmarks
    ]
    best_method, best_obj = max(all_objs, key=lambda x: x[1])
    if best_obj <= 0:
        gap_pct = 0.0  # all-zero objectives degenerate; no useful gap
    else:
        gap_pct = 100.0 * (best_obj - float(primary_obj)) / best_obj

    verdict = "GAP_TOO_LARGE" if gap_pct > DEFAULT_GAP_THRESHOLD_PCT else "CLEAN"

    return {
        "verdict": verdict,
        "gap_pct": gap_pct,
        "errors": [],
        "primary": {"method": primary_method, "objective": float(primary_obj)},
        "best": {"method": best_method, "objective": best_obj},
        "benchmarks": valid_benchmarks,
    }


def _run_self_test() -> int:
    """Inline self-test cases. Returns 0 on success, 1 on failure."""
    import tempfile

    failures: list[str] = []

    def ok(cond: bool, label: str) -> None:
        if not cond:
            failures.append(label)

    with tempfile.TemporaryDirectory() as d:
        # Case T1: clean — primary close to best.
        f1 = os.path.join(d, "t1.yaml")
        with open(f1, "w") as f:
            f.write(
                "primary_method: greedy\n"
                "primary_objective: 5400000\n"
                "benchmark_methods:\n"
                "  - method: ilp_pulp\n"
                "    objective: 5450000\n"
                "    runtime_sec: 300\n"
            )
        r1 = validate_optimization_quality(f1)
        ok(r1["verdict"] == "CLEAN",
           f"T1: small gap should be CLEAN, got {r1}")
        ok(abs(r1["gap_pct"] - 0.917) < 0.01,
           f"T1: gap_pct ~0.92%, got {r1['gap_pct']}")

        # Case T2: gap too large.
        f2 = os.path.join(d, "t2.yaml")
        with open(f2, "w") as f:
            f.write(
                "primary_method: greedy\n"
                "primary_objective: 4000000\n"
                "benchmark_methods:\n"
                "  - method: ilp_pulp\n"
                "    objective: 5500000\n"
            )
        r2 = validate_optimization_quality(f2)
        ok(r2["verdict"] == "GAP_TOO_LARGE",
           f"T2: 27% gap should be GAP_TOO_LARGE, got {r2['verdict']}")

        # Case T3: no benchmark.
        f3 = os.path.join(d, "t3.yaml")
        with open(f3, "w") as f:
            f.write(
                "primary_method: greedy\n"
                "primary_objective: 5400000\n"
                "benchmark_methods: []\n"
            )
        r3 = validate_optimization_quality(f3)
        ok(r3["verdict"] == "NO_BENCHMARK",
           f"T3: empty benchmarks should fire NO_BENCHMARK, got {r3['verdict']}")

        # Case T4: malformed (unknown method).
        f4 = os.path.join(d, "t4.yaml")
        with open(f4, "w") as f:
            f.write(
                "primary_method: my_proprietary_solver\n"
                "primary_objective: 5400000\n"
                "benchmark_methods:\n"
                "  - method: ilp_pulp\n"
                "    objective: 5450000\n"
            )
        r4 = validate_optimization_quality(f4)
        ok(r4["verdict"] == "MALFORMED",
           f"T4: unknown method should be MALFORMED, got {r4['verdict']}")

        # Case T5: missing file.
        r5 = validate_optimization_quality(os.path.join(d, "nope.yaml"))
        ok(r5["verdict"] == "MISSING",
           f"T5: nonexistent file should be MISSING, got {r5['verdict']}")

        # Case T6: random_restart_50 prefix match.
        f6 = os.path.join(d, "t6.yaml")
        with open(f6, "w") as f:
            f.write(
                "primary_method: greedy\n"
                "primary_objective: 5400000\n"
                "benchmark_methods:\n"
                "  - method: random_restart_50\n"
                "    objective: 5410000\n"
                "  - method: simulated_annealing_T100_a0.95\n"
                "    objective: 5395000\n"
            )
        r6 = validate_optimization_quality(f6)
        ok(r6["verdict"] == "CLEAN",
           f"T6: prefix-matched method names should validate, got {r6}")

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
    args = p.parse_args()

    if args.self_test:
        return _run_self_test()
    if not args.run_dir:
        p.error("run_dir is required (or use --self-test)")

    yaml_path = os.path.join(args.run_dir, "models", "optimization_quality.yaml")
    result = validate_optimization_quality(yaml_path)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"verdict: {result['verdict']}", file=sys.stderr)
        if result["gap_pct"] is not None:
            print(f"gap_pct: {result['gap_pct']:.2f}%", file=sys.stderr)
        if result.get("errors"):
            for e in result["errors"]:
                print(f"  - {e}", file=sys.stderr)

    if result["verdict"] in ("CLEAN",):
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
