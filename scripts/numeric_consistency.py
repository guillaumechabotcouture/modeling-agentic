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
import math
import os
import re
import sys
from typing import Callable, Optional


MEDIUM_DRIFT_THRESHOLD = 0.05   # 5%
HIGH_DRIFT_THRESHOLD = 0.25     # 25%

# Tokens for parsing M / k / no-suffix numeric claims like "54.7M cases"
# or "127.5M". Returns the value scaled to its base unit.
_NUMERIC_M_RE = re.compile(r"(\d+(?:\.\d+)?)\s*M\b")
_NUMERIC_K_RE = re.compile(r"(\d+(?:\.\d+)?)\s*k\b")
_DOLLAR_PER_CASE_RE = re.compile(r"\$(\d+(?:\.\d+)?)\s*/\s*case")
_DOLLAR_PER_DALY_RE = re.compile(r"\$(\d+(?:\.\d+)?)\s*/\s*DALY",
                                 re.IGNORECASE)
_ROUND_TITLE_RE = re.compile(r"Round\s*(\d+)", re.IGNORECASE)


def _parse_m_or_raw(text: str) -> Optional[float]:
    """Parse '54.7M' → 54_700_000, '127500000' → 127500000.
    Returns None if no parse possible."""
    m = _NUMERIC_M_RE.search(text)
    if m:
        return float(m.group(1)) * 1_000_000
    k = _NUMERIC_K_RE.search(text)
    if k:
        return float(k.group(1)) * 1_000
    raw = re.search(r"\b(\d+(?:\.\d+)?)\b", text)
    if raw:
        v = float(raw.group(1))
        # Heuristic: a "cases averted" value < 1000 was probably a
        # millions-stripped number; flag for caller to decide.
        return v
    return None


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
    sources produce a None value; the cross-check then reports
    `authoritative_unavailable` rather than guessing. Both the per-
    archetype and portfolio-level numbers are loaded so claims like
    "$4.71/case for PBO in NW" can be checked against the per-archetype
    row, not the portfolio average.
    """
    auth: dict = {
        "headline_cases_averted": None,
        "headline_uniform_cases": None,
        "improvement_pct": None,
        "cost_per_case_portfolio": None,
        "cost_per_daly_portfolio": None,
        "pbo_nw_cost_per_case": None,
        "pbo_nw_pfpr_reduction": None,
        "pbo_nw_smc_pfpr_reduction": None,
        "final_round": None,
    }

    summary_path = os.path.join(run_dir, "models", "optimization_summary.json")
    if os.path.exists(summary_path):
        try:
            with open(summary_path) as f:
                s = json.load(f)
            auth["headline_cases_averted"] = float(s.get("total_cases_averted", 0)) or None
            auth["headline_uniform_cases"] = float(s.get("uniform_cases_averted", 0)) or None
            if (auth["headline_cases_averted"] is not None
                    and auth["headline_uniform_cases"] is not None
                    and auth["headline_uniform_cases"] != 0):
                auth["improvement_pct"] = (
                    (auth["headline_cases_averted"] - auth["headline_uniform_cases"])
                    / auth["headline_uniform_cases"] * 100.0
                )
            auth["cost_per_case_portfolio"] = s.get("cost_per_case_averted")
            auth["cost_per_daly_portfolio"] = s.get("cost_per_daly_averted")
        except (json.JSONDecodeError, OSError, KeyError):
            pass

    # Per-archetype numbers from package_evaluation.csv. The
    # PBO-in-NW row (the famous $5.05/$4.71 contradiction in 104914)
    # is in the row with archetype="Taura", zone="North West",
    # package="llin_pbo". We parse the FIRST NW/llin_pbo row we find
    # (Taura — alphabetically first archetype in the run).
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
                            auth["pbo_nw_pfpr_reduction"] = float(
                                cells[idx["pfpr_reduction_pct"]])
                        except (KeyError, ValueError):
                            pass
                        break
                # Reset and look for PBO+SMC in NW
                f.seek(0)
                f.readline()
                for line in f:
                    cells = line.strip().split(",")
                    if len(cells) <= max(idx.values()):
                        continue
                    if (cells[idx.get("zone", -1)] == "North West"
                            and cells[idx.get("package", -1)] == "llin_pbo_smc"):
                        try:
                            auth["pbo_nw_smc_pfpr_reduction"] = float(
                                cells[idx["pfpr_reduction_pct"]])
                        except (KeyError, ValueError):
                            pass
                        break
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


def _scan_doc_costs(doc_path: str, doc_text: str
                    ) -> list[tuple[float, str, str, str]]:
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

    # Report-level files to scan. scope_declaration.yaml is YAML but
    # we read it as text — numeric drift detection is regex-based.
    report_files = [
        "results.md",
        "figure_rationale.md",
        "decision_rule.md",
        "scope_declaration.yaml",
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
            costs = _scan_doc_costs(results_path, text)
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
                violations.append(_build_violation(
                    "same_doc_inconsistency", "MEDIUM",
                    f"results.md contains {len(distinct)} distinct cost-per-case "
                    f"values for PBO in NW: {distinct}. The CSV-authoritative "
                    f"value is "
                    f"${auth['pbo_nw_cost_per_case']:.2f}/case "
                    f"(models/package_evaluation.csv, Taura row). "
                    f"All in-text mentions must agree to within {MEDIUM_DRIFT_THRESHOLD*100:.0f}%."
                    if auth['pbo_nw_cost_per_case'] is not None else
                    f"results.md contains {len(distinct)} distinct cost-per-case "
                    f"values for PBO in NW: {distinct}. authoritative source "
                    f"unavailable; cannot determine which is correct, but the "
                    f"in-text spread is itself a problem."
                ))
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
                            f"results.md claims $\\${val:.2f}/case for PBO in NW "
                            f"but models/package_evaluation.csv (Taura/NW/"
                            f"llin_pbo) is $\\${auth['pbo_nw_cost_per_case']:.2f}/case "
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

    # --- Round-number drift: results.md title vs progress.md ---
    if auth["final_round"] is not None and os.path.exists(results_path):
        try:
            with open(results_path, encoding="utf-8") as f:
                first_lines = "".join(f.readline() for _ in range(3))
            m = _ROUND_TITLE_RE.search(first_lines)
            if m:
                claimed_round = int(m.group(1))
                if claimed_round != auth["final_round"]:
                    drift = abs(claimed_round - auth["final_round"]) / max(
                        auth["final_round"], 1)
                    sev = ("HIGH" if drift > HIGH_DRIFT_THRESHOLD
                           else "MEDIUM" if drift > MEDIUM_DRIFT_THRESHOLD
                           else None)
                    if sev is None:
                        sev = "MEDIUM"  # round mismatch always at least MEDIUM
                    violations.append(_build_violation(
                        "numeric_drift_detected", sev,
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
                     figure_rationale_text: str | None = None) -> None:
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
