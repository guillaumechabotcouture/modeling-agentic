#!/usr/bin/env python3
"""
Phase 13 Commit α — disease-agnostic structural sanity checks.

The Phase 12 RESUME run (190855) shipped DECLARE_SCOPE with internally
self-consistent numbers — but no gate cross-checked aggregate impact
against the model's own internal relationships. Headline: 48.4M cases
averted on 32M people in 123 LGAs, claiming to avert 24% of Nigeria's
3-year case burden. Three obvious questions a domain expert asks
("does that scale match the units?", "do the shares close to 100%?",
"does the structural-uncertainty bound show up in the headline?")
were unanswered because validation against external literature is
circular and topical, while hard scale gates would block legitimate
findings.

This script enforces eight DISEASE-AGNOSTIC structural relationships
that hold in any optimization-under-budget problem. The modeler
writes `models/sanity_schema.yaml` declaring abstract slots (outcome
name, baseline reservoir, exposure unit, shares, derived-consistency
formulas, composite-dimension windows, structural-uncertainty
bounds, outlier sniff). This script reads the schema and runs eight
checks. None of the check code knows what disease/topic the run is
about — only the schema does.

Schema (modeler writes ~30 lines):

```yaml
outcome:
  name: DALYs averted
  unit: DALYs
  baseline_total: 16_600_000
  averted_point: 8_390_000

exposure:
  unit: people
  total_in_allocated: 32_000_000
  rate_per_unit_per_year: 0.5
  years: 3

allocation:
  units_total: 774               # declared universe size; Phase 14 α
                                  # cross-checks against allocation CSV row count
  units_allocated: 123
  budget: 319_829_076

exact_counts: [lga_count, package_count, of_n_denominator]
                                  # Phase 14 β opt-in: count classes that
                                  # must match the CSV exactly. Drift > 0
                                  # fires MEDIUM `exact_count_drift`
                                  # regardless of magnitude. Backward
                                  # compatible — if absent, default Phase 13 β
                                  # 5%/25% thresholds apply.

shares:
  - name: zone_budget
    sums_to: 1.0
    tol: 0.005
    values: {NW: 0.781, NC: 0.108, NE: 0.111}

counterfactual:
  name: null_model
  averted: 421_000
  optimized_vs_null_acceptable: [1.5, 100]

derived_consistency:
  - primary: cases_averted
    primary_value: 48_400_000
    derived: deaths_averted
    derived_actual: 194_000
    formula: "primary * cfr"
    constants: {cfr: 0.004}
    tol: 0.10

composite_dimensions:
  - name: DALY_per_death
    value: 43.2
    window: [15, 60]

structural_uncertainty:
  - source: within_zone_heterogeneity.yaml
    verdict: INCONCLUSIVE
    lower_bound: 7_500_000
    must_appear_in: report.md

outlier_sniff:
  metric: dalys_averted_per_dollar
  rule: max_over_median
  threshold: 10.0
  csv_path: models/allocation_result.csv
  numerator_col: dalys_averted
  denominator_col: total_cost_usd
```

Each section is OPTIONAL. Missing sections silently skip their check
(they don't fire MEDIUMs about themselves missing — that's the
schema-missing concern, handled in validate_critique_yaml.py). The
script fires MALFORMED only if the schema is YAML-broken or has the
wrong shape for a section the modeler DID declare.

Usage:
    python3 scripts/sanity_checks.py <run_dir>
    python3 scripts/sanity_checks.py --self-test
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys

try:
    import yaml
except ImportError:
    print("ERROR: pyyaml not installed", file=sys.stderr)
    sys.exit(2)


# Module-level regexes — compiled once, scanned per text (not per check).
_NUMERIC_TOKEN_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(M|million|k|thousand)?",
    re.IGNORECASE,
)
_NUMERIC_COMMA_RE = re.compile(r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?)")


# Whitelisted AST nodes for derived_consistency formula evaluation.
# No calls, no attributes, no subscripts, no imports — only arithmetic
# on declared variables.
_FORMULA_ALLOWED_NODES = (
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant, ast.Name,
    ast.Load, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv,
    ast.Mod, ast.Pow, ast.USub, ast.UAdd,
)


def _safe_eval_formula(formula: str, vars_: dict) -> float:
    """Evaluate a constants-only arithmetic expression. Allowed:
    +, -, *, /, //, %, **, parens, and references to keys in vars_.
    Disallowed: function calls, attributes, subscripts, comparisons,
    booleans, anything else. Raises ValueError on invalid input."""
    try:
        tree = ast.parse(formula, mode="eval")
    except SyntaxError as e:
        raise ValueError(f"formula {formula!r} parse error: {e}")
    for node in ast.walk(tree):
        if not isinstance(node, _FORMULA_ALLOWED_NODES):
            raise ValueError(
                f"formula {formula!r} uses disallowed construct "
                f"{type(node).__name__}; only arithmetic on declared "
                f"variables is permitted")

    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant):
            if not isinstance(n.value, (int, float)):
                raise ValueError(
                    f"non-numeric constant {n.value!r} in formula")
            return n.value
        if isinstance(n, ast.Name):
            if n.id not in vars_:
                raise ValueError(
                    f"formula references undeclared variable {n.id!r}; "
                    f"declared: {sorted(vars_)}")
            return vars_[n.id]
        if isinstance(n, ast.UnaryOp):
            v = ev(n.operand)
            if isinstance(n.op, ast.USub):
                return -v
            return +v
        if isinstance(n, ast.BinOp):
            l, r = ev(n.left), ev(n.right)
            op = n.op
            if isinstance(op, ast.Add):
                return l + r
            if isinstance(op, ast.Sub):
                return l - r
            if isinstance(op, ast.Mult):
                return l * r
            if isinstance(op, ast.Div):
                return l / r
            if isinstance(op, ast.FloorDiv):
                return l // r
            if isinstance(op, ast.Mod):
                return l % r
            if isinstance(op, ast.Pow):
                return l ** r
        raise ValueError(f"unhandled node {type(n).__name__}")

    return float(ev(tree))


def _check_mass_balance(schema: dict) -> list[dict]:
    """outcome.averted_point ≤ 0.95 × outcome.baseline_total."""
    sec = schema.get("outcome")
    if not isinstance(sec, dict):
        return []
    averted = sec.get("averted_point")
    baseline = sec.get("baseline_total")
    if averted is None or baseline is None:
        return []
    try:
        averted, baseline = float(averted), float(baseline)
    except (TypeError, ValueError):
        return [{"id": "mass_balance", "passed": False,
                 "claim": f"outcome.averted_point or baseline_total "
                          f"non-numeric (got {averted!r}, {baseline!r})"}]
    if baseline <= 0:
        return [{"id": "mass_balance", "passed": False,
                 "claim": f"outcome.baseline_total must be positive "
                          f"(got {baseline})"}]
    frac = averted / baseline
    if frac > 0.95:
        return [{"id": "mass_balance", "passed": False,
                 "claim": (f"outcome.averted_point ({averted:,.0f}) is "
                           f"{frac*100:.1f}% of baseline_total "
                           f"({baseline:,.0f}). Averting >95% of what's "
                           f"at stake is biologically/physically extreme; "
                           f"either the baseline reservoir is mis-scoped or "
                           f"the averted estimate is too high.")}]
    return [{"id": "mass_balance", "passed": True,
             "claim": f"averted/baseline = {frac*100:.1f}% (≤ 95%)"}]


def _check_per_unit_intensity(schema: dict) -> list[dict]:
    """outcome.averted ≤ exposure.total × rate × years (per-unit)."""
    out = schema.get("outcome") or {}
    exp = schema.get("exposure") or {}
    averted = out.get("averted_point")
    total = exp.get("total_in_allocated")
    rate = exp.get("rate_per_unit_per_year")
    years = exp.get("years", 1)
    if averted is None or total is None or rate is None:
        return []
    try:
        averted, total, rate, years = (
            float(averted), float(total), float(rate), float(years))
    except (TypeError, ValueError):
        return [{"id": "per_unit_intensity", "passed": False,
                 "claim": "exposure or outcome fields non-numeric"}]
    cap = total * rate * years
    if cap <= 0:
        return [{"id": "per_unit_intensity", "passed": False,
                 "claim": (f"exposure.total × rate × years must be "
                           f"positive (got {cap})")}]
    frac = averted / cap
    if frac > 1.0:
        return [{"id": "per_unit_intensity", "passed": False,
                 "claim": (f"averted ({averted:,.0f}) exceeds the "
                           f"unit-cap (exposure.total × rate × years = "
                           f"{cap:,.0f}). Per-unit averted intensity "
                           f"{frac:.2f} > 1.0 — the model claims to "
                           f"avert more than each exposed unit could "
                           f"experience.")}]
    return [{"id": "per_unit_intensity", "passed": True,
             "claim": f"averted/cap = {frac:.2f} (≤ 1.0)"}]


def _check_share_closure(schema: dict) -> list[dict]:
    """For each shares[i]: abs(sum(values) - sums_to) ≤ tol."""
    shares = schema.get("shares")
    if not isinstance(shares, list):
        return []
    out: list[dict] = []
    for i, sh in enumerate(shares):
        if not isinstance(sh, dict):
            out.append({"id": f"share_closure_{i}", "passed": False,
                        "claim": f"shares[{i}] not a mapping"})
            continue
        name = sh.get("name", f"share_{i}")
        sums_to = sh.get("sums_to", 1.0)
        tol = sh.get("tol", 0.005)
        values = sh.get("values")
        if not isinstance(values, dict) or not values:
            out.append({"id": f"share_closure_{name}", "passed": False,
                        "claim": (f"shares[{i}] ({name!r}) missing "
                                  f"'values' mapping")})
            continue
        try:
            total = sum(float(v) for v in values.values())
            sums_to = float(sums_to)
            tol = float(tol)
        except (TypeError, ValueError):
            out.append({"id": f"share_closure_{name}", "passed": False,
                        "claim": (f"shares[{i}] ({name!r}) has non-numeric "
                                  f"values")})
            continue
        diff = total - sums_to
        if abs(diff) > tol:
            out.append({"id": f"share_closure_{name}", "passed": False,
                        "claim": (f"share {name!r} sums to {total:.4f}, "
                                  f"expected {sums_to:.4f} ± {tol:.4f} "
                                  f"(off by {diff:+.4f})")})
        else:
            out.append({"id": f"share_closure_{name}", "passed": True,
                        "claim": f"share {name!r} sums to {total:.4f} ✓"})
    return out


def _check_derived_consistency(schema: dict) -> list[dict]:
    """For each entry: |derived_actual - eval(formula)| / derived_actual ≤ tol."""
    entries = schema.get("derived_consistency")
    if not isinstance(entries, list):
        return []
    out: list[dict] = []
    for i, e in enumerate(entries):
        if not isinstance(e, dict):
            out.append({"id": f"derived_consistency_{i}", "passed": False,
                        "claim": f"derived_consistency[{i}] not a mapping"})
            continue
        primary = e.get("primary", f"primary_{i}")
        derived = e.get("derived", f"derived_{i}")
        primary_val = e.get("primary_value")
        derived_actual = e.get("derived_actual")
        formula = e.get("formula")
        constants = e.get("constants") or {}
        tol = e.get("tol", 0.10)
        if primary_val is None or derived_actual is None or not formula:
            out.append({"id": f"derived_consistency_{primary}_to_{derived}",
                        "passed": False,
                        "claim": (f"derived_consistency[{i}] missing "
                                  f"primary_value, derived_actual, or "
                                  f"formula")})
            continue
        try:
            vars_ = {"primary": float(primary_val)}
            for k, v in constants.items():
                vars_[k] = float(v)
            tol = float(tol)
            derived_actual = float(derived_actual)
            expected = _safe_eval_formula(formula, vars_)
        except (ValueError, TypeError) as exc:
            out.append({"id": f"derived_consistency_{primary}_to_{derived}",
                        "passed": False,
                        "claim": (f"derived_consistency[{i}] eval error: "
                                  f"{exc}")})
            continue
        if derived_actual == 0:
            drift = float("inf") if expected != 0 else 0.0
        else:
            drift = abs(expected - derived_actual) / abs(derived_actual)
        if drift > tol:
            out.append({"id": f"derived_consistency_{primary}_to_{derived}",
                        "passed": False,
                        "claim": (f"derived {derived!r} actual="
                                  f"{derived_actual:,.4g}, formula "
                                  f"{formula!r} predicts {expected:,.4g} "
                                  f"({drift*100:.1f}% drift > "
                                  f"{tol*100:.1f}% tol)")})
        else:
            out.append({"id": f"derived_consistency_{primary}_to_{derived}",
                        "passed": True,
                        "claim": (f"{derived!r} formula consistent "
                                  f"({drift*100:.1f}% drift)")})
    return out


def _check_composite_dimensions(schema: dict) -> list[dict]:
    """For each composite: value ∈ window."""
    comps = schema.get("composite_dimensions")
    if not isinstance(comps, list):
        return []
    out: list[dict] = []
    for i, c in enumerate(comps):
        if not isinstance(c, dict):
            out.append({"id": f"composite_dim_{i}", "passed": False,
                        "claim": f"composite_dimensions[{i}] not a mapping"})
            continue
        name = c.get("name", f"composite_{i}")
        val = c.get("value")
        win = c.get("window")
        if val is None or not isinstance(win, list) or len(win) != 2:
            out.append({"id": f"composite_dim_{name}", "passed": False,
                        "claim": (f"composite {name!r} missing value or "
                                  f"window:[lo, hi]")})
            continue
        try:
            val, lo, hi = float(val), float(win[0]), float(win[1])
        except (TypeError, ValueError):
            out.append({"id": f"composite_dim_{name}", "passed": False,
                        "claim": (f"composite {name!r} non-numeric "
                                  f"value/window")})
            continue
        if not (lo <= val <= hi):
            out.append({"id": f"composite_dim_{name}", "passed": False,
                        "claim": (f"composite {name!r} = {val:.3g} outside "
                                  f"window [{lo:.3g}, {hi:.3g}]")})
        else:
            out.append({"id": f"composite_dim_{name}", "passed": True,
                        "claim": (f"composite {name!r} = {val:.3g} "
                                  f"in [{lo:.3g}, {hi:.3g}] ✓")})
    return out


def _check_counterfactual_ratio(schema: dict) -> list[dict]:
    """outcome.averted / counterfactual.averted ∈ acceptable_range."""
    cf = schema.get("counterfactual")
    out_sec = schema.get("outcome") or {}
    if not isinstance(cf, dict):
        return []
    optimized = out_sec.get("averted_point")
    null_av = cf.get("averted")
    rng = cf.get("optimized_vs_null_acceptable", [1.5, 100])
    if optimized is None or null_av is None:
        return []
    try:
        optimized, null_av = float(optimized), float(null_av)
        lo, hi = float(rng[0]), float(rng[1])
    except (TypeError, ValueError, IndexError):
        return [{"id": "counterfactual_ratio", "passed": False,
                 "claim": "counterfactual fields non-numeric or "
                          "acceptable_range malformed"}]
    if null_av <= 0:
        return [{"id": "counterfactual_ratio", "passed": False,
                 "claim": (f"counterfactual.averted must be positive "
                           f"(got {null_av}); a counterfactual that "
                           f"averts nothing inflates ratios artificially")}]
    ratio = optimized / null_av
    if not (lo <= ratio <= hi):
        return [{"id": "counterfactual_ratio", "passed": False,
                 "claim": (f"optimized/null = {ratio:.1f}× outside "
                           f"[{lo:.1f}, {hi:.1f}]. Below {lo:.1f}× = "
                           f"optimization adds little; above {hi:.1f}× "
                           f"= the null comparator is too weak (likely "
                           f"'no interventions at all' rather than "
                           f"'current standard of care'). Either adjust "
                           f"the counterfactual or document the "
                           f"definition explicitly.")}]
    return [{"id": "counterfactual_ratio", "passed": True,
             "claim": f"optimized/null = {ratio:.1f}× ∈ "
                      f"[{lo:.1f}, {hi:.1f}]"}]


def _check_heterogeneity_carryforward(schema: dict, run_dir: str) -> list[dict]:
    """For each entry: lower_bound numeric token must appear in the
    must_appear_in document, and verdict must be acknowledged."""
    entries = schema.get("structural_uncertainty")
    if not isinstance(entries, list):
        return []
    out: list[dict] = []
    for i, e in enumerate(entries):
        if not isinstance(e, dict):
            out.append({"id": f"heterogeneity_carryforward_{i}",
                        "passed": False,
                        "claim": f"structural_uncertainty[{i}] not a mapping"})
            continue
        verdict = e.get("verdict", "BOUNDED")
        if verdict == "BOUNDED":
            # Bounded structural uncertainty: nothing to carry forward.
            continue
        source = e.get("source", "<unknown>")
        target_rel = e.get("must_appear_in")
        lower_bound = e.get("lower_bound")
        check_id = f"heterogeneity_carryforward_{source}"
        if not target_rel or lower_bound is None:
            out.append({"id": check_id, "passed": False,
                        "claim": (f"structural_uncertainty[{i}] "
                                  f"({source!r}, verdict {verdict}) "
                                  f"missing must_appear_in or "
                                  f"lower_bound")})
            continue
        target_path = os.path.join(run_dir, target_rel)
        if not os.path.exists(target_path):
            out.append({"id": check_id, "passed": False,
                        "claim": (f"target {target_rel!r} not found in "
                                  f"run_dir; cannot verify "
                                  f"{lower_bound} appears")})
            continue
        try:
            with open(target_path, encoding="utf-8") as f:
                text = f.read()
        except (OSError, UnicodeDecodeError):
            out.append({"id": check_id, "passed": False,
                        "claim": f"could not read {target_rel!r}"})
            continue
        # Match human-formatted variants: "7.50M", "7,500,000",
        # "7500000", "7.5 million". Tolerant ±2% so "7.5M" matches
        # 7_500_000.
        try:
            lb = float(lower_bound)
        except (TypeError, ValueError):
            out.append({"id": check_id, "passed": False,
                        "claim": f"lower_bound {lower_bound!r} non-numeric"})
            continue
        found = _text_contains_value(text, lb, tol_frac=0.02)
        if not found:
            out.append({"id": check_id, "passed": False,
                        "claim": (f"structural-uncertainty lower bound "
                                  f"{lb:,.4g} (from {source!r}, verdict "
                                  f"{verdict}) does NOT appear in "
                                  f"{target_rel!r}. The {verdict} verdict "
                                  f"is documented in the source artifact "
                                  f"but not surfaced where readers see the "
                                  f"headline. Add the lower bound to the "
                                  f"target file (suggested section: "
                                  f"{e.get('section_hint', '§Executive Summary')}).")})
        else:
            out.append({"id": check_id, "passed": True,
                        "claim": (f"lower bound {lb:,.4g} appears in "
                                  f"{target_rel!r} ✓")})
    return out


def _text_contains_value(text: str, target: float,
                          tol_frac: float = 0.02) -> bool:
    """Return True if `text` contains a numeric token within tol_frac
    of `target`. Handles M/million/k/thousand suffixes and
    comma-formatted integers. Short-circuits on first match."""
    tol = 0.0 if target == 0 else abs(target) * tol_frac
    for m in _NUMERIC_TOKEN_RE.finditer(text):
        try:
            v = float(m.group(1))
        except ValueError:
            continue
        unit = (m.group(2) or "").lower()
        if unit in ("m", "million"):
            v *= 1_000_000
        elif unit in ("k", "thousand"):
            v *= 1_000
        if abs(v - target) <= tol:
            return True
    for m in _NUMERIC_COMMA_RE.finditer(text):
        try:
            v = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        if abs(v - target) <= tol:
            return True
    return False


_OUTLIER_SUPPORTED_RULES = {"max_over_median"}


def _check_outlier_sniff(schema: dict, run_dir: str) -> list[dict]:
    """max(metric)/median(metric) ≤ threshold for an allocation column.

    The median is the upper-median for even-length lists
    (`ratios[len // 2]`) — adequate for a robustness sniff and
    avoids floating-point rounding artifacts."""
    sec = schema.get("outlier_sniff")
    if not isinstance(sec, dict):
        return []
    csv_rel = sec.get("csv_path")
    num_col = sec.get("numerator_col")
    denom_col = sec.get("denominator_col")
    threshold = sec.get("threshold", 10.0)
    metric_name = sec.get("metric", "metric")
    rule = sec.get("rule", "max_over_median")
    if rule not in _OUTLIER_SUPPORTED_RULES:
        return [{"id": "outlier_sniff", "passed": False,
                 "claim": (f"outlier_sniff.rule {rule!r} is not "
                           f"supported. Supported rules: "
                           f"{sorted(_OUTLIER_SUPPORTED_RULES)}.")}]
    if not csv_rel or not num_col:
        return []
    csv_path = os.path.join(run_dir, csv_rel)
    if not os.path.exists(csv_path):
        return [{"id": "outlier_sniff", "passed": False,
                 "claim": f"csv_path {csv_rel!r} not found in run_dir"}]
    try:
        with open(csv_path, encoding="utf-8") as f:
            header = f.readline().strip().split(",")
            idx = {n: i for i, n in enumerate(header)}
            if num_col not in idx:
                return [{"id": "outlier_sniff", "passed": False,
                         "claim": (f"numerator_col {num_col!r} not in "
                                   f"CSV header {header}")}]
            if denom_col is not None and denom_col not in idx:
                return [{"id": "outlier_sniff", "passed": False,
                         "claim": (f"denominator_col {denom_col!r} not "
                                   f"in CSV header {header}")}]
            ratios: list[float] = []
            for line in f:
                cells = line.strip().split(",")
                if len(cells) < len(header):
                    continue
                try:
                    n = float(cells[idx[num_col]])
                except (ValueError, IndexError):
                    continue
                if denom_col is not None:
                    try:
                        d = float(cells[idx[denom_col]])
                    except (ValueError, IndexError):
                        continue
                    if d <= 0:
                        continue
                    ratios.append(n / d)
                else:
                    ratios.append(n)
    except OSError as e:
        return [{"id": "outlier_sniff", "passed": False,
                 "claim": f"could not read {csv_rel!r}: {e}"}]
    if not ratios:
        return [{"id": "outlier_sniff", "passed": False,
                 "claim": f"no usable rows in {csv_rel!r}"}]
    ratios.sort()
    median = ratios[len(ratios) // 2]
    top = ratios[-1]
    if median <= 0:
        return [{"id": "outlier_sniff", "passed": True,
                 "claim": (f"median {metric_name} = {median:.4g}; "
                           f"outlier check inconclusive")}]
    factor = top / median
    if factor > float(threshold):
        return [{"id": "outlier_sniff", "passed": False,
                 "claim": (f"{metric_name}: top/median = {factor:.1f}× > "
                           f"{threshold}× threshold (top={top:.4g}, "
                           f"median={median:.4g}). One unit is doing "
                           f"disproportionate work; the optimization "
                           f"may be fragile to dropping it.")}]
    return [{"id": "outlier_sniff", "passed": True,
             "claim": (f"{metric_name}: top/median = {factor:.1f}× "
                       f"(≤ {threshold}×)")}]


def _count_allocation_csv_rows(run_dir: str) -> int | None:
    """Phase 14 α: return the MAX row count (excluding header) across
    all `*allocation*.csv` files under run_dir, run_dir/data, and
    run_dir/models. Returns None if no allocation CSV is found.

    The MAX is the right aggregator: when a run has multiple budget
    scenarios (e.g., allocation_optimal_320M.csv, _400M.csv) the
    universe size is the same across all of them; taking max is
    robust to one file having dropped rows mid-write."""
    candidates: list[str] = []
    for sub in ("", "data", "models"):
        d = os.path.join(run_dir, sub) if sub else run_dir
        if not os.path.isdir(d):
            continue
        for entry in os.listdir(d):
            if "allocation" in entry and entry.endswith(".csv"):
                candidates.append(os.path.join(d, entry))
    if not candidates:
        return None
    best = 0
    for path in candidates:
        try:
            with open(path) as f:
                rows = sum(1 for _ in f) - 1  # exclude header
        except OSError:
            continue
        if rows > best:
            best = rows
    return best if best > 0 else None


def _check_universe_completeness(schema: dict, run_dir: str) -> list[dict]:
    """Phase 14 α: compare schema.allocation.units_total against the
    actual row count of allocation CSVs. Catches silent universe
    shrinkage: when the modeler frames the question against N units
    but data joins drop M units before optimization, the result
    silently answers a different question.

    Triggers MEDIUM `universe_completeness` (failed) when schema
    declares units_total and CSV row count is strictly less.
    Silently passes when:
      - schema.allocation.units_total is absent (opt-in field)
      - no allocation CSV exists (early-round invocation)
      - declared units_total equals or is below CSV row count
        (over-counting the declaration is benign — the CSV has more
        units than the framing claimed; an unusual but not
        consequence-bearing case)."""
    sec = schema.get("allocation")
    if not isinstance(sec, dict):
        return []
    declared = sec.get("units_total")
    if declared is None:
        return []
    try:
        declared = int(declared)
    except (TypeError, ValueError):
        return [{"id": "universe_completeness", "passed": False,
                 "claim": (f"allocation.units_total {declared!r} is not "
                           f"an integer")}]
    csv_rows = _count_allocation_csv_rows(run_dir)
    if csv_rows is None:
        return []  # no allocation CSV yet; silent
    if csv_rows >= declared:
        return [{"id": "universe_completeness", "passed": True,
                 "claim": (f"allocation universe complete: declared "
                           f"{declared}, CSV has {csv_rows} ✓")}]
    dropped = declared - csv_rows
    return [{"id": "universe_completeness", "passed": False,
             "claim": (f"Schema declares {declared} allocation units "
                       f"but allocation CSV has {csv_rows} rows. "
                       f"{dropped} unit(s) silently dropped during "
                       f"data prep — the model is answering for "
                       f"{csv_rows} units while the framing claims "
                       f"{declared}. Either fix the upstream join or "
                       f"document the dropout explicitly in §Data Gaps.")}]


_CHECKS = (
    ("mass_balance", _check_mass_balance),
    ("per_unit_intensity", _check_per_unit_intensity),
    ("share_closure", _check_share_closure),
    ("derived_consistency", _check_derived_consistency),
    ("composite_dimensions", _check_composite_dimensions),
    ("counterfactual_ratio", _check_counterfactual_ratio),
)


def validate_sanity_schema(yaml_path: str,
                            run_dir: str | None = None) -> dict:
    """Load and validate sanity_schema.yaml. Returns:

      verdict: PASS | FAIL | MALFORMED | MISSING
      checks: list of {id, passed, claim} entries
      errors: list of strings (schema-level errors only)
    """
    if not os.path.exists(yaml_path):
        return {"verdict": "MISSING", "checks": [],
                "errors": [f"{yaml_path} does not exist"]}
    try:
        with open(yaml_path) as f:
            schema = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        return {"verdict": "MALFORMED", "checks": [],
                "errors": [f"YAML parse error: {e}"]}
    if not isinstance(schema, dict):
        return {"verdict": "MALFORMED", "checks": [],
                "errors": ["sanity_schema.yaml top-level must be a mapping"]}

    if run_dir is None:
        run_dir = os.path.dirname(os.path.dirname(os.path.abspath(yaml_path)))

    # Catch only the user-input-driven exception classes that
    # arise from malformed schema entries (TypeError on non-numeric
    # fields, KeyError on missing keys we didn't pre-validate,
    # IndexError on bad list shapes, ValueError on number parsing,
    # OSError on file reads). Anything else is a real bug — let it
    # surface during development.
    _USER_INPUT_EXC = (TypeError, KeyError, IndexError, ValueError,
                        OSError, AttributeError)
    checks: list[dict] = []
    for _name, fn in _CHECKS:
        try:
            checks.extend(fn(schema))
        except _USER_INPUT_EXC as e:
            checks.append({"id": _name, "passed": False,
                           "claim": f"{_name} crashed: {e}"})
    try:
        checks.extend(_check_heterogeneity_carryforward(schema, run_dir))
    except _USER_INPUT_EXC as e:
        checks.append({"id": "heterogeneity_carryforward", "passed": False,
                       "claim": f"heterogeneity check crashed: {e}"})
    try:
        checks.extend(_check_outlier_sniff(schema, run_dir))
    except _USER_INPUT_EXC as e:
        checks.append({"id": "outlier_sniff", "passed": False,
                       "claim": f"outlier_sniff crashed: {e}"})
    try:
        checks.extend(_check_universe_completeness(schema, run_dir))
    except _USER_INPUT_EXC as e:
        checks.append({"id": "universe_completeness", "passed": False,
                       "claim": f"universe_completeness crashed: {e}"})

    failed = [c for c in checks if not c.get("passed", False)]
    verdict = "PASS" if not failed else "FAIL"
    return {"verdict": verdict, "checks": checks, "errors": []}


def _run_self_test() -> int:
    import tempfile

    failures: list[str] = []

    def ok(cond: bool, label: str) -> None:
        if not cond:
            failures.append(label)

    def write_schema(d: str, body: str) -> str:
        path = os.path.join(d, "sanity_schema.yaml")
        with open(path, "w") as f:
            f.write(body)
        return path

    # W1: mass_balance fail (averting >95%)
    with tempfile.TemporaryDirectory() as d:
        p = write_schema(d,
            "outcome:\n"
            "  averted_point: 99\n"
            "  baseline_total: 100\n")
        r = validate_sanity_schema(p, run_dir=d)
        ok(r["verdict"] == "FAIL"
           and any(c["id"] == "mass_balance" and not c["passed"]
                   for c in r["checks"]),
           f"W1: mass_balance should fail, got {r}")

    # W2: per_unit_intensity fail (averted > cap)
    with tempfile.TemporaryDirectory() as d:
        p = write_schema(d,
            "outcome:\n"
            "  averted_point: 100\n"
            "  baseline_total: 1000\n"
            "exposure:\n"
            "  total_in_allocated: 10\n"
            "  rate_per_unit_per_year: 1.0\n"
            "  years: 1\n")
        r = validate_sanity_schema(p, run_dir=d)
        ok(any(c["id"] == "per_unit_intensity" and not c["passed"]
               for c in r["checks"]),
           f"W2: per_unit_intensity should fail, got {r}")

    # W3: share_closure fail
    with tempfile.TemporaryDirectory() as d:
        p = write_schema(d,
            "shares:\n"
            "  - name: zone\n"
            "    sums_to: 1.0\n"
            "    tol: 0.01\n"
            "    values: {NW: 0.5, NC: 0.3}\n")  # sums to 0.8
        r = validate_sanity_schema(p, run_dir=d)
        ok(any(c["id"] == "share_closure_zone" and not c["passed"]
               for c in r["checks"]),
           f"W3: share_closure should fail, got {r}")

    # W4: derived_consistency fail
    with tempfile.TemporaryDirectory() as d:
        p = write_schema(d,
            "derived_consistency:\n"
            "  - primary: cases\n"
            "    primary_value: 100\n"
            "    derived: deaths\n"
            "    derived_actual: 50\n"   # formula predicts 0.4
            "    formula: \"primary * cfr\"\n"
            "    constants: {cfr: 0.004}\n"
            "    tol: 0.10\n")
        r = validate_sanity_schema(p, run_dir=d)
        ok(any(c["id"].startswith("derived_consistency_cases")
               and not c["passed"] for c in r["checks"]),
           f"W4: derived_consistency should fail, got {r}")

    # W4b: derived_consistency PASS (correct math)
    with tempfile.TemporaryDirectory() as d:
        p = write_schema(d,
            "derived_consistency:\n"
            "  - primary: cases\n"
            "    primary_value: 100000\n"
            "    derived: deaths\n"
            "    derived_actual: 400\n"
            "    formula: \"primary * cfr\"\n"
            "    constants: {cfr: 0.004}\n"
            "    tol: 0.05\n")
        r = validate_sanity_schema(p, run_dir=d)
        ok(r["verdict"] == "PASS",
           f"W4b: derived_consistency clean should PASS, got {r}")

    # W5: composite_dimensions fail (DALY/death = 200, window [15,60])
    with tempfile.TemporaryDirectory() as d:
        p = write_schema(d,
            "composite_dimensions:\n"
            "  - name: DALY_per_death\n"
            "    value: 200\n"
            "    window: [15, 60]\n")
        r = validate_sanity_schema(p, run_dir=d)
        ok(any(c["id"] == "composite_dim_DALY_per_death"
               and not c["passed"] for c in r["checks"]),
           f"W5: composite_dim should fail (200 outside [15,60]), got {r}")

    # W6: counterfactual_ratio fail (200× exceeds 100× cap)
    with tempfile.TemporaryDirectory() as d:
        p = write_schema(d,
            "outcome:\n"
            "  averted_point: 8_390_000\n"
            "  baseline_total: 16_600_000\n"
            "counterfactual:\n"
            "  name: null_model\n"
            "  averted: 41_000\n"  # 8.39M / 41k = 204×
            "  optimized_vs_null_acceptable: [1.5, 100]\n")
        r = validate_sanity_schema(p, run_dir=d)
        ok(any(c["id"] == "counterfactual_ratio" and not c["passed"]
               for c in r["checks"]),
           f"W6: counterfactual ratio 200× should fail, got {r}")

    # W7: heterogeneity_carryforward fail (lower bound not in target file)
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "report.md"), "w") as f:
            f.write("## Executive Summary\n8.57M DALYs averted.\n")
        p = write_schema(d,
            "structural_uncertainty:\n"
            "  - source: within_zone_heterogeneity.yaml\n"
            "    verdict: INCONCLUSIVE\n"
            "    lower_bound: 7_500_000\n"
            "    must_appear_in: report.md\n")
        r = validate_sanity_schema(p, run_dir=d)
        ok(any("heterogeneity_carryforward" in c["id"]
               and not c["passed"] for c in r["checks"]),
           f"W7: lower bound missing from report.md should fail, got {r}")

    # W7b: heterogeneity_carryforward PASS (lower bound present)
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "report.md"), "w") as f:
            f.write("## Executive Summary\n"
                    "8.57M DALYs averted (7.50M under zone-mean "
                    "substitution).\n")
        p = write_schema(d,
            "structural_uncertainty:\n"
            "  - source: within_zone_heterogeneity.yaml\n"
            "    verdict: INCONCLUSIVE\n"
            "    lower_bound: 7_500_000\n"
            "    must_appear_in: report.md\n")
        r = validate_sanity_schema(p, run_dir=d)
        ok(all(c["passed"] for c in r["checks"]
               if "heterogeneity_carryforward" in c["id"]),
           f"W7b: 7.50M present should PASS, got {r}")

    # W8: outlier_sniff fail (top/median > threshold)
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "models", "alloc.csv"), "w") as f:
            f.write("name,value\n")
            for v in [1.0, 1.0, 1.0, 1.0, 1.0, 50.0]:  # top 50 / median 1
                f.write(f"x,{v}\n")
        p = write_schema(d,
            "outlier_sniff:\n"
            "  metric: per_dollar\n"
            "  rule: max_over_median\n"
            "  threshold: 10.0\n"
            "  csv_path: models/alloc.csv\n"
            "  numerator_col: value\n")
        r = validate_sanity_schema(p, run_dir=d)
        ok(any(c["id"] == "outlier_sniff" and not c["passed"]
               for c in r["checks"]),
           f"W8: 50× outlier should fail, got {r}")

    # W-malformed: invalid YAML
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "sanity_schema.yaml")
        with open(p, "w") as f:
            f.write("not: valid: yaml: at all: nope\n")
        r = validate_sanity_schema(p, run_dir=d)
        ok(r["verdict"] == "MALFORMED",
           f"W-malformed: bad YAML should be MALFORMED, got {r}")

    # W-formula-disallowed: formula tries to call a function
    with tempfile.TemporaryDirectory() as d:
        p = write_schema(d,
            "derived_consistency:\n"
            "  - primary: cases\n"
            "    primary_value: 100\n"
            "    derived: deaths\n"
            "    derived_actual: 0.4\n"
            "    formula: \"abs(primary)\"\n"
            "    tol: 0.10\n")
        r = validate_sanity_schema(p, run_dir=d)
        ok(any("derived_consistency" in c["id"] and not c["passed"]
               and "disallowed" in c["claim"]
               for c in r["checks"]),
           f"W-formula-disallowed: function call should be rejected, "
           f"got {r}")

    # W-formula-power: ** operator is allowed and computes correctly.
    with tempfile.TemporaryDirectory() as d:
        p = write_schema(d,
            "derived_consistency:\n"
            "  - primary: x\n"
            "    primary_value: 10\n"
            "    derived: y\n"
            "    derived_actual: 100\n"
            "    formula: \"primary ** 2\"\n"
            "    tol: 0.05\n")
        r = validate_sanity_schema(p, run_dir=d)
        ok(r["verdict"] == "PASS",
           f"W-formula-power: 10**2 == 100 should PASS, got {r}")

    # W-outlier-bad-rule: unsupported rule should fail with a
    # clear message rather than silently using max_over_median.
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "models", "alloc.csv"), "w") as f:
            f.write("name,value\n1,1.0\n2,2.0\n")
        p = write_schema(d,
            "outlier_sniff:\n"
            "  rule: max_over_p95\n"
            "  threshold: 10.0\n"
            "  csv_path: models/alloc.csv\n"
            "  numerator_col: value\n")
        r = validate_sanity_schema(p, run_dir=d)
        ok(any(c["id"] == "outlier_sniff" and not c["passed"]
               and "max_over_p95" in c["claim"]
               for c in r["checks"]),
           f"W-outlier-bad-rule: unsupported rule should fail, got {r}")

    # W-universe-shrink: schema declares 774 LGAs, allocation CSV has
    # 769 → universe_completeness fires (the 103105 retro pattern).
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "models",
                               "allocation_optimal_320M.csv"), "w") as f:
            f.write("lga_name,total_cost_usd\n")
            for i in range(769):
                f.write(f"lga_{i},1000\n")
        p = write_schema(d,
            "allocation:\n"
            "  units_total: 774\n"
            "  units_allocated: 312\n"
            "  budget: 320000000\n")
        r = validate_sanity_schema(p, run_dir=d)
        ok(any(c["id"] == "universe_completeness" and not c["passed"]
               and "774" in c["claim"] and "769" in c["claim"]
               and "5" in c["claim"]
               for c in r["checks"]),
           f"W-universe-shrink: 774 declared vs 769 CSV should fail, "
           f"got {r}")

    # W-universe-clean: declared = CSV row count → passes silently.
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "models",
                               "allocation_optimal_320M.csv"), "w") as f:
            f.write("lga_name,total_cost_usd\n")
            for i in range(774):
                f.write(f"lga_{i},1000\n")
        p = write_schema(d,
            "allocation:\n"
            "  units_total: 774\n"
            "  budget: 320000000\n")
        r = validate_sanity_schema(p, run_dir=d)
        ok(all(c["passed"] for c in r["checks"]
               if c["id"] == "universe_completeness"),
           f"W-universe-clean: 774 == 774 should PASS, got {r}")

    # W-universe-no-csv: declared but no CSV in run_dir → silent.
    with tempfile.TemporaryDirectory() as d:
        p = write_schema(d,
            "allocation:\n"
            "  units_total: 774\n")
        r = validate_sanity_schema(p, run_dir=d)
        ok(not any(c["id"] == "universe_completeness"
                   for c in r["checks"]),
           f"W-universe-no-csv: missing CSV should be silent, got {r}")

    # W-universe-over: declared 100 but CSV has 200 → silent
    # (over-counting framing is benign — uncommon edge case).
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "models",
                               "allocation_optimal.csv"), "w") as f:
            f.write("lga_name,total_cost_usd\n")
            for i in range(200):
                f.write(f"lga_{i},1000\n")
        p = write_schema(d,
            "allocation:\n"
            "  units_total: 100\n")
        r = validate_sanity_schema(p, run_dir=d)
        ok(all(c["passed"] for c in r["checks"]
               if c["id"] == "universe_completeness"),
           f"W-universe-over: declared < CSV should PASS, got {r}")

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
        p.error("run_dir required (or use --self-test)")

    yaml_path = os.path.join(args.run_dir, "models", "sanity_schema.yaml")
    result = validate_sanity_schema(yaml_path, run_dir=args.run_dir)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"verdict: {result['verdict']}", file=sys.stderr)
        for c in result.get("checks", []):
            tag = "PASS" if c.get("passed") else "FAIL"
            print(f"  [{tag}] {c['id']}: {c['claim']}", file=sys.stderr)
        for e in result.get("errors", []):
            print(f"  ERROR: {e}", file=sys.stderr)

    return 0 if result["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
