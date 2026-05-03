"""Expert priors registry: loader, question matcher, validator, retro-tester.

Single source of truth for cross-run institutional memory. The registry
lives at `.claude/orchestration/expert_priors.yaml`; this module exposes:

  - load_priors() / matching_priors() / prior() / report_acknowledges()
    — the basic registry access (Phase 17 γ).
  - validate_priors() — structural literature-corroboration check
    (Phase 17 δ): each prior must meet `min_sources_*` for its severity
    and have been re-checked within `max_age_days_*`.
  - retro_test(run_dir) — replay a past run's critique YAMLs against
    the current registry; report which expert-prior-class blockers
    would have been surfaced pre-MODEL.
  - validation_warnings_for_pre_mortem() — string used to inject
    registry-health warnings into the pre-mortem agent's spawn prompt.

Why this exists
---------------
Phase 17 γ shipped a per-prior schema with a single `source_citation`
field — adequate for bootstrap, inadequate for the registry to RIDE on
literature consensus rather than reviewer judgment. Phase 17 δ adds:
  - `literature_corroboration: list[Citation]` (≥2 for MEDIUM,
    ≥3 for HIGH).
  - `last_literature_check: ISO date` (auto-staleness).
  - `auto_validation:` top-level config (thresholds + max ages).
The lib auto-validates on load; warnings flow into the pre-mortem
spawn prompt. Per-prior enrichment (Semantic Scholar / WebSearch) is
agent-side, not network-from-Python — that work belongs in an
enrichment agent, not the deterministic validator.

Public API
----------
    load_priors(manifest_path=None) -> list[ExpertPrior]
    matching_priors(question_text, decision_year, priors=None) -> list[ExpertPrior]
    prior(id_) -> ExpertPrior
    report_acknowledges(prior_, report_text) -> bool

    validate_priors(priors=None, today=None) -> list[ValidationIssue]
    validation_warnings_for_pre_mortem(priors=None) -> str

    retro_test(run_dir, priors=None) -> RetroResult
"""
from __future__ import annotations

import datetime as _dt
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any

import yaml


_MANIFEST_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..",
                 ".claude", "orchestration", "expert_priors.yaml")
)

_VALID_KINDS = frozenset({
    "external_fact",
    "data_vintage",
    "methodological_standard",
    "policy_caveat",
})

_VALID_SEVERITIES = frozenset({"HIGH", "MEDIUM", "LOW"})

_VALID_SOURCE_TYPES = frozenset({
    "paper", "report", "dataset", "tool", "meta_analysis",
})

_VALID_RELEVANCE = frozenset({
    "primary", "corroborating", "supporting", "contextual",
})


@dataclass(frozen=True)
class Citation:
    source_type: str
    citation: str
    year: int
    relevance: str
    found_via: str
    doi_or_pmid: str | None = None
    excerpt: str | None = None


@dataclass(frozen=True)
class ExpertPrior:
    id: str
    kind: str
    severity_if_unaddressed: str
    claim: str
    rationale: str
    applies_when: dict
    canonical_value: Any = None
    units: str | None = None
    acknowledgment_hints: tuple[str, ...] = field(default_factory=tuple)
    last_literature_check: str | None = None
    literature_corroboration: tuple[Citation, ...] = field(default_factory=tuple)
    found_in_runs: tuple[str, ...] = field(default_factory=tuple)
    # Legacy single-source field for backwards compat with γ-era priors;
    # δ-era priors should populate literature_corroboration instead.
    source_citation_legacy: str | None = None


@dataclass(frozen=True)
class AutoValidationConfig:
    min_sources_high: int = 3
    min_sources_medium: int = 2
    min_sources_low: int = 1
    max_age_days_high: int = 365
    max_age_days_medium: int = 730
    max_age_days_low: int = 1825
    warn_on_unverified_doi: bool = True


@dataclass(frozen=True)
class ValidationIssue:
    prior_id: str
    severity: str  # HIGH | MEDIUM | LOW (the prior's own severity)
    kind: str  # insufficient_sources | stale | unverified_doi | etc.
    message: str


@dataclass(frozen=True)
class RetroBlocker:
    """A blocker extracted from a past run's critique markdown."""
    blocker_id: str  # e.g., R-007, D-008, M-002
    excerpt: str
    file: str  # e.g., critique_redteam.md
    is_expert_prior_class: bool  # heuristic: would a prior catch it?
    matching_prior_ids: tuple[str, ...]  # priors whose hints/claim text overlap


@dataclass(frozen=True)
class RetroResult:
    run_dir: str
    n_blockers: int
    n_expert_prior_class: int
    n_covered: int
    blockers: tuple[RetroBlocker, ...]

    @property
    def coverage_rate(self) -> float:
        return (self.n_covered / self.n_expert_prior_class
                if self.n_expert_prior_class else 0.0)


_cache: list[ExpertPrior] | None = None
_cache_config: AutoValidationConfig | None = None


def load_priors(manifest_path: str | None = None) -> list[ExpertPrior]:
    """Load and validate the priors manifest. Cached after first call."""
    global _cache, _cache_config
    if manifest_path is None and _cache is not None:
        return _cache
    path = manifest_path or _MANIFEST_PATH
    if not os.path.exists(path):
        if manifest_path is None:
            _cache = []
            _cache_config = AutoValidationConfig()
        return []
    with open(path) as f:
        doc = yaml.safe_load(f) or {}

    cfg_raw = doc.get("auto_validation") or {}
    cfg = AutoValidationConfig(
        min_sources_high=int(cfg_raw.get("min_sources_high", 3)),
        min_sources_medium=int(cfg_raw.get("min_sources_medium", 2)),
        min_sources_low=int(cfg_raw.get("min_sources_low", 1)),
        max_age_days_high=int(cfg_raw.get("max_age_days_high", 365)),
        max_age_days_medium=int(cfg_raw.get("max_age_days_medium", 730)),
        max_age_days_low=int(cfg_raw.get("max_age_days_low", 1825)),
        warn_on_unverified_doi=bool(cfg_raw.get("warn_on_unverified_doi", True)),
    )

    raw = doc.get("priors")
    if raw is None:
        if manifest_path is None:
            _cache = []
            _cache_config = cfg
        return []
    if not isinstance(raw, list):
        raise ValueError(f"{path}: top-level `priors:` must be a list")
    seen_ids: set[str] = set()
    out: list[ExpertPrior] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ValueError(f"{path}: priors[{i}] is not a mapping")
        try:
            p = _from_dict(entry)
        except (KeyError, ValueError, TypeError) as e:
            raise ValueError(f"{path}: priors[{i}]: {e}") from e
        if p.id in seen_ids:
            raise ValueError(f"{path}: duplicate prior id {p.id!r}")
        seen_ids.add(p.id)
        out.append(p)
    if manifest_path is None:
        _cache = out
        _cache_config = cfg
    return out


def get_auto_validation_config() -> AutoValidationConfig:
    """Return the cached config. Triggers load if needed."""
    if _cache_config is None:
        load_priors()
    return _cache_config or AutoValidationConfig()


def _from_dict(d: dict) -> ExpertPrior:
    required = ("id", "kind", "severity_if_unaddressed", "claim",
                "rationale", "applies_when")
    for k in required:
        if k not in d:
            raise KeyError(f"missing required field {k!r}")
    kind = d["kind"]
    if kind not in _VALID_KINDS:
        raise ValueError(
            f"kind must be one of {sorted(_VALID_KINDS)}, got {kind!r}")
    sev = d["severity_if_unaddressed"]
    if sev not in _VALID_SEVERITIES:
        raise ValueError(
            f"severity_if_unaddressed must be one of "
            f"{sorted(_VALID_SEVERITIES)}, got {sev!r}")
    aw = d["applies_when"]
    if not isinstance(aw, dict):
        raise ValueError("applies_when must be a mapping")
    for key in ("keywords_all", "keywords_any"):
        if key in aw and not (
            isinstance(aw[key], list)
            and all(isinstance(x, str) for x in aw[key])
        ):
            raise ValueError(f"applies_when.{key} must be a list of strings")
    for key in ("decision_year_min", "decision_year_max"):
        if key in aw and not isinstance(aw[key], int):
            raise ValueError(f"applies_when.{key} must be an integer")
    hints = d.get("acknowledgment_hints") or []
    if not isinstance(hints, list) or not all(isinstance(x, str) for x in hints):
        raise ValueError("acknowledgment_hints must be a list of strings")
    runs = d.get("found_in_runs") or []
    if not isinstance(runs, list) or not all(isinstance(x, str) for x in runs):
        raise ValueError("found_in_runs must be a list of strings")

    corroboration_raw = d.get("literature_corroboration") or []
    if not isinstance(corroboration_raw, list):
        raise ValueError("literature_corroboration must be a list")
    citations: list[Citation] = []
    for j, c in enumerate(corroboration_raw):
        if not isinstance(c, dict):
            raise ValueError(
                f"literature_corroboration[{j}] must be a mapping")
        for ck in ("source_type", "citation", "year", "relevance",
                   "found_via"):
            if ck not in c:
                raise ValueError(
                    f"literature_corroboration[{j}]: missing {ck!r}")
        if c["source_type"] not in _VALID_SOURCE_TYPES:
            raise ValueError(
                f"literature_corroboration[{j}].source_type must be one of "
                f"{sorted(_VALID_SOURCE_TYPES)}, got {c['source_type']!r}")
        if c["relevance"] not in _VALID_RELEVANCE:
            raise ValueError(
                f"literature_corroboration[{j}].relevance must be one of "
                f"{sorted(_VALID_RELEVANCE)}, got {c['relevance']!r}")
        try:
            year = int(c["year"])
        except (TypeError, ValueError):
            raise ValueError(
                f"literature_corroboration[{j}].year must be an integer")
        citations.append(Citation(
            source_type=str(c["source_type"]),
            citation=str(c["citation"]).strip(),
            year=year,
            relevance=str(c["relevance"]),
            found_via=str(c["found_via"]),
            doi_or_pmid=c.get("doi_or_pmid"),
            excerpt=(str(c["excerpt"]).strip()
                     if c.get("excerpt") else None),
        ))

    return ExpertPrior(
        id=str(d["id"]),
        kind=kind,
        severity_if_unaddressed=sev,
        claim=str(d["claim"]).strip(),
        rationale=str(d["rationale"]).strip(),
        applies_when=dict(aw),
        canonical_value=d.get("canonical_value"),
        units=d.get("units"),
        acknowledgment_hints=tuple(hints),
        last_literature_check=d.get("last_literature_check"),
        literature_corroboration=tuple(citations),
        found_in_runs=tuple(runs),
        source_citation_legacy=(str(d["source_citation"]).strip()
                                 if d.get("source_citation") else None),
    )


def prior(id_: str) -> ExpertPrior:
    for p in load_priors():
        if p.id == id_:
            return p
    raise KeyError(f"unknown expert prior id: {id_!r}")


# --------------------------------------------------------------------------- #
# Question matching
# --------------------------------------------------------------------------- #


def _matches(p: ExpertPrior, question_lower: str,
             decision_year: int | None) -> bool:
    aw = p.applies_when
    keywords_all = aw.get("keywords_all") or []
    keywords_any = aw.get("keywords_any") or []
    for kw in keywords_all:
        if kw.lower() not in question_lower:
            return False
    if keywords_any:
        if not any(kw.lower() in question_lower for kw in keywords_any):
            return False
    y_min = aw.get("decision_year_min")
    y_max = aw.get("decision_year_max")
    if decision_year is not None:
        if y_min is not None and decision_year < y_min:
            return False
        if y_max is not None and decision_year > y_max:
            return False
    return True


def matching_priors(
    question_text: str,
    decision_year: int | None = None,
    priors: list[ExpertPrior] | None = None,
) -> list[ExpertPrior]:
    if priors is None:
        priors = load_priors()
    q = (question_text or "").lower()
    return [p for p in priors if _matches(p, q, decision_year)]


def report_acknowledges(prior_: ExpertPrior, report_text: str) -> bool:
    if not prior_.acknowledgment_hints:
        return False
    text = (report_text or "").lower()
    return any(h.lower() in text for h in prior_.acknowledgment_hints)


# --------------------------------------------------------------------------- #
# Phase 17 δ — Auto-validation
# --------------------------------------------------------------------------- #


def _min_sources_for_severity(
    severity: str, cfg: AutoValidationConfig
) -> int:
    return {
        "HIGH": cfg.min_sources_high,
        "MEDIUM": cfg.min_sources_medium,
        "LOW": cfg.min_sources_low,
    }.get(severity, cfg.min_sources_medium)


def _max_age_days_for_severity(
    severity: str, cfg: AutoValidationConfig
) -> int:
    return {
        "HIGH": cfg.max_age_days_high,
        "MEDIUM": cfg.max_age_days_medium,
        "LOW": cfg.max_age_days_low,
    }.get(severity, cfg.max_age_days_medium)


def validate_priors(
    priors: list[ExpertPrior] | None = None,
    today: _dt.date | None = None,
    cfg: AutoValidationConfig | None = None,
) -> list[ValidationIssue]:
    """Structural literature-corroboration check. No network.

    Emits one ValidationIssue per problem; multiple issues per prior
    are possible (e.g., insufficient sources AND stale).
    """
    if priors is None:
        priors = load_priors()
    if cfg is None:
        cfg = get_auto_validation_config()
    if today is None:
        today = _dt.date.today()

    issues: list[ValidationIssue] = []
    for p in priors:
        # 1. Source-count check
        n_sources = len(p.literature_corroboration)
        required = _min_sources_for_severity(
            p.severity_if_unaddressed, cfg)
        if n_sources < required:
            issues.append(ValidationIssue(
                prior_id=p.id,
                severity=p.severity_if_unaddressed,
                kind="insufficient_sources",
                message=(
                    f"{p.severity_if_unaddressed} prior {p.id!r} has "
                    f"{n_sources} corroborating source(s); "
                    f"requires ≥{required}. Consider running "
                    f"`--enrich-suggestions {p.id}` to find more."
                ),
            ))

        # 2. Freshness check
        if p.last_literature_check:
            checked: _dt.date | None = None
            if isinstance(p.last_literature_check, _dt.date):
                checked = p.last_literature_check
            elif isinstance(p.last_literature_check, str):
                try:
                    checked = _dt.date.fromisoformat(p.last_literature_check)
                except ValueError:
                    checked = None
            if checked is None:
                issues.append(ValidationIssue(
                    prior_id=p.id,
                    severity=p.severity_if_unaddressed,
                    kind="malformed_date",
                    message=(
                        f"prior {p.id!r}: last_literature_check value "
                        f"{p.last_literature_check!r} is not ISO date."
                    ),
                ))
            else:
                age = (today - checked).days
                max_age = _max_age_days_for_severity(
                    p.severity_if_unaddressed, cfg)
                if age > max_age:
                    issues.append(ValidationIssue(
                        prior_id=p.id,
                        severity=p.severity_if_unaddressed,
                        kind="stale",
                        message=(
                            f"prior {p.id!r}: last_literature_check "
                            f"{age} days ago (>{max_age} for "
                            f"{p.severity_if_unaddressed}). Re-validate."
                        ),
                    ))
        else:
            issues.append(ValidationIssue(
                prior_id=p.id,
                severity=p.severity_if_unaddressed,
                kind="missing_date",
                message=(
                    f"prior {p.id!r}: last_literature_check is unset. "
                    f"Add an ISO date to track freshness."
                ),
            ))

        # 3. Unverified-DOI advisory
        if cfg.warn_on_unverified_doi:
            n_unverified = sum(
                1 for c in p.literature_corroboration
                if c.doi_or_pmid is None
                and c.source_type in ("paper", "meta_analysis")
            )
            if n_unverified > 0:
                issues.append(ValidationIssue(
                    prior_id=p.id,
                    severity="LOW",  # advisory only
                    kind="unverified_doi",
                    message=(
                        f"prior {p.id!r}: {n_unverified} paper/meta-"
                        f"analysis citation(s) lack doi_or_pmid. Resolve "
                        f"to DOI or PMID for stable citation tracking."
                    ),
                ))

        # 4. At least one PRIMARY relevance source
        if p.literature_corroboration:
            has_primary = any(
                c.relevance == "primary"
                for c in p.literature_corroboration
            )
            if not has_primary:
                issues.append(ValidationIssue(
                    prior_id=p.id,
                    severity=p.severity_if_unaddressed,
                    kind="no_primary_source",
                    message=(
                        f"prior {p.id!r}: no source with "
                        f"relevance: primary. Designate the strongest "
                        f"corroborating source as primary."
                    ),
                ))

    return issues


def validation_warnings_for_pre_mortem(
    priors: list[ExpertPrior] | None = None,
) -> str:
    """Render registry-health warnings as a string suitable for
    injection into the pre-mortem agent's spawn prompt. Returns "" if
    the registry is clean.
    """
    issues = validate_priors(priors)
    if not issues:
        return ""
    high_issues = [i for i in issues if i.severity == "HIGH"]
    med_issues = [i for i in issues
                  if i.severity == "MEDIUM" and i.kind != "unverified_doi"]
    if not high_issues and not med_issues:
        return ""
    lines = ["**Expert priors registry health warnings:**"]
    if high_issues:
        lines.append(f"- {len(high_issues)} issue(s) on HIGH priors:")
        for i in high_issues[:5]:
            lines.append(f"  - [{i.kind}] {i.message}")
    if med_issues:
        lines.append(f"- {len(med_issues)} issue(s) on MEDIUM priors:")
        for i in med_issues[:5]:
            lines.append(f"  - [{i.kind}] {i.message}")
    lines.append(
        "These warnings do NOT block pre-mortem operation but indicate "
        "the registry needs literature refresh."
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Phase 17 δ — Retro-test against past runs
# --------------------------------------------------------------------------- #


# Heuristic markers that classify a critique blocker as "expert-prior class"
# (i.e., one a domain-knowledge prior could have caught pre-MODEL) rather
# than "within-run" (numeric drift, citation typo, figure mismatch). These
# substrings, when found in the blocker's text, suggest the concern is
# the kind of thing a prior should pre-empt.
_EXPERT_PRIOR_CLASS_MARKERS = (
    # External-fact markers
    "supply", "procurement", "global supply", "unicef", "supply chain",
    "feasibility", "operational cost", "pmi cost", "$8", "$12",
    "gbd", "burden", "daly burden", "yll only", "deaths only",
    "disaggregated", "$993m", "global fund allocation",
    # Data-vintage markers
    "vintage", "predates", "pre-2018", "pre-2015", "2010 baseline",
    "14-year gap", "resistance", "kdr", "pyrethroid",
    "synthetic proxy", "synthetic lga", "map raster", "api authentication",
    # Methodological-standard markers
    "or-to-rr", "zhang-yu", "zhang 1998", "or / (1",
    "multiplicative", "independence assumption", "sub-additive",
    "pryce 2022", "delivery efficiency", "70% coverage",
    "programmatic", "100% coverage",
)


_BLOCKER_ID_RE = re.compile(r"\b([RDMP])-(\d{3})\b")


def _extract_blockers_from_critique(
    md_text: str, file_label: str
) -> list[tuple[str, str]]:
    """Walk a critique markdown and pull out blocker_id + paragraph
    excerpt around it. Returns list of (blocker_id, excerpt_paragraph).

    Strategy: each unique blocker ID gets ONE entry — the first
    occurrence's paragraph (where it's most likely to be defined).
    """
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    # Split into paragraphs (blank-line separated).
    paragraphs = re.split(r"\n\s*\n", md_text)
    for para in paragraphs:
        for m in _BLOCKER_ID_RE.finditer(para):
            bid = m.group(0)
            if bid in seen:
                continue
            seen.add(bid)
            excerpt = " ".join(para.split())[:500]
            out.append((bid, excerpt))
    return out


def _classify_blocker(
    excerpt: str, priors: list[ExpertPrior]
) -> tuple[bool, list[str]]:
    """Classify a blocker excerpt as expert-prior-class and find
    matching priors. Returns (is_expert_prior_class, [prior_ids]).
    """
    el = excerpt.lower()
    is_class = any(mk in el for mk in _EXPERT_PRIOR_CLASS_MARKERS)
    matching: list[str] = []
    for p in priors:
        # Match if any of the prior's acknowledgment_hints, claim
        # keywords, or applies_when keywords appear in the excerpt.
        match_tokens = set()
        match_tokens.update(h.lower() for h in p.acknowledgment_hints)
        match_tokens.update(
            (p.applies_when.get("keywords_any") or []))
        match_tokens.update(
            (p.applies_when.get("keywords_all") or []))
        match_tokens = {t.lower() for t in match_tokens if len(t) > 3}
        # Also look for distinctive prior-specific phrases from the
        # claim itself (first 60 chars, lowercase).
        claim_first = p.claim.lower()[:120]
        for tok in match_tokens:
            if tok in el:
                matching.append(p.id)
                break
        else:
            # Try claim-first-phrase match for niche priors
            if any(kw in el and kw in claim_first
                   for kw in ("zhang-yu", "pryce", "gbd 2021",
                              "unicef", "map raster", "hbhi")):
                matching.append(p.id)
    return is_class, list(dict.fromkeys(matching))  # dedupe preserving order


def retro_test(
    run_dir: str,
    priors: list[ExpertPrior] | None = None,
) -> RetroResult:
    """Replay a past run's critique markdowns against the registry.

    For each unique blocker ID found in
    `critique_redteam.md`, `critique_domain.md`, `critique_methods.md`:
    - Classify as expert-prior-class (heuristic marker match) or not.
    - Look up matching prior IDs (by acknowledgment_hints and keyword overlap).
    - Coverage = (matching priors found AND classified as expert-prior class)
      / (total expert-prior class blockers).
    """
    if priors is None:
        priors = load_priors()
    blockers: list[RetroBlocker] = []
    for fname in ("critique_redteam.md", "critique_domain.md",
                  "critique_methods.md"):
        fpath = os.path.join(run_dir, fname)
        if not os.path.exists(fpath):
            continue
        try:
            with open(fpath) as f:
                text = f.read()
        except OSError:
            continue
        for bid, excerpt in _extract_blockers_from_critique(text, fname):
            is_class, match_ids = _classify_blocker(excerpt, priors)
            blockers.append(RetroBlocker(
                blocker_id=bid,
                excerpt=excerpt,
                file=fname,
                is_expert_prior_class=is_class,
                matching_prior_ids=tuple(match_ids),
            ))
    # Dedupe across files (R-007 in redteam == R-007 in domain summary)
    by_id: dict[str, RetroBlocker] = {}
    for b in blockers:
        if b.blocker_id not in by_id:
            by_id[b.blocker_id] = b
        else:
            # Prefer the entry with more matching priors / longer excerpt
            existing = by_id[b.blocker_id]
            if (len(b.matching_prior_ids) > len(existing.matching_prior_ids)
                    or len(b.excerpt) > len(existing.excerpt)):
                by_id[b.blocker_id] = b
    deduped = list(by_id.values())
    n_class = sum(1 for b in deduped if b.is_expert_prior_class)
    n_covered = sum(1 for b in deduped
                    if b.is_expert_prior_class and b.matching_prior_ids)
    return RetroResult(
        run_dir=run_dir,
        n_blockers=len(deduped),
        n_expert_prior_class=n_class,
        n_covered=n_covered,
        blockers=tuple(deduped),
    )


# --------------------------------------------------------------------------- #
# Self-test
# --------------------------------------------------------------------------- #


def _self_test() -> int:
    import tempfile

    failures: list[str] = []

    def ok(cond: bool, msg: str) -> None:
        if not cond:
            failures.append(msg)

    # T1: real manifest loads cleanly with new schema fields.
    pp = load_priors(_MANIFEST_PATH)
    ok(isinstance(pp, list),
       f"T1: manifest must load to list, got {type(pp)}")
    if pp:
        # Every prior should have ≥1 corroboration (or
        # source_citation_legacy as a fallback).
        for p in pp:
            ok(len(p.literature_corroboration) >= 1
               or p.source_citation_legacy is not None,
               f"T1: prior {p.id!r} has zero corroboration sources")

    # T2: validate_priors flags a HIGH prior with 1 source as insufficient.
    syn_insufficient = """priors:
  - id: pp_test
    kind: external_fact
    severity_if_unaddressed: HIGH
    claim: synthetic claim
    rationale: synthetic
    applies_when: {}
    last_literature_check: 2026-04-01
    literature_corroboration:
      - source_type: paper
        citation: Single Source 2024
        year: 2024
        relevance: primary
        found_via: bootstrap
"""
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(syn_insufficient)
        path = f.name
    try:
        syn_pp = load_priors(path)
        # Use default cfg; HIGH requires 3 sources
        cfg = AutoValidationConfig()
        issues = validate_priors(syn_pp, cfg=cfg)
        ok(any(i.kind == "insufficient_sources" and i.prior_id == "pp_test"
               for i in issues),
           f"T2: HIGH prior with 1 source must trigger insufficient_sources, got {issues}")
    finally:
        os.unlink(path)

    # T3: validate_priors flags a stale prior.
    syn_stale = """priors:
  - id: pp_stale
    kind: external_fact
    severity_if_unaddressed: HIGH
    claim: stale
    rationale: stale
    applies_when: {}
    last_literature_check: 2020-01-01
    literature_corroboration:
      - source_type: paper
        citation: A
        year: 2024
        relevance: primary
        found_via: bootstrap
      - source_type: paper
        citation: B
        year: 2024
        relevance: corroborating
        found_via: bootstrap
      - source_type: paper
        citation: C
        year: 2024
        relevance: supporting
        found_via: bootstrap
"""
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(syn_stale)
        path = f.name
    try:
        syn_pp = load_priors(path)
        # 2026-04-29 minus 2020-01-01 ≈ 2300 days, > 365 (HIGH max age)
        issues = validate_priors(syn_pp, today=_dt.date(2026, 4, 29))
        ok(any(i.kind == "stale" and i.prior_id == "pp_stale"
               for i in issues),
           f"T3: 2020 last_literature_check on HIGH prior must trigger stale, got {issues}")
    finally:
        os.unlink(path)

    # T4: validate_priors flags missing primary source.
    syn_no_primary = """priors:
  - id: pp_no_prim
    kind: external_fact
    severity_if_unaddressed: MEDIUM
    claim: x
    rationale: x
    applies_when: {}
    last_literature_check: 2026-04-01
    literature_corroboration:
      - source_type: paper
        citation: A
        year: 2024
        relevance: corroborating
        found_via: bootstrap
      - source_type: paper
        citation: B
        year: 2024
        relevance: supporting
        found_via: bootstrap
"""
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(syn_no_primary)
        path = f.name
    try:
        syn_pp = load_priors(path)
        issues = validate_priors(syn_pp)
        ok(any(i.kind == "no_primary_source" and i.prior_id == "pp_no_prim"
               for i in issues),
           f"T4: 0 primary sources must trigger no_primary_source, got {issues}")
    finally:
        os.unlink(path)

    # T5: validate_priors silent for a healthy prior.
    syn_healthy = """priors:
  - id: pp_healthy
    kind: external_fact
    severity_if_unaddressed: MEDIUM
    claim: x
    rationale: x
    applies_when: {}
    last_literature_check: 2026-04-01
    literature_corroboration:
      - source_type: paper
        citation: A
        doi_or_pmid: "10.1000/xxxxx"
        year: 2024
        relevance: primary
        found_via: bootstrap
      - source_type: paper
        citation: B
        doi_or_pmid: "10.1000/yyyyy"
        year: 2024
        relevance: corroborating
        found_via: bootstrap
"""
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(syn_healthy)
        path = f.name
    try:
        syn_pp = load_priors(path)
        issues = validate_priors(syn_pp, today=_dt.date(2026, 4, 29))
        ok(not issues,
           f"T5: healthy prior must have zero issues, got {issues}")
    finally:
        os.unlink(path)

    # T6: matching_priors still works (regression check).
    if pp:
        matches = matching_priors(
            "Build an agent-based model of malaria across Nigeria's "
            "774 LGAs ... PBO LLINs ... Global Fund GC7 $320M",
            decision_year=2024,
            priors=pp,
        )
        match_ids = {p.id for p in matches}
        for must in ("nigeria_malaria_total_burden_dalys",
                     "pbo_llin_global_supply_ceiling",
                     "gc7_malaria_budget_disaggregation"):
            ok(must in match_ids,
               f"T6: {must!r} must match Nigeria question, got {sorted(match_ids)}")

    # T7: retro_test on the 161312 run reports non-zero coverage.
    retro_run = os.path.normpath(os.path.join(
        os.path.dirname(__file__), "..", "..",
        "runs", "2026-04-29_161312_build-an-agent-based-model-of-malaria-tr",
    ))
    if os.path.exists(retro_run) and pp:
        result = retro_test(retro_run, priors=pp)
        ok(result.n_blockers > 0,
           f"T7: 161312 retro must find blockers, got {result.n_blockers}")
        ok(result.n_expert_prior_class > 0,
           f"T7: 161312 retro must find expert-prior-class blockers, "
           f"got {result.n_expert_prior_class}")
        # Coverage need not be 100% — we want SOME hits to confirm the
        # registry+matcher pipeline works against real data.
        ok(result.n_covered > 0,
           f"T7: 161312 retro must cover ≥1 expert-prior-class blocker, "
           f"got coverage {result.coverage_rate:.1%}")
        # Verify R-007 (PBO supply) is classified correctly
        r007 = next((b for b in result.blockers
                     if b.blocker_id == "R-007"), None)
        if r007 is not None:
            ok(r007.is_expert_prior_class,
               f"T7: R-007 (PBO supply) must be expert-prior-class")
            ok("pbo_llin_global_supply_ceiling" in r007.matching_prior_ids,
               f"T7: R-007 should match pbo_llin_global_supply_ceiling, "
               f"got {r007.matching_prior_ids}")

    # T8: retro_test on 224202 also reports SOMETHING (coverage of
    # delivery_efficiency / vintage class blockers).
    retro_run_2 = os.path.normpath(os.path.join(
        os.path.dirname(__file__), "..", "..",
        "runs", "2026-04-28_224202_build-an-agent-based-model-of-malaria-tr",
    ))
    if os.path.exists(retro_run_2) and pp:
        result2 = retro_test(retro_run_2, priors=pp)
        ok(result2.n_blockers > 0,
           f"T8: 224202 retro must find blockers, got {result2.n_blockers}")
        # Don't require coverage — older run may have different concerns

    # T9: validation_warnings_for_pre_mortem returns empty string for a
    # healthy synthetic registry.
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(syn_healthy)
        path = f.name
    try:
        syn_pp = load_priors(path)
        # Patch today via direct call
        # validation_warnings_for_pre_mortem reuses validate_priors with
        # today=today.today(); for fully deterministic test, use a
        # synthetic "today" close to last_literature_check
        # We're using 2026-04-01 last_check; today=2026-04-29 is fine
        # for a 730-day MEDIUM window
        issues = validate_priors(syn_pp, today=_dt.date(2026, 4, 29))
        ok(not issues, f"T9: healthy prior issues {issues}")
        # validation_warnings goes through validate_priors() with no
        # explicit today; we'd need to monkey-patch _dt.date.today.
        # For this regression we just check the function doesn't crash.
        s = validation_warnings_for_pre_mortem(syn_pp)
        ok(isinstance(s, str), f"T9: warnings_for_pre_mortem must return str")
    finally:
        os.unlink(path)

    # T10: missing primary source on real registry — NEW regression
    # check against accidentally publishing a prior with no primary.
    if pp:
        for p in pp:
            if p.literature_corroboration:
                ok(any(c.relevance == "primary"
                       for c in p.literature_corroboration),
                   f"T10: real prior {p.id!r} has no primary source")

    if failures:
        print(f"FAIL: {len(failures)} case(s)", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    print("OK: all self-test cases passed.", file=sys.stderr)
    return 0


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true",
                        help="run inline test cases")
    parser.add_argument("--list", action="store_true",
                        help="print all priors as id: claim summary")
    parser.add_argument("--match", metavar="QUESTION",
                        help="print priors matching the given question text")
    parser.add_argument("--match-yaml", metavar="QUESTION",
                        help="print matching priors as a YAML doc with full "
                             "literature corroboration (for agent spawn injection)")
    parser.add_argument("--decision-year", type=int, default=None,
                        help="decision year for --match / --match-yaml filtering")
    parser.add_argument("--validate", action="store_true",
                        help="run structural validation; print issues")
    parser.add_argument("--retro-test", metavar="RUN_DIR",
                        help="replay a past run's critiques against the registry")
    parser.add_argument("--enrich-suggestions", metavar="PRIOR_ID",
                        help="suggest Semantic-Scholar / WebSearch queries for "
                             "enriching a prior's corroboration list "
                             "(does NOT call the network — emits queries to run)")
    args = parser.parse_args()

    if args.self_test:
        return _self_test()
    if args.list:
        for p in load_priors():
            print(f"{p.id} [{p.severity_if_unaddressed}] {p.kind} "
                  f"({len(p.literature_corroboration)} sources)")
            print(f"  {p.claim.splitlines()[0]}")
        return 0
    if args.match:
        ms = matching_priors(args.match, decision_year=args.decision_year)
        for p in ms:
            print(f"{p.id} [{p.severity_if_unaddressed}] {p.kind}")
            print(f"  {p.claim.splitlines()[0]}")
        return 0
    if args.match_yaml:
        # Render matching priors as YAML for direct injection into the
        # pre-mortem agent's spawn prompt. Includes claim, severity,
        # acknowledgment_hints, and literature_corroboration so the
        # agent can ground its concerns in cited sources.
        ms = matching_priors(args.match_yaml,
                             decision_year=args.decision_year)
        # Also include validation warnings so the agent sees registry health
        warnings = validation_warnings_for_pre_mortem()
        out = {
            "matched_priors": [
                {
                    "id": p.id,
                    "kind": p.kind,
                    "severity_if_unaddressed": p.severity_if_unaddressed,
                    "claim": p.claim,
                    "acknowledgment_hints": list(p.acknowledgment_hints),
                    "literature_corroboration": [
                        {
                            "source_type": c.source_type,
                            "citation": c.citation,
                            "year": c.year,
                            "relevance": c.relevance,
                            "doi_or_pmid": c.doi_or_pmid,
                            "excerpt": c.excerpt,
                        } for c in p.literature_corroboration
                    ],
                } for p in ms
            ],
            "registry_warnings": warnings or "",
            "n_matched": len(ms),
        }
        print(yaml.safe_dump(out, sort_keys=False, default_flow_style=False))
        return 0
    if args.validate:
        priors = load_priors()
        issues = validate_priors(priors)
        if not issues:
            print(f"OK: {len(priors)} priors loaded, registry CLEAN.",
                  file=sys.stderr)
            return 0
        print(f"{len(issues)} issue(s) found across {len(priors)} priors:",
              file=sys.stderr)
        for i in issues:
            print(f"  [{i.severity}/{i.kind}] {i.prior_id}: {i.message}",
                  file=sys.stderr)
        # Exit nonzero only on HIGH issues; MEDIUM/LOW are advisory
        any_high = any(i.severity == "HIGH" for i in issues
                       if i.kind not in ("unverified_doi", "missing_date"))
        return 1 if any_high else 0
    if args.retro_test:
        result = retro_test(args.retro_test)
        print(f"Retro test on {result.run_dir}", file=sys.stderr)
        print(f"  Total blockers: {result.n_blockers}", file=sys.stderr)
        print(f"  Expert-prior class: {result.n_expert_prior_class}",
              file=sys.stderr)
        print(f"  Covered by registry: {result.n_covered}", file=sys.stderr)
        if result.n_expert_prior_class:
            print(f"  Coverage rate: {result.coverage_rate:.1%}",
                  file=sys.stderr)
        print(file=sys.stderr)
        for b in result.blockers:
            tag = ("EP" if b.is_expert_prior_class else "  ") + (
                "+" if b.matching_prior_ids else "-")
            mids = (",".join(b.matching_prior_ids)
                    if b.matching_prior_ids else "—")
            print(f"  [{tag}] {b.blocker_id} ({b.file}): "
                  f"matches {mids}", file=sys.stderr)
            print(f"        {b.excerpt[:150]}...", file=sys.stderr)
        return 0
    if args.enrich_suggestions:
        try:
            p = prior(args.enrich_suggestions)
        except KeyError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2
        # Emit ready-to-run queries the user (or an enrichment agent)
        # can execute via Semantic-Scholar / WebSearch / asta-literature-search.
        print(f"# Enrichment query suggestions for prior {p.id!r}",
              file=sys.stderr)
        print(f"# Current sources: {len(p.literature_corroboration)} "
              f"({p.severity_if_unaddressed} requires "
              f"{_min_sources_for_severity(p.severity_if_unaddressed, get_auto_validation_config())})",
              file=sys.stderr)
        print(file=sys.stderr)
        # Build queries from claim keywords
        first_line = p.claim.splitlines()[0][:120]
        print("## Semantic Scholar / WebSearch queries:")
        # Use distinctive nouns from the claim
        for kw in (p.applies_when.get("keywords_any") or [])[:3]:
            print(f'  "{kw}" "{first_line[:50]}"')
        for hint in p.acknowledgment_hints[:3]:
            print(f'  "{hint}" review meta-analysis')
        print()
        print("## Existing sources to deduplicate:")
        for c in p.literature_corroboration:
            print(f"  [{c.relevance}] {c.citation} ({c.year})")
        return 0
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
