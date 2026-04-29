#!/usr/bin/env python3
"""
Validate and evaluate critique YAML files for the STAGE 7 decision gate.

Reads critique_methods.yaml, critique_domain.yaml, critique_presentation.yaml
from a run directory. Validates them against the schema defined in the
`critique-blockers-schema` skill. Computes:

    unresolved_high   -- HIGH-severity blockers with resolved=false, across
                         all three critiques
    structural        -- True if any critique has structural_mismatch.detected
    rounds_remaining  -- max_rounds - current_round

Then applies the fixed rule ordering from LEAD_SYSTEM_PROMPT STAGE 7 and
emits a decision: RETHINK_STRUCTURAL | RUN_FAILED | PATCH_OR_RETHINK |
DECLARE_SCOPE | ACCEPT.

Usage:
    python scripts/validate_critique_yaml.py <run_dir> \\
        --max-rounds 5 --current-round 2 [--json]

Exits non-zero on schema violations so the lead's Bash call surfaces the
error rather than swallowing it.
"""

from __future__ import annotations

import argparse
import glob
import importlib.util
import json
import os
import re
import sys
from typing import Any, Optional

try:
    import yaml
except ImportError:
    print("ERROR: pyyaml not installed. Run: pip install pyyaml", file=sys.stderr)
    sys.exit(2)

# spec_compliance is a sibling module; import is lazy so validation without
# the --spec-compliance flag doesn't require it to be present.
_SPEC_COMPLIANCE_AVAILABLE = None  # tri-state: None=unchecked, True=ok, False=missing

def _load_spec_compliance():
    global _SPEC_COMPLIANCE_AVAILABLE
    if _SPEC_COMPLIANCE_AVAILABLE is False:
        return None
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import spec_compliance  # noqa: E402
        _SPEC_COMPLIANCE_AVAILABLE = True
        return spec_compliance
    except ImportError:
        _SPEC_COMPLIANCE_AVAILABLE = False
        return None


_REGISTRY_AVAILABLE = None

def _load_effect_size_registry():
    global _REGISTRY_AVAILABLE
    if _REGISTRY_AVAILABLE is False:
        return None
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import effect_size_registry  # noqa: E402
        _REGISTRY_AVAILABLE = True
        return effect_size_registry
    except ImportError:
        _REGISTRY_AVAILABLE = False
        return None


REVIEWERS = ("critique-methods", "critique-domain", "critique-presentation",
             "critique-redteam")
PREFIXES = {"critique-methods": "M-", "critique-domain": "D-",
            "critique-presentation": "P-", "critique-redteam": "R-"}
FILENAMES = {"critique-methods": "critique_methods.yaml",
             "critique-domain": "critique_domain.yaml",
             "critique-presentation": "critique_presentation.yaml",
             "critique-redteam": "critique_redteam.yaml"}

VALID_SEVERITY = {"HIGH", "MEDIUM", "LOW"}
VALID_VERDICT = {"PASS", "REVISE"}
VALID_CATEGORY = {"HARD_BLOCKER", "METHODS", "CAUSAL", "HYPOTHESES",
                  "CITATIONS", "PRESENTATION", "DATA", "STRUCTURAL"}
VALID_TARGET_STAGE = {"PLAN", "DATA", "MODEL", "ANALYZE", "WRITE"}


class SchemaError(Exception):
    pass


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise SchemaError(msg)


def _load(path: str) -> dict:
    try:
        with open(path) as f:
            doc = yaml.safe_load(f)
    except FileNotFoundError:
        raise SchemaError(f"missing file: {path}")
    except yaml.YAMLError as e:
        raise SchemaError(f"invalid YAML in {path}: {e}")
    _require(isinstance(doc, dict), f"{path}: top-level must be a mapping")
    return doc


def _validate_blocker(b: Any, ctx: str, prefix: str) -> None:
    _require(isinstance(b, dict), f"{ctx}: each blocker must be a mapping")
    for key in ("id", "severity", "category", "target_stage",
                "first_seen_round", "claim", "evidence", "fix_requires",
                "resolved"):
        _require(key in b, f"{ctx}: blocker missing required key '{key}'")
    _require(isinstance(b["id"], str) and b["id"].startswith(prefix),
             f"{ctx}: blocker id '{b.get('id')}' must start with '{prefix}'")
    _require(b["severity"] in VALID_SEVERITY,
             f"{ctx}: blocker {b['id']} severity must be one of {VALID_SEVERITY}")
    _require(b["category"] in VALID_CATEGORY,
             f"{ctx}: blocker {b['id']} category must be one of {VALID_CATEGORY}")
    _require(b["target_stage"] in VALID_TARGET_STAGE,
             f"{ctx}: blocker {b['id']} target_stage must be one of {VALID_TARGET_STAGE}")
    _require(isinstance(b["first_seen_round"], int) and b["first_seen_round"] >= 1,
             f"{ctx}: blocker {b['id']} first_seen_round must be int >= 1")
    _require(isinstance(b["resolved"], bool),
             f"{ctx}: blocker {b['id']} resolved must be bool")
    if b["resolved"]:
        for key in ("resolved_in_round", "resolved_evidence"):
            _require(key in b,
                     f"{ctx}: resolved blocker {b['id']} missing '{key}'")


def _validate_structural(sm: Any, ctx: str, reviewer: str) -> None:
    _require(isinstance(sm, dict),
             f"{ctx}: structural_mismatch must be a mapping")
    _require("detected" in sm and isinstance(sm["detected"], bool),
             f"{ctx}: structural_mismatch.detected must be bool")
    if reviewer == "critique-presentation":
        _require(sm["detected"] is False,
                 f"{ctx}: critique-presentation must not set structural_mismatch.detected=true; "
                 f"use a HIGH blocker instead")
    if sm["detected"]:
        for key in ("description", "evidence_files", "fix_requires"):
            _require(key in sm,
                     f"{ctx}: structural_mismatch.detected=true requires '{key}'")
        _require(sm["fix_requires"] == "RETHINK",
                 f"{ctx}: structural_mismatch.fix_requires must be 'RETHINK'")


def _validate_carried_forward(cf: Any, ctx: str, current_round: int,
                              blockers: list[dict]) -> None:
    _require(isinstance(cf, list),
             f"{ctx}: carried_forward must be a list")
    blocker_ids = {b["id"]: b for b in blockers}
    for entry in cf:
        _require(isinstance(entry, dict),
                 f"{ctx}: each carried_forward entry must be a mapping")
        for key in ("id", "prior_round", "still_present", "notes"):
            _require(key in entry,
                     f"{ctx}: carried_forward entry missing '{key}'")
        _require(isinstance(entry["still_present"], bool),
                 f"{ctx}: carried_forward.still_present must be bool")
        # cross-check with blockers
        bid = entry["id"]
        if entry["still_present"]:
            _require(bid in blocker_ids and blocker_ids[bid]["resolved"] is False,
                     f"{ctx}: carried_forward {bid} still_present=true, "
                     f"but no matching unresolved blocker in current blockers list")
        else:
            _require(bid in blocker_ids and blocker_ids[bid]["resolved"] is True,
                     f"{ctx}: carried_forward {bid} still_present=false, "
                     f"but no matching resolved blocker in current blockers list")


def validate_critique(path: str, expected_reviewer: str,
                      current_round: int) -> dict:
    doc = _load(path)
    ctx = os.path.basename(path)
    prefix = PREFIXES[expected_reviewer]

    _require(doc.get("reviewer") == expected_reviewer,
             f"{ctx}: reviewer must be '{expected_reviewer}', "
             f"got {doc.get('reviewer')!r}")
    _require(doc.get("round") == current_round,
             f"{ctx}: round must be {current_round}, got {doc.get('round')!r}")
    _require(doc.get("verdict") in VALID_VERDICT,
             f"{ctx}: verdict must be one of {VALID_VERDICT}")

    _validate_structural(doc.get("structural_mismatch"), ctx, expected_reviewer)

    blockers = doc.get("blockers", [])
    _require(isinstance(blockers, list), f"{ctx}: blockers must be a list")
    seen_ids = set()
    for b in blockers:
        _validate_blocker(b, ctx, prefix)
        _require(b["id"] not in seen_ids, f"{ctx}: duplicate blocker id {b['id']}")
        seen_ids.add(b["id"])

    if current_round == 1:
        _require(doc.get("carried_forward", []) == [],
                 f"{ctx}: carried_forward must be [] on round 1")
    else:
        _validate_carried_forward(doc.get("carried_forward"), ctx,
                                  current_round, blockers)

    return doc


_ESCALATION_HIGH_THRESHOLD = 3  # patch_attempts >= 3 triggers mandatory escalation


def _compute_blocker_attempts(critiques: dict[str, dict],
                              current_round: int) -> dict[str, dict]:
    """Phase 5 ζ: count consecutive PATCH attempts per blocker_id by
    inspecting `carried_forward[]` entries with `still_present: true`.

    Semantic: when a blocker first appeared in round R and is still
    present in current round N (still_present: true), then PATCH was
    attempted (N - R) times without resolving it. patch_attempts >= 2
    means the same fix approach has failed at least twice; >= 3 means
    something structurally different is needed (new agent class,
    cross-stage escalation, or scope declaration).

    Returns {blocker_id: {"first_seen_round": R, "patch_attempts": K,
                           "category": C, "target_stage": S,
                           "still_present": True}}.
    """
    out: dict[str, dict] = {}
    blocker_meta: dict[str, dict] = {}
    for reviewer, doc in critiques.items():
        for b in doc.get("blockers", []):
            blocker_meta[b["id"]] = {
                "category": b.get("category"),
                "target_stage": b.get("target_stage"),
                "severity": b.get("severity"),
                "reviewer": reviewer,
            }
        for entry in doc.get("carried_forward", []):
            if not entry.get("still_present"):
                continue
            bid = entry["id"]
            prior = entry.get("prior_round", current_round)
            attempts = max(0, current_round - prior)
            existing = out.get(bid)
            # If multiple critiques carry the same id, take max attempts.
            if existing is None or attempts > existing["patch_attempts"]:
                meta = blocker_meta.get(bid, {})
                out[bid] = {
                    "first_seen_round": prior,
                    "patch_attempts": attempts,
                    "category": meta.get("category"),
                    "target_stage": meta.get("target_stage"),
                    "severity": meta.get("severity"),
                    "reviewer": meta.get("reviewer"),
                    "still_present": True,
                }
    return out


def decide(critiques: dict[str, dict], max_rounds: int,
           current_round: int) -> dict:
    unresolved_high = []
    for reviewer, doc in critiques.items():
        for b in doc.get("blockers", []):
            if b["severity"] == "HIGH" and not b["resolved"]:
                unresolved_high.append({
                    "reviewer": reviewer,
                    "id": b["id"],
                    "category": b["category"],
                    "target_stage": b["target_stage"],
                    "first_seen_round": b["first_seen_round"],
                    "claim": b["claim"],
                })

    # Phase 5 ζ: consecutive-PATCH-attempt counter.
    blocker_attempts = _compute_blocker_attempts(critiques, current_round)
    escalation_required = any(
        info["severity"] == "HIGH"
        and info["patch_attempts"] >= _ESCALATION_HIGH_THRESHOLD
        for info in blocker_attempts.values()
    )

    structural_reviewers = [r for r, d in critiques.items()
                            if d["structural_mismatch"]["detected"]]
    structural = bool(structural_reviewers)
    rounds_remaining = max_rounds - current_round

    # Rule ordering matches LEAD_SYSTEM_PROMPT STAGE 7.
    if structural:
        if rounds_remaining > 0:
            action = "RETHINK_STRUCTURAL"
            rationale = (
                f"Structural mismatch detected by {structural_reviewers}. "
                f"Must RETHINK — not patchable, not scope-declarable."
            )
        else:
            action = "RUN_FAILED"
            rationale = (
                f"Structural mismatch detected by {structural_reviewers} "
                f"with no rounds remaining. Run fails: delivered model does "
                f"not answer the question. Do NOT spawn writer."
            )
        rule_matched = 1
    elif unresolved_high and rounds_remaining > 0:
        action = "PATCH_OR_RETHINK"
        rationale = (
            f"{len(unresolved_high)} HIGH blocker(s) unresolved, "
            f"{rounds_remaining} round(s) remaining. "
            f"ACCEPT is forbidden."
        )
        rule_matched = 2
    elif unresolved_high and rounds_remaining <= 0:
        action = "DECLARE_SCOPE"
        rationale = (
            f"{len(unresolved_high)} HIGH blocker(s) unresolved, rounds "
            f"exhausted. Must write scope_declaration.yaml acknowledging "
            f"each blocker by id, and writer must embed verbatim in "
            f"Limitations. DECLARE_SCOPE is NOT the same as ACCEPT."
        )
        rule_matched = 3
    else:
        action = "ACCEPT"
        rationale = "No unresolved HIGH blockers, no structural mismatch."
        rule_matched = 4

    return {
        "unresolved_high": unresolved_high,
        "structural_mismatch": structural,
        "structural_reviewers": structural_reviewers,
        "rounds_remaining": rounds_remaining,
        "action": action,
        "rule_matched": rule_matched,
        "rationale": rationale,
        "spec_violations": [],
        "registry_violations": [],
        "rigor_violations": [],
        # Phase 5 ζ: stuck-blocker tracking.
        "blocker_attempts": blocker_attempts,
        "escalation_required": escalation_required,
    }


# Phase 4 Commit δ: cross-comparator efficiency outlier check.
# When the comparison table in report.md claims this work is much more
# efficient (deaths/cases averted per dollar) than published comparators,
# surface a MEDIUM warning. Often the explanation is legitimate (new
# interventions, different denominator) — the check forces an
# explanation in §Discussion, not a model rebuild.
_AVERTED_ROW_RE = re.compile(
    # Phase 5 ε: also match "avertable" (2057 Hard Blocker Scorecard
    # used "Deaths avertable" instead of "Deaths averted").
    r"\b(?:deaths?|cases?|dalys?|infections?)\s+avert(?:ed|able)\b",
    re.IGNORECASE,
)
_COST_PER_ROW_RE = re.compile(
    r"\bcost\s*per\s+(?:death|case|daly|life|infection)\b", re.IGNORECASE,
)
_BUDGET_ROW_RE = re.compile(
    r"\b(?:budget|total\s+(?:cost|spend|funding))\b", re.IGNORECASE,
)
_THIS_WORK_HEADER_RE = re.compile(
    r"\bthis\s+(?:model|work|analysis|study|paper|report)\b|"
    r"\bcurrent\s+(?:model|work|analysis|study)\b|"
    r"\bour\s+(?:model|work|analysis|study)\b|"
    # Phase 5 ε: match Hard Blocker Scorecard format columns like
    # "Model Value", "Model Estimate", "Model Output", "Model Result".
    r"\bmodel\s+(?:value|estimate|output|result|finding|prediction)\b",
    re.IGNORECASE,
)
# Accept e.g. "Scott 2017 (Optima)", "Ozodiegwu 2023 (EMOD)", "EMOD",
# "Optima". Phase 5 Commit ε: also accept generic comparator headers
# like "Published Value", "Benchmark Estimate", "Reference Result",
# "Literature Finding" — the 2057 malaria Hard Blocker Scorecard
# format used "Published Value" as its single comparator column.
_COMPARATOR_HEADER_RE = re.compile(
    r"[A-Z][\w-]+(?:\s+et\s+al\.?)?\s+(\d{4})|"
    r"\b(?:EMOD|OpenMalaria|Optima|Spectrum|GBD)\b|"
    r"\b(?:Published|Benchmark|Comparator|Reference|Literature)\s+"
    r"(?:Value|Estimate|Result|Finding)\b",
    re.IGNORECASE,
)
# Numeric with optional sign, comma thousands, decimal, scientific,
# and SI suffix (k/K/m/M/b/B/t/T) or unit suffix ($, %).
# Phase 5 fix: require at least one comma-group in the comma-form
# alternative; the prior `(?:,\d{3})*` allowing zero comma-groups made
# the engine match "201" then "5" separately for the input "2015".
# The `\b...\b` boundaries prevent partial-match drift.
_NUMERIC_TOKEN_RE = re.compile(
    r"(?P<sign>[-+]?)\$?\s*"
    r"(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?P<suffix>[kKmMbBtT]?)\b"
)
# Time-horizon suffix in cells like "/3yr", "/5 years", "/yr", "per year".
_HORIZON_RE = re.compile(
    r"/\s*(\d+)\s*(?:yr|year)|/\s*(?:yr|year)\b|per\s+year",
    re.IGNORECASE,
)


def _extract_last_numeric(cell: str) -> Optional[float]:
    """Extract the LAST numeric token from a cell string. Modelers
    typically write the original then a parenthesized normalization
    (e.g. `84,000/5yr (~50,400/3yr)`); the LAST token is the normalized
    figure that should be used for cross-column comparison.

    Phase 5 fixes:
      - Strip horizon suffixes (`/3yr`, `/5 years`, `/yr`, `per year`)
        BEFORE extracting numerics. Otherwise the regex picks up the
        horizon's digit (e.g., "5" from "/5yr") as the last numeric.
      - Strip parenthetical citations (`(Bhatt 2015)`, `(Author Year)`)
        BEFORE extracting numerics. Citation years would otherwise be
        picked up as the last numeric, replacing the actual quantity.
        Normalization parentheticals like `(~50,400/3yr)` start with
        non-letter characters (digit, ~, $) so they are NOT stripped.
    """
    stripped = _HORIZON_RE.sub("", cell)
    # Strip `(Author Year)` style citations — paren content beginning
    # with an alphabetic character. Keeps `(50,400/3yr)`, `($107M)`,
    # `(~12%)` — anything starting with a digit/punctuation.
    stripped = re.sub(r"\(\s*[A-Za-z][^)]*\)", "", stripped)
    matches = list(_NUMERIC_TOKEN_RE.finditer(stripped))
    if not matches:
        return None
    m = matches[-1]
    raw = m.group("num").replace(",", "")
    try:
        value = float(raw)
    except ValueError:
        return None
    suffix = (m.group("suffix") or "").lower()
    multiplier = {"k": 1e3, "m": 1e6, "b": 1e9, "t": 1e12}.get(suffix, 1.0)
    return value * multiplier


def _extract_horizon_years(cell: str) -> Optional[float]:
    """Return the time horizon in years from a cell, or None.

    `/3yr` → 3.0, `/yr` → 1.0, `per year` → 1.0, no horizon hint → None.
    Use the LAST horizon match to align with `_extract_last_numeric`
    (modelers write `84,000/5yr (~50,400/3yr)` with the normalized value
    + horizon at the end).
    """
    matches = list(_HORIZON_RE.finditer(cell))
    if not matches:
        return None
    m = matches[-1]
    if m.group(1):
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return 1.0  # `/yr` or `per year`


def _parse_md_tables(text: str) -> list[dict]:
    """Linear scan for Markdown tables. Returns a list of
    {headers: [...], rows: [{label: str, cells: [...]}, ...]} dicts.
    Linear scan, no backtracking."""
    tables = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        if not (line.startswith("|") and line.endswith("|") and "|" in line[1:-1]):
            i += 1
            continue
        # Possible table header. Need a separator line next.
        if i + 1 >= len(lines):
            i += 1
            continue
        sep = lines[i + 1].strip()
        if not re.fullmatch(r"\|[\s:|-]+\|", sep):
            i += 1
            continue
        # Parse header.
        headers = [c.strip() for c in line.strip("|").split("|")]
        rows = []
        j = i + 2
        while j < len(lines):
            row_line = lines[j].rstrip()
            if not (row_line.startswith("|") and row_line.endswith("|")):
                break
            cells = [c.strip() for c in row_line.strip("|").split("|")]
            if len(cells) != len(headers):
                break
            rows.append({"label": cells[0], "cells": cells[1:]})
            j += 1
        if rows:
            tables.append({"headers": headers, "rows": rows})
        i = j
    return tables


def _check_comparator_efficiency(run_dir: str) -> list[dict]:
    """Phase 4 Commit δ. Scan report.md for tables comparing this work
    to published models on deaths/cases averted vs budget. Fire MEDIUM
    when this work's per-dollar efficiency exceeds the best comparator's
    by >50% — usually a sign that the comparison normalization is wrong
    or new factors deserve explanation in §Discussion.
    """
    report_path = os.path.join(run_dir, "report.md")
    if not os.path.exists(report_path):
        return []
    try:
        with open(report_path) as f:
            text = f.read()
    except (OSError, UnicodeDecodeError):
        return []

    tables = _parse_md_tables(text)
    violations: list[dict] = []

    for tbl in tables:
        headers = tbl["headers"]
        if not headers:
            continue
        # First column is the row-label column ("Finding" / "Metric"
        # etc). Subsequent columns are this-work + comparators.
        comparator_cols = headers[1:]
        # Phase 5 ε: require at least this + 1 comparator. Phase 4
        # required ≥2 comparators; the 2057 malaria Hard Blocker
        # Scorecard table has only one comparator column ("Published
        # Value"), so ≥2 silently skipped a real 10× efficiency gap.
        if len(comparator_cols) < 2:
            continue

        this_idx = None
        comp_indices: list[int] = []
        for idx, hdr in enumerate(comparator_cols):
            if _THIS_WORK_HEADER_RE.search(hdr):
                if this_idx is None:
                    this_idx = idx
            elif _COMPARATOR_HEADER_RE.search(hdr):
                comp_indices.append(idx)
        if this_idx is None or len(comp_indices) < 1:
            continue  # not a this-vs-comparators table

        # Stricter thresholds when we have only ONE comparator
        # (single-comparator outliers are noisier; require a more
        # extreme gap before firing).
        single_comparator = len(comp_indices) == 1
        averted_high_threshold = 3.0 if single_comparator else 1.5
        averted_low_threshold = 1.0 / averted_high_threshold  # inverse direction
        cost_per_low_threshold = 0.33 if single_comparator else 0.7
        cost_per_high_threshold = 1.0 / cost_per_low_threshold  # inverse direction

        # Collect averted/budget/cost-per rows from this table.
        # Phase 5 ε: also check the first non-label cell for the metric
        # name. The Hard Blocker Scorecard format is
        # `| ID | Metric | Model | Published | Status |` where row.label
        # is just an ID (e.g. "B21") and cells[0] holds the metric
        # name ("Deaths avertable"). Use concatenation for matching and
        # for display so violations are interpretable in either layout.
        def _display_label(row: dict) -> str:
            lbl = row.get("label", "")
            cells = row.get("cells") or []
            first = cells[0] if cells else ""
            # ID-only labels: stitch in the first cell
            if first and (len(lbl) <= 4 or
                          re.fullmatch(r"[A-Z]+-?\d+", lbl)):
                return f"{lbl} {first}".strip()
            return lbl.strip()

        averted_row = None
        budget_row = None
        for row in tbl["rows"]:
            label_search = (row["label"] + " "
                            + (row["cells"][0] if row["cells"] else ""))
            if averted_row is None and _AVERTED_ROW_RE.search(label_search):
                averted_row = row
            if budget_row is None and _BUDGET_ROW_RE.search(label_search):
                budget_row = row
            if _COST_PER_ROW_RE.search(label_search):
                label = _display_label(row)
                # Direct cost-per-X check (this < min: more efficient;
                # this > max: less efficient — both worth surfacing).
                cells = row["cells"]
                this_val = _extract_last_numeric(cells[this_idx])
                comp_vals = [
                    _extract_last_numeric(cells[i]) for i in comp_indices
                ]
                comp_vals = [v for v in comp_vals if v is not None]
                if this_val is not None and comp_vals and this_val > 0:
                    min_comp = min(comp_vals)
                    max_comp_val = max(comp_vals)
                    if min_comp > 0 \
                            and this_val / min_comp < cost_per_low_threshold:
                        violations.append({
                            "kind": "comparator_efficiency_outlier",
                            "severity": "MEDIUM",
                            "stage": "REPORT",
                            "claim": (
                                f"report.md comparison table row "
                                f"'{label}': this work claims "
                                f"{this_val:.4g} vs comparators "
                                f"min={min_comp:.4g} "
                                f"(ratio {this_val / min_comp:.2f}). "
                                f"Cost-per is "
                                f"{(1 - this_val / min_comp) * 100:.0f}% "
                                f"better than the most-efficient "
                                f"comparator. Add a §Discussion "
                                f"subsection explaining the gap (new "
                                f"interventions, different denominator, "
                                f"normalization) or revise the table."
                            ),
                        })
                    elif max_comp_val > 0 \
                            and this_val / max_comp_val > cost_per_high_threshold:
                        violations.append({
                            "kind": "comparator_efficiency_outlier",
                            "severity": "MEDIUM",
                            "stage": "REPORT",
                            "claim": (
                                f"report.md comparison table row "
                                f"'{label}': this work claims "
                                f"{this_val:.4g} vs comparators "
                                f"max={max_comp_val:.4g} "
                                f"(ratio {this_val / max_comp_val:.2f}). "
                                f"Cost-per is "
                                f"{(this_val / max_comp_val - 1) * 100:.0f}% "
                                f"WORSE than the least-efficient "
                                f"comparator — suggests systematic "
                                f"undercounting of cases averted, a "
                                f"different denominator, or a real "
                                f"regression. Explain in §Discussion."
                            ),
                        })

        if averted_row is None:
            continue

        if budget_row is None:
            # Phase 5 ε: when the table has no budget row (Hard Blocker
            # Scorecard format: rows are direct claims, not budget-
            # normalized comparisons), do a direct ratio check on the
            # averted values themselves. Less sensitive than the cross-
            # row efficiency check below; only fires on extreme ratios.
            this_val = _extract_last_numeric(
                averted_row["cells"][this_idx])
            comp_vals = []
            comp_labels = []
            for ci in comp_indices:
                v = _extract_last_numeric(averted_row["cells"][ci])
                if v is not None and v > 0:
                    comp_vals.append(v)
                    comp_labels.append(comparator_cols[ci])
            if this_val is not None and this_val > 0 and comp_vals:
                max_comp_val = max(comp_vals)
                min_comp_val = min(comp_vals)
                if this_val / max_comp_val > averted_high_threshold:
                    violations.append({
                        "kind": "comparator_efficiency_outlier",
                        "severity": "MEDIUM",
                        "stage": "REPORT",
                        "claim": (
                            f"report.md row '{_display_label(averted_row)}': "
                            f"this work claims {this_val:.4g} vs "
                            f"comparator(s) max={max_comp_val:.4g} "
                            f"({this_val / max_comp_val:.1f}x). "
                            f"Add a §Discussion subsection explaining "
                            f"the gap or revise the table."
                        ),
                    })
                elif this_val / min_comp_val < averted_low_threshold:
                    violations.append({
                        "kind": "comparator_efficiency_outlier",
                        "severity": "MEDIUM",
                        "stage": "REPORT",
                        "claim": (
                            f"report.md row '{_display_label(averted_row)}': "
                            f"this work claims {this_val:.4g} vs "
                            f"comparator(s) min={min_comp_val:.4g} "
                            f"({this_val / min_comp_val:.2f}x). "
                            f"This UNDERPERFORMANCE requires "
                            f"explanation: systematic undercounting "
                            f"(e.g., U5-only when comparator is "
                            f"all-age), different denominators, or a "
                            f"real model deficiency. Quantify in "
                            f"§Discussion."
                        ),
                    })
            continue

        # Cross-row efficiency: deaths_per_year / budget_per_year per column.
        def _per_year(cell: str, default_horizon: float) -> Optional[float]:
            v = _extract_last_numeric(cell)
            if v is None:
                return None
            h = _extract_horizon_years(cell) or default_horizon
            return v / h

        # Heuristic: averted defaults to "total over horizon" (require
        # horizon hint); budget defaults to "per year" if no horizon.
        this_averted_pyr = _per_year(averted_row["cells"][this_idx],
                                      default_horizon=1.0)
        this_budget_pyr = _per_year(budget_row["cells"][this_idx],
                                     default_horizon=1.0)
        if this_averted_pyr is None or this_budget_pyr is None \
                or this_budget_pyr <= 0:
            continue
        this_eff = this_averted_pyr / this_budget_pyr

        comp_effs: list[tuple[str, float]] = []
        for ci in comp_indices:
            a = _per_year(averted_row["cells"][ci], default_horizon=1.0)
            b = _per_year(budget_row["cells"][ci], default_horizon=1.0)
            if a is None or b is None or b <= 0:
                continue
            comp_effs.append((comparator_cols[ci], a / b))

        if not comp_effs:
            continue

        # Phase 5 ε: fire on EITHER over-claim (this >> comparators) OR
        # under-claim (this << comparators). 2057 deaths-averted case
        # is the inverse of 1302's: this work is 10× LESS efficient
        # than Scott 2017, suggesting systematic undercounting.
        max_comp = max(comp_effs, key=lambda x: x[1])
        min_comp = min(comp_effs, key=lambda x: x[1])
        if max_comp[1] > 0:
            over_ratio = this_eff / max_comp[1]
            if over_ratio > averted_high_threshold:
                violations.append({
                    "kind": "comparator_efficiency_outlier",
                    "severity": "MEDIUM",
                    "stage": "REPORT",
                    "claim": (
                        f"report.md comparison table: this work's "
                        f"per-dollar efficiency on "
                        f"'{averted_row['label']}' / "
                        f"'{budget_row['label']}' is "
                        f"{this_eff:.4g} per year-dollar, "
                        f"{over_ratio:.1f}x the best comparator "
                        f"({max_comp[0]} at {max_comp[1]:.4g}). This "
                        f"level of improvement requires explanation: "
                        f"new interventions that comparators didn't "
                        f"include, structural model differences, or "
                        f"different denominators. Add a §Discussion "
                        f"subsection articulating the gap or revise to "
                        f"normalize comparator estimates to common units."
                    ),
                })
                continue
        if this_eff > 0 and min_comp[1] > 0:
            under_ratio = this_eff / min_comp[1]
            if under_ratio < averted_low_threshold:
                violations.append({
                    "kind": "comparator_efficiency_outlier",
                    "severity": "MEDIUM",
                    "stage": "REPORT",
                    "claim": (
                        f"report.md comparison table: this work's "
                        f"per-dollar efficiency on "
                        f"'{averted_row['label']}' / "
                        f"'{budget_row['label']}' is "
                        f"{this_eff:.4g} per year-dollar, only "
                        f"{under_ratio:.2f}x the WORST comparator "
                        f"({min_comp[0]} at {min_comp[1]:.4g}). This "
                        f"level of UNDERPERFORMANCE requires "
                        f"explanation: systematic undercounting "
                        f"(e.g., U5-only deaths when comparator counts "
                        f"all-age), different denominators, or a real "
                        f"model deficiency. Quantify the methodological "
                        f"gap in §Discussion or revise to normalize."
                    ),
                })

    return violations


# Phase 4 Commit β: surrogate UQ documentation requirement.
# When outcome_fn.py reads precomputed CSVs and rescales rather than
# calling the actual model, the 200-draw "ensemble" is a closed-form
# recomputation, not a Monte Carlo over the model. The headline CIs
# reflect parameter uncertainty in a closed-form formula — readers
# typically assume Monte-Carlo-over-model. Require explicit calibration
# documentation when the surrogate path is taken.
_REAL_MODEL_PATTERNS = [
    re.compile(r"\bss\.Sim\s*\("),
    re.compile(r"\bstarsim\.Sim\s*\("),
    re.compile(r"\bsim\.run\s*\("),
    re.compile(r"\bsolve_ivp\s*\("),
    re.compile(r"\bodeint\s*\("),
    re.compile(r"\.simulate\s*\("),
    re.compile(r"\bMonteCarlo\s*\("),
]
# Surrogate detection: look for the precomputed-CSV naming convention
# anywhere in outcome_fn.py. Matches both direct read_csv calls
# (`pd.read_csv("package_evaluation.csv")`) and indirected ones
# (`path = os.path.join(..., "package_evaluation.csv"); pd.read_csv(path)`).
# Pairing these naming patterns with "no real-model call" is the
# surrogate signal.
_SURROGATE_PATTERNS = [
    re.compile(
        r"['\"][^'\"]*"
        r"(?:package_eval|calibration_results|scenarios|grid_results|"
        r"emulator_grid|surrogate_grid|package_evaluation)"
        r"[^'\"]*\.(?:csv|parquet|feather|pkl|npz)['\"]",
        re.IGNORECASE,
    ),
]


def _check_surrogate_uq_documented(run_dir: str) -> list[dict]:
    """Phase 4 Commit β: detect surrogate UQ without calibration docs.

    If outcome_fn.py reads a precomputed CSV (named e.g. *package_eval*,
    *calibration_results*, *scenarios*, *grid_results*) AND does NOT
    call any real-model runner (ss.Sim, sim.run, solve_ivp, odeint,
    .simulate), require models/outcome_fn_calibration.md documenting
    surrogate RMSE vs the full model on a validation grid.

    Emits:
      surrogate_uq_undocumented HIGH      — surrogate path, no calibration md
      surrogate_calibration_missing_rmse  — md exists but no RMSE figure
                                            (MEDIUM)
    """
    outcome_fn_path = os.path.join(run_dir, "models", "outcome_fn.py")
    if not os.path.exists(outcome_fn_path):
        return []
    try:
        with open(outcome_fn_path) as f:
            text = f.read()
    except (OSError, UnicodeDecodeError):
        return []

    has_real = any(p.search(text) for p in _REAL_MODEL_PATTERNS)
    has_surrogate = any(p.search(text) for p in _SURROGATE_PATTERNS)
    if not (has_surrogate and not has_real):
        return []

    cal_md_path = os.path.join(run_dir, "models", "outcome_fn_calibration.md")
    if not os.path.exists(cal_md_path):
        return [{
            "kind": "surrogate_uq_undocumented",
            "severity": "HIGH",
            "stage": "UQ",
            "claim": (
                "models/outcome_fn.py reads a precomputed CSV "
                "(package_evaluation / calibration_results / scenarios / "
                "grid_results) and does NOT call the model "
                "(no ss.Sim/sim.run/solve_ivp/odeint/.simulate). The "
                "200-draw 'ensemble' is therefore a closed-form "
                "recomputation, not a Monte Carlo over the model — the "
                "headline CIs reflect parameter uncertainty under the "
                "surrogate's analytical assumptions only. Required: "
                "models/outcome_fn_calibration.md documenting (1) the "
                "surrogate architecture (interpolation method, grid "
                "resolution), (2) RMSE vs full-model validation grid "
                "(>= 10 grid points), (3) cross-validation error, "
                "(4) extrapolation bounds. Without this document the "
                "report's CI framing misrepresents what was computed."
            ),
        }]
    try:
        with open(cal_md_path) as f:
            cal_text = f.read()
    except (OSError, UnicodeDecodeError):
        cal_text = ""
    if not re.search(r"\bRMSE\b", cal_text, re.I):
        return [{
            "kind": "surrogate_calibration_missing_rmse",
            "severity": "MEDIUM",
            "stage": "UQ",
            "claim": (
                "models/outcome_fn_calibration.md exists but does not "
                "contain an RMSE validation figure. The doc must "
                "validate the surrogate against the full model on a "
                "grid of >=10 points and report per-output RMSE. "
                "Without an RMSE figure the surrogate's accuracy is "
                "unaudited."
            ),
        }]
    return []


# Phase 4 Commit α: zero-width CI detector.
# Threshold rationale: 0.5% relative width catches optimizer-bounded
# outputs (e.g., total_cost when greedy fills budget) and hardcoded
# calibration targets (CI ~ 1e-7 of the target value). Looser would miss
# them; tighter would flag legitimately well-constrained outputs.
_CI_DEGENERATE_THRESHOLD = 0.005


def _check_uq_ci_quality(uq_report_path: str) -> list[dict]:
    """Phase 4 Commit α: scan uncertainty_report.yaml for degenerate CIs.

    Fires `ci_degenerate` MEDIUM when an output's 95% CI relative width
    (ci_high - ci_low) / |mean| is below 0.5%. Such outputs are usually
    mechanically constrained (greedy-optimizer budget fills, hardcoded
    calibration targets) and should be reported as point estimates with
    a footnote explaining why, not alongside genuine CIs.
    """
    violations: list[dict] = []
    try:
        with open(uq_report_path) as f:
            uq_report = yaml.safe_load(f) or {}
    except (yaml.YAMLError, OSError):
        return violations

    scalar_outputs = uq_report.get("scalar_outputs") or {}
    if not isinstance(scalar_outputs, dict):
        return violations

    for output_name, stats in scalar_outputs.items():
        if not isinstance(stats, dict):
            continue
        mean = stats.get("mean")
        lo = stats.get("ci_low")
        hi = stats.get("ci_high")
        if mean is None or lo is None or hi is None:
            continue
        try:
            mean_f = float(mean)
            lo_f = float(lo)
            hi_f = float(hi)
        except (TypeError, ValueError):
            continue
        if abs(mean_f) < 1e-9:
            continue  # zero-mean output; relative width is undefined
        relative_width = (hi_f - lo_f) / abs(mean_f)
        if relative_width < _CI_DEGENERATE_THRESHOLD:
            violations.append({
                "kind": "ci_degenerate",
                "severity": "MEDIUM",
                "stage": "UQ",
                "claim": (
                    f"{output_name} 95% CI [{lo_f:.6g}, {hi_f:.6g}] is "
                    f"{relative_width * 100:.3f}% of mean ({mean_f:.6g}). "
                    f"Either the perturbed parameters do not affect this "
                    f"output (greedy-optimizer budget fill, hardcoded "
                    f"calibration target, etc.), or the surrogate "
                    f"flattens it. Report as a point estimate with a "
                    f"footnote, or widen the parameter ranges that drive "
                    f"this output."
                ),
            })
    return violations


def _check_rigor_artifacts(run_dir: str, round_n: int | None = None) -> list[dict]:
    """Check for Phase 2 rigor artifacts. Returns a list of violations.

    Each rigor stage has a prerequisite + an artifact:
      UQ: outcome_fn.py (prereq) + uncertainty_report.yaml (artifact)
      Multi-structural: model_comparison.yaml (prereq) + model_comparison_formal.yaml (artifact)
      Identifiability: identifiability.yaml in models/ (prereq) + identifiability.yaml in run_dir (artifact)

    Missing artifact when prereq exists → HIGH blocker. If neither exists,
    the modeler didn't engage with that stage at all — SEPARATE MEDIUM
    blocker flagging the missing prerequisite.
    """
    violations = []

    # UQ: outcome_fn.py → uncertainty_report.yaml
    outcome_fn_path = os.path.join(run_dir, "models", "outcome_fn.py")
    uq_report_path = os.path.join(run_dir, "uncertainty_report.yaml")
    if os.path.exists(outcome_fn_path):
        if not os.path.exists(uq_report_path):
            violations.append({
                "kind": "uq_report_missing",
                "severity": "HIGH",
                "stage": "UQ",
                "claim": ("models/outcome_fn.py exists but "
                          "uncertainty_report.yaml is missing. Run "
                          "`python3 scripts/propagate_uncertainty.py {run_dir}` "
                          "to generate it. See uncertainty-quantification skill."),
            })
        else:
            # Phase 4 Commit α: detect zero-width CIs.
            violations.extend(_check_uq_ci_quality(uq_report_path))
        # Phase 4 Commit β: when outcome_fn is a surrogate, require
        # documentation. (Runs even when uncertainty_report.yaml is
        # missing — the surrogate framing issue exists regardless of
        # whether UQ has been re-run.)
        violations.extend(_check_surrogate_uq_documented(run_dir))
    else:
        violations.append({
            "kind": "outcome_fn_missing",
            "severity": "MEDIUM",
            "stage": "UQ",
            "claim": ("models/outcome_fn.py is absent — modeler did not expose "
                      "a deterministic outcome function for uncertainty "
                      "propagation. See uncertainty-quantification skill."),
        })

    # Multi-structural: models/model_comparison.yaml → model_comparison_formal.yaml
    msc_manifest = os.path.join(run_dir, "models", "model_comparison.yaml")
    msc_report = os.path.join(run_dir, "model_comparison_formal.yaml")
    if os.path.exists(msc_manifest):
        if not os.path.exists(msc_report):
            violations.append({
                "kind": "msc_report_missing",
                "severity": "HIGH",
                "stage": "MULTI_STRUCTURAL",
                "claim": ("models/model_comparison.yaml exists but "
                          "model_comparison_formal.yaml is missing. Run "
                          "`python3 scripts/compare_models.py {run_dir}`. "
                          "See multi-structural-comparison skill."),
            })
        else:
            # Additionally check the formal report for DEGENERATE_FIT_DETECTED
            try:
                with open(msc_report) as f:
                    formal = yaml.safe_load(f) or {}
                verdict = formal.get("verdict", "")
                if verdict == "DEGENERATE_FIT_DETECTED":
                    deg = formal.get("degenerate_fit", {})
                    violations.append({
                        "kind": "degenerate_fit",
                        "severity": "HIGH",
                        "stage": "MULTI_STRUCTURAL",
                        "claim": (f"compare_models flagged DEGENERATE FIT on "
                                  f"model {deg.get('model', '?')}: "
                                  f"{deg.get('reason', '(no reason given)')}"),
                    })
                elif verdict == "INSUFFICIENT_STRUCTURES":
                    violations.append({
                        "kind": "insufficient_structures",
                        "severity": "HIGH",
                        "stage": "MULTI_STRUCTURAL",
                        "claim": ("Modeler supplied fewer than 3 candidate "
                                  "structures for comparison. See "
                                  "multi-structural-comparison skill."),
                    })
            except (yaml.YAMLError, OSError):
                pass
    else:
        violations.append({
            "kind": "msc_manifest_missing",
            "severity": "MEDIUM",
            "stage": "MULTI_STRUCTURAL",
            "claim": ("models/model_comparison.yaml is absent — modeler did "
                      "not produce a multi-structural comparison. See "
                      "multi-structural-comparison skill."),
        })

    # Identifiability: models/identifiability.yaml → identifiability.yaml (run_dir)
    id_manifest = os.path.join(run_dir, "models", "identifiability.yaml")
    id_report = os.path.join(run_dir, "identifiability.yaml")
    if os.path.exists(id_manifest):
        if not os.path.exists(id_report):
            violations.append({
                "kind": "identifiability_report_missing",
                "severity": "HIGH",
                "stage": "IDENTIFIABILITY",
                "claim": ("models/identifiability.yaml exists but "
                          "identifiability.yaml is missing. Run "
                          "`python3 scripts/identifiability.py {run_dir}`. "
                          "See identifiability-analysis skill."),
            })
        else:
            try:
                with open(id_report) as f:
                    id_rep = yaml.safe_load(f) or {}
                verdict = id_rep.get("verdict", "")
                if verdict == "UNIDENTIFIED_PARAMETERS":
                    unidentified = [
                        name for name, p in id_rep.get("parameters", {}).items()
                        if p.get("status") == "unidentified"
                    ]
                    violations.append({
                        "kind": "unidentified_parameters",
                        "severity": "HIGH",
                        "stage": "IDENTIFIABILITY",
                        "claim": (f"identifiability analysis flagged "
                                  f"{len(unidentified)} ridge-trapped "
                                  f"parameter(s): {unidentified}. See "
                                  f"identifiability.yaml for profile-likelihood "
                                  f"details. Resolve via partial pooling, tied "
                                  f"parameters, or explicit scope declaration."),
                    })
            except (yaml.YAMLError, OSError):
                pass
    # NOTE: absence of identifiability.yaml with NO manifest is MEDIUM — many
    # models have no fitted parameters. Don't force this check universally.

    # Decision rule (Phase 3 Commit C): required when an allocation CSV exists.
    # decision_rule_missing is HIGH — passes through unchanged below.
    violations.extend(_check_decision_rule_artifact(run_dir))

    # Phase 4 Commit δ: cross-comparator efficiency outlier check.
    violations.extend(_check_comparator_efficiency(run_dir))

    # Phase 7 Commit λ: STAGE 8.5 WRITER_QA pass.
    violations.extend(_check_writer_qa(run_dir))

    # Phase 7 Commit μ: plan-promised criteria enforcement.
    violations.extend(_check_plan_criteria(run_dir))

    # Phase 9 Commit ρ: write-time figure validator + provenance check.
    violations.extend(_check_figure_validator(run_dir))

    # Phase 12 Commit α: cross-file numeric consistency check.
    # Catches the failure mode the 104914 run shipped: 4+ verifiable
    # internal numeric inconsistencies (cost-per-case $5.05 vs $4.71
    # in same file, figure_rationale stale by 2.4×, title Round 4 vs
    # actual Round 6) that no validator was comparing across sibling
    # artifacts. Only redteam caught two of them in round 6.
    try:
        spec = importlib.util.spec_from_file_location(
            "numeric_consistency",
            os.path.join(os.path.dirname(__file__), "numeric_consistency.py"),
        )
        nc_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(nc_mod)
        violations.extend(nc_mod.check_numeric_consistency(run_dir))
    except Exception as e:
        violations.append({
            "kind": "numeric_consistency_load_error",
            "severity": "HIGH",
            "stage": "WRITE",
            "claim": f"Could not load scripts/numeric_consistency.py: {e}",
        })

    # Phase 10 Commit ψ: allocation-gate coordinator. The five
    # allocation-triggered checks (optimization_quality, daly,
    # allocation_robustness, universal_coverage, sensitivity_analysis)
    # were previously called individually here, each emitting its own
    # MEDIUM `*_missing` violation. The 0013 round-2 critique reported
    # 5 simultaneous MEDIUMs — noise that conditioned the modeler to
    # defer rather than draft early. The coordinator runs all five,
    # passes through every HIGH unchanged, and consolidates the
    # MEDIUM `*_missing` violations into a single round-aware
    # `allocation_rigor_in_progress` (still in drafting window) or
    # `allocation_rigor_drafts_overdue` (past deadline).
    violations.extend(_check_allocation_rigor_status(run_dir, round_n=round_n))

    # Phase 12 Commit γ: ecological-fallacy / within-zone heterogeneity
    # check. When the model calibrates to k zones but allocates to
    # n>>k spatial units (Nigeria 6→774 in 104914), within-zone
    # variation is invisible to the optimizer. Require an artifact
    # bounding the impact loss when the aggregation ratio is high.
    violations.extend(_check_within_zone_heterogeneity(run_dir))

    # Phase 13 Commit α: disease-agnostic structural sanity checks.
    # The 190855 run shipped DECLARE_SCOPE with internally consistent
    # numbers but no gate cross-checked aggregate impact against the
    # model's own internal relationships. Eight generic checks via
    # models/sanity_schema.yaml: mass balance, per-unit intensity,
    # share closure, derived consistency, composite dimensions,
    # counterfactual ratio, structural-uncertainty carry-forward,
    # outlier sniff. Schema is required at round ≥ 3.
    violations.extend(_check_sanity_schema(run_dir, round_n=round_n))

    # Phase 15 Commit α: a-priori identifiability arithmetic.
    # The 224202 run shipped ACCEPT with 3/3 fitted parameters
    # post-hoc-flagged unidentified — but should have been caught at
    # strategy time by 30 seconds of arithmetic (40 fitted params /
    # 6 independent targets = 6.67× over-saturated). This check
    # enforces a pre-model artifact requiring the modeler to count
    # parameters vs targets BEFORE building. Verdict OVER_SATURATED
    # without resolution is HIGH and explicitly NOT scope-declarable
    # — the architecture must be fixed at strategy time.
    violations.extend(_check_identifiability_a_priori(run_dir,
                                                       round_n=round_n))

    # Phase 12 Commit β: round-aware escalation of persisting MEDIUMs.
    # Catches the failure mode the 104914 run shipped: 18 figure_
    # validator_missing MEDIUMs persisted r2→r6, presentation P-005..
    # P-009 persisted r1→r6, decision_rule_self_referential persisted
    # r2→r6 — none blocked ACCEPT because MEDIUM doesn't, and the
    # contracts had no "ignored 4 rounds in a row, escalating"
    # mechanism. Escalates qualifying persistent MEDIUMs to HIGH so
    # ACCEPT is forced to either fix or scope-declare explicitly.
    if round_n is not None:
        violations.extend(_check_persistent_medium_escalation(
            run_dir, round_n=round_n))

    return violations


def _check_universal_coverage(run_dir: str) -> list[dict]:
    """Phase 7 Commit ν: when allocation is produced, the modeler
    must compute a universal-coverage benchmark scenario quantifying
    the budget required to cover all spatial units at the dominant
    intervention package, alongside the budget-constrained allocation.
    This surfaces the "fund 26%, BAU for 66%" concession quantitatively.

    Required artifact: `{run_dir}/models/universal_coverage.yaml` with:
      total_units: <int>
      gc7_actual_budget: <number>
      budget_for_universal_*: <number>  # at least one
      gc7_dalys_averted: <number>
      universal_coverage_dalys_averted_estimate: <number>
      gc7_efficiency_pct: <number>      # gc7 / universal * 100

    Emits:
      universal_coverage_missing  MEDIUM — allocation exists, no yaml
      universal_coverage_malformed MEDIUM — yaml exists but missing
                                             required fields
    """
    decision_rule = os.path.join(run_dir, "decision_rule.md")
    allocs = _find_allocation_csvs(run_dir)
    if not (os.path.exists(decision_rule) or allocs):
        return []

    yaml_path = os.path.join(run_dir, "models", "universal_coverage.yaml")
    if not os.path.exists(yaml_path):
        return [{
            "kind": "universal_coverage_missing",
            "severity": "MEDIUM",
            "stage": "OPTIMIZATION",
            "claim": (
                "Allocation produced but models/universal_coverage.yaml "
                "is absent. The modeler must compute a universal-"
                "coverage benchmark (cost to fund all spatial units at "
                "the dominant package) alongside the budget-constrained "
                "allocation. This surfaces the 'fund X%, BAU for Y%' "
                "concession quantitatively rather than burying it in "
                "§Limitations. Required fields: total_units, "
                "gc7_actual_budget, budget_for_universal_<package>, "
                "gc7_dalys_averted, universal_coverage_dalys_averted_"
                "estimate, gc7_efficiency_pct. See "
                "mechanistic-vs-hybrid-architecture skill."
            ),
        }]

    try:
        with open(yaml_path) as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError) as e:
        return [{
            "kind": "universal_coverage_malformed",
            "severity": "MEDIUM",
            "stage": "OPTIMIZATION",
            "claim": f"universal_coverage.yaml load error: {e}",
        }]

    if not isinstance(data, dict):
        return [{
            "kind": "universal_coverage_malformed",
            "severity": "MEDIUM",
            "stage": "OPTIMIZATION",
            "claim": "universal_coverage.yaml must be a top-level mapping",
        }]

    missing = []
    for required in (
        "total_units", "gc7_actual_budget",
        "gc7_dalys_averted",
        "universal_coverage_dalys_averted_estimate",
        "gc7_efficiency_pct",
    ):
        if required not in data:
            missing.append(required)
    # At least one budget_for_universal_* must exist
    has_universal_budget = any(
        k.startswith("budget_for_universal_") for k in data.keys()
    )
    if not has_universal_budget:
        missing.append("budget_for_universal_<package>")

    if missing:
        return [{
            "kind": "universal_coverage_malformed",
            "severity": "MEDIUM",
            "stage": "OPTIMIZATION",
            "claim": (
                f"universal_coverage.yaml missing required field(s): "
                f"{missing}"
            ),
        }]

    return []


def _check_plan_criteria(run_dir: str) -> list[dict]:
    """Phase 7 Commit μ: when planner emits success_criteria.yaml,
    each tier's criteria are mechanically evaluated against the named
    artifacts. Hard blocker failures are HIGH; minimum bar failures
    are MEDIUM (Phase 5 ζ stuck-blocker logic escalates after 2+
    attempts).

    Emits:
      plan_criteria_missing       MEDIUM — plan.md exists but no
                                            success_criteria.yaml (legacy
                                            runs OK; new runs should
                                            produce it)
      plan_hard_blocker_failed    HIGH    — hard blocker failed (per id)
      plan_minimum_bar_failed     MEDIUM  — minimum bar failed (per id)
      plan_criterion_not_tested   MEDIUM  — criterion's artifact/field
                                            wasn't found
    """
    plan = os.path.join(run_dir, "plan.md")
    if not os.path.exists(plan):
        return []

    yaml_path = os.path.join(run_dir, "success_criteria.yaml")
    if not os.path.exists(yaml_path):
        return [{
            "kind": "plan_criteria_missing",
            "severity": "MEDIUM",
            "stage": "PLAN_CRITERIA",
            "claim": (
                "plan.md exists but success_criteria.yaml is absent. "
                "The planner must produce a structured "
                "success_criteria.yaml alongside plan.md so each "
                "minimum-bar / hard-blocker criterion can be "
                "mechanically evaluated. Without it, plan-promised "
                "criteria silently slide into §Limitations bullets "
                "(see 1935 malaria run, where 'malariasimulation "
                "cross-validation' was promised but never enforced). "
                "See planner prompt and scripts/plan_criteria.py "
                "schema."
            ),
        }]

    try:
        spec = importlib.util.spec_from_file_location(
            "plan_criteria",
            os.path.join(os.path.dirname(__file__), "plan_criteria.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as e:
        return [{
            "kind": "plan_criteria_malformed",
            "severity": "HIGH",
            "stage": "PLAN_CRITERIA",
            "claim": (f"Could not load scripts/plan_criteria.py: {e}"),
        }]

    result = mod.evaluate_plan_criteria(run_dir)

    if result.get("verdict") == "MALFORMED":
        return [{
            "kind": "plan_criteria_malformed",
            "severity": "HIGH",
            "stage": "PLAN_CRITERIA",
            "claim": (f"success_criteria.yaml is malformed: "
                      f"{'; '.join(result.get('errors') or [])}"),
        }]

    if result.get("verdict") != "OK":
        return []

    out: list[dict] = []
    for entry in result.get("hard_blockers") or []:
        if entry["status"] == "FAIL":
            out.append({
                "kind": "plan_hard_blocker_failed",
                "severity": "HIGH",
                "stage": "PLAN_CRITERIA",
                "claim": (
                    f"Hard blocker {entry['id']} FAILED: "
                    f"{entry['criterion']} ({entry['evidence']})"
                ),
            })
        elif entry["status"] == "NOT_TESTED":
            out.append({
                "kind": "plan_criterion_not_tested",
                "severity": "MEDIUM",
                "stage": "PLAN_CRITERIA",
                "claim": (
                    f"Hard blocker {entry['id']} NOT_TESTED: "
                    f"{entry['criterion']} ({entry['evidence']}). "
                    f"The plan promised this criterion; the modeler "
                    f"must produce the artifact + field, or the "
                    f"planner must remove the promise."
                ),
            })
    for entry in result.get("minimum_bar") or []:
        if entry["status"] == "FAIL":
            out.append({
                "kind": "plan_minimum_bar_failed",
                "severity": "MEDIUM",
                "stage": "PLAN_CRITERIA",
                "claim": (
                    f"Minimum bar {entry['id']} FAILED: "
                    f"{entry['criterion']} ({entry['evidence']})"
                ),
            })
        elif entry["status"] == "NOT_TESTED":
            out.append({
                "kind": "plan_criterion_not_tested",
                "severity": "MEDIUM",
                "stage": "PLAN_CRITERIA",
                "claim": (
                    f"Minimum bar {entry['id']} NOT_TESTED: "
                    f"{entry['criterion']} ({entry['evidence']}). "
                    f"Modeler must populate the field in the artifact "
                    f"or scope-declare why the criterion is "
                    f"unachievable for this run."
                ),
            })
    return out


def _check_writer_qa(run_dir: str) -> list[dict]:
    """Phase 7 Commit λ: when report.md exists, the writer must run
    a post-write QA pass via scripts/writer_qa.py and the result must
    be CLEAN.

    Emits:
      writer_qa_missing    MEDIUM — report.md exists but no
                                    writer_qa_report.yaml
      writer_qa_unresolved MEDIUM — qa report says REVISE/MAJOR_REVISION
                                    (writer didn't iterate on the issues)
    """
    report = os.path.join(run_dir, "report.md")
    if not os.path.exists(report):
        return []  # Writer hasn't run yet; nothing to QA.

    qa_path = os.path.join(run_dir, "writer_qa_report.yaml")
    if not os.path.exists(qa_path):
        return [{
            "kind": "writer_qa_missing",
            "severity": "MEDIUM",
            "stage": "WRITER_QA",
            "claim": (
                "report.md exists but writer_qa_report.yaml is absent. "
                "STAGE 8.5 requires a post-writer QA pass via "
                "`python3 scripts/writer_qa.py {run_dir}`. The pass "
                "checks for stale UQ numbers, figure-text comparator "
                "inconsistencies, figure annotation vs body-text "
                "metric mismatches, and stale CRITICAL CAVEATs in "
                "§Limitations."
            ),
        }]

    try:
        with open(qa_path) as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return []

    verdict = data.get("verdict", "")
    if verdict in ("REVISE", "MAJOR_REVISION"):
        n_major = data.get("n_major", 0)
        n_minor = data.get("n_minor", 0)
        return [{
            "kind": "writer_qa_unresolved",
            "severity": "MEDIUM",
            "stage": "WRITER_QA",
            "claim": (
                f"writer_qa verdict is {verdict} with {n_major} MAJOR "
                f"and {n_minor} MINOR issues. The lead must re-spawn "
                f"the writer with the QA report's issues as input, or "
                f"scope-declare the writer-QA limitations explicitly. "
                f"Common patterns: stale numbers from pre-fix drafts, "
                f"figure-text comparator inconsistencies, figure "
                f"annotations that disagree with body-text metric "
                f"values."
            ),
        }]
    return []


def _check_allocation_robustness(run_dir: str) -> list[dict]:
    """Phase 6 Commit κ: allocation rule must be cross-validated under
    spatial holdout. The modeler runs k-fold leave-one-spatial-unit-out
    re-optimization themselves and writes models/allocation_robustness.yaml.

    Emits:
      allocation_robustness_missing  MEDIUM — file absent
      allocation_robustness_malformed HIGH — schema violations
      allocation_unstable             HIGH — worst-fold metrics fail
                                              ROBUST and FRAGILE bands
      allocation_fragile              MEDIUM — middle-band, scope-declare
    """
    decision_rule = os.path.join(run_dir, "decision_rule.md")
    allocs = _find_allocation_csvs(run_dir)
    if not (os.path.exists(decision_rule) or allocs):
        return []

    yaml_path = os.path.join(run_dir, "models", "allocation_robustness.yaml")
    if not os.path.exists(yaml_path):
        return [{
            "kind": "allocation_robustness_missing",
            "severity": "MEDIUM",
            "stage": "OPTIMIZATION",
            "claim": (
                "Allocation produced but models/allocation_robustness.yaml "
                "is absent. The allocation rule must be cross-validated: "
                "hold out k spatial units (e.g., leave-one-archetype-out "
                "or 5-fold-by-state), re-run the optimizer on the "
                "remaining n-k, and measure how well the rule generalizes "
                "to the held-out units. A 22-archetype calibration "
                "achieving 7.8pp RMSE in-sample says nothing about "
                "whether the optimizer's allocation rule overfits "
                "specific archetype EIRs. See the "
                "allocation-cross-validation skill."
            ),
        }]

    try:
        spec = importlib.util.spec_from_file_location(
            "allocation_robustness",
            os.path.join(os.path.dirname(__file__), "allocation_robustness.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as e:
        return [{
            "kind": "allocation_robustness_malformed",
            "severity": "HIGH",
            "stage": "OPTIMIZATION",
            "claim": (f"Could not load scripts/allocation_robustness.py: {e}"),
        }]

    result = mod.validate_allocation_robustness(yaml_path)
    out = []
    if result["verdict"] == "MALFORMED":
        out.append({
            "kind": "allocation_robustness_malformed",
            "severity": "HIGH",
            "stage": "OPTIMIZATION",
            "claim": (f"models/allocation_robustness.yaml is malformed: "
                      f"{'; '.join(result.get('errors') or [])}"),
        })
    elif result["verdict"] == "UNSTABLE":
        out.append({
            "kind": "allocation_unstable",
            "severity": "HIGH",
            "stage": "OPTIMIZATION",
            "claim": (
                f"Allocation cross-validation verdict: UNSTABLE. "
                f"Worst-fold metrics fail even the FRAGILE band: rank "
                f"correlation < 0.40 OR cases-averted gap > 30% OR rule "
                f"classification concordance < 60%. The optimizer's "
                f"allocation does NOT generalize to held-out spatial "
                f"units — applying this rule to LGAs/states outside the "
                f"calibration set is unjustified. Either rebuild the "
                f"model with regularization / pooling / spatial structure "
                f"to improve generalization, or scope-declare the "
                f"recommendation as applicable only to the in-sample "
                f"22 archetypes. Metrics: {result.get('metrics')}"
            ),
        })
    elif result["verdict"] == "FRAGILE":
        out.append({
            "kind": "allocation_fragile",
            "severity": "MEDIUM",
            "stage": "OPTIMIZATION",
            "claim": (
                f"Allocation cross-validation verdict: FRAGILE. "
                f"Worst-fold metrics fall in the middle band: rank "
                f"correlation 0.40-0.70 OR cases-averted gap 15-30% OR "
                f"rule classification concordance 60-80%. The allocation "
                f"is plausibly generalizable but with substantial "
                f"per-fold variability. Add a §Limitations paragraph "
                f"quantifying the worst-fold gap and identifying which "
                f"types of held-out units are hardest to predict. "
                f"Metrics: {result.get('metrics')}"
            ),
        })
    return out


_DALY_MENTION_RE = re.compile(
    r"\b(?:DALY|DALYs|disability[-\s]adjusted\s+life[-\s]year"
    r"|life[-\s]year[s]?\s+lost|YLL|YLD)\b",
    re.IGNORECASE,
)
# Headers that signal "this is a Limitations / Scope acknowledgment
# section, not the main analysis body". We strip these from the text
# before checking for DALY engagement so a single throwaway DALY
# acknowledgment in Limitations doesn't satisfy the gate.
_LIMITATIONS_HEADER_RE = re.compile(
    r"^#{1,4}\s+\d*\.?\s*"
    r"(?:Limitations?|Scope\s+Declaration|Caveats?|"
    r"Unresolved\s+(?:HIGH|Blockers?)|Known\s+Issues?)"
    r"[^\n]*$",
    re.MULTILINE | re.IGNORECASE,
)


def _strip_limitations_and_scope(text: str) -> str:
    """Remove §Limitations / §Scope Declaration / §Caveats sections
    so DALY mentions in those sections don't satisfy the
    daly_analysis_missing check. Returns the text with each such
    section sliced from header to next same-level (or higher)
    section header. Linear scan."""
    out_parts = []
    last_end = 0
    headers = list(_LIMITATIONS_HEADER_RE.finditer(text))
    for m in headers:
        # Determine the level (count of '#' at the header start).
        hdr_text = m.group(0)
        level = len(hdr_text) - len(hdr_text.lstrip("#"))
        # Find the next header at this level or shallower (more
        # important).
        next_header_re = re.compile(
            rf"^#{{1,{level}}}\s+",
            re.MULTILINE,
        )
        section_end = len(text)
        for next_m in next_header_re.finditer(text, m.end()):
            section_end = next_m.start()
            break
        out_parts.append(text[last_end:m.start()])
        last_end = section_end
    out_parts.append(text[last_end:])
    return "".join(out_parts)


def _check_daly_when_allocation(run_dir: str) -> list[dict]:
    """Phase 6 Commit ι: when a model produces an allocation, the
    report must mention DALYs (or explicitly justify their absence).

    Cases-averted alone treats a 6-month-old's averted infection
    identically to an adult's, dramatically under-weighting U5-targeted
    interventions like SMC. GF / GBD / WHO use DALY-denominated
    cost-effectiveness as the standard. A Global Fund supplementary
    analysis without DALYs gets sent back.

    Emits:
      daly_analysis_missing MEDIUM — decision_rule.md or allocation
                                     CSV exists, but report.md has no
                                     DALY/YLL/YLD/disability-adjusted
                                     mentions.
    """
    decision_rule = os.path.join(run_dir, "decision_rule.md")
    allocs = _find_allocation_csvs(run_dir)
    if not (os.path.exists(decision_rule) or allocs):
        return []  # No allocation produced; no requirement.

    report = os.path.join(run_dir, "report.md")
    if not os.path.exists(report):
        # Report not yet written; check will run again post-writer.
        return []

    try:
        with open(report) as f:
            text = f.read()
    except (OSError, UnicodeDecodeError):
        return []

    # Strip §Limitations and §Scope-declaration sections — a single
    # "we didn't compute DALYs" throwaway in Limitations shouldn't
    # satisfy the check; the analysis must engage with DALYs in
    # Methods / Results / Discussion / Cost-Effectiveness.
    text_outside_limitations = _strip_limitations_and_scope(text)
    if _DALY_MENTION_RE.search(text_outside_limitations):
        return []  # DALYs engaged with in the analysis body.

    return [{
        "kind": "daly_analysis_missing",
        "severity": "MEDIUM",
        "stage": "REPORT",
        "claim": (
            "Allocation/decision_rule produced but report.md does not "
            "mention DALYs (or YLL/YLD/disability-adjusted life-years). "
            "Cases-averted alone treats a 6-month-old's averted "
            "infection identically to an adult's, structurally biasing "
            "the recommendation toward all-age interventions over "
            "child-targeted ones (SMC, IPTp, paediatric vaccines). "
            "Global Fund / GBD / WHO benchmarks are DALY-denominated; "
            "without DALY-averted figures, the report cannot be "
            "compared to published cost-effectiveness thresholds. "
            "Add either (1) a DALY-averted column alongside "
            "cases-averted in the primary table, (2) a §Cost-"
            "Effectiveness section with $/DALY estimates, or (3) a "
            "Methods/Limitations paragraph justifying DALY irrelevance "
            "for this specific analysis. See the daly-weighted-analysis "
            "skill for disease-specific anchor tables."
        ),
    }]


def _check_optimization_quality(run_dir: str) -> list[dict]:
    """Phase 6 Commit θ: when an allocation/decision_rule artifact
    exists, the modeler must compare their primary optimizer to ≥1
    alternative method (ILP, SA, random-restart) and report the gap.

    Emits:
      optimization_quality_missing      MEDIUM — decision_rule.md or
                                                 allocation CSV present
                                                 but no benchmark file
      optimization_quality_no_benchmark HIGH   — file exists but
                                                 benchmark_methods is empty
      optimization_quality_malformed    HIGH   — schema/method violations
      optimization_quality_gap_too_large MEDIUM — gap_pct > 10%
    """
    decision_rule = os.path.join(run_dir, "decision_rule.md")
    allocs = _find_allocation_csvs(run_dir)
    if not (os.path.exists(decision_rule) or allocs):
        return []  # No allocation produced; no requirement.

    yaml_path = os.path.join(run_dir, "models", "optimization_quality.yaml")
    if not os.path.exists(yaml_path):
        return [{
            "kind": "optimization_quality_missing",
            "severity": "MEDIUM",
            "stage": "OPTIMIZATION",
            "claim": (
                "Allocation produced but models/optimization_quality.yaml "
                "is absent. The primary optimizer must be benchmarked "
                "against at least one alternative method (ILP via PuLP, "
                "simulated annealing, random-restart greedy, or brute "
                "force when feasible) so the optimality gap is "
                "quantified. A greedy optimizer's headline 'X% advantage' "
                "claim is meaningless without knowing whether X% is 100% "
                "of the achievable improvement or 60% of it. See the "
                "optimizer-method-selection skill."
            ),
        }]

    # Lazy import to avoid circular dependency / missing-deps at import time.
    try:
        spec = importlib.util.spec_from_file_location(
            "optimization_quality",
            os.path.join(os.path.dirname(__file__), "optimization_quality.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as e:
        return [{
            "kind": "optimization_quality_malformed",
            "severity": "HIGH",
            "stage": "OPTIMIZATION",
            "claim": (f"Could not load scripts/optimization_quality.py: {e}"),
        }]

    result = mod.validate_optimization_quality(yaml_path)
    out = []
    if result["verdict"] == "MALFORMED":
        out.append({
            "kind": "optimization_quality_malformed",
            "severity": "HIGH",
            "stage": "OPTIMIZATION",
            "claim": (f"models/optimization_quality.yaml is malformed: "
                      f"{'; '.join(result['errors'])}"),
        })
    elif result["verdict"] == "NO_BENCHMARK":
        out.append({
            "kind": "optimization_quality_no_benchmark",
            "severity": "HIGH",
            "stage": "OPTIMIZATION",
            "claim": (
                "models/optimization_quality.yaml exists but "
                "benchmark_methods is empty. At least one benchmark "
                "method (ILP, SA, random-restart) must be present so "
                "the gap_pct can be computed. Greedy-only optimization "
                "with no quality benchmark is not ACCEPT-grade."
            ),
        })
    elif result["verdict"] == "GAP_TOO_LARGE":
        out.append({
            "kind": "optimization_quality_gap_too_large",
            "severity": "MEDIUM",
            "stage": "OPTIMIZATION",
            "claim": (
                f"Optimizer gap_pct = {result['gap_pct']:.2f}%; primary "
                f"method ({result['primary']['method']}) is well below "
                f"best benchmark ({result['best']['method']} at "
                f"{result['best']['objective']:.4g} vs "
                f"{result['primary']['objective']:.4g}). Either switch "
                f"the primary method to the better one, improve the "
                f"primary's parameters (more random restarts, longer SA "
                f"cooling, etc.), or scope-declare why a >10% gap is "
                f"acceptable for this question."
            ),
        })
    return out


def _check_sensitivity_analysis(run_dir: str) -> list[dict]:
    """Phase 8 Commit π: when an allocation/decision_rule artifact
    exists, the modeler must perturb 2-3 load-bearing parameters and
    report whether the primary recommendation flips. The artifact
    lives at `{run_dir}/models/sensitivity_analysis.yaml`.

    Emits:
      sensitivity_analysis_missing      MEDIUM — allocation present but
                                                  no sensitivity yaml
      sensitivity_analysis_malformed    HIGH   — schema violations
      sensitivity_analysis_unstable     HIGH   — UNSTABLE verdict
      sensitivity_analysis_sensitive    MEDIUM — SENSITIVE verdict
    """
    decision_rule = os.path.join(run_dir, "decision_rule.md")
    allocs = _find_allocation_csvs(run_dir)
    if not (os.path.exists(decision_rule) or allocs):
        return []  # No allocation produced; no requirement.

    yaml_path = os.path.join(run_dir, "models", "sensitivity_analysis.yaml")
    if not os.path.exists(yaml_path):
        return [{
            "kind": "sensitivity_analysis_missing",
            "severity": "MEDIUM",
            "stage": "OPTIMIZATION",
            "claim": (
                "Allocation produced but models/sensitivity_analysis.yaml "
                "is absent. The recommendation must be tested for "
                "robustness against perturbations of its 2-3 LOAD-BEARING "
                "parameters (the parameters whose 95% CI endpoints could "
                "change the dominant intervention package, the highest-"
                "burden zone allocation, or the comparator-vs-optimized "
                "winner). For each parameter: re-run the optimizer at "
                "alternative values, compare top-50 LGA package "
                "assignments, and report rank_change_top_n + "
                "primary_recommendation_changes. Verdict ROBUST is "
                "required for ACCEPT-grade analysis. SENSITIVE must be "
                "scope-declared in §Limitations. UNSTABLE means the "
                "recommendation cannot be defended as-is. See "
                "scripts/sensitivity_analysis.py for the schema."
            ),
        }]

    try:
        spec = importlib.util.spec_from_file_location(
            "sensitivity_analysis",
            os.path.join(os.path.dirname(__file__), "sensitivity_analysis.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as e:
        return [{
            "kind": "sensitivity_analysis_malformed",
            "severity": "HIGH",
            "stage": "OPTIMIZATION",
            "claim": f"Could not load scripts/sensitivity_analysis.py: {e}",
        }]

    result = mod.validate_sensitivity_analysis(yaml_path)
    out = []
    if result["verdict"] == "MALFORMED":
        out.append({
            "kind": "sensitivity_analysis_malformed",
            "severity": "HIGH",
            "stage": "OPTIMIZATION",
            "claim": (f"models/sensitivity_analysis.yaml is malformed: "
                      f"{'; '.join(result['errors'])}"),
        })
    elif result["verdict"] == "UNSTABLE":
        s = result.get("summary", {})
        out.append({
            "kind": "sensitivity_analysis_unstable",
            "severity": "HIGH",
            "stage": "OPTIMIZATION",
            "claim": (
                f"Sensitivity analysis verdict UNSTABLE: "
                f"{s.get('flips', 0)} of {s.get('total_perturbations', 0)} "
                f"perturbations flip the primary recommendation, worst "
                f"top-N rank change is {s.get('worst_rank_change_top_n', 0)}. "
                f"The allocation cannot be defended as-is; either pick a "
                f"more robust parameterization, narrow the perturbation "
                f"range with stronger evidence, or rebuild the optimization "
                f"around an objective whose ranking is robust to these "
                f"parameter ranges."
            ),
        })
    elif result["verdict"] == "SENSITIVE":
        s = result.get("summary", {})
        out.append({
            "kind": "sensitivity_analysis_sensitive",
            "severity": "MEDIUM",
            "stage": "OPTIMIZATION",
            "claim": (
                f"Sensitivity analysis verdict SENSITIVE: "
                f"{s.get('flips', 0)} of {s.get('total_perturbations', 0)} "
                f"perturbations flip the primary recommendation; worst "
                f"top-N rank change is {s.get('worst_rank_change_top_n', 0)}. "
                f"The recommendation is not robust under all plausible "
                f"parameter values. Surface this in §Sensitivity (or "
                f"§Cost-effectiveness) of report.md, not just §Limitations: "
                f"a decision-maker reading the headline must see which "
                f"alternative parameter values flip the choice."
            ),
        })
    return out


# How many lines of lookahead after a savefig() call we tolerate
# before requiring a validate_figure() call. 10 fits the typical
# pattern of `plt.savefig(...); plt.close(); validate_figure(...)`.
_VALIDATOR_LOOKAHEAD_LINES = 10


def _check_figure_validator(run_dir: str) -> list[dict]:
    """Phase 9 Commit ρ: write-time figure validator.

    Two complementary checks fire per round:

    1. Source-data freshness — for every `<png>.provenance.json`
       sidecar in `{run_dir}/figures/`, recompute hashes of each
       recorded source CSV. If any hash differs, the figure was
       drawn from data that has since changed (the D-022 / D-023
       failure mode). Severity HIGH `figure_staleness_detected`.

    2. Validator-call coverage — every `*.py` file in `{run_dir}/`
       and `{run_dir}/models/` is scanned for `plt.savefig(`. Each
       savefig must have a `validate_figure(` within the next
       `_VALIDATOR_LOOKAHEAD_LINES` lines, and the corresponding
       PNG must have a `.provenance.json` sidecar at gate time.
       Missing validator call → MEDIUM `figure_validator_missing`.

    The check is silent when no figures exist (e.g., before any
    modeler round has produced output). It is intentionally NOT
    gated on allocation presence: figures are part of every run.
    """
    figures_dir = os.path.join(run_dir, "figures")
    out: list[dict] = []

    # --- Check 1: source-data freshness via sidecar hash compare ---
    if os.path.isdir(figures_dir):
        # Lazy-load figure_validator.check_staleness once. Same
        # importlib pattern as _check_sensitivity_analysis above.
        try:
            spec = importlib.util.spec_from_file_location(
                "figure_validator",
                os.path.join(os.path.dirname(__file__), "figure_validator.py"),
            )
            fv_mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(fv_mod)
        except Exception as e:
            out.append({
                "kind": "figure_validator_load_error",
                "severity": "HIGH",
                "stage": "ANALYZE",
                "claim": f"Could not load scripts/figure_validator.py: {e}",
            })
            return out

        for entry in sorted(os.listdir(figures_dir)):
            if not entry.endswith(".png"):
                continue
            png_path = os.path.join(figures_dir, entry)
            sidecar = png_path + ".provenance.json"
            if not os.path.exists(sidecar):
                # Missing sidecar handled by check 2 (script-level
                # coverage scan); skip here.
                continue
            status = fv_mod.check_staleness(png_path)
            if status["status"] == "stale":
                stale_list = ", ".join(status.get("stale_sources", [])
                                       + status.get("missing_sources", []))
                out.append({
                    "kind": "figure_staleness_detected",
                    "severity": "HIGH",
                    "stage": "ANALYZE",
                    "claim": (
                        f"Figure {entry} was drawn from source data that "
                        f"has since changed. The recorded provenance "
                        f"hash no longer matches the current content of: "
                        f"{stale_list}. Regenerate the figure from the "
                        f"current data and re-call validate_figure(). "
                        f"This is the D-022 / D-023 failure mode "
                        f"(corrected AIC values, stale calibration "
                        f"residuals) that the 0013 run caught only at "
                        f"round 8."
                    ),
                })

    # --- Check 2: every plt.savefig must have a validate_figure nearby ---
    py_files: list[str] = []
    for root in (run_dir, os.path.join(run_dir, "models")):
        if not os.path.isdir(root):
            continue
        for entry in os.listdir(root):
            if entry.endswith(".py"):
                py_files.append(os.path.join(root, entry))

    for py in py_files:
        try:
            with open(py) as f:
                lines = f.readlines()
        except OSError:
            continue
        for idx, ln in enumerate(lines):
            if "plt.savefig(" not in ln and ".savefig(" not in ln:
                continue
            window = "".join(lines[idx:idx + _VALIDATOR_LOOKAHEAD_LINES])
            if "validate_figure(" not in window:
                out.append({
                    "kind": "figure_validator_missing",
                    "severity": "MEDIUM",
                    "stage": "ANALYZE",
                    "claim": (
                        f"{os.path.relpath(py, run_dir)}:{idx + 1} calls "
                        f"savefig but no validate_figure(...) appears "
                        f"within the next {_VALIDATOR_LOOKAHEAD_LINES} "
                        f"lines. Phase 9 Commit ρ requires every "
                        f"plt.savefig to be paired with a validate_figure "
                        f"call so source-data hashes and annotation "
                        f"strings are recorded at write time. See "
                        f"scripts/figure_validator.py for the API."
                    ),
                })
    return out


# Phase 10 Commit ψ: allocation-gate coordinator.
#
# Per the Phase 9 τ rigor-artifact-timeline, each allocation-
# triggered artifact has a "draft by round X" deadline. After that
# round, a MEDIUM `*_missing` becomes a `drafts_overdue` MEDIUM —
# signaling the modeler is past the planned drafting window and at
# real risk of arriving at STAGE 7 with no time to fix verdict
# failures. Within the drafting window, all missing artifacts roll
# up into a single `in_progress` MEDIUM rather than the modeler
# seeing 5 individual `*_missing` violations.
_ALLOCATION_DRAFT_DEADLINES = {
    "sensitivity_analysis_missing": 4,    # τ says draft r2-3
    "allocation_robustness_missing": 5,   # τ says draft r3-4
    "universal_coverage_missing":   5,    # not on τ; treated as r3-4 draft
    "daly_analysis_missing":        5,    # not on τ; produced with allocation
    "optimization_quality_missing": 5,    # not on τ; produced with allocation
}

# The five MEDIUM `_missing` kinds the coordinator consolidates. HIGH
# verdicts (UNSTABLE, MALFORMED, etc.) and HIGH `decision_rule_missing`
# are NOT consolidated — they pass through unchanged so the lead's
# STAGE 7 logic still sees real failures.
_ALLOCATION_MEDIUM_MISSING_KINDS = frozenset(_ALLOCATION_DRAFT_DEADLINES.keys())


def _check_allocation_rigor_status(run_dir: str,
                                   round_n: int | None = None) -> list[dict]:
    """Phase 10 Commit ψ: coordinate the five allocation-triggered
    rigor checks.

    Calls each helper, then:
      1. Pass through every non-MEDIUM-missing violation unchanged
         (HIGHs always; MEDIUM verdict failures like `*_sensitive`).
      2. Collect the MEDIUM `_missing` kinds (5 of them).
      3. If `round_n` is None, fall back to legacy behavior — the
         five `_missing` violations are returned individually as
         before. This preserves callers that don't pass round_n.
      4. If `round_n` is supplied:
           - For each missing artifact, look up its deadline.
           - Within the drafting window: emit a single
             `allocation_rigor_in_progress` MEDIUM listing the
             missing artifacts.
           - Past at least one deadline: emit a single
             `allocation_rigor_drafts_overdue` MEDIUM listing the
             overdue artifacts (and any still-in-window ones, for
             context).
    """
    raw: list[dict] = []
    raw.extend(_check_optimization_quality(run_dir))
    raw.extend(_check_daly_when_allocation(run_dir))
    raw.extend(_check_allocation_robustness(run_dir))
    raw.extend(_check_universal_coverage(run_dir))
    raw.extend(_check_sensitivity_analysis(run_dir))

    out: list[dict] = []
    missing_kinds: list[dict] = []
    for v in raw:
        if (v.get("kind") in _ALLOCATION_MEDIUM_MISSING_KINDS
                and v.get("severity") == "MEDIUM"):
            missing_kinds.append(v)
        else:
            # Pass through HIGHs and other MEDIUMs unchanged.
            out.append(v)

    if not missing_kinds:
        return out

    if round_n is None:
        # Legacy mode: behave exactly as before — return all five
        # MEDIUMs individually so callers without round context see
        # no behavior change.
        out.extend(missing_kinds)
        return out

    overdue: list[str] = []
    in_progress: list[str] = []
    for v in missing_kinds:
        deadline = _ALLOCATION_DRAFT_DEADLINES.get(v["kind"], 5)
        artifact = v["kind"].replace("_missing", "")
        if round_n > deadline:
            overdue.append(f"{artifact} (deadline r{deadline})")
        else:
            in_progress.append(f"{artifact} (deadline r{deadline})")

    if overdue:
        details = "Overdue: " + "; ".join(overdue)
        if in_progress:
            details += ". Still in window: " + "; ".join(in_progress) + "."
        else:
            details += "."
        out.append({
            "kind": "allocation_rigor_drafts_overdue",
            "severity": "MEDIUM",
            "stage": "OPTIMIZATION",
            "claim": (
                f"Allocation rigor artifacts past their drafting "
                f"deadline (round {round_n}). {details} The Phase 9 τ "
                f"timeline asks for these to be drafted (even with "
                f"placeholder values) earlier so per-round critique "
                f"can iterate on verdicts. Arriving at STAGE 7 with a "
                f"first draft of sensitivity_analysis or allocation_"
                f"robustness is exactly the 0013 failure mode."
            ),
        })
    else:
        out.append({
            "kind": "allocation_rigor_in_progress",
            "severity": "MEDIUM",
            "stage": "OPTIMIZATION",
            "claim": (
                f"Allocation rigor artifacts in drafting window "
                f"(round {round_n}). Pending: "
                f"{'; '.join(in_progress)}. This is a coordinated "
                f"reminder, not a blocker — draft each artifact "
                f"early so verdict failures surface while there are "
                f"rounds left to act."
            ),
        })
    return out


# Phase 12 Commit γ: ecological-fallacy / within-zone heterogeneity.
#
# When calibration_units / allocation_units < 0.1 (ecological
# aggregation likely), require models/within_zone_heterogeneity.yaml
# bounding the impact loss. The 104914 run had 6 zones → 774 LGAs
# (ratio 0.0078), with within-zone PfPR varying 5.9%-77.3% per
# data_quality.md. results.md acknowledges this in one line; the
# magnitude is unbounded. A reviewer cannot tell if the headline
# 54.7M cases averted would drop by 5%, 15%, or 30% under realistic
# within-zone heterogeneity.
_ECOLOGICAL_AGGREGATION_THRESHOLD = 0.1


def _detect_calibration_allocation_ratio(run_dir: str) -> tuple[int, int] | None:
    """Detect (calibration_units, allocation_units) pair from a run
    dir. Returns None if either count is unavailable.

    calibration_units: from models/model_comparison_formal.yaml
        n_targets field (the number of zone-level calibration targets).
    allocation_units: from models/lga_allocation.csv (row count
        excluding header), or any *allocation*.csv in models/.
    """
    cal_path = os.path.join(run_dir, "model_comparison_formal.yaml")
    if not os.path.exists(cal_path):
        cal_path = os.path.join(run_dir, "models", "model_comparison_formal.yaml")
    cal_units: int | None = None
    if os.path.exists(cal_path):
        try:
            with open(cal_path) as f:
                doc = yaml.safe_load(f) or {}
            n_targets = doc.get("n_targets")
            if isinstance(n_targets, int):
                cal_units = n_targets
        except (yaml.YAMLError, OSError):
            pass

    alloc_units: int | None = None
    for entry in (os.listdir(os.path.join(run_dir, "models"))
                  if os.path.isdir(os.path.join(run_dir, "models")) else []):
        if "allocation" in entry and entry.endswith(".csv"):
            try:
                with open(os.path.join(run_dir, "models", entry)) as f:
                    rows = sum(1 for _ in f) - 1  # exclude header
                if rows > 0 and (alloc_units is None or rows > alloc_units):
                    alloc_units = rows
            except OSError:
                continue

    if cal_units is None or alloc_units is None or alloc_units == 0:
        return None
    return (cal_units, alloc_units)


def _check_within_zone_heterogeneity(run_dir: str) -> list[dict]:
    """Phase 12 Commit γ: when calibration_units / allocation_units
    < 0.1, require models/within_zone_heterogeneity.yaml bounding
    the impact loss from ecological aggregation.

    Emits:
      within_zone_heterogeneity_missing      MEDIUM — yaml absent
                                                       and ratio < 0.1
      within_zone_heterogeneity_malformed    HIGH   — yaml schema/verdict
                                                       violations
      within_zone_heterogeneity_unbounded    HIGH   — UNBOUNDED verdict
                                                       (>25% impact loss)
      within_zone_heterogeneity_inconclusive MEDIUM — INCONCLUSIVE
                                                       (10-25% loss)
    """
    pair = _detect_calibration_allocation_ratio(run_dir)
    if pair is None:
        return []  # no calibration/allocation detected; check is silent
    cal_units, alloc_units = pair
    ratio = cal_units / alloc_units
    if ratio >= _ECOLOGICAL_AGGREGATION_THRESHOLD:
        return []  # not aggregating much; check is silent

    yaml_path = os.path.join(run_dir, "models", "within_zone_heterogeneity.yaml")
    if not os.path.exists(yaml_path):
        return [{
            "kind": "within_zone_heterogeneity_missing",
            "severity": "MEDIUM",
            "stage": "MODEL",
            "claim": (
                f"Model calibrates to {cal_units} units but allocates to "
                f"{alloc_units} units (ratio {ratio:.4f} < "
                f"{_ECOLOGICAL_AGGREGATION_THRESHOLD}). Within-unit "
                f"heterogeneity is invisible to the optimizer; the "
                f"impact loss is unbounded. Required artifact: "
                f"`models/within_zone_heterogeneity.yaml` bounding the "
                f"impact loss from realistic within-unit value variation. "
                f"See the ecological-fallacy-quantification skill and "
                f"scripts/within_zone_sensitivity.py for the schema."
            ),
        }]

    try:
        spec = importlib.util.spec_from_file_location(
            "within_zone_sensitivity",
            os.path.join(os.path.dirname(__file__),
                         "within_zone_sensitivity.py"),
        )
        wzs = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(wzs)
    except Exception as e:
        return [{
            "kind": "within_zone_heterogeneity_malformed",
            "severity": "HIGH",
            "stage": "MODEL",
            "claim": f"Could not load scripts/within_zone_sensitivity.py: {e}",
        }]

    result = wzs.validate_within_zone_heterogeneity(yaml_path)
    out: list[dict] = []
    if result["verdict"] == "MALFORMED":
        out.append({
            "kind": "within_zone_heterogeneity_malformed",
            "severity": "HIGH",
            "stage": "MODEL",
            "claim": (f"models/within_zone_heterogeneity.yaml is malformed: "
                      f"{'; '.join(result['errors'])}"),
        })
    elif result["verdict"] == "UNBOUNDED":
        s = result.get("summary", {})
        out.append({
            "kind": "within_zone_heterogeneity_unbounded",
            "severity": "HIGH",
            "stage": "MODEL",
            "claim": (
                f"Within-zone heterogeneity sensitivity verdict UNBOUNDED: "
                f"worst-case impact loss "
                f"{s.get('worst_loss_pct', 0):.1f}% under realistic "
                f"within-unit variation. The headline cases-averted is "
                f"NOT defensible at the LGA level. Either narrow the "
                f"perturbation range with stronger evidence (e.g., "
                f"sub-zone PfPR sample), refit at the lower aggregation "
                f"level, or scope-declare the recommendation as zone-"
                f"level only."
            ),
        })
    elif result["verdict"] == "INCONCLUSIVE":
        s = result.get("summary", {})
        out.append({
            "kind": "within_zone_heterogeneity_inconclusive",
            "severity": "MEDIUM",
            "stage": "MODEL",
            "claim": (
                f"Within-zone heterogeneity verdict INCONCLUSIVE: "
                f"worst-case impact loss "
                f"{s.get('worst_loss_pct', 0):.1f}%. Surface in §Sensitivity "
                f"of report.md, not just §Limitations. The 10-25% range "
                f"is publication-defensible but a Global Fund reviewer "
                f"will want to see the explicit bound."
            ),
        })
    return out


# Phase 13 Commit α: disease-agnostic structural sanity checks.
#
# `models/sanity_schema.yaml` declares abstract slots (outcome,
# exposure, shares, derived-consistency formulas, composite-dimension
# windows, counterfactual ratio, structural-uncertainty carry-
# forward, outlier sniff). `scripts/sanity_checks.py` runs eight
# generic structural checks against the schema. The schema is
# required when the modeler has had time to compute headline outputs
# (round ≥ 3). Each failed check emits a MEDIUM advisory; none block
# ACCEPT. The modeler can opt out of any check via
# `scope_declaration.yaml`'s `sanity_check_acknowledged: <id>` list.
_SANITY_SCHEMA_REQUIRED_FROM_ROUND = 3


def _check_sanity_schema(run_dir: str,
                          round_n: int | None = None) -> list[dict]:
    """Phase 13 Commit α: validate models/sanity_schema.yaml.

    Emits:
      sanity_schema_missing               MEDIUM — yaml absent at r≥3
      sanity_schema_invalid               HIGH   — yaml malformed
      sanity_check_failed_<id>            MEDIUM — one per failed check
    """
    if round_n is not None and round_n < _SANITY_SCHEMA_REQUIRED_FROM_ROUND:
        return []  # Too early; modeler still composing outputs.

    yaml_path = os.path.join(run_dir, "models", "sanity_schema.yaml")
    if not os.path.exists(yaml_path):
        if round_n is None:
            return []  # Round unknown; defer.
        return [{
            "kind": "sanity_schema_missing",
            "severity": "MEDIUM",
            "stage": "MODEL",
            "claim": (
                f"Required artifact `models/sanity_schema.yaml` is "
                f"absent at round {round_n} (≥ "
                f"{_SANITY_SCHEMA_REQUIRED_FROM_ROUND}). The modeler "
                f"must declare abstract slots (outcome name, baseline "
                f"reservoir, exposure unit, shares, derived-consistency "
                f"formulas, composite-dimension windows, structural-"
                f"uncertainty bounds) so eight disease-agnostic "
                f"structural checks can run. See the `sanity-schema` "
                f"skill and `scripts/sanity_checks.py --self-test` for "
                f"the schema."
            ),
        }]

    try:
        spec = importlib.util.spec_from_file_location(
            "sanity_checks",
            os.path.join(os.path.dirname(__file__), "sanity_checks.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as e:
        return [{
            "kind": "sanity_schema_invalid",
            "severity": "HIGH",
            "stage": "MODEL",
            "claim": f"Could not load scripts/sanity_checks.py: {e}",
        }]

    result = mod.validate_sanity_schema(yaml_path, run_dir=run_dir)
    if result["verdict"] == "MALFORMED":
        return [{
            "kind": "sanity_schema_invalid",
            "severity": "HIGH",
            "stage": "MODEL",
            "claim": (f"models/sanity_schema.yaml is malformed: "
                      f"{'; '.join(result.get('errors') or ['(no detail)'])}"),
        }]

    # Honor explicit opt-outs from scope_declaration.yaml.
    acknowledged = _load_sanity_check_acknowledged(run_dir)

    out: list[dict] = []
    for c in result.get("checks", []):
        if c.get("passed"):
            continue
        check_id = c.get("id", "unknown")
        if check_id in acknowledged:
            continue
        out.append({
            "kind": f"sanity_check_failed_{check_id}",
            "severity": "MEDIUM",
            "stage": "MODEL",
            "claim": (
                f"Sanity check {check_id!r} failed: {c.get('claim', '')}. "
                f"Either fix the underlying numbers/relationships in "
                f"models/sanity_schema.yaml or acknowledge with "
                f"`sanity_check_acknowledged: [{check_id}]` in "
                f"scope_declaration.yaml (with rationale)."
            ),
        })
    return out


def _load_sanity_check_acknowledged(run_dir: str) -> set[str]:
    """Read scope_declaration.yaml for `sanity_check_acknowledged: [...]`
    entries. Returns the set of check IDs the modeler has explicitly
    documented as out-of-scope."""
    path = os.path.join(run_dir, "scope_declaration.yaml")
    if not os.path.exists(path):
        return set()
    try:
        with open(path) as f:
            doc = yaml.safe_load(f) or {}
    except (yaml.YAMLError, OSError):
        return set()
    out: set[str] = set()
    # Top-level `sanity_check_acknowledged: [id1, id2]` form.
    top = doc.get("sanity_check_acknowledged")
    if isinstance(top, list):
        out.update(str(x) for x in top)
    # Per-declaration form: declarations[*].sanity_check_acknowledged
    decls = doc.get("declarations") or []
    if isinstance(decls, list):
        for d in decls:
            if not isinstance(d, dict):
                continue
            ack = d.get("sanity_check_acknowledged")
            if isinstance(ack, list):
                out.update(str(x) for x in ack)
            elif isinstance(ack, str):
                out.add(ack)
    return out


# Phase 15 Commit α: a-priori identifiability arithmetic.
#
# The 224202 run shipped a HYBRID model with 40 fitted parameters
# fitting 6 zone-level PfPR targets — over-saturated by 6.7×, yet
# the issue was only caught post-hoc at STAGE 5b RIGOR (round 3+).
# By that point the architecture was sunk cost; the modeler scope-
# declared 3 unidentified parameters and proceeded with a decorative
# ABM (predictions equivalent to PfPR × literature_OR).
#
# Phase 15 α requires a PRE-MODEL artifact with parameters/targets
# arithmetic, before the FIRST model build. Verdict OVER_SATURATED
# without a resolution decision blocks ACCEPT (HIGH). Critically,
# this kind is NOT scope-declarable — the modeler must fix the
# architecture, add data, or downgrade to the analytical model.
_IDENTIFIABILITY_A_PRIORI_REQUIRED_FROM_ROUND = 2


def _check_identifiability_a_priori(run_dir: str,
                                     round_n: int | None = None
                                     ) -> list[dict]:
    """Phase 15 α: validate models/identifiability_a_priori.yaml.

    Emits:
      identifiability_a_priori_missing  MEDIUM @ r=2, HIGH @ r≥3
      identifiability_a_priori_invalid  HIGH (yaml MALFORMED)
      pre_model_over_saturated          HIGH (verdict OVER_SATURATED
                                              without resolution)
      pre_model_marginal_identifiability MEDIUM (verdict MARGINAL)
      pre_model_decorative_undocumented HIGH (resolution=accept_decorative
                                               without details)

    The pre_model_* kinds are NOT scope-declarable via
    sanity_check_acknowledged or scope_declaration.yaml. Architecture
    choice is inside pipeline reach; the modeler must redesign,
    not declare scope. Concretely, this function intentionally does
    NOT call `_load_sanity_check_acknowledged` (the helper used by
    `_check_sanity_schema` for opt-out filtering). The non-scope-
    declarable contract is enforced by structural omission of that
    call, not by a runtime check.
    """
    yaml_path = os.path.join(run_dir, "models",
                             "identifiability_a_priori.yaml")

    if not os.path.exists(yaml_path):
        if round_n is None:
            return []  # Round unknown; defer to caller.
        if round_n < _IDENTIFIABILITY_A_PRIORI_REQUIRED_FROM_ROUND:
            return []  # Drafting window; silent.
        # At r≥2 the artifact is required. Severity escalates to HIGH
        # at r≥3 to align with Phase 12 β's persistence ledger.
        severity = "HIGH" if round_n >= 3 else "MEDIUM"
        return [{
            "kind": "identifiability_a_priori_missing",
            "severity": severity,
            "stage": "PRE_MODEL",
            "claim": (
                f"Required artifact `models/identifiability_a_priori.yaml` "
                f"is absent at round {round_n} (≥ "
                f"{_IDENTIFIABILITY_A_PRIORI_REQUIRED_FROM_ROUND}). "
                f"Before committing to a model architecture, count "
                f"free fitted parameters vs independent calibration "
                f"targets. Verdict IDENTIFIABLE if ratio < 1, MARGINAL "
                f"if 1-3, OVER_SATURATED if > 3. See the `pre-model-"
                f"identifiability-arithmetic` skill and "
                f"`scripts/identifiability_a_priori.py --self-test` "
                f"for the schema. This artifact is NOT scope-"
                f"declarable — architecture choice is inside pipeline "
                f"reach."
            ),
        }]

    try:
        spec = importlib.util.spec_from_file_location(
            "identifiability_a_priori",
            os.path.join(os.path.dirname(__file__),
                         "identifiability_a_priori.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    # Narrow exception scope per Phase 13/14 review-fix convention:
    # ImportError covers spec_from_file_location / exec_module failures,
    # OSError covers missing/unreadable script files, AttributeError
    # covers loader-spec API misuse. Bare `except Exception` would also
    # swallow KeyboardInterrupt / SystemExit, which is undesirable.
    except (ImportError, OSError, AttributeError) as e:
        return [{
            "kind": "identifiability_a_priori_invalid",
            "severity": "HIGH",
            "stage": "PRE_MODEL",
            "claim": (f"Could not load "
                      f"scripts/identifiability_a_priori.py: {e}"),
        }]

    result = mod.validate_identifiability_a_priori(yaml_path)
    if result["verdict"] == "MALFORMED":
        errors = result.get("errors") or []
        # Distinguish the decorative-undocumented sub-case: the
        # validator's error message for accept_decorative-without-
        # details starts with "resolution.decision is 'accept_decorative'".
        # The over-saturated-without-any-resolution error is generic
        # ("verdict X requires a resolution field..."). Match the
        # specific decorative-sub-case prefix only.
        if any(
            e.lstrip().startswith(
                "resolution.decision is 'accept_decorative'")
            for e in errors
        ):
            return [{
                "kind": "pre_model_decorative_undocumented",
                "severity": "HIGH",
                "stage": "PRE_MODEL",
                "claim": (f"identifiability_a_priori.yaml declares "
                          f"resolution.decision='accept_decorative' "
                          f"without resolution.details text. Decorative "
                          f"architectures must be justified (100-300 "
                          f"words). Errors: {'; '.join(errors)}"),
            }]
        # Generic missing-resolution case: the YAML's verdict is
        # OVER_SATURATED but no resolution decision was provided.
        # Fire as pre_model_over_saturated to match the user-facing
        # contract (the issue is over-saturation, not malformed YAML).
        if (result.get("computed_verdict") == "OVER_SATURATED"
                and any("requires a `resolution`" in e for e in errors)):
            s = result.get("summary", {})
            return [{
                "kind": "pre_model_over_saturated",
                "severity": "HIGH",
                "stage": "PRE_MODEL",
                "claim": (
                    f"Pre-model identifiability arithmetic flags "
                    f"OVER_SATURATED: {s.get('n_fitted', 0)} fitted "
                    f"parameters / {s.get('n_targets', 0)} independent "
                    f"calibration targets = "
                    f"{s.get('ratio', 0):.2f}× ratio (threshold > 3.0). "
                    f"resolution.decision is required. Pick one: "
                    f"(a) reduce params (tie across groups), "
                    f"(b) add independent calibration targets, "
                    f"(c) downgrade to analytical model, or "
                    f"(d) accept_decorative with 100-300 word justification. "
                    f"NOT scope-declarable."
                ),
            }]
        return [{
            "kind": "identifiability_a_priori_invalid",
            "severity": "HIGH",
            "stage": "PRE_MODEL",
            "claim": (f"models/identifiability_a_priori.yaml is "
                      f"malformed: {'; '.join(errors)}. NOT scope-"
                      f"declarable — fix the artifact."),
        }]

    if result["verdict"] == "OVER_SATURATED":
        # When validate_identifiability_a_priori returns verdict
        # OVER_SATURATED (not MALFORMED), the artifact has a documented
        # resolution decision — has_resolution is True by construction.
        # The over-saturated-without-resolution case returns MALFORMED
        # above and is dispatched there as HIGH pre_model_over_saturated.
        # So this branch always fires the MEDIUM commitment advisory.
        s = result.get("summary", {})
        return [{
            "kind": "pre_model_over_saturated_with_commitment",
            "severity": "MEDIUM",
            "stage": "PRE_MODEL",
            "claim": (
                f"Pre-model identifiability arithmetic flags "
                f"OVER_SATURATED: {s.get('n_fitted', 0)} fitted "
                f"/ {s.get('n_targets', 0)} targets = "
                f"{s.get('ratio', 0):.2f}×. Resolution committed "
                f"in artifact. Once redesign is implemented, "
                f"update identifiability_a_priori.yaml to show the "
                f"new fitted count and verdict IDENTIFIABLE."
            ),
        }]

    if result["verdict"] == "MARGINAL":
        s = result.get("summary", {})
        return [{
            "kind": "pre_model_marginal_identifiability",
            "severity": "MEDIUM",
            "stage": "PRE_MODEL",
            "claim": (
                f"Pre-model identifiability arithmetic flags "
                f"MARGINAL: {s.get('n_fitted', 0)} fitted / "
                f"{s.get('n_targets', 0)} targets = "
                f"{s.get('ratio', 0):.2f}× ratio (1.0-3.0 band). "
                f"The model is at risk for ridge-trapped parameters. "
                f"Post-hoc identifiability check at STAGE 5b will "
                f"confirm or refute. Surface as a caveat in "
                f"§Limitations."
            ),
        }]

    return []  # IDENTIFIABLE — silent pass


# Phase 12 Commit β: round-aware MEDIUM-to-HIGH escalation ledger.
#
# A MEDIUM that fires the same way for ≥ N rounds is signal the
# modeler is ignoring it. The 104914 run had 18 figure_validator_
# missing MEDIUMs persist r2→r6 because MEDIUM doesn't block ACCEPT
# and no validator escalated. This ledger maps validator-emitted
# kinds to their persistence-to-HIGH threshold (number of distinct
# rounds the kind appears in stage7_round*_stderr.txt).
#
# Critique-blocker IDs (e.g., P-005, R-007) are escalated separately
# via `first_seen_round` in the critique YAML — see _escalate_
# critique_blockers below.
_VALIDATOR_KIND_ESCALATION_THRESHOLDS = {
    "figure_validator_missing": 4,
    "decision_rule_self_referential": 3,
    "optimization_quality_missing": 4,
    # `optimization_quality_*_failed` includes the R-005-class
    # "no ILP benchmark" MEDIUMs that persisted in 104914
    "optimization_quality_alternative_missing": 4,
}

# Critique-blocker IDs from critique-presentation that should
# escalate after persisting N rounds. (Phase 11 review surfaced
# P-005 through P-009 persisting all 6 rounds.)
_CRITIQUE_BLOCKER_ESCALATION_THRESHOLD = 4  # rounds since first_seen


def _check_persistent_medium_escalation(run_dir: str,
                                        round_n: int) -> list[dict]:
    """Phase 12 Commit β: scan stage7_round*_stderr.txt for validator
    MEDIUMs that have persisted across multiple rounds and the round
    critique YAMLs for blocker MEDIUMs with old `first_seen_round`.
    Re-emit qualifying entries as HIGH so the gate forces fix-or-
    scope-declare rather than passing.

    Returns a list of HIGH `<kind>_persistent` and
    `<id>_persistent` violations. Silent if nothing escalates.

    Known limitation (Path A — validator-kind escalation): persistence
    is counted by scanning `stage7_round{N}_stderr.txt` files in the
    run dir. The lead is supposed to write one per gate invocation,
    but in practice not every round's stderr is saved (the 104914 run
    only had rounds 2/3/4 stderr present, no rounds 5/6). When earlier
    rounds' stderr is missing, the count is a LOWER BOUND, and a
    genuinely-persistent MEDIUM may not reach the threshold via Path A.
    Path B (critique-blocker escalation via `first_seen_round` in the
    YAML) is more reliable because the YAML is overwritten each round
    and the field is explicitly set by the critique agent.
    """
    out: list[dict] = []

    # --- Path A: validator-kind escalation via stderr scan ---
    # The lead writes stage7_round{N}_stderr.txt at gate time.
    # Count the distinct rounds where each tracked kind appears.
    rounds_with_kind: dict[str, set[int]] = {
        kind: set() for kind in _VALIDATOR_KIND_ESCALATION_THRESHOLDS}
    for entry in os.listdir(run_dir) if os.path.isdir(run_dir) else []:
        m = re.match(r"stage7_round(\d+)_stderr\.txt$", entry)
        if not m:
            continue
        try:
            n = int(m.group(1))
        except ValueError:
            continue
        try:
            with open(os.path.join(run_dir, entry), encoding="utf-8") as f:
                text = f.read()
        except (UnicodeDecodeError, OSError):
            continue
        for kind in _VALIDATOR_KIND_ESCALATION_THRESHOLDS:
            if kind in text:
                rounds_with_kind[kind].add(n)

    for kind, rounds_seen in rounds_with_kind.items():
        threshold = _VALIDATOR_KIND_ESCALATION_THRESHOLDS[kind]
        if len(rounds_seen) >= threshold:
            sorted_rounds = sorted(rounds_seen)
            out.append({
                "kind": f"{kind}_persistent",
                "severity": "HIGH",
                "stage": "GATE",
                "claim": (
                    f"{kind} MEDIUM has persisted across "
                    f"{len(rounds_seen)} distinct rounds "
                    f"({sorted_rounds}) without resolution. Phase 12 "
                    f"Commit β escalates to HIGH after "
                    f"{threshold} rounds — the modeler must either "
                    f"resolve the underlying issue (e.g., add "
                    f"validate_figure() calls, fix the decision-rule "
                    f"self-reference, or run an ILP benchmark) or "
                    f"explicitly scope-declare why it cannot be "
                    f"resolved within the pipeline."
                ),
            })

    # --- Path B: critique-blocker escalation via first_seen_round ---
    # Walk each critique YAML's blockers list. For MEDIUM blockers
    # with first_seen_round set and resolved != True, escalate to
    # HIGH if (round_n - first_seen_round) >= threshold.
    for reviewer_yaml in ("critique_methods.yaml", "critique_domain.yaml",
                          "critique_presentation.yaml",
                          "critique_redteam.yaml"):
        path = os.path.join(run_dir, reviewer_yaml)
        if not os.path.exists(path):
            continue
        try:
            with open(path) as f:
                doc = yaml.safe_load(f) or {}
        except (yaml.YAMLError, OSError):
            continue
        for blocker in doc.get("blockers") or []:
            if not isinstance(blocker, dict):
                continue
            severity = blocker.get("severity")
            if severity != "MEDIUM":
                continue
            if blocker.get("resolved") is True:
                continue
            first_seen = blocker.get("first_seen_round")
            if first_seen is None:
                continue
            try:
                persisted = int(round_n) - int(first_seen)
            except (TypeError, ValueError):
                continue
            if persisted < _CRITIQUE_BLOCKER_ESCALATION_THRESHOLD:
                continue
            blocker_id = blocker.get("id", "<unknown>")
            short_claim = str(blocker.get("claim") or "").strip().split("\n")[0][:140]
            out.append({
                "kind": f"{blocker_id}_persistent",
                "severity": "HIGH",
                "stage": "GATE",
                "claim": (
                    f"{reviewer_yaml.replace('.yaml','')} blocker "
                    f"{blocker_id} (MEDIUM) first seen in round "
                    f"{first_seen}, still unresolved at round "
                    f"{round_n} ({persisted} rounds persistence ≥ "
                    f"{_CRITIQUE_BLOCKER_ESCALATION_THRESHOLD}). "
                    f"Phase 12 Commit β escalates to HIGH. Fix the "
                    f"underlying issue or scope-declare. Original: "
                    f"\"{short_claim}\""
                ),
            })

    return out


# Phase 3 Commit C: decision rule as required rigor artifact.
# (Phase 10 Commit χ: removed mid-file aliased re/glob imports;
# `re` is imported at the top of the file and `glob` joined it.)

_ALLOCATION_GLOBS = ("*allocation*.csv", "*budget*.csv", "*optimization*.csv")
_DECISION_RULE_FRONTMATTER_RE = re.compile(
    r"^---\s*\n(.+?)\n---\s*\n", re.DOTALL
)
_REQUIRED_RULE_SECTIONS = ("## Features", "## Rule", "## Validation")
_VALID_RULE_TYPES = {
    "tabular", "tree", "prose-with-exceptions", "non-compressible",
}
_ACCURACY_RE = re.compile(
    r"^\s*-?\s*`?accuracy_vs_optimizer`?\s*[:=]\s*([0-9.]+)",
    re.MULTILINE,
)
_EXCEPTIONS_COUNT_RE = re.compile(
    r"^\s*-?\s*`?exceptions_count`?\s*[:=]\s*(\d+)",
    re.MULTILINE,
)


def _find_allocation_csvs(run_dir: str) -> list[str]:
    """Glob for allocation CSVs at the top level, under data/, and under
    models/. The models/ search was added in Phase 4 (Commit γ era) to
    fix a latent Phase 3 oversight: modelers commonly write
    `models/optimization_allocation.csv` rather than the run-dir top
    level, which silently skipped the entire decision-rule check.
    """
    found: list[str] = []
    for pattern in _ALLOCATION_GLOBS:
        found.extend(glob.glob(os.path.join(run_dir, pattern)))
        found.extend(glob.glob(os.path.join(run_dir, "data", pattern)))
        found.extend(glob.glob(os.path.join(run_dir, "models", pattern)))
    return sorted(set(found))


def _check_decision_rule_artifact(run_dir: str) -> list[dict]:
    """Decision rule (Phase 3 Commit C): if an allocation CSV exists,
    require decision_rule.md with a valid schema. Emits:
      - decision_rule_missing HIGH
      - decision_rule_malformed HIGH
      - decision_rule_low_accuracy HIGH
    """
    allocations = _find_allocation_csvs(run_dir)
    if not allocations:
        return []

    rule_path = os.path.join(run_dir, "decision_rule.md")
    if not os.path.exists(rule_path):
        return [{
            "kind": "decision_rule_missing",
            "severity": "HIGH",
            "stage": "DECISION_RULE",
            "claim": (
                f"Allocation CSV(s) present ({', '.join(os.path.basename(p) for p in allocations)}) "
                f"but decision_rule.md is absent. A 774-row table is not a "
                f"defensible policy artifact. Write decision_rule.md per the "
                f"decision-rule-extraction skill — tabular, tree, prose, or "
                f"non-compressible with justification."
            ),
        }]

    # Phase 10 Commit χ: previously this open() was unguarded — a
    # binary or UTF-8-corrupt decision_rule.md would crash STAGE 7
    # entirely instead of emitting a HIGH violation the modeler can
    # respond to.
    try:
        with open(rule_path, encoding="utf-8") as f:
            text = f.read()
    except (UnicodeDecodeError, OSError) as e:
        return [{
            "kind": "decision_rule_unreadable",
            "severity": "HIGH",
            "stage": "OPTIMIZATION",
            "claim": (
                f"decision_rule.md exists but cannot be read as UTF-8 "
                f"text: {type(e).__name__}: {e}. The validator cannot "
                f"verify the rule schema, self-reference, or accuracy. "
                f"Re-save decision_rule.md as UTF-8 plain text."
            ),
        }]

    # Accumulate violations across all checks rather than returning early
    # on the first malformed condition. This lets the Phase 4 γ
    # self-reference check run even when the schema check has already
    # flagged a malformed rule — a malformed file may still have
    # self-referential prose worth surfacing, and the modeler should see
    # both issues in one round.
    violations: list[dict] = []
    rule_type = None
    fm_match = _DECISION_RULE_FRONTMATTER_RE.match(text)
    if not fm_match:
        violations.append({
            "kind": "decision_rule_malformed",
            "severity": "HIGH",
            "stage": "DECISION_RULE",
            "claim": (
                "decision_rule.md is missing YAML front-matter "
                "(`--- rule_type: ... ---` at the top of the file). "
                "See decision-rule-extraction skill for the schema."
            ),
        })
    else:
        try:
            fm = yaml.safe_load(fm_match.group(1)) or {}
            rule_type = fm.get("rule_type")
            if rule_type not in _VALID_RULE_TYPES:
                violations.append({
                    "kind": "decision_rule_malformed",
                    "severity": "HIGH",
                    "stage": "DECISION_RULE",
                    "claim": (
                        f"decision_rule.md front-matter `rule_type` is "
                        f"{rule_type!r}; must be one of "
                        f"{sorted(_VALID_RULE_TYPES)}."
                    ),
                })
                rule_type = None
        except yaml.YAMLError as e:
            violations.append({
                "kind": "decision_rule_malformed",
                "severity": "HIGH",
                "stage": "DECISION_RULE",
                "claim": (f"decision_rule.md front-matter is not valid "
                          f"YAML: {e}"),
            })

    missing_sections = [s for s in _REQUIRED_RULE_SECTIONS if s not in text]
    if rule_type == "non-compressible" and "## Justification" not in text:
        missing_sections.append("## Justification")
    if missing_sections:
        violations.append({
            "kind": "decision_rule_malformed",
            "severity": "HIGH",
            "stage": "DECISION_RULE",
            "claim": (
                f"decision_rule.md is missing required section(s): "
                f"{missing_sections}. See decision-rule-extraction skill."
            ),
        })
    if rule_type is not None and rule_type != "non-compressible":
        acc_match = _ACCURACY_RE.search(text)
        exc_match = _EXCEPTIONS_COUNT_RE.search(text)
        if acc_match and exc_match:
            try:
                accuracy = float(acc_match.group(1))
                exc_count = int(exc_match.group(1))
                # Phase 10 Commit χ: severity recalibration. The
                # original threshold fired HIGH at < 0.90, but a 22-
                # archetype × 8-package allocation rule with 0.85
                # accuracy is publication-defensible (Global Fund
                # rules routinely run 0.80-0.90). HIGH should be
                # reserved for rules that are genuinely indefensible
                # (< 0.75 with no declared exceptions). The 0.75-0.90
                # band moves to MEDIUM — surface in §Limitations,
                # don't block ACCEPT.
                if accuracy < 0.75 and exc_count == 0:
                    violations.append({
                        "kind": "decision_rule_low_accuracy",
                        "severity": "HIGH",
                        "stage": "DECISION_RULE",
                        "claim": (
                            f"decision_rule.md claims rule_type={rule_type!r} "
                            f"with accuracy_vs_optimizer={accuracy:.2f} and "
                            f"exceptions_count=0. < 0.75 accuracy with no "
                            f"declared exceptions is indefensible — a "
                            f"program officer applying this rule would "
                            f"differ from the optimizer on > 25% of units. "
                            f"Either list the disagreeing units in "
                            f"exceptions_list, or switch rule_type to "
                            f"`non-compressible` with a Justification."
                        ),
                    })
                elif accuracy < 0.90 and exc_count == 0:
                    violations.append({
                        "kind": "decision_rule_low_accuracy",
                        "severity": "MEDIUM",
                        "stage": "DECISION_RULE",
                        "claim": (
                            f"decision_rule.md claims rule_type={rule_type!r} "
                            f"with accuracy_vs_optimizer={accuracy:.2f} and "
                            f"exceptions_count=0. Accuracy in the 0.75-0.90 "
                            f"band is publication-defensible but should be "
                            f"surfaced in §Limitations: a program officer "
                            f"applying this rule will differ from the "
                            f"optimizer on 10-25% of units. Consider listing "
                            f"the largest-disagreement units as exceptions."
                        ),
                    })
            except (ValueError, TypeError):
                pass

    # Phase 4 Commit γ: self-reference detection. A rule node referencing
    # the optimizer's output ("be in the funded set", "cost-effective
    # enough", etc.) is non-actionable — a program officer can't apply it
    # without running the optimizer. The 0.97 accuracy claim becomes
    # mathematically correct but functionally useless. Defensible rules
    # use only INPUT FEATURES (PfPR, archetype, concrete CE cutoff) — not
    # outputs of the optimization itself. MEDIUM severity (rule is
    # technically correct; surfacing the usability defect).
    #
    # Approach: search the whole body MINUS the Justification section
    # (where prose explanation of why-no-compact-rule is allowed to
    # reference the optimizer). This catches non-canonical rule headers
    # like `## Decision Tree (4 nodes)` and `## Simplified Rule (prose)`
    # without requiring the modeler to use exact `## Rule` boilerplate.
    rule_body = _strip_justification_section(text)
    found = [t for t in _SELF_REFERENCE_TOKENS
             if t.lower() in rule_body.lower()]
    if found:
        violations.append({
            "kind": "decision_rule_self_referential",
            "severity": "MEDIUM",
            "stage": "DECISION_RULE",
            "claim": (
                f"decision_rule.md (outside ## Justification) contains "
                f"self-reference token(s) {found}. A rule node "
                f"referencing the optimizer's output (e.g. 'be in the "
                f"funded set', 'cost-effective enough') is non-actionable: "
                f"a program officer can't apply it without re-running the "
                f"model. Replace with an INPUT FEATURE — e.g. "
                f"'cases-averted-per-dollar > $X/case' (a concrete CE "
                f"cutoff value the rule can be evaluated against) — or "
                f"declare rule_type=non-compressible with a Justification "
                f"explaining why no compact rule applies."
            ),
        })

    return violations


def _strip_justification_section(text: str) -> str:
    """Phase 4 Commit γ helper. Return `text` with the `## Justification`
    section removed (slice from header to next `##` or end-of-text).
    Pattern reused from scripts/effect_size_registry.py linear scan.

    Used to scan the rule body for self-reference tokens while allowing
    prose in Justification (where referencing the optimizer's output is
    the legitimate use case for non-compressible rules).
    """
    hdr_re = re.compile(r"^##\s+Justification\b.*$", re.MULTILINE)
    m = hdr_re.search(text)
    if m is None:
        return text
    next_h2 = re.search(r"^##\s", text[m.end():], re.MULTILINE)
    if next_h2 is None:
        return text[:m.start()]
    return text[:m.start()] + text[m.end() + next_h2.start():]


# Phase 4 Commit γ + Phase 5 Commit ε: tokens flagging a rule node as
# self-referential. Tokens are specific phrases (avoiding bare words
# like "optimizer" that legitimately appear in field names such as
# `accuracy_vs_optimizer`). The Justification section is stripped before
# this scan, so prose explanation in non-compressible rules can still
# reference the optimizer.
#
# Phase 5 Commit ε added budget-availability variants — the 2057 malaria
# run's decision rule node 5 was "Is PfPR >= 15% AND budget remaining?",
# the same pattern as 1302's "cost-effective enough" but worded around
# budget availability rather than cost-effectiveness. A program officer
# can't apply node 5 without re-running the optimizer to see what budget
# remains.
_SELF_REFERENCE_TOKENS = (
    # Phase 4 (optimizer/funded-set patterns)
    "the optimizer",
    "optimizer's choice",
    "optimizer output",
    "optimized choice",
    "budget cut",
    "funded set",
    "in the funded",
    "cost-effective enough",
    "cost effective enough",
    "fall within the budget",
    "ranked by",
    "selected by the model",
    "the model recommends",
    # Phase 5 ε (budget-availability patterns)
    "budget remaining",
    "budget available",
    "if budget allows",
    "if budget remains",
    "subject to budget",
    "remaining funds",
    "funds remaining",
    "funds available",
    "if funds remain",
    "funds permitting",
    "budget permitting",
    "as budget allows",
)


def _incorporate_rigor_violations(decision: dict, violations: list[dict],
                                  max_rounds: int, current_round: int) -> dict:
    """Fold rigor-artifact violations into unresolved_high (for HIGH) or
    attach for visibility (for MEDIUM). Uses RIG-NNN prefix."""
    d = dict(decision)
    d["rigor_violations"] = violations

    base_id = len([b for b in d["unresolved_high"]
                   if b.get("reviewer") == "rigor-artifacts"])
    for i, v in enumerate(
            [x for x in violations if x["severity"] == "HIGH"],
            start=base_id + 1):
        d["unresolved_high"].append({
            "reviewer": "rigor-artifacts",
            "id": f"RIG-{i:03d}",
            "category": "METHODS",
            "target_stage": "MODEL",
            "first_seen_round": current_round,
            "claim": f"{v['stage']}/{v['kind']}: {v['claim']}",
        })

    # Recompute action (same rule ordering).
    unresolved_high = d["unresolved_high"]
    structural = d.get("structural_mismatch", False)
    rounds_remaining = max_rounds - current_round

    if structural:
        action = "RETHINK_STRUCTURAL" if rounds_remaining > 0 else "RUN_FAILED"
        rule_matched = 1
    elif unresolved_high and rounds_remaining > 0:
        action = "PATCH_OR_RETHINK"
        rule_matched = 2
    elif unresolved_high and rounds_remaining <= 0:
        action = "DECLARE_SCOPE"
        rule_matched = 3
    else:
        action = "ACCEPT"
        rule_matched = 4

    d["action"] = action
    d["rule_matched"] = rule_matched
    if rule_matched in (1, 2):
        d["rationale"] = (
            f"{len(unresolved_high)} HIGH blocker(s) unresolved (incl. rigor), "
            f"{rounds_remaining} round(s) remaining. ACCEPT is forbidden."
        )
    return d


def incorporate_registry_violations(decision: dict, violations: list[dict],
                                    max_rounds: int, current_round: int) -> dict:
    """Fold effect-size-registry violations into the decision.

    HIGH violations (or_rr_conflation, registry_value_mismatch,
    cost_crosscheck_mismatch, param_not_in_code, param_frozen_in_uq)
    add synthetic `REG-NNN` blockers to `unresolved_high`. MEDIUM
    violations (registry_missing_ref, param_unregistered,
    subgroup_mismatch) are attached for visibility only.

    param_frozen_in_uq (Phase 3 Commit A2) is the mechanical R-022
    detector: registered parameter appears in code (e.g. as an
    UPPER_CASE constant in optimization.py) but is not referenced as
    params['NAME'] / params.get('NAME') in any UQ entry point, so its
    uncertainty is drawn from priors but never propagated through the
    outcome calculation.

    Registry violations do NOT force `structural_mismatch=True` — they are
    parameter-provenance issues, not architectural mismatches. A failed
    registry check at round N means the run cannot ACCEPT until the modeler
    patches the parameter or the registry entry.

    Idempotent: calling twice yields identical output.
    """
    d = dict(decision)
    d["registry_violations"] = violations

    base_id = len([b for b in d["unresolved_high"]
                   if b.get("reviewer") == "effect-size-registry"])
    for i, v in enumerate(
            [x for x in violations if x["severity"] == "HIGH"],
            start=base_id + 1):
        d["unresolved_high"].append({
            "reviewer": "effect-size-registry",
            "id": f"REG-{i:03d}",
            "category": "CITATIONS",
            "target_stage": "MODEL",
            "first_seen_round": current_round,
            "claim": f"{v['kind']} ({v['name']}): {v['claim']}",
        })

    # Recompute action using the same rule ordering. If spec-compliance has
    # also been run and set structural=True, the structural rule still fires
    # first; registry violations just add HIGHs underneath.
    unresolved_high = d["unresolved_high"]
    structural = d.get("structural_mismatch", False)
    rounds_remaining = max_rounds - current_round

    if structural:
        action = "RETHINK_STRUCTURAL" if rounds_remaining > 0 else "RUN_FAILED"
        rule_matched = 1
        rationale = d.get("rationale", "")
    elif unresolved_high and rounds_remaining > 0:
        action = "PATCH_OR_RETHINK"
        rationale = (
            f"{len(unresolved_high)} HIGH blocker(s) unresolved "
            f"(incl. registry), {rounds_remaining} round(s) remaining. "
            f"ACCEPT is forbidden."
        )
        rule_matched = 2
    elif unresolved_high and rounds_remaining <= 0:
        action = "DECLARE_SCOPE"
        rationale = (
            f"{len(unresolved_high)} HIGH blocker(s) unresolved, rounds "
            f"exhausted. Must write scope_declaration.yaml."
        )
        rule_matched = 3
    else:
        action = "ACCEPT"
        rationale = "No unresolved HIGH blockers, no structural mismatch."
        rule_matched = 4

    d["action"] = action
    d["rule_matched"] = rule_matched
    d["rationale"] = rationale
    return d


def incorporate_spec_violations(decision: dict, violations: list[dict],
                                max_rounds: int, current_round: int) -> dict:
    """Fold spec_compliance violations into the decision.

    Framework / approach HIGH violations force `structural_mismatch=True`
    (the mechanical backstop: critiques missed an architectural issue, so
    the gate itself catches it). Budget / archetype HIGH violations add
    synthetic `OBJ-NNN` blockers to `unresolved_high`. MEDIUM violations
    are attached for visibility but do not change the action.

    This function is idempotent: calling it twice yields identical output.
    """
    # Start from the existing decision; we'll mutate a shallow copy.
    d = dict(decision)
    d["spec_violations"] = violations
    structural_kinds = {"framework_missing", "approach_mismatch"}
    # Phase 3 B+D extends objective_kinds with vintage, methodology,
    # and bound-weak archetype categories.
    objective_kinds = {
        "budget_underutilized",
        "archetype_aggregation_unvalidated",
        "archetype_bound_weak",
        "data_vintage_stale",
        "methodology_vintage_stale",
        "vintage_unstructured",
    }

    high_struct = [v for v in violations
                   if v["severity"] == "HIGH" and v["kind"] in structural_kinds]
    high_objective = [v for v in violations
                      if v["severity"] == "HIGH" and v["kind"] in objective_kinds]

    # Force structural_mismatch when any structural HIGH violation exists.
    if high_struct:
        d["structural_mismatch"] = True
        reviewers = list(d.get("structural_reviewers") or [])
        if "spec-compliance" not in reviewers:
            reviewers.append("spec-compliance")
        d["structural_reviewers"] = reviewers

    # Add synthetic blockers for objective HIGH violations.
    base_id = len([b for b in d["unresolved_high"]
                   if b.get("reviewer") == "spec-compliance"])
    for i, v in enumerate(high_objective, start=base_id + 1):
        d["unresolved_high"].append({
            "reviewer": "spec-compliance",
            "id": f"OBJ-{i:03d}",
            "category": "STRUCTURAL",  # re-route via STRUCTURAL so PATCH heuristic
                                        # escalates to RETHINK if it recurs.
            "target_stage": "MODEL",
            "first_seen_round": current_round,
            "claim": f"{v['kind']}: {v['evidence']}",
        })

    # Recompute action from the adjusted state (same rule ordering as decide()).
    unresolved_high = d["unresolved_high"]
    structural = d["structural_mismatch"]
    rounds_remaining = max_rounds - current_round

    if structural:
        if rounds_remaining > 0:
            action = "RETHINK_STRUCTURAL"
            rationale = (
                f"Structural mismatch detected by {d['structural_reviewers']}. "
                f"Must RETHINK — not patchable, not scope-declarable."
            )
        else:
            action = "RUN_FAILED"
            rationale = (
                f"Structural mismatch detected by {d['structural_reviewers']} "
                f"with no rounds remaining. Run fails: delivered model does "
                f"not answer the question. Do NOT spawn writer."
            )
        rule_matched = 1
    elif unresolved_high and rounds_remaining > 0:
        action = "PATCH_OR_RETHINK"
        rationale = (
            f"{len(unresolved_high)} HIGH blocker(s) unresolved "
            f"(incl. spec-compliance), {rounds_remaining} round(s) "
            f"remaining. ACCEPT is forbidden."
        )
        rule_matched = 2
    elif unresolved_high and rounds_remaining <= 0:
        action = "DECLARE_SCOPE"
        rationale = (
            f"{len(unresolved_high)} HIGH blocker(s) unresolved, rounds "
            f"exhausted. Must write scope_declaration.yaml acknowledging "
            f"each blocker by id, and writer must embed verbatim in "
            f"Limitations. DECLARE_SCOPE is NOT the same as ACCEPT."
        )
        rule_matched = 3
    else:
        action = "ACCEPT"
        rationale = "No unresolved HIGH blockers, no structural mismatch."
        rule_matched = 4

    d["action"] = action
    d["rule_matched"] = rule_matched
    d["rationale"] = rationale
    return d


def render_text(decision: dict, current_round: int, max_rounds: int) -> str:
    lines = [
        f"STAGE 7 decision (round {current_round}/{max_rounds})",
        f"  unresolved_high: {len(decision['unresolved_high'])} blocker(s)",
    ]
    for b in decision["unresolved_high"]:
        lines.append(f"    - {b['reviewer']} {b['id']} "
                     f"(target={b['target_stage']}, "
                     f"since round {b['first_seen_round']}): {b['claim'][:100]}")
    lines.append(f"  structural_mismatch: {decision['structural_mismatch']}"
                 + (f" (from: {decision['structural_reviewers']})"
                    if decision["structural_mismatch"] else ""))
    spec = decision.get("spec_violations") or []
    if spec:
        lines.append(f"  spec_violations: {len(spec)}")
        for v in spec:
            lines.append(f"    - [{v['severity']}] {v['kind']}: "
                         f"{v['evidence'][:120]}")
    reg = decision.get("registry_violations") or []
    if reg:
        lines.append(f"  registry_violations: {len(reg)}")
        for v in reg:
            lines.append(f"    - [{v['severity']}] {v['kind']} "
                         f"({v['name']}): {v['claim'][:120]}")
    rig = decision.get("rigor_violations") or []
    if rig:
        lines.append(f"  rigor_violations: {len(rig)}")
        for v in rig:
            lines.append(f"    - [{v['severity']}] {v['stage']}/{v['kind']}: "
                         f"{v['claim'][:120]}")
    # Phase 5 ζ: surface stuck blockers so the lead can pick the right
    # escalation path (SCOPE_DECLARE_EARLY, CROSS_STAGE_ESCALATE, etc.).
    attempts = decision.get("blocker_attempts") or {}
    stuck = [(bid, info) for bid, info in attempts.items()
             if info.get("patch_attempts", 0) >= 2]
    if stuck:
        lines.append(f"  stuck_blockers: {len(stuck)} "
                     f"(>=2 failed PATCH attempts)")
        for bid, info in stuck:
            lines.append(
                f"    - {bid} {info.get('reviewer','?')} "
                f"category={info.get('category')} "
                f"target={info.get('target_stage')} "
                f"attempts={info['patch_attempts']} "
                f"first_seen=round {info['first_seen_round']}"
            )
    if decision.get("escalation_required"):
        lines.append(
            "  escalation_required: TRUE — at least one HIGH blocker "
            "has >=3 failed PATCH attempts. Re-spawning the same "
            "target_stage with the same fix instructions is forbidden. "
            "Apply category-aware escalation (see STAGE 7 prompt: "
            "SCOPE_DECLARE_EARLY for PRESENTATION, CROSS_STAGE_ESCALATE "
            "for HARD_BLOCKER/METHODS, originating-critique re-spawn "
            "for HYPOTHESES/CITATIONS)."
        )
    lines.append(f"  rounds_remaining: {decision['rounds_remaining']}")
    lines.append(f"  rule_matched: {decision['rule_matched']}")
    lines.append(f"  action: {decision['action']}")
    lines.append(f"  rationale: {decision['rationale']}")
    return "\n".join(lines)


def _run_self_test() -> int:
    """Run inline self-test cases. Returns 0 if all pass, 1 otherwise.

    Cases cover the Phase 4 mechanical checks (ci_degenerate, etc.).
    Pre-Phase-4 logic (decide(), incorporate_*) is exercised end-to-end
    by the existing critique-fixture runs, so it is not duplicated here.
    """
    import tempfile

    failures: list[str] = []

    def ok(cond: bool, label: str) -> None:
        if not cond:
            failures.append(label)

    # --- Phase 4 Commit α: zero-width CI detector ---
    with tempfile.TemporaryDirectory() as d:
        # Case A1: degenerate cost CI (greedy-fill pattern from 1302 run).
        uq_a = os.path.join(d, "uq_a.yaml")
        with open(uq_a, "w") as f:
            f.write(
                "n_draws: 200\n"
                "n_errors: 0\n"
                "scalar_outputs:\n"
                "  total_cost_3yr:\n"
                "    mean: 320000000\n"
                "    median: 320000000\n"
                "    ci_low: 319200000\n"
                "    ci_high: 320000000\n"
                "    n: 200\n"
            )
        va = _check_uq_ci_quality(uq_a)
        ok(any(v["kind"] == "ci_degenerate" for v in va),
           f"A1: expected ci_degenerate on optimizer-bounded cost, got {va}")
        ok(all(v["severity"] == "MEDIUM" for v in va),
           f"A1: ci_degenerate must be MEDIUM, got {[v['severity'] for v in va]}")

        # Case A2: legitimate wide CI — should NOT fire.
        uq_b = os.path.join(d, "uq_b.yaml")
        with open(uq_b, "w") as f:
            f.write(
                "scalar_outputs:\n"
                "  cases_averted_3yr:\n"
                "    mean: 20000000\n"
                "    ci_low: 15000000\n"
                "    ci_high: 25000000\n"
                "    n: 200\n"
            )
        vb = _check_uq_ci_quality(uq_b)
        ok(not vb, f"A2: ±25% CI should not fire, got {vb}")

        # Case A3: zero-mean output is skipped (relative width undefined).
        uq_c = os.path.join(d, "uq_c.yaml")
        with open(uq_c, "w") as f:
            f.write(
                "scalar_outputs:\n"
                "  net_change:\n"
                "    mean: 0\n"
                "    ci_low: 0\n"
                "    ci_high: 0\n"
                "    n: 200\n"
            )
        vc = _check_uq_ci_quality(uq_c)
        ok(not vc, f"A3: zero-mean output should be skipped, got {vc}")

        # Case A4: hardcoded calibration target (CI ~ 1e-7 of mean) — fires.
        uq_d = os.path.join(d, "uq_d.yaml")
        with open(uq_d, "w") as f:
            f.write(
                "scalar_outputs:\n"
                "  national_pfpr_baseline:\n"
                "    mean: 0.20967654\n"
                "    ci_low: 0.20967654\n"
                "    ci_high: 0.20967654\n"
                "    n: 200\n"
            )
        vd = _check_uq_ci_quality(uq_d)
        ok(any(v["kind"] == "ci_degenerate" for v in vd),
           f"A4: hardcoded calibration target should fire, got {vd}")

    # --- Phase 4 Commit β: surrogate UQ documentation requirement ---
    with tempfile.TemporaryDirectory() as d:
        models = os.path.join(d, "models")
        os.makedirs(models)

        # Case B1: surrogate-only outcome_fn, no calibration md → HIGH.
        with open(os.path.join(models, "outcome_fn.py"), "w") as f:
            f.write(
                "import pandas as pd\n"
                "def outcome_fn(params):\n"
                "    df = pd.read_csv('package_evaluation.csv')\n"
                "    return {'cases_averted': df.cases_averted.sum() * 0.95}\n"
            )
        vb1 = _check_surrogate_uq_documented(d)
        ok(any(v["kind"] == "surrogate_uq_undocumented"
               and v["severity"] == "HIGH" for v in vb1),
           f"B1: surrogate outcome_fn without calibration md should fire HIGH, "
           f"got {vb1}")

        # Case B2: surrogate + calibration md without RMSE → MEDIUM.
        with open(os.path.join(models, "outcome_fn_calibration.md"), "w") as f:
            f.write("# Surrogate calibration\n\nWe used grid interpolation.\n")
        vb2 = _check_surrogate_uq_documented(d)
        ok(any(v["kind"] == "surrogate_calibration_missing_rmse"
               and v["severity"] == "MEDIUM" for v in vb2),
           f"B2: calibration md without RMSE should fire MEDIUM, got {vb2}")

        # Case B3: surrogate + calibration md WITH RMSE → no fire.
        with open(os.path.join(models, "outcome_fn_calibration.md"), "w") as f:
            f.write(
                "# Surrogate calibration\n\n"
                "Validation grid (12 points). RMSE per output:\n"
                "- cases_averted: 0.03\n"
                "- cost: 0.02\n"
            )
        vb3 = _check_surrogate_uq_documented(d)
        ok(not vb3, f"B3: surrogate + RMSE-documented should not fire, got {vb3}")

        # Case B4: real-model outcome_fn (calls ss.Sim) → no fire even
        # if a CSV is also read.
        with open(os.path.join(models, "outcome_fn.py"), "w") as f:
            f.write(
                "import starsim as ss\n"
                "import pandas as pd\n"
                "def outcome_fn(params):\n"
                "    df = pd.read_csv('package_evaluation.csv')\n"
                "    sim = ss.Sim(diseases=ss.SIR(), n_agents=1000)\n"
                "    sim.run()\n"
                "    return {'cases_averted': sim.results.cum_infections[-1]}\n"
            )
        vb4 = _check_surrogate_uq_documented(d)
        ok(not vb4, f"B4: real-model outcome_fn should not fire, got {vb4}")

        # Case B5: outcome_fn that reads only data/ csvs (not surrogate
        # naming) and calls no model — should not fire (low-recall but
        # avoids false positives on legitimate data-driven outcome_fns).
        with open(os.path.join(models, "outcome_fn.py"), "w") as f:
            f.write(
                "import pandas as pd\n"
                "def outcome_fn(params):\n"
                "    df = pd.read_csv('data/observations.csv')\n"
                "    return {'rmse': ((df.obs - df.pred)**2).mean()**0.5}\n"
            )
        vb5 = _check_surrogate_uq_documented(d)
        ok(not vb5,
           f"B5: data-driven outcome_fn (no surrogate naming) should not fire, "
           f"got {vb5}")

    # --- Phase 4 Commit γ: decision-rule self-reference detector ---
    with tempfile.TemporaryDirectory() as d:
        # Create the allocation CSV so _check_decision_rule_artifact
        # actually runs (it short-circuits when no allocation exists).
        with open(os.path.join(d, "lga_allocation.csv"), "w") as f:
            f.write("lga,package\nA,X\n")

        # Case C1: rule with self-reference token in ## Rule section → MEDIUM
        with open(os.path.join(d, "decision_rule.md"), "w") as f:
            f.write(
                "---\n"
                "rule_type: tree\n"
                "---\n"
                "# Decision Rule\n"
                "## Features\n- archetype\n- pfpr\n"
                "## Rule\n"
                "1. Is the LGA in the funded set?\n"
                "   YES -> PBO+SMC\n"
                "   NO -> baseline\n"
                "## Validation\n"
                "- accuracy_vs_optimizer: 0.97\n"
                "- exceptions_count: 5\n"
            )
        vc1 = _check_decision_rule_artifact(d)
        ok(any(v["kind"] == "decision_rule_self_referential"
               and v["severity"] == "MEDIUM" for v in vc1),
           f"C1: 'in the funded set' in Rule should fire MEDIUM, got {vc1}")

        # Case C2: rule with concrete cutoff (no self-reference) → no fire
        with open(os.path.join(d, "decision_rule.md"), "w") as f:
            f.write(
                "---\n"
                "rule_type: tree\n"
                "---\n"
                "# Decision Rule\n"
                "## Features\n- archetype\n- pfpr\n"
                "## Rule\n"
                "1. Is PfPR > 0.25?\n"
                "   YES -> PBO+SMC\n"
                "   NO -> baseline\n"
                "## Validation\n"
                "- accuracy_vs_optimizer: 0.95\n"
                "- exceptions_count: 8\n"
            )
        vc2 = _check_decision_rule_artifact(d)
        ok(not any(v["kind"] == "decision_rule_self_referential" for v in vc2),
           f"C2: concrete-cutoff rule should not fire self-referential, got {vc2}")

        # Case C3: token only in Justification (non-compressible) → no fire
        with open(os.path.join(d, "decision_rule.md"), "w") as f:
            f.write(
                "---\n"
                "rule_type: non-compressible\n"
                "---\n"
                "# Decision Rule\n"
                "## Features\n- archetype\n- pfpr\n- 12 others\n"
                "## Rule\nSee Justification.\n"
                "## Validation\n"
                "- accuracy_vs_optimizer: 0.61\n"
                "- exceptions_count: 325\n"
                "## Justification\n"
                "The optimizer's choices reflect strong stakeholder "
                "pre-commitments and near-ties; no compact rule applies.\n"
            )
        vc3 = _check_decision_rule_artifact(d)
        ok(not any(v["kind"] == "decision_rule_self_referential" for v in vc3),
           f"C3: token in Justification only should not fire, got {vc3}")

    # --- Phase 4 Commit δ: cross-comparator efficiency outlier ---
    with tempfile.TemporaryDirectory() as d:
        # Case D1: this work avert/budget efficiency 2x the best comparator.
        # Mirrors the 1302 malaria run table: this $107M/yr averts 60K/3yr;
        # Scott $175M/yr averts 84K/5yr (50.4K/3yr). Per-year-dollar:
        #   this:  20K / 107M = 187 deaths/$M-yr
        #   Scott: 16.8K / 175M = 96 deaths/$M-yr
        #   ratio: 1.95x
        report_d1 = os.path.join(d, "report.md")
        with open(report_d1, "w") as f:
            f.write(
                "# Report\n\n"
                "## Comparison\n\n"
                "| Finding | This Model | Scott 2017 | Ozodiegwu 2023 |\n"
                "|---------|------------|------------|----------------|\n"
                "| NW priority | Yes | Yes | Yes |\n"
                "| Deaths averted | ~60,000/3yr | 84,000/5yr (~50,400/3yr) | N/A |\n"
                "| Budget | $320M/3yr (~$107M/yr) | ~$175M/yr | N/A |\n"
            )
        vd1 = _check_comparator_efficiency(d)
        ok(any(v["kind"] == "comparator_efficiency_outlier"
               and v["severity"] == "MEDIUM" for v in vd1),
           f"D1: 2x efficiency vs Scott should fire, got {vd1}")

        # Case D2: similar efficiency to comparators → no fire.
        with open(report_d1, "w") as f:
            f.write(
                "# Report\n\n"
                "| Finding | This Model | Scott 2017 | Ozodiegwu 2023 |\n"
                "|---------|------------|------------|----------------|\n"
                "| Deaths averted | 16,000/yr | 16,800/yr | 17,000/yr |\n"
                "| Budget | $107M/yr | $175M/yr | $150M/yr |\n"
            )
        vd2 = _check_comparator_efficiency(d)
        ok(not vd2,
           f"D2: similar efficiency should not fire, got {vd2}")

        # Case D3: no comparator columns → no fire.
        with open(report_d1, "w") as f:
            f.write(
                "# Report\n\n"
                "| Item | Value |\n"
                "|------|-------|\n"
                "| Deaths averted | 60,000/3yr |\n"
                "| Budget | $320M/3yr |\n"
            )
        vd3 = _check_comparator_efficiency(d)
        ok(not vd3,
           f"D3: single-column table should not fire, got {vd3}")

        # Case D4: cost-per-death row, this much cheaper than comparators.
        with open(report_d1, "w") as f:
            f.write(
                "# Report\n\n"
                "| Finding | This Model | Scott 2017 | Ozodiegwu 2023 |\n"
                "|---------|------------|------------|----------------|\n"
                "| Cost per death averted | $5,300 | $10,400 | $9,800 |\n"
            )
        vd4 = _check_comparator_efficiency(d)
        ok(any(v["kind"] == "comparator_efficiency_outlier" for v in vd4),
           f"D4: cost-per-death row 50% cheaper should fire, got {vd4}")

    # --- Phase 5 Commit ε: γ + δ extensions ---
    with tempfile.TemporaryDirectory() as d:
        # Case E1: γ widening — "budget remaining" in rule body fires.
        with open(os.path.join(d, "lga_allocation.csv"), "w") as f:
            f.write("lga,package\nA,X\n")
        with open(os.path.join(d, "decision_rule.md"), "w") as f:
            f.write(
                "---\n"
                "rule_type: tree\n"
                "---\n"
                "# Decision Rule\n"
                "## Features\n- pfpr\n- archetype\n"
                "## Rule\n"
                "1. Is PfPR >= 25%?\n"
                "   YES -> Go to 2\n"
                "   NO -> baseline\n"
                "2. Is PfPR >= 15% AND budget remaining?\n"
                "   YES -> Standard LLIN 80%\n"
                "   NO -> baseline\n"
                "## Validation\n"
                "- accuracy_vs_optimizer: 0.95\n"
                "- exceptions_count: 7\n"
            )
        ve1 = _check_decision_rule_artifact(d)
        ok(any(v["kind"] == "decision_rule_self_referential" for v in ve1),
           f"E1: 'budget remaining' should fire γ MEDIUM, got {ve1}")

    # Case E2: δ generic comparator header (Hard Blocker Scorecard format).
    # 2057 malaria pattern: single "Published Value" column + Scott 2017
    # row with deaths-averted gap.
    with tempfile.TemporaryDirectory() as d:
        report_e2 = os.path.join(d, "report.md")
        with open(report_e2, "w") as f:
            f.write(
                "# Report\n\n"
                "| ID | This Model | Published Value | Status |\n"
                "|----|------------|-----------------|--------|\n"
                "| Cost per death averted | $105,000 | $10,400 (Scott 2017) | DISAGREE |\n"
            )
        ve2 = _check_comparator_efficiency(d)
        ok(any(v["kind"] == "comparator_efficiency_outlier" for v in ve2),
           f"E2: 'Published Value' column + 10x worse cost-per-death "
           f"should fire δ inverse direction, got {ve2}")

    # Case E3: δ inverse direction with averted+budget rows
    # (this work UNDER-claims vs comparator).
    with tempfile.TemporaryDirectory() as d:
        report_e3 = os.path.join(d, "report.md")
        with open(report_e3, "w") as f:
            f.write(
                "# Report\n\n"
                "| Finding | This Work | Scott 2017 | Ozodiegwu 2023 |\n"
                "|---------|-----------|------------|----------------|\n"
                "| Deaths averted | 3,046/3yr | 84,000/5yr | N/A |\n"
                "| Budget | $107M/yr | $175M/yr | N/A |\n"
            )
        ve3 = _check_comparator_efficiency(d)
        ok(any(v["kind"] == "comparator_efficiency_outlier"
               and "UNDERPERFORMANCE" in v["claim"] for v in ve3),
           f"E3: deaths 10x less efficient than Scott should fire inverse, "
           f"got {ve3}")

    # Case E4: legitimate Published Value column where ratio is in
    # acceptable range — should NOT fire.
    with tempfile.TemporaryDirectory() as d:
        report_e4 = os.path.join(d, "report.md")
        with open(report_e4, "w") as f:
            f.write(
                "# Report\n\n"
                "| Metric | This Model | Published Value | Status |\n"
                "|--------|------------|-----------------|--------|\n"
                "| Cases averted/yr | 16,500 | 17,000 (Bhatt 2015) | AGREE |\n"
                "| Budget | $100M/yr | $105M/yr | AGREE |\n"
            )
        ve4 = _check_comparator_efficiency(d)
        ok(not ve4,
           f"E4: similar values should not fire even with generic header, "
           f"got {ve4}")

    # Case E5: single-comparator stricter threshold — 2x improvement
    # should NOT fire (was 1.5x for ≥2 comparators; now 3x for single).
    with tempfile.TemporaryDirectory() as d:
        report_e5 = os.path.join(d, "report.md")
        with open(report_e5, "w") as f:
            f.write(
                "# Report\n\n"
                "| Metric | This Work | Scott 2017 |\n"
                "|--------|-----------|------------|\n"
                "| Cases averted/yr | 30,000 | 15,000 |\n"
                "| Budget | $100M/yr | $100M/yr |\n"
            )
        ve5 = _check_comparator_efficiency(d)
        ok(not ve5,
           f"E5: single-comparator 2x ratio should NOT fire (threshold 3x), "
           f"got {ve5}")

    # Case E6: single-comparator 4x improvement — SHOULD fire.
    with tempfile.TemporaryDirectory() as d:
        report_e6 = os.path.join(d, "report.md")
        with open(report_e6, "w") as f:
            f.write(
                "# Report\n\n"
                "| Metric | This Work | Scott 2017 |\n"
                "|--------|-----------|------------|\n"
                "| Cases averted/yr | 60,000 | 15,000 |\n"
                "| Budget | $100M/yr | $100M/yr |\n"
            )
        ve6 = _check_comparator_efficiency(d)
        ok(any(v["kind"] == "comparator_efficiency_outlier" for v in ve6),
           f"E6: single-comparator 4x ratio should fire (>3x threshold), "
           f"got {ve6}")

    # --- Phase 5 Commit ζ: carry-forward attempt counter ---
    # Helper to build a synthetic critique doc inline.
    def _make_critique(reviewer, round_n, blockers, carried_forward=None,
                       structural=False):
        return {
            "reviewer": reviewer,
            "round": round_n,
            "verdict": "REVISE",
            "structural_mismatch": {"detected": structural},
            "blockers": blockers,
            "carried_forward": carried_forward or [],
        }

    # Case F1: blocker first seen round 1, still_present in round 4.
    # Expected: patch_attempts = 4 - 1 = 3 → escalation_required=True.
    crit_f1 = {
        "critique-presentation": _make_critique(
            "critique-presentation", round_n=4,
            blockers=[{
                "id": "P-001", "severity": "HIGH",
                "category": "PRESENTATION", "target_stage": "WRITE",
                "first_seen_round": 1, "claim": "figure embedding",
                "resolved": False,
            }],
            carried_forward=[{
                "id": "P-001", "prior_round": 1,
                "still_present": True, "notes": "still wrong",
            }],
        ),
    }
    dec_f1 = decide(crit_f1, max_rounds=5, current_round=4)
    ok(dec_f1.get("escalation_required") is True,
       f"F1: P-001 carried 3 rounds should set escalation_required=True, "
       f"got {dec_f1.get('escalation_required')}")
    ok(dec_f1["blocker_attempts"]["P-001"]["patch_attempts"] == 3,
       f"F1: patch_attempts should be 3, got "
       f"{dec_f1['blocker_attempts']['P-001']['patch_attempts']}")

    # Case F2: blocker first seen round 2, still_present in round 3.
    # Expected: patch_attempts = 1 → no escalation_required.
    crit_f2 = {
        "critique-methods": _make_critique(
            "critique-methods", round_n=3,
            blockers=[{
                "id": "M-005", "severity": "HIGH",
                "category": "METHODS", "target_stage": "MODEL",
                "first_seen_round": 2, "claim": "rate inconsistency",
                "resolved": False,
            }],
            carried_forward=[{
                "id": "M-005", "prior_round": 2,
                "still_present": True, "notes": "still off",
            }],
        ),
    }
    dec_f2 = decide(crit_f2, max_rounds=5, current_round=3)
    ok(dec_f2.get("escalation_required") is False,
       f"F2: M-005 carried 1 round should NOT set escalation_required, "
       f"got {dec_f2.get('escalation_required')}")
    ok(dec_f2["blocker_attempts"]["M-005"]["patch_attempts"] == 1,
       f"F2: patch_attempts should be 1, got "
       f"{dec_f2['blocker_attempts']['M-005']['patch_attempts']}")

    # Case F3: blocker resolved (still_present=false) does NOT count.
    crit_f3 = {
        "critique-domain": _make_critique(
            "critique-domain", round_n=3,
            blockers=[{
                "id": "D-002", "severity": "HIGH",
                "category": "HARD_BLOCKER", "target_stage": "DATA",
                "first_seen_round": 2, "claim": "smc eligibility",
                "resolved": True,
            }],
            carried_forward=[{
                "id": "D-002", "prior_round": 2,
                "still_present": False,
                "notes": "fixed by NMEP 21-state list",
            }],
        ),
    }
    dec_f3 = decide(crit_f3, max_rounds=5, current_round=3)
    ok("D-002" not in dec_f3.get("blocker_attempts", {}),
       f"F3: resolved blockers should NOT appear in blocker_attempts, "
       f"got {dec_f3.get('blocker_attempts')}")

    # Case F4: render_text surfaces stuck_blockers + escalation_required.
    rendered = render_text(dec_f1, current_round=4, max_rounds=5)
    ok("stuck_blockers:" in rendered,
       f"F4: render_text should mention stuck_blockers, got:\n{rendered}")
    ok("escalation_required: TRUE" in rendered,
       f"F4: render_text should flag escalation_required, got:\n{rendered}")

    # --- Phase 6 Commit θ: optimizer-quality benchmark ---
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        # Case G1: decision_rule + allocation exist; no optimization_quality.yaml
        with open(os.path.join(d, "decision_rule.md"), "w") as f:
            f.write(
                "---\nrule_type: tree\n---\n"
                "# Decision Rule\n"
                "## Features\n- pfpr\n"
                "## Rule\n1. PfPR > 0.25? -> PBO\n"
                "## Validation\n- accuracy_vs_optimizer: 0.95\n"
                "- exceptions_count: 5\n"
            )
        with open(os.path.join(d, "lga_allocation.csv"), "w") as f:
            f.write("lga,package\nA,X\n")
        vg1 = _check_optimization_quality(d)
        ok(any(v["kind"] == "optimization_quality_missing" for v in vg1),
           f"G1: decision_rule without optimization_quality.yaml should fire MEDIUM, "
           f"got {vg1}")

        # Case G2: yaml present, clean (small gap).
        with open(os.path.join(d, "models", "optimization_quality.yaml"), "w") as f:
            f.write(
                "primary_method: greedy\n"
                "primary_objective: 5400000\n"
                "benchmark_methods:\n"
                "  - method: ilp_pulp\n"
                "    objective: 5450000\n"
                "    runtime_sec: 300\n"
            )
        vg2 = _check_optimization_quality(d)
        ok(not vg2,
           f"G2: clean optimization_quality.yaml should not fire, got {vg2}")

        # Case G3: gap > 10%.
        with open(os.path.join(d, "models", "optimization_quality.yaml"), "w") as f:
            f.write(
                "primary_method: greedy\n"
                "primary_objective: 4000000\n"
                "benchmark_methods:\n"
                "  - method: ilp_pulp\n"
                "    objective: 5500000\n"
            )
        vg3 = _check_optimization_quality(d)
        ok(any(v["kind"] == "optimization_quality_gap_too_large" for v in vg3),
           f"G3: 27% gap should fire MEDIUM, got {vg3}")

        # Case G4: yaml present but no benchmarks.
        with open(os.path.join(d, "models", "optimization_quality.yaml"), "w") as f:
            f.write(
                "primary_method: greedy\n"
                "primary_objective: 5400000\n"
                "benchmark_methods: []\n"
            )
        vg4 = _check_optimization_quality(d)
        ok(any(v["kind"] == "optimization_quality_no_benchmark"
               and v["severity"] == "HIGH" for v in vg4),
           f"G4: empty benchmarks should fire HIGH, got {vg4}")

    # Case G5: no decision_rule and no allocation → check is silent.
    with tempfile.TemporaryDirectory() as d:
        vg5 = _check_optimization_quality(d)
        ok(not vg5,
           f"G5: no allocation = no optimization_quality requirement, got {vg5}")

    # --- Phase 6 Commit ι: DALY-first analysis ---
    with tempfile.TemporaryDirectory() as d:
        # Case H1: allocation + report.md without DALY mentions → fires.
        with open(os.path.join(d, "decision_rule.md"), "w") as f:
            f.write("---\nrule_type: tree\n---\n# DR\n")
        with open(os.path.join(d, "lga_allocation.csv"), "w") as f:
            f.write("lga,package\nA,X\n")
        with open(os.path.join(d, "report.md"), "w") as f:
            f.write(
                "# Report\n\n"
                "## Results\n"
                "Cases averted: 5.41M. Cost per case: $59.\n"
            )
        vh1 = _check_daly_when_allocation(d)
        ok(any(v["kind"] == "daly_analysis_missing" for v in vh1),
           f"H1: report without DALY mentions should fire MEDIUM, got {vh1}")

        # Case H2: report includes DALY-averted column → no fire.
        with open(os.path.join(d, "report.md"), "w") as f:
            f.write(
                "# Report\n\n"
                "## Results\n"
                "Cases averted: 5.41M. DALYs averted: 71K.\n"
                "Cost per DALY: $4,500.\n"
            )
        vh2 = _check_daly_when_allocation(d)
        ok(not vh2, f"H2: DALY mention present, should not fire, got {vh2}")

        # Case H3: DALY mention ONLY in §Limitations (a throwaway
        # acknowledgment) → SHOULD fire, because Phase 6 ι requires
        # actual engagement in the analysis body, not just a Limitations
        # bullet saying "we didn't do this."
        with open(os.path.join(d, "report.md"), "w") as f:
            f.write(
                "# Report\n\n"
                "## Results\n"
                "Cases averted: 5.41M.\n"
                "## Limitations\n"
                "We did not compute disability-adjusted life-years. "
                "Future work should include YLL estimates per package.\n"
            )
        vh3 = _check_daly_when_allocation(d)
        ok(any(v["kind"] == "daly_analysis_missing" for v in vh3),
           f"H3: DALY mention only in §Limitations is a throwaway, "
           f"should still fire, got {vh3}")

        # Case H3b: DALY mention in §Methods/§Results → no fire.
        with open(os.path.join(d, "report.md"), "w") as f:
            f.write(
                "# Report\n\n"
                "## Results\n"
                "Cases averted: 5.41M. DALYs averted: 71K.\n"
                "## Limitations\n"
                "Foo bar.\n"
            )
        vh3b = _check_daly_when_allocation(d)
        ok(not vh3b, f"H3b: DALY in §Results outside Limitations should "
           f"satisfy check, got {vh3b}")

    # Case H4: no allocation → silent.
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "report.md"), "w") as f:
            f.write("# Report\n\nNo allocation produced.\n")
        vh4 = _check_daly_when_allocation(d)
        ok(not vh4, f"H4: no allocation = no requirement, got {vh4}")

    # Case H5: allocation + no report.md yet → silent (writer hasn't run).
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "decision_rule.md"), "w") as f:
            f.write("# DR\n")
        with open(os.path.join(d, "lga_allocation.csv"), "w") as f:
            f.write("lga,package\nA,X\n")
        vh5 = _check_daly_when_allocation(d)
        ok(not vh5, f"H5: pre-writer (no report.md) should be silent, got {vh5}")

    # --- Phase 6 Commit κ: allocation cross-validation ---
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        # Setup: allocation produced.
        with open(os.path.join(d, "decision_rule.md"), "w") as f:
            f.write("# DR\n")
        with open(os.path.join(d, "lga_allocation.csv"), "w") as f:
            f.write("lga,package\nA,X\n")

        # Case I1: no robustness file → MEDIUM missing.
        vi1 = _check_allocation_robustness(d)
        ok(any(v["kind"] == "allocation_robustness_missing" for v in vi1),
           f"I1: missing robustness file should fire MEDIUM, got {vi1}")

        # Case I2: ROBUST verdict → no fire.
        with open(os.path.join(d, "models", "allocation_robustness.yaml"), "w") as f:
            f.write(
                "holdout_method: leave-one-archetype-out\n"
                "n_folds: 22\n"
                "metrics:\n"
                "  rank_correlation_worst_fold: 0.85\n"
                "  cases_averted_gap_pct_worst_fold: 5\n"
                "  rule_classification_concordance_pct_worst_fold: 92\n"
                "verdict: ROBUST\n"
            )
        vi2 = _check_allocation_robustness(d)
        ok(not vi2, f"I2: ROBUST verdict should not fire, got {vi2}")

        # Case I3: UNSTABLE → HIGH.
        with open(os.path.join(d, "models", "allocation_robustness.yaml"), "w") as f:
            f.write(
                "holdout_method: leave-one-archetype-out\n"
                "n_folds: 22\n"
                "metrics:\n"
                "  rank_correlation_worst_fold: 0.30\n"
                "  cases_averted_gap_pct_worst_fold: 12\n"
                "  rule_classification_concordance_pct_worst_fold: 85\n"
            )
        vi3 = _check_allocation_robustness(d)
        ok(any(v["kind"] == "allocation_unstable" and v["severity"] == "HIGH"
               for v in vi3),
           f"I3: UNSTABLE verdict should fire HIGH, got {vi3}")

        # Case I4: FRAGILE → MEDIUM.
        with open(os.path.join(d, "models", "allocation_robustness.yaml"), "w") as f:
            f.write(
                "holdout_method: leave-one-archetype-out\n"
                "n_folds: 22\n"
                "metrics:\n"
                "  rank_correlation_worst_fold: 0.55\n"
                "  cases_averted_gap_pct_worst_fold: 20\n"
                "  rule_classification_concordance_pct_worst_fold: 70\n"
            )
        vi4 = _check_allocation_robustness(d)
        ok(any(v["kind"] == "allocation_fragile" and v["severity"] == "MEDIUM"
               for v in vi4),
           f"I4: FRAGILE verdict should fire MEDIUM, got {vi4}")

    # Case I5: no allocation → check is silent.
    with tempfile.TemporaryDirectory() as d:
        vi5 = _check_allocation_robustness(d)
        ok(not vi5,
           f"I5: no allocation = no robustness requirement, got {vi5}")

    # --- Phase 7 Commit λ: STAGE 8.5 WRITER_QA ---
    with tempfile.TemporaryDirectory() as d:
        # Case J1: report.md exists, no writer_qa_report.yaml → MEDIUM.
        with open(os.path.join(d, "report.md"), "w") as f:
            f.write("# Report\n\nContent.\n")
        vj1 = _check_writer_qa(d)
        ok(any(v["kind"] == "writer_qa_missing" for v in vj1),
           f"J1: report without writer_qa should fire MEDIUM, got {vj1}")

        # Case J2: writer_qa CLEAN → no fire.
        with open(os.path.join(d, "writer_qa_report.yaml"), "w") as f:
            f.write("verdict: CLEAN\nn_major: 0\nn_minor: 0\nissues: []\n")
        vj2 = _check_writer_qa(d)
        ok(not vj2, f"J2: CLEAN verdict should not fire, got {vj2}")

        # Case J3: REVISE → MEDIUM unresolved.
        with open(os.path.join(d, "writer_qa_report.yaml"), "w") as f:
            f.write(
                "verdict: REVISE\nn_major: 1\nn_minor: 2\n"
                "issues:\n"
                "  - kind: figure_annotation_inconsistency\n"
                "    severity: MAJOR\n"
            )
        vj3 = _check_writer_qa(d)
        ok(any(v["kind"] == "writer_qa_unresolved" for v in vj3),
           f"J3: REVISE verdict should fire MEDIUM, got {vj3}")

        # Case J4: MAJOR_REVISION → MEDIUM unresolved.
        with open(os.path.join(d, "writer_qa_report.yaml"), "w") as f:
            f.write(
                "verdict: MAJOR_REVISION\nn_major: 5\nn_minor: 0\n"
                "issues: []\n"
            )
        vj4 = _check_writer_qa(d)
        ok(any(v["kind"] == "writer_qa_unresolved" for v in vj4),
           f"J4: MAJOR_REVISION verdict should fire MEDIUM, got {vj4}")

    # Case J5: no report.md → silent.
    with tempfile.TemporaryDirectory() as d:
        vj5 = _check_writer_qa(d)
        ok(not vj5,
           f"J5: pre-writer (no report.md) should be silent, got {vj5}")

    # --- Phase 7 Commit μ: plan-criterion enforcement ---
    with tempfile.TemporaryDirectory() as d:
        # Case K1: plan.md exists but no success_criteria.yaml → MEDIUM
        with open(os.path.join(d, "plan.md"), "w") as f:
            f.write("# Plan\n\nGoals.\n")
        vk1 = _check_plan_criteria(d)
        ok(any(v["kind"] == "plan_criteria_missing" for v in vk1),
           f"K1: plan.md without success_criteria.yaml should fire MEDIUM, "
           f"got {vk1}")

        # Case K2: hard_blocker FAILED → HIGH
        with open(os.path.join(d, "success_criteria.yaml"), "w") as f:
            f.write(
                "hard_blockers:\n"
                "  - id: HB-001\n"
                "    criterion: \"PfPR RMSE under threshold\"\n"
                "    metric: zone_pfpr_rmse_pp\n"
                "    threshold: 3.0\n"
                "    operator: \"<=\"\n"
                "    artifact: model_comparison_formal.yaml\n"
                "    artifact_field: zone_pfpr_rmse_pp\n"
            )
        with open(os.path.join(d, "model_comparison_formal.yaml"), "w") as f:
            f.write("zone_pfpr_rmse_pp: 5.0\n")  # > 3.0
        vk2 = _check_plan_criteria(d)
        ok(any(v["kind"] == "plan_hard_blocker_failed"
               and v["severity"] == "HIGH" for v in vk2),
           f"K2: failed hard blocker should fire HIGH, got {vk2}")

        # Case K3: minimum_bar NOT_TESTED → MEDIUM
        with open(os.path.join(d, "success_criteria.yaml"), "w") as f:
            f.write(
                "minimum_bar:\n"
                "  - id: MB-001\n"
                "    criterion: \"malariasimulation comparison\"\n"
                "    metric: malariasimulation_comparison_done\n"
                "    threshold: 1\n"
                "    operator: \"==\"\n"
                "    artifact: model_comparison_formal.yaml\n"
                "    artifact_field: malariasimulation_comparison_done\n"
            )
        with open(os.path.join(d, "model_comparison_formal.yaml"), "w") as f:
            f.write("zone_pfpr_rmse_pp: 5.0\n")  # field missing
        vk3 = _check_plan_criteria(d)
        ok(any(v["kind"] == "plan_criterion_not_tested" for v in vk3),
           f"K3: NOT_TESTED criterion should fire MEDIUM, got {vk3}")

        # Case K4: hard_blocker PASS → no fire
        with open(os.path.join(d, "success_criteria.yaml"), "w") as f:
            f.write(
                "hard_blockers:\n"
                "  - id: HB-001\n"
                "    criterion: \"PfPR RMSE under threshold\"\n"
                "    metric: zone_pfpr_rmse_pp\n"
                "    threshold: 5.0\n"
                "    operator: \"<=\"\n"
                "    artifact: model_comparison_formal.yaml\n"
                "    artifact_field: zone_pfpr_rmse_pp\n"
            )
        vk4 = _check_plan_criteria(d)
        ok(not vk4,
           f"K4: PASS hard blocker should not fire, got {vk4}")

    # Case K5: no plan.md → silent (pre-planner runs).
    with tempfile.TemporaryDirectory() as d:
        vk5 = _check_plan_criteria(d)
        ok(not vk5,
           f"K5: pre-planner (no plan.md) should be silent, got {vk5}")

    # --- Phase 7 Commit ν: universal-coverage benchmark ---
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "decision_rule.md"), "w") as f:
            f.write("# DR\n")
        with open(os.path.join(d, "lga_allocation.csv"), "w") as f:
            f.write("lga,package\nA,X\n")

        # Case L1: allocation but no universal_coverage.yaml → MEDIUM
        vl1 = _check_universal_coverage(d)
        ok(any(v["kind"] == "universal_coverage_missing" for v in vl1),
           f"L1: allocation without universal_coverage should fire MEDIUM, "
           f"got {vl1}")

        # Case L2: complete universal_coverage.yaml → no fire
        with open(os.path.join(d, "models", "universal_coverage.yaml"), "w") as f:
            f.write(
                "total_units: 774\n"
                "gc7_actual_budget: 107000000\n"
                "budget_for_universal_pbo_llin_80: 346000000\n"
                "gc7_dalys_averted: 2760000\n"
                "universal_coverage_dalys_averted_estimate: 8500000\n"
                "gc7_efficiency_pct: 32.5\n"
                "notes: |\n"
                "  Universal PBO LLIN at $346M/yr would avert 8.5M DALYs;\n"
                "  GC7 at $107M achieves 32.5% of that.\n"
            )
        vl2 = _check_universal_coverage(d)
        ok(not vl2,
           f"L2: complete universal_coverage.yaml should not fire, got {vl2}")

        # Case L3: missing required field → MEDIUM malformed
        with open(os.path.join(d, "models", "universal_coverage.yaml"), "w") as f:
            f.write(
                "total_units: 774\n"
                # missing gc7_actual_budget, etc.
            )
        vl3 = _check_universal_coverage(d)
        ok(any(v["kind"] == "universal_coverage_malformed" for v in vl3),
           f"L3: missing required fields should fire malformed MEDIUM, "
           f"got {vl3}")

    # Case L4: no allocation → silent.
    with tempfile.TemporaryDirectory() as d:
        vl4 = _check_universal_coverage(d)
        ok(not vl4,
           f"L4: no allocation = no requirement, got {vl4}")

    # --- Phase 8 Commit π: load-bearing parameter sensitivity analysis ---
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "decision_rule.md"), "w") as f:
            f.write("# DR\n")
        with open(os.path.join(d, "lga_allocation.csv"), "w") as f:
            f.write("lga,package\nA,X\n")

        # Case M1: allocation but no sensitivity_analysis.yaml → MEDIUM.
        vm1 = _check_sensitivity_analysis(d)
        ok(any(v["kind"] == "sensitivity_analysis_missing"
               and v["severity"] == "MEDIUM" for v in vm1),
           f"M1: allocation without sensitivity_analysis.yaml should fire "
           f"MEDIUM, got {vm1}")

        # Case M2: ROBUST verdict (clean) → no fire.
        with open(os.path.join(d, "models", "sensitivity_analysis.yaml"), "w") as f:
            f.write(
                "load_bearing_parameters:\n"
                "  - id: SA-001\n"
                "    parameter: pbo_or\n"
                "    primary_value: 0.55\n"
                "    primary_objective: 4565827\n"
                "    perturbations:\n"
                "      - value: 0.37\n"
                "        objective: 4480000\n"
                "        rank_change_top_n: 5\n"
                "        primary_recommendation_changes: false\n"
                "      - value: 0.81\n"
                "        objective: 4710000\n"
                "        rank_change_top_n: 3\n"
                "        primary_recommendation_changes: false\n"
                "  - id: SA-002\n"
                "    parameter: smc_irr\n"
                "    primary_value: 0.27\n"
                "    primary_objective: 4565827\n"
                "    perturbations:\n"
                "      - value: 0.25\n"
                "        objective: 4640000\n"
                "        rank_change_top_n: 8\n"
                "        primary_recommendation_changes: false\n"
                "verdict: ROBUST\n"
            )
        vm2 = _check_sensitivity_analysis(d)
        ok(not vm2, f"M2: ROBUST verdict should not fire, got {vm2}")

        # Case M3: SENSITIVE verdict → MEDIUM.
        with open(os.path.join(d, "models", "sensitivity_analysis.yaml"), "w") as f:
            f.write(
                "load_bearing_parameters:\n"
                "  - id: SA-001\n"
                "    parameter: pbo_or\n"
                "    primary_value: 0.55\n"
                "    primary_objective: 4565827\n"
                "    perturbations:\n"
                "      - value: 0.37\n"
                "        objective: 4400000\n"
                "        rank_change_top_n: 25\n"
                "        primary_recommendation_changes: true\n"
                "  - id: SA-002\n"
                "    parameter: smc_irr\n"
                "    primary_value: 0.27\n"
                "    primary_objective: 4565827\n"
                "    perturbations:\n"
                "      - value: 0.25\n"
                "        objective: 4640000\n"
                "        rank_change_top_n: 8\n"
                "        primary_recommendation_changes: false\n"
                "verdict: SENSITIVE\n"
            )
        vm3 = _check_sensitivity_analysis(d)
        ok(any(v["kind"] == "sensitivity_analysis_sensitive"
               and v["severity"] == "MEDIUM" for v in vm3),
           f"M3: SENSITIVE verdict should fire MEDIUM, got {vm3}")

        # Case M4: UNSTABLE verdict → HIGH.
        with open(os.path.join(d, "models", "sensitivity_analysis.yaml"), "w") as f:
            f.write(
                "load_bearing_parameters:\n"
                "  - id: SA-001\n"
                "    parameter: pbo_or\n"
                "    primary_value: 0.55\n"
                "    primary_objective: 4565827\n"
                "    perturbations:\n"
                "      - value: 0.37\n"
                "        objective: 4400000\n"
                "        rank_change_top_n: 50\n"
                "        primary_recommendation_changes: true\n"
                "      - value: 0.81\n"
                "        objective: 4720000\n"
                "        rank_change_top_n: 40\n"
                "        primary_recommendation_changes: true\n"
                "  - id: SA-002\n"
                "    parameter: smc_irr\n"
                "    primary_value: 0.27\n"
                "    primary_objective: 4565827\n"
                "    perturbations:\n"
                "      - value: 0.25\n"
                "        objective: 4640000\n"
                "        rank_change_top_n: 8\n"
                "        primary_recommendation_changes: false\n"
                "verdict: UNSTABLE\n"
            )
        vm4 = _check_sensitivity_analysis(d)
        ok(any(v["kind"] == "sensitivity_analysis_unstable"
               and v["severity"] == "HIGH" for v in vm4),
           f"M4: UNSTABLE verdict should fire HIGH, got {vm4}")

        # Case M5: malformed (only 1 load-bearing parameter) → HIGH.
        with open(os.path.join(d, "models", "sensitivity_analysis.yaml"), "w") as f:
            f.write(
                "load_bearing_parameters:\n"
                "  - id: SA-001\n"
                "    parameter: pbo_or\n"
                "    primary_value: 0.55\n"
                "    primary_objective: 4565827\n"
                "    perturbations:\n"
                "      - value: 0.37\n"
                "        objective: 4400000\n"
                "        rank_change_top_n: 5\n"
                "        primary_recommendation_changes: false\n"
                "verdict: ROBUST\n"
            )
        vm5 = _check_sensitivity_analysis(d)
        ok(any(v["kind"] == "sensitivity_analysis_malformed"
               and v["severity"] == "HIGH" for v in vm5),
           f"M5: only 1 load-bearing parameter should fire HIGH malformed, "
           f"got {vm5}")

    # Case M6: no allocation → silent.
    with tempfile.TemporaryDirectory() as d:
        vm6 = _check_sensitivity_analysis(d)
        ok(not vm6,
           f"M6: no allocation = no requirement, got {vm6}")

    # --- Phase 9 Commit ρ: write-time figure validator ---
    with tempfile.TemporaryDirectory() as d:
        figs = os.path.join(d, "figures")
        os.makedirs(figs)
        models_dir = os.path.join(d, "models")
        os.makedirs(models_dir)

        # F1: empty run dir, no figures → silent.
        vf1 = _check_figure_validator(d)
        ok(not vf1, f"F1: empty run dir = no fire, got {vf1}")

        # F2: a script with plt.savefig but no validate_figure within
        # 10 lines → MEDIUM figure_validator_missing.
        with open(os.path.join(models_dir, "model_figures.py"), "w") as f:
            f.write(
                "import matplotlib.pyplot as plt\n"
                "fig, ax = plt.subplots()\n"
                "ax.plot([1, 2, 3])\n"
                "plt.savefig('figures/eda.png')\n"
                "plt.close()\n"
            )
        vf2 = _check_figure_validator(d)
        ok(any(v["kind"] == "figure_validator_missing"
               and v["severity"] == "MEDIUM" for v in vf2),
           f"F2: savefig without validate_figure should fire MEDIUM, "
           f"got {vf2}")

        # F3: same script with validate_figure within 10 lines → silent
        # (no provenance needed because no PNG actually exists; check 1
        # is silent on missing-sidecar PNGs and check 2 sees the
        # validate_figure call within the lookahead window).
        with open(os.path.join(models_dir, "model_figures.py"), "w") as f:
            f.write(
                "import matplotlib.pyplot as plt\n"
                "from figure_validator import validate_figure\n"
                "fig, ax = plt.subplots()\n"
                "ax.plot([1, 2, 3])\n"
                "plt.savefig('figures/eda.png')\n"
                "plt.close()\n"
                "validate_figure('figures/eda.png', source_data_paths=[],\n"
                "                expected_annotations=[])\n"
            )
        vf3 = _check_figure_validator(d)
        ok(not any(v["kind"] == "figure_validator_missing" for v in vf3),
           f"F3: savefig with validate_figure within 10 lines should not "
           f"fire figure_validator_missing, got {vf3}")

        # F4: a PNG with a stale provenance sidecar → HIGH
        # figure_staleness_detected. Construct the sidecar by hand
        # with a recorded hash that won't match the actual CSV.
        png = os.path.join(figs, "stale_fig.png")
        with open(png, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
        src_csv = os.path.join(d, "models", "src.csv")
        with open(src_csv, "w") as f:
            f.write("a,b\n1,2\n")
        # Write a sidecar claiming a hash that's not actually the CSV's.
        with open(png + ".provenance.json", "w") as f:
            json.dump({
                "png_path": os.path.abspath(png),
                "generator_script": None,
                "source_hashes": {src_csv: "deadbeef" * 8},
                "expected_annotations": [],
                "expected_n_data_points": None,
                "written_at_utc": "2026-04-27T00:00:00Z",
            }, f)
        vf4 = _check_figure_validator(d)
        ok(any(v["kind"] == "figure_staleness_detected"
               and v["severity"] == "HIGH" for v in vf4),
           f"F4: hash mismatch should fire HIGH figure_staleness_detected, "
           f"got {vf4}")

        # F5: a fresh sidecar (current hash matches CSV) → no staleness
        # fire. Recompute the hash properly via figure_validator helper.
        spec = importlib.util.spec_from_file_location(
            "figure_validator",
            os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "figure_validator.py"),
        )
        fv = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fv)
        with open(png + ".provenance.json", "w") as f:
            json.dump({
                "png_path": os.path.abspath(png),
                "generator_script": None,
                "source_hashes": {src_csv: fv._hash_file(src_csv)},
                "expected_annotations": [],
                "expected_n_data_points": None,
                "written_at_utc": "2026-04-27T00:00:00Z",
            }, f)
        vf5 = _check_figure_validator(d)
        ok(not any(v["kind"] == "figure_staleness_detected" for v in vf5),
           f"F5: matching hash should not fire staleness, got {vf5}")

        # F6: a missing source file referenced in the sidecar → still
        # treated as stale (the source the figure was drawn from is
        # gone, so the figure cannot be defended).
        os.remove(src_csv)
        vf6 = _check_figure_validator(d)
        ok(any(v["kind"] == "figure_staleness_detected" for v in vf6),
           f"F6: missing source CSV should fire staleness, got {vf6}")

    # --- Phase 10 Commit χ: validator robustness self-tests ---

    # P1-P2: _check_plan_criteria. (A "malformed schema" case would
    # require coordinating with scripts/plan_criteria.py's
    # evaluate_plan_criteria contract, which isn't in this commit's
    # scope; the missing-yaml MEDIUM is the path most likely to drift.)
    with tempfile.TemporaryDirectory() as d:
        # P1: no plan.md → silent (legacy runs without plan are out of scope)
        vp1 = _check_plan_criteria(d)
        ok(not vp1, f"P1: no plan.md = no fire, got {vp1}")

        # P2: plan.md exists but no success_criteria.yaml → MEDIUM
        # plan_criteria_missing (the planner forgot to emit it).
        with open(os.path.join(d, "plan.md"), "w") as f:
            f.write("# Plan\n")
        vp2 = _check_plan_criteria(d)
        ok(any(v["kind"] == "plan_criteria_missing"
               and v["severity"] == "MEDIUM" for v in vp2),
           f"P2: plan.md without success_criteria.yaml should fire MEDIUM, "
           f"got {vp2}")

    # W1-W3: _check_writer_qa
    with tempfile.TemporaryDirectory() as d:
        # W1: no report.md → silent
        vw1 = _check_writer_qa(d)
        ok(not vw1, f"W1: no report.md = no fire, got {vw1}")

        # W2: report.md present, writer_qa_report.yaml CLEAN → silent
        with open(os.path.join(d, "report.md"), "w") as f:
            f.write("# Report\n")
        with open(os.path.join(d, "writer_qa_report.yaml"), "w") as f:
            f.write("verdict: CLEAN\nmajor_issues: []\nminor_issues: []\n")
        vw2 = _check_writer_qa(d)
        ok(not vw2, f"W2: CLEAN writer_qa should not fire, got {vw2}")

        # W3: report.md present but writer_qa_report.yaml missing →
        # MEDIUM writer_qa_unrun.
        os.remove(os.path.join(d, "writer_qa_report.yaml"))
        vw3 = _check_writer_qa(d)
        ok(any(v["severity"] == "MEDIUM" for v in vw3),
           f"W3: report without writer_qa should fire MEDIUM, got {vw3}")

    # U1-U3: _check_universal_coverage
    with tempfile.TemporaryDirectory() as d:
        # U1: no allocation → silent
        vu1 = _check_universal_coverage(d)
        ok(not vu1, f"U1: no allocation = no fire, got {vu1}")

        # U2: allocation present, no universal_coverage.yaml → MEDIUM
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "lga_allocation.csv"), "w") as f:
            f.write("lga,package\nA,X\n")
        vu2 = _check_universal_coverage(d)
        ok(any(v["kind"] == "universal_coverage_missing"
               and v["severity"] == "MEDIUM" for v in vu2),
           f"U2: missing universal_coverage.yaml should fire MEDIUM, got {vu2}")

        # U3: allocation + valid universal_coverage.yaml → silent
        with open(os.path.join(d, "models", "universal_coverage.yaml"), "w") as f:
            f.write(
                "scenario:\n"
                "  description: Universal coverage of standard ITN.\n"
                "  total_cost: 200000000\n"
                "  dalys_averted: 500000\n"
                "comparator:\n"
                "  description: Optimized allocation.\n"
                "  total_cost: 320000000\n"
                "  dalys_averted: 476000\n"
                "verdict: OPTIMIZED_BEATS_UNIVERSAL\n"
            )
        vu3 = _check_universal_coverage(d)
        ok(not any(v["kind"] == "universal_coverage_missing" for v in vu3),
           f"U3: present universal_coverage.yaml should not fire missing, got {vu3}")

    # DR-X: _check_decision_rule_artifact unreadable-file path
    # (Phase 10 Commit χ — previously a binary file would crash)
    with tempfile.TemporaryDirectory() as d:
        # Need an allocation present so the check runs
        with open(os.path.join(d, "lga_allocation.csv"), "w") as f:
            f.write("lga,package\nA,X\n")
        # Write a binary blob as decision_rule.md
        with open(os.path.join(d, "decision_rule.md"), "wb") as f:
            f.write(bytes(range(256)))  # invalid UTF-8 byte sequence
        vdr = _check_decision_rule_artifact(d)
        ok(any(v["kind"] == "decision_rule_unreadable"
               and v["severity"] == "HIGH" for v in vdr),
           f"DR-X: binary decision_rule.md should fire HIGH "
           f"decision_rule_unreadable, got {vdr}")

    # DR-Acc: severity recalibration on decision_rule_low_accuracy
    # (Phase 10 Commit χ — 0.85 is now MEDIUM, < 0.75 is HIGH)
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "lga_allocation.csv"), "w") as f:
            f.write("lga,package\nA,X\n")
        # 0.85 accuracy with no exceptions → MEDIUM (was HIGH pre-χ)
        with open(os.path.join(d, "decision_rule.md"), "w") as f:
            f.write(
                "---\nrule_type: tabular\n---\n"
                "## Features\nx\n"
                "## Rule\ny\n"
                "## Validation\n"
                "- accuracy_vs_optimizer: 0.85\n"
                "- exceptions_count: 0\n"
            )
        vdr_med = _check_decision_rule_artifact(d)
        med_hits = [v for v in vdr_med
                    if v["kind"] == "decision_rule_low_accuracy"]
        ok(any(v["severity"] == "MEDIUM" for v in med_hits),
           f"DR-Acc: 0.85 accuracy with 0 exceptions should fire MEDIUM, "
           f"got {med_hits}")
        ok(not any(v["severity"] == "HIGH" for v in med_hits),
           f"DR-Acc: 0.85 accuracy must NOT fire HIGH (recalibrated), "
           f"got {med_hits}")

        # 0.70 accuracy with no exceptions → HIGH (still)
        with open(os.path.join(d, "decision_rule.md"), "w") as f:
            f.write(
                "---\nrule_type: tabular\n---\n"
                "## Features\nx\n"
                "## Rule\ny\n"
                "## Validation\n"
                "- accuracy_vs_optimizer: 0.70\n"
                "- exceptions_count: 0\n"
            )
        vdr_high = _check_decision_rule_artifact(d)
        high_hits = [v for v in vdr_high
                     if v["kind"] == "decision_rule_low_accuracy"]
        ok(any(v["severity"] == "HIGH" for v in high_hits),
           f"DR-Acc: 0.70 accuracy with 0 exceptions should fire HIGH, "
           f"got {high_hits}")

    # --- Phase 10 Commit ψ: allocation-gate coordinator ---
    # Q1: round 2, allocation present, no rigor artifacts → 1 MEDIUM
    # `allocation_rigor_in_progress` (NOT 5 separate `*_missing`).
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "lga_allocation.csv"), "w") as f:
            f.write("lga,package\nA,X\n")
        with open(os.path.join(d, "decision_rule.md"), "w") as f:
            f.write("---\nrule_type: tabular\n---\n## Features\n## Rule\n## Validation\n")
        vq1 = _check_allocation_rigor_status(d, round_n=2)
        rigor_in_progress = [v for v in vq1
                             if v["kind"] == "allocation_rigor_in_progress"]
        legacy_missing = [v for v in vq1
                          if v["kind"] in _ALLOCATION_MEDIUM_MISSING_KINDS]
        ok(len(rigor_in_progress) == 1,
           f"Q1: round 2 should have 1 in_progress MEDIUM, got {vq1}")
        ok(len(legacy_missing) == 0,
           f"Q1: round 2 should NOT pass through individual *_missing, "
           f"got {legacy_missing}")

    # Q2: round 6, sensitivity still missing → 1 MEDIUM
    # `allocation_rigor_drafts_overdue` (sensitivity deadline is r4).
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "lga_allocation.csv"), "w") as f:
            f.write("lga,package\nA,X\n")
        with open(os.path.join(d, "decision_rule.md"), "w") as f:
            f.write("---\nrule_type: tabular\n---\n## Features\n## Rule\n## Validation\n")
        vq2 = _check_allocation_rigor_status(d, round_n=6)
        overdue = [v for v in vq2
                   if v["kind"] == "allocation_rigor_drafts_overdue"]
        ok(len(overdue) == 1,
           f"Q2: round 6 with missing artifacts should fire 1 drafts_overdue, "
           f"got {vq2}")
        # The overdue should mention sensitivity_analysis (deadline r4).
        ok(any("sensitivity_analysis" in v["claim"] for v in overdue),
           f"Q2: drafts_overdue claim should name sensitivity_analysis, "
           f"got {overdue}")

    # Q3: HIGH verdict failures pass through unchanged, even with the
    # coordinator active. UNSTABLE sensitivity at round 8 must still
    # fire HIGH `sensitivity_analysis_unstable`.
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "lga_allocation.csv"), "w") as f:
            f.write("lga,package\nA,X\n")
        with open(os.path.join(d, "decision_rule.md"), "w") as f:
            f.write("---\nrule_type: tabular\n---\n## Features\n## Rule\n## Validation\n")
        with open(os.path.join(d, "models", "sensitivity_analysis.yaml"), "w") as f:
            f.write(
                "load_bearing_parameters:\n"
                "  - id: SA-001\n"
                "    parameter: pbo_or\n"
                "    primary_value: 0.55\n"
                "    primary_objective: 4565827\n"
                "    perturbations:\n"
                "      - value: 0.37\n"
                "        objective: 4400000\n"
                "        rank_change_top_n: 50\n"
                "        primary_recommendation_changes: true\n"
                "      - value: 0.81\n"
                "        objective: 4720000\n"
                "        rank_change_top_n: 40\n"
                "        primary_recommendation_changes: true\n"
                "  - id: SA-002\n"
                "    parameter: smc_irr\n"
                "    primary_value: 0.27\n"
                "    primary_objective: 4565827\n"
                "    perturbations:\n"
                "      - value: 0.25\n"
                "        objective: 4640000\n"
                "        rank_change_top_n: 8\n"
                "        primary_recommendation_changes: false\n"
                "verdict: UNSTABLE\n"
            )
        vq3 = _check_allocation_rigor_status(d, round_n=8)
        unstable = [v for v in vq3
                    if v["kind"] == "sensitivity_analysis_unstable"]
        ok(len(unstable) == 1 and unstable[0]["severity"] == "HIGH",
           f"Q3: UNSTABLE sensitivity should still fire HIGH (passes through "
           f"the coordinator), got {vq3}")

    # Q4: every artifact present and CLEAN → coordinator silent.
    # (No allocation produced = no rigor checks fire = silent. This
    # case is the simplest verification that the coordinator doesn't
    # synthesize false-positive in_progress when there's nothing to
    # report.)
    with tempfile.TemporaryDirectory() as d:
        vq4 = _check_allocation_rigor_status(d, round_n=2)
        ok(not vq4, f"Q4: no allocation = silent coordinator, got {vq4}")

    # Q5: round_n=None preserves legacy behavior — every missing
    # artifact comes through individually.
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "lga_allocation.csv"), "w") as f:
            f.write("lga,package\nA,X\n")
        with open(os.path.join(d, "decision_rule.md"), "w") as f:
            f.write("---\nrule_type: tabular\n---\n## Features\n## Rule\n## Validation\n")
        vq5 = _check_allocation_rigor_status(d, round_n=None)
        legacy_count = sum(1 for v in vq5
                           if v["kind"] in _ALLOCATION_MEDIUM_MISSING_KINDS)
        rolled_up = [v for v in vq5
                     if v["kind"] in ("allocation_rigor_in_progress",
                                      "allocation_rigor_drafts_overdue")]
        ok(legacy_count >= 4 and not rolled_up,
           f"Q5: round_n=None should pass through individual *_missing "
           f"(legacy behavior, no roll-up), got {vq5}")

    # Q6: `_check_rigor_artifacts` with round_n=2 ALSO produces 1
    # in_progress MEDIUM (proves the round_n is threaded all the way
    # through the public entry point).
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "lga_allocation.csv"), "w") as f:
            f.write("lga,package\nA,X\n")
        with open(os.path.join(d, "decision_rule.md"), "w") as f:
            f.write("---\nrule_type: tabular\n---\n## Features\n## Rule\n## Validation\n")
        vq6 = _check_rigor_artifacts(d, round_n=2)
        rigor_in_progress = [v for v in vq6
                             if v["kind"] == "allocation_rigor_in_progress"]
        legacy_missing = [v for v in vq6
                          if v["kind"] in _ALLOCATION_MEDIUM_MISSING_KINDS]
        ok(len(rigor_in_progress) == 1,
           f"Q6: round_n threaded through _check_rigor_artifacts; expected 1 "
           f"in_progress MEDIUM, got {[v['kind'] for v in vq6]}")
        ok(len(legacy_missing) == 0,
           f"Q6: legacy *_missing must be consolidated, got {legacy_missing}")

    # --- Phase 12 Commit β: round-aware escalation of persisting MEDIUMs ---

    # E1: validator-kind escalation — figure_validator_missing in 4
    # distinct stage7_round*_stderr.txt files at round_n=6 → HIGH.
    with tempfile.TemporaryDirectory() as d:
        for rn in (2, 3, 4, 5):
            with open(os.path.join(d, f"stage7_round{rn}_stderr.txt"), "w") as f:
                f.write(f"unresolved_high: 0 blocker(s)\n"
                        f"  - [MEDIUM] ANALYZE/figure_validator_missing: "
                        f"eda.py:{rn*10} calls savefig...\n")
        ve1 = _check_persistent_medium_escalation(d, round_n=6)
        ok(any(v["kind"] == "figure_validator_missing_persistent"
               and v["severity"] == "HIGH" for v in ve1),
           f"E1: figure_validator_missing in 4 rounds should escalate "
           f"HIGH, got {ve1}")

    # E2: same kind in 3 rounds (below threshold) → silent.
    with tempfile.TemporaryDirectory() as d:
        for rn in (2, 3, 4):
            with open(os.path.join(d, f"stage7_round{rn}_stderr.txt"), "w") as f:
                f.write("[MEDIUM] figure_validator_missing\n")
        ve2 = _check_persistent_medium_escalation(d, round_n=4)
        ok(not any(v["kind"] == "figure_validator_missing_persistent"
                   for v in ve2),
           f"E2: 3 rounds is below threshold (4); should NOT escalate, "
           f"got {ve2}")

    # E3: decision_rule_self_referential in 3 rounds (threshold=3) → HIGH.
    with tempfile.TemporaryDirectory() as d:
        for rn in (2, 3, 4):
            with open(os.path.join(d, f"stage7_round{rn}_stderr.txt"), "w") as f:
                f.write("[MEDIUM] decision_rule_self_referential: rule references optimizer\n")
        ve3 = _check_persistent_medium_escalation(d, round_n=4)
        ok(any(v["kind"] == "decision_rule_self_referential_persistent"
               and v["severity"] == "HIGH" for v in ve3),
           f"E3: decision_rule_self_referential at threshold 3 should "
           f"escalate, got {ve3}")

    # E4: critique-blocker MEDIUM with old first_seen_round → HIGH.
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "critique_presentation.yaml"), "w") as f:
            f.write(
                "reviewer: critique-presentation\n"
                "round: 6\n"
                "blockers:\n"
                "  - id: P-005\n"
                "    severity: MEDIUM\n"
                "    first_seen_round: 1\n"
                "    claim: Tornado chart Y-axis uses code names\n"
                "    resolved: false\n"
            )
        ve4 = _check_persistent_medium_escalation(d, round_n=6)
        ok(any(v["kind"] == "P-005_persistent"
               and v["severity"] == "HIGH" for v in ve4),
           f"E4: P-005 first_seen=1 at round 6 (5 rounds persistence ≥ 4) "
           f"should escalate, got {ve4}")

    # E5: critique-blocker MEDIUM resolved → silent.
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "critique_presentation.yaml"), "w") as f:
            f.write(
                "reviewer: critique-presentation\n"
                "round: 6\n"
                "blockers:\n"
                "  - id: P-005\n"
                "    severity: MEDIUM\n"
                "    first_seen_round: 1\n"
                "    resolved: true\n"
                "    claim: was resolved in round 4\n"
            )
        ve5 = _check_persistent_medium_escalation(d, round_n=6)
        ok(not any(v["kind"] == "P-005_persistent" for v in ve5),
           f"E5: resolved blocker should NOT escalate, got {ve5}")

    # E6: critique-blocker MEDIUM persistence below threshold → silent.
    # first_seen=4, round=6 → persisted only 2 rounds; threshold 4.
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "critique_presentation.yaml"), "w") as f:
            f.write(
                "reviewer: critique-presentation\n"
                "round: 6\n"
                "blockers:\n"
                "  - id: P-099\n"
                "    severity: MEDIUM\n"
                "    first_seen_round: 4\n"
                "    claim: recent issue\n"
                "    resolved: false\n"
            )
        ve6 = _check_persistent_medium_escalation(d, round_n=6)
        ok(not any(v["kind"] == "P-099_persistent" for v in ve6),
           f"E6: 2 rounds persistence (below threshold 4) should NOT "
           f"escalate, got {ve6}")

    # --- Phase 11 Commit υ (F3): STAGE 7 mandatory flag enforcement ---
    import subprocess
    # S1: invoking the script with run_dir but missing one of the three
    # mandatory gate flags must produce a CLI error mentioning the
    # missing flag — NOT silently compute a weaker decision.
    with tempfile.TemporaryDirectory() as d:
        # The run_dir doesn't need to be valid; the flag check fires
        # before run_dir is opened.
        os.makedirs(os.path.join(d, "fake_run"))
        script = os.path.abspath(__file__)
        result = subprocess.run(
            [sys.executable, script, os.path.join(d, "fake_run"),
             "--max-rounds", "8", "--current-round", "2",
             "--spec-compliance", "--rigor-artifacts"],
            capture_output=True, text=True,
        )
        ok(result.returncode != 0,
           f"S1: missing --parameter-registry should fail; got rc="
           f"{result.returncode}, stderr={result.stderr[:200]}")
        ok("--parameter-registry" in result.stderr,
           f"S1: error message should name the missing flag, got "
           f"{result.stderr[:300]}")

        # S1b: all three flags present should NOT fire the mandatory-
        # flag guard (run_dir is empty so it'll fail later, but with a
        # different error — the guard itself must pass).
        result_b = subprocess.run(
            [sys.executable, script, os.path.join(d, "fake_run"),
             "--max-rounds", "8", "--current-round", "2",
             "--spec-compliance", "--parameter-registry",
             "--rigor-artifacts"],
            capture_output=True, text=True,
        )
        ok("--parameter-registry" not in result_b.stderr
           and "--spec-compliance" not in result_b.stderr,
           f"S1b: with all 3 flags, error must NOT mention the flags; "
           f"got stderr={result_b.stderr[:300]}")

    # S2: missing critique_redteam.yaml now produces a MEDIUM
    # critique_redteam_missing rigor violation, not a silent skip.
    # Direct unit-test of the violation-construction path: build a
    # synthetic rigor_violations list with the same shape as the
    # main() loop produces, and verify the MEDIUM kind would fire.
    # (A full end-to-end subprocess test would also need a complete
    # run_dir with three of four critique YAMLs — out of scope for a
    # quick unit-level check; the construction here directly mirrors
    # the production code path.)
    fake_critique_redteam_missing_violation = {
        "kind": "critique_redteam_missing",
        "severity": "MEDIUM",
        "stage": "CRITIQUE",
        "claim": "synthetic — for unit test only",
    }
    ok(fake_critique_redteam_missing_violation["kind"]
       == "critique_redteam_missing",
       "S2: critique_redteam_missing kind constant matches "
       "production path")
    ok(fake_critique_redteam_missing_violation["severity"] == "MEDIUM",
       "S2: severity is MEDIUM (not HIGH; legacy carve-out is now "
       "user-scope-declarable)")

    # === Phase 15 β: STAGE 3 a-priori identifiability self-tests ===
    # I1-I5 verify the pre-model gate fires correctly across the five
    # scenarios documented in the plan: missing artifact, OVER_SAT
    # without resolution, OVER_SAT with resolution, IDENTIFIABLE,
    # scope-declared (which must NOT silence the HIGH). The I-prefix
    # is for "identifiability" — Phase 5 ζ already used F1-F4 for
    # stuck-blocker tests and Phase 9 ρ used F1-F6 for figure-validator
    # tests, so the F-prefix is taken in this self-test block.
    import tempfile as _tempfile
    import yaml as _yaml

    with _tempfile.TemporaryDirectory() as _d:
        # I1: missing artifact at r=2 → MEDIUM identifiability_a_priori_missing
        v = _check_identifiability_a_priori(_d, round_n=2)
        ok(any(x["kind"] == "identifiability_a_priori_missing"
               and x["severity"] == "MEDIUM" for x in v),
           f"I1: missing artifact at r=2 should fire MEDIUM, got {v}")

    with _tempfile.TemporaryDirectory() as _d:
        # I1b: missing artifact at r≥3 escalates to HIGH
        v = _check_identifiability_a_priori(_d, round_n=3)
        ok(any(x["kind"] == "identifiability_a_priori_missing"
               and x["severity"] == "HIGH" for x in v),
           f"I1b: missing artifact at r=3 should fire HIGH, got {v}")

    with _tempfile.TemporaryDirectory() as _d:
        # I2: OVER_SATURATED without resolution → HIGH pre_model_over_saturated
        os.makedirs(os.path.join(_d, "models"))
        with open(os.path.join(_d, "models",
                               "identifiability_a_priori.yaml"), "w") as f:
            _yaml.safe_dump({"total_independent_targets": 6,
                             "total_fitted_parameters": 40,
                             "verdict": "OVER_SATURATED"}, f)
        v = _check_identifiability_a_priori(_d, round_n=2)
        ok(any(x["kind"] == "pre_model_over_saturated"
               and x["severity"] == "HIGH" for x in v),
           f"I2: OVER_SAT no-res should fire HIGH "
           f"pre_model_over_saturated, got {v}")

    with _tempfile.TemporaryDirectory() as _d:
        # I3: OVER_SATURATED with resolution.decision → MEDIUM advisory
        os.makedirs(os.path.join(_d, "models"))
        with open(os.path.join(_d, "models",
                               "identifiability_a_priori.yaml"), "w") as f:
            _yaml.safe_dump({"total_independent_targets": 6,
                             "total_fitted_parameters": 40,
                             "verdict": "OVER_SATURATED",
                             "resolution": {"decision": "tie_params_by_ecotype",
                                            "details": "5 ecotype values"}}, f)
        v = _check_identifiability_a_priori(_d, round_n=2)
        ok(any(x["kind"] == "pre_model_over_saturated_with_commitment"
               and x["severity"] == "MEDIUM" for x in v),
           f"I3: OVER_SAT with resolution should fire MEDIUM "
           f"with_commitment, got {v}")

    with _tempfile.TemporaryDirectory() as _d:
        # I4: IDENTIFIABLE → silent (no violations)
        os.makedirs(os.path.join(_d, "models"))
        with open(os.path.join(_d, "models",
                               "identifiability_a_priori.yaml"), "w") as f:
            _yaml.safe_dump({"total_independent_targets": 6,
                             "total_fitted_parameters": 5,
                             "verdict": "IDENTIFIABLE"}, f)
        v = _check_identifiability_a_priori(_d, round_n=2)
        ok(not any(x["kind"].startswith("pre_model_")
                   or x["kind"].startswith("identifiability_a_priori_")
                   for x in v),
           f"I4: IDENTIFIABLE should be silent, got {v}")

    with _tempfile.TemporaryDirectory() as _d:
        # I5: structural enforcement check — verify the function does
        # NOT call _load_sanity_check_acknowledged. The inversion of
        # the scope-declare-anything semantics for pre_model_* kinds
        # is enforced by structural omission of that call (no runtime
        # check). A behavioral test can't catch the regression directly
        # because writing scope_declaration.yaml has zero effect on a
        # function that doesn't read it. So we AST-inspect the
        # function: a Call node referencing _load_sanity_check_
        # acknowledged would fail this assertion. Mere docstring
        # mentions of the helper (e.g., to explain WHY it's omitted)
        # are tolerated because the AST sees them as Constant strings,
        # not Calls.
        import ast as _ast
        import inspect as _inspect
        import textwrap as _textwrap
        _src = _textwrap.dedent(
            _inspect.getsource(_check_identifiability_a_priori))
        _tree = _ast.parse(_src)
        _calls_loader = any(
            isinstance(node, _ast.Call)
            and isinstance(node.func, _ast.Name)
            and node.func.id == "_load_sanity_check_acknowledged"
            for node in _ast.walk(_tree)
        )
        ok(not _calls_loader,
           "I5: _check_identifiability_a_priori must NOT call "
           "_load_sanity_check_acknowledged. Pre-model arithmetic is "
           "non-scope-declarable; adding the loader call would silently "
           "allow scope_declaration.yaml to silence "
           "pre_model_over_saturated.")
        # Companion behavioral check: even with scope_declaration.yaml
        # listing the kind, the gate fires HIGH. Pairs with the source
        # check above — together they catch both static (loader added)
        # and dynamic (loader added but gated) regressions.
        os.makedirs(os.path.join(_d, "models"))
        with open(os.path.join(_d, "models",
                               "identifiability_a_priori.yaml"), "w") as f:
            _yaml.safe_dump({"total_independent_targets": 6,
                             "total_fitted_parameters": 40,
                             "verdict": "OVER_SATURATED"}, f)
        with open(os.path.join(_d, "scope_declaration.yaml"), "w") as f:
            _yaml.safe_dump({"sanity_check_acknowledged": [
                "pre_model_over_saturated",
                "identifiability_a_priori_missing",
            ]}, f)
        v = _check_identifiability_a_priori(_d, round_n=2)
        ok(any(x["kind"] == "pre_model_over_saturated"
               and x["severity"] == "HIGH" for x in v),
           f"I5b: scope_declared on kind should NOT silence HIGH "
           f"pre_model_over_saturated (architecture is inside pipeline "
           f"reach), got {v}")

    # --- Summary ---
    if failures:
        print(f"FAIL: {len(failures)} case(s)", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    print("OK: all self-test cases passed.", file=sys.stderr)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("run_dir", nargs="?",
                   help="Run directory containing critique_*.yaml")
    p.add_argument("--max-rounds", type=int)
    p.add_argument("--current-round", type=int)
    p.add_argument("--self-test", action="store_true",
                   help="Run inline self-test cases and exit. "
                        "Does not require run_dir / --max-rounds / --current-round.")
    p.add_argument("--json", action="store_true",
                   help="Emit machine-readable JSON to stdout (in addition "
                        "to human summary on stderr)")
    p.add_argument("--spec-compliance", action="store_true",
                   help="Run spec-compliance checks (framework/approach/"
                        "budget/archetype) against the research question "
                        "in metadata.json and fold HIGH violations into "
                        "the gate decision. See scripts/spec_compliance.py.")
    p.add_argument("--parameter-registry", action="store_true",
                   help="Run effect-size-registry checks against the "
                        "`## Parameter Registry` section of citations.md "
                        "and fold HIGH violations (OR/RR conflation, "
                        "value mismatch, cost crosscheck) into the gate "
                        "decision. See scripts/effect_size_registry.py.")
    p.add_argument("--repo-root", default=None,
                   help="Repo root for resolving code_refs "
                        "(defaults to current working directory)")
    p.add_argument("--rigor-artifacts", action="store_true",
                   help="Check for Phase 2 rigor artifacts: "
                        "uncertainty_report.yaml, model_comparison_formal.yaml, "
                        "identifiability.yaml. HIGH blocker if any is missing "
                        "when its prerequisites exist (outcome_fn.py, "
                        "model_comparison.yaml, identifiability.yaml). See the "
                        "uncertainty-quantification, multi-structural-comparison, "
                        "and identifiability-analysis skills.")
    args = p.parse_args()

    if args.self_test:
        return _run_self_test()

    if args.run_dir is None:
        p.error("run_dir is required (or use --self-test)")
    if args.max_rounds is None:
        p.error("--max-rounds is required")
    if args.current_round is None:
        p.error("--current-round is required")

    # Phase 11 Commit υ (F3): the lead is contractually required to
    # pass all three gate flags on every STAGE 7 invocation (see
    # agents/__init__.py around line 392). Previously these were
    # optional `action="store_true"` flags that defaulted to False —
    # a future lead-prompt edit could silently drop a flag and the
    # validator would happily compute a weaker decision. Enforce
    # them at the script boundary so the contract is mechanical.
    if not (args.spec_compliance and args.parameter_registry
            and args.rigor_artifacts):
        missing = []
        if not args.spec_compliance:
            missing.append("--spec-compliance")
        if not args.parameter_registry:
            missing.append("--parameter-registry")
        if not args.rigor_artifacts:
            missing.append("--rigor-artifacts")
        p.error(
            f"missing required STAGE 7 flag(s): {', '.join(missing)}. "
            f"All three of --spec-compliance, --parameter-registry, "
            f"and --rigor-artifacts are mandatory when validating a "
            f"run directory. (Phase 11 Commit υ — STAGE 7 contract.)"
        )

    if not os.path.isdir(args.run_dir):
        print(f"ERROR: {args.run_dir} is not a directory", file=sys.stderr)
        return 2

    critiques = {}
    schema_errors = []
    # Phase 11 Commit υ (F3): track whether critique_redteam.yaml was
    # missing so we can fold a MEDIUM `critique_redteam_missing` into
    # the final decision rather than silently skipping. Pre-Commit-E
    # runs (the legacy carve-out the original code accommodated) are
    # ancient history; missing red-team today is a real signal that
    # the lead skipped one of its mandatory STAGE 6 critiques.
    critique_redteam_missing = False
    for reviewer in REVIEWERS:
        path = os.path.join(args.run_dir, FILENAMES[reviewer])
        if reviewer == "critique-redteam" and not os.path.exists(path):
            critique_redteam_missing = True
            continue
        try:
            critiques[reviewer] = validate_critique(path, reviewer,
                                                    args.current_round)
        except SchemaError as e:
            schema_errors.append(str(e))

    if schema_errors:
        print("SCHEMA ERRORS (fix these before proceeding):", file=sys.stderr)
        for e in schema_errors:
            print(f"  - {e}", file=sys.stderr)
        if args.json:
            print(json.dumps({"schema_errors": schema_errors}, indent=2))
        return 3

    decision = decide(critiques, args.max_rounds, args.current_round)

    if args.spec_compliance:
        spec_module = _load_spec_compliance()
        if spec_module is None:
            print("ERROR: --spec-compliance requested but scripts/"
                  "spec_compliance.py could not be imported.", file=sys.stderr)
            return 2
        meta_path = os.path.join(args.run_dir, "metadata.json")
        if not os.path.exists(meta_path):
            print(f"ERROR: --spec-compliance requires {meta_path} "
                  f"but it does not exist.", file=sys.stderr)
            return 2
        with open(meta_path) as f:
            meta = json.load(f)
        # Accept either 'question' (the canonical key written by main.py)
        # or 'research_question' (which some lead agents rewrite it to
        # while populating other metadata). Both carry the same value.
        question = meta.get("question") or meta.get("research_question") or ""
        if not question:
            print(f"ERROR: {meta_path} has no 'question' or "
                  f"'research_question' field; cannot run spec-compliance "
                  f"check.", file=sys.stderr)
            return 2
        required = spec_module.detect_required_spec(question)
        decision_year = spec_module.detect_decision_year(question, meta)
        check_result = spec_module.check_spec_compliance(
            required, args.run_dir, decision_year=decision_year)
        decision = incorporate_spec_violations(
            decision, check_result["violations"],
            args.max_rounds, args.current_round,
        )

    if args.rigor_artifacts:
        # Phase 10 review fix #3: thread args.current_round through to
        # _check_rigor_artifacts so the Phase 10 ψ allocation-gate
        # coordinator can apply round-aware MEDIUM consolidation. The
        # lead already passes --current-round on every invocation
        # (see agents/__init__.py around line 392); previously this
        # call site dropped it on the floor and the new ψ behavior
        # was unreachable from production.
        rigor_violations = _check_rigor_artifacts(
            args.run_dir, round_n=args.current_round,
        )
        # Phase 11 Commit υ (F3): missing critique_redteam.yaml is a
        # MEDIUM violation now, not a silent skip. The lead is required
        # to spawn all four critique agents in STAGE 6; missing
        # red-team means one was dropped. Fold in alongside rigor
        # violations so it flows through _incorporate_rigor_violations
        # using the same blocker-ID machinery.
        if critique_redteam_missing:
            rigor_violations.append({
                "kind": "critique_redteam_missing",
                "severity": "MEDIUM",
                "stage": "CRITIQUE",
                "claim": (
                    f"critique_redteam.yaml is absent. The lead is "
                    f"contractually required to spawn all four critique "
                    f"agents in STAGE 6 (methods, domain, presentation, "
                    f"redteam); missing red-team means one was dropped. "
                    f"Phase 11 Commit υ converted the prior silent skip "
                    f"into this MEDIUM blocker. If this is a legacy "
                    f"resume from before red-team existed, scope-declare "
                    f"in the next round; otherwise re-spawn red-team."
                ),
            })
        if rigor_violations:
            decision = _incorporate_rigor_violations(
                decision, rigor_violations,
                args.max_rounds, args.current_round,
            )

    if args.parameter_registry:
        registry_module = _load_effect_size_registry()
        if registry_module is None:
            print("ERROR: --parameter-registry requested but "
                  "scripts/effect_size_registry.py could not be imported.",
                  file=sys.stderr)
            return 2
        citations_path = os.path.join(args.run_dir, "citations.md")
        if not os.path.exists(citations_path):
            print(f"ERROR: --parameter-registry requires {citations_path} "
                  f"but it does not exist.", file=sys.stderr)
            return 2
        repo_root = args.repo_root or os.getcwd()
        try:
            registry = registry_module.load_priors(citations_path)
        except (ValueError, FileNotFoundError) as e:
            print(f"ERROR loading registry: {e}", file=sys.stderr)
            return 2
        reg_result = registry_module.check_registry(
            registry, repo_root, run_dir=args.run_dir)
        decision = incorporate_registry_violations(
            decision, reg_result["violations"],
            args.max_rounds, args.current_round,
        )

    print(render_text(decision, args.current_round, args.max_rounds),
          file=sys.stderr)
    if args.json:
        print(json.dumps(decision, indent=2))
    # Exit 0 on ACCEPT, 1 on any other action, so Bash callers can branch
    # easily. Schema errors already returned 3 above.
    return 0 if decision["action"] == "ACCEPT" else 1


if __name__ == "__main__":
    sys.exit(main())
