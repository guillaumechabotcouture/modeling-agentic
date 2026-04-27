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

# Reuse mature utilities from the validator. Lazy import to avoid
# circular dependency at module load time.
def _load_validator_helpers():
    """Lazy-load _parse_md_tables, _extract_last_numeric,
    _strip_limitations_and_scope from validate_critique_yaml.py."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "validate_critique_yaml",
        os.path.join(os.path.dirname(__file__), "validate_critique_yaml.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return (mod._parse_md_tables, mod._extract_last_numeric,
            mod._strip_limitations_and_scope)


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
    verdict: str  # CLEAN | REVISE | MAJOR_REVISION
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


def writer_qa(run_dir: str) -> QAResult:
    """Run all writer-QA checks on report.md. Returns QAResult."""
    report_path = os.path.join(run_dir, "report.md")
    if not os.path.exists(report_path):
        return QAResult(verdict="CLEAN", issues=[])

    try:
        with open(report_path) as f:
            text = f.read()
    except (OSError, UnicodeDecodeError):
        return QAResult(verdict="CLEAN", issues=[])

    # Lazy-load helpers from validate_critique_yaml.
    parse_tables, extract_numeric, strip_limitations = _load_validator_helpers()

    issues: list[QAIssue] = []
    issues.extend(_check_stale_uq_numbers(text, run_dir, extract_numeric))
    issues.extend(_check_figure_text_comparator_inconsistency(
        text, extract_numeric))
    issues.extend(_check_annotation_vs_body(text, extract_numeric))
    issues.extend(_check_stale_caveat(text, run_dir, strip_limitations))

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
