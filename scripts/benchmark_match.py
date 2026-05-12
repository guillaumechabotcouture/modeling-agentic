#!/usr/bin/env python3
"""Phase 19 γ — benchmark-locked acceptance.

Compares the modeler's computed outputs against literature-anchored
benchmark targets the planner extracted into the plan's
`Published benchmarks` table. Writes benchmark_match.yaml.

Inputs (under run_dir/)
-----------------------
models/benchmark_targets.yaml   Required. Per-target schema:
    benchmarks:
      - id: incidence_under5_baseline
        metric: incidence_per_1000_under5
        target_value: 412                 # the published number
        units: per_1000_pyo
        tolerance_factor: 2.0             # acceptance band: [target/factor, target*factor]
        # OR an absolute tolerance:
        tolerance_abs: 50
        source: "WMR 2023 Table A.5"
        computed_value: 387               # the modeler fills this after the run
                                           # (or `computed_field:` points the script
                                           #  at where to read it — see below)
        # Optional pointer if computed_value is not inlined:
        computed_field: "uncertainty_report.yaml::scalar_outputs.incidence.mean"

Resolution rules
----------------
1. If `computed_value` is set on the target entry, use it directly.
2. Else if `computed_field` is set, parse it as
   `<relative-yaml-path>::<dotted-key-path>` and resolve.
3. Else emit a `computed_value_missing` violation.

Acceptance band
---------------
Default (`tolerance_factor`): observed must lie in
    [target / factor, target * factor].
If `tolerance_abs` is set, the band is `[target - abs, target + abs]`.
If both are set, both must hold (intersection).

Verdict
-------
PASS if all benchmarks are within band. DRIFT if any are outside.

Exit status
-----------
0  PASS (or no benchmarks declared yet — drafting window)
1  DRIFT recorded in benchmark_match.yaml
2  malformed inputs / run_dir missing
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from typing import Any

import yaml


@dataclass
class BenchResult:
    id: str
    metric: str
    target_value: float
    observed: float | None
    band_low: float | None
    band_high: float | None
    within_band: bool
    source: str
    diagnostic: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "metric": self.metric,
            "target_value": self.target_value,
            "observed": self.observed,
            "band_low": self.band_low,
            "band_high": self.band_high,
            "within_band": self.within_band,
            "source": self.source,
            "diagnostic": self.diagnostic,
        }


def _resolve_dotpath(doc: Any, path: str) -> Any:
    parts = path.split(".")
    cur: Any = doc
    for part in parts:
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _resolve_computed_field(run_dir: str, ref: str) -> tuple[float | None, str]:
    """Parse '<rel-yaml>::<dot.path>' and resolve. Returns (value, diag)."""
    if "::" not in ref:
        return None, f"computed_field must be 'path::dotted.key', got {ref!r}"
    rel, key = ref.split("::", 1)
    p = os.path.join(run_dir, rel)
    if not os.path.exists(p):
        return None, f"computed_field artifact {rel} not found"
    try:
        with open(p) as f:
            doc = yaml.safe_load(f) or {}
    except (yaml.YAMLError, OSError) as e:
        return None, f"could not parse {rel}: {e}"
    val = _resolve_dotpath(doc, key)
    if val is None:
        return None, f"key {key!r} absent in {rel}"
    try:
        return float(val), ""
    except (TypeError, ValueError):
        return None, f"value at {rel}::{key} not numeric (got {val!r})"


def _resolve_observed(run_dir: str, target: dict) -> tuple[float | None, str]:
    """Return (observed, diagnostic). observed is None on failure."""
    if "computed_value" in target and target["computed_value"] is not None:
        try:
            return float(target["computed_value"]), ""
        except (TypeError, ValueError):
            return None, (f"computed_value not numeric: "
                          f"{target['computed_value']!r}")
    cf = target.get("computed_field")
    if cf:
        return _resolve_computed_field(run_dir, str(cf))
    return None, ("neither computed_value nor computed_field set; "
                  "modeler must populate one after the run")


def _band(target: dict) -> tuple[float | None, float | None, str]:
    """Return (low, high, diag). None on failure."""
    try:
        tv = float(target["target_value"])
    except (KeyError, TypeError, ValueError) as e:
        return None, None, f"target_value invalid: {e}"
    factor = target.get("tolerance_factor")
    abs_tol = target.get("tolerance_abs")
    low: float | None = None
    high: float | None = None
    if factor is not None:
        try:
            f = float(factor)
            if f <= 0:
                return None, None, (f"tolerance_factor must be > 0, got {factor!r}")
            # For target=0 the factor band collapses; require abs in that case.
            if tv == 0 and abs_tol is None:
                return None, None, ("target_value == 0 with no "
                                    "tolerance_abs; band undefined")
            low = tv / f if tv >= 0 else tv * f
            high = tv * f if tv >= 0 else tv / f
        except (TypeError, ValueError) as e:
            return None, None, f"tolerance_factor invalid: {e}"
    if abs_tol is not None:
        try:
            a = float(abs_tol)
            if a < 0:
                return None, None, (f"tolerance_abs must be ≥ 0, got {abs_tol!r}")
            ab_low = tv - a
            ab_high = tv + a
            # Intersection
            low = max(low, ab_low) if low is not None else ab_low
            high = min(high, ab_high) if high is not None else ab_high
        except (TypeError, ValueError) as e:
            return None, None, f"tolerance_abs invalid: {e}"
    if low is None and high is None:
        return None, None, ("neither tolerance_factor nor tolerance_abs set; "
                            "benchmark cannot be evaluated")
    return low, high, ""


def evaluate(run_dir: str) -> dict:
    """Walk benchmark_targets.yaml, return a structured report."""
    path = os.path.join(run_dir, "models", "benchmark_targets.yaml")
    if not os.path.exists(path):
        return {
            "verdict": "ABSENT",
            "n_benchmarks": 0,
            "n_drifted": 0,
            "missing_computed": 0,
            "results": [],
            "note": "models/benchmark_targets.yaml absent",
        }
    try:
        with open(path) as f:
            doc = yaml.safe_load(f) or {}
    except (yaml.YAMLError, OSError) as e:
        return {
            "verdict": "MALFORMED",
            "error": f"could not parse {path}: {e}",
            "n_benchmarks": 0,
            "n_drifted": 0,
            "missing_computed": 0,
            "results": [],
        }
    if not isinstance(doc, dict):
        return {
            "verdict": "MALFORMED",
            "error": "top-level must be a mapping",
            "n_benchmarks": 0, "n_drifted": 0,
            "missing_computed": 0, "results": [],
        }
    targets = doc.get("benchmarks") or []
    if not isinstance(targets, list):
        return {
            "verdict": "MALFORMED",
            "error": "`benchmarks:` must be a list",
            "n_benchmarks": 0, "n_drifted": 0,
            "missing_computed": 0, "results": [],
        }
    if not targets:
        return {
            "verdict": "MALFORMED",
            "error": "`benchmarks:` must contain at least one target",
            "n_benchmarks": 0, "n_drifted": 0,
            "missing_computed": 0, "results": [],
        }
    results: list[BenchResult] = []
    missing_computed = 0
    for i, t in enumerate(targets):
        if not isinstance(t, dict):
            continue
        tid = str(t.get("id") or f"benchmark_{i}")
        metric = str(t.get("metric") or tid)
        source = str(t.get("source") or "?")
        try:
            tv = float(t.get("target_value"))
        except (TypeError, ValueError) as e:
            results.append(BenchResult(
                id=tid, metric=metric, target_value=float("nan"),
                observed=None, band_low=None, band_high=None,
                within_band=False, source=source,
                diagnostic=f"target_value invalid: {e}",
            ))
            continue
        low, high, band_diag = _band(t)
        observed, obs_diag = _resolve_observed(run_dir, t)
        if observed is None:
            missing_computed += 1
            results.append(BenchResult(
                id=tid, metric=metric, target_value=tv,
                observed=None, band_low=low, band_high=high,
                within_band=False, source=source,
                diagnostic=obs_diag or band_diag,
            ))
            continue
        if low is None or high is None:
            results.append(BenchResult(
                id=tid, metric=metric, target_value=tv,
                observed=observed, band_low=low, band_high=high,
                within_band=False, source=source,
                diagnostic=band_diag,
            ))
            continue
        within = low <= observed <= high
        results.append(BenchResult(
            id=tid, metric=metric, target_value=tv,
            observed=observed, band_low=low, band_high=high,
            within_band=within, source=source,
            diagnostic="",
        ))

    n_drifted = sum(1 for r in results if r.observed is not None and not r.within_band)
    n_total = len([r for r in results if r.observed is not None])
    verdict = "PASS" if (n_drifted == 0 and missing_computed == 0
                          and n_total > 0) else (
        "DRIFT" if (n_drifted > 0 or missing_computed > 0)
        else "PENDING"
    )
    return {
        "verdict": verdict,
        "n_benchmarks": len(results),
        "n_with_observed": n_total,
        "n_drifted": n_drifted,
        "missing_computed": missing_computed,
        "results": [r.to_dict() for r in results],
    }


def write_report(run_dir: str, report: dict) -> str:
    out_path = os.path.join(run_dir, "benchmark_match.yaml")
    with open(out_path, "w") as f:
        yaml.safe_dump(report, f, sort_keys=False)
    return out_path


# --------------------------------------------------------------------------- #
# Self-test
# --------------------------------------------------------------------------- #


def _self_test() -> int:
    import tempfile

    failures: list[str] = []

    def ok(cond: bool, label: str) -> None:
        if not cond:
            failures.append(label)

    # T1: no benchmark file → ABSENT verdict
    with tempfile.TemporaryDirectory() as d:
        rep = evaluate(d)
        ok(rep["verdict"] == "ABSENT", f"T1: ABSENT expected, got {rep}")

    # T2: inlined computed_value within tolerance_factor band → PASS
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "models", "benchmark_targets.yaml"), "w") as f:
            yaml.safe_dump({
                "benchmarks": [
                    {"id": "b1", "metric": "incidence", "target_value": 400,
                     "tolerance_factor": 2.0, "source": "WMR 2023",
                     "computed_value": 387},
                ]
            }, f)
        rep = evaluate(d)
        ok(rep["verdict"] == "PASS" and rep["n_drifted"] == 0,
           f"T2: 387 vs 400 (band 200-800) should PASS, got {rep}")

    # T3: out-of-band → DRIFT
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "models", "benchmark_targets.yaml"), "w") as f:
            yaml.safe_dump({
                "benchmarks": [
                    {"id": "b1", "metric": "incidence", "target_value": 400,
                     "tolerance_factor": 1.5, "source": "WMR 2023",
                     "computed_value": 100},
                ]
            }, f)
        rep = evaluate(d)
        ok(rep["verdict"] == "DRIFT" and rep["n_drifted"] == 1,
           f"T3: 100 vs 400 (band 267-600) should DRIFT, got {rep}")
        ok(rep["results"][0]["within_band"] is False,
           f"T3: within_band must be False, got {rep['results']}")

    # T4: computed_field resolution from sibling YAML
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        # the targets file
        with open(os.path.join(d, "models", "benchmark_targets.yaml"), "w") as f:
            yaml.safe_dump({
                "benchmarks": [
                    {"id": "b1", "metric": "incidence", "target_value": 400,
                     "tolerance_factor": 2.0, "source": "WMR 2023",
                     "computed_field":
                        "uncertainty_report.yaml::scalar_outputs.incidence.mean"},
                ]
            }, f)
        # the source artifact
        with open(os.path.join(d, "uncertainty_report.yaml"), "w") as f:
            yaml.safe_dump({"scalar_outputs": {"incidence": {"mean": 350}}}, f)
        rep = evaluate(d)
        ok(rep["verdict"] == "PASS" and rep["results"][0]["observed"] == 350,
           f"T4: computed_field resolution should work, got {rep}")

    # T5: neither computed_value nor computed_field → missing_computed
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "models", "benchmark_targets.yaml"), "w") as f:
            yaml.safe_dump({
                "benchmarks": [
                    {"id": "b1", "metric": "incidence", "target_value": 400,
                     "tolerance_factor": 2.0, "source": "WMR 2023"},
                ]
            }, f)
        rep = evaluate(d)
        ok(rep["missing_computed"] == 1 and rep["verdict"] == "DRIFT",
           f"T5: missing computed must DRIFT, got {rep}")

    # T6: tolerance_abs band
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "models", "benchmark_targets.yaml"), "w") as f:
            yaml.safe_dump({
                "benchmarks": [
                    # band [350, 450]; observed 380 inside.
                    {"id": "b1", "metric": "incidence", "target_value": 400,
                     "tolerance_abs": 50, "source": "WMR 2023",
                     "computed_value": 380},
                    # band [380, 420]; observed 425 OUTSIDE.
                    {"id": "b2", "metric": "prev", "target_value": 400,
                     "tolerance_abs": 20, "source": "WMR 2023",
                     "computed_value": 425},
                ]
            }, f)
        rep = evaluate(d)
        ok(rep["n_benchmarks"] == 2 and rep["n_drifted"] == 1,
           f"T6: 1 of 2 should drift, got {rep}")
        ok(rep["results"][0]["within_band"]
           and not rep["results"][1]["within_band"],
           f"T6: per-benchmark within_band wrong, got {rep['results']}")

    # T7: combined factor + abs intersection
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "models", "benchmark_targets.yaml"), "w") as f:
            yaml.safe_dump({
                "benchmarks": [
                    # factor 2.0 -> [200, 800]; abs 50 -> [350, 450].
                    # Intersection [350, 450]; observed 360 inside.
                    {"id": "b1", "metric": "incidence", "target_value": 400,
                     "tolerance_factor": 2.0, "tolerance_abs": 50,
                     "source": "WMR 2023",
                     "computed_value": 360},
                ]
            }, f)
        rep = evaluate(d)
        ok(rep["verdict"] == "PASS",
           f"T7: intersection band [350,450], 360 should PASS, got {rep}")
        # Bands recorded should reflect intersection.
        r = rep["results"][0]
        ok(r["band_low"] == 350 and r["band_high"] == 450,
           f"T7: intersection band low/high wrong, got {r}")

    # T8: malformed YAML → MALFORMED
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "models", "benchmark_targets.yaml"), "w") as f:
            f.write("benchmarks: not_a_list_just_a_string\n")
        rep = evaluate(d)
        ok(rep["verdict"] == "MALFORMED",
           f"T8: non-list benchmarks must be MALFORMED, got {rep}")

    # T8b: empty benchmark list must not silently bypass acceptance.
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "models", "benchmark_targets.yaml"), "w") as f:
            yaml.safe_dump({"benchmarks": []}, f)
        rep = evaluate(d)
        ok(rep["verdict"] == "MALFORMED"
           and "at least one" in rep.get("error", ""),
           f"T8b: empty benchmarks must be MALFORMED, got {rep}")

    # T9: write_report round-trips
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "models", "benchmark_targets.yaml"), "w") as f:
            yaml.safe_dump({
                "benchmarks": [
                    {"id": "b1", "metric": "x", "target_value": 100,
                     "tolerance_factor": 2.0, "source": "test",
                     "computed_value": 110},
                ]
            }, f)
        rep = evaluate(d)
        p = write_report(d, rep)
        with open(p) as f:
            loaded = yaml.safe_load(f)
        ok(loaded["verdict"] == "PASS",
           f"T9: roundtrip must preserve verdict, got {loaded}")

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
        return _self_test()
    if not args.run_dir:
        p.error("run_dir is required (or use --self-test)")
    if not os.path.isdir(args.run_dir):
        print(f"ERROR: {args.run_dir} is not a directory", file=sys.stderr)
        return 2
    rep = evaluate(args.run_dir)
    write_report(args.run_dir, rep)
    if args.json:
        import json
        print(json.dumps(rep, indent=2, default=str))
    print(f"benchmark_match: verdict={rep['verdict']} "
          f"{rep['n_drifted']} drifted, "
          f"{rep.get('missing_computed', 0)} missing computed "
          f"-> {args.run_dir}/benchmark_match.yaml", file=sys.stderr)
    return 1 if rep["verdict"] == "DRIFT" else (
        2 if rep["verdict"] == "MALFORMED" else 0
    )


if __name__ == "__main__":
    sys.exit(main())
