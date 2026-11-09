#!/usr/bin/env python3
"""
Phase 7 Commit λ — STAGE 8.5 WRITER_QA pass.

Post-writer mechanical check that catches the late-round writer/figure
inconsistency class observed in the 1935 malaria run (4 unresolved HIGH
were all text-figure misalignments and stale numbers — entirely cosmetic
but the pipeline had no QA pass to catch them).

Validates `{run_dir}/report.md` for:

1. Stale UQ numbers — text claims a metric value that disagrees with
   uncertainty_report.yaml by >10% or by >2 orders of magnitude.

2. Figure-text comparator inconsistency — text near a figure reference
   says "X% advantage" while the figure caption says "Y% advantage" with
   abs(log10(X/Y)) > 0.5 (5×).

3. Figure annotation vs body-text — same metric named in body text and
   figure caption but with different values.

4. Stale CAVEAT — a §Limitations CRITICAL CAVEAT references a known bug
   that was resolved in a later round (cross-checked against
   pipeline_state.yaml round_progression).

5. Acceptance language (F6 S1-7) — a report whose run was not accepted
   (declared scope, not accepted, failed, or not yet decided) uses a term
   from .claude/orchestration/acceptance_language.yaml.

Output: `{run_dir}/writer_qa_report.yaml` with verdict
CLEAN / REVISE / MAJOR_REVISION and a list of issues.

Usage:
    python3 scripts/writer_qa.py <run_dir>
    python3 scripts/writer_qa.py --self-test
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Optional

try:
    import yaml
except ImportError:
    print("ERROR: pyyaml not installed", file=sys.stderr)
    sys.exit(2)

# Phase 21 γ: closed verdict vocabulary (the validator treats an
# exported VERDICTS tuple as authoritative). MISSING / ERROR are
# fail-closed outcomes for an absent or unreadable report.md — the
# script used to return CLEAN for both.
VERDICTS = ("CLEAN", "REVISE", "MAJOR_REVISION", "MISSING", "ERROR")


def normalize_verdict(v: object) -> str:
    """'major revision' / 'major-revision' / 'Major_Revision' →
    'MAJOR_REVISION'. Unknown strings are returned upper-cased so the
    consumer can reject them."""
    return re.sub(r"[\s\-]+", "_", str(v or "").strip()).upper()


# Phase 21 γ: the helpers come from scripts/lib/md_helpers.py instead of
# exec'ing the whole validator module.
def _load_validator_helpers():
    """Return (_parse_md_tables, _extract_last_numeric,
    _strip_limitations_and_scope)."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from lib import md_helpers as _md
    return (_md._parse_md_tables, _md._extract_last_numeric,
            _md._strip_limitations_and_scope)


# Markdown image reference: ![caption](path/to/file.png)
_IMG_REF_RE = re.compile(
    r"!\[(?P<caption>[^\]]*)\]\((?P<path>[^)]+)\)",
    re.MULTILINE,
)

# Section header (any level).
_SECTION_HEADER_RE = re.compile(
    r"^#{1,4}\s+(?P<title>[^\n]+?)\s*$", re.MULTILINE,
)

# Likely UQ-related section names (where stale uncertainty numbers
# typically live).
_UQ_SECTION_TITLES = (
    "uncertainty", "uq", "uncertainty quantification",
    "results", "key findings", "headline",
)

# Stale-CAVEAT detector: matches "CRITICAL CAVEAT" or "KNOWN BUG"
# alongside specific bug-name patterns.
_STALE_CAVEAT_RE = re.compile(
    r"\b(?:CRITICAL\s+CAVEAT|KNOWN\s+BUG|UNRESOLVED\s+BUG)\b",
    re.IGNORECASE,
)

# Common bug-name patterns the writer might describe.
_BUG_NAMES = (
    "yll_per_death=1.0", "yll=1", "RMSE=0.0",
    "yll_per_death = 1", "frozen", "hardcoded",
)

# Comparator-claim sentences: "X% advantage", "X% improvement",
# "X% gain", "X% better than", etc.
_COMPARATOR_CLAIM_RE = re.compile(
    r"(?P<num>[+-]?\d{1,3}(?:[.,]\d+)?)\s*%\s*"
    r"(?:advantage|improvement|better|gain|lift|increase|reduction)",
    re.IGNORECASE,
)


@dataclass
class QAIssue:
    kind: str
    severity: str  # MAJOR | MINOR
    section: Optional[str]
    description: str
    evidence: str = ""

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "severity": self.severity,
            "section": self.section,
            "description": self.description,
            "evidence": self.evidence[:300],
        }


@dataclass
class QAResult:
    verdict: str  # CLEAN | REVISE | MAJOR_REVISION | MISSING | ERROR
    issues: list[QAIssue] = field(default_factory=list)
    n_major: int = 0
    n_minor: int = 0


def _slice_sections(text: str) -> dict[str, tuple[int, int]]:
    """Return {section_title_lower: (start_offset, end_offset)} where
    offsets bound the section body (header excluded). Linear scan."""
    sections: dict[str, tuple[int, int]] = {}
    headers = list(_SECTION_HEADER_RE.finditer(text))
    for i, m in enumerate(headers):
        title = m.group("title").strip().lower()
        # Strip leading numbering "1.", "1.2", etc.
        title = re.sub(r"^\d+(?:\.\d+)*\.?\s*", "", title)
        start = m.end()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        sections[title] = (start, end)
    return sections


def _find_uq_section_text(text: str, sections: dict) -> str:
    """Return concatenated text of any sections whose title matches a
    UQ-related keyword. Empty string if none."""
    parts = []
    for title, (start, end) in sections.items():
        if any(kw in title for kw in _UQ_SECTION_TITLES):
            parts.append(text[start:end])
    return "\n\n".join(parts)


def _check_stale_uq_numbers(text: str, run_dir: str,
                            extract_numeric) -> list[QAIssue]:
    """Compare numerics in §Results / §UQ sections against
    uncertainty_report.yaml's scalar_outputs. Flag if any explicit
    DALYs/cases/cost_per_daly value in text disagrees with yaml by
    >10x (orders-of-magnitude staleness).
    """
    issues: list[QAIssue] = []
    uq_path = os.path.join(run_dir, "uncertainty_report.yaml")
    if not os.path.exists(uq_path):
        return issues
    try:
        with open(uq_path) as f:
            uq = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return issues
    scalar_outputs = uq.get("scalar_outputs") or {}
    if not isinstance(scalar_outputs, dict):
        return issues

    # Look for "X DALYs averted" / "X deaths averted" / "$Y per DALY"
    # patterns in the report's §Results and §UQ sections.
    sections = _slice_sections(text)
    target_text = _find_uq_section_text(text, sections)
    if not target_text:
        return issues

    # Heuristic patterns: extract a numeric value preceding a metric name.
    metric_patterns = [
        (r"(?P<num>[\d,.]+\s*[MmBbKk]?)\s*DALY", "dalys_averted"),
        (r"(?P<num>[\d,.]+\s*[MmBbKk]?)\s*deaths?\s+averted", "deaths_averted"),
        (r"\$(?P<num>[\d,.]+)\s*(?:per|/)\s*DALY", "cost_per_daly"),
        (r"(?P<num>[\d,.]+\s*[MmBbKk]?)\s*cases?\s+averted", "cases_averted"),
    ]

    # Build a normalized lookup: yaml metric mean values keyed by
    # canonical name. Match the FIRST yaml key containing the metric token.
    def _yaml_value_for(metric_token: str) -> Optional[float]:
        for k, v in scalar_outputs.items():
            if metric_token in k.lower():
                if isinstance(v, dict) and "mean" in v:
                    try:
                        return float(v["mean"])
                    except (TypeError, ValueError):
                        return None
        return None

    for pattern, metric_token in metric_patterns:
        for m in re.finditer(pattern, target_text, re.IGNORECASE):
            text_value = extract_numeric(m.group("num"))
            if text_value is None:
                continue
            yaml_value = _yaml_value_for(metric_token)
            if yaml_value is None or yaml_value <= 0 or text_value <= 0:
                continue
            ratio = max(text_value, yaml_value) / min(text_value, yaml_value)
            if ratio > 10.0:  # order-of-magnitude staleness
                issues.append(QAIssue(
                    kind="stale_uq_number",
                    severity="MAJOR",
                    section="Results/UQ",
                    description=(
                        f"Text reports {metric_token}={text_value:.4g} "
                        f"vs uncertainty_report.yaml mean={yaml_value:.4g} "
                        f"({ratio:.1f}x discrepancy). Likely stale numbers "
                        f"from a pre-fix draft. Update text to match yaml."
                    ),
                    evidence=m.group(0),
                ))
                break  # one issue per pattern is enough
    return issues


def _check_figure_text_comparator_inconsistency(
        text: str, extract_numeric) -> list[QAIssue]:
    """When text near a figure reference says X% advantage and the
    figure's caption says Y% advantage with X/Y > 5x or Y/X > 5x,
    flag as inconsistency.
    """
    issues: list[QAIssue] = []
    images = list(_IMG_REF_RE.finditer(text))
    for img in images:
        caption = img.group("caption") or ""
        cap_claims = [extract_numeric(m.group("num"))
                      for m in _COMPARATOR_CLAIM_RE.finditer(caption)]
        cap_claims = [v for v in cap_claims if v is not None and v != 0]
        if not cap_claims:
            continue

        # Look for comparator claims in surrounding text (±400 chars).
        ctx_start = max(0, img.start() - 400)
        ctx_end = min(len(text), img.end() + 400)
        # Exclude the caption itself from the surrounding text scan.
        before = text[ctx_start:img.start()]
        after = text[img.end():ctx_end]
        ctx = before + " " + after
        text_claims = [extract_numeric(m.group("num"))
                       for m in _COMPARATOR_CLAIM_RE.finditer(ctx)]
        text_claims = [v for v in text_claims if v is not None and v != 0]
        if not text_claims:
            continue

        # Compare the closest pair (smallest in caption vs nearest in text).
        for cap_v in cap_claims:
            for txt_v in text_claims:
                if cap_v == 0 or txt_v == 0:
                    continue
                ratio = max(abs(cap_v), abs(txt_v)) / min(abs(cap_v), abs(txt_v))
                if ratio > 5.0:
                    issues.append(QAIssue(
                        kind="figure_text_comparator_inconsistency",
                        severity="MAJOR",
                        section=None,
                        description=(
                            f"Figure caption says {cap_v}% but nearby text "
                            f"says {txt_v}% ({ratio:.1f}x ratio). The "
                            f"figure and text use different comparators or "
                            f"one is stale. Pick the honest comparator and "
                            f"align both."
                        ),
                        evidence=(
                            f"caption: {caption[:150]} | "
                            f"text near: {ctx.strip()[:150]}"
                        ),
                    ))
                    break
            else:
                continue
            break  # one issue per figure
    return issues


def _check_annotation_vs_body(text: str, extract_numeric) -> list[QAIssue]:
    """Check for cases where a figure caption mentions a metric value
    and the body text mentions the same metric with a different value.
    Specifically: RMSE / MAE / R-squared / accuracy / coverage values.

    Heuristic: extract patterns like "RMSE = X" from captions and
    "RMSE = Y" from non-caption text; flag if X != Y when both refer to
    the same artifact (within ±200 chars of figure reference).
    """
    issues: list[QAIssue] = []
    metric_pattern = re.compile(
        r"(?P<metric>RMSE|MAE|R[\s-]?squared|R\^?2|accuracy|concordance)\s*"
        r"[=:]\s*(?P<num>[\d.]+)\s*(?P<unit>pp|%)?",
        re.IGNORECASE,
    )

    images = list(_IMG_REF_RE.finditer(text))
    for img in images:
        caption = img.group("caption") or ""
        cap_metrics = {
            m.group("metric").upper().replace(" ", "").replace("-", ""):
            (extract_numeric(m.group("num")), m.group("unit") or "")
            for m in metric_pattern.finditer(caption)
        }
        if not cap_metrics:
            continue

        # Look in ±300 chars excluding the caption itself.
        ctx_start = max(0, img.start() - 300)
        ctx_end = min(len(text), img.end() + 300)
        before = text[ctx_start:img.start()]
        after = text[img.end():ctx_end]
        ctx = before + " " + after
        for m in metric_pattern.finditer(ctx):
            key = m.group("metric").upper().replace(" ", "").replace("-", "")
            txt_val = extract_numeric(m.group("num"))
            txt_unit = m.group("unit") or ""
            if key not in cap_metrics or txt_val is None:
                continue
            cap_val, cap_unit = cap_metrics[key]
            if cap_val is None:
                continue
            # Different values for same metric and matching units
            if cap_unit == txt_unit and abs(cap_val - txt_val) > 0.01:
                issues.append(QAIssue(
                    kind="figure_annotation_inconsistency",
                    severity="MAJOR",
                    section=None,
                    description=(
                        f"Figure caption says {key}={cap_val}{cap_unit} "
                        f"but body text near the figure says {key}="
                        f"{txt_val}{txt_unit}. Reconcile to the correct "
                        f"value (re-run calibration if needed) and update "
                        f"both."
                    ),
                    evidence=f"caption: {caption[:120]} | body: {m.group(0)}",
                ))
                break
    return issues


def _check_stale_caveat(text: str, run_dir: str,
                        strip_limitations) -> list[QAIssue]:
    """Detect §Limitations sections referencing CRITICAL CAVEATs that
    name bugs already fixed in later rounds.

    Heuristic: if §Limitations mentions a known bug pattern AND the
    pipeline_state.yaml shows the run completed STAGE 8 (writer ran
    after at least one PATCH round), the caveat is likely stale.
    """
    issues: list[QAIssue] = []
    # Identify Limitations text by inverse: stripped text vs full text.
    # The stripped output excludes Limitations; what was removed IS the
    # Limitations content.
    stripped = strip_limitations(text)
    if len(stripped) >= len(text):
        return issues  # no Limitations section
    # Re-extract roughly which content was in §Limitations.
    # Easier: just search for the caveat regex anywhere; the Limitations
    # section is where it almost always appears.
    caveats = list(_STALE_CAVEAT_RE.finditer(text))
    if not caveats:
        return issues

    # Cross-check pipeline_state.yaml for round progression.
    state_path = os.path.join(run_dir, "pipeline_state.yaml")
    if not os.path.exists(state_path):
        return issues
    try:
        with open(state_path) as f:
            state = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return issues
    completed = state.get("completed") or {}
    # Did the run progress past round 1? If so, a CAVEAT for a bug
    # is suspicious — maybe it was fixed in a later round.
    has_late_rounds = any("R2" in k or "R3" in k or "R4" in k or "R5" in k
                          for k in completed.keys())
    if not has_late_rounds:
        return issues

    for m in caveats:
        ctx_start = max(0, m.start() - 50)
        ctx_end = min(len(text), m.end() + 300)
        ctx = text[ctx_start:ctx_end]
        # Look for bug-name patterns in the context.
        for bug in _BUG_NAMES:
            if bug.lower() in ctx.lower():
                issues.append(QAIssue(
                    kind="stale_caveat",
                    severity="MINOR",
                    section="Limitations",
                    description=(
                        f"§Limitations contains a CRITICAL CAVEAT "
                        f"referencing a bug pattern ('{bug}') in a run "
                        f"that progressed past round 1. The bug may have "
                        f"been fixed in a later round. Verify and remove "
                        f"the stale caveat if the bug is no longer "
                        f"present."
                    ),
                    evidence=ctx,
                ))
                break
    return issues


ACCEPTANCE_MANIFEST = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                   ".claude", "orchestration", "acceptance_language.yaml")


def load_acceptance_language(path: str = ACCEPTANCE_MANIFEST) -> dict:
    """The acceptance-language manifest. Raises on a missing or malformed file (callers fail closed)."""
    with open(path, encoding="utf-8") as f:
        doc = yaml.safe_load(f) or {}
    if not isinstance(doc.get("terms"), list) or not doc["terms"] or not isinstance(doc.get("status_lines"), dict):
        raise ValueError(f"{os.path.basename(path)}: needs a non-empty `terms` list and `status_lines`")
    for t in doc["terms"]:
        if not (isinstance(t, dict) and isinstance(t.get("id"), str) and isinstance(t.get("pattern"), str)):
            raise ValueError(f"{os.path.basename(path)}: each term needs a string id and pattern")
        re.compile(t["pattern"])
    for k in ("negations", "allowed", "attributions"):
        v = doc.get(k) or []
        if not isinstance(v, list) or not all(isinstance(x, str) and x for x in v):
            raise ValueError(f"{os.path.basename(path)}: `{k}` must be a list of quoted strings "
                             "(YAML reads an unquoted no as false)")
    return doc


ACCEPTED_STATUSES = ("completed", "completed_with_report_restored")
REPORT_RENDER = "report_render.json"


def _record(run_dir: str, name: str) -> Optional[dict]:
    try:
        doc = json.load(open(os.path.join(run_dir, name)))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else {}


def run_status(run_dir: str) -> Optional[str]:
    """The run's status from the harness's own records: the final gate's decision (final_gate.json) and
    the v2 machine's state (run_state.json). metadata.json is not read: agents can edit it. Records that
    disagree on acceptance are `records_conflict` (not accepted); a run still in progress is `pending`.
    Only a current gate decision counts: none, or one older than report.md, is `no_gate_decision`."""
    g, rs = _record(run_dir, "final_gate.json"), _record(run_dir, "run_state.json")
    try:
        stale = os.path.getmtime(os.path.join(run_dir, "final_gate.json")) < os.path.getmtime(
            os.path.join(run_dir, "report.md"))
    except OSError:
        stale = g is None
    if g is None or stale:
        return "no_gate_decision"
    action = (g or {}).get("action")
    by_gate = {"ACCEPT": "completed", "DECLARE_SCOPE": "completed_scope_declared"}.get(action) or (
        "not_accepted" if g is not None else None)
    st = (rs or {}).get("status")
    if st and by_gate and (st in ACCEPTED_STATUSES) != (by_gate == "completed"):
        return "records_conflict"
    if rs is not None:
        return str(st) if st else "pending"
    return by_gate


def report_writer_text(run_dir: str, text: str) -> str:
    """The part of report.md to check: the writer's span the harness recorded when it rendered the
    report (report_render.json), only while the record's hash matches the report; else all of it."""
    import hashlib
    rec = _record(run_dir, REPORT_RENDER) or {}
    try:
        a, b = (int(x) for x in rec["writer_span"])
        if rec["report_sha256"] == hashlib.sha256(text.encode("utf-8")).hexdigest() and 0 <= a <= b <= len(text):
            return text[a:b]
    except (KeyError, TypeError, ValueError):
        pass
    return text


def acceptance_language_hits(text: str, status: Optional[str], manifest: Optional[dict] = None) -> list[str]:
    """Each use of an acceptance term ("<term id>: <context>") in a report whose status is not an
    accepted one. A term negated within the manifest's window, or an allowed phrase, does not count."""
    m = manifest or load_acceptance_language()
    if status in (m.get("accepted_statuses") or []):
        return []
    for a, b in (("\u2018", "'"), ("\u2019", "'"), ("\u201c", '"'), ("\u201d", '"')):
        text = text.replace(a, b)
    for a in m.get("allowed") or []:
        text = re.sub(a, " ", text, flags=re.I)
    att = "|".join(re.escape(a) for a in m.get("attributions") or [])
    if att:   # a quotation attributed to a critic or reviewer is their words, not the report's claim
        text = re.sub(r'(\b(?:%s)\w*\b[^"\n.]{0,60})"[^"\n]{0,400}"' % att, r"\1 ", text, flags=re.I)
    # A Markdown soft break (one newline inside a paragraph) is a space; a blank line, a heading, a list
    # item, a quote or a table row still starts a new clause.
    text = re.sub(r"(?<!\n)\n(?![\n#>|*+-]|\s*\d+[.)]\s)", " ", text)
    negs = {n.lower() for n in m.get("negations") or []}
    win = int(m.get("negation_window", 3))
    out = []
    for t in m["terms"]:
        for hit in re.finditer(t["pattern"], text, re.I):
            # a negation counts only in the term's own clause
            clause = re.split(r"[.;:!?,()\n]|\bbut\b", text[max(0, hit.start() - 120):hit.start()], flags=re.I)[-1]
            before = re.findall(r"[a-z']+", clause.lower())[-win:]
            if any(w in negs or w.endswith("n't") for w in before):
                continue
            ctx = " ".join(text[max(0, hit.start() - 60):hit.end() + 60].split())
            out.append(f"{t['id']}: ...{ctx}...")
    return out


def _check_acceptance_language(text: str, run_dir: str) -> list[QAIssue]:
    status = run_status(run_dir)
    try:
        hits = acceptance_language_hits(report_writer_text(run_dir, text), status)
    except Exception as e:  # noqa: BLE001 -- fail closed: the check could not run
        return [QAIssue(kind="acceptance_language_unchecked", severity="MAJOR", section=None,
                        description=f"The acceptance-language manifest could not be read ({type(e).__name__}: {e}).")]
    return [QAIssue(kind="acceptance_language", severity="MAJOR", section=None,
                    description=(f"The run status is {status or 'not yet decided'}, so the report may not use "
                                 "acceptance language; say what was tested and what is declared."),
                    evidence=h) for h in hits]


def writer_qa(run_dir: str) -> QAResult:
    """Run all writer-QA checks on report.md. Returns QAResult."""
    report_path = os.path.join(run_dir, "report.md")
    if not os.path.exists(report_path):
        return QAResult(verdict="MISSING", issues=[QAIssue(
            kind="report_missing", severity="MAJOR", section=None,
            description="report.md does not exist; nothing was QA'd.",
            evidence=report_path)], n_major=1)

    try:
        with open(report_path, encoding="utf-8") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError) as e:
        return QAResult(verdict="ERROR", issues=[QAIssue(
            kind="report_unreadable", severity="MAJOR", section=None,
            description=f"report.md could not be read ({type(e).__name__}).",
            evidence=str(e)[:200])], n_major=1)

    # Lazy-load helpers from validate_critique_yaml.
    parse_tables, extract_numeric, strip_limitations = _load_validator_helpers()

    issues: list[QAIssue] = []
    issues.extend(_check_stale_uq_numbers(text, run_dir, extract_numeric))
    issues.extend(_check_figure_text_comparator_inconsistency(
        text, extract_numeric))
    issues.extend(_check_annotation_vs_body(text, extract_numeric))
    issues.extend(_check_stale_caveat(text, run_dir, strip_limitations))
    issues.extend(_check_acceptance_language(text, run_dir))

    n_major = sum(1 for i in issues if i.severity == "MAJOR")
    n_minor = sum(1 for i in issues if i.severity == "MINOR")

    if n_major == 0 and n_minor == 0:
        verdict = "CLEAN"
    elif n_major == 0:
        verdict = "REVISE"
    elif n_major <= 2:
        verdict = "REVISE"
    else:
        verdict = "MAJOR_REVISION"

    return QAResult(
        verdict=verdict,
        issues=issues,
        n_major=n_major,
        n_minor=n_minor,
    )


def write_report(run_dir: str, result: QAResult) -> str:
    """Write writer_qa_report.yaml. Returns path."""
    out_path = os.path.join(run_dir, "writer_qa_report.yaml")
    payload = {
        "verdict": result.verdict,
        "n_major": result.n_major,
        "n_minor": result.n_minor,
        "issues": [i.to_dict() for i in result.issues],
    }
    with open(out_path, "w") as f:
        yaml.safe_dump(payload, f, sort_keys=False, default_flow_style=False)
    return out_path


def _run_self_test() -> int:
    """Inline self-test. Returns 0 on success, 1 on failure."""
    import tempfile

    failures: list[str] = []

    def ok(cond: bool, label: str) -> None:
        if not cond:
            failures.append(label)

    # Case T1: stale UQ — text says 2.76M DALYs, yaml mean 8.85M.
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "report.md"), "w") as f:
            f.write(
                "# Report\n\n"
                "## Results\n"
                "The optimized allocation averts 2.76M DALYs annually.\n\n"
                "## Uncertainty Quantification\n"
                "DALYs averted (3yr): mean 2.76M.\n"
            )
        with open(os.path.join(d, "uncertainty_report.yaml"), "w") as f:
            f.write(
                "scalar_outputs:\n"
                "  dalys_averted_3yr:\n"
                "    mean: 88500000\n"  # 88.5M, 32x larger than 2.76M
                "    median: 81900000\n"
                "    ci_low: 40700000\n"
                "    ci_high: 178000000\n"
                "    n: 200\n"
            )
        r = writer_qa(d)
        ok(any(i.kind == "stale_uq_number" for i in r.issues),
           f"T1: expected stale_uq_number, got {[i.kind for i in r.issues]}")

    # Case T2: figure-text comparator inconsistency.
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "report.md"), "w") as f:
            f.write(
                "# Report\n\n"
                "## H1 Results\n"
                "Geographic targeting yields a modest 2% advantage over "
                "smart-uniform allocation.\n\n"
                "![Figure 7: Hypothesis tests showing +105% improvement "
                "for optimized vs uniform.](figures/fig07.png)\n\n"
                "The honest figure should match the text.\n"
            )
        r = writer_qa(d)
        ok(any(i.kind == "figure_text_comparator_inconsistency"
               for i in r.issues),
           f"T2: expected figure_text inconsistency, "
           f"got {[i.kind for i in r.issues]}")

    # Case T3: annotation-vs-body (RMSE 0.22 in caption, 0.26 in text).
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "report.md"), "w") as f:
            f.write(
                "# Report\n\n"
                "## Calibration\n"
                "The model achieves RMSE = 0.26 pp across 22 archetypes.\n\n"
                "![Figure 2: Calibration fit. Annotation: RMSE = 0.22 pp.]"
                "(figures/fig02.png)\n"
            )
        r = writer_qa(d)
        ok(any(i.kind == "figure_annotation_inconsistency"
               for i in r.issues),
           f"T3: expected figure_annotation_inconsistency, "
           f"got {[i.kind for i in r.issues]}")

    # Case T4: clean report → no issues.
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "report.md"), "w") as f:
            f.write(
                "# Report\n\n"
                "## Results\n"
                "The model achieves RMSE = 0.26 pp.\n\n"
                "![Figure 2: Calibration fit. RMSE 0.26 pp.](figures/fig02.png)\n\n"
                "Geographic targeting yields a 2% advantage.\n\n"
                "![Figure 7: Hypothesis tests showing +2% advantage.]"
                "(figures/fig07.png)\n"
            )
        with open(os.path.join(d, "uncertainty_report.yaml"), "w") as f:
            f.write("scalar_outputs: {}\n")
        r = writer_qa(d)
        ok(r.verdict == "CLEAN",
           f"T4: clean report should be CLEAN, got {r.verdict} "
           f"with issues {[i.kind for i in r.issues]}")

    # Case T5: stale CAVEAT in Limitations after late rounds.
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "report.md"), "w") as f:
            f.write(
                "# Report\n\n"
                "## Results\n"
                "Numbers look fine.\n\n"
                "## 10. Limitations\n"
                "**CRITICAL CAVEAT**: yll_per_death=1.0 in the UQ "
                "surrogate causes DALY estimates to be 50x wrong.\n"
            )
        with open(os.path.join(d, "pipeline_state.yaml"), "w") as f:
            f.write(
                "current_stage: COMPLETE\n"
                "current_round: 5\n"
                "completed:\n"
                "  PLAN: {at: '2026-04-25T20:07:00'}\n"
                "  DATA: {at: '2026-04-25T20:27:00'}\n"
                "  MODEL_R1: {at: '2026-04-25T21:05:00'}\n"
                "  MODEL_R2: {at: '2026-04-25T22:05:00'}\n"
                "  MODEL_R5: {at: '2026-04-25T01:05:00'}\n"
            )
        r = writer_qa(d)
        ok(any(i.kind == "stale_caveat" for i in r.issues),
           f"T5: stale CAVEAT after round 5 should fire, "
           f"got {[i.kind for i in r.issues]}")

    # Case T6: writer_qa output yaml is well-formed.
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "report.md"), "w") as f:
            f.write("# Report\n\nClean content.\n")
        r = writer_qa(d)
        path = write_report(d, r)
        with open(path) as f:
            data = yaml.safe_load(f)
        ok(data["verdict"] == "CLEAN" and data["n_major"] == 0,
           f"T6: writer_qa_report.yaml roundtrip failed, got {data}")

    # Phase 21 γ: missing / unreadable report.md fails closed; verdict
    # normalization for consumers reading the YAML.
    with tempfile.TemporaryDirectory() as d:
        r = writer_qa(d)
        ok(r.verdict == "MISSING" and r.n_major == 1,
           f"T-missing: absent report.md must be MISSING, got {r.verdict}")
        with open(os.path.join(d, "report.md"), "wb") as fh:
            fh.write(b"\xff\xfe\x00bad utf8 \xc3\x28")
        r = writer_qa(d)
        ok(r.verdict == "ERROR", f"T-unreadable: must be ERROR, got {r.verdict}")
    for raw, want in (("major revision", "MAJOR_REVISION"),
                      ("Major-Revision", "MAJOR_REVISION"),
                      (" clean ", "CLEAN"), (None, "")):
        ok(normalize_verdict(raw) == want,
           f"T-normalize: {raw!r} → {normalize_verdict(raw)!r}, want {want!r}")
    ok(set(VERDICTS) >= {"CLEAN", "REVISE", "MAJOR_REVISION"},
       "T-verdicts: VERDICTS must include the Phase 7 vocabulary")

    # F6 S1-7: a report whose run was not accepted uses no acceptance language
    # (the terms live in .claude/orchestration/acceptance_language.yaml).
    over = ("# Report\n\nThe allocation was accepted. The model is validated against the trials, "
            "and the analysis shows conclusively that the plan is best.\n")
    honest = ("# Report\n\nRun status: declared scope, not accepted. The model was tested against the "
              "trials; it is not validated against cases by state.\n")
    # The status comes from the harness's gate and run state, never metadata.json (agent-writable);
    # records that disagree fail closed.
    dec, acc = {"final_gate.json": {"action": "DECLARE_SCOPE"}}, {"final_gate.json": {"action": "ACCEPT"}}
    forged = "<!-- harness: rendered from analysis/analysis.json -->\nThe plan is validated.\n"
    for label, files, text, want in (
            ("declared", dec, over, 3), ("accepted", acc, over, 0), ("declared, honest", dec, honest, 0),
            ("no records", {}, over, 3), ("metadata says completed", dict(dec, **{"metadata.json": {"status": "completed"}}), over, 3),
            ("run state accepted, gate declared", dict(dec, **{"run_state.json": {"status": "completed"}}), over, 3),
            ("gate accepted, run state declared", dict(acc, **{"run_state.json": {"status": "completed_scope_declared"}}), over, 3),
            ("forged harness marker", dec, forged, 1),
            ("run state says completed, no gate decision", {"run_state.json": {"status": "completed"}}, over, 3)):
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "report.md"), "w").write(text)
            for name, doc in files.items():
                json.dump(doc, open(os.path.join(d, name), "w"))
            r = writer_qa(d)
            hits = [i for i in r.issues if i.kind == "acceptance_language"]
            ok(len(hits) == want and all(i.severity == "MAJOR" for i in hits),
               f"T-accept ({label}): {want} acceptance-language issue(s) expected, got {[i.evidence for i in hits]}")
    # Only harness additions recorded with the report's hash are skipped (report_render.json).
    import hashlib
    with tempfile.TemporaryDirectory() as d:
        draft = "The plan was tested against the trials.\n"
        rep = "<!-- harness: run status -->\n**Run status: declared scope, not accepted.**\n\n" + draft
        rep += "## Open findings\n- D-1: the reviewer says the model is validated only on prevalence.\n"
        open(os.path.join(d, "report.md"), "w").write(rep)
        json.dump(dec["final_gate.json"], open(os.path.join(d, "final_gate.json"), "w"))
        s0 = rep.index(draft)
        json.dump({"report_sha256": hashlib.sha256(rep.encode()).hexdigest(), "writer_span": [s0, s0 + len(draft)]},
                  open(os.path.join(d, "report_render.json"), "w"))
        n_auth = sum(i.kind == "acceptance_language" for i in writer_qa(d).issues)
        open(os.path.join(d, "report.md"), "a").write("The allocation is accepted.\n")
        n_tamp = sum(i.kind == "acceptance_language" for i in writer_qa(d).issues)
        ok(n_auth == 0 and n_tamp >= 1,
           f"T-accept-auth: recorded harness blocks are skipped ({n_auth}); a changed report is read whole ({n_tamp})")
    # Negations: within their own clause, quoted in the manifest, curly apostrophes; attributed quotes.
    for text, want in (("This is not a caveat. Accepted findings support implementation.", 1),
                       ("No accepted conclusions are offered.", 0),
                       ("The model isn’t validated against cases.", 0),
                       ("The model has not been tested, but is validated by the fit.", 1),
                       ("The domain reviewer wrote: “the model is validated only on prevalence”.", 0),
                       ("Our finding: \"the plan is validated\".", 1)):
        try:
            got = len(acceptance_language_hits(text, "completed_scope_declared"))
        except Exception as e:  # noqa: BLE001
            got = repr(e)
        ok(got == want, f"T-accept-neg: {text!r}: {want} hit(s) expected, got {got}")
    try:
        man = load_acceptance_language()
        typed = all(isinstance(x, str) for k in ("negations", "allowed", "attributions") for x in man.get(k) or [None])
    except Exception as e:  # noqa: BLE001
        typed = repr(e)
    ok(typed is True, f"T-accept-types: manifest negations, allowed phrases and attributions are strings ({typed})")
    # Round 2: a gate decision older than the report is not current; run_state.json is protected from agents;
    # a Markdown soft break does not end a clause, a blank line or a list item does.
    with tempfile.TemporaryDirectory() as d:
        json.dump({"action": "ACCEPT"}, open(os.path.join(d, "final_gate.json"), "w"))
        os.utime(os.path.join(d, "final_gate.json"), (1, 1))
        open(os.path.join(d, "report.md"), "w").write(over)
        ok(sum(i.kind == "acceptance_language" for i in writer_qa(d).issues) == 3,
           "T-accept-stale: an ACCEPT older than the report grants no exemption")
        try:
            sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            from agents import enforcement as _EN
            why = _EN.protected_reason(os.path.join(d, "run_state.json"), d, None)
        except Exception as e:  # noqa: BLE001
            why = None
        ok(bool(why), "T-accept-protect: agents may not write run_state.json")
    for text, want in (("The model is not yet\nvalidated.", 0), ("We did not run it.\n\nValidated by the fit.", 1),
                       ("Not run:\n- validated by the fit", 1)):
        got = len(acceptance_language_hits(text, "completed_scope_declared"))
        ok(got == want, f"T-accept-softbreak: {text!r}: {want} hit(s) expected, got {got}")
    with tempfile.TemporaryDirectory() as d:
        bad = os.path.join(d, "m.yaml")
        open(bad, "w").write("terms: [{id: a, pattern: 'a'}]\nstatus_lines: {other: x}\nnegations: [not, no]\n")
        try:
            load_acceptance_language(bad)
            ok(False, "T-accept-types: an unquoted `no` (read as False) is refused")
        except ValueError:
            ok(True, "T-accept-types: an unquoted `no` (read as False) is refused")
    try:
        terms = load_acceptance_language()["terms"]
    except Exception as e:  # noqa: BLE001
        terms = [str(e)]
    ok(any(t.get("id") == "accepted" for t in terms if isinstance(t, dict)),
       f"T-accept-manifest: the terms come from the manifest: {terms}")

    if failures:
        print(f"FAIL: {len(failures)} case(s)", file=sys.stderr)
        for f_ in failures:
            print(f"  - {f_}", file=sys.stderr)
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

    result = writer_qa(args.run_dir)
    out_path = write_report(args.run_dir, result)

    if args.json:
        print(json.dumps({
            "verdict": result.verdict,
            "n_major": result.n_major,
            "n_minor": result.n_minor,
            "issues": [i.to_dict() for i in result.issues],
            "report_path": out_path,
        }, indent=2))
    else:
        print(f"verdict: {result.verdict}", file=sys.stderr)
        print(f"  major issues: {result.n_major}", file=sys.stderr)
        print(f"  minor issues: {result.n_minor}", file=sys.stderr)
        for i in result.issues:
            print(f"  [{i.severity}] {i.kind}: {i.description[:120]}",
                  file=sys.stderr)
        print(f"report written: {out_path}", file=sys.stderr)

    return 0 if result.verdict == "CLEAN" else 1


if __name__ == "__main__":
    sys.exit(main())
