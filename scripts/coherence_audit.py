"""Phase 17 β — Coherence audit.

Standalone post-WRITE validator that catches the residual drift class
that survived to ACCEPT in the 2026-04-29_161312 run:

  Duty 1 (label coherence): verdict labels in prose contradict the
    canonical YAML source (R-019: decision_rule.md says "UNSTABLE"
    while sensitivity_analysis.yaml says "SENSITIVE").

  Duty 2 (cross-file counts/costs): prose numbers don't reconcile
    against the allocation CSV (R-020: choropleth legend "698 PBO"
    vs CSV's 691 PBO-only; "$197M for 691 LGAs" conflates PBO
    component across all 773 LGAs with the cost of the 691 PBO-only).

  Duty 3 (self-contradicting artifacts): prose `notes:` in a single
    YAML contradict structured fields in the same file (M-010:
    identifiability.yaml notes claim seasonal_amplitude "converged
    to 0.1" while `point_estimate: 0.8702`).

The auditor runs after WRITE, alongside `scripts/writer_qa.py`. It
emits `coherence_audit.yaml` at run_dir root. The validator
(`_check_coherence_audit` in `validate_critique_yaml.py`) folds
HIGH violations into the STAGE 7 decision via the existing
`_incorporate_rigor_violations` pattern.

Usage:
    python3 scripts/coherence_audit.py {run_dir}        # write yaml
    python3 scripts/coherence_audit.py --self-test
"""
from __future__ import annotations

import argparse
import csv
import datetime as _dt
import json
import os
import re
import sys
from typing import Any

import yaml

# Reuse the prose-scan primitives from numeric_consistency.py rather
# than re-implementing. Phase 17 β follows Phase 16 α's principle of
# extracting shared infrastructure rather than duplicating.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)
import numeric_consistency as _nc  # type: ignore  # noqa: E402


# ---------------------------------------------------------------------------
# Duty 1 — Label coherence
# ---------------------------------------------------------------------------

# Canonical (artifact_path, field_path, allowed_values, prose_files).
# field_path uses dot notation for nested keys.
# prose_files are scanned for any allowed_value that is NOT the canonical
# one — those are the drift cases.
_LABEL_CANONICAL_SOURCES = [
    {
        "id": "sensitivity_verdict",
        "artifact": "models/sensitivity_analysis.yaml",
        "field": "verdict",
        "allowed_values": ["ROBUST", "SENSITIVE", "UNSTABLE"],
        "prose_files": ["report.md", "decision_rule.md", "results.md"],
        # Aliases that should also count as a mention of a label
        # (e.g., "allocation is unstable" → UNSTABLE). Matched
        # case-insensitively so title-case ("Robust", "Sensitive",
        # "Unstable") at sentence starts and after punctuation is
        # caught alongside ALL-CAPS verdict labels and lowercase prose.
        "alias_patterns": {
            "ROBUST": [r"\brobust\b"],
            "SENSITIVE": [r"\bsensitive\b"],
            "UNSTABLE": [r"\bunstable\b"],
        },
        # If the prose word appears next to one of these qualifier words,
        # it's a generic English usage, not a verdict claim — skip.
        "generic_use_qualifiers": [
            # "robust" used as adjective for things other than the verdict
            "robust to", "robust against", "robust standard",
            # "sensitive" as adjective
            "sensitive to", "sensitive analysis", "sensitive parameters",
            "sensitivity analysis",  # the section heading, not a verdict
        ],
    },
]


def _read_yaml_field(path: str, field: str) -> str | None:
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            doc = yaml.safe_load(f) or {}
    except (yaml.YAMLError, OSError):
        return None
    cur: Any = doc
    for part in field.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return str(cur) if cur is not None else None


def _audit_label_coherence(run_dir: str) -> list[dict]:
    """For each canonical (yaml, field) pair, scan prose files for
    mentions of OTHER allowed values. Each such mention is a drift.
    """
    violations: list[dict] = []
    for src in _LABEL_CANONICAL_SOURCES:
        canonical_path = os.path.join(run_dir, src["artifact"])
        canonical = _read_yaml_field(canonical_path, src["field"])
        if canonical is None:
            continue  # artifact missing or unreadable; not our duty
        canonical_upper = canonical.upper()
        if canonical_upper not in [v.upper() for v in src["allowed_values"]]:
            continue
        for prose_file in src["prose_files"]:
            ppath = os.path.join(run_dir, prose_file)
            if not os.path.exists(ppath):
                continue
            try:
                with open(ppath) as f:
                    text = f.read()
            except OSError:
                continue
            for label in src["allowed_values"]:
                if label.upper() == canonical_upper:
                    continue  # the canonical label is OK to appear
                for pat in src["alias_patterns"].get(label, []):
                    for m in re.finditer(pat, text, re.IGNORECASE):
                        # Inspect 30-char window around the match for
                        # generic-use qualifiers — skip if found.
                        win_start = max(0, m.start() - 25)
                        win_end = min(len(text), m.end() + 25)
                        window = text[win_start:win_end].lower()
                        if any(q in window for q in src["generic_use_qualifiers"]):
                            continue
                        # Compute approximate line number for the location
                        # field. Cheap: count newlines up to the match.
                        line_no = text.count("\n", 0, m.start()) + 1
                        ctx_start = max(0, m.start() - 60)
                        ctx_end = min(len(text), m.end() + 60)
                        ctx = text[ctx_start:ctx_end].replace("\n", " ").strip()
                        violations.append({
                            "id": f"CA-L-{len(violations) + 1:03d}",
                            "severity": "HIGH",
                            "duty": "label_coherence",
                            "location": f"{prose_file}:{line_no}",
                            "claim": (
                                f"Prose contains label {label!r} but the "
                                f"canonical source {src['artifact']}::"
                                f"{src['field']} is {canonical_upper!r}. "
                                f"Context: …{ctx}…"
                            ),
                            "canonical_source": (
                                f"{src['artifact']}:{src['field']}"
                            ),
                            "canonical_value": canonical_upper,
                            "drifted_value": label.upper(),
                        })
    return violations


# ---------------------------------------------------------------------------
# Duty 2 — Cross-file counts/costs
# ---------------------------------------------------------------------------

# Tolerance for numeric drift before we flag. The 161312 R-020 case is a
# 7-LGA gap on 691 (1.0%); the $197M vs $177M conflation is 11%. We pick
# 5% as the default threshold — large enough to ignore rounding, small
# enough to catch conflation.
_COUNT_DRIFT_THRESHOLD = 0.05


def _read_allocation_csv(run_dir: str) -> list[dict] | None:
    """Find and parse the allocation CSV.

    Searches the same locations as
    `validate_critique_yaml.py::_find_allocation_csvs`: run_dir root,
    then `data/` and `models/` subdirectories. Real runs place
    allocation CSVs in any of these (root for 161312 / 115318;
    `models/` for several earlier runs in 224202-class trees).
    """
    candidates = ("allocation_optimized.csv", "allocation.csv")
    search_dirs = (run_dir,
                   os.path.join(run_dir, "data"),
                   os.path.join(run_dir, "models"))
    for d in search_dirs:
        for name in candidates:
            path = os.path.join(d, name)
            if os.path.exists(path):
                try:
                    with open(path) as f:
                        return list(csv.DictReader(f))
                except OSError:
                    return None
    return None


# Threshold/hypothetical markers that disqualify a "$X for package P"
# match — the prose is discussing scenarios, not the actual allocation.
_HYPOTHETICAL_MARKERS = (
    "exceeds", "if budget", "if the budget", "above $", "below $",
    "threshold", "would require", "would need", "scenario b",
    "alternative scenario", "hypothetical", "only if", "only when",
    "rises above", "drops below",
)

# "$X budget" sub-patterns where X is the dollar amount itself —
# the prose is naming the budget pool, not a package cost.
_BUDGET_POOL_PATTERNS = (
    re.compile(r"\$\s*[\d.,]+\s*M?\s+(?:gc7|annual|total|overall)\s+budget",
               re.IGNORECASE),
    re.compile(r"the\s+\$\s*[\d.,]+\s*M?\s+(?:gc7|annual|total|overall)?\s*budget",
               re.IGNORECASE),
)


def _scan_dollar_amount_with_package(doc_text: str) -> list[dict]:
    """Find prose like 'PBO LLIN 80% costs ~$197M' tightly enough
    to catch package-cost claims, and discard threshold/hypothetical
    framings (e.g., 'SMC becomes cost-effective if budget exceeds
    $450M' is NOT a claim that SMC costs $450M).
    """
    out: list[dict] = []
    pkg_tokens = ["pbo llin", "pbo+irs", "dual-ai", "dual ai",
                  "standard llin", "smc", "irs", "pbo"]
    seen: set[tuple[int, int]] = set()
    for pkg_tok in pkg_tokens:
        # Tight span (60 chars) between package keyword and the dollar
        # amount. Forbid intervening sentence boundaries ('.' or '|'
        # which mark table cells we want to read separately) and the
        # word "if" (which strongly signals hypothetical framing).
        pat = re.compile(
            rf"\b({re.escape(pkg_tok)})\b[^.|\n]{{0,60}}?"
            r"\$\s*([\d.,]+)\s*(M|million|K|k|thousand)?",
            re.IGNORECASE,
        )
        for m in pat.finditer(doc_text):
            key = (m.start(), m.end())
            if key in seen:
                continue
            seen.add(key)
            try:
                amt = float(m.group(2).replace(",", ""))
            except ValueError:
                continue
            unit = (m.group(3) or "").lower()
            if unit in ("m", "million"):
                amt *= 1_000_000
            elif unit in ("k", "thousand"):
                amt *= 1_000
            elif amt < 1_000_000:
                # No unit suffix AND amount < $1M: almost always a
                # per-unit ratio ($/DALY, $/case, $/person) or per-
                # person cost ($1.20/person/yr), not a portfolio claim
                # worth checking against the CSV. Phase 17 ε: skip
                # these to eliminate the 115318-retro false-positive
                # class. Bare amounts ≥ $1M (e.g., "$320,000,000")
                # are kept — they are rare but legitimate portfolio
                # claims when prose writes the integer instead of
                # using an M suffix.
                continue
            # Wider context window for filtering
            ctx_start = max(0, m.start() - 80)
            ctx_end = min(len(doc_text), m.end() + 80)
            ctx = doc_text[ctx_start:ctx_end].replace("\n", " ")
            cl = ctx.lower()
            # Skip hypothetical/threshold framings
            if any(mk in cl for mk in _HYPOTHETICAL_MARKERS):
                continue
            # Skip if the matched dollar amount IS the budget pool
            # (e.g., "$320M GC7 budget" — naming the budget, not a
            # package cost). Check the 40 chars after the dollar
            # match to see if the dollar phrase IS the budget.
            after_dollar = doc_text[m.end():
                                     min(len(doc_text), m.end() + 40)]
            full_dollar_phrase = m.group(0) + after_dollar
            if any(p.search(full_dollar_phrase) for p in _BUDGET_POOL_PATTERNS):
                continue
            # Skip per-unit cost ratios (e.g., "$1,500/DALY",
            # "$30/case", "$8/person/year") — these are unit prices,
            # not portfolio costs. Phase 17 ε regression fix: the
            # 115318 retro had 6 false positives where $/DALY ratios
            # in narrative were classified as package costs.
            ratio_suffix = doc_text[m.end():
                                    min(len(doc_text), m.end() + 25)].lower()
            if any(s in ratio_suffix for s in
                   ("/daly", "/case", "/person", " per daly",
                    " per case", " per person")):
                continue
            # Skip markdown table rows — table cells often pair
            # benchmark-vs-our-value comparisons that aren't package-cost
            # claims (e.g., "IRS outcompetes SMC at $320M" — naming the
            # comparison budget, not claiming IRS cost = $320M).
            if _nc._is_table_row(doc_text, m.start()):
                continue
            out.append({
                "package_token": _nc._normalize_package_token(pkg_tok),
                "dollar_usd": amt,
                "context": ctx,
                "match_start": m.start(),
            })
    return out


def _audit_cross_file_counts(run_dir: str) -> list[dict]:
    """Reconcile prose numerics against allocation_optimized.csv."""
    violations: list[dict] = []
    rows = _read_allocation_csv(run_dir)
    if rows is None or not rows:
        return violations

    # Compute canonical aggregates from the CSV. Cost column varies
    # across run schemas: older runs (161312) use `annual_cost`; newer
    # runs (115318+) use `cost_usd`. Try both.
    cost_columns = ("annual_cost", "cost_usd")
    pkg_counts: dict[str, int] = {}
    pkg_costs: dict[str, float] = {}
    for r in rows:
        pkg = r.get("package", "").strip()
        if not pkg:
            continue
        pkg_counts[pkg] = pkg_counts.get(pkg, 0) + 1
        cost = 0.0
        for col in cost_columns:
            v = r.get(col)
            if v not in (None, ""):
                try:
                    cost = float(v)
                    break
                except (TypeError, ValueError):
                    continue
        pkg_costs[pkg] = pkg_costs.get(pkg, 0.0) + cost

    # Read the report (the writer's output is the primary target)
    for prose_file in ("report.md", "decision_rule.md", "results.md"):
        ppath = os.path.join(run_dir, prose_file)
        if not os.path.exists(ppath):
            continue
        try:
            with open(ppath) as f:
                text = f.read()
        except OSError:
            continue

        # Sub-duty 2a: package counts
        # _scan_package_counts returns (count, package_norm, context).
        for n, pkg_norm, ctx in _nc._scan_package_counts(text):
            if not pkg_norm:
                continue
            # Map base tokens (e.g., 'pbo' / 'irs') to a CSV package
            # key. 'pbo' likely refers to 'pbo_llin_80'; 'smc' to
            # 'smc' or 'pbo_llin_80_smc' depending on context.
            csv_pkg_key = _resolve_csv_package(pkg_norm, ctx, pkg_counts)
            canonical = pkg_counts.get(csv_pkg_key) if csv_pkg_key else None
            if canonical is None:
                continue
            if abs(n - canonical) / max(canonical, 1) > _COUNT_DRIFT_THRESHOLD:
                line_no = text.count("\n", 0, text.find(ctx.strip()[:20])) + 1 \
                    if ctx.strip()[:20] in text else 1
                violations.append({
                    "id": f"CA-C-{len(violations) + 1:03d}",
                    "severity": "HIGH",
                    "duty": "cross_file_counts",
                    "location": f"{prose_file}:~{line_no}",
                    "claim": (
                        f"Prose says {n} LGAs for package {pkg_norm!r}, "
                        f"but allocation CSV has {canonical}. "
                        f"Drift {abs(n - canonical)/canonical*100:.1f}%. "
                        f"Context: …{ctx.strip()[:120]}…"
                    ),
                    "canonical_source": "allocation CSV",
                    "canonical_value": canonical,
                    "drifted_value": n,
                })

        # Sub-duty 2b: package costs (the $197M conflation class)
        for hit in _scan_dollar_amount_with_package(text):
            pkg_norm = hit["package_token"]
            amt = hit["dollar_usd"]
            # Look up canonical cost for this package. Two possible
            # interpretations: (a) cost OF this package's LGAs,
            # (b) cost contribution OF this package across all LGAs
            # (only meaningful if packages compose, e.g., PBO is in
            # both pbo_llin_80 and pbo_llin_80_irs). The safer check:
            # flag only when the prose's $-amount differs from BOTH
            # interpretations by > threshold.
            if pkg_norm in pkg_costs:
                canonical_a = pkg_costs[pkg_norm]
            else:
                # Try fuzzy match
                canonical_a = None
                for csv_pkg in pkg_costs:
                    if pkg_norm in csv_pkg:
                        canonical_a = pkg_costs[csv_pkg]
                        break
            if canonical_a is None:
                continue
            # Interpretation (b): cost of any package whose name
            # CONTAINS this token (e.g., 'pbo' is in both pbo_llin_80
            # and pbo_llin_80_irs). Sum those.
            canonical_b = sum(
                c for k, c in pkg_costs.items() if pkg_norm in k
            )
            drift_a = abs(amt - canonical_a) / max(canonical_a, 1)
            drift_b = abs(amt - canonical_b) / max(canonical_b, 1)
            # Flag when neither interpretation matches AND the prose
            # is NOT plausibly close to either.
            if drift_a > _COUNT_DRIFT_THRESHOLD and \
                    drift_b > _COUNT_DRIFT_THRESHOLD:
                line_no = text.count("\n", 0, hit["match_start"]) + 1
                violations.append({
                    "id": f"CA-C-{len(violations) + 1:03d}",
                    "severity": "MEDIUM",  # Cost conflation is often
                    # MEDIUM-grade because it can be defensible (e.g.,
                    # rounding); HIGH only if drift > 25%.
                    "duty": "cross_file_counts",
                    "location": f"{prose_file}:~{line_no}",
                    "claim": (
                        f"Prose says ${amt/1e6:.1f}M for package "
                        f"{pkg_norm!r}, but CSV-computed cost is "
                        f"${canonical_a/1e6:.1f}M (LGAs assigned this "
                        f"exact package) or ${canonical_b/1e6:.1f}M "
                        f"(any package containing {pkg_norm!r}). "
                        f"Min drift {min(drift_a, drift_b)*100:.1f}%. "
                        f"Context: …{hit['context'].strip()[:120]}…"
                    ),
                    "canonical_source": "allocation CSV",
                    "canonical_value": (
                        f"${canonical_a/1e6:.1f}M (exact) | "
                        f"${canonical_b/1e6:.1f}M (any-containing)"
                    ),
                    "drifted_value": f"${amt/1e6:.1f}M",
                })
                # Escalate to HIGH if drift is > 25% on both
                if drift_a > 0.25 and drift_b > 0.25:
                    violations[-1]["severity"] = "HIGH"

    # Track totals for the summary
    return violations


def _resolve_csv_package(
    base_token: str, ctx: str, pkg_counts: dict[str, int]
) -> str | None:
    """Map a base package token (e.g., 'pbo', 'irs') from the prose
    scan to the canonical CSV package key.

    Priority:
      1. Exact match (e.g., 'smc' → 'smc' if present in CSV).
      2. Context-augmented match (e.g., 'pbo' + 'IRS' in context →
         'pbo_llin_80_irs'; 'pbo' alone → 'pbo_llin_80').
      3. First fuzzy substring match.
    """
    if not pkg_counts:
        return None
    # 1. Exact
    if base_token in pkg_counts:
        return base_token
    # 2. Context-augmented for known compositions
    cl = ctx.lower()
    bt = base_token.lower()
    if bt in ("pbo", "llin_pbo"):
        if "irs" in cl and "pbo_llin_80_irs" in pkg_counts:
            return "pbo_llin_80_irs"
        if "smc" in cl and "pbo_llin_80_smc" in pkg_counts:
            return "pbo_llin_80_smc"
        if "pbo_llin_80" in pkg_counts:
            return "pbo_llin_80"
    # 3. Fuzzy substring
    for csv_pkg in pkg_counts:
        if bt in csv_pkg or csv_pkg in bt:
            return csv_pkg
    return None


def _extract_package_from_context(ctx: str) -> str | None:
    """Best-effort: extract a normalized package token from a
    context window. Used to attach package_count scan results to
    a CSV package."""
    cl = ctx.lower()
    candidates = [
        ("pbo llin 80% + irs", "pbo_llin_80_irs"),
        ("pbo llin 80% + smc", "pbo_llin_80_smc"),
        ("pbo+irs", "pbo_llin_80_irs"),
        ("pbo llin 80%", "pbo_llin_80"),
        ("pbo llin", "pbo_llin_80"),
        ("dual-ai", "llin_dual_ai"),
        ("dual ai", "llin_dual_ai"),
        ("standard llin", "llin_standard"),
        ("smc", "smc"),
        ("irs", "irs"),
    ]
    for needle, norm in candidates:
        if needle in cl:
            return norm
    return None


# ---------------------------------------------------------------------------
# Duty 3 — Self-contradicting artifacts
# ---------------------------------------------------------------------------

# Files where notes prose may contradict structured fields with similar
# names. (artifact_path, parameters_field, notes_field, point_field).
_SELF_CONTRADICTION_TARGETS = [
    {
        "artifact": "models/identifiability.yaml",
        "parameters_field": "parameters",
        "notes_field": "notes",
        "point_field": "point_estimate",
        "lower_field": "lower_bound",
        "upper_field": "upper_bound",
    },
    {
        "artifact": "models/sensitivity_analysis.yaml",
        "parameters_field": "load_bearing_parameters",
        "notes_field": "notes",
        "point_field": "primary_value",
    },
]


_NUM_RE = re.compile(r"-?\d+\.?\d*")


def _audit_self_contradicting(run_dir: str) -> list[dict]:
    """For each target artifact, look for parameter-name + number
    co-occurrences in `notes:` prose that contradict the same
    parameter's structured field."""
    violations: list[dict] = []
    for tgt in _SELF_CONTRADICTION_TARGETS:
        path = os.path.join(run_dir, tgt["artifact"])
        if not os.path.exists(path):
            continue
        try:
            with open(path) as f:
                doc = yaml.safe_load(f) or {}
        except (yaml.YAMLError, OSError):
            continue
        if not isinstance(doc, dict):
            continue
        params = doc.get(tgt["parameters_field"]) or []
        notes = doc.get(tgt["notes_field"]) or ""
        if not isinstance(params, list) or not isinstance(notes, str):
            continue
        notes_lower = notes.lower()
        for p in params:
            if not isinstance(p, dict):
                continue
            name = p.get("name") or p.get("parameter")
            if not isinstance(name, str):
                continue
            point = p.get(tgt["point_field"])
            if point is None:
                continue
            try:
                point_val = float(point)
            except (TypeError, ValueError):
                continue
            # Find name in notes; for each occurrence, scan a 80-char
            # window for numbers and check whether any number is
            # inconsistent with point_val.
            for m in re.finditer(re.escape(name.lower()), notes_lower):
                win_start = m.end()
                win_end = min(len(notes), win_start + 200)
                window = notes[win_start:win_end]
                # Look for "X.XX" or "to X.XX" within the window
                nums = _NUM_RE.findall(window)
                for num_str in nums[:3]:  # only check first few
                    try:
                        num_val = float(num_str)
                    except ValueError:
                        continue
                    # Skip integers used as line numbers / counts that
                    # aren't parameter values (e.g., "555% relative SE")
                    if num_val > 100 or num_val == 0:
                        continue
                    # Check: does the window context suggest this number
                    # is a claim ABOUT the parameter's value?
                    pre = notes[max(0, win_start - 80):win_start].lower()
                    full_window = (pre + " " + window[:120]).lower()
                    is_value_claim = any(
                        kw in full_window for kw in (
                            "converged to", "fixed at", "set to",
                            "value is", "estimated at", "lower bound",
                            "at the lower bound", "boundary at",
                        )
                    )
                    if not is_value_claim:
                        continue
                    # Compare against point_val. A drift > 20% is
                    # suspicious; > 5x is a clear self-contradiction.
                    if point_val == 0:
                        relative = float("inf") if num_val != 0 else 0
                    else:
                        relative = abs(num_val - point_val) / abs(point_val)
                    if relative > 0.50:
                        violations.append({
                            "id": f"CA-S-{len(violations) + 1:03d}",
                            "severity": (
                                "HIGH" if relative > 5 else "MEDIUM"
                            ),
                            "duty": "self_contradicting",
                            "location": f"{tgt['artifact']}::{tgt['notes_field']}",
                            "claim": (
                                f"Notes claim {name!r} = {num_val} "
                                f"but `{tgt['point_field']}: {point_val}`. "
                                f"Drift {relative*100:.0f}%. Notes "
                                f"likely stale after re-fit."
                            ),
                            "canonical_source": (
                                f"{tgt['artifact']}::"
                                f"{tgt['parameters_field']}.{name}."
                                f"{tgt['point_field']}"
                            ),
                            "canonical_value": point_val,
                            "drifted_value": num_val,
                        })
                        break  # one violation per parameter per file
    return violations


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def audit_run(run_dir: str) -> dict:
    """Run all three duties and return a structured result dict."""
    label_v = _audit_label_coherence(run_dir)
    counts_v = _audit_cross_file_counts(run_dir)
    self_v = _audit_self_contradicting(run_dir)
    all_v = label_v + counts_v + self_v
    has_high = any(v["severity"] == "HIGH" for v in all_v)
    return {
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "duties": {
            "label_coherence": {"violations": label_v, "n": len(label_v)},
            "cross_file_counts": {"violations": counts_v, "n": len(counts_v)},
            "self_contradicting": {"violations": self_v, "n": len(self_v)},
        },
        "violations": all_v,
        "verdict": "DRIFT_DETECTED" if all_v else "CLEAN",
        "has_high": has_high,
    }


def write_audit_yaml(run_dir: str, result: dict | None = None) -> str:
    """Write coherence_audit.yaml to run_dir; return the path."""
    if result is None:
        result = audit_run(run_dir)
    path = os.path.join(run_dir, "coherence_audit.yaml")
    with open(path, "w") as f:
        yaml.safe_dump(result, f, sort_keys=False, default_flow_style=False)
    return path


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------


def _self_test() -> int:
    import tempfile

    failures: list[str] = []

    def ok(cond: bool, msg: str) -> None:
        if not cond:
            failures.append(msg)

    # T1: label coherence — synthetic R-019 (sensitivity SENSITIVE in
    # YAML, "UNSTABLE" in prose).
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "models", "sensitivity_analysis.yaml"), "w") as f:
            yaml.safe_dump({"verdict": "SENSITIVE",
                            "load_bearing_parameters": []}, f)
        with open(os.path.join(d, "decision_rule.md"), "w") as f:
            f.write("## Sensitivity\n\nThe decision rule is UNSTABLE "
                    "with respect to PBO LLIN OR.\n")
        v = _audit_label_coherence(d)
        ok(any(x["duty"] == "label_coherence"
               and x["drifted_value"] == "UNSTABLE"
               and x["canonical_value"] == "SENSITIVE" for x in v),
           f"T1: R-019-style mismatch must fire, got {v}")

    # T1b: label coherence — generic-use qualifier (e.g., "robust to
    # perturbations") must NOT fire.
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "models", "sensitivity_analysis.yaml"), "w") as f:
            yaml.safe_dump({"verdict": "SENSITIVE",
                            "load_bearing_parameters": []}, f)
        with open(os.path.join(d, "report.md"), "w") as f:
            f.write("The PBO recommendation is robust to all "
                    "perturbations within the 95% CI.\n")
        v = _audit_label_coherence(d)
        ok(not any(x["drifted_value"] == "ROBUST" for x in v),
           f"T1b: 'robust to' should be filtered (generic use), got {v}")

    # T2: cross-file count — synthetic 691 vs 698 mismatch (R-020 class).
    with tempfile.TemporaryDirectory() as d:
        # Build a tiny CSV with 10 LGAs total: 8 PBO-only, 2 PBO+IRS
        with open(os.path.join(d, "allocation_optimized.csv"), "w") as f:
            f.write("lga_pcode,lga_name,zone,population,package,annual_cost\n")
            for i in range(8):
                f.write(f"L{i},Name{i},NW,100000,pbo_llin_80,1000\n")
            for i in range(8, 10):
                f.write(f"L{i},Name{i},NW,100000,pbo_llin_80_irs,5000\n")
        with open(os.path.join(d, "report.md"), "w") as f:
            # The numeric_consistency package-count regex anchors on
            # "<N> <package_keyword>" — prose says "12 PBO LGAs".
            f.write("## Allocation summary\n\nAcross all LGAs, 12 PBO "
                    "LGAs (the universal package) receive the dominant "
                    "intervention.\n")
        v = _audit_cross_file_counts(d)
        # Either 12 != 8 (PBO-only) or 12 != 10 (any-containing PBO);
        # both are wrong, so a drift should fire.
        ok(any(x["duty"] == "cross_file_counts"
               and "12" in x.get("claim", "") for x in v),
           f"T2: count mismatch (12 vs 8) must fire, got {v}")

    # T3: cross-file cost — synthetic $197M conflation.
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "allocation_optimized.csv"), "w") as f:
            f.write("lga_pcode,lga_name,zone,population,package,annual_cost\n")
            # 8 PBO-only @ $1M = $8M; 2 PBO+IRS @ $5M = $10M
            for i in range(8):
                f.write(f"L{i},N{i},NW,1,pbo_llin_80,1000000\n")
            for i in range(8, 10):
                f.write(f"L{i},N{i},NW,1,pbo_llin_80_irs,5000000\n")
        with open(os.path.join(d, "report.md"), "w") as f:
            # Claim "$50M for PBO LLIN 80%" — neither $8M (exact)
            # nor $18M (any-containing). Should fire.
            f.write("## Costs\n\nThe PBO LLIN 80% component costs "
                    "approximately $50M for 8 LGAs.\n")
        v = _audit_cross_file_counts(d)
        ok(any(x["duty"] == "cross_file_counts"
               and "$50" in x.get("drifted_value", "") for x in v),
           f"T3: cost conflation ($50M vs $8M/$18M) must fire, got {v}")

    # T4: self-contradicting — synthetic M-010 (notes say "converged to
    # 0.1" while point_estimate: 0.87).
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "models", "identifiability.yaml"), "w") as f:
            yaml.safe_dump({
                "parameters": [
                    {"name": "seasonal_amplitude",
                     "point_estimate": 0.87,
                     "lower_bound": 0.1,
                     "upper_bound": 2.0},
                ],
                "notes": (
                    "The seasonal_amplitude parameter converged to its "
                    "lower bound (0.1), suggesting it may not be "
                    "identifiable independently from ext_FOI."),
            }, f)
        v = _audit_self_contradicting(d)
        ok(any(x["duty"] == "self_contradicting"
               and "seasonal_amplitude" in x.get("claim", "") for x in v),
           f"T4: seasonal_amplitude self-contradiction must fire, got {v}")

    # T5: clean run — no drift, verdict CLEAN.
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "models", "sensitivity_analysis.yaml"), "w") as f:
            yaml.safe_dump({"verdict": "ROBUST",
                            "load_bearing_parameters": []}, f)
        with open(os.path.join(d, "report.md"), "w") as f:
            f.write("# Report\n\nThe analysis is internally consistent.\n")
        result = audit_run(d)
        ok(result["verdict"] == "CLEAN",
           f"T5: no drift should yield CLEAN verdict, got {result}")
        ok(not result["has_high"],
           "T5: clean run must have has_high=False")

    # T6: write_audit_yaml round-trips through YAML.
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "models", "sensitivity_analysis.yaml"), "w") as f:
            yaml.safe_dump({"verdict": "SENSITIVE",
                            "load_bearing_parameters": []}, f)
        with open(os.path.join(d, "decision_rule.md"), "w") as f:
            f.write("decision rule is UNSTABLE\n")
        path = write_audit_yaml(d)
        ok(os.path.exists(path), "T6: coherence_audit.yaml must be written")
        with open(path) as f:
            loaded = yaml.safe_load(f)
        ok(loaded["verdict"] == "DRIFT_DETECTED",
           f"T6: written YAML must round-trip, got {loaded}")
        ok(loaded["has_high"] is True,
           "T6: HIGH violation must propagate to has_high=True")

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
    p.add_argument("--json", action="store_true",
                   help="print full result to stdout as JSON")
    args = p.parse_args()

    if args.self_test:
        return _self_test()
    if not args.run_dir:
        p.error("run_dir is required (or use --self-test)")

    result = audit_run(args.run_dir)
    write_audit_yaml(args.run_dir, result)

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(f"verdict: {result['verdict']} (has_high={result['has_high']})",
              file=sys.stderr)
        for duty_name, duty_data in result["duties"].items():
            print(f"  {duty_name}: {duty_data['n']} violation(s)",
                  file=sys.stderr)
        for v in result["violations"]:
            print(f"    [{v['severity']}] {v['id']} ({v['duty']}) "
                  f"{v.get('location', '')}: "
                  f"{v.get('claim', '')[:140]}", file=sys.stderr)

    return 0 if not result["has_high"] else 1


if __name__ == "__main__":
    sys.exit(main())
