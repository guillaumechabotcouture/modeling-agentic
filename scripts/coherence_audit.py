"""Phase 17 β + Phase 18 β — Coherence audit.

Standalone post-WRITE validator. Phase 17 β shipped duties 1-3
catching the residual drift class that survived to ACCEPT in the
2026-04-29_161312 run; Phase 18 β added duty 4 (ledger binding) to
catch unbound prose against `models/claims_ledger.yaml`:

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

  Duty 4 (ledger binding, Phase 18 β + δ): every currency, scalar,
    percentage, count, and verdict label in `report.md` must appear
    in `models/claims_ledger.yaml`'s rendered values OR within 5%
    proximity of a ledger numeric. Raw surviving `[CLAIM:id]` tokens
    are also flagged HIGH. No-op when the ledger is absent (the
    `_check_claims_ledger_present` validator fires separately).

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
from lib.text_spans import code_spans, in_spans  # noqa: E402


# Phase 21 γ: kept as thin wrappers (shared implementation in
# scripts/lib/text_spans.py).
def _fenced_or_inline_code_spans(text: str) -> list[tuple[int, int]]:
    return code_spans(text, inline=True)


def _in_protected_span(spans: list[tuple[int, int]], pos: int) -> bool:
    return in_spans(spans, pos)


# Closed verdict vocabulary (read by validate_critique_yaml.py).
VERDICTS = ("CLEAN", "DRIFT_DETECTED")


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


# Phase 21 precision pass. The before/after retro labelled 83 HIGH
# label-coherence hits on 18 historical runs: 15 were real (prose
# asserting a different verdict for the recommendation, e.g. results.md
# "verdict: UNSTABLE" while the YAML says SENSITIVE). The rest matched the
# bare English word: "ROBUST threshold = 0.70", "Spearman 0.983 (ROBUST)",
# "geographic prioritization is robust", "verdict corrected from
# UNSTABLE", "not a robust result". A mention now counts only when it
# asserts a verdict about the recommendation.
_LABEL_THRESHOLD_RE = re.compile(
    r"threshold|cut-?off|criteri\w*|defin\w*|→|->|[<>≤≥]|=\s*\d", re.IGNORECASE)
_LABEL_OTHER_METRIC_RE = re.compile(
    r"spearman|\brho\b|rank\s+correlation|concordance|\bLOO\b|cross-?valid\w*|"
    r"\bfolds?\b|\bUQ\b|\bdraws?\b|monte\s+carlo|\bCI\b|confidence\s+interval|"
    r"effect\s+size", re.IGNORECASE)
_LABEL_META_RE = re.compile(
    r"corrected\s+from|changed\s+from|previously|still\s+(?:says|references|reads)|"
    r"in\s+prose|instead\s+of|rather\s+than|was\s+(?:reported|labell?ed)|"
    r"authoritative", re.IGNORECASE)
_LABEL_SUBCLAIM_RE = re.compile(
    r"geograph\w*|zone[- ]level|zonal|regional|qualitative|direction\w*|"
    r"ranking|prioriti[sz]\w*|effect\s+size|finding", re.IGNORECASE)
_LABEL_RECOMMENDATION_RE = re.compile(
    r"recommend\w*|primary|dominan\w*|decision\s+rule|verdict|package", re.IGNORECASE)
_LABEL_SENSITIVITY_CONTEXT_RE = re.compile(
    r"sensitivity|perturb\w*|\bSA-?\d|with\s+respect\s+to|across\s+all", re.IGNORECASE)


def _asserts_verdict(text: str, start: int, end: int) -> bool:
    """True when the label mention at [start, end) asserts a sensitivity
    verdict about the recommendation (see the precision note above)."""
    clause = _nc._clause(text, start, end)
    near = text[max(0, start - 30):min(len(text), end + 30)]
    word = text[start:end]
    if _LABEL_META_RE.search(clause):
        return False
    if word.isupper() and re.search(r"\bverdict\W{0,4}\Z", text[max(0, start - 15):start],
                                    re.IGNORECASE):
        return True  # "verdict: UNSTABLE" is an assertion whatever follows
    if _LABEL_THRESHOLD_RE.search(near) or _LABEL_OTHER_METRIC_RE.search(clause):
        return False
    if re.search(r"\b(?:not|never|no\s+longer)\s+(?:a\s+|an\s+|as\s+)?\Z",
                 text[max(0, start - 20):start], re.IGNORECASE):
        return False
    if _LABEL_SUBCLAIM_RE.search(clause) and not _LABEL_RECOMMENDATION_RE.search(clause):
        return False
    if word.isupper():
        return True  # an ALL-CAPS verdict token
    # Lower-case prose only counts as a predicate over sensitivity results:
    # "the recommendation is robust across all tested perturbations".
    return bool(_LABEL_SENSITIVITY_CONTEXT_RE.search(clause)
                and re.search(r"\b(?:is|are|remains?|was|were|be|becomes?)\s+(?:\w+\s+){0,2}\Z",
                              text[max(0, start - 40):start], re.IGNORECASE))


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
                        if not _asserts_verdict(text, m.start(), m.end()):
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
# 1.0% gap; the $197M vs $177M conflation is 11%. We pick 5% as the
# default threshold — large enough to ignore rounding, small enough to
# catch conflation.
_COUNT_DRIFT_THRESHOLD = 0.05

_COST_COLUMNS = ("annual_cost", "cost_usd", "total_cost_usd", "cost",
                 "total_cost", "cost_3yr")
_PACKAGE_COLUMNS = ("package", "assigned_package", "package_name")


def _read_allocation_csv(run_dir: str) -> list[dict] | None:
    """Find and parse the allocation CSV.

    Phase 21 γ: resolution is shared with numeric_consistency
    (`allocation.canonical_csv` → models/allocation_result.csv → first
    *allocation*.csv under models/, run root, data/, preferring the
    optimized one), so both auditors read the same file.
    """
    path = _nc._resolve_alloc_path(run_dir)
    if path is None:
        return None
    rows = _nc._read_csv_rows(path)
    return rows or None


# Threshold/hypothetical markers that disqualify a "$X for package P"
# match — the prose is discussing scenarios, not the actual allocation.
_HYPOTHETICAL_MARKERS = (
    "exceeds", "if budget", "if the budget", "above $", "below $",
    "threshold", "would require", "would need", "scenario b",
    "alternative scenario", "hypothetical", "only if", "only when",
    "rises above", "drops below",
)

# "$X budget" sub-patterns where X is the dollar amount itself — the
# prose is naming the budget pool, not a package cost. Phase 21 γ:
# generic ("$320M grant budget", "the $45M programme envelope"), no
# funder-specific keywords.
_BUDGET_POOL_PATTERNS = (
    re.compile(r"\$\s*[\d.,]+\s*(?:[MBK]|million|billion|thousand)?\s+"
               r"(?:[A-Za-z0-9\-]+\s+){0,2}(?:budget|envelope|allocation\b)",
               re.IGNORECASE),
)

# Unit-price suffix right after an amount: "$1,500/DALY", "$30 per
# case", "$8/person/year". Generic: any "/word" or "per word".
# "$320M/774" (a per-unit budget share) is a ratio too.
_PER_UNIT_SUFFIX_RE = re.compile(r"\s*(?:/\s*[A-Za-z0-9]|per\s+[A-Za-z])",
                                 re.IGNORECASE)


def _scan_dollar_amount_with_package(doc_text: str,
                                     vocab: Any = None) -> list[dict]:
    """Find prose like '<package> costs ~$197M' tightly enough to catch
    package-cost claims, and discard threshold/hypothetical framings
    (e.g., 'X becomes cost-effective if budget exceeds $450M' is NOT a
    claim that X costs $450M).

    Phase 21 γ: package names come from the run vocabulary (allocation
    CSV package ids, labels, unique tokens, schema aliases) instead of a
    fixed intervention list.
    """
    vocab = vocab or _nc.RunVocabulary()
    out: list[dict] = []
    amount_re = re.compile(
        r"[^.|\n]{0,60}?\$\s*([\d.,]+)\s*(?:(M|million|K|k|thousand|B|billion)\b)?",
        re.IGNORECASE)
    prev_end = -10
    for ps, pe, pid in vocab.find_packages(doc_text):
        # Phase 21 precision pass: an alias directly continuing the
        # previous package name ("dual-AI LLIN": the shared token "LLIN"
        # is the base package's alias) is part of that name.
        continues = ps - prev_end <= 2 and not doc_text[prev_end:ps].strip(" -")
        prev_end = pe
        if continues:
            continue
        if _nc._joined_combination(doc_text, pe, vocab):
            continue  # "X+Y costs $…" names a combination, not package X
        m = amount_re.match(doc_text, pe)
        if m is None or re.search(r"\bif\b", m.group(0), re.IGNORECASE):
            continue
        # Phase 21 precision pass: the amount belongs to the nearest
        # package before it — "dual-AI accounts for $293.6M and PBO …"
        # must not attribute a later amount across another package.
        between = doc_text[pe:m.start() + m.group(0).rfind("$")]
        if vocab.find_packages(between):
            continue
        dollar_start = m.start() + m.group(0).rfind("$")
        try:
            amt = float(m.group(1).replace(",", "").rstrip("."))
        except ValueError:
            continue
        unit = (m.group(2) or "").lower()
        if unit in ("m", "million"):
            amt *= 1_000_000
        elif unit in ("k", "thousand"):
            amt *= 1_000
        elif unit in ("b", "billion"):
            amt *= 1_000_000_000
        elif amt < 1_000_000:
            # No unit suffix AND amount < $1M: almost always a per-unit
            # price, not a portfolio claim (Phase 17 ε).
            continue
        ctx_start = max(0, ps - 80)
        ctx_end = min(len(doc_text), m.end() + 80)
        ctx = doc_text[ctx_start:ctx_end].replace("\n", " ")
        # Phase 21 precision pass: hypothetical framing is read from the
        # clause up to the amount plus a short tail ("$450M would be
        # required"), not from a trailing justification 80 chars later.
        _cs, _ce = _nc._clause_span(doc_text, ps, m.end())
        frame = doc_text[_cs:min(_ce, m.end() + 30)].lower()
        if any(mk in frame for mk in _HYPOTHETICAL_MARKERS):
            continue
        phrase = doc_text[dollar_start:min(len(doc_text), m.end() + 40)]
        if any(p.match(phrase) for p in _BUDGET_POOL_PATTERNS):
            continue
        if _PER_UNIT_SUFFIX_RE.match(doc_text, m.end()):
            continue
        if _nc._is_table_row(doc_text, ps):
            continue
        # Phase 21 precision pass: other scenarios / budgets, the run's
        # own envelope, and money left unspent are not package costs.
        # Only the clause text up to the amount decides: a trailing
        # justification ("… because it is below the CE threshold") does
        # not turn a stated cost into a hypothetical.
        cs, _ce = _nc._clause_span(doc_text, ps, m.end())
        lead = doc_text[cs:m.end()]
        if (_nc._id_or_bound_adjacent(doc_text, dollar_start)
                or _nc._OFF_HEADLINE_RE.search(lead)
                # other envelopes named BEFORE the claimed amount ("At $60M,
                # X would reach $20M"); the claim's own amount is not one.
                or _nc._mentions_other_budget(doc_text[cs:dollar_start], vocab.budget)
                or re.search(r"\b(?:unallocated|unspent|leav\w*|remain\w*|left\s+over|"
                             r"saved|savings|headroom)\b",
                             _nc._clause(doc_text, ps, m.end()), re.IGNORECASE)):
            continue
        if vocab.budget and abs(amt - vocab.budget) <= 0.02 * vocab.budget:
            continue
        out.append({
            "package_token": pid,
            "dollar_usd": amt,
            "context": ctx,
            "match_start": ps,
        })
    return out


def _pkg_tokens(pid: str) -> set[str]:
    return {t for t in re.split(r"[_\-\s+]+", pid.lower()) if t}


def _audit_cross_file_counts(run_dir: str) -> list[dict]:
    """Reconcile prose numerics against the allocation CSV."""
    violations: list[dict] = []
    rows = _read_allocation_csv(run_dir)
    if rows is None or not rows:
        return violations
    vocab = _nc.build_vocabulary(run_dir, alloc_rows=rows)
    vocab.budget = _nc._question_budget(run_dir)

    pkg_col = _nc._pick_col(rows, _PACKAGE_COLUMNS)
    pkg_counts: dict[str, int] = {}
    pkg_costs: dict[str, float] = {}
    # Phase 21 precision pass: count only FUNDED units (cost > 0, or an
    # allocated/funded flag). A CSV with neither signal is a candidate
    # table (every unit's best package before the budget cut) — counting
    # its rows compared "172 funded PBO+SMC LGAs" against 404 candidates.
    has_cost = any(c in rows[0] for c in _COST_COLUMNS)
    flag_col = next((c for c in ("allocated", "funded", "selected")
                     if c in rows[0]), None)
    for r in rows:
        pkg = (r.get(pkg_col) or "").strip().lower() if pkg_col else ""
        if not pkg:
            continue
        cost = 0.0
        for col in _COST_COLUMNS:
            v = r.get(col)
            if v not in (None, ""):
                try:
                    cost = float(v)
                    break
                except (TypeError, ValueError):
                    continue
        funded = (str(r.get(flag_col, "")).strip().lower() in ("true", "1", "yes")
                  if flag_col else cost > 0)
        if funded:
            pkg_counts[pkg] = pkg_counts.get(pkg, 0) + 1
        pkg_costs[pkg] = pkg_costs.get(pkg, 0.0) + cost
    if not (has_cost or flag_col):
        pkg_counts = {}

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
        for n, pkg_id, ctx in _nc._scan_package_counts(text, vocab):
            canonical = pkg_counts.get(pkg_id)
            if canonical is None:
                continue
            if abs(n - canonical) / max(canonical, 1) > _COUNT_DRIFT_THRESHOLD:
                anchor = ctx.strip()[:20]
                line_no = (text.count("\n", 0, text.find(anchor)) + 1
                           if anchor in text else 1)
                violations.append({
                    "id": f"CA-C-{len(violations) + 1:03d}",
                    "severity": "HIGH",
                    "duty": "cross_file_counts",
                    "location": f"{prose_file}:~{line_no}",
                    "claim": (
                        f"Prose says {n} units for package {pkg_id!r}, "
                        f"but allocation CSV has {canonical}. "
                        f"Drift {abs(n - canonical)/canonical*100:.1f}%. "
                        f"Context: …{ctx.strip()[:120]}…"
                    ),
                    "canonical_source": "allocation CSV",
                    "canonical_value": canonical,
                    "drifted_value": n,
                })

        # Sub-duty 2b: package costs (the $197M conflation class).
        # Two readings: (a) cost of units assigned exactly this package;
        # (b) cost of every package that contains this package's
        # components (e.g. "a" inside "a_plus_b"). Flag only when the
        # prose differs from BOTH.
        for hit in _scan_dollar_amount_with_package(text, vocab):
            pkg_id = hit["package_token"]
            amt = hit["dollar_usd"]
            canonical_a = pkg_costs.get(pkg_id)
            if canonical_a is None:
                continue
            toks = _pkg_tokens(pkg_id)
            canonical_b = sum(c for k, c in pkg_costs.items()
                              if toks <= _pkg_tokens(k))
            if canonical_a == 0 and canonical_b == 0:
                continue  # package not funded: prose is a comparison/counterfactual
            drift_a = abs(amt - canonical_a) / max(canonical_a, 1)
            drift_b = abs(amt - canonical_b) / max(canonical_b, 1)
            if drift_a > _COUNT_DRIFT_THRESHOLD and \
                    drift_b > _COUNT_DRIFT_THRESHOLD:
                line_no = text.count("\n", 0, hit["match_start"]) + 1
                violations.append({
                    "id": f"CA-C-{len(violations) + 1:03d}",
                    # MEDIUM by default (can be defensible rounding);
                    # HIGH when > 25% off both readings.
                    "severity": ("HIGH" if drift_a > 0.25 and drift_b > 0.25
                                 else "MEDIUM"),
                    "duty": "cross_file_counts",
                    "location": f"{prose_file}:~{line_no}",
                    "claim": (
                        f"Prose says ${amt/1e6:.1f}M for package "
                        f"{pkg_id!r}, but CSV-computed cost is "
                        f"${canonical_a/1e6:.1f}M (units assigned this "
                        f"exact package) or ${canonical_b/1e6:.1f}M "
                        f"(any package containing it). "
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
    return violations


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
# Duty 4 — Ledger binding (Phase 18 β + δ, Phase 21 γ)
# ---------------------------------------------------------------------------
#
# Phase 21 γ binding rule. A prose number binds to a ledger value only
# when the NEAREST same-kind ledger value (value or CI endpoint) renders
# to the prose figure at the prose figure's own precision: |ledger −
# prose| < one unit of the last displayed digit ("$197M" binds 197.8M,
# "$335M" does not bind 320M; "52.4%" does not bind 50%). The Phase 18 δ
# flat 5% band let nearby invented numbers through.

# Generic-use qualifiers for verdict labels — when these phrases
# appear in the 25-char window around a label match, the label is
# treated as English usage, not a verdict claim.
_LEDGER_BINDING_GENERIC_USE_QUALIFIERS = (
    "robust to", "robust against", "robust standard",
    "sensitive to", "sensitive analysis", "sensitive parameters",
    "sensitivity analysis",
    "supported by", "supported in", "supported via",
    "refuted by", "refuted in",
    "inconclusive evidence", "inconclusive results",
)

_VERDICT_WORDS = ("ROBUST", "SENSITIVE", "UNSTABLE", "SUPPORTED",
                  "REFUTED", "INCONCLUSIVE")

# A percentage is interval/test METADATA (not a claim) when the words
# right after it name the interval or test ("95% CI", "95% credible
# interval", "(95% CI: …)", "5% significance level") or the words right
# before it do ("coverage of 95%", "at the 5% level"). Phase 21 γ: the
# Phase 20 α ±35-char window also skipped claim values that merely sat
# near a CI ("99% (95% CI: 10-20%)" skipped the 99%) and matched
# " ci" inside "city".
_PCT_METADATA_RIGHT_RE = re.compile(
    r"^\s*\(?\s*(?:bayesian\s+)?(?:ci|cri|ui|pi|hdi|hpd)\b"
    r"|^\s*(?:bayesian\s+)?(?:confidence|credible|credibility|uncertainty|"
    r"prediction|posterior|highest[\s-]density)\s+"
    r"(?:interval|region|band|level|bound)"
    r"|^\s*(?:significance|confidence)\s+level"
    r"|^\s*level\b",
    re.IGNORECASE)
_PCT_METADATA_LEFT_RE = re.compile(
    # Only interval coverage is metadata; bare "coverage 80%" is
    # intervention coverage, a claim.
    r"(?:(?:ci|interval|nominal|credible)\s+coverage|significance|alpha|"
    r"confidence\s+level)\s*(?:of|=|:|at)?\s*$",
    re.IGNORECASE)

# Currency prefixes recognized in prose (amounts are compared to the
# ledger's currency values numerically; the code is kept for display).
_CURRENCY_PREFIX = r"(US\$|\$|USD\s?|EUR\s?|€|£|GBP\s?|₦|NGN\s?|KES\s?|INR\s?|₹)"

_MAG = {"b": 1e9, "bn": 1e9, "billion": 1e9,
        "m": 1e6, "mn": 1e6, "million": 1e6,
        "k": 1e3, "thousand": 1e3}
_MAG_DISPLAY = {"b": "B", "bn": "B", "billion": "B",
                "m": "M", "mn": "M", "million": "M",
                "k": "K", "thousand": "K"}

_NUMBER = r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"

_PAT_CURRENCY = re.compile(
    _CURRENCY_PREFIX + r"\s*" + _NUMBER +
    r"(?:\s*(billion|bn|million|mn|thousand|[BMK]|k)(?![A-Za-z]))?",
    re.IGNORECASE)
# Case-sensitive single-letter suffixes: "100m" is metres (a raster
# resolution), "5 b" is not a magnitude; "100M"/"5B"/"22K"/"22k" are.
_PAT_SCALAR = re.compile(
    r"(?<![\$\d.,€£₦₹])" + _NUMBER +
    r"\s*((?i:billion|million|thousand)|bn|mn|[BMK]|k)(?![A-Za-z])")
_PAT_PERCENT = re.compile(r"(?<![\d.,])(\d+(?:\.\d+)?)\s*%")
_PAT_PCT_RANGE = re.compile(
    r"(?<![\d.,])(\d+(?:\.\d+)?)\s*%?\s*(?:–|—|-|to)\s*\d+(?:\.\d+)?\s*%")
_PAT_COMMA_INT = re.compile(r"(?<![\d.,$€£₦₹])(\d{1,3}(?:,\d{3})+)(?![\d,]|\.\d)")
_PAT_RAW_CLAIM = re.compile(r"\[CLAIM:[^\]\n]*\]")
_PAT_LABEL = re.compile(
    r"\b((?:NOT|NON)[\s_\-]+)?(" + "|".join(_VERDICT_WORDS) + r")\b",
    re.IGNORECASE)

# Generic allocation-unit / count nouns (plural forms included via the
# regex). Domain-neutral: admin units, sites, modeling objects.
_COUNT_UNITS = (
    "lga", "lgas", "district", "districts", "county", "counties",
    "province", "provinces", "region", "regions", "zone", "zones",
    "state", "states", "ward", "wards", "commune", "communes",
    "municipality", "municipalities", "facility", "facilities",
    "site", "sites", "cluster", "clusters", "village", "villages",
    "archetype", "archetypes", "package", "packages", "draw", "draws",
    "restart", "restarts", "fold", "folds", "parameter", "parameters",
    "scenario", "scenarios", "unit", "units",
)
_PAT_COUNT = re.compile(
    rf"(?<![\d.,])(\d{{1,6}}(?:,\d{{3}})*)\s+({'|'.join(_COUNT_UNITS)})\b",
    re.IGNORECASE)


def _norm_label(s: str) -> str:
    return re.sub(r"[\s_\-]+", " ", str(s).strip()).upper()


def _last_digit_unit(num_str: str, multiplier: float) -> float:
    """One unit of the last displayed digit, in absolute terms."""
    s = num_str.replace(",", "")
    decimals = len(s.split(".", 1)[1]) if "." in s else 0
    return (10.0 ** -decimals) * multiplier


def _bound_to_nearest(value: float, unit: float,
                      candidates: list[float]) -> bool:
    """True when the nearest candidate is within one last-digit unit."""
    if not candidates:
        return False
    nearest = min(candidates, key=lambda c: abs(c - value))
    return abs(nearest - value) < unit * (1 - 1e-9) or nearest == value


def _within_proximity(value: float, bound_numeric_values,
                      tolerance: float = 0.05) -> bool:
    """Kept for API compatibility (Phase 18 δ). Phase 21 γ binding uses
    `_bound_to_nearest` instead."""
    for bv in bound_numeric_values or ():
        denom = max(abs(value), abs(bv), 1.0)
        if abs(value - bv) / denom <= tolerance:
            return True
    return False


def _line_ctx(text: str, start: int, end: int, pad: int = 60) -> tuple[int, str]:
    line_no = text.count("\n", 0, start) + 1
    ctx = text[max(0, start - pad):min(len(text), end + pad)]
    return line_no, ctx.replace("\n", " ").strip()[:140]


def _audit_ledger_binding(run_dir: str) -> list[dict]:
    """Verify every numeric / verdict-label in report.md is ledger-bound.

    Scans (outside fenced code blocks; inline code is NOT exempt for
    numbers — a reader still sees them):
      - Currency amounts ("$320M", "USD 900 million", "$1,200,000")
      - Bare scalars with magnitude suffix ("7.47M DALYs", "1.2 billion")
      - Unsuffixed comma-grouped numbers ("412,331 cases")
      - Percentages, including both ends of ranges ("10–20%")
      - Counts in count-unit contexts ("63 districts")
      - Verdict labels, negation-aware ("NOT SUPPORTED" ≠ SUPPORTED)
      - Raw `[CLAIM:...]` tokens (any id shape) that escaped rendering

    Per-unit prices ("$1,500 per DALY", "$30/case") are skipped.

    No-op when either claims_ledger.yaml or report.md is absent (the
    `_check_claims_ledger_present` check fires separately).
    """
    ledger_path = os.path.join(run_dir, "models", "claims_ledger.yaml")
    report_path = os.path.join(run_dir, "report.md")
    if not os.path.exists(ledger_path) or not os.path.exists(report_path):
        return []
    try:
        with open(ledger_path) as f:
            doc = yaml.safe_load(f) or {}
    except (yaml.YAMLError, OSError):
        return []
    ledger_claims = doc.get("claims") if isinstance(doc, dict) else None
    if not isinstance(ledger_claims, list):
        return []
    try:
        with open(report_path) as f:
            text = f.read()
    except OSError:
        return []

    nums: dict[str, list[float]] = {k: [] for k in
                                    ("scalar", "currency", "percentage", "count")}
    labels: set[str] = set()
    for c in ledger_claims:
        if not isinstance(c, dict):
            continue
        kind = str(c.get("claim_kind") or "").lower()
        v = c.get("value")
        if v is None:
            continue
        if kind == "label":
            labels.add(_norm_label(v))
            continue
        if kind not in nums:
            continue
        for fld in ("value", "ci_low", "ci_high"):
            x = c.get(fld)
            if x is None or isinstance(x, bool):
                continue
            try:
                fx = float(x)
            except (TypeError, ValueError):
                continue
            if fx == fx:
                nums[kind].append(fx)

    fenced = code_spans(text, inline=False)
    with_inline = code_spans(text, inline=True)
    unbound: list[dict] = []
    seen: set = set()

    def _emit(key, line_no: int, claim: str, drifted: str) -> None:
        if key in seen:
            return
        seen.add(key)
        unbound.append({
            "id": f"CA-LB-{len(unbound) + 1:03d}",
            "severity": "HIGH",
            "duty": "ledger_binding",
            "location": f"report.md:{line_no}",
            "claim": claim,
            "canonical_source": "models/claims_ledger.yaml",
            "drifted_value": drifted,
        })

    def _is_per_unit(end: int) -> bool:
        return bool(_PER_UNIT_SUFFIX_RE.match(text, end))

    claimed_spans: list[tuple[int, int]] = []

    # Currency
    for m in _PAT_CURRENCY.finditer(text):
        if in_spans(fenced, m.start()):
            continue
        claimed_spans.append((m.start(), m.end()))
        num_s, mag = m.group(2), (m.group(3) or "").lower()
        mult = _MAG.get(mag, 1.0)
        amt = float(num_s.replace(",", "")) * mult
        if not mag and amt < 1_000:
            continue  # small bare amounts are prices, not portfolio claims
        if _is_per_unit(m.end()):
            continue
        if _bound_to_nearest(amt, _last_digit_unit(num_s, mult),
                             nums["currency"]):
            continue
        display = f"{m.group(1).strip()}{num_s}{_MAG_DISPLAY.get(mag, '')}"
        line_no, ctx = _line_ctx(text, m.start(), m.end())
        _emit(("cur", display), line_no,
              f"Currency `{display}` in prose is not in claims_ledger.yaml "
              f"(no ledger currency value renders to it). Every "
              f"quantitative claim must be `[CLAIM:id]`-referenced and "
              f"rendered from the ledger. Context: …{ctx}…", display)

    def _inside_claimed(pos: int) -> bool:
        return any(s <= pos < e for s, e in claimed_spans)

    # Scalars with magnitude suffix
    for m in _PAT_SCALAR.finditer(text):
        if in_spans(fenced, m.start()) or _inside_claimed(m.start()):
            continue
        claimed_spans.append((m.start(), m.end()))
        num_s, mag = m.group(1), m.group(2).lower()
        mult = _MAG[mag]
        val = float(num_s.replace(",", "")) * mult
        if _is_per_unit(m.end()):
            continue
        if _bound_to_nearest(val, _last_digit_unit(num_s, mult),
                             nums["scalar"] + nums["count"]):
            continue
        display = f"{num_s}{_MAG_DISPLAY[mag]}"
        line_no, ctx = _line_ctx(text, m.start(), m.end(), 50)
        _emit(("scalar", display), line_no,
              f"Scalar `{display}` in prose is not in claims_ledger.yaml. "
              f"Every scalar/count must be `[CLAIM:id]`-referenced from "
              f"the ledger. Context: …{ctx}…", display)

    # Percentages (and lower ends of ranges)
    pct_hits: list[tuple[int, int, str]] = []
    for m in _PAT_PERCENT.finditer(text):
        pct_hits.append((m.start(1), m.end(), m.group(1)))
    for m in _PAT_PCT_RANGE.finditer(text):
        pct_hits.append((m.start(1), m.end(1), m.group(1)))
    for start, end, num_s in sorted(set(pct_hits)):
        if in_spans(fenced, start) or _inside_claimed(start):
            continue
        after = text[end:end + 40]
        if after.startswith("%"):
            after = after[1:]
        if _PCT_METADATA_RIGHT_RE.match(after) or \
                _PCT_METADATA_LEFT_RE.search(text[max(0, start - 30):start]):
            continue
        pct = float(num_s)
        if _bound_to_nearest(pct, _last_digit_unit(num_s, 1.0),
                             nums["percentage"]):
            continue
        display = f"{num_s}%"
        line_no, _ = _line_ctx(text, start, end)
        _emit(("pct", display), line_no,
              f"Percentage `{display}` in prose is not in "
              f"claims_ledger.yaml (no ledger percentage renders to it).",
              display)

    # Counts in count-unit contexts
    for m in _PAT_COUNT.finditer(text):
        if in_spans(fenced, m.start()) or _inside_claimed(m.start()):
            continue
        claimed_spans.append((m.start(), m.end()))
        raw_n = m.group(1)
        n = int(raw_n.replace(",", ""))
        if n >= 1900 and "," not in raw_n:
            continue  # bare 4-digit numbers before a unit are usually years
        if _bound_to_nearest(float(n), 1.0, nums["count"] + nums["scalar"]):
            continue
        display = f"{raw_n} {m.group(2)}"
        line_no, ctx = _line_ctx(text, m.start(), m.end(), 50)
        _emit(("count", display.lower()), line_no,
              f"Count `{display}` in prose is not in claims_ledger.yaml. "
              f"Every count claim must be `[CLAIM:id]`-referenced from the "
              f"ledger. Context: …{ctx}…", display)

    # Unsuffixed comma-grouped numbers ("412,331 cases")
    for m in _PAT_COMMA_INT.finditer(text):
        if in_spans(fenced, m.start()) or _inside_claimed(m.start()):
            continue
        if text[m.end():m.end() + 2].lstrip().startswith("%"):
            continue
        n = float(m.group(1).replace(",", ""))
        if _bound_to_nearest(n, 1.0, nums["scalar"] + nums["count"]
                             + nums["currency"]):
            continue
        display = m.group(1)
        line_no, ctx = _line_ctx(text, m.start(), m.end(), 50)
        _emit(("comma", display), line_no,
              f"Number `{display}` in prose is not in claims_ledger.yaml. "
              f"Context: …{ctx}…", display)

    # Verdict labels (negation-aware)
    for m in _PAT_LABEL.finditer(text):
        if in_spans(fenced, m.start()):
            continue
        neg = m.group(1)
        word = m.group(2)
        label = _norm_label(("NOT " if neg else "") + word)
        win = text[max(0, m.start() - 25):min(len(text), m.end() + 25)].lower()
        if not neg and any(q in win for q in
                           _LEDGER_BINDING_GENERIC_USE_QUALIFIERS):
            continue
        if label in labels:
            continue
        line_no, _ = _line_ctx(text, m.start(), m.end())
        raw = m.group(0)
        _emit(("label", label), line_no,
              f"Verdict label {raw!r} in prose is not in claims_ledger.yaml"
              + (" (the ledger holds the un-negated label, which this "
                 "phrase contradicts)" if neg and _norm_label(word) in labels
                 else "")
              + ". Verdict labels must be `[CLAIM:id]`-referenced from the "
                "ledger.", label)

    # Raw `[CLAIM:...]` tokens that escaped rendering (any id shape —
    # `[CLAIM:foo-bar]`, `[CLAIM:id:NO_VALUE]`). Inline/fenced code
    # documenting the syntax is exempt.
    for m in _PAT_RAW_CLAIM.finditer(text):
        if in_spans(with_inline, m.start()):
            continue
        token = m.group(0)
        line_no, _ = _line_ctx(text, m.start(), m.end())
        _emit(("raw_claim", token), line_no,
              f"Raw `{token}` reference survived in report.md — "
              f"`scripts/render_claims.py` was not invoked or failed "
              f"to substitute. Run STAGE 8.1: "
              f"`python3 scripts/render_claims.py {{run_dir}}`.", token)

    return unbound


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _sha256_or_none(path: str) -> str | None:
    import hashlib
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def audit_run(run_dir: str) -> dict:
    """Run all four duties and return a structured result dict.

    Phase 21 γ: `inputs_sha256` records the report and ledger the audit
    saw, so a consumer can tell a stale coherence_audit.yaml (report
    edited after the audit) from a current one.
    """
    label_v = _audit_label_coherence(run_dir)
    counts_v = _audit_cross_file_counts(run_dir)
    self_v = _audit_self_contradicting(run_dir)
    ledger_v = _audit_ledger_binding(run_dir)
    all_v = label_v + counts_v + self_v + ledger_v
    has_high = any(v["severity"] == "HIGH" for v in all_v)
    return {
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "duties": {
            "label_coherence": {"violations": label_v, "n": len(label_v)},
            "cross_file_counts": {"violations": counts_v, "n": len(counts_v)},
            "self_contradicting": {"violations": self_v, "n": len(self_v)},
            "ledger_binding": {"violations": ledger_v, "n": len(ledger_v)},
        },
        "violations": all_v,
        "verdict": "DRIFT_DETECTED" if all_v else "CLEAN",
        "has_high": has_high,
        "inputs_sha256": {
            "report": _sha256_or_none(os.path.join(run_dir, "report.md")),
            "ledger": _sha256_or_none(
                os.path.join(run_dir, "models", "claims_ledger.yaml")),
        },
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

    # T7: ledger_binding duty — ledger absent → silent
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "report.md"), "w") as f:
            f.write("# Report\nValue: 7.47M DALYs.\n")
        v = _audit_ledger_binding(d)
        ok(v == [], f"T7: no ledger → silent, got {v}")

    # T8: ledger_binding duty — fully bound report → no violations
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "models", "claims_ledger.yaml"), "w") as f:
            yaml.safe_dump({"claims": [
                {"id": "dalys", "claim_kind": "scalar",
                 "value": 7467997, "ci_low": 5140000, "ci_high": 10420000},
                {"id": "verdict", "claim_kind": "label", "value": "ROBUST"},
                {"id": "share", "claim_kind": "percentage", "value": 52.5},
                {"id": "budget", "claim_kind": "currency", "value": 320000000},
            ]}, f)
        with open(os.path.join(d, "report.md"), "w") as f:
            f.write("# Report\n\nThe optimized allocation averts 7.47M "
                    "DALYs (95% CI: 5.14M-10.42M). North West receives "
                    "52.5% of the $320M budget. Verdict: ROBUST.\n")
        v = _audit_ledger_binding(d)
        ok(v == [],
           f"T8: fully bound report → 0 violations, got {len(v)}: {v[:2]}")

    # T9: ledger_binding — unbound currency → HIGH
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "models", "claims_ledger.yaml"), "w") as f:
            yaml.safe_dump({"claims": [
                {"id": "budget", "claim_kind": "currency", "value": 320000000},
            ]}, f)
        with open(os.path.join(d, "report.md"), "w") as f:
            # $999M is NOT in the ledger; should fire
            f.write("# Report\n\nThe budget is $320M but the ask was $999M.\n")
        v = _audit_ledger_binding(d)
        ok(any("$999" in x.get("drifted_value", "") for x in v),
           f"T9: unbound $999M should fire HIGH ledger_binding, got {v}")

    # T10: ledger_binding — unbound verdict label → HIGH
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "models", "claims_ledger.yaml"), "w") as f:
            yaml.safe_dump({"claims": [
                {"id": "verdict", "claim_kind": "label", "value": "ROBUST"},
            ]}, f)
        with open(os.path.join(d, "report.md"), "w") as f:
            f.write("# Report\n\nVerdict was UNSTABLE per the latest run.\n")
        v = _audit_ledger_binding(d)
        ok(any(x.get("drifted_value") == "UNSTABLE" for x in v),
           f"T10: unbound UNSTABLE should fire ledger_binding, got {v}")

    # T11: generic-use qualifier ("sensitivity analysis") must not fire
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "models", "claims_ledger.yaml"), "w") as f:
            yaml.safe_dump({"claims": [
                {"id": "verdict", "claim_kind": "label", "value": "ROBUST"},
            ]}, f)
        with open(os.path.join(d, "report.md"), "w") as f:
            f.write("# Report\n\n## Sensitivity analysis\nWe ran a "
                    "sensitivity analysis using three perturbations.\n")
        v = _audit_ledger_binding(d)
        ok(not any("SENSITIVE" in x.get("drifted_value", "") for x in v),
           f"T11: 'sensitivity analysis' (heading) must not fire SENSITIVE, got {v}")

    # T12: proximity is claim-kind-aware. A count near a percentage
    # must not bind the percentage claim.
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "models", "claims_ledger.yaml"), "w") as f:
            yaml.safe_dump({"claims": [
                {"id": "lga_count", "claim_kind": "count", "value": 53},
            ]}, f)
        with open(os.path.join(d, "report.md"), "w") as f:
            f.write("# Report\n\nCoverage increased by 52.5%.\n")
        v = _audit_ledger_binding(d)
        ok(any(x.get("drifted_value") == "52.5%" for x in v),
           f"T12: count=53 must not proximity-bind 52.5%, got {v}")

    # T13: unbound bare count in a count-unit context → HIGH.
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "models", "claims_ledger.yaml"), "w") as f:
            yaml.safe_dump({"claims": [
                {"id": "other_count", "claim_kind": "count", "value": 63},
            ]}, f)
        with open(os.path.join(d, "report.md"), "w") as f:
            f.write("# Report\n\nThe analysis covers 774 LGAs.\n")
        v = _audit_ledger_binding(d)
        ok(any(x.get("drifted_value") == "774 LGAs" for x in v),
           f"T13: unbound 774 LGAs should fire ledger_binding, got {v}")

    # T14: ledger-bound count in a count-unit context → silent.
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "models", "claims_ledger.yaml"), "w") as f:
            yaml.safe_dump({"claims": [
                {"id": "lga_count", "claim_kind": "count", "value": 774},
            ]}, f)
        with open(os.path.join(d, "report.md"), "w") as f:
            f.write("# Report\n\nThe analysis covers 774 LGAs.\n")
        v = _audit_ledger_binding(d)
        ok(not any(x.get("drifted_value") == "774 LGAs" for x in v),
           f"T14: ledger-bound 774 LGAs should be silent, got {v}")

    # T15 (Phase 20 α): Bayesian / credible-interval / significance
    # prose must NOT fire HIGH on the percentage scan. The prior
    # right-window-only check missed all four shapes below; Phase 19 δ
    # sufficiency critic actively pushes the writer toward this prose.
    bayesian_prose_cases = [
        ("95% credible interval contains the estimate.", "95%"),
        ("Using a 95% Bayesian CI, the value lies within bounds.", "95%"),
        ("At a 95% (CI: 5.1M-10.4M), the model converges.", "95%"),
        ("At the 5% significance level we reject the null.", "5%"),
        ("The 90% credibility region excludes zero.", "90%"),
        ("The 95% credible region contains the estimate.", "95%"),
    ]
    for prose, expected_pct in bayesian_prose_cases:
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "models"))
            with open(os.path.join(d, "models", "claims_ledger.yaml"), "w") as f:
                yaml.safe_dump({"claims": [
                    {"id": "x", "claim_kind": "scalar", "value": 100},
                ]}, f)
            with open(os.path.join(d, "report.md"), "w") as f:
                f.write(f"# Report\n\n{prose}\n")
            v = _audit_ledger_binding(d)
            ok(not any(x.get("drifted_value") == expected_pct
                       and x.get("claim", "").startswith("Percentage")
                       for x in v),
               f"T15 Bayesian/CI prose {prose!r} should NOT fire "
               f"HIGH on {expected_pct}, got {v}")

    # T16 (Phase 20 α): legitimate percentage claim still fires HIGH
    # when not paired with CI/credible/significance language.
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "models", "claims_ledger.yaml"), "w") as f:
            yaml.safe_dump({"claims": [
                {"id": "share", "claim_kind": "percentage", "value": 52.5},
            ]}, f)
        with open(os.path.join(d, "report.md"), "w") as f:
            f.write("# Report\n\nNorth West gets 77.3% of the budget.\n")
        v = _audit_ledger_binding(d)
        ok(any(x.get("drifted_value") == "77.3%" for x in v),
           f"T16: legitimate unbound 77.3% must still fire, got {v}")

    # T17 (Phase 20 α): `[CLAIM:id]` inside a fenced code block must
    # NOT fire `raw_claim` HIGH (writer documents the syntax in a code
    # example).
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "models", "claims_ledger.yaml"), "w") as f:
            yaml.safe_dump({"claims": [
                {"id": "x", "claim_kind": "scalar", "value": 100},
            ]}, f)
        with open(os.path.join(d, "report.md"), "w") as f:
            f.write("# Report\n\nUse `[CLAIM:x]` to reference a claim. "
                    "Example:\n\n```\n[CLAIM:x]\n```\n\nIt renders.\n")
        v = _audit_ledger_binding(d)
        ok(not any(x.get("drifted_value", "").startswith("[CLAIM:")
                   for x in v),
           f"T17: [CLAIM:] inside fenced/inline code must not fire, got {v}")

    # T18 (Phase 20 α): currency/scalar inside a fenced code block must
    # NOT fire on values absent from the ledger (writer can show
    # examples without binding them).
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "models", "claims_ledger.yaml"), "w") as f:
            yaml.safe_dump({"claims": [
                {"id": "budget", "claim_kind": "currency", "value": 320000000},
            ]}, f)
        with open(os.path.join(d, "report.md"), "w") as f:
            f.write("# Report\n\nThe ledger holds `$320M`. Example "
                    "syntax:\n\n```\nbudget: $999M\n```\n")
        v = _audit_ledger_binding(d)
        ok(not any("$999" in x.get("drifted_value", "") for x in v),
           f"T18: $999M inside a code fence must not fire, got {v}")

    # ---- Phase 21 γ ---------------------------------------------------- #

    def _lb(claims: list[dict], prose: str) -> list[dict]:
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "models"))
            with open(os.path.join(d, "models", "claims_ledger.yaml"), "w") as f:
                yaml.safe_dump({"claims": claims}, f)
            with open(os.path.join(d, "report.md"), "w") as f:
                f.write(prose)
            return _audit_ledger_binding(d)

    def _drifts(v: list[dict]) -> set[str]:
        return {str(x.get("drifted_value")) for x in v}

    # T19: decimal / comma tails of a bound currency are not separate
    # scalars ("$251.6M" ≠ "6M"; "$1,200M" ≠ "200M").
    v = _lb([{"id": "a", "claim_kind": "currency", "value": 251_600_000},
             {"id": "b", "claim_kind": "currency", "value": 1_200_000_000}],
            "Costs: | $251.6M | and a $1,200M envelope.\n")
    ok(not v, f"T19: bound decimal/comma currency must be silent, got {v}")

    # T20: per-unit prices with a space before "per" are skipped for any
    # unit word.
    v = _lb([{"id": "x", "claim_kind": "scalar", "value": 1}],
            "Cost-effectiveness is $1,500 per DALY and $1,250 per child "
            "vaccinated; also $30/case.\n")
    ok(not any(x.get("drifted_value", "").startswith("$") for x in v),
       f"T20: per-unit prices must be skipped, got {v}")

    # T21: a claim next to a CI is still checked; " ci" inside "city" is
    # not CI metadata.
    v = _lb([{"id": "e", "claim_kind": "percentage", "value": 15,
              "ci_low": 10, "ci_high": 20}],
            "Efficacy is 99% (95% CI: 10-20%). 87% of the city population.\n")
    ok({"99%", "87%"} <= _drifts(v) and "95%" not in _drifts(v)
       and "10%" not in _drifts(v),
       f"T21: 99%/87% must fire, 95%/10% must not, got {_drifts(v)}")

    # T22: negation-aware labels.
    v = _lb([{"id": "h", "claim_kind": "label", "value": "SUPPORTED"}],
            "Hypothesis H2 is NOT SUPPORTED by the model.\n")
    ok("NOT SUPPORTED" in _drifts(v), f"T22: negated label must fire, got {v}")
    v = _lb([{"id": "r", "claim_kind": "label", "value": "NOT_ROBUST"}],
            "The allocation is NOT ROBUST to the vaccine efficacy prior.\n")
    ok(not v, f"T22b: NOT_ROBUST ↔ 'NOT ROBUST' must bind, got {v}")

    # T23: binding is to the nearest ledger value at the prose figure's
    # precision — nearby invented numbers no longer pass.
    v = _lb([{"id": "b", "claim_kind": "currency", "value": 320_000_000},
             {"id": "s", "claim_kind": "percentage", "value": 50},
             {"id": "n", "claim_kind": "count", "value": 774},
             {"id": "c", "claim_kind": "currency", "value": 197_800_000}],
            "Budget $335M; share 52.4%; we fund 740 districts. "
            "Component cost $197M.\n")
    ok({"$335M", "52.4%", "740 districts"} <= _drifts(v),
       f"T23: near-miss numbers must fire, got {_drifts(v)}")
    ok("$197M" not in _drifts(v), f"T23: $197M binds 197.8M, got {_drifts(v)}")

    # T24: unsuffixed comma numbers, both range ends, "billion" display,
    # numbers in inline code, non-$ currency prefixes.
    v = _lb([{"id": "x", "claim_kind": "scalar", "value": 1}],
            "The plan averts 412,331 cases; coverage 10–20%; "
            "cost 1.2 billion doses; ledger shows `$999M`; "
            "USD 900 million total.\n")
    d24 = _drifts(v)
    ok({"412,331", "10%", "20%", "1.2B", "$999M"} <= d24,
       f"T24: expected unbound tokens, got {d24}")
    ok(any(x.startswith("USD") and x.endswith("900M") for x in d24),
       f"T24: USD-prefixed currency must be scanned, got {d24}")

    # T25: raw `[CLAIM:...]` tokens with hyphen/dot ids are flagged.
    v = _lb([{"id": "x", "claim_kind": "scalar", "value": 1}],
            "Population raster: WorldPop 100m grid; 30 km buffer.\n")
    ok(not v, f"T24b: '100m' (metres) is not a magnitude, got {v}")

    v = _lb([{"id": "x", "claim_kind": "scalar", "value": 1}],
            "Averts [CLAIM:cases-averted] cases ([CLAIM:h2.verdict]).\n")
    ok({"[CLAIM:cases-averted]", "[CLAIM:h2.verdict]"} <= _drifts(v),
       f"T25: raw hyphen/dot tokens must fire, got {_drifts(v)}")

    # T26: audit records input hashes for staleness detection.
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "report.md"), "w") as f:
            f.write("# R\n")
        res = audit_run(d)
        ok(isinstance(res.get("inputs_sha256"), dict)
           and len(res["inputs_sha256"].get("report") or "") == 64
           and res["inputs_sha256"].get("ledger") is None,
           f"T26: inputs_sha256 must hash report, got {res.get('inputs_sha256')}")

    # T27: cross-file counts with a non-malaria package vocabulary taken
    # from the allocation CSV (measles campaign vs routine).
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "allocation_optimized.csv"), "w") as f:
            f.write("district,zone,package,cost_usd\n")
            for i in range(30):
                f.write(f"D{i},Coast,sia_mr,100000\n")
            for i in range(30, 40):
                f.write(f"D{i},Coast,routine_mcv2,20000\n")
        with open(os.path.join(d, "report.md"), "w") as f:
            f.write("## Allocation\n\nIn total 45 SIA districts are "
                    "funded; the SIA campaign costs $9.0M overall.\n")
        v = _audit_cross_file_counts(d)
        ok(any(x["drifted_value"] == 45 and x["canonical_value"] == 30
               for x in v), f"T27: 45 vs 30 SIA must fire, got {v}")
        ok(any("$9.0M" == x["drifted_value"] for x in v),
           f"T27: $9.0M vs $3.0M SIA cost must fire, got {v}")
        with open(os.path.join(d, "report.md"), "w") as f:
            f.write("Each district receives SIA at a budget share of "
                    "$12M/40 under the uniform rule.\n")
        v = _audit_cross_file_counts(d)
        ok(not v, f"T27b: '$12M/40' is a per-unit share, got {v}")

    # ---- Phase 21 precision pass (before/after retro on 18 runs) ------ #
    def _label_run(d, verdict, text, fname="results.md"):
        os.makedirs(os.path.join(d, "models"), exist_ok=True)
        with open(os.path.join(d, "models", "sensitivity_analysis.yaml"), "w") as f:
            yaml.safe_dump({"verdict": verdict, "load_bearing_parameters": []}, f)
        with open(os.path.join(d, fname), "w") as f:
            f.write(text)
        return _audit_label_coherence(d)

    # LP1/LP2 (TP): prose asserts a different verdict for the recommendation.
    with tempfile.TemporaryDirectory() as d:
        v = _label_run(d, "SENSITIVE", "The primary recommendation is ROBUST "
                       "across all tested perturbations.\n")
        ok(any(x["drifted_value"] == "ROBUST" for x in v), f"LP1 must fire, got {v}")
    with tempfile.TemporaryDirectory() as d:
        v = _label_run(d, "SENSITIVE", "Sensitivity verdict: UNSTABLE (two "
                       "perturbations flip the package; 95% CI excludes zero).\n")
        ok(any(x["drifted_value"] == "UNSTABLE" for x in v), f"LP2 must fire, got {v}")
    with tempfile.TemporaryDirectory() as d:
        v = _label_run(d, "UNSTABLE", "The school-vax recommendation is robust "
                       "across all sensitivity perturbations.\n")
        ok(any(x["drifted_value"] == "ROBUST" for x in v), f"LP3 must fire, got {v}")
    # LN1-LN6 (FP patterns): thresholds, other metrics, sub-claims about
    # another quantity, meta-text, negation, generic adjectives.
    for tag, verdict, text in (
        ("LN1 threshold", "SENSITIVE", "FRAGILE threshold = 0.40; ROBUST threshold = 0.70.\n"),
        ("LN2 other metric", "SENSITIVE", "Worst-fold Spearman rho of 0.98 (ROBUST) "
         "but concordance 76%.\n"),
        ("LN3 sub-claim", "SENSITIVE", "Geographic prioritization (Region A first) is "
         "robust across all parameter perturbations.\n"),
        ("LN4 meta", "SENSITIVE", "The verdict was corrected from UNSTABLE to SENSITIVE "
         "in the authoritative YAML.\n"),
        ("LN5 negation", "SENSITIVE", "This is not a robust epidemiological result.\n"),
        ("LN6 adjective", "SENSITIVE", "The effect size is robust (narrow CI: 0.25-0.29) "
         "and the evidence base is the most robust finding here.\n"),
    ):
        with tempfile.TemporaryDirectory() as d:
            v = _label_run(d, verdict, text)
            ok(not v, f"{tag}: must not fire, got {v}")

    # CP1 (TP) / CN1-CN4 (FP): package-cost claims against the CSV.
    def _cost_run(d, text, rows=None, question="Allocate a fixed $40M budget."):
        os.makedirs(os.path.join(d, "models"), exist_ok=True)
        rows = rows or ([{"unit": f"a{i}", "zone": "Region A", "package": "school_vax",
                          "cost_usd": 640_000} for i in range(13)]
                        + [{"unit": f"b{i}", "zone": "Region B", "package": "routine_cov",
                            "cost_usd": 1_000_000} for i in range(30)])
        cols = list(rows[0].keys())
        with open(os.path.join(d, "models", "allocation_result.csv"), "w") as f:
            f.write(",".join(cols) + "\n")
            for r in rows:
                f.write(",".join(str(r[c]) for c in cols) + "\n")
        with open(os.path.join(d, "metadata.json"), "w") as f:
            json.dump({"question": question}, f)
        with open(os.path.join(d, "decision_rule.md"), "w") as f:
            f.write(text)
        return [x for x in _audit_cross_file_counts(d) if x["severity"] == "HIGH"]

    with tempfile.TemporaryDirectory() as d:
        v = _cost_run(d, "One exception: unit a1 receives school-vax at a cost of "
                         "$12.6M because its cost per case is below the CE threshold.\n")
        ok(any("school_vax" in x["claim"] and "$12.6M" in x["claim"] for x in v),
           f"CP1: $12.6M vs $8.3M school-vax cost must fire, got {v}")
    for tag, text in (
        ("CN1 run envelope", "School-vax entry at $40M is cost-effective.\n"),
        ("CN2 unspent", "The optimizer selects zero routine-cov, leaving $9.0M unallocated.\n"),
        ("CN3 other package between", "School-vax lags; routine-cov accounts for $30.0M.\n"),
        ("CN4 other scenario", "At $60M, school-vax would reach $20.0M of spend.\n"),
        ("CN6 multi-word name", "By package, routine-cov school accounts for $30.0M "
         "and school-vax accounts for $8.3M.\n"),
    ):
        with tempfile.TemporaryDirectory() as d:
            v = _cost_run(d, text)
            ok(not v, f"{tag}: must not fire, got {v}")
    # CN5: a candidate table (no cost / funded column) gives no count findings.
    with tempfile.TemporaryDirectory() as d:
        rows = [{"unit": f"u{i}", "package": "school_vax", "cost_per_unit": 10}
                for i in range(40)]
        v = _cost_run(d, "Allocation: 12 school-vax units.\n", rows=rows)
        ok(not v, f"CN5: candidate table must not yield counts, got {v}")

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
