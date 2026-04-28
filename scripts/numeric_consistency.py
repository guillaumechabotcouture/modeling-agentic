#!/usr/bin/env python3
"""
Phase 12 Commit α — cross-file numeric consistency check.

The Phase 11 live malaria run (104914) reached STAGE 7 ACCEPT but
shipped with at least 4 verifiable internal inconsistencies a domain
expert would catch in 5 minutes:

- "$5.05/case" appeared in 3 places of results.md while the same
  metric (PBO-in-NW cost-per-case) was "$4.71/case" elsewhere in the
  same file (CSV-authoritative: $4.71).
- H3 evidence claimed "29% to 30%" PfPR reduction; CSV-actual was
  "25.7% to 26.0%".
- figure_rationale.md showed "127.5M cases averted (+18.9%)" while
  the authoritative optimization_summary.json said "54.7M (+158%)".
- results.md title said "Round 4" after STAGE 7 ACCEPT at Round 6.

The redteam caught two of these in round 6 (R-014, R-015) but
emitted MEDIUM, not HIGH — and only redteam noticed. The unifying
gap: no validator compares headline numbers ACROSS sibling artifacts.

This script extracts labeled numeric claims from the report-level
files (results.md, figure_rationale.md, decision_rule.md,
scope_declaration.yaml) and cross-references each against
authoritative source data (models/optimization_summary.json,
models/package_evaluation.csv, progress.md). Drift > 5% fires MEDIUM
`numeric_drift_detected`; drift > 25% fires HIGH
`numeric_drift_extreme`. Same-document contradictions
(e.g., "$5.05" and "$4.71" for the same metric in same file) fire
MEDIUM `same_doc_inconsistency`.

Usage:
    python3 scripts/numeric_consistency.py <run_dir>
    python3 scripts/numeric_consistency.py --self-test
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Optional


MEDIUM_DRIFT_THRESHOLD = 0.05   # 5%
HIGH_DRIFT_THRESHOLD = 0.25     # 25%

_DOLLAR_PER_CASE_RE = re.compile(r"\$(\d+(?:\.\d+)?)\s*/\s*case")
_ROUND_TITLE_RE = re.compile(r"Round\s*(\d+)", re.IGNORECASE)

# Phase 13 Commit β: extended token classes for staleness drift.
# The 190855 run shipped decision_rule.md claiming 121 LGAs (CSV: 123)
# and figure_rationale.md claiming "104 dual-AI" (CSV: 106). α v1
# scanned only $X/case and X[M] cases averted. v2 adds three classes
# anchored on labeled context to avoid false positives on bare digits.
#
# LGA-count: require a TOTAL-allocation anchor to avoid matching
# legitimate per-subset counts ("106 dual-AI LGAs", "93 NW LGAs").
# Accepted forms:
#   "121 LGAs allocated|received|receive"  (right-window anchor)
#   "121 of N LGAs"                         (left "of N" anchor)
#   "all 121 LGAs"                          (left "all" anchor)
#   "total of 121 LGAs"                     (left "total of" anchor)
_LGA_COUNT_RE = re.compile(
    r"(\d{1,4})\s+LGAs?\b",
    re.IGNORECASE,
)
_LGA_COUNT_OF_N_RE = re.compile(
    r"(\d{1,4})\s+of\s+\d{1,4}\s+LGAs?\b",
    re.IGNORECASE,
)
_LGA_TOTAL_LEFT_TOKENS = (" all ", "total of ", "allocate to ",
                          "allocated to ", "across ")
_LGA_TOTAL_RIGHT_TOKENS = (" allocated", " received", " receive",
                           " in total", " total")

# Zone qualifiers that mark a count as a PER-ZONE subset (NOT the
# global aggregate). When a zone token (or "of 774", the universe
# pattern, or "<", the prediction-range marker, or "Prediction"
# anchor) appears in the 30-char window around an LGA-count or
# package-count match, the match is skipped: "93 NW LGAs",
# "76 dual-AI in NW", "NC zone has 14 LGAs allocated", "X of 774 LGAs",
# "<100 LGAs", "Prediction: 250-400 LGAs" are all per-subset / non-
# headline counts.
_ZONE_RE = re.compile(
    r"\b(NW|NE|NC|SW|SE|SS"
    r"|north[\s\-]?(?:west|east|central)"
    r"|south[\s\-]?(?:west|east|south))\b",
    re.IGNORECASE,
)
_NON_HEADLINE_TOKENS = (
    " of 774",         # universe of all LGAs
    "of 774 ",         # universe form
    "<",                # prediction range "<100"
    "prediction",       # hypothesis prediction context
    "predicted",
    "expected",
    "should",
    "may receive",
    "could receive",
    "hypothesis",       # hypothesis statement
    "h1 ", "h2 ", "h3 ", "h4 ", "h5 ", "h6 ", "h7 ",   # H<N> labels
    "(h1)", "(h2)", "(h3)", "(h4)", "(h5)", "(h6)", "(h7)",
    "accuracy",         # decision-rule accuracy line
)


def _has_zone_qualifier(window_text: str) -> bool:
    """Return True if a per-zone qualifier or non-headline anchor
    appears in the window. Uses regex word boundaries to avoid
    false positives like 'zo**NE**' matching 'NE'."""
    if _ZONE_RE.search(window_text):
        return True
    w = window_text.lower()
    return any(tok in w for tok in _NON_HEADLINE_TOKENS)


def _extract_zone_from_window(window: str, zone_alt_map: dict,
                               from_right: bool = False,
                               require_no_sentence_break: bool = False
                               ) -> tuple[str | None, int]:
    """Find the closest zone keyword in `window` (using word-boundary
    regex) and return (canonical_zone_name, distance) or (None, 999).

    `from_right`: when True, distance = position from start of window
    (closer to the source = smaller distance). When False (default),
    distance = chars between zone-end and window-end (closer to the
    source = smaller distance, since the source is at end of left
    window).

    `require_no_sentence_break`: when True (right-window fallback
    mode), skip a zone match if any of `.`, `;`, `\\n` appears
    between window start and the zone — that means the zone is in
    the next sentence, not attributable to the source percent."""
    matched_zone = None
    best_dist = 999
    for zm in _ZONE_RE.finditer(window):
        z_str = zm.group(0).lower().replace("-", "")
        z_canonical = (zone_alt_map.get(z_str)
                       or zone_alt_map.get(z_str.replace(" ", "")))
        if z_canonical is None:
            continue
        if require_no_sentence_break:
            between = window[:zm.start()]
            if any(ch in between for ch in (".", ";", "\n")):
                continue
        dist = zm.start() if from_right else len(window) - zm.end()
        if dist < best_dist:
            best_dist = dist
            matched_zone = z_canonical
    return matched_zone, best_dist


def _is_table_row(doc_text: str, pos: int) -> bool:
    """Return True if `pos` falls inside a markdown table row
    (the line begins with `|`). Table cells almost always have
    zone/archetype context in their row header that's lost in a
    30-char window — skip them to avoid false positives."""
    line_start = doc_text.rfind("\n", 0, pos) + 1
    line_end = doc_text.find("\n", pos)
    if line_end == -1:
        line_end = len(doc_text)
    line = doc_text[line_start:line_end].lstrip()
    return line.startswith("|")
# Match "104 dual-AI", "17 PBO", "12 SMC LGAs". Anchored on the
# package keyword to avoid catching arbitrary digit-keyword pairs.
_PACKAGE_COUNT_RE = re.compile(
    r"(\d{1,4})\s+(dual[\s\-_]?ai|pbo|standard\s+llin|smc|irs)\b",
    re.IGNORECASE,
)
# Match "78.1% of budget", "11% of cost", "10.8% of allocation".
_BUDGET_SHARE_RE = re.compile(
    r"(\d{1,3}(?:\.\d+)?)\s*%\s+of\s+(?:the\s+)?(?:budget|cost|allocation)",
    re.IGNORECASE,
)


def _drift_pct(claimed: float, authoritative: float) -> float:
    """Relative drift |claimed - authoritative| / |authoritative|.
    Returns 0 if authoritative is 0 (caller must guard)."""
    if authoritative == 0:
        return 0.0 if claimed == 0 else float("inf")
    return abs(claimed - authoritative) / abs(authoritative)


def _severity_for_drift(drift: float) -> Optional[str]:
    """Map a drift fraction to a severity. Returns None for in-tolerance."""
    if drift > HIGH_DRIFT_THRESHOLD:
        return "HIGH"
    if drift > MEDIUM_DRIFT_THRESHOLD:
        return "MEDIUM"
    return None


def _load_authoritative(run_dir: str) -> dict:
    """Load the authoritative source values used for cross-checking.

    Each key is a claim_id; the value is the canonical number. Missing
    sources produce a None value; the cross-check then skips that claim
    silently rather than guessing.
    """
    auth: dict = {
        "headline_cases_averted": None,
        "headline_uniform_cases": None,
        "pbo_nw_cost_per_case": None,
        "final_round": None,
        # Phase 13 β: extended canonical truth from allocation_result.csv.
        "n_allocated_lgas": None,        # rows with cost > 0
        "package_counts": {},            # {dual_ai: 106, pbo: 17, ...}
        "zone_budget_shares": {},        # {NW: 0.781, NC: 0.108, ...}
    }

    summary_path = os.path.join(run_dir, "models", "optimization_summary.json")
    if os.path.exists(summary_path):
        try:
            with open(summary_path) as f:
                s = json.load(f)
            auth["headline_cases_averted"] = float(s.get("total_cases_averted", 0)) or None
            auth["headline_uniform_cases"] = float(s.get("uniform_cases_averted", 0)) or None
        except (json.JSONDecodeError, OSError, KeyError):
            pass

    # Per-archetype PBO-in-NW cost from package_evaluation.csv. We use
    # the first NW/llin_pbo row encountered (file-order, not
    # alphabetical) — within a single run, all NW/llin_pbo archetype
    # rows share the same cost_per_case_averted because the cost is
    # a function of zone-level baseline ITN coverage and package
    # composition, not archetype-specific population.
    eval_path = os.path.join(run_dir, "models", "package_evaluation.csv")
    if os.path.exists(eval_path):
        try:
            with open(eval_path) as f:
                header = f.readline().strip().split(",")
                idx = {name: i for i, name in enumerate(header)}
                for line in f:
                    cells = line.strip().split(",")
                    if len(cells) <= max(idx.values()):
                        continue
                    if (cells[idx.get("zone", -1)] == "North West"
                            and cells[idx.get("package", -1)] == "llin_pbo"):
                        try:
                            auth["pbo_nw_cost_per_case"] = float(
                                cells[idx["cost_per_case_averted"]])
                        except (KeyError, ValueError):
                            pass
                        break
        except OSError:
            pass

    # Phase 13 β: extended canonical truth from allocation CSVs.
    # Look for any *allocation*.csv under models/. Compute:
    #   - n_allocated_lgas (rows with total_cost_usd > 0)
    #   - per-package counts (group by `package` column)
    #   - per-zone budget shares (sum cost by `zone`, divided by total)
    alloc_path: str | None = None
    models_dir = os.path.join(run_dir, "models")
    if os.path.isdir(models_dir):
        for entry in os.listdir(models_dir):
            if "allocation" in entry and entry.endswith(".csv"):
                alloc_path = os.path.join(models_dir, entry)
                break
    if alloc_path:
        try:
            with open(alloc_path) as f:
                header = f.readline().strip().split(",")
                idx = {n: i for i, n in enumerate(header)}
                cost_col = idx.get("total_cost_usd")
                pkg_col = idx.get("package")
                zone_col = idx.get("zone")
                pkg_counts: dict = {}
                zone_costs: dict = {}
                n_allocated = 0
                total_cost = 0.0
                for line in f:
                    cells = line.strip().split(",")
                    if cost_col is None or len(cells) <= cost_col:
                        continue
                    try:
                        c = float(cells[cost_col])
                    except ValueError:
                        continue
                    if c <= 0:
                        continue
                    n_allocated += 1
                    total_cost += c
                    if pkg_col is not None and len(cells) > pkg_col:
                        pkg = cells[pkg_col].strip().lower()
                        if pkg and pkg != "baseline_act":
                            pkg_counts[pkg] = pkg_counts.get(pkg, 0) + 1
                    if zone_col is not None and len(cells) > zone_col:
                        z = cells[zone_col].strip()
                        if z:
                            zone_costs[z] = zone_costs.get(z, 0.0) + c
                if n_allocated > 0:
                    auth["n_allocated_lgas"] = n_allocated
                if pkg_counts:
                    auth["package_counts"] = pkg_counts
                if zone_costs and total_cost > 0:
                    auth["zone_budget_shares"] = {
                        z: c / total_cost for z, c in zone_costs.items()}
        except OSError:
            pass

    # Final round from progress.md — search for last "Round N" in a
    # STAGE 7 decision header.
    progress_path = os.path.join(run_dir, "progress.md")
    if os.path.exists(progress_path):
        try:
            with open(progress_path) as f:
                text = f.read()
            rounds = [int(m.group(1))
                      for m in re.finditer(
                          r"(?:Stage|STAGE)\s*7\s*decision\s*\(round\s*(\d+)",
                          text, re.IGNORECASE)]
            if rounds:
                auth["final_round"] = max(rounds)
        except OSError:
            pass

    return auth


def _scan_doc_costs(doc_text: str) -> list[tuple[float, str, str, str]]:
    """Find all '$X/case' occurrences in a doc with three windows:
      - left_window (25 chars BEFORE the match) — for entity
        classification
      - right_window (30 chars AFTER the match) — for "for X" /
        "vs $X" disambiguation
      - context (60/60) — for the violation message
    All three are anchored to the ACTUAL match position. Returns
    (value, left_window, right_window, context)."""
    results = []
    for m in _DOLLAR_PER_CASE_RE.finditer(doc_text):
        try:
            val = float(m.group(1))
        except ValueError:
            continue
        left_window = doc_text[max(0, m.start() - 25):m.start()]
        right_window = doc_text[m.end():min(len(doc_text), m.end() + 30)]
        ctx = doc_text[max(0, m.start() - 60):
                       min(len(doc_text), m.end() + 60)]
        results.append((val, left_window, right_window, ctx))
    return results


def _scan_doc_cases_averted(doc_text: str) -> list[tuple[float, str, str]]:
    """Find all 'X[M] cases averted' patterns with left-context (used to
    classify the match as referring to the OPTIMIZED total or the
    UNIFORM baseline). Returns (value, classification, context_window)
    where classification ∈ {"optimized", "uniform"}.

    We classify based on the ~40 chars immediately to the LEFT of the
    matched number — the label that precedes the number, not the
    next-clause comparison that follows. e.g.
    "Optimized averts 54.7M cases vs 21.2M uniform" produces:
      - match 1: 54.7M, left="Optimized averts " → optimized
      - match 2: 21.2M (different pattern, not matched here unless we
        broaden the regex)
    A trailing "uniform" should NEVER reclassify a preceding optimized
    number — that was the T1 bug in the first draft.
    """
    pat = re.compile(
        r"(?:averts?\s+|averted\s*[:\-]?\s*|allocation\s+averts?\s+)"
        r"(\d+(?:\.\d+)?)\s*(M|million|thousand|k)?\s*cases",
        re.IGNORECASE)
    out = []
    for m in pat.finditer(doc_text):
        val = float(m.group(1))
        unit = (m.group(2) or "").lower()
        if unit in ("m", "million"):
            val *= 1_000_000
        elif unit in ("thousand", "k"):
            val *= 1_000
        # Left context: 40 chars BEFORE the match.
        left_start = max(0, m.start() - 40)
        left = doc_text[left_start:m.start()].lower()
        # If the immediate left says "uniform"/"baseline_uniform"/
        # "uniform allocation", classify as uniform-baseline; else
        # default to optimized (the headline claim).
        classification = ("uniform"
                          if any(tok in left
                                 for tok in ("uniform allocation averts",
                                             "uniform allocation:",
                                             "uniform_baseline",
                                             "uniform baseline",
                                             "baseline averts"))
                          else "optimized")
        # Wider context window for the violation message itself.
        ctx_start = max(0, m.start() - 60)
        ctx_end = min(len(doc_text), m.end() + 60)
        out.append((val, classification, doc_text[ctx_start:ctx_end]))
    return out


def _normalize_package_token(tok: str) -> str:
    """Map regex-matched package keyword to the canonical CSV
    `package` column value. The CSV uses underscored snake_case
    (e.g., 'llin_dual_ai', 'llin_pbo'); the prose uses 'dual-AI',
    'PBO', 'standard LLIN'. This normalizer aligns them."""
    t = tok.lower().replace("-", "_").replace(" ", "_")
    # Common aliases
    aliases = {
        "dual_ai": "llin_dual_ai",
        "dualai": "llin_dual_ai",
        "pbo": "llin_pbo",
        "standard_llin": "llin_standard",
        "smc": "smc",
        "irs": "irs",
    }
    return aliases.get(t, t)


def _scan_lga_counts(doc_text: str) -> list[tuple[int, str]]:
    """Find labeled LGA counts that refer to the TOTAL allocated
    set (not per-subset counts). Requires either:
      - "X of N LGAs" form (unambiguous total), OR
      - left-window anchor (all / total of / across), OR
      - right-window anchor (allocated / received / receive / in total)
    Returns (count, context) tuples."""
    out = []
    seen_positions: set[int] = set()
    # Try "X of N LGAs" first (highest-confidence total form). Skip
    # if a zone qualifier appears in the left/right window or if the
    # second number is the universe size (774 for Nigeria) — "X of
    # 774 LGAs" is the unallocated-or-decision-rule-accuracy form,
    # NOT the X-allocated-of-Y-total form.
    for m in _LGA_COUNT_OF_N_RE.finditer(doc_text):
        try:
            n = int(m.group(1))
        except ValueError:
            continue
        if n < 10 or n > 5000:
            continue
        # Inspect the matched text for "of 774" — skip if so.
        matched = m.group(0).lower()
        if " of 774" in matched or "of 774 " in matched:
            continue
        left_window = doc_text[max(0, m.start() - 30):m.start()]
        right_window = doc_text[m.end():
                                 min(len(doc_text), m.end() + 30)]
        if _has_zone_qualifier(left_window + " " + right_window):
            continue
        seen_positions.add(m.start())
        ctx_start = max(0, m.start() - 60)
        ctx_end = min(len(doc_text), m.end() + 60)
        out.append((n, doc_text[ctx_start:ctx_end]))
    # Then sweep for bare "X LGAs" with anchor in left or right window.
    for m in _LGA_COUNT_RE.finditer(doc_text):
        if m.start() in seen_positions:
            continue
        try:
            n = int(m.group(1))
        except ValueError:
            continue
        if n < 10 or n > 5000:
            continue
        left_window = doc_text[max(0, m.start() - 30):m.start()]
        right_window = doc_text[m.end():
                                 min(len(doc_text), m.end() + 30)]
        # Skip per-zone subsets.
        if _has_zone_qualifier(left_window + " " + right_window):
            continue
        lw = left_window.lower()
        rw = right_window.lower()
        has_left_anchor = any(tok in lw
                              for tok in _LGA_TOTAL_LEFT_TOKENS)
        has_right_anchor = any(rw.startswith(tok)
                               for tok in _LGA_TOTAL_RIGHT_TOKENS)
        if not (has_left_anchor or has_right_anchor):
            continue
        ctx_start = max(0, m.start() - 60)
        ctx_end = min(len(doc_text), m.end() + 60)
        out.append((n, doc_text[ctx_start:ctx_end]))
    return out


def _scan_package_counts(doc_text: str) -> list[tuple[int, str, str]]:
    """Find labeled package counts. Skips per-zone subsets (any
    match with a zone qualifier in the 30-char window). Returns
    (count, package_token, context) tuples."""
    out = []
    for m in _PACKAGE_COUNT_RE.finditer(doc_text):
        try:
            n = int(m.group(1))
        except ValueError:
            continue
        if n < 1 or n > 5000:
            continue
        # Skip markdown table rows — table cells almost always have
        # per-zone/per-archetype row headers that the narrow window
        # can't see.
        if _is_table_row(doc_text, m.start()):
            continue
        left_window = doc_text[max(0, m.start() - 30):m.start()]
        right_window = doc_text[m.end():
                                 min(len(doc_text), m.end() + 30)]
        # Skip per-zone subsets.
        if _has_zone_qualifier(left_window + " " + right_window):
            continue
        pkg_raw = m.group(2)
        pkg_norm = _normalize_package_token(pkg_raw)
        ctx_start = max(0, m.start() - 60)
        ctx_end = min(len(doc_text), m.end() + 60)
        out.append((n, pkg_norm, doc_text[ctx_start:ctx_end]))
    return out


def _scan_budget_shares(doc_text: str) -> list[tuple[float, int, str, str, str]]:
    """Find labeled budget shares ("78.1% of budget"). Returns
    (fraction, left_window, right_window, context) tuples.
    Fraction in [0, 1]. The left_window (40 chars BEFORE) and
    right_window (40 chars AFTER) are what callers use to identify
    which zone — the zone may appear before ("NW: 78% of budget")
    OR after ("78% of budget to NW")."""
    out = []
    for m in _BUDGET_SHARE_RE.finditer(doc_text):
        try:
            pct = float(m.group(1))
        except ValueError:
            continue
        if pct < 0 or pct > 100:
            continue
        left_start = max(0, m.start() - 40)
        right_end = min(len(doc_text), m.end() + 40)
        ctx_start = max(0, m.start() - 60)
        ctx_end = min(len(doc_text), m.end() + 60)
        left_window = doc_text[left_start:m.start()]
        right_window = doc_text[m.end():right_end]
        out.append((pct / 100.0, m.start(), left_window, right_window,
                    doc_text[ctx_start:ctx_end]))
    return out


def _check_count_drift(run_dir: str, auth: dict,
                        report_files: list[str]) -> list[dict]:
    """Phase 13 β: cross-doc drift on LGA counts, package counts,
    and budget shares. Truth is models/allocation_result.csv (loaded
    into auth by _load_authoritative). Each scanned file's count
    must agree to within 5% MEDIUM / 25% HIGH."""
    out: list[dict] = []
    auth_n_lga = auth.get("n_allocated_lgas")
    auth_pkgs = auth.get("package_counts") or {}
    auth_zones = auth.get("zone_budget_shares") or {}
    if auth_n_lga is None and not auth_pkgs and not auth_zones:
        return []

    for fname in report_files:
        fpath = os.path.join(run_dir, fname)
        if not os.path.exists(fpath):
            continue
        try:
            with open(fpath, encoding="utf-8") as f:
                text = f.read()
        except (UnicodeDecodeError, OSError):
            continue

        # LGA counts
        if auth_n_lga is not None:
            for n, ctx in _scan_lga_counts(text):
                # Skip "774 LGAs" (the universe — not the allocated
                # subset). Accept anything else as a candidate.
                if n in (774,):  # known total-Nigeria LGA count
                    continue
                drift = _drift_pct(n, auth_n_lga)
                sev = _severity_for_drift(drift)
                if sev is not None:
                    kind = ("numeric_drift_extreme" if sev == "HIGH"
                            else "numeric_drift_detected")
                    out.append(_build_violation(
                        kind, sev,
                        f"{fname} reports {n} LGAs but "
                        f"models/allocation_result.csv shows "
                        f"{auth_n_lga} allocated "
                        f"({drift*100:.1f}% drift). Context: "
                        f"\"...{ctx.strip()[:120]}...\"."
                    ))

        # Package counts
        for n, pkg_norm, ctx in _scan_package_counts(text):
            auth_count = auth_pkgs.get(pkg_norm)
            if auth_count is None or auth_count == 0:
                continue
            drift = _drift_pct(n, auth_count)
            sev = _severity_for_drift(drift)
            if sev is not None:
                kind = ("numeric_drift_extreme" if sev == "HIGH"
                        else "numeric_drift_detected")
                out.append(_build_violation(
                    kind, sev,
                    f"{fname} reports {n} {pkg_norm} LGAs but "
                    f"models/allocation_result.csv shows "
                    f"{auth_count} ({drift*100:.1f}% drift). "
                    f"Context: \"...{ctx.strip()[:120]}...\"."
                ))

        # Budget shares (zone-wise). The text doesn't always tag
        # which zone — we look for the zone keyword only in the
        # 40-char LEFT window to avoid pulling in zones from
        # adjacent clauses (e.g., "NW: 78%; NC: 22%" must NOT
        # match NC's 22% to NW's authoritative share). We also
        # skip prediction/range anchors ("predicts", "<", "25-35%").
        if auth_zones:
            zone_alt_map = {z.lower(): z for z in auth_zones}
            for z_key in list(zone_alt_map):
                # Add abbreviated form (NW, NC, ...) for full-name zones.
                zone_alt_map.setdefault(
                    z_key.replace(" ", "")
                          .replace("north", "n")
                          .replace("south", "s")
                          .replace("east", "e")
                          .replace("west", "w"),
                    zone_alt_map[z_key])
            for (share, pos, left_window, right_window,
                 ctx) in _scan_budget_shares(text):
                # Skip table rows — the row header carries zone
                # context the window can't see.
                if _is_table_row(text, pos):
                    continue
                # Skip if this is a prediction range or hypothesis.
                lw_lower = left_window.lower()
                non_headline = ("prediction", "predicted",
                                "expected", "should receive",
                                "may receive", "could receive",
                                "<", "approximately ", "hypothesis",
                                "h1 ", "h2 ", "h3 ", "h4 ", "h5 ",
                                "h6 ", "h7 ",
                                "(h1)", "(h2)", "(h3)", "(h4)",
                                "(h5)", "(h6)", "(h7)",
                                "(25-", "(20-", "(30-")
                if any(tok in lw_lower for tok in non_headline):
                    continue
                # Prefer the zone keyword in the LEFT window
                # (closest to the percent). Only fall back to the
                # RIGHT window when the left has none — handles
                # both "NW receives 78%" and "78% to NW" but
                # avoids "NW: 75%; NC: 24%" attribution drift.
                matched_zone, _ = _extract_zone_from_window(
                    left_window, zone_alt_map, from_right=False)
                if matched_zone is None:
                    matched_zone, _ = _extract_zone_from_window(
                        right_window, zone_alt_map,
                        from_right=True,
                        require_no_sentence_break=True)
                if matched_zone is None:
                    continue
                auth_share = auth_zones[matched_zone]
                drift = _drift_pct(share, auth_share)
                sev = _severity_for_drift(drift)
                if sev is not None:
                    kind = ("numeric_drift_extreme" if sev == "HIGH"
                            else "numeric_drift_detected")
                    out.append(_build_violation(
                        kind, sev,
                        f"{fname} reports {share*100:.1f}% of budget "
                        f"for {matched_zone} but "
                        f"models/allocation_result.csv shows "
                        f"{auth_share*100:.1f}% "
                        f"({drift*100:.1f}% drift). Context: "
                        f"\"...{ctx.strip()[:120]}...\"."
                    ))
    return out


def _build_violation(kind: str, severity: str, claim: str,
                     stage: str = "WRITE") -> dict:
    return {
        "kind": kind,
        "severity": severity,
        "stage": stage,
        "claim": claim,
    }


def check_numeric_consistency(run_dir: str) -> list[dict]:
    """Cross-check report-level numeric claims against authoritative
    sources. Returns a list of violations.

    Three classes of violation:
      numeric_drift_detected    MEDIUM — cross-doc drift >5% from authority
      numeric_drift_extreme     HIGH   — cross-doc drift >25% from authority
      same_doc_inconsistency    MEDIUM — same labeled metric, different
                                          numbers in same file
    """
    violations: list[dict] = []
    auth = _load_authoritative(run_dir)

    # Files scanned for cross-doc cases-averted drift. results.md,
    # figure_rationale.md, and (Phase 13 β) decision_rule.md are the
    # headline-number locations. The cost-per-case same-doc check is
    # still hardcoded to results.md below; decision_rule.md typically
    # doesn't contain $X/case mentions but does carry LGA counts,
    # package counts, and budget shares — those are checked separately
    # via _check_count_drift below.
    report_files = [
        "results.md",
        "figure_rationale.md",
        "decision_rule.md",
    ]

    # --- Same-doc inconsistency: PBO-in-NW cost-per-case ---
    # If results.md mentions "PBO" + "NW" + multiple distinct $X/case
    # values, fire same_doc_inconsistency. This catches the
    # $5.05/$4.71 split that affected 104914.
    results_path = os.path.join(run_dir, "results.md")
    if os.path.exists(results_path):
        try:
            with open(results_path, encoding="utf-8") as f:
                text = f.read()
            costs = _scan_doc_costs(text)
            # Filter to "PBO ALONE" + "NW" / "North West" context.
            # Critical: exclude PBO+SMC, PBO+IRS, and standard-LLIN
            # mentions. The 104914 retro fired false positives on
            # $7.14 (PBO+SMC) and $8.41 (standard LLIN). Tightening:
            # require "pbo" in the 25-char LEFT window AND no
            # combination tokens in that window. The left_window now
            # correctly anchors to the actual match position.
            pbo_nw_costs = []
            for val, left_window, right_window, ctx in costs:
                lw = left_window.lower()
                rw = right_window.lower()
                lc = ctx.lower()
                has_pbo_left = "pbo" in lw
                has_nw = "nw" in lc or "north west" in lc
                # Left-window exclusion: package-combination tokens
                # immediately preceding the dollar value.
                excluded_left = any(tok in lw
                                    for tok in ("smc", "irs", "+smc", "+irs",
                                                "full", "dual"))
                # Right-window exclusion: "for standard LLIN" /
                # "for dual-AI" / "for IRS" pattern is the dollar
                # value attributed to a different package. Distinct
                # from "(...) over standard LLINs (...)" which is a
                # comparison clause where the $X belongs to PBO
                # (the subject) and the standard cost is its own
                # dollar value (caught separately).
                excluded_right = any(tok in rw
                                     for tok in ("for standard",
                                                 "for dual",
                                                 "for irs",
                                                 "for the standard",
                                                 "for the dual"))
                if (has_pbo_left and has_nw
                        and not excluded_left and not excluded_right):
                    pbo_nw_costs.append((val, ctx))
            distinct = sorted({round(v, 2) for v, _ in pbo_nw_costs})
            if len(distinct) > 1:
                if auth['pbo_nw_cost_per_case'] is not None:
                    claim_msg = (
                        f"results.md contains {len(distinct)} distinct "
                        f"cost-per-case values for PBO in NW: {distinct}. "
                        f"The CSV-authoritative value is "
                        f"${auth['pbo_nw_cost_per_case']:.2f}/case "
                        f"(models/package_evaluation.csv, first NW/llin_pbo "
                        f"row). All in-text mentions must agree to within "
                        f"{MEDIUM_DRIFT_THRESHOLD*100:.0f}%."
                    )
                else:
                    claim_msg = (
                        f"results.md contains {len(distinct)} distinct "
                        f"cost-per-case values for PBO in NW: {distinct}. "
                        f"Authoritative source unavailable; cannot determine "
                        f"which is correct, but the in-text spread is itself "
                        f"a problem."
                    )
                violations.append(_build_violation(
                    "same_doc_inconsistency", "MEDIUM", claim_msg))
            # Cross-check vs authoritative
            if auth["pbo_nw_cost_per_case"] is not None and pbo_nw_costs:
                for val, ctx in pbo_nw_costs:
                    drift = _drift_pct(val, auth["pbo_nw_cost_per_case"])
                    sev = _severity_for_drift(drift)
                    if sev is not None:
                        kind = ("numeric_drift_extreme" if sev == "HIGH"
                                else "numeric_drift_detected")
                        violations.append(_build_violation(
                            kind, sev,
                            f"results.md claims ${val:.2f}/case for PBO in NW "
                            f"but models/package_evaluation.csv (NW/"
                            f"llin_pbo first row) is "
                            f"${auth['pbo_nw_cost_per_case']:.2f}/case "
                            f"({drift*100:.1f}% drift). Context: "
                            f"\"...{ctx.strip()[:120]}...\"."
                        ))
        except (UnicodeDecodeError, OSError):
            pass

    # --- Cross-doc drift: cases averted ---
    # The 104914 figure_rationale.md said "127.5M cases" while
    # optimization_summary.json said 54.7M. Drift > 100% → HIGH.
    if auth["headline_cases_averted"] is not None:
        for fname in report_files:
            fpath = os.path.join(run_dir, fname)
            if not os.path.exists(fpath):
                continue
            try:
                with open(fpath, encoding="utf-8") as f:
                    text = f.read()
            except (UnicodeDecodeError, OSError):
                continue
            for val, classification, ctx in _scan_doc_cases_averted(text):
                if classification == "uniform":
                    auth_val = auth["headline_uniform_cases"]
                else:
                    auth_val = auth["headline_cases_averted"]
                if auth_val is None or auth_val == 0:
                    continue
                drift = _drift_pct(val, auth_val)
                sev = _severity_for_drift(drift)
                if sev is not None:
                    kind = ("numeric_drift_extreme" if sev == "HIGH"
                            else "numeric_drift_detected")
                    violations.append(_build_violation(
                        kind, sev,
                        f"{fname} reports {val/1e6:.1f}M cases averted "
                        f"but optimization_summary.json says "
                        f"{auth_val/1e6:.1f}M ({drift*100:.0f}% drift). "
                        f"Context: \"...{ctx.strip()[:120]}...\". The "
                        f"report's numbers must match the authoritative "
                        f"optimization output."
                    ))

    # --- Phase 13 β: extended count drift across decision_rule.md,
    # results.md, figure_rationale.md ---
    # LGA counts ("121 LGAs allocated"), package counts ("104 dual-AI"),
    # budget shares ("78.1% of budget"). Each is cross-referenced
    # against models/allocation_result.csv truth.
    violations.extend(_check_count_drift(run_dir, auth, report_files))

    # --- Round-number drift: results.md title vs progress.md ---
    if auth["final_round"] is not None and os.path.exists(results_path):
        try:
            with open(results_path, encoding="utf-8") as f:
                first_lines = "".join(f.readline() for _ in range(3))
            m = _ROUND_TITLE_RE.search(first_lines)
            if m:
                claimed_round = int(m.group(1))
                if claimed_round != auth["final_round"]:
                    # A round mismatch always fires; HIGH when the drift
                    # exceeds the extreme threshold, MEDIUM otherwise.
                    # Kind matches the convention used elsewhere in this
                    # module: `_extreme` for HIGH, `_detected` for MEDIUM.
                    drift = abs(claimed_round - auth["final_round"]) / max(
                        auth["final_round"], 1)
                    if drift > HIGH_DRIFT_THRESHOLD:
                        sev, kind = "HIGH", "numeric_drift_extreme"
                    else:
                        sev, kind = "MEDIUM", "numeric_drift_detected"
                    violations.append(_build_violation(
                        kind, sev,
                        f"results.md title says \"Round {claimed_round}\" but "
                        f"progress.md shows STAGE 7 decisions through "
                        f"Round {auth['final_round']} (the authoritative "
                        f"final round). Update the title."
                    ))
        except (UnicodeDecodeError, OSError):
            pass

    return violations


def _run_self_test() -> int:
    import tempfile

    failures: list[str] = []

    def ok(cond: bool, label: str) -> None:
        if not cond:
            failures.append(label)

    # Helper to build a minimal run dir fixture
    def make_run_dir(d: str, *,
                     opt_summary: dict | None = None,
                     pkg_eval_rows: list[dict] | None = None,
                     progress_text: str | None = None,
                     results_text: str | None = None,
                     figure_rationale_text: str | None = None,
                     decision_rule_text: str | None = None,
                     allocation_rows: list[dict] | None = None) -> None:
        os.makedirs(os.path.join(d, "models"), exist_ok=True)
        if opt_summary is not None:
            with open(os.path.join(d, "models", "optimization_summary.json"),
                      "w") as f:
                json.dump(opt_summary, f)
        if pkg_eval_rows is not None:
            cols = ["archetype", "zone", "package", "cost_per_case_averted",
                    "pfpr_reduction_pct"]
            with open(os.path.join(d, "models", "package_evaluation.csv"),
                      "w") as f:
                f.write(",".join(cols) + "\n")
                for row in pkg_eval_rows:
                    f.write(",".join(str(row.get(c, "")) for c in cols) + "\n")
        if progress_text is not None:
            with open(os.path.join(d, "progress.md"), "w") as f:
                f.write(progress_text)
        if results_text is not None:
            with open(os.path.join(d, "results.md"), "w") as f:
                f.write(results_text)
        if figure_rationale_text is not None:
            with open(os.path.join(d, "figure_rationale.md"), "w") as f:
                f.write(figure_rationale_text)
        if decision_rule_text is not None:
            with open(os.path.join(d, "decision_rule.md"), "w") as f:
                f.write(decision_rule_text)
        if allocation_rows is not None:
            cols = ["lga_name", "zone", "package", "total_cost_usd"]
            with open(os.path.join(d, "models", "allocation_result.csv"),
                      "w") as f:
                f.write(",".join(cols) + "\n")
                for row in allocation_rows:
                    f.write(",".join(str(row.get(c, "")) for c in cols) + "\n")

    # T1: clean run, all numbers consistent → silent
    with tempfile.TemporaryDirectory() as d:
        make_run_dir(d,
            opt_summary={
                "total_cases_averted": 54_700_000,
                "uniform_cases_averted": 21_200_000,
                "cost_per_case_averted": 5.83,
                "cost_per_daly_averted": 68.07,
            },
            pkg_eval_rows=[
                {"archetype": "Taura", "zone": "North West",
                 "package": "llin_pbo", "cost_per_case_averted": 4.71,
                 "pfpr_reduction_pct": 25.74},
            ],
            progress_text="STAGE 7 decision (round 6/8)\nACCEPT\n",
            results_text=(
                "# Results: Nigeria GC7 Allocation Model (Round 6)\n\n"
                "Optimized allocation averts 54.7M cases vs 21.2M uniform "
                "(+158%). Cost per case averted for PBO in NW is "
                "$4.71/case.\n"
            ))
        v = check_numeric_consistency(d)
        ok(not v, f"T1: clean run should be silent, got {v}")

    # T2: same-doc inconsistency in results.md ($5.05 vs $4.71 for PBO/NW)
    with tempfile.TemporaryDirectory() as d:
        make_run_dir(d,
            opt_summary={"total_cases_averted": 54_700_000,
                         "uniform_cases_averted": 21_200_000},
            pkg_eval_rows=[
                {"archetype": "Taura", "zone": "North West",
                 "package": "llin_pbo", "cost_per_case_averted": 4.71,
                 "pfpr_reduction_pct": 25.74},
            ],
            progress_text="STAGE 7 decision (round 6/8)\n",
            results_text=(
                "# Results (Round 6)\n"
                "PBO in NW: $4.71/case (line 1).\n"
                "PBO in NW: $5.05/case (line 2).\n"
                "PBO in NW: $5.05/case (line 3).\n"
            ))
        v = check_numeric_consistency(d)
        ok(any(x["kind"] == "same_doc_inconsistency" for x in v),
           f"T2: should fire same_doc_inconsistency, got {v}")

    # T3: cross-doc drift > 25% → HIGH numeric_drift_extreme
    # (figure_rationale claims 127.5M vs optimization 54.7M)
    with tempfile.TemporaryDirectory() as d:
        make_run_dir(d,
            opt_summary={"total_cases_averted": 54_700_000,
                         "uniform_cases_averted": 21_200_000},
            pkg_eval_rows=[],
            progress_text="STAGE 7 decision (round 6/8)\n",
            figure_rationale_text=(
                "## cases_averted_comparison.png\n"
                "Optimized allocation averts 127.5M cases vs 107.2M uniform.\n"
            ))
        v = check_numeric_consistency(d)
        ok(any(x["kind"] == "numeric_drift_extreme"
               and x["severity"] == "HIGH" for x in v),
           f"T3: should fire HIGH numeric_drift_extreme, got {v}")

    # T4: cross-doc drift 5-25% → MEDIUM numeric_drift_detected
    with tempfile.TemporaryDirectory() as d:
        make_run_dir(d,
            opt_summary={"total_cases_averted": 54_700_000,
                         "uniform_cases_averted": 21_200_000},
            pkg_eval_rows=[],
            progress_text="STAGE 7 decision (round 6/8)\n",
            figure_rationale_text=(
                # 60M is +9.7% from 54.7M — within 5-25% MEDIUM band
                "Optimized allocation averts 60.0M cases over the period.\n"
            ))
        v = check_numeric_consistency(d)
        ok(any(x["kind"] == "numeric_drift_detected"
               and x["severity"] == "MEDIUM" for x in v),
           f"T4: 9.7% drift should fire MEDIUM, got {v}")

    # T5: drift just below 5% threshold → silent
    with tempfile.TemporaryDirectory() as d:
        make_run_dir(d,
            opt_summary={"total_cases_averted": 54_700_000,
                         "uniform_cases_averted": 21_200_000},
            pkg_eval_rows=[],
            progress_text="STAGE 7 decision (round 6/8)\n",
            figure_rationale_text=(
                # 56M is +2.4% from 54.7M — below 5%
                "Optimized allocation averts 56.0M cases.\n"
            ))
        v = check_numeric_consistency(d)
        ok(not any(x["kind"].startswith("numeric_drift") for x in v),
           f"T5: 2.4% drift should be silent, got {v}")

    # T6: missing optimization_summary.json → silent on cross-doc
    # checks (auth_val is None), but same-doc inconsistency still
    # fires if present
    with tempfile.TemporaryDirectory() as d:
        make_run_dir(d,
            opt_summary=None,
            pkg_eval_rows=None,
            progress_text="STAGE 7 decision (round 6/8)\n",
            results_text=(
                "# Results\n"
                "PBO in NW: $4.71/case (line 1).\n"
                "PBO in NW: $5.05/case (line 2).\n"
            ))
        v = check_numeric_consistency(d)
        # Should still detect same-doc inconsistency in results.md even
        # without authoritative source.
        ok(any(x["kind"] == "same_doc_inconsistency" for x in v),
           f"T6: same-doc inconsistency should fire even without auth, got {v}")

    # T7: title round mismatch
    with tempfile.TemporaryDirectory() as d:
        make_run_dir(d,
            opt_summary={"total_cases_averted": 54_700_000,
                         "uniform_cases_averted": 21_200_000},
            pkg_eval_rows=[],
            progress_text=(
                "## Stage 7 decision (round 4)\n"
                "## Stage 7 decision (round 5)\n"
                "## Stage 7 decision (round 6)\n"
            ),
            results_text=(
                "# Results: Nigeria GC7 Model (Round 4)\n\n"
                "Optimized averts 54.7M cases.\n"
            ))
        v = check_numeric_consistency(d)
        ok(any("Round 4" in x["claim"] and "Round 6" in x["claim"]
               for x in v),
           f"T7: title round 4 vs progress round 6 should fire, got {v}")

    # T8: empty run dir → silent (no panic)
    with tempfile.TemporaryDirectory() as d:
        v = check_numeric_consistency(d)
        ok(not v, f"T8: empty run dir should be silent, got {v}")

    # Phase 13 β: T9-T12 cover the extended count-drift checks.

    # T9: LGA count drift in decision_rule.md (110 vs CSV 130 ≈ 15%).
    # The 1.6% drift in 190855 (121 vs 123) was below MEDIUM_DRIFT_
    # THRESHOLD; the gate intentionally tolerates sub-5% drift. Test
    # uses a realistic ≥10% drift above threshold. The text uses the
    # canonical aggregate phrasing "110 LGAs allocated" — which
    # triggers the right-anchor "allocated" and has no zone qualifier
    # (the bare 110 LGAs without "in NW" / table-cell context).
    with tempfile.TemporaryDirectory() as d:
        rows = []
        for i in range(65):
            rows.append({"lga_name": f"lga_{i}", "zone": "NW",
                         "package": "llin_dual_ai",
                         "total_cost_usd": 2_000_000})
        for i in range(65):
            rows.append({"lga_name": f"lga_n_{i}", "zone": "NC",
                         "package": "llin_dual_ai",
                         "total_cost_usd": 2_000_000})
        make_run_dir(d,
            allocation_rows=rows,
            progress_text="STAGE 7 decision (round 6/8)\n",
            decision_rule_text=(
                "# Decision Rule\n"
                "## Allocation Summary\n"
                "Total of 110 LGAs allocated.\n"
            ))
        v = check_numeric_consistency(d)
        ok(any(x["kind"] == "numeric_drift_detected"
               and "110" in x["claim"] and "130" in x["claim"]
               for x in v),
           f"T9: 110 vs 130 LGA-count drift should fire MEDIUM, got {v}")

    # T10: package count drift in figure_rationale.md
    # (figure says 80 dual-AI, CSV has 106 ≈ 24.5%).
    with tempfile.TemporaryDirectory() as d:
        rows = []
        for i in range(106):
            rows.append({"lga_name": f"lga_{i}", "zone": "NW",
                         "package": "llin_dual_ai",
                         "total_cost_usd": 2_000_000})
        make_run_dir(d,
            allocation_rows=rows,
            progress_text="STAGE 7 decision (round 6/8)\n",
            figure_rationale_text=(
                "## allocation breakdown\n"
                "80 dual-AI, 17 PBO LGAs.\n"
            ))
        v = check_numeric_consistency(d)
        ok(any(x["kind"] == "numeric_drift_detected"
               and "dual" in x["claim"].lower()
               and "80" in x["claim"] and "106" in x["claim"]
               for x in v),
           f"T10: 80 vs 106 dual-AI package-count drift should fire "
           f"MEDIUM, got {v}")

    # T11: budget share drift across files (decision_rule says 70% NW
    # but CSV has 78%). Should fire numeric_drift_detected.
    with tempfile.TemporaryDirectory() as d:
        rows = []
        for i in range(78):
            rows.append({"lga_name": f"lga_{i}", "zone": "NW",
                         "package": "llin_dual_ai",
                         "total_cost_usd": 1_000_000})
        for i in range(22):
            rows.append({"lga_name": f"lga_n_{i}", "zone": "NC",
                         "package": "llin_dual_ai",
                         "total_cost_usd": 1_000_000})
        # NW budget share: 78M / 100M = 78%
        make_run_dir(d,
            allocation_rows=rows,
            progress_text="STAGE 7 decision (round 6/8)\n",
            decision_rule_text=(
                "# Decision Rule\n"
                "## NW Zone Allocation\n"
                "70% of budget goes to NW.\n"
            ))
        v = check_numeric_consistency(d)
        ok(any(x["kind"] == "numeric_drift_detected"
               and "70" in x["claim"] and "78" in x["claim"]
               for x in v),
           f"T11: 70% vs 78% budget-share drift should fire MEDIUM, "
           f"got {v}")

    # T12: clean — all sources match CSV → no count-drift violations.
    with tempfile.TemporaryDirectory() as d:
        rows = []
        for i in range(123):
            rows.append({"lga_name": f"lga_{i}",
                         "zone": "NW" if i < 93 else "NC",
                         "package": ("llin_dual_ai" if i < 106
                                     else "llin_pbo"),
                         "total_cost_usd": 1_000_000})
        # CSV: 123 LGAs, 106 dual-AI, 17 PBO, NW 93/123 by count =
        # cost share NW 93M/123M = 75.6%; NC 30/123 = 24.4%
        make_run_dir(d,
            opt_summary={"total_cases_averted": 54_700_000,
                         "uniform_cases_averted": 21_200_000},
            allocation_rows=rows,
            progress_text="STAGE 7 decision (round 6/8)\n",
            decision_rule_text=(
                "# Decision Rule\n"
                "## Allocation\n"
                "123 LGAs receive intervention. NW Zone Allocation: "
                "75.6% of budget; NC: 24.4% of budget.\n"
                "Mix: 106 dual-AI, 17 PBO.\n"
            ))
        v = check_numeric_consistency(d)
        # No count-drift violations expected. Other classes (cost,
        # round, etc.) may still fire — we only assert on count_drift.
        count_v = [x for x in v
                   if x["kind"].startswith("numeric_drift")
                   and ("LGA" in x["claim"]
                        or "% of budget" in x["claim"]
                        or "dual" in x["claim"].lower()
                        or "pbo" in x["claim"].lower())]
        ok(not count_v,
           f"T12: clean run should produce no count-drift violations, "
           f"got {count_v}")

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

    violations = check_numeric_consistency(args.run_dir)
    if args.json:
        print(json.dumps(violations, indent=2))
    else:
        print(f"violations: {len(violations)}", file=sys.stderr)
        for v in violations:
            print(f"  [{v['severity']:6s}] {v['kind']}: {v['claim'][:200]}",
                  file=sys.stderr)
    return 0 if not violations else 1


if __name__ == "__main__":
    sys.exit(main())
