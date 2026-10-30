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

models/benchmark_computed.yaml  Optional (Phase 45 γ). The modeler's
    computed values, rewritten after every model run:
      benchmarks: [{id, computed_value | computed_field}, ...]
    or
      computed: {<id>: <value>, ...}
    The targets file is sha-locked by the gate the first time it sees it
    (scripts/decision_brief.py, acceptance_lock.json), so a value the
    modeler wrote into it is frozen at that round; later values can only
    go here. NaN / null / an absent value means "not computed".

Resolution rules
----------------
0. If models/benchmark_computed.yaml lists the id, its value wins
   (`computed_value`, else `computed_field`; NaN/null = not computed,
   never in band). A different value in the targets file is a stale copy:
   reported under `stale_copies` (HIGH when the stale copy is in band and
   the current value is not or was not computed, otherwise MEDIUM).
1. Else if `computed_value` is set on the target entry, use it directly.
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
MALFORMED (Phase 21 γ) if any target is malformed: non-mapping entry,
non-finite / non-numeric target or observed value, tolerance_factor
> 10, or tolerance_abs non-finite or wider than |target_value| without
a `tolerance_justification`. A malformed target used to be dropped
from the verdict, so one good target plus a typo'd one read PASS, and
an unbounded tolerance (factor 1000, abs .inf) passed anything.

Exit status
-----------
0  PASS (or no benchmarks declared yet — drafting window)
1  DRIFT recorded in benchmark_match.yaml
2  malformed inputs / run_dir missing
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from dataclasses import dataclass, field
from typing import Any

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib.safe_io import inside_dir  # noqa: E402
from lib.text_spans import resolve_dotpath as _resolve_dotpath  # noqa: E402

# Phase 21 γ: maximum multiplicative tolerance. A factor-10 band already
# spans two orders of magnitude; anything wider accepts any plausible
# output and is not a benchmark.
MAX_TOLERANCE_FACTOR = 10.0

# Closed verdict vocabulary (read by validate_critique_yaml.py).
VERDICTS = ("PASS", "DRIFT", "MALFORMED", "ABSENT", "PENDING")


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
    malformed: bool = False
    value_source: str = "models/benchmark_targets.yaml"

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
            "malformed": self.malformed,
            "value_source": self.value_source,
        }


def _resolve_computed_field(run_dir: str, ref: str) -> tuple[float | None, str]:
    """Parse '<rel-yaml>::<dot.path>' and resolve. Returns (value, diag)."""
    if "::" not in ref:
        return None, f"computed_field must be 'path::dotted.key', got {ref!r}"
    rel, key = ref.split("::", 1)
    p = os.path.join(run_dir, rel)
    if not inside_dir(run_dir, p):
        return None, f"computed_field artifact {rel} resolves outside the run directory"
    if not os.path.exists(p):
        return None, f"computed_field artifact {rel} not found"
    try:
        with open(p) as f:
            doc = yaml.safe_load(f) or {}
    except (yaml.YAMLError, OSError, UnicodeDecodeError, ValueError) as e:
        return None, f"could not parse {rel}: {e}"
    val = _resolve_dotpath(doc, key)
    if val is None:
        return None, f"key {key!r} absent in {rel}"
    if isinstance(val, bool):
        return None, f"value at {rel}::{key} is {val!r}, not a number"
    try:
        return float(val), ""
    except (TypeError, ValueError, OverflowError):
        return None, f"value at {rel}::{key} not numeric (got {val!r})"


COMPUTED_PATH = os.path.join("models", "benchmark_computed.yaml")


def _computed_values(run_dir: str) -> tuple[dict[str, tuple[float | None, str]] | None, str]:
    """({id: (value or None, why not)}, "") from models/benchmark_computed.yaml,
    ({}, "") when it is absent, or (None, error) when it cannot be used: fail
    closed, a computed-values file that cannot be read must not let the frozen
    copies in the targets file stand in for it."""
    p = os.path.join(run_dir, COMPUTED_PATH)
    if not os.path.lexists(p):
        return {}, ""
    if not inside_dir(run_dir, p):
        return None, f"{COMPUTED_PATH} resolves outside the run directory"
    try:
        with open(p, encoding="utf-8") as f:
            doc = yaml.safe_load(f)
    except (yaml.YAMLError, OSError, UnicodeDecodeError, ValueError) as e:
        return None, f"could not parse {COMPUTED_PATH}: {str(e)[:200]}"
    if not isinstance(doc, dict):
        return None, f"{COMPUTED_PATH}: top level must be a mapping"
    out: dict[str, tuple[float | None, str]] = {}
    entries = doc.get("benchmarks")
    for e in entries if isinstance(entries, list) else []:
        if not isinstance(e, dict) or e.get("id") is None:
            continue
        if "computed_value" in e:
            # Present means this round's value, even null/NaN ("not computed"):
            # a computed_field beside it is never consulted.
            v, diag = _finite_value(e["computed_value"])
        elif "computed_field" in e:
            v, diag = _resolve_observed(run_dir, e)
            if v is not None and not math.isfinite(v):
                v, diag = None, f"computed value is {v!r}"
        else:
            v, diag = None, "entry has no computed_value"
        out[str(e["id"])] = (v, diag)
    computed = doc.get("computed")
    for k, raw in (computed.items() if isinstance(computed, dict) else []):
        out.setdefault(str(k), _finite_value(raw))
    if entries is None and computed is None:
        return None, f"{COMPUTED_PATH} has neither `benchmarks:` nor `computed:`"
    return out, ""


def _finite_value(raw) -> tuple[float | None, str]:
    """(value, "") for a finite number, else (None, why): null, NaN, inf, text,
    a bool, or a number too large for a float are all "not computed"."""
    if isinstance(raw, bool):
        return None, f"computed value is {raw!r}"
    try:
        v = float(raw)
    except (TypeError, ValueError, OverflowError):
        v = None
    if v is None or not math.isfinite(v):
        try:
            shown = repr(raw)
        except ValueError:  # an integer past Python's digit limit
            shown = "an integer too large to print"
        shown = shown if len(shown) <= 40 else shown[:37] + "..."
        return None, f"computed value is {shown}"
    return v, ""


def _resolve_observed(run_dir: str, target: dict) -> tuple[float | None, str]:
    """Return (observed, diagnostic). observed is None on failure."""
    if "computed_value" in target and target["computed_value"] is not None:
        try:
            return float(target["computed_value"]), ""
        except (TypeError, ValueError, OverflowError):
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
    except (KeyError, TypeError, ValueError, OverflowError) as e:
        return None, None, f"target_value invalid: {e}"
    if not math.isfinite(tv):
        return None, None, f"target_value must be finite, got {tv!r}"
    factor = target.get("tolerance_factor")
    abs_tol = target.get("tolerance_abs")
    low: float | None = None
    high: float | None = None
    if factor is not None:
        try:
            f = float(factor)
            if f <= 0:
                return None, None, (f"tolerance_factor must be > 0, got {factor!r}")
            # Phase 20 β: reject sub-unit factor. A factor < 1 inverts
            # the band (low = tv/f > tv > tv*f = high), so the
            # `low <= observed <= high` check in `evaluate` is always
            # False and every benchmark fires DRIFT regardless of the
            # observed value. The natural-language reading
            # `tolerance_factor: 0.2` → "±20%" is wrong but plausible;
            # surface the misuse instead of silently breaking.
            if not math.isfinite(f) or f > MAX_TOLERANCE_FACTOR:
                return None, None, (
                    f"tolerance_factor {factor!r} exceeds the maximum "
                    f"{MAX_TOLERANCE_FACTOR:g}; a band that wide is not a "
                    f"benchmark")
            if f < 1.0:
                return None, None, (
                    f"tolerance_factor must be ≥ 1.0 (got {factor!r}); "
                    f"factor is multiplicative — for ±20% bands use "
                    f"`tolerance_factor: 1.25` or `tolerance_abs: "
                    f"<0.2*target_value>`"
                )
            # For target=0 the factor band collapses; require abs in that case.
            if tv == 0 and abs_tol is None:
                return None, None, ("target_value == 0 with no "
                                    "tolerance_abs; band undefined")
            low = tv / f if tv >= 0 else tv * f
            high = tv * f if tv >= 0 else tv / f
        except (TypeError, ValueError, OverflowError) as e:
            return None, None, f"tolerance_factor invalid: {e}"
    if abs_tol is not None:
        try:
            a = float(abs_tol)
            if a < 0:
                return None, None, (f"tolerance_abs must be ≥ 0, got {abs_tol!r}")
            if not math.isfinite(a):
                return None, None, f"tolerance_abs must be finite, got {abs_tol!r}"
            justification = str(target.get("tolerance_justification") or "").strip()
            if tv != 0 and a > abs(tv) and not justification:
                return None, None, (
                    f"tolerance_abs {a:g} is wider than |target_value| "
                    f"{abs(tv):g}; add `tolerance_justification:` or tighten "
                    f"the band")
            ab_low = tv - a
            ab_high = tv + a
            # Intersection
            low = max(low, ab_low) if low is not None else ab_low
            high = min(high, ab_high) if high is not None else ab_high
        except (TypeError, ValueError, OverflowError) as e:
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
    except (yaml.YAMLError, OSError, UnicodeDecodeError, ValueError) as e:
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
    fresh, fresh_err = _computed_values(run_dir)
    if fresh is None:
        return {
            "verdict": "MALFORMED", "error": fresh_err,
            "n_benchmarks": 0, "n_drifted": 0,
            "missing_computed": 0, "results": [],
        }
    results: list[BenchResult] = []
    stale: list[dict] = []
    missing_computed = 0
    for i, t in enumerate(targets):
        if not isinstance(t, dict):
            # Phase 21 γ: a non-mapping entry is a malformed target, not
            # an absent one.
            results.append(BenchResult(
                id=f"benchmark_{i}", metric="?", target_value=float("nan"),
                observed=None, band_low=None, band_high=None,
                within_band=False, source="?",
                diagnostic=f"benchmarks[{i}] is not a mapping ({t!r})",
                malformed=True,
            ))
            continue
        tid = str(t.get("id") or f"benchmark_{i}")
        metric = str(t.get("metric") or tid)
        source = str(t.get("source") or "?")
        try:
            tv = float(t.get("target_value"))
        except (TypeError, ValueError, OverflowError) as e:
            results.append(BenchResult(
                id=tid, metric=metric, target_value=float("nan"),
                observed=None, band_low=None, band_high=None,
                within_band=False, source=source,
                diagnostic=f"target_value invalid: {e}",
                malformed=True,
            ))
            continue
        low, high, band_diag = _band(t)
        observed, obs_diag = _resolve_observed(run_dir, t)
        value_source = "models/benchmark_targets.yaml"
        if tid in fresh:
            copy, (observed, fdiag) = observed, fresh[tid]
            value_source = COMPUTED_PATH
            obs_diag = f"not computed in {COMPUTED_PATH} ({fdiag})" if observed is None else ""
            stale_entry = _stale(tid, copy, observed, low, high)
            if stale_entry:
                stale.append(stale_entry)
        if observed is not None and not math.isfinite(observed):
            results.append(BenchResult(
                id=tid, metric=metric, target_value=tv,
                observed=None, band_low=low, band_high=high,
                within_band=False, source=source,
                diagnostic=f"computed value is not finite ({observed!r})",
                malformed=True,
            ))
            continue
        if observed is None:
            missing_computed += 1
            results.append(BenchResult(
                id=tid, metric=metric, target_value=tv,
                observed=None, band_low=low, band_high=high,
                within_band=False, source=source,
                diagnostic=obs_diag or band_diag, value_source=value_source,
            ))
            continue
        if low is None or high is None:
            results.append(BenchResult(
                id=tid, metric=metric, target_value=tv,
                observed=observed, band_low=low, band_high=high,
                within_band=False, source=source,
                diagnostic=band_diag, malformed=True,
            ))
            continue
        within = low <= observed <= high
        results.append(BenchResult(
            id=tid, metric=metric, target_value=tv,
            observed=observed, band_low=low, band_high=high,
            within_band=within, source=source,
            diagnostic="", value_source=value_source,
        ))

    n_malformed = sum(1 for r in results if r.malformed)
    n_drifted = sum(1 for r in results
                    if r.observed is not None and not r.within_band
                    and not r.malformed)
    n_total = len([r for r in results if r.observed is not None])
    if n_malformed:
        verdict = "MALFORMED"
    elif n_drifted > 0 or missing_computed > 0:
        verdict = "DRIFT"
    elif n_total > 0:
        verdict = "PASS"
    else:
        verdict = "PENDING"
    out = {
        "verdict": verdict,
        "n_benchmarks": len(results),
        "n_with_observed": n_total,
        "n_drifted": n_drifted,
        "missing_computed": missing_computed,
        "n_malformed": n_malformed,
        "results": [r.to_dict() for r in results],
        "stale_copies": stale,
    }
    if n_malformed:
        out["error"] = "; ".join(f"{r.id}: {r.diagnostic}"
                                 for r in results if r.malformed)[:500]
    return out


def _stale(tid: str, copy, current, low, high) -> dict | None:
    """A value in the targets file that differs from the current computed value
    (or stands where it was not computed). HIGH when the stale copy is in band
    and the current value is not, or was not computed: the copy alone would
    make the benchmark pass. Otherwise MEDIUM. None when they agree, when the
    targets file holds no usable value, or when the band is undefined."""
    if copy is None or not math.isfinite(copy) or low is None or high is None:
        return None
    if current is not None and math.isclose(copy, current, rel_tol=1e-9, abs_tol=1e-12):
        return None
    copy_in = low <= copy <= high
    cur_in = current is not None and low <= current <= high
    return {"id": tid, "targets_value": copy, "computed_value": current,
            "severity": "HIGH" if copy_in and not cur_in else "MEDIUM"}


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

    # T10 (Phase 20 β): sub-unit tolerance_factor is rejected before
    # producing an inverted, always-DRIFT band. `tolerance_factor: 0.5`
    # naively reads as "±50%" but is multiplicative — it would invert
    # `low/high` and silently fail every benchmark.
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "models", "benchmark_targets.yaml"), "w") as f:
            yaml.safe_dump({
                "benchmarks": [
                    {"id": "b1", "metric": "incidence", "target_value": 400,
                     "tolerance_factor": 0.5, "source": "WMR 2023",
                     "computed_value": 387},
                ]
            }, f)
        rep = evaluate(d)
        # Should be a band-construction failure (DRIFT with diagnostic),
        # not silently inverting bands and computing False inclusion.
        r = rep["results"][0]
        ok(r.get("band_low") is None and r.get("band_high") is None,
           f"T10: sub-unit factor must NOT yield a band, got {r}")
        ok("must be ≥ 1.0" in (r.get("diagnostic") or ""),
           f"T10: diagnostic must explain factor ≥ 1, got {r}")

    # T10b (Phase 20 β): tolerance_factor exactly 1.0 produces a
    # zero-width band — narrow but mathematically valid.
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "models", "benchmark_targets.yaml"), "w") as f:
            yaml.safe_dump({
                "benchmarks": [
                    {"id": "b1", "metric": "incidence", "target_value": 400,
                     "tolerance_factor": 1.0, "source": "WMR 2023",
                     "computed_value": 400},
                ]
            }, f)
        rep = evaluate(d)
        ok(rep["verdict"] == "PASS",
           f"T10b: factor=1.0 with exact match should PASS, got {rep}")

    # T11 (Phase 21 γ): a malformed target makes the whole verdict
    # MALFORMED — one good target no longer masks a typo'd one, and
    # unbounded tolerances / non-finite values / non-mappings are not
    # silently accepted or skipped.
    good = {"id": "ok", "metric": "measles_incidence", "target_value": 120,
            "tolerance_factor": 1.5, "source": "WHO 2024",
            "computed_value": 118}
    bad_cases = {
        "str_target": {**good, "id": "b", "target_value": "~0.3"},
        "huge_factor": {**good, "id": "b", "tolerance_factor": 1000},
        "inf_abs": {"id": "b", "metric": "m", "target_value": 10,
                    "tolerance_abs": float("inf"), "source": "s",
                    "computed_value": 1e9},
        "wide_abs": {"id": "b", "metric": "m", "target_value": 10,
                     "tolerance_abs": 50, "source": "s", "computed_value": 55},
        "nan_observed": {**good, "id": "b", "computed_value": float("nan")},
        "non_mapping": "incidence 120 per 100k",
    }
    for label, bad in bad_cases.items():
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "models"))
            with open(os.path.join(d, "models", "benchmark_targets.yaml"), "w") as f:
                yaml.safe_dump({"benchmarks": [good, bad]}, f)
            rep = evaluate(d)
            ok(rep["verdict"] == "MALFORMED" and rep.get("n_malformed") == 1,
               f"T11 {label}: expected MALFORMED, got {rep['verdict']} "
               f"(n_malformed={rep.get('n_malformed')})")
    # T11b: a wide absolute tolerance with a written justification is ok.
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "models", "benchmark_targets.yaml"), "w") as f:
            yaml.safe_dump({"benchmarks": [{
                "id": "b", "metric": "m", "target_value": 10,
                "tolerance_abs": 50, "source": "s", "computed_value": 55,
                "tolerance_justification": "survey CI spans 0-60"}]}, f)
        rep = evaluate(d)
        ok(rep["verdict"] == "PASS", f"T11b: justified band should PASS, got {rep}")
    # T11c: computed_field supports list indices via the shared resolver.
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "out.yaml"), "w") as f:
            yaml.safe_dump({"series": [{"v": 5.0}, {"v": 7.0}]}, f)
        with open(os.path.join(d, "models", "benchmark_targets.yaml"), "w") as f:
            yaml.safe_dump({"benchmarks": [{
                "id": "b", "metric": "m", "target_value": 7,
                "tolerance_factor": 1.2, "source": "s",
                "computed_field": "out.yaml::series.1.v"}]}, f)
        rep = evaluate(d)
        ok(rep["verdict"] == "PASS", f"T11c: indexed computed_field, got {rep}")
    # T11d (Phase 45 β): a computed_field through a symlink out of the run is not read.
    with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as outside:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(outside, "out.yaml"), "w") as f:
            yaml.safe_dump({"v": 7.0}, f)
        os.symlink(outside, os.path.join(d, "ext"))
        with open(os.path.join(d, "models", "benchmark_targets.yaml"), "w") as f:
            yaml.safe_dump({"benchmarks": [{
                "id": "b", "metric": "m", "target_value": 7,
                "tolerance_factor": 1.2, "source": "s",
                "computed_field": "ext/out.yaml::v"}]}, f)
        rep = evaluate(d)
        ok(rep["missing_computed"] == 1 and "outside the run" in rep["results"][0]["diagnostic"],
           f"T11d: symlinked computed_field must not be read, got {rep}")

    # T12 (Phase 45 γ): models/benchmark_computed.yaml holds the current values;
    # the locked targets file's computed_value is a frozen copy.
    def fresh_case(computed_text, targets=None):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "models"))
            with open(os.path.join(d, "models", "benchmark_targets.yaml"), "w") as f:
                yaml.safe_dump({"benchmarks": targets or [
                    {"id": "b", "metric": "m", "target_value": 40, "tolerance_factor": 5.0,
                     "source": "s", "computed_value": 35.0},
                    {"id": "c", "metric": "n", "target_value": 10, "tolerance_factor": 2.0,
                     "source": "s", "computed_value": 9.0}]}, f)
            if computed_text is not None:
                with open(os.path.join(d, "models", "benchmark_computed.yaml"), "w") as f:
                    f.write(computed_text)
            with open(os.path.join(d, "models", "out.yaml"), "w") as f:
                f.write("v: 30.0\n")
            return evaluate(d)

    by = lambda rep, i: next(r for r in rep["results"] if r["id"] == i)  # noqa: E731
    # T12a: NaN in the computed file = not computed, never in band; the in-band copy is a HIGH stale copy.
    rep = fresh_case("benchmarks:\n- id: b\n  computed_value: .nan\n")
    ok(by(rep, "b")["observed"] is None and rep["missing_computed"] == 1 and rep["verdict"] == "DRIFT"
       and "not computed in models/benchmark_computed.yaml" in by(rep, "b")["diagnostic"]
       and rep["stale_copies"] == [{"id": "b", "targets_value": 35.0, "computed_value": None, "severity": "HIGH"}],
       f"T12a: NaN computed value must be not computed + HIGH stale, got {rep}")
    # T12b: a fresh out-of-band value wins over the in-band copy (HIGH stale, DRIFT).
    rep = fresh_case("computed:\n  b: 500.0\n")
    ok(by(rep, "b")["observed"] == 500.0 and not by(rep, "b")["within_band"] and rep["verdict"] == "DRIFT"
       and by(rep, "b")["value_source"] == "models/benchmark_computed.yaml"
       and [s["severity"] for s in rep["stale_copies"]] == ["HIGH"], f"T12b: fresh value wins, got {rep}")
    # T12c: a fresh in-band value replacing an out-of-band copy is MEDIUM and PASS.
    rep = fresh_case("computed:\n  b: 30.0\n  c: 9.5\n",
                     targets=[{"id": "b", "metric": "m", "target_value": 40, "tolerance_factor": 5.0,
                               "source": "s", "computed_value": 900.0},
                              {"id": "c", "metric": "n", "target_value": 10, "tolerance_factor": 2.0,
                               "source": "s", "computed_value": 9.5}])
    ok(rep["verdict"] == "PASS" and rep["stale_copies"] == [
        {"id": "b", "targets_value": 900.0, "computed_value": 30.0, "severity": "MEDIUM"}],
       f"T12c: stale out-of-band copy is MEDIUM, equal copy silent, got {rep}")
    # T12d: computed_field in the computed file resolves; an id it does not list keeps the targets value.
    rep = fresh_case("benchmarks:\n- id: b\n  computed_field: models/out.yaml::v\n")
    ok(by(rep, "b")["observed"] == 30.0 and by(rep, "c")["observed"] == 9.0
       and by(rep, "c")["value_source"] == "models/benchmark_targets.yaml" and rep["verdict"] == "PASS"
       and [s["severity"] for s in rep["stale_copies"]] == ["MEDIUM"], f"T12d: computed_field + fallback, got {rep}")
    # T12e: no computed file: unchanged behaviour, no stale copies.
    rep = fresh_case(None)
    ok(rep["verdict"] == "PASS" and rep["stale_copies"] == [], f"T12e: absent computed file, got {rep}")
    # T12f: an unusable computed file fails closed (MALFORMED), never falls back to the frozen copies.
    for txt, why in (("a: [1,\n", "parse"), ("- 1\n", "mapping"), ("note: x\n", "neither")):
        rep = fresh_case(txt)
        ok(rep["verdict"] == "MALFORMED" and why in rep.get("error", ""), f"T12f {why}: got {rep}")
    with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as outside:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "models", "benchmark_targets.yaml"), "w") as f:
            yaml.safe_dump({"benchmarks": [{"id": "b", "metric": "m", "target_value": 40,
                                            "tolerance_factor": 5.0, "source": "s", "computed_value": 35.0}]}, f)
        with open(os.path.join(outside, "c.yaml"), "w") as f:
            f.write("computed:\n  b: 35.0\n")
        os.symlink(os.path.join(outside, "c.yaml"), os.path.join(d, "models", "benchmark_computed.yaml"))
        rep = evaluate(d)
        ok(rep["verdict"] == "MALFORMED" and "outside the run" in rep.get("error", ""),
           f"T12g: symlinked computed file must not be read, got {rep}")
    # T12h: an explicit `computed_value: null` (or NaN) is not computed; a computed_field
    # beside it is not consulted, so a locked DRIFT cannot turn into a PASS through it.
    drift_target = [{"id": "b", "metric": "m", "target_value": 100, "tolerance_abs": 10,
                     "source": "s", "computed_value": 500.0}]
    for null in ("null", ".nan"):
        rep = fresh_case(f"benchmarks:\n- id: b\n  computed_value: {null}\n  computed_field: models/out.yaml::v\n",
                         targets=drift_target)
        ok(rep["verdict"] == "DRIFT" and by(rep, "b")["observed"] is None and rep["missing_computed"] == 1,
           f"T12h {null}: explicit not-computed must not fall through to computed_field, got {rep}")
    # T12i: a number too large for a float never raises; it is a non-passing result.
    huge = "9" * 400
    for label, text, targets in (
            ("computed list", f"benchmarks:\n- id: b\n  computed_value: {huge}\n", None),
            ("computed mapping", f"computed:\n  b: {huge}\n", None),
            ("targets computed_value", None, [{"id": "b", "metric": "m", "target_value": 40,
                                                "tolerance_factor": 5.0, "source": "s", "computed_value": int(huge)}]),
            ("targets target_value", None, [{"id": "b", "metric": "m", "target_value": int(huge),
                                              "tolerance_factor": 5.0, "source": "s", "computed_value": 35.0}]),
            ("targets tolerance", None, [{"id": "b", "metric": "m", "target_value": 40,
                                           "tolerance_abs": int(huge), "source": "s", "computed_value": 35.0}])):
        try:
            rep = fresh_case(text, targets=targets)
        except Exception as e:  # noqa: BLE001
            rep = {"verdict": f"raised {type(e).__name__}"}
        ok(rep["verdict"] in ("DRIFT", "MALFORMED") and not by(rep, "b")["within_band"],
           f"T12i {label}: a huge number must give a non-passing result, got {rep}")
    # T12j: an integer past Python's digit limit (YAML raises ValueError while
    # loading, or repr() does while printing) never raises.
    huger = "9" * 5000
    for label, text in (("decimal", f"computed:\n  b: {huger}\n"),
                        ("hex", "computed:\n  b: 0x" + "f" * 4000 + "\n")):
        try:
            rep = fresh_case(text, targets=None)
        except Exception as e:  # noqa: BLE001
            rep = {"verdict": f"raised {type(e).__name__}"}
        ok(rep["verdict"] in ("DRIFT", "MALFORMED"),
           f"T12j {label}: a huge integer must not raise, got {rep.get('verdict')}")
    # T12k: a boolean reached through computed_field is not the number 1.
    with tempfile.TemporaryDirectory() as td:
        os.makedirs(os.path.join(td, "models"))
        with open(os.path.join(td, "models", "out.yaml"), "w") as f:
            f.write("raw: true\n")
        v, diag = _resolve_computed_field(td, "models/out.yaml::raw")
        ok(v is None and "not a number" in diag,
           f"T12k: a boolean computed_field must not resolve to a number, got {(v, diag)}")

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
