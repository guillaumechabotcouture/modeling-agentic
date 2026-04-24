#!/usr/bin/env python3
"""
Spec-compliance detection and checking.

Parses a research question for named frameworks, modeling approaches,
budget envelopes, and spatial-unit counts. Checks the delivered model
code in `{run_dir}/models/` and allocation CSVs in `{run_dir}/` against
those requirements. Emits structured violations that the STAGE 7 gate
can treat as a mechanical backstop when critique agents are generous.

Design principles:

- **Conservative parsing**: only match on specific, unambiguous phrases.
  False negatives (missing a framework mention) are preferable to false
  positives that would block legitimate runs. We use positive
  indicators ("using the X framework") rather than bare mentions
  ("benchmark against X") because the latter is common in research
  questions and does not require X.

- **Pure functions**: `detect_required_spec(question)` and
  `check_spec_compliance(required, run_dir)` are deterministic and
  idempotent so the validator can call them twice and get identical
  output.

- **No external judgments**: the module does NOT decide whether the
  model is "good." It only checks mechanical compliance with explicit
  requirements parsed from the question. A spec violation means "the
  delivered work does not satisfy a stated requirement," not "the
  science is wrong."

Usage as a library:

    from scripts.spec_compliance import (
        detect_required_spec, check_spec_compliance,
    )
    required = detect_required_spec(metadata["question"])
    violations = check_spec_compliance(required, run_dir)

Usage as a CLI (self-test):

    python3 scripts/spec_compliance.py --self-test
    python3 scripts/spec_compliance.py <run_dir>  # checks the run
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import re
import sys
from typing import Optional


# ---------------------------------------------------------------------------
# Question parsing
# ---------------------------------------------------------------------------

# Framework positive indicators. Each pattern is paired with the framework
# name it identifies. Bare mentions like "benchmark against EMOD" are NOT
# counted as a requirement — only "using X" / "with X" / "in X" phrasing.
_FRAMEWORK_PATTERNS = [
    # starsim
    (r"\busing\s+(?:the\s+)?starsim(?:\s+framework)?", "starsim"),
    (r"\bstarsim\s+framework\b", "starsim"),
    (r"\bwith\s+starsim\b", "starsim"),
    (r"\bimplement(?:ed)?\s+in\s+starsim\b", "starsim"),
    (r"\bbuilt\s+on\s+starsim\b", "starsim"),
    # laser (typically written uppercase in epi literature)
    (r"\busing\s+(?:the\s+)?LASER(?:\s+framework)?\b", "laser"),
    (r"\bLASER\s+framework\b", "laser"),
    (r"\bbuilt\s+on\s+LASER\b", "laser"),
    # stisim
    (r"\busing\s+(?:the\s+)?stisim(?:\s+framework)?", "stisim"),
    (r"\bstisim\s+framework\b", "stisim"),
    # emod
    (r"\busing\s+(?:the\s+)?EMOD(?:\s+framework)?\b", "emod"),
    (r"\bimplement(?:ed)?\s+in\s+EMOD\b", "emod"),
]

# Approach positive indicators.
_APPROACH_PATTERNS = [
    (r"\bagent[-\s]?based\s+(?:\w+\s+){0,2}model(?:s|ling|ing)?\b", "abm"),
    (r"\bABM\b", "abm"),
    (r"\bindividual[-\s]based\s+model\b", "abm"),
    (r"\bcompartmental\b", "compartmental"),
    (r"\bstochastic\s+(?:simulation|model)\b", "stochastic"),
    (r"\bdeterministic\s+(?:\w+\s+){0,2}model\b", "deterministic"),
    (r"\b(?:linear\s+)?regression\s+model\b", "regression"),
]

# Budget envelope: match "$320M", "$1.5B", "$320 million", "~$320M".
# We avoid matching plain "320 million" without a dollar sign to reduce
# false positives on non-budget numbers.
_BUDGET_PATTERN = re.compile(
    r"\$\s?(\d+(?:\.\d+)?)\s?"
    r"(?:([MmBbKk])(?:illion)?|million|billion|thousand)",
    re.IGNORECASE,
)

_BUDGET_SUFFIX_MULT = {
    "m": 1e6, "b": 1e9, "k": 1e3,
    "million": 1e6, "billion": 1e9, "thousand": 1e3,
}

# Spatial-unit counts: "774 LGAs", "36 states", "47 counties",
# "774 Local Government Areas". Allow up to 3 intervening words so the
# number-and-unit phrase can be connected by qualifiers.
_SPATIAL_PATTERN = re.compile(
    r"\b(\d{2,6})\s+(?:\w+\s+){0,3}"
    r"(LGAs?|Local\s+Government\s+Areas?|regions?|districts?|counties|"
    r"provinces|states|wards?|sub[-\s]?counties|municipalities)\b",
    re.IGNORECASE,
)

_ARCHETYPE_PATTERN = re.compile(r"\b(\d+)\s+archetypes?\b", re.IGNORECASE)

# Question-based decision-year hints (GF funding cycles, common target years).
_DECISION_YEAR_PATTERNS = [
    (re.compile(r"\bGC7\b", re.IGNORECASE), 2024),
    (re.compile(r"\bGC8\b", re.IGNORECASE), 2027),
    (re.compile(r"\bGC9\b", re.IGNORECASE), 2030),
    (re.compile(r"\bby\s+(20\d{2})\b", re.IGNORECASE), None),  # dynamic
    (re.compile(r"\bfor\s+(20\d{2})\b", re.IGNORECASE), None),
]


def detect_decision_year(question: str, metadata: Optional[dict] = None) -> Optional[int]:
    """Parse a decision year from the question + metadata.

    Priority:
      1. Explicit `decision_year` field in metadata.json (if present).
      2. GF funding cycle (GC7 → 2024, GC8 → 2027, GC9 → 2030).
      3. "by YYYY" / "for YYYY" numeric phrases.
      4. Year of metadata['started'] as fallback.
    Returns None when no decision year can be inferred.
    """
    if metadata:
        if "decision_year" in metadata and metadata["decision_year"]:
            try:
                return int(metadata["decision_year"])
            except (ValueError, TypeError):
                pass

    for pattern, year in _DECISION_YEAR_PATTERNS:
        m = pattern.search(question)
        if m:
            if year is not None:
                return year
            # Dynamic pattern: captured group is the year.
            try:
                return int(m.group(1))
            except (ValueError, IndexError):
                continue

    if metadata:
        for key in ("started", "created"):
            if key in metadata and metadata[key]:
                try:
                    return int(str(metadata[key])[:4])
                except (ValueError, TypeError):
                    pass
    return None


def detect_required_spec(question: str) -> dict:
    """Parse a research question for declared requirements.

    Returns a dict with keys:
        frameworks: list[str]     — deduplicated, lower-case
        approaches: list[str]     — deduplicated, lower-case
        budget_envelope: float | None   — in dollars
        budget_raw: str | None          — the matched phrase, for display
        spatial_units: tuple[int, str] | None  — (count, unit)
        archetype_spec: int | None
    """
    frameworks: list[str] = []
    for pattern, name in _FRAMEWORK_PATTERNS:
        if re.search(pattern, question, re.IGNORECASE):
            if name not in frameworks:
                frameworks.append(name)

    approaches: list[str] = []
    for pattern, name in _APPROACH_PATTERNS:
        if re.search(pattern, question, re.IGNORECASE):
            if name not in approaches:
                approaches.append(name)

    budget_envelope: Optional[float] = None
    budget_raw: Optional[str] = None
    m = _BUDGET_PATTERN.search(question)
    if m:
        number = float(m.group(1))
        suffix = (m.group(2) or "").lower() or m.group(0).lower()
        # Map suffix to multiplier. If group(2) is None, the word form
        # (million/billion/thousand) is inside group(0); parse it.
        mult = None
        if m.group(2):
            mult = _BUDGET_SUFFIX_MULT.get(m.group(2).lower())
        else:
            for word, w_mult in _BUDGET_SUFFIX_MULT.items():
                if len(word) > 1 and word in m.group(0).lower():
                    mult = w_mult
                    break
        if mult is not None:
            budget_envelope = number * mult
            budget_raw = m.group(0)

    spatial: Optional[tuple[int, str]] = None
    m = _SPATIAL_PATTERN.search(question)
    if m:
        try:
            n = int(m.group(1))
            if n >= 10:  # sanity filter
                raw_unit = m.group(2).lower()
                # Normalize multi-word phrases to canonical short forms.
                if "local government" in raw_unit:
                    unit = "lgas"
                elif raw_unit.endswith("ies"):  # counties, municipalities
                    unit = raw_unit
                elif not raw_unit.endswith("s"):
                    unit = raw_unit + "s"
                else:
                    unit = raw_unit
                spatial = (n, unit)
        except ValueError:
            pass

    archetype_spec: Optional[int] = None
    m = _ARCHETYPE_PATTERN.search(question)
    if m:
        try:
            archetype_spec = int(m.group(1))
        except ValueError:
            pass

    return {
        "frameworks": frameworks,
        "approaches": approaches,
        "budget_envelope": budget_envelope,
        "budget_raw": budget_raw,
        "spatial_units": spatial,
        "archetype_spec": archetype_spec,
    }


# ---------------------------------------------------------------------------
# Framework/approach checks against code
# ---------------------------------------------------------------------------

def _read_models_code(run_dir: str) -> dict[str, str]:
    """Return {relative_path: source} for all .py files under run_dir/models/."""
    models_dir = os.path.join(run_dir, "models")
    if not os.path.isdir(models_dir):
        return {}
    out: dict[str, str] = {}
    for path in sorted(glob.glob(os.path.join(models_dir, "**", "*.py"),
                                 recursive=True)):
        if "__pycache__" in path:
            continue
        try:
            with open(path) as f:
                out[os.path.relpath(path, run_dir)] = f.read()
        except OSError:
            continue
    return out


# Starsim "the framework is actually running" signals. We require both an
# import AND at least one signal that a Starsim Sim is constructed or run.
# Subclassing `ss.SIS` / `ss.Disease` alone does NOT count — the malaria
# run post-mortem showed that can be done while the real dynamics are in
# scipy.integrate.odeint.
_STARSIM_IMPORT = re.compile(
    r"^\s*(?:import\s+starsim\b|from\s+starsim\s+import)", re.MULTILINE
)
_STARSIM_RUN_SIGNALS = [
    (re.compile(r"\bss\.Sim\s*\("), "ss.Sim(...) construction"),
    (re.compile(r"\bstarsim\.Sim\s*\("), "starsim.Sim(...) construction"),
    (re.compile(r"\.run\s*\(\s*\)\s*(?:#.*)?$", re.MULTILINE), "sim.run() call"),
    (re.compile(r"\bss\.People\s*\("), "ss.People(...) construction"),
    (re.compile(r"\bstarsim\.People\s*\("), "starsim.People(...) construction"),
]

# Competing ODE-integration signals that would indicate the primary
# dynamics are compartmental rather than agent-based.
_ODE_SIGNALS = [
    (re.compile(r"\bscipy\.integrate\.odeint\b|\bfrom\s+scipy\.integrate\s+import[^\n]*\bodeint\b"), "scipy.integrate.odeint"),
    (re.compile(r"\bscipy\.integrate\.solve_ivp\b|\bfrom\s+scipy\.integrate\s+import[^\n]*\bsolve_ivp\b"), "scipy.integrate.solve_ivp"),
    (re.compile(r"\bodeint\s*\("), "odeint() call"),
    (re.compile(r"\bsolve_ivp\s*\("), "solve_ivp() call"),
]

# ABM signals (per-agent state/dynamics).
_ABM_SIGNALS = [
    (re.compile(r"\bss\.People\s*\("), "ss.People(...) individual-agent container"),
    (re.compile(r"\bstarsim\.People\s*\("), "starsim.People(...) individual-agent container"),
    (re.compile(r"^class\s+\w*(?:Agent|Person|Host|Individual)\b", re.MULTILINE),
     "per-agent class definition"),
]


def _scan(patterns, code: str) -> list[str]:
    hits = []
    for pattern, label in patterns:
        if pattern.search(code):
            hits.append(label)
    return hits


def _check_framework(framework: str, code_by_file: dict[str, str]) -> Optional[dict]:
    """Return a violation dict or None if compliant."""
    if framework == "starsim":
        any_import = False
        run_signals_found: list[str] = []
        for path, code in code_by_file.items():
            if _STARSIM_IMPORT.search(code):
                any_import = True
            run_signals_found.extend(
                f"{path}: {label}" for label in _scan(_STARSIM_RUN_SIGNALS, code)
            )
        if not any_import:
            return {
                "kind": "framework_missing",
                "required": "starsim",
                "severity": "HIGH",
                "evidence": "No `import starsim` or `from starsim import` "
                            "statements found in models/",
            }
        if not run_signals_found:
            # Starsim imported but never runs. Classic cosmetic-wrap pattern.
            return {
                "kind": "framework_missing",
                "required": "starsim",
                "severity": "HIGH",
                "evidence": "Starsim is imported but never instantiates a "
                            "simulation. No `ss.Sim(...)`, `ss.People(...)`, "
                            "`starsim.Sim(...)`, or `sim.run()` calls found. "
                            "Subclassing `ss.Disease` / `ss.SIS` alone is not "
                            "sufficient — the question requires running the "
                            "simulation through Starsim's framework.",
            }
        return None  # compliant

    if framework == "laser":
        for code in code_by_file.values():
            if re.search(r"\bimport\s+laser\b|\bfrom\s+laser\b", code):
                return None
        return {
            "kind": "framework_missing",
            "required": "laser",
            "severity": "HIGH",
            "evidence": "No `import laser` or `from laser` found in models/",
        }

    if framework == "stisim":
        for code in code_by_file.values():
            if re.search(r"\bimport\s+stisim\b|\bfrom\s+stisim\b", code):
                return None
        return {
            "kind": "framework_missing",
            "required": "stisim",
            "severity": "HIGH",
            "evidence": "No `import stisim` or `from stisim` found in models/",
        }

    if framework == "emod":
        for code in code_by_file.values():
            if re.search(r"\bimport\s+emod(?:_api)?\b|\bfrom\s+emod", code):
                return None
        return {
            "kind": "framework_missing",
            "required": "emod",
            "severity": "HIGH",
            "evidence": "No `import emod` / `from emod` found in models/",
        }

    # Unknown framework — no check.
    return None


def _check_abm(code_by_file: dict[str, str]) -> Optional[dict]:
    abm_hits: list[str] = []
    ode_hits: list[str] = []
    for path, code in code_by_file.items():
        for label in _scan(_ABM_SIGNALS, code):
            abm_hits.append(f"{path}: {label}")
        for label in _scan(_ODE_SIGNALS, code):
            ode_hits.append(f"{path}: {label}")

    if abm_hits and not ode_hits:
        return None  # clearly ABM

    if not abm_hits and ode_hits:
        return {
            "kind": "approach_mismatch",
            "required": "abm",
            "severity": "HIGH",
            "evidence": (
                "No agent-based indicators found (ss.People, per-agent class, "
                "etc.). Primary dynamics appear compartmental via: "
                + "; ".join(ode_hits[:3])
                + (f" (+{len(ode_hits)-3} more)" if len(ode_hits) > 3 else "")
            ),
        }

    if abm_hits and ode_hits:
        # Both present — possible hybrid. Heuristic: if ODE signals outnumber
        # ABM signals by >2x, call it ODE-dominant.
        if len(ode_hits) > 2 * len(abm_hits):
            return {
                "kind": "approach_mismatch",
                "required": "abm",
                "severity": "HIGH",
                "evidence": (
                    f"Found ABM indicators ({len(abm_hits)}) but "
                    f"ODE/compartmental signals ({len(ode_hits)}) dominate. "
                    "Primary dynamics run through ODE solver. Examples: "
                    + "; ".join(ode_hits[:2])
                ),
            }
        return None  # mixed but ABM signals are competitive

    # Neither — no evidence either way.
    return {
        "kind": "approach_mismatch",
        "required": "abm",
        "severity": "HIGH",
        "evidence": (
            "No agent-based indicators (ss.People, per-agent class) AND no "
            "clear simulation dynamics found in models/. Cannot verify "
            "model is agent-based."
        ),
    }


# ---------------------------------------------------------------------------
# Budget check
# ---------------------------------------------------------------------------

_COST_COLUMN_CANDIDATES = ("cost", "total_cost", "budget", "price",
                           "spend", "amount", "allocated_cost")


def _find_allocation_csvs(run_dir: str) -> list[str]:
    """Return CSV paths that look like allocation outputs."""
    candidates = []
    for pattern in ("*allocation*.csv", "*budget*.csv",
                    "*optimization*.csv", "optimizer*.csv"):
        candidates.extend(glob.glob(os.path.join(run_dir, pattern)))
        candidates.extend(glob.glob(os.path.join(run_dir, "data", pattern)))
    # Deduplicate preserving order.
    seen = set()
    return [p for p in candidates if not (p in seen or seen.add(p))]


def _sum_cost_column(csv_path: str) -> tuple[Optional[float], Optional[str]]:
    """Return (sum, column_name) or (None, None) if no cost column found."""
    try:
        with open(csv_path) as f:
            reader = csv.DictReader(f)
            rows = list(reader)
    except (OSError, csv.Error):
        return None, None
    if not rows:
        return None, None
    cols = list(rows[0].keys())
    # Prefer exact matches over substring matches.
    cost_col = None
    for candidate in _COST_COLUMN_CANDIDATES:
        if candidate in cols:
            cost_col = candidate
            break
    if cost_col is None:
        for col in cols:
            col_l = col.lower()
            if any(c in col_l for c in ("cost", "budget", "spend")):
                # Skip per-unit costs (cost_per_X columns) — those are ratios.
                if "per_" in col_l or "_per" in col_l:
                    continue
                cost_col = col
                break
    if cost_col is None:
        return None, None

    # Optionally restrict to "allocated=True" rows if the column exists.
    alloc_filter = None
    for col in cols:
        if col.lower() == "allocated":
            alloc_filter = col
            break
    total = 0.0
    for row in rows:
        if alloc_filter is not None:
            val = row.get(alloc_filter, "").strip().lower()
            if val not in ("true", "1", "yes"):
                continue
        raw = row.get(cost_col, "").strip()
        if not raw:
            continue
        try:
            total += float(raw)
        except ValueError:
            continue
    return total, cost_col


def _check_budget(budget_envelope: float, budget_raw: str,
                  run_dir: str) -> Optional[dict]:
    csvs = _find_allocation_csvs(run_dir)
    if not csvs:
        # No allocation CSV → can't check. This is not a violation; it may
        # be that the question names a budget but no optimization step
        # produced a CSV artifact yet.
        return None

    # Pick the largest (by cost sum) allocation we find.
    best: tuple[Optional[float], Optional[str], Optional[str]] = (None, None, None)
    for path in csvs:
        total, col = _sum_cost_column(path)
        if total is not None and (best[0] is None or total > best[0]):
            best = (total, col, path)

    total, col, path = best
    if total is None:
        return None

    utilization = total / budget_envelope
    if utilization >= 0.80:
        return None  # compliant

    return {
        "kind": "budget_underutilized",
        "required": budget_raw,
        "severity": "HIGH",
        "required_budget": budget_envelope,
        "actual_spend": total,
        "utilization": utilization,
        "evidence": (
            f"sum({col}) from {os.path.basename(path)} = ${total:,.0f} "
            f"= {utilization:.1%} of {budget_raw} envelope "
            f"(${budget_envelope:,.0f}). Threshold is 80%. "
            "An optimizer that leaves >20% of the stated budget "
            "unallocated is an artifact, not a finding."
        ),
    }


# ---------------------------------------------------------------------------
# Data vintage check (Phase 3 Commit B)
# ---------------------------------------------------------------------------

# Preferred structured marker going forward.
_VINTAGE_LABEL_RE = re.compile(
    r"^\*\*Vintage\*\*:\s*(\d{4})\b", re.MULTILINE
)

# Fallback: extract year from "**Temporal coverage**: ..." prose lines.
_TEMPORAL_COVERAGE_RE = re.compile(
    r"^\*\*Temporal coverage\*\*:\s*([^\n]+)$", re.MULTILINE
)
_YEAR_IN_LINE_RE = re.compile(r"\b(19|20)(\d{2})\b")

# Section headers in data_quality.md: `## N. filename.csv` or `## filename`.
_DATA_SECTION_RE = re.compile(
    r"^##\s+(?:\d+\.\s+)?(?P<name>[^\n]+?)\s*$", re.MULTILINE
)


def _parse_data_sections(data_quality_md: str) -> list[dict]:
    """Parse data_quality.md into per-section dicts with extracted vintage.

    Returns list of {name, start, end, body, vintage, vintage_source}.
    vintage_source is "structured" (from **Vintage**:) or "temporal" (from
    **Temporal coverage**:) or None (no vintage found).
    """
    sections = []
    headers = list(_DATA_SECTION_RE.finditer(data_quality_md))
    # Skip the first section if it's the document title (no dataset name).
    for i, hdr in enumerate(headers):
        name = hdr.group("name").strip()
        if name.lower().startswith("nigeria malaria") or "assessment" in name.lower():
            continue
        start = hdr.end()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(data_quality_md)
        body = data_quality_md[start:end]

        vintage = None
        source = None
        m = _VINTAGE_LABEL_RE.search(body)
        if m:
            vintage = int(m.group(1))
            source = "structured"
        else:
            tc = _TEMPORAL_COVERAGE_RE.search(body)
            if tc:
                years = [
                    int(match.group(0))
                    for match in _YEAR_IN_LINE_RE.finditer(tc.group(1))
                ]
                if years:
                    # Use the EARLIEST year mentioned — that's the base
                    # vintage of the data. If text says "2010-2021", the
                    # data starts at 2010.
                    vintage = min(years)
                    source = "temporal"

        sections.append({
            "name": name,
            "body": body,
            "vintage": vintage,
            "vintage_source": source,
        })
    return sections


_PRIMARY_LABEL_RE = re.compile(
    r"^\*\*Primary calibration\*\*:\s*(yes|no|true|false)\b",
    re.MULTILINE | re.IGNORECASE,
)


def _is_primary_calibration_section(section: dict) -> bool:
    """Prefer the structured `**Primary calibration**: yes/no` line when
    present. Fall back to prose heuristics for legacy files."""
    m = _PRIMARY_LABEL_RE.search(section["body"])
    if m:
        return m.group(1).lower() in ("yes", "true")
    body_low = section["body"].lower()
    return any(kw in body_low for kw in (
        "calibration target", "primary authority",
        "primary national survey",
    ))


def check_data_vintage(run_dir: str, decision_year: Optional[int]) -> list[dict]:
    """Check dataset vintages in data_quality.md against decision_year.

    Emits:
      - `data_vintage_stale` HIGH when gap ≥ 10 on a primary calibration target
      - `data_vintage_stale` MEDIUM when gap 5-9 (any dataset)
      - `vintage_unstructured` MEDIUM when data_quality.md exists but has
        no **Vintage** labels (going-forward contract)
    """
    violations = []
    if decision_year is None:
        return []  # Nothing to compare against.

    dq_path = os.path.join(run_dir, "data_quality.md")
    if not os.path.exists(dq_path):
        return []
    with open(dq_path) as f:
        text = f.read()

    sections = _parse_data_sections(text)
    if not sections:
        return []

    structured_count = sum(
        1 for s in sections if s["vintage_source"] == "structured"
    )
    if structured_count == 0 and sections:
        violations.append({
            "kind": "vintage_unstructured",
            "severity": "MEDIUM",
            "evidence": (
                f"data_quality.md has {len(sections)} dataset sections but "
                f"none carries a structured `**Vintage**: YYYY` line. "
                f"Data-agent must emit `**Vintage**: YYYY` per section."
            ),
        })

    for section in sections:
        if section["vintage"] is None:
            continue
        gap = decision_year - section["vintage"]
        if gap < 5:
            continue
        is_primary = _is_primary_calibration_section(section)
        if gap >= 10 and is_primary:
            severity = "HIGH"
        elif gap >= 10:
            severity = "MEDIUM"
        else:  # 5-9
            severity = "MEDIUM"
        violations.append({
            "kind": "data_vintage_stale",
            "severity": severity,
            "primary": is_primary,
            "vintage": section["vintage"],
            "gap": gap,
            "evidence": (
                f"{section['name']}: vintage {section['vintage']} "
                f"({section['vintage_source']}), decision year {decision_year}, "
                f"gap = {gap} years"
                + (" (primary calibration target)" if is_primary else "")
            ),
        })
    return violations


# ---------------------------------------------------------------------------
# Methodological vintage check (Phase 3 Commit B)
# ---------------------------------------------------------------------------

# Matches "following <Author YYYY>" or "archetype clustering per [CN]
# (<Author> YYYY)" — methodological-basis claims tied to cited papers.
_METHOD_CITE_RE = re.compile(
    r"\b(?:following|based\s+on|using\s+the\s+method\s+of|per|adapted\s+from|replic\w+\s+of)\s+"
    r"(?:the\s+)?(?:\w+\s+(?:et\s+al\.?\s+)?(\d{4})|\[C\d+\])",
    re.IGNORECASE,
)

_CITATION_YEAR_RE = re.compile(
    r"^##\s+\[(C\d+)\][^\n]*?(\d{4})", re.MULTILINE
)


def _extract_citation_years(citations_md: str) -> dict[str, int]:
    """Parse `## [CN] ... YYYY` headers for citation years."""
    out = {}
    for m in _CITATION_YEAR_RE.finditer(citations_md):
        out[m.group(1)] = int(m.group(2))
    return out


def check_methodological_vintage(run_dir: str,
                                 decision_year: Optional[int]) -> list[dict]:
    """If plan.md / modeling_strategy.md cites a methodological basis
    (e.g. 'archetype clustering per [C2]' or 'following Author YYYY')
    and the cited paper is ≥ 10 years before the decision year, flag as
    `methodology_vintage_stale` MEDIUM. Cheap signal — doesn't require
    reading the cited paper.
    """
    violations = []
    if decision_year is None:
        return []

    citations_md = os.path.join(run_dir, "citations.md")
    citation_years: dict[str, int] = {}
    if os.path.exists(citations_md):
        with open(citations_md) as f:
            citation_years = _extract_citation_years(f.read())

    for md_name in ("plan.md", "modeling_strategy.md"):
        path = os.path.join(run_dir, md_name)
        if not os.path.exists(path):
            continue
        with open(path) as f:
            text = f.read()
        for m in _METHOD_CITE_RE.finditer(text):
            # Either a year was captured directly (\d{4}) or a [CN] ref.
            year = None
            if m.group(1):
                try:
                    year = int(m.group(1))
                except ValueError:
                    continue
            else:
                # Extract [CN] from the full match.
                cn = re.search(r"\[(C\d+)\]", m.group(0))
                if cn and cn.group(1) in citation_years:
                    year = citation_years[cn.group(1)]
            if year is None:
                continue
            gap = decision_year - year
            if gap >= 10:
                # Capture 40 chars of context for evidence.
                context = text[max(0, m.start() - 20):m.end() + 20]
                violations.append({
                    "kind": "methodology_vintage_stale",
                    "severity": "MEDIUM",
                    "evidence": (
                        f"{md_name}: methodological basis cited from "
                        f"{year} ({gap}-year gap vs decision year "
                        f"{decision_year}). Context: '{context.strip()}...'"
                    ),
                })
    return violations


# ---------------------------------------------------------------------------
# Archetype check (advisory)
# ---------------------------------------------------------------------------

def _check_archetype_aggregation(archetype_spec: int, spatial_n: Optional[int],
                                 run_dir: str) -> Optional[dict]:
    """If question names K archetypes but code uses fewer, require an
    error-bound justification in model_comparison.md or results.md."""
    # Try to find the actual archetype count used.
    # Heuristic: grep models/ for patterns like "A1", "A2", ... or for an
    # archetype list.
    models = _read_models_code(run_dir)
    archetype_ids: set[str] = set()
    arch_pattern = re.compile(r"\b(A[1-9]\d?)\b")
    for code in models.values():
        archetype_ids.update(arch_pattern.findall(code))
    k_used = len(archetype_ids) if archetype_ids else None

    if k_used is None or k_used >= archetype_spec:
        return None  # can't determine or compliant

    # Check for an error-bound statement in markdown artifacts.
    bound_keywords = ("within_archetype_error", "within-archetype error",
                      "aggregation error", "archetype variance",
                      "within-archetype PfPR variance")
    bound_found = False
    for md_name in ("model_comparison.md", "results.md",
                    "modeling_strategy.md"):
        md_path = os.path.join(run_dir, md_name)
        if not os.path.exists(md_path):
            continue
        try:
            with open(md_path) as f:
                text = f.read().lower()
            if any(kw.lower() in text for kw in bound_keywords):
                bound_found = True
                break
        except OSError:
            continue

    # Phase 3 D: check for a STRUCTURED bound, not just any prose match.
    # `**Within-archetype error**: <value>` (or `variance`) is the
    # machine-readable contract. If it's absent, the bound is treated
    # as undocumented and severity defaults to HIGH (any reduction
    # without a quantitative bound).
    structured_bound_re = re.compile(
        r"^\*\*Within-archetype\s+(?:error|variance)\*\*:\s*([0-9.]+)\s*(pp|%)?\s*$",
        re.MULTILINE | re.IGNORECASE,
    )
    structured_bound_value: Optional[float] = None
    structured_bound_unit: Optional[str] = None
    for md_name in ("modeling_strategy.md", "model_comparison.md", "results.md"):
        md_path = os.path.join(run_dir, md_name)
        if not os.path.exists(md_path):
            continue
        try:
            with open(md_path) as f:
                text = f.read()
        except OSError:
            continue
        m = structured_bound_re.search(text)
        if m:
            structured_bound_value = float(m.group(1))
            structured_bound_unit = m.group(2) or "pp"
            break

    ratio = archetype_spec / k_used

    if structured_bound_value is not None:
        # A structured bound is present. Severity depends on its magnitude.
        if structured_bound_value > 20:
            severity = "MEDIUM"
            kind = "archetype_bound_weak"
            reason = (
                f"Structured bound documented "
                f"(**Within-archetype error**: {structured_bound_value}{structured_bound_unit}) "
                f"but exceeds 20{structured_bound_unit} threshold — within-archetype "
                f"heterogeneity is too large to defend LGA-level guidance."
            )
        else:
            # Bound is documented and tight enough — no blocker.
            return None
    else:
        # No structured bound → HIGH by default (Phase 3 D: any K_used <
        # K_spec without explicit quantitative bound blocks ACCEPT).
        severity = "HIGH"
        kind = "archetype_aggregation_unvalidated"
        reason = (
            f"No structured within-archetype error bound found. "
            f"{ratio:.1f}× reduction from {archetype_spec} → {k_used} "
            f"archetypes requires an explicit `**Within-archetype error**: "
            f"<value>pp` line in modeling_strategy.md before LGA-level "
            f"guidance is defensible."
        )

    return {
        "kind": kind,
        "required": archetype_spec,
        "severity": severity,
        "used": k_used,
        "evidence": (
            f"Question names {archetype_spec} archetypes; code uses "
            f"{k_used} (grepped models/ for A1/A2/... identifiers). "
            + reason
        ),
    }


# ---------------------------------------------------------------------------
# Top-level check
# ---------------------------------------------------------------------------

def check_spec_compliance(required: dict, run_dir: str,
                          decision_year: Optional[int] = None) -> dict:
    """Run all applicable checks. Returns {'violations': [...]}.

    decision_year: used for Phase 3 B vintage checks. Pass None to skip
    vintage checks entirely (preserves legacy behavior).
    """
    violations: list[dict] = []
    code_by_file = _read_models_code(run_dir)

    for fw in required.get("frameworks", []):
        v = _check_framework(fw, code_by_file)
        if v is not None:
            violations.append(v)

    if "abm" in required.get("approaches", []):
        v = _check_abm(code_by_file)
        if v is not None:
            violations.append(v)

    budget = required.get("budget_envelope")
    if budget is not None:
        v = _check_budget(budget, required.get("budget_raw") or f"${budget:,.0f}",
                          run_dir)
        if v is not None:
            violations.append(v)

    arch = required.get("archetype_spec")
    if arch is not None:
        spatial_n = None
        sp = required.get("spatial_units")
        if sp is not None:
            spatial_n = sp[0]
        v = _check_archetype_aggregation(arch, spatial_n, run_dir)
        if v is not None:
            violations.append(v)

    # Phase 3 Commit B: data + methodological vintage.
    if decision_year is not None:
        violations.extend(check_data_vintage(run_dir, decision_year))
        violations.extend(check_methodological_vintage(run_dir, decision_year))

    return {"violations": violations}


# ---------------------------------------------------------------------------
# CLI / self-test
# ---------------------------------------------------------------------------

def _run_self_test() -> int:
    """Run 8 inline cases and return 0 if all pass, 1 otherwise."""
    import tempfile

    failures: list[str] = []

    def ok(cond: bool, label: str) -> None:
        if not cond:
            failures.append(label)

    # --- Question parsing ---
    q1 = ("Build an agent-based model of malaria transmission across Nigeria's "
          "774 Local Government Areas (LGAs) using the Starsim framework. "
          "Benchmark against the published EMOD Nigeria analysis. "
          "Use the archetype approach from Ozodiegwu et al. 2023 (22 archetypes). "
          "Optimized cost-constrained allocation for Nigeria's Global Fund GC7 "
          "(~$320M malaria component).")
    r1 = detect_required_spec(q1)
    ok("starsim" in r1["frameworks"], "q1: starsim detected")
    ok("emod" not in r1["frameworks"],
       "q1: emod NOT required (it's a benchmark mention, not a requirement)")
    ok("abm" in r1["approaches"], "q1: abm detected")
    ok(r1["budget_envelope"] == 320e6,
       f"q1: budget=320M, got {r1['budget_envelope']}")
    ok(r1["spatial_units"] == (774, "lgas"),
       f"q1: 774 LGAs, got {r1['spatial_units']}")
    ok(r1["archetype_spec"] == 22,
       f"q1: 22 archetypes, got {r1['archetype_spec']}")

    q2 = "Fit a simple SIR model of influenza transmission."
    r2 = detect_required_spec(q2)
    ok(not r2["frameworks"], "q2: no framework required")
    ok(not r2["approaches"], "q2: no approach required")
    ok(r2["budget_envelope"] is None, "q2: no budget")

    q3 = ("Build a compartmental deterministic model of measles with "
          "$1.5 billion in interventions.")
    r3 = detect_required_spec(q3)
    ok("compartmental" in r3["approaches"], "q3: compartmental detected")
    ok("deterministic" in r3["approaches"], "q3: deterministic detected")
    ok(r3["budget_envelope"] == 1.5e9,
       f"q3: budget=$1.5B, got {r3['budget_envelope']}")

    # --- Filesystem checks ---
    with tempfile.TemporaryDirectory() as d:
        models = os.path.join(d, "models")
        os.makedirs(models)

        # Case A: question requires starsim, code has no starsim at all
        with open(os.path.join(models, "model.py"), "w") as f:
            f.write("import numpy as np\n"
                    "from scipy.integrate import odeint\n"
                    "def step(y, t): return -0.1 * y\n"
                    "odeint(step, [1.0], [0, 1, 2])\n")
        required_a = {"frameworks": ["starsim"], "approaches": ["abm"],
                      "budget_envelope": None, "budget_raw": None,
                      "spatial_units": None, "archetype_spec": None}
        va = check_spec_compliance(required_a, d)
        kinds_a = {v["kind"] for v in va["violations"]}
        ok("framework_missing" in kinds_a,
           f"A: expected framework_missing, got {kinds_a}")
        ok("approach_mismatch" in kinds_a,
           f"A: expected approach_mismatch, got {kinds_a}")

        # Case B: question requires starsim, code imports but never runs
        # (malaria-run pattern).
        with open(os.path.join(models, "model.py"), "w") as f:
            f.write("import starsim as ss\n"
                    "class Malaria(ss.SIS):\n"
                    "    pass\n"
                    "from scipy.integrate import odeint\n"
                    "odeint(lambda y, t: -0.1 * y, [1.0], [0, 1, 2])\n")
        vb = check_spec_compliance(required_a, d)
        kinds_b = {v["kind"] for v in vb["violations"]}
        ok("framework_missing" in kinds_b,
           f"B: starsim imported but not run; expected framework_missing, got {kinds_b}")

        # Case C: question requires starsim, code actually uses it.
        with open(os.path.join(models, "model.py"), "w") as f:
            f.write("import starsim as ss\n"
                    "people = ss.People(1000)\n"
                    "sim = ss.Sim(people=people, dur=10)\n"
                    "sim.run()\n")
        vc = check_spec_compliance(required_a, d)
        kinds_c = {v["kind"] for v in vc["violations"]}
        ok("framework_missing" not in kinds_c,
           f"C: starsim used properly; unexpected framework_missing in {kinds_c}")
        ok("approach_mismatch" not in kinds_c,
           f"C: ss.People present; unexpected approach_mismatch in {kinds_c}")

        # Case D: budget envelope check.
        with open(os.path.join(d, "lga_allocation.csv"), "w") as f:
            f.write("lga,cost,allocated\n"
                    "A,100000000,True\n"
                    "B,41000000,True\n")
        required_d = {"frameworks": [], "approaches": [],
                      "budget_envelope": 320e6, "budget_raw": "$320M",
                      "spatial_units": None, "archetype_spec": None}
        vd = check_spec_compliance(required_d, d)
        kinds_d = {v["kind"] for v in vd["violations"]}
        ok("budget_underutilized" in kinds_d,
           f"D: $141M of $320M should trigger, got {kinds_d}")
        bu = next(v for v in vd["violations"] if v["kind"] == "budget_underutilized")
        ok(abs(bu["actual_spend"] - 141e6) < 1e3,
           f"D: actual_spend={bu['actual_spend']}, expected 141M")

        # Case E: budget is adequately utilized.
        with open(os.path.join(d, "lga_allocation.csv"), "w") as f:
            f.write("lga,cost,allocated\n"
                    "A,200000000,True\n"
                    "B,90000000,True\n")
        ve = check_spec_compliance(required_d, d)
        kinds_e = {v["kind"] for v in ve["violations"]}
        ok("budget_underutilized" not in kinds_e,
           f"E: $290M of $320M = 90.6% should NOT trigger, got {kinds_e}")

        # --- Phase 3 Commit B: data vintage ---

        # Case F: primary calibration dataset with vintage 2006, decision_year
        # 2024 → gap 18 → HIGH data_vintage_stale.
        with open(os.path.join(d, "data_quality.md"), "w") as f:
            f.write("## lga_archetypes.csv\n"
                    "**Vintage**: 2006\n"
                    "**Primary calibration**: yes\n"
                    "Archetype clustering derived from 2006 DHS + MIS.\n\n"
                    "## seasonal_profiles.csv\n"
                    "**Vintage**: 2015\n"
                    "**Primary calibration**: no\n"
                    "Seasonal ITN usage profile.\n")
        required_f = {"frameworks": [], "approaches": [],
                      "budget_envelope": None, "budget_raw": None,
                      "spatial_units": None, "archetype_spec": None}
        vf = check_spec_compliance(required_f, d, decision_year=2024)
        vints_f = [v for v in vf["violations"] if v["kind"] == "data_vintage_stale"]
        ok(any(v["severity"] == "HIGH" and v.get("primary")
               for v in vints_f),
           f"F: expected HIGH data_vintage_stale on primary "
           f"calibration target, got {[(v['severity'], v.get('primary')) for v in vints_f]}")
        ok(any(v["severity"] == "MEDIUM" and not v.get("primary")
               for v in vints_f),
           f"F: expected MEDIUM on non-primary 2015 dataset (gap=9)")

        # Case G: data_quality.md with no structured Vintage lines →
        # vintage_unstructured MEDIUM.
        with open(os.path.join(d, "data_quality.md"), "w") as f:
            f.write("## some_dataset.csv\n"
                    "Collected sometime around 2010 or so.\n"
                    "Data quality is good.\n")
        vg = check_spec_compliance(required_f, d, decision_year=2024)
        kinds_g = {v["kind"] for v in vg["violations"]}
        ok("vintage_unstructured" in kinds_g,
           f"G: expected vintage_unstructured, got {kinds_g}")

        # --- Phase 3 Commit D: archetype bound refinement ---

        # Case H: K=6, N=22, structured bound 3pp → no HIGH; no
        # archetype_bound_weak (3pp ≤ 20).
        with open(os.path.join(d, "models", "model.py"), "w") as f:
            f.write("archetypes = ['A1','A2','A3','A4','A5','A6']\n")
        with open(os.path.join(d, "modeling_strategy.md"), "w") as f:
            f.write("## Archetype aggregation\n"
                    "Collapsing 22 archetypes to 6 for tractability.\n"
                    "**Within-archetype error**: 3pp\n")
        required_h = {"frameworks": [], "approaches": [],
                      "budget_envelope": None, "budget_raw": None,
                      "spatial_units": None, "archetype_spec": 22}
        vh = check_spec_compliance(required_h, d)
        kinds_h = {v["kind"] for v in vh["violations"]}
        ok("archetype_aggregation_unvalidated" not in kinds_h,
           f"H: structured bound present, expected no HIGH archetype, got {kinds_h}")
        ok("archetype_bound_weak" not in kinds_h,
           f"H: 3pp is a tight bound, no bound_weak expected, got {kinds_h}")

        # Case I: K=6, N=22, structured bound 25pp → MEDIUM
        # archetype_bound_weak.
        with open(os.path.join(d, "modeling_strategy.md"), "w") as f:
            f.write("## Archetype aggregation\n"
                    "Collapsing 22 archetypes to 6 for tractability.\n"
                    "**Within-archetype error**: 25pp\n")
        vi = check_spec_compliance(required_h, d)
        kinds_i = {v["kind"] for v in vi["violations"]}
        ok("archetype_bound_weak" in kinds_i,
           f"I: 25pp exceeds 20, expected archetype_bound_weak, got {kinds_i}")
        ok("archetype_aggregation_unvalidated" not in kinds_i,
           f"I: bound is documented (if weak), no _unvalidated expected")

        # Case J: K=6, N=22, NO structured bound → HIGH
        # archetype_aggregation_unvalidated.
        with open(os.path.join(d, "modeling_strategy.md"), "w") as f:
            f.write("## Archetype aggregation\n"
                    "Collapsing to 6 archetypes, error is small.\n")
        vj = check_spec_compliance(required_h, d)
        kinds_j = {v["kind"] for v in vj["violations"]}
        ok("archetype_aggregation_unvalidated" in kinds_j,
           f"J: no structured bound, expected HIGH _unvalidated, got {kinds_j}")

        # --- decision_year detection ---
        meta_gc7 = {"question": "Global Fund GC7 allocation for Nigeria",
                    "started": "2026-04-23T07:00:00"}
        ok(detect_decision_year(meta_gc7["question"], meta_gc7) == 2024,
           "K: GC7 → 2024")
        meta_gc8 = {"question": "Build a GC8 allocation model",
                    "started": "2026-04-23T07:00:00"}
        ok(detect_decision_year(meta_gc8["question"], meta_gc8) == 2027,
           "L: GC8 → 2027")
        meta_fallback = {"question": "A model of measles",
                         "started": "2026-04-23T07:00:00"}
        ok(detect_decision_year(meta_fallback["question"], meta_fallback) == 2026,
           "M: fallback to metadata.started year")

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
                   help="Run directory to check (requires metadata.json)")
    p.add_argument("--self-test", action="store_true",
                   help="Run inline self-test cases and exit")
    p.add_argument("--json", action="store_true",
                   help="Emit machine-readable JSON to stdout")
    args = p.parse_args()

    if args.self_test:
        return _run_self_test()

    if not args.run_dir:
        p.error("run_dir is required (or use --self-test)")

    run_dir = args.run_dir
    meta_path = os.path.join(run_dir, "metadata.json")
    if not os.path.exists(meta_path):
        print(f"ERROR: {meta_path} not found", file=sys.stderr)
        return 2
    with open(meta_path) as f:
        meta = json.load(f)
    question = meta.get("question", "")
    if not question:
        print(f"ERROR: {meta_path} has no 'question' field", file=sys.stderr)
        return 2

    required = detect_required_spec(question)
    result = check_spec_compliance(required, run_dir)

    # Human-readable to stderr.
    print(f"Spec requirements parsed from question:", file=sys.stderr)
    print(f"  frameworks: {required['frameworks']}", file=sys.stderr)
    print(f"  approaches: {required['approaches']}", file=sys.stderr)
    if required["budget_envelope"]:
        print(f"  budget: {required['budget_raw']} "
              f"(${required['budget_envelope']:,.0f})", file=sys.stderr)
    if required["spatial_units"]:
        print(f"  spatial: {required['spatial_units'][0]} "
              f"{required['spatial_units'][1]}", file=sys.stderr)
    if required["archetype_spec"]:
        print(f"  archetypes: {required['archetype_spec']}", file=sys.stderr)
    print(f"Violations: {len(result['violations'])}", file=sys.stderr)
    for v in result["violations"]:
        print(f"  [{v['severity']}] {v['kind']}: {v['evidence']}",
              file=sys.stderr)

    if args.json:
        print(json.dumps({"required": required,
                          "violations": result["violations"]}, indent=2))

    return 0 if not result["violations"] else 1


if __name__ == "__main__":
    sys.exit(main())
