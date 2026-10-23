#!/usr/bin/env python3
"""
Phase 12 Commit α — cross-file numeric consistency check.

The Phase 11 live run (104914) reached STAGE 7 ACCEPT but shipped with
at least 4 verifiable internal inconsistencies a domain expert would
catch in 5 minutes:

- A cost-per-case figure appeared as "$5.05/case" in 3 places of
  results.md while the same metric was "$4.71/case" elsewhere in the
  same file (CSV-authoritative: $4.71).
- figure_rationale.md showed "127.5M cases averted (+18.9%)" while
  the authoritative optimization_summary.json said "54.7M (+158%)".
- results.md title said "Round 4" after STAGE 7 ACCEPT at Round 6.

The unifying gap: no validator compares headline numbers ACROSS
sibling artifacts.

This script extracts labeled numeric claims from the report-level
files (results.md, figure_rationale.md, decision_rule.md, report.md)
and cross-references each against authoritative source data
(models/optimization_summary.json, models/package_evaluation.csv,
the allocation CSV, progress.md). Drift > 5% fires MEDIUM
`numeric_drift_detected`; drift > 25% fires HIGH
`numeric_drift_extreme`. Same-document contradictions (the same
labeled cost-per-outcome metric at different values in one file) fire
MEDIUM `same_doc_inconsistency`.

Phase 21 γ — disease/country agnostic. Earlier versions hardcoded one
country's admin unit ("LGA"), its universe size (774), its zone
abbreviations (NW/NE/…), one cost-per-case metric (a specific
intervention in a specific zone), and an intervention alias table. All
vocabulary now comes from the run itself (`RunVocabulary`):

  unit nouns   sanity_schema `allocation.unit_noun(s)` + generic
               admin/site nouns (district, county, LGA, facility, …)
  packages     distinct values of the allocation CSV `package` column,
               `package_label` values, auto-derived unique tokens, and
               sanity_schema `allocation.package_aliases`
  zones        distinct `zone` values, their word initials ("North
               West" → "NW"), and sanity_schema `allocation.zone_aliases`
  universe     sanity_schema `allocation.units_total`, else CSV row count
  outcomes     cases / infections / deaths / DALYs / … (+ schema
               `outcome.name`)

Usage:
    python3 scripts/numeric_consistency.py <run_dir>
    python3 scripts/numeric_consistency.py --self-test
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Optional

try:
    import yaml
except ImportError:  # yaml is optional for non-schema-aware callers
    yaml = None  # type: ignore


MEDIUM_DRIFT_THRESHOLD = 0.05   # 5%
HIGH_DRIFT_THRESHOLD = 0.25     # 25%

_ROUND_TITLE_RE = re.compile(r"Round\s*(\d+)", re.IGNORECASE)

# A number as written in prose: comma-grouped ("1,234,567") or plain
# ("54.7"). The lookbehind keeps "1,234" from being read as "234" and
# "$251.6M" from yielding "6".
_NUM = r"(?<![\d,.])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"

_MAGNITUDE = {
    "b": 1e9, "bn": 1e9, "billion": 1e9,
    "m": 1e6, "mn": 1e6, "million": 1e6,
    "k": 1e3, "thousand": 1e3,
}
_MAG_ALT = r"(billion|bn|million|mn|thousand|[BMK]|k)"

# Generic allocation-unit nouns (singular). A run adds its own via
# sanity_schema `allocation.unit_noun` / `unit_nouns`.
_DEFAULT_UNIT_NOUNS = (
    "LGA", "district", "county", "sub-county", "subcounty",
    "municipality", "commune", "woreda", "ward", "province",
    "facility", "site", "catchment", "unit", "health zone",
)

# Outcome nouns for "averts X <noun>" headline claims. The
# optimization_summary.json key is `total_<noun>_averted` /
# `uniform_<noun>_averted` (lower-case, plural as written here).
_DEFAULT_OUTCOME_NOUNS = (
    "cases", "infections", "deaths", "DALYs", "hospitalizations",
    "hospitalisations", "admissions", "episodes",
)

# Per-outcome unit price denominators ("$4.71/case", "$80 per DALY").
_DEFAULT_PRICE_UNITS = (
    "case", "infection", "death", "DALY", "person", "child", "dose",
    "hospitalization", "episode",
)

# Package values that denote "no intervention" rather than a package.
_NON_PACKAGE_VALUES = frozenset({
    "", "none", "null", "baseline", "status_quo", "no_intervention",
    "nothing", "bau", "business_as_usual",
})

# Tokens never used as auto-derived package aliases.
_ALIAS_STOPWORDS = frozenset({
    "and", "with", "plus", "only", "the", "pkg", "package", "full",
    "standard", "baseline", "high", "low", "cov", "coverage",
})

# Acronyms that never identify a package/zone entity in a price label.
_GENERIC_ACRONYMS = frozenset({
    "ci", "cri", "ui", "pi", "uq", "or", "rr", "hr", "irr", "usd", "us",
    "yr", "vs", "na", "n", "id", "ok", "csv", "md", "yaml", "json",
})

# Non-headline anchors: when these appear near a count, the count is a
# prediction / hypothesis / per-subset figure, not the realized total.
_NON_HEADLINE_TOKENS = (
    "<",
    "prediction",
    "predicted",
    "expected",
    "should",
    "may receive",
    "could receive",
    "hypothesis",
    "h1 ", "h2 ", "h3 ", "h4 ", "h5 ", "h6 ", "h7 ",
    "(h1)", "(h2)", "(h3)", "(h4)", "(h5)", "(h6)", "(h7)",
    "accuracy",
)

_TOTAL_LEFT_TOKENS = (" all ", "total of ", "allocate to ",
                      "allocated to ", "across ")
_TOTAL_RIGHT_TOKENS = (" allocated", " received", " receive",
                       " in total", " total")

# Match "78.1% of budget", "11% of cost", "10.8% of allocation".
_BUDGET_SHARE_RE = re.compile(
    r"(?<![\d.])(\d{1,3}(?:\.\d+)?)\s*%\s+of\s+(?:the\s+)?"
    r"(?:budget|cost|allocation|spend|envelope)",
    re.IGNORECASE,
)


def _parse_num(s: str) -> float:
    return float(s.replace(",", ""))


def _is_year_like(raw: str) -> bool:
    """A bare 4-digit 1900–2100 integer (no comma) is almost always a
    year in prose, not a count."""
    return ("," not in raw and "." not in raw and len(raw) == 4
            and 1900 <= int(raw) <= 2100)


def _flex(alias: str) -> str:
    """Regex for an alias that tolerates `_`, `-`, space, `+` separators
    ("dual_ai" matches "dual-AI", "dual AI")."""
    parts = [re.escape(p) for p in re.split(r"[_\-\s+]+", alias) if p]
    return r"[\s_\-+]?".join(parts)


def _alias_regex(aliases: list[str]) -> re.Pattern | None:
    alts = sorted({a for a in aliases if a}, key=len, reverse=True)
    if not alts:
        return None
    return re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(_flex(a) for a in alts)
                      + r")(?![A-Za-z0-9])", re.IGNORECASE)


def _plural_alt(noun: str) -> str:
    """Regex alternation for a noun and its plural."""
    base = _flex(noun)
    if noun.lower().endswith("y") and not noun.lower().endswith(("ay", "ey", "oy")):
        return rf"{_flex(noun[:-1])}(?:y|ies)"
    return rf"{base}(?:e?s)?"


def _initials(name: str) -> str | None:
    words = [w for w in re.split(r"[\s\-_]+", name) if w]
    if len(words) < 2:
        return None
    return "".join(w[0] for w in words).upper()


@dataclass
class RunVocabulary:
    """Phase 21 γ: per-run vocabulary replacing hardcoded domain terms."""
    unit_nouns: list[str] = field(default_factory=lambda: list(_DEFAULT_UNIT_NOUNS))
    packages: dict[str, list[str]] = field(default_factory=dict)
    zones: dict[str, list[str]] = field(default_factory=dict)
    universe: int | None = None
    outcome_nouns: list[str] = field(default_factory=lambda: list(_DEFAULT_OUTCOME_NOUNS))
    price_units: list[str] = field(default_factory=lambda: list(_DEFAULT_PRICE_UNITS))
    budget: float | None = None   # the question's envelope (scenario guard)

    # -- derived regexes (built lazily) --
    def unit_re(self) -> re.Pattern:
        alt = "|".join(_plural_alt(n) for n in
                       sorted(set(self.unit_nouns), key=len, reverse=True))
        return re.compile(rf"(?:{alt})(?![A-Za-z])", re.IGNORECASE)

    def package_res(self) -> dict[str, re.Pattern]:
        out = {}
        for pid, aliases in self.packages.items():
            rx = _alias_regex(aliases)
            if rx is not None:
                out[pid] = rx
        return out

    def zone_res(self) -> dict[str, re.Pattern]:
        out = {}
        for z, aliases in self.zones.items():
            rx = _alias_regex(aliases)
            if rx is not None:
                out[z] = rx
        return out

    def find_packages(self, text: str) -> list[tuple[int, int, str]]:
        """All package-alias matches in `text` as (start, end, id), with
        overlapping matches resolved to the longest."""
        hits: list[tuple[int, int, str]] = []
        for pid, rx in self.package_res().items():
            for m in rx.finditer(text):
                hits.append((m.start(), m.end(), pid))
        hits.sort(key=lambda h: (h[0], -(h[1] - h[0])))
        kept: list[tuple[int, int, str]] = []
        for h in hits:
            if kept and h[0] < kept[-1][1]:
                continue
            kept.append(h)
        return kept

    def find_zones(self, text: str) -> list[tuple[int, int, str]]:
        hits: list[tuple[int, int, str]] = []
        for z, rx in self.zone_res().items():
            for m in rx.finditer(text):
                hits.append((m.start(), m.end(), z))
        hits.sort()
        return hits

    def has_zone(self, text: str) -> bool:
        return bool(self.find_zones(text))


def _derive_package_aliases(pkg_ids: list[str],
                            labels: dict[str, set[str]]) -> dict[str, list[str]]:
    """Aliases per package id: the id itself, its CSV labels, and every
    token / adjacent-token bigram unique to that id among all ids."""
    tok_lists = {p: [t for t in re.split(r"[_\-\s+]+", p.lower()) if t]
                 for p in pkg_ids}
    grams: dict[str, set[str]] = {}
    for p, toks in tok_lists.items():
        cands = set()
        for t in toks:
            if len(t) >= 3 and not t.isdigit() and t not in _ALIAS_STOPWORDS:
                cands.add(t)
        for a, b in zip(toks, toks[1:]):
            if not (a.isdigit() and b.isdigit()):
                cands.add(f"{a}_{b}")
        grams[p] = cands
    owners: dict[str, set[str]] = {}
    for p, cands in grams.items():
        for c in cands:
            owners.setdefault(c, set()).add(p)
    # A token shared by several packages goes to the unique owner with
    # the fewest tokens (the "base" package: a bare "X" names package
    # `x_only` rather than `x_plus_y`); ties assign it to nobody.
    def _owner(c: str) -> str | None:
        ps = owners.get(c) or set()
        if len(ps) == 1:
            return next(iter(ps))
        sizes = sorted((len(tok_lists[q]), q) for q in ps)
        if len(sizes) >= 2 and sizes[0][0] < sizes[1][0]:
            return sizes[0][1]
        return None

    out: dict[str, list[str]] = {}
    for p in pkg_ids:
        aliases = [p]
        aliases += sorted(labels.get(p, ()))
        aliases += sorted(c for c in grams[p] if _owner(c) == p and c != p)
        out[p] = aliases
    return out


_JOINER_RE = re.compile(r"\s*(?:\+|&|/|\band\b|\bwith\b|\bplus\b)\s*",
                        re.IGNORECASE)


def _joined_combination(text: str, end: int, vocab: "RunVocabulary") -> bool:
    """True when the package alias ending at `end` is joined to another
    package alias ("X+Y", "X and Y") — a combination the single-alias
    match does not identify."""
    m = _JOINER_RE.match(text, end)
    if not m:
        return False
    nxt = vocab.find_packages(text[m.end():m.end() + 40])
    return bool(nxt and nxt[0][0] == 0)


def _schema_list(sec: dict, *keys: str) -> list[str]:
    out: list[str] = []
    for k in keys:
        v = sec.get(k)
        if isinstance(v, str) and v.strip():
            out.append(v.strip())
        elif isinstance(v, list):
            out += [str(x).strip() for x in v if str(x).strip()]
    return out


def build_vocabulary(run_dir: str,
                     alloc_rows: list[dict] | None = None,
                     schema: dict | None = None,
                     extra_rows: list[dict] | None = None) -> RunVocabulary:
    """Assemble the run's vocabulary from its allocation CSV rows (plus
    `extra_rows`, e.g. package_evaluation.csv, which contribute package
    and zone names but not the universe) and the sanity schema. Every
    field degrades to a generic default."""
    vocab = RunVocabulary()
    if schema is None:
        schema = _load_sanity_schema_dict(run_dir) or {}
    alloc = schema.get("allocation") if isinstance(schema.get("allocation"), dict) else {}
    for n in _schema_list(alloc, "unit_noun", "unit_nouns"):
        if n not in vocab.unit_nouns:
            vocab.unit_nouns.append(n)
    try:
        ut = alloc.get("units_total")
        if ut is not None and not isinstance(ut, bool):
            vocab.universe = int(ut)
    except (TypeError, ValueError):
        pass
    outcome = schema.get("outcome") if isinstance(schema.get("outcome"), dict) else {}
    oname = outcome.get("name")
    if isinstance(oname, str) and oname.strip():
        noun = oname.strip().split("_")[0]
        if noun and noun not in vocab.outcome_nouns:
            vocab.outcome_nouns.append(noun)

    if alloc_rows is None:
        path = _resolve_alloc_path(run_dir)
        alloc_rows = _read_csv_rows(path) if path else []
    def _cols(rows: list[dict]) -> tuple:
        return (_pick_col(rows, ("package", "assigned_package", "package_name")),
                _pick_col(rows, ("package_label", "package_display", "label")),
                _pick_col(rows, ("zone", "zone_name", "zone_abbr", "region")))
    cols_alloc = _cols(alloc_rows)
    cols_extra = _cols(list(extra_rows or []))
    pkg_ids: list[str] = []
    labels: dict[str, set[str]] = {}
    zones: list[str] = []
    tagged = [(r, cols_alloc) for r in alloc_rows] + \
        [(r, cols_extra) for r in (extra_rows or [])]
    for r, (pkg_col, label_col, zone_col) in tagged:
        if pkg_col:
            p = (r.get(pkg_col) or "").strip().lower()
            if p and p not in _NON_PACKAGE_VALUES and not p.startswith("baseline"):
                if p not in pkg_ids:
                    pkg_ids.append(p)
                if label_col:
                    lab = (r.get(label_col) or "").strip()
                    if lab and len(lab) <= 40:
                        labels.setdefault(p, set()).add(lab)
        if zone_col:
            z = (r.get(zone_col) or "").strip()
            if z and z not in zones:
                zones.append(z)
    if vocab.universe is None and alloc_rows:
        vocab.universe = len(alloc_rows)
    vocab.packages = _derive_package_aliases(pkg_ids, labels)
    for pid, extra in (alloc.get("package_aliases") or {}).items() \
            if isinstance(alloc.get("package_aliases"), dict) else []:
        key = str(pid).lower()
        vocab.packages.setdefault(key, [key])
        vocab.packages[key] += [str(a) for a in (extra or [])]
    for z in zones:
        aliases = [z]
        ini = _initials(z)
        if ini:
            aliases.append(ini)
        vocab.zones[z] = aliases
    for z, extra in (alloc.get("zone_aliases") or {}).items() \
            if isinstance(alloc.get("zone_aliases"), dict) else []:
        vocab.zones.setdefault(str(z), [str(z)])
        vocab.zones[str(z)] += [str(a) for a in (extra or [])]
    return vocab


def _has_zone_qualifier(window_text: str,
                        vocab: RunVocabulary | None = None) -> bool:
    """Return True if a per-zone qualifier or non-headline anchor
    appears in the window. Zones come from the run vocabulary (word-
    boundary matched, so 'zoNE' never matches a zone abbreviated 'NE')."""
    if vocab is not None and vocab.has_zone(window_text):
        return True
    w = window_text.lower()
    return any(tok in w for tok in _NON_HEADLINE_TOKENS)


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


# --------------------------------------------------------------------------- #
# Phase 21 precision guards. The before/after retro on 18 historical runs
# found 2/35 HIGH numeric_drift_extreme findings were real; the rest
# compared a sentence against a quantity it did not assert ("1 of 12
# perturbations" read as a package count, "NW + NE + NC receive 61.3%"
# attributed to NC, a $200M-scenario sentence checked against the $320M
# CSV, "SA-005" read as 5). A HIGH must only fire when the clause
# unambiguously asserts the quantity the artifact holds; otherwise abstain.
# --------------------------------------------------------------------------- #

_CLAUSE_BREAK_RE = re.compile(r"\n|[.;!?](?=\s)|\|")

# Clause words that move a number off the headline allocation: other
# scenarios, uncertainty summaries, hypotheses, thresholds.
_OFF_HEADLINE_RE = re.compile(
    r"\b(?:scenarios?|sensitivity|perturb\w*|alternative|counterfactual|"
    r"hypothetic\w*|if|would|could|upper|lower|UQ|draws?|mean|median|"
    r"percentile|level\s*\d|pessimistic|optimistic|status\s+quo|uniform|"
    r"predict\w*|expected|target|threshold|H\d{1,2}|hypothes\w*|"
    r"prior\s+round|previous\s+round|round\s+\d|earlier)\b",
    re.IGNORECASE)

# Clause words marking a count as a subset rather than the allocated total.
_SUBSET_RE = re.compile(
    r"\b(?:remaining|only|eligible|covered|uncovered|matched|unmatched|"
    r"missing|containing|subset|of\s+these|of\s+which|including|excluding|"
    r"weighted|shift\w*|switch\w*|chang\w*|flip\w*|mov\w*|gain\w*|los[est]\w*|"
    r"drop\w*|add\w*|new|additional|fewer|more|among|imputed|label(?:ed|led)?)\b",
    re.IGNORECASE)

# Narrower subset vocabulary for package counts ("Mix: 30 SIA, 20 MCV2"
# is a package count; "7 IRS units unmatched" and "15 units shift" are not).
_PACKAGE_SUBSET_RE = re.compile(
    r"\b(?:remaining|matched|unmatched|missing|imputed|excluding|of\s+these|"
    r"of\s+which|shift\w*|switch\w*|flip\w*|mov(?:e|es|ed|ing)\b|gain\w*|"
    r"los[est]\w*|drop\w*|added|additional|new|fewer|more)\b",
    re.IGNORECASE)

# Immediately before a number: identifiers ("SA-005", "PM-003", "H1",
# "round 3", "Level 0", "Table 2") and bounds ("> 40%", "up to 30").
_ID_PREFIX_RE = re.compile(
    r"(?:[A-Za-z][A-Za-z0-9]{0,6}[-_#.]|[A-Za-z]|§\s*|"
    r"\b(?:round|level|thread|step|stage|phase|tier|fold|option|version|"
    r"figure|fig|table|section|model|archetype|cluster|scenario|run|draw|"
    r"iteration|item|question|criterion|blocker|priority|no)\.?\s+)\Z",
    re.IGNORECASE)
_BOUND_PREFIX_RE = re.compile(
    r"(?:[<>≤≥~]|\bup\s+to|\bat\s+least|\bat\s+most|\bmore\s+than|"
    r"\bless\s+than|\bfewer\s+than|\bover|\bunder|\bnearly|\babove|\bbelow)\s*\Z",
    re.IGNORECASE)
_FRACTION_SUFFIX_RE = re.compile(r"\s*(?:of|out\s+of|/|in)\s+\d", re.IGNORECASE)
_DOLLAR_AMOUNT_RE = re.compile(
    r"\$\s*(\d+(?:\.\d+)?)\s*(billion|bn|million|mn|[BMK])\b", re.IGNORECASE)


def _clause_span(doc_text: str, start: int, end: int | None = None) -> tuple[int, int]:
    """(start, end) offsets of the clause (sentence / line / table cell)
    containing [start, end)."""
    end = start if end is None else end
    s = max(0, start - 400) if start > 400 else 0
    for m in _CLAUSE_BREAK_RE.finditer(doc_text, max(0, start - 400), start):
        s = m.end()
    m = _CLAUSE_BREAK_RE.search(doc_text, end)
    return s, (m.start() if m else len(doc_text))


def _clause(doc_text: str, start: int, end: int | None = None) -> str:
    """The clause (sentence / line / table cell) containing [start, end)."""
    s, e = _clause_span(doc_text, start, end)
    return doc_text[s:e]


def _id_or_bound_adjacent(doc_text: str, start: int) -> bool:
    """True when the number at `start` is part of an identifier or a bound."""
    left = doc_text[max(0, start - 24):start]
    return bool(_ID_PREFIX_RE.search(left) or _BOUND_PREFIX_RE.search(left))


def _mentions_other_budget(clause: str, budget: float | None) -> bool:
    """True when the clause names a dollar envelope other than the run's
    budget (a different budget scenario). Amounts within 2% of the run
    budget, and amounts too small to be an envelope, are ignored."""
    if not budget:
        return False
    for m in _DOLLAR_AMOUNT_RE.finditer(clause):
        v = float(m.group(1)) * _MAGNITUDE.get(m.group(2).lower(), 1.0)
        if v >= 0.25 * budget and abs(v - budget) > 0.02 * budget:
            return True
    return False


def _question_budget(run_dir: str) -> float | None:
    """The budget envelope named in metadata.json's question, if any."""
    try:
        with open(os.path.join(run_dir, "metadata.json")) as f:
            q = (json.load(f) or {}).get("question") or ""
    except (OSError, json.JSONDecodeError, AttributeError):
        return None
    m = _DOLLAR_AMOUNT_RE.search(q) if isinstance(q, str) else None
    if not m:
        return None
    return float(m.group(1)) * _MAGNITUDE.get(m.group(2).lower(), 1.0)


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


# --------------------------------------------------------------------------- #
# Authoritative sources
# --------------------------------------------------------------------------- #


def _read_csv_rows(path: str | None) -> list[dict]:
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, newline="", encoding="utf-8") as f:
            return [dict(r) for r in csv.DictReader(f)]
    except (OSError, UnicodeDecodeError, csv.Error):
        return []


def _pick_col(rows: list[dict], names: tuple[str, ...]) -> str | None:
    if not rows:
        return None
    cols = rows[0].keys()
    return next((c for c in names if c in cols), None)


# Allocation CSVs that are comparators, not the recommended allocation.
_COMPARATOR_CSV_RE = re.compile(
    r"status[_-]?quo|uniform|baseline|bau|counterfactual|universal", re.IGNORECASE)
# A budget tag in a file name ("allocation_optimal_200M.csv").
_CSV_BUDGET_TAG_RE = re.compile(r"(?<![A-Za-z0-9])(\d+(?:\.\d+)?)\s*([MBK])(?![A-Za-z])",
                                re.IGNORECASE)


def _resolve_alloc_path_ex(run_dir: str) -> tuple[str | None, str]:
    """Resolve the headline allocation CSV. Returns (path, reason).

    Order (Phase 14 β; Phase 21 γ adds run root/data; Phase 21 precision
    pass adds scenario handling):
      1. sanity_schema `allocation.canonical_csv`
      2. models/allocation_result.csv (legacy convention)
      3. *allocation*.csv under models/, run root, data/, excluding
         comparators (status quo / uniform / baseline). If file names
         carry budget tags, keep those matching the question's budget.
         Prefer "optim" names, then the finest granularity (most rows).
    When two or more candidates remain equally preferred and they are
    budget- or method-tagged scenario variants, there is no way to know
    which one the prose describes: return (None, "ambiguous") so checks
    abstain instead of comparing a sentence to the wrong scenario
    (2026-04-28_103105 checked $320M prose against the $200M CSV).
    """
    canonical = _load_canonical_alloc_path_from_schema(run_dir)
    if canonical and os.path.exists(canonical):
        return canonical, "canonical"
    legacy = os.path.join(run_dir, "models", "allocation_result.csv")
    if os.path.exists(legacy):
        return legacy, "legacy"
    cands: list[str] = []
    for d in (os.path.join(run_dir, "models"), run_dir,
              os.path.join(run_dir, "data")):
        if os.path.isdir(d):
            cands += [os.path.join(d, e) for e in sorted(os.listdir(d))
                      if "allocation" in e and e.endswith(".csv")]
    if not cands:
        return None, "none"
    cands = [c for c in cands
             if not _COMPARATOR_CSV_RE.search(os.path.basename(c))] or cands
    budget = _question_budget(run_dir)
    tagged = {c: _CSV_BUDGET_TAG_RE.search(os.path.basename(c)) for c in cands}
    if budget and any(tagged.values()):
        def _val(m):
            return float(m.group(1)) * _MAGNITUDE.get(m.group(2).lower(), 1.0)
        match = [c for c in cands if tagged[c] and abs(_val(tagged[c]) - budget)
                 <= 0.02 * budget]
        cands = match or [c for c in cands if not tagged[c]] or cands
    optim = [c for c in cands if "optim" in os.path.basename(c)]
    cands = optim or cands
    sized = sorted(((len(_read_csv_rows(c)), c) for c in cands), reverse=True)
    top = [c for n, c in sized if n == sized[0][0]]
    if len(top) > 1 and any(tagged.get(c) for c in top):
        return None, "ambiguous"
    # Equal size, untagged: keep the deterministic directory order
    # (models/ before run root before data/).
    return (top[0] if len(top) == 1 else next(c for c in cands if c in top)), "inferred"


def _resolve_alloc_path(run_dir: str) -> str | None:
    """Back-compat wrapper: the headline allocation CSV path or None."""
    return _resolve_alloc_path_ex(run_dir)[0]


def _load_authoritative(run_dir: str) -> dict:
    """Load the authoritative source values used for cross-checking.

    Missing sources produce a None value; the cross-check then skips that
    claim silently rather than guessing.
    """
    auth: dict = {
        # {outcome_noun_lower: value} from optimization_summary.json
        "headline_averted": {},
        "uniform_averted": {},
        # [(entity_key frozenset, price_unit_lower, value)] from
        # package_evaluation.csv cost_per_<unit>_averted columns
        "unit_prices": [],
        "final_round": None,
        "n_allocated_units": None,       # rows with cost > 0
        "package_counts": {},            # {package_id: n}
        "zone_budget_shares": {},        # {zone: share}
        "alloc_source": None,
        "vocab": RunVocabulary(),
    }

    summary_path = os.path.join(run_dir, "models", "optimization_summary.json")
    if os.path.exists(summary_path):
        try:
            with open(summary_path) as f:
                s = json.load(f)
            if isinstance(s, dict):
                for k, v in s.items():
                    m = re.fullmatch(r"(total|uniform)_(\w+?)_averted", str(k))
                    if not m:
                        continue
                    try:
                        fv = float(v)
                    except (TypeError, ValueError):
                        continue
                    if fv != fv or fv == 0:
                        continue
                    bucket = ("headline_averted" if m.group(1) == "total"
                              else "uniform_averted")
                    auth[bucket][m.group(2).lower()] = fv
        except (json.JSONDecodeError, OSError):
            pass

    alloc_path = _resolve_alloc_path(run_dir)
    rows = _read_csv_rows(alloc_path)
    eval_rows = _read_csv_rows(os.path.join(run_dir, "models",
                                            "package_evaluation.csv"))
    vocab = build_vocabulary(run_dir, alloc_rows=rows, extra_rows=eval_rows)
    vocab.budget = _question_budget(run_dir)
    auth["vocab"] = vocab
    if rows:
        cost_col = _pick_col(rows, ("total_cost_usd", "cost_3yr", "cost_usd",
                                    "cost", "total_cost", "annual_cost"))
        pkg_col = _pick_col(rows, ("package", "assigned_package", "package_name"))
        zone_col = _pick_col(rows, ("zone", "zone_name", "zone_abbr", "region"))
        pkg_counts: dict = {}
        zone_costs: dict = {}
        n_allocated = 0
        total_cost = 0.0
        for r in rows:
            if cost_col is None:
                break
            try:
                c = float(r.get(cost_col) or "")
            except ValueError:
                continue
            if not c > 0:
                continue
            n_allocated += 1
            total_cost += c
            if pkg_col:
                pkg = (r.get(pkg_col) or "").strip().lower()
                if pkg in vocab.packages:
                    pkg_counts[pkg] = pkg_counts.get(pkg, 0) + 1
            if zone_col:
                z = (r.get(zone_col) or "").strip()
                if z:
                    zone_costs[z] = zone_costs.get(z, 0.0) + c
        if n_allocated > 0:
            auth["n_allocated_units"] = n_allocated
            auth["alloc_source"] = os.path.relpath(alloc_path, start=run_dir)
        if pkg_counts:
            auth["package_counts"] = pkg_counts
        if zone_costs and total_cost > 0:
            auth["zone_budget_shares"] = {
                z: c / total_cost for z, c in zone_costs.items()}

    # Unit prices per (package, zone) from package_evaluation.csv. All
    # cost_per_<unit>_averted columns are read; the first row per
    # (entity key, unit) wins (file order), matching the Phase 12 α rule.
    if eval_rows:
        epkg = _pick_col(eval_rows, ("package", "assigned_package",
                                     "package_name"))
        ezone = _pick_col(eval_rows, ("zone", "zone_name", "zone_abbr",
                                      "region"))
        price_cols = {}
        for col in eval_rows[0].keys():
            m = re.fullmatch(r"cost_per_(\w+?)_averted", col)
            if m:
                price_cols[col] = m.group(1).lower()
                if m.group(1).lower() not in [u.lower() for u in vocab.price_units]:
                    vocab.price_units.append(m.group(1))
        seen: set = set()
        for r in eval_rows:
            pkg = (r.get(epkg) or "").strip().lower() if epkg else ""
            z = (r.get(ezone) or "").strip() if ezone else ""
            key = frozenset(x for x in (f"pkg:{pkg}" if pkg else "",
                                        f"zone:{z}" if z else "") if x)
            for col, unit in price_cols.items():
                if (key, unit) in seen:
                    continue
                try:
                    val = float(r.get(col) or "")
                except ValueError:
                    continue
                seen.add((key, unit))
                auth["unit_prices"].append((key, unit, val))

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


# --------------------------------------------------------------------------- #
# Prose scanners
# --------------------------------------------------------------------------- #


def _entity_key(window: str, vocab: RunVocabulary) -> frozenset:
    """Entities named in a label window: package ids, zones, and any
    remaining ALL-CAPS acronyms (so labels like "HPV in R2" still group
    when the run vocabulary is empty)."""
    keys: set[str] = set()
    consumed: list[tuple[int, int]] = []
    for s, e, pid in vocab.find_packages(window):
        keys.add(f"pkg:{pid}")
        consumed.append((s, e))
    for s, e, z in vocab.find_zones(window):
        keys.add(f"zone:{z}")
        consumed.append((s, e))
    generic = {u.lower().rstrip("s") for u in
               list(vocab.price_units) + list(vocab.outcome_nouns)}
    for m in re.finditer(r"(?<![A-Za-z0-9])([A-Z][A-Z0-9]{1,9})(?![A-Za-z0-9])",
                         window):
        if any(s <= m.start() < e for s, e in consumed):
            continue
        tok = m.group(1).lower()
        # Units ("DALY" from a preceding "$X/DALY") and statistical /
        # currency acronyms name no entity.
        if tok.rstrip("s") in generic or tok in _GENERIC_ACRONYMS:
            continue
        keys.add(f"tok:{tok}")
    return frozenset(keys)


def _scan_unit_prices(doc_text: str, vocab: RunVocabulary
                      ) -> list[tuple[float, str, frozenset, str, str]]:
    """Find '$X/<unit>' and '$X per <unit>' occurrences. Returns
    (value, unit_lower, entity_key, right_window, context)."""
    units = "|".join(_flex(u) for u in
                     sorted(set(vocab.price_units), key=len, reverse=True))
    rx = re.compile(rf"\$\s*{_NUM}\s*(?:/|per\s+)\s*({units})(?![A-Za-z])",
                    re.IGNORECASE)
    out = []
    for m in rx.finditer(doc_text):
        try:
            val = _parse_num(m.group(1))
        except ValueError:
            continue
        unit = m.group(2).lower().replace(" ", "").replace("-", "")
        left = doc_text[max(0, m.start() - 25):m.start()]
        # Phase 21 precision pass: in "$5.05/case in NW for PBO vs
        # $8.41/case for standard nets" the second price's label is on
        # its right; never let it inherit the left-hand comparison's.
        cut = list(re.finditer(r"\bvs\.?|\bversus\b|\bcompared\s+(?:to|with)\b|"
                               r"\bwhile\b|\bwhereas\b|\bthan\b", left, re.IGNORECASE))
        if cut:
            left = left[cut[-1].end():]
        right = doc_text[m.end():min(len(doc_text), m.end() + 30)]
        ctx = doc_text[max(0, m.start() - 60):min(len(doc_text), m.end() + 60)]
        out.append((val, unit, _entity_key(left, vocab), right, ctx))
    return out


def _scan_doc_averted(doc_text: str, vocab: RunVocabulary
                      ) -> list[tuple[float, str, str, str]]:
    """Find 'averts X[M] <outcome>' claims. Returns (value, noun_lower,
    classification, context) with classification ∈ {optimized, uniform},
    decided from the ~40 chars to the LEFT of the number (the label that
    precedes it, never a trailing comparison clause)."""
    nouns = "|".join(_flex(n) for n in
                     sorted(set(vocab.outcome_nouns), key=len, reverse=True))
    pat = re.compile(
        r"(?:averts?\s+|averted\s*[:\-]?\s*|allocation\s+averts?\s+)"
        rf"{_NUM}\s*(?:{_MAG_ALT}(?![A-Za-z]))?\s*({nouns})(?![A-Za-z])",
        re.IGNORECASE)
    out = []
    for m in pat.finditer(doc_text):
        val = _parse_num(m.group(1))
        mag = (m.group(2) or "").lower()
        val *= _MAGNITUDE.get(mag, 1.0)
        noun = m.group(3).lower()
        left = doc_text[max(0, m.start() - 40):m.start()].lower()
        classification = ("uniform"
                          if any(tok in left
                                 for tok in ("uniform allocation averts",
                                             "uniform allocation:",
                                             "uniform_baseline",
                                             "uniform baseline",
                                             "baseline averts"))
                          else "optimized")
        ctx = doc_text[max(0, m.start() - 60):min(len(doc_text), m.end() + 60)]
        out.append((val, noun, classification, ctx))
    return out


def _normalize_package_token(tok: str) -> str:
    """Lower-case snake_case form of a prose package token. Phase 21 γ:
    the intervention-specific alias table is gone; prose-to-package
    mapping goes through `RunVocabulary.find_packages`."""
    return re.sub(r"[\s\-+]+", "_", tok.strip().lower())


def _off_headline(doc_text: str, start: int, end: int,
                  vocab: RunVocabulary) -> bool:
    """Phase 21 precision pass: True when the number at [start, end) is
    an identifier / bound, or its clause is about another scenario, an
    uncertainty summary, a hypothesis, or a different budget."""
    if _id_or_bound_adjacent(doc_text, start):
        return True
    clause = _clause(doc_text, start, end)
    return bool(_OFF_HEADLINE_RE.search(clause)
                or _mentions_other_budget(clause, vocab.budget))


def _scan_unit_counts(doc_text: str, vocab: RunVocabulary | None = None
                      ) -> list[tuple[int, str, str, str]]:
    """Find labeled allocation-unit counts that refer to the TOTAL
    allocated set (not per-subset counts). Requires either:
      - "X of N <units>" form (unambiguous total, N ≠ universe), OR
      - left-window anchor (all / total of / across), OR
      - right-window anchor (allocated / received / receive / in total)
    Returns (count, noun_as_written, context, confidence)."""
    vocab = vocab or RunVocabulary()
    unit_rx = vocab.unit_re().pattern
    count_re = re.compile(rf"{_NUM}\s+({unit_rx})", re.IGNORECASE)
    of_n_re = re.compile(rf"{_NUM}\s+of\s+{_NUM}\s+({unit_rx})", re.IGNORECASE)
    out = []
    seen_positions: set[int] = set()
    universe = vocab.universe
    for m in of_n_re.finditer(doc_text):
        raw = m.group(1)
        if "." in raw:
            continue
        n = int(_parse_num(raw))
        if n < 10 or n > 100_000:
            continue
        total = int(_parse_num(m.group(2)))
        seen_positions.add(m.start())
        if universe is not None and total == universe:
            continue  # "X of <universe>" is coverage/accuracy, not allocation
        if _off_headline(doc_text, m.start(), m.end(), vocab) or \
                _SUBSET_RE.search(_clause(doc_text, m.start(), m.end())):
            continue
        left = doc_text[max(0, m.start() - 30):m.start()]
        right = doc_text[m.end():min(len(doc_text), m.end() + 30)]
        if _has_zone_qualifier(left + " " + right, vocab):
            continue
        ctx = doc_text[max(0, m.start() - 60):min(len(doc_text), m.end() + 60)]
        out.append((n, m.group(3), ctx, "aggregate"))
    for m in count_re.finditer(doc_text):
        if m.start() in seen_positions:
            continue
        raw = m.group(1)
        if "." in raw:
            continue
        n = int(_parse_num(raw))
        if n < 10 or n > 100_000:
            continue
        if universe is not None and n == universe:
            continue  # the universe, not the allocated subset
        if _is_table_row(doc_text, m.start()):
            continue
        left = doc_text[max(0, m.start() - 30):m.start()]
        right = doc_text[m.end():min(len(doc_text), m.end() + 30)]
        if _has_zone_qualifier(left + " " + right, vocab):
            continue
        # Phase 21 precision pass: a count is the allocated TOTAL only
        # when its clause names no package, zone, subset word, other
        # scenario, or possessive universe ("the country's 47 counties").
        clause = _clause(doc_text, m.start(), m.end())
        if (_off_headline(doc_text, m.start(), m.end(), vocab)
                or _SUBSET_RE.search(clause)
                or vocab.find_packages(clause) or vocab.has_zone(clause)
                or re.search(r"['’]s\s*\Z", left)):
            continue
        lw, rw = left.lower(), right.lower()
        if universe is not None and (f"of {universe}" in rw[:12]):
            continue
        has_left = any(tok in lw for tok in _TOTAL_LEFT_TOKENS)
        has_right = any(rw.startswith(tok) for tok in _TOTAL_RIGHT_TOKENS)
        wider = doc_text[max(0, m.start() - 80):
                         min(len(doc_text), m.end() + 80)].lower()
        is_headline_candidate = any(tok in wider
                                    for tok in ("100% of budget",
                                                "100% of the budget",
                                                "% of the budget",
                                                "concentrating "))
        if not (has_left or has_right or is_headline_candidate):
            continue
        ctx = doc_text[max(0, m.start() - 60):min(len(doc_text), m.end() + 60)]
        out.append((n, m.group(2),
                    ctx, "aggregate" if (has_left or has_right)
                    else "headline_candidate"))
    return out


# Back-compat name (Phase 13 β callers).
def _scan_lga_counts(doc_text: str, vocab: RunVocabulary | None = None
                     ) -> list[tuple[int, str, str]]:
    return [(n, ctx, conf) for n, _noun, ctx, conf
            in _scan_unit_counts(doc_text, vocab)]


def _scan_package_counts(doc_text: str, vocab: RunVocabulary | None = None
                         ) -> list[tuple[int, str, str]]:
    """Find "<N> <package alias>" counts ("104 dual-AI", "12 HPV
    sites"). Skips per-zone subsets and table rows. A year-like number
    (1900–2100, no comma) only counts when the package alias is followed
    by an allocation-unit noun ("2026 SMC campaigns" is a year).
    Returns (count, package_id, context)."""
    vocab = vocab or RunVocabulary()
    unit_re = vocab.unit_re()
    out = []
    num_re = re.compile(_NUM + r"\s+")
    for m in num_re.finditer(doc_text):
        raw = m.group(1)
        if "." in raw:
            continue
        hits = vocab.find_packages(doc_text[m.end():m.end() + 40])
        if not hits or hits[0][0] != 0:
            continue
        _, pe, pid = hits[0]
        if _joined_combination(doc_text, m.end() + pe, vocab):
            continue
        n = int(_parse_num(raw))
        if n < 1 or n > 100_000:
            continue
        after = doc_text[m.end() + pe:m.end() + pe + 30]
        if _is_year_like(raw) and not unit_re.match(after.lstrip()):
            continue
        if _is_table_row(doc_text, m.start()):
            continue
        left = doc_text[max(0, m.start() - 30):m.start()]
        right = doc_text[m.end() + pe:min(len(doc_text), m.end() + pe + 30)]
        if _has_zone_qualifier(left + " " + right, vocab):
            continue
        # Phase 21 precision pass: identifiers ("SA-001 PBO"), fractions,
        # class words ("SMC-eligible", "SMC-containing"), per-zone or
        # off-headline clauses are not package-assignment counts.
        clause = _clause(doc_text, m.start(), m.end() + pe)
        if (_off_headline(doc_text, m.start(), m.end() + pe, vocab)
                or _FRACTION_SUFFIX_RE.match(doc_text, m.end(1))
                or re.match(r"[-\s]?(?:eligible|containing|based|only)\b",
                            doc_text[m.end() + pe:], re.IGNORECASE)
                # "1 dual-AI archetype" counts analysis groups, not units.
                or re.match(r"\s*(?:[\w-]+\s+)?(?:archetypes?|clusters?|strat(?:a|um)|"
                            r"groups?|categor\w+|types?|scenarios?|options?|draws?|"
                            r"perturbations?|runs?|levels?|tiers?|arms?)\b",
                            doc_text[m.end() + pe:], re.IGNORECASE)
                or _PACKAGE_SUBSET_RE.search(clause)
                or vocab.has_zone(clause)):
            continue
        ctx = doc_text[max(0, m.start() - 60):min(len(doc_text), m.end() + 60)]
        out.append((n, pid, ctx))
    return out


def _scan_budget_shares(doc_text: str) -> list[tuple[float, int, str, str, str]]:
    """Find labeled budget shares ("78.1% of budget"). Returns
    (fraction, pos, left_window, right_window, context) tuples.
    Fraction in [0, 1]."""
    out = []
    for m in _BUDGET_SHARE_RE.finditer(doc_text):
        try:
            pct = float(m.group(1))
        except ValueError:
            continue
        if pct < 0 or pct > 100:
            continue
        left_window = doc_text[max(0, m.start() - 40):m.start()]
        right_window = doc_text[m.end():min(len(doc_text), m.end() + 40)]
        ctx = doc_text[max(0, m.start() - 60):min(len(doc_text), m.end() + 60)]
        out.append((pct / 100.0, m.start(), left_window, right_window, ctx))
    return out


def _closest_zone(window: str, vocab: RunVocabulary, from_right: bool,
                  require_no_sentence_break: bool = False) -> str | None:
    best, best_d = None, 10**9
    for s, e, z in vocab.find_zones(window):
        if require_no_sentence_break and any(
                ch in window[:s] for ch in (".", ";", "\n")):
            continue
        d = s if from_right else len(window) - e
        if d < best_d:
            best, best_d = z, d
    return best


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #


def _classify_drift(drift: float, exact_class: str | None,
                     exact_counts: set[str] | None
                     ) -> tuple[str | None, str | None]:
    """Phase 14 β: severity-and-kind dispatcher honoring exact-match
    opt-in. When `exact_class` is in `exact_counts` AND drift is
    sub-5%, return (MEDIUM, exact_count_drift) — catches integer
    staleness (313 vs 312) the 5% classifier tolerates. Above 5%, the
    regular classifier applies so alternative-scenario counts keep the
    Phase 13 β treatment."""
    if (exact_counts and exact_class in exact_counts
            and 0 < drift < MEDIUM_DRIFT_THRESHOLD):
        return "MEDIUM", "exact_count_drift"
    sev = _severity_for_drift(drift)
    if sev is None:
        return None, None
    kind = ("numeric_drift_extreme" if sev == "HIGH"
            else "numeric_drift_detected")
    return sev, kind


# Phase 21 γ: `unit_count` is the generic name; `lga_count` stays as a
# schema alias for runs written before the rename.
_UNIT_COUNT_CLASSES = frozenset({"unit_count", "lga_count"})


def _check_count_drift(run_dir: str, auth: dict,
                        report_files: list[str],
                        exact_counts: set[str] | None = None
                        ) -> list[dict]:
    """Phase 13 β: cross-doc drift on allocation-unit counts, package
    counts, and budget shares against the allocation CSV."""
    out: list[dict] = []
    vocab: RunVocabulary = auth.get("vocab") or RunVocabulary()
    auth_n = auth.get("n_allocated_units")
    auth_pkgs = auth.get("package_counts") or {}
    auth_zones = auth.get("zone_budget_shares") or {}
    if auth_n is None and not auth_pkgs and not auth_zones:
        return []
    src = auth.get("alloc_source") or "the allocation CSV"
    unit_exact = bool(exact_counts and (exact_counts & _UNIT_COUNT_CLASSES))
    unit_exact_set = ({"unit_count"} | (exact_counts or set())) if unit_exact \
        else exact_counts

    for fname in report_files:
        fpath = os.path.join(run_dir, fname)
        if not os.path.exists(fpath):
            continue
        try:
            with open(fpath, encoding="utf-8") as f:
                text = f.read()
        except (UnicodeDecodeError, OSError):
            continue

        if auth_n is not None:
            for n, noun, ctx, confidence in _scan_unit_counts(text, vocab):
                drift = _drift_pct(n, auth_n)
                if confidence == "headline_candidate":
                    if not (unit_exact and 0 < drift < MEDIUM_DRIFT_THRESHOLD):
                        continue
                sev, kind = _classify_drift(drift, "unit_count", unit_exact_set)
                if sev is not None:
                    out.append(_build_violation(
                        kind, sev,
                        f"{fname} reports {n} {noun} but {src} shows "
                        f"{auth_n} allocated ({drift*100:.1f}% drift). "
                        f"Context: \"...{ctx.strip()[:120]}...\"."
                    ))

        for n, pkg_id, ctx in _scan_package_counts(text, vocab):
            auth_count = auth_pkgs.get(pkg_id)
            if not auth_count:
                continue
            drift = _drift_pct(n, auth_count)
            sev, kind = _classify_drift(drift, "package_count", exact_counts)
            if sev is not None:
                out.append(_build_violation(
                    kind, sev,
                    f"{fname} reports {n} units on package {pkg_id} but "
                    f"{src} shows {auth_count} ({drift*100:.1f}% drift). "
                    f"Context: \"...{ctx.strip()[:120]}...\"."
                ))

        if auth_zones:
            non_headline = ("prediction", "predicted", "expected",
                            "should receive", "may receive",
                            "could receive", "<", "approximately ",
                            "hypothesis", "h1 ", "h2 ", "h3 ", "h4 ",
                            "h5 ", "h6 ", "h7 ", "(h1)", "(h2)", "(h3)",
                            "(h4)", "(h5)", "(h6)", "(h7)",
                            "(25-", "(20-", "(30-")
            for (share, pos, left_window, right_window,
                 ctx) in _scan_budget_shares(text):
                if _is_table_row(text, pos):
                    continue
                if any(tok in left_window.lower() for tok in non_headline):
                    continue
                # Phase 21 precision pass: the zone must come from the
                # share's own clause, and a clause naming several zones
                # ("NW + NE + NC receive 61.3%") is an aggregate, not a
                # per-zone share.
                clause = _clause(text, pos)
                if _off_headline(text, pos, pos + 1, vocab):
                    continue
                if len({z for _s, _e, z in vocab.find_zones(clause)}) > 1:
                    continue
                cs = text.rfind(clause, max(0, pos - 400), pos + 1) \
                    if clause else pos
                left_clause = text[max(cs, pos - 40):pos] if cs >= 0 else left_window
                zone = _closest_zone(left_clause, vocab, from_right=False)
                if zone is None:
                    zone = _closest_zone(right_window, vocab, from_right=True,
                                         require_no_sentence_break=True)
                if zone is None or zone not in auth_zones:
                    continue
                auth_share = auth_zones[zone]
                drift = _drift_pct(share, auth_share)
                sev, kind = _classify_drift(drift, None, exact_counts)
                if sev is not None:
                    out.append(_build_violation(
                        kind, sev,
                        f"{fname} reports {share*100:.1f}% of budget "
                        f"for {zone} but {src} shows "
                        f"{auth_share*100:.1f}% ({drift*100:.1f}% drift). "
                        f"Context: \"...{ctx.strip()[:120]}...\"."
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


_SCHEMA_LOADER_EXC = (OSError,) + (
    (yaml.YAMLError,) if yaml is not None else ())


def _load_sanity_schema_dict(run_dir: str) -> dict | None:
    """Phase 14 β: load and parse models/sanity_schema.yaml. Returns the
    parsed dict, or None if absent, unreadable, malformed, or not a
    mapping."""
    if yaml is None:
        return None
    path = os.path.join(run_dir, "models", "sanity_schema.yaml")
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            doc = yaml.safe_load(f) or {}
    except _SCHEMA_LOADER_EXC:
        return None
    return doc if isinstance(doc, dict) else None


def _load_canonical_alloc_path_from_schema(run_dir: str) -> str | None:
    """Phase 14 β: `allocation.canonical_csv` from the sanity schema,
    as an absolute path under run_dir; None when unset."""
    doc = _load_sanity_schema_dict(run_dir)
    if doc is None:
        return None
    sec = doc.get("allocation")
    if not isinstance(sec, dict):
        return None
    rel = sec.get("canonical_csv")
    if not isinstance(rel, str):
        return None
    return os.path.join(run_dir, rel)


def _load_exact_counts_from_schema(run_dir: str) -> set[str]:
    """Phase 14 β: `exact_counts: [...]` from the sanity schema."""
    doc = _load_sanity_schema_dict(run_dir)
    if doc is None:
        return set()
    val = doc.get("exact_counts")
    if isinstance(val, list):
        return {str(x) for x in val}
    return set()


def check_numeric_consistency(run_dir: str,
                                exact_counts: set[str] | None = None
                                ) -> list[dict]:
    """Cross-check report-level numeric claims against authoritative
    sources. Returns a list of violations.

    Four classes of violation:
      numeric_drift_detected    MEDIUM — cross-doc drift >5% from authority
      numeric_drift_extreme     HIGH   — cross-doc drift >25% from authority
      same_doc_inconsistency    MEDIUM — same labeled metric, different
                                          numbers in same file
      exact_count_drift         MEDIUM — Phase 14 β: any non-zero drift on
                                          a count class declared
                                          integer-exact in the sanity
                                          schema's `exact_counts`

    If `exact_counts` is None, the schema is auto-loaded from
    `<run_dir>/models/sanity_schema.yaml`.
    """
    violations: list[dict] = []
    auth = _load_authoritative(run_dir)
    vocab: RunVocabulary = auth["vocab"]
    if exact_counts is None:
        exact_counts = _load_exact_counts_from_schema(run_dir)

    report_files = [
        "results.md",
        "figure_rationale.md",
        "decision_rule.md",
        "report.md",
    ]

    # --- Same-doc inconsistency + authority check: unit prices ---
    # Phase 21 γ: generalizes the Phase 12 α single-metric check. Every
    # "$X/<unit>" is keyed by the entities named just before it
    # (package, zone, acronyms). One key with >1 distinct values in one
    # document is a same-doc inconsistency; a key matching a
    # package_evaluation.csv row is also checked against that row.
    for fname in ("results.md", "report.md"):
        fpath = os.path.join(run_dir, fname)
        if not os.path.exists(fpath):
            continue
        try:
            with open(fpath, encoding="utf-8") as f:
                text = f.read()
        except (UnicodeDecodeError, OSError):
            continue
        groups: dict[tuple[frozenset, str], list[tuple[float, str]]] = {}
        for val, unit, key, right, ctx in _scan_unit_prices(text, vocab):
            if not key:
                continue
            # "$X for <other package>" attributes the price to a
            # different entity than the label on the left.
            if re.match(r"\s*(?:\)|,)?\s*for\s+(?:the\s+)?\w", right, re.I):
                continue
            groups.setdefault((key, unit), []).append((val, ctx))
        for (key, unit), vals in groups.items():
            label = ", ".join(sorted(k.split(":", 1)[1] for k in key))
            distinct = sorted({round(v, 2) for v, _ in vals})
            auth_val = next((v for k, u, v in auth["unit_prices"]
                             if k == key and u == unit), None)
            if len(distinct) > 1:
                tail = (f"The package_evaluation.csv value is "
                        f"${auth_val:.2f}/{unit}. All in-text mentions "
                        f"must agree to within "
                        f"{MEDIUM_DRIFT_THRESHOLD*100:.0f}%."
                        if auth_val is not None else
                        "Authoritative source unavailable; cannot determine "
                        "which is correct, but the in-text spread is itself "
                        "a problem.")
                violations.append(_build_violation(
                    "same_doc_inconsistency", "MEDIUM",
                    f"{fname} contains {len(distinct)} distinct "
                    f"cost-per-{unit} values for [{label}]: {distinct}. "
                    + tail))
            if auth_val is not None:
                for val, ctx in vals:
                    drift = _drift_pct(val, auth_val)
                    sev = _severity_for_drift(drift)
                    if sev is not None:
                        violations.append(_build_violation(
                            "numeric_drift_extreme" if sev == "HIGH"
                            else "numeric_drift_detected", sev,
                            f"{fname} claims ${val:.2f}/{unit} for "
                            f"[{label}] but models/package_evaluation.csv "
                            f"is ${auth_val:.2f}/{unit} "
                            f"({drift*100:.1f}% drift). Context: "
                            f"\"...{ctx.strip()[:120]}...\"."))

    # --- Cross-doc drift: outcomes averted ---
    if auth["headline_averted"]:
        for fname in report_files:
            fpath = os.path.join(run_dir, fname)
            if not os.path.exists(fpath):
                continue
            try:
                with open(fpath, encoding="utf-8") as f:
                    text = f.read()
            except (UnicodeDecodeError, OSError):
                continue
            for val, noun, classification, ctx in _scan_doc_averted(text, vocab):
                bucket = ("uniform_averted" if classification == "uniform"
                          else "headline_averted")
                auth_val = auth[bucket].get(noun)
                if not auth_val:
                    continue
                drift = _drift_pct(val, auth_val)
                sev = _severity_for_drift(drift)
                if sev is not None:
                    violations.append(_build_violation(
                        "numeric_drift_extreme" if sev == "HIGH"
                        else "numeric_drift_detected", sev,
                        f"{fname} reports {val/1e6:.1f}M {noun} averted "
                        f"but optimization_summary.json says "
                        f"{auth_val/1e6:.1f}M ({drift*100:.0f}% drift). "
                        f"Context: \"...{ctx.strip()[:120]}...\". The "
                        f"report's numbers must match the authoritative "
                        f"optimization output."
                    ))

    violations.extend(_check_count_drift(run_dir, auth, report_files,
                                            exact_counts=exact_counts))

    # --- Round-number drift: results.md title vs progress.md ---
    results_path = os.path.join(run_dir, "results.md")
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

    # Phase 14 β: T13-T15 cover exact-match opt-in.

    # T13: 313 vs 312 LGAs (0.32% drift, sub-5% by design) fires
    # MEDIUM exact_count_drift when exact_counts={'lga_count'}.
    # The 103105 retro pattern.
    with tempfile.TemporaryDirectory() as d:
        rows = []
        for i in range(312):
            rows.append({"lga_name": f"lga_{i}", "zone": "NW",
                         "package": "llin_pbo",
                         "total_cost_usd": 1_000_000})
        make_run_dir(d,
            allocation_rows=rows,
            progress_text="STAGE 7 decision (round 6/8)\n",
            decision_rule_text=(
                "# Decision Rule\n"
                "## Allocation Summary\n"
                "Total of 313 LGAs allocated.\n"
            ))
        v = check_numeric_consistency(d, exact_counts={"lga_count"})
        ok(any(x["kind"] == "exact_count_drift"
               and "313" in x["claim"] and "312" in x["claim"]
               for x in v),
           f"T13: 313 vs 312 with exact_counts opt-in should fire "
           f"MEDIUM exact_count_drift, got {v}")

    # T14: same 0.32% drift with exact_counts UNSET → silent
    # (backward compat: Phase 13 β's 5% threshold tolerates it).
    with tempfile.TemporaryDirectory() as d:
        rows = []
        for i in range(312):
            rows.append({"lga_name": f"lga_{i}", "zone": "NW",
                         "package": "llin_pbo",
                         "total_cost_usd": 1_000_000})
        make_run_dir(d,
            allocation_rows=rows,
            progress_text="STAGE 7 decision (round 6/8)\n",
            decision_rule_text=(
                "# Decision Rule\n"
                "## Allocation Summary\n"
                "Total of 313 LGAs allocated.\n"
            ))
        v = check_numeric_consistency(d)  # no exact_counts
        ok(not any(x["kind"] == "exact_count_drift" for x in v)
           and not any("313" in x["claim"] and "312" in x["claim"]
                       and x["kind"].startswith("numeric_drift")
                       for x in v),
           f"T14: 0.32% drift without exact_counts should be silent, "
           f"got {v}")

    # T15: exact_counts opt-in but counts MATCH exactly → silent.
    with tempfile.TemporaryDirectory() as d:
        rows = []
        for i in range(312):
            rows.append({"lga_name": f"lga_{i}", "zone": "NW",
                         "package": "llin_pbo",
                         "total_cost_usd": 1_000_000})
        make_run_dir(d,
            allocation_rows=rows,
            progress_text="STAGE 7 decision (round 6/8)\n",
            decision_rule_text=(
                "# Decision Rule\n"
                "## Allocation Summary\n"
                "Total of 312 LGAs allocated.\n"
            ))
        v = check_numeric_consistency(d, exact_counts={"lga_count"})
        ok(not any(x["kind"] == "exact_count_drift" for x in v),
           f"T15: 312 == 312 should be silent under exact_counts, "
           f"got {v}")

    # ---- Phase 21 γ: disease/country-agnostic vocabulary ------------- #

    def write_alloc(d: str, rows: list[dict], name: str = "allocation_result.csv",
                    cols: list[str] | None = None) -> None:
        cols = cols or list(rows[0].keys())
        os.makedirs(os.path.join(d, "models"), exist_ok=True)
        with open(os.path.join(d, "models", name), "w") as f:
            f.write(",".join(cols) + "\n")
            for r in rows:
                f.write(",".join(str(r.get(c, "")) for c in cols) + "\n")

    # T16: measles SIA allocation across districts — vocabulary comes
    # from the CSV (packages, zones, unit noun "district").
    with tempfile.TemporaryDirectory() as d:
        rows = ([{"district": f"d{i}", "zone": "Rift Valley",
                  "package": "sia_mr", "cost_usd": 50_000} for i in range(40)]
                + [{"district": f"e{i}", "zone": "Coast",
                    "package": "routine_mcv2", "cost_usd": 50_000}
                   for i in range(20)])
        write_alloc(d, rows)
        with open(os.path.join(d, "decision_rule.md"), "w") as f:
            f.write("# Rule\nTotal of 45 districts allocated. "
                    "Mix: 30 SIA, 20 MCV2.\n")
        v = check_numeric_consistency(d)
        ok(any("45 districts" in x["claim"] and "60" in x["claim"] for x in v),
           f"T16: 45 vs 60 districts must fire, got {v}")
        ok(any("sia_mr" in x["claim"] and "30" in x["claim"] for x in v),
           f"T16: 30 vs 40 SIA package count must fire, got {v}")
        ok(not any("routine_mcv2" in x["claim"] for x in v),
           f"T16: 20 MCV2 matches CSV, must be silent, got {v}")

    # T17: "1,234 districts" is 1234, not 234 (comma lookbehind).
    with tempfile.TemporaryDirectory() as d:
        rows = [{"district": f"d{i}", "package": "hpv_school",
                 "cost_usd": 10} for i in range(1234)]
        write_alloc(d, rows)
        with open(os.path.join(d, "report.md"), "w") as f:
            f.write("Funding reaches all 1,234 districts allocated.\n")
        auth = _load_authoritative(d)
        hits = _scan_unit_counts("Funding reaches across 1,234 districts.",
                                 RunVocabulary(universe=5000))
        ok(hits and hits[0][0] == 1234, f"T17: expected 1234, got {hits}")
        ok(auth["n_allocated_units"] == 1234, f"T17 auth: {auth}")

    # T18: a year followed by a package alias is not a package count.
    vocab = RunVocabulary(packages={"smc": ["smc"]}, universe=774)
    hits = _scan_package_counts("The 2026 SMC campaigns began in June.", vocab)
    ok(not hits, f"T18: '2026 SMC campaigns' must not be a count, got {hits}")
    hits = _scan_package_counts("In total 2,026 SMC LGAs were covered.", vocab)
    ok(hits and hits[0][0] == 2026, f"T18b: comma count kept, got {hits}")

    # T19: comma-millions, "billion", and per-unit prices parse.
    vocab = RunVocabulary()
    got = [(v, n) for v, n, _c, _x in _scan_doc_averted(
        "The plan averts 1,234,567 infections; the alternative averts "
        "1.2 billion cases.", vocab)]
    ok((1_234_567.0, "infections") in got and (1.2e9, "cases") in got,
       f"T19: averted parse, got {got}")
    prices = [(v, u) for v, u, _k, _r, _c in _scan_unit_prices(
        "TB in R1: $1,234/case and TB in R1: $2.50 per DALY", vocab)]
    ok((1234.0, "case") in prices and (2.5, "daly") in prices,
       f"T19: per-unit price parse, got {prices}")

    # T20: outcome nouns other than cases are cross-checked against
    # total_<noun>_averted.
    with tempfile.TemporaryDirectory() as d:
        make_run_dir(d, opt_summary={"total_deaths_averted": 12_000},
                     figure_rationale_text="The allocation averts 30,000 deaths.\n")
        v = check_numeric_consistency(d)
        ok(any(x["kind"] == "numeric_drift_extreme" and "deaths" in x["claim"]
               for x in v), f"T20: deaths drift must fire, got {v}")

    # T21: the universe comes from the schema/CSV, not a hardcoded 774.
    with tempfile.TemporaryDirectory() as d:
        rows = ([{"county": f"c{i}", "package": "art_scaleup",
                  "cost_usd": 1} for i in range(30)]
                + [{"county": f"z{i}", "package": "none", "cost_usd": 0}
                   for i in range(17)])
        write_alloc(d, rows)
        with open(os.path.join(d, "report.md"), "w") as f:
            f.write("We cover all 47 counties allocated in the envelope; "
                    "in total 30 counties allocated funds.\n")
        v = check_numeric_consistency(d)
        ok(not any("47 counties" in x["claim"] for x in v),
           f"T21: universe (47 rows) must be skipped, got {v}")

    # T22: same-doc price inconsistency keyed by package + zone from the
    # run's own vocabulary; different packages do not collide.
    with tempfile.TemporaryDirectory() as d:
        make_run_dir(d, pkg_eval_rows=[
            {"archetype": "a", "zone": "Upper East", "package": "irs_campaign",
             "cost_per_case_averted": 9.10}])
        with open(os.path.join(d, "results.md"), "w") as f:
            f.write("IRS campaign in UE: $9.10/case.\n"
                    "IRS campaign in UE: $11.00/case.\n")
        v = check_numeric_consistency(d)
        ok(any(x["kind"] == "same_doc_inconsistency" for x in v),
           f"T22: same-doc inconsistency must fire, got {v}")

    # T22b: shared tokens go to the base package; written-out
    # combinations ("X+Y") are skipped, not mis-assigned.
    al = _derive_package_aliases(["pbo_llin_80", "pbo_llin_80_irs", "irs"], {})
    ok("pbo" in al["pbo_llin_80"] and "pbo" not in al["pbo_llin_80_irs"],
       f"T22b: 'pbo' must belong to the base package, got {al}")
    v22 = RunVocabulary(packages=al)
    ok(not _scan_package_counts("We fund 12 PBO+IRS units in total.", v22),
       "T22b: combination prose must be skipped")
    ok(_scan_package_counts("We fund 12 PBO units in total.", v22)[0][1]
       == "pbo_llin_80", "T22b: bare PBO maps to base package")

    # T23: a unit acronym in the label window ("... $254/DALY, mean
    # $299/DALY") is not an entity — no spurious same-doc grouping.
    with tempfile.TemporaryDirectory() as d:
        make_run_dir(d, results_text=(
            "- Option A: median $254/DALY, mean $299/DALY\n"
            "- Option B: median $318/DALY, mean $375/DALY\n"
            "- Combined: $172/DALY vs $220/DALY overall\n"))
        v = check_numeric_consistency(d)
        ok(not any(x["kind"] == "same_doc_inconsistency" for x in v),
           f"T23: unit-acronym keys must not group, got {v}")

    # ---- Phase 21 precision pass (before/after retro on 18 runs) ------ #
    # Generic fixture: 98 districts on school_vax, 20 on routine_cov, two
    # regions. Question budget $40M so other-budget sentences are known.
    def _prec_run(d, text, fname="report.md", extra_csvs=None, question=None):
        rows = ([{"district": f"a{i}", "zone": "Region A", "package": "school_vax",
                  "cost_usd": 100_000} for i in range(98)]
                + [{"district": f"b{i}", "zone": "Region B", "package": "routine_cov",
                    "cost_usd": 1_100_000} for i in range(20)])
        write_alloc(d, rows)
        for name, rs in (extra_csvs or {}).items():
            os.remove(os.path.join(d, "models", "allocation_result.csv"))
            p_ = os.path.join(d, "models", name)
            with open(p_, "w") as f:
                f.write("district,zone,package,cost_usd\n")
                for r in rs:
                    f.write(f"{r['district']},{r['zone']},{r['package']},{r['cost_usd']}\n")
        with open(os.path.join(d, "metadata.json"), "w") as f:
            json.dump({"question": question or
                       "Allocate a fixed $40M budget across districts."}, f)
        with open(os.path.join(d, fname), "w") as f:
            f.write(text)
        return [x for x in check_numeric_consistency(d) if x["severity"] == "HIGH"]

    # P1 (TP): a stated package count that the CSV contradicts still fires.
    with tempfile.TemporaryDirectory() as d:
        v = _prec_run(d, "Allocation summary: 142 school-vax districts and "
                         "20 routine-cov districts.\n")
        ok(any("school_vax" in x["claim"] and "142" in x["claim"] for x in v),
           f"P1: 142 vs 98 package count must fire HIGH, got {v}")
    # P2 (TP): a single-region budget share the CSV contradicts still fires
    # (Region A = 9.8M / 31.8M = 30.8%).
    with tempfile.TemporaryDirectory() as d:
        v = _prec_run(d, "Region A receives 52% of the budget.\n")
        ok(any("52.0% of budget" in x["claim"] for x in v),
           f"P2: 52% vs 30.8% share must fire HIGH, got {v}")
    # P3-P9 (FP patterns from the retro) must stay silent.
    for tag, text in (
        ("P3 fraction + identifier", "One perturbation (1 of 12 perturbations "
         "flips the primary recommendation: SA-001 school-vax OR at upper CI).\n"),
        ("P4 identifier", "Worst case: SA-005 routine-cov cost +10%.\n"),
        ("P5 multi-region aggregate", "Region A + Region B receive 61.3% of the budget.\n"),
        ("P6 other scenario", "Under the $200M scenario, 394 districts receive "
         "routine-cov and 70% of budget goes to Region A.\n"),
        ("P7 subset of total", "The remaining 400 districts receive routine coverage. "
         "School-vax packages are allocated to 60 districts eligible for campaigns.\n"),
        ("P8 hypothesis bound", "### H4: Optimal allocation directs >40% of budget "
         "to Region A\n"),
        ("P9 possessive universe", "We allocate across the country's 774 districts.\n"),
        ("P10 analysis groups", "The allocation includes 1 school-vax archetype.\n"),
    ):
        with tempfile.TemporaryDirectory() as d:
            v = _prec_run(d, text)
            ok(not v, f"{tag}: must abstain, got {v}")
    # P11: price label on the right ("for routine") is not attributed to the
    # left-hand comparison's entity.
    with tempfile.TemporaryDirectory() as d:
        make_run_dir(d, pkg_eval_rows=[
            {"archetype": "a", "zone": "Region A", "package": "school_vax",
             "cost_per_case_averted": 5.05}])
        with open(os.path.join(d, "results.md"), "w") as f:
            f.write("Cheaper per case ($5.05/case in Region A for school-vax vs "
                    "$8.41/case for routine coverage).\n")
        v = [x for x in check_numeric_consistency(d) if x["severity"] == "HIGH"]
        ok(not v, f"P11: right-labelled price must not be misattributed, got {v}")
    # P12: several budget-tagged scenario CSVs that all match the question
    # budget → ambiguous → abstain; exactly one match → that file is used.
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        for name in ("allocation_optimal_40M.csv", "allocation_optimal_alt_40M.csv",
                     "allocation_optimal_60M.csv"):
            with open(os.path.join(d, "models", name), "w") as f:
                f.write("district,zone,package,cost_usd\nd1,Region A,school_vax,1\n")
        with open(os.path.join(d, "metadata.json"), "w") as f:
            json.dump({"question": "Allocate a fixed $40M budget."}, f)
        ok(_resolve_alloc_path_ex(d) == (None, "ambiguous"),
           f"P12: tied scenario CSVs must abstain, got {_resolve_alloc_path_ex(d)}")
        os.remove(os.path.join(d, "models", "allocation_optimal_alt_40M.csv"))
        ok(os.path.basename(_resolve_alloc_path(d) or "") == "allocation_optimal_40M.csv",
           f"P12: budget-matching CSV must be chosen, got {_resolve_alloc_path(d)}")
    # P13: finest-granularity CSV wins over an aggregate one; comparators
    # (uniform / status quo) are never the headline.
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        for name, n in (("archetype_allocation.csv", 5), ("unit_allocation.csv", 50),
                        ("allocation_uniform.csv", 80)):
            with open(os.path.join(d, "models", name), "w") as f:
                f.write("unit,package,cost_usd\n" + "".join(f"u{i},x,1\n" for i in range(n)))
        ok(os.path.basename(_resolve_alloc_path(d) or "") == "unit_allocation.csv",
           f"P13: expected unit_allocation.csv, got {_resolve_alloc_path(d)}")

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
