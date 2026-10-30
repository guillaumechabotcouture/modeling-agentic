#!/usr/bin/env python3
"""Cross-comparator efficiency outlier (STAGE 7 gate check, Phase 4 δ / 5 ε).

    python3 scripts/comparator_efficiency.py <run_dir>
    python3 scripts/comparator_efficiency.py --self-test

Phase 46 β moved this check out of scripts/validate_critique_yaml.py
unchanged. `gate_findings(run_dir)` reads report.md's comparison tables and
emits MEDIUM `comparator_efficiency_outlier` when this work's per-dollar
efficiency is far above the best comparator's (or far below the worst). The
validator runs it with `run_guarded` under the name
`_check_comparator_efficiency`, at the same place in its check order.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from typing import Optional

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:   # loaded by the validator, scripts/ is already there
    sys.path.insert(0, HERE)
from lib.md_helpers import (  # noqa: E402
    _HORIZON_RE, _extract_last_numeric, _parse_md_tables)

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
    # Phase 21 β: named comparator frameworks across disease areas (the
    # author-year alternative above covers everything else).
    r"\b(?:EMOD|OpenMalaria|Optima|Spectrum|GBD|IHME|LiST|Starsim|"
    r"Covasim|EpiModel)\b|"
    r"\b(?:Published|Benchmark|Comparator|Reference|Literature)\s+"
    r"(?:Value|Estimate|Result|Finding)\b",
    re.IGNORECASE,
)


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


def gate_findings(run_dir: str) -> list[dict]:
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


def _self_test() -> int:
    failures: list[str] = []

    def ok(cond: bool, label: str) -> None:
        if not cond:
            failures.append(label)

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
        vd1 = gate_findings(d)
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
        vd2 = gate_findings(d)
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
        vd3 = gate_findings(d)
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
        vd4 = gate_findings(d)
        ok(any(v["kind"] == "comparator_efficiency_outlier" for v in vd4),
           f"D4: cost-per-death row 50% cheaper should fire, got {vd4}")

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
        ve2 = gate_findings(d)
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
        ve3 = gate_findings(d)
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
        ve4 = gate_findings(d)
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
        ve5 = gate_findings(d)
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
        ve6 = gate_findings(d)
        ok(any(v["kind"] == "comparator_efficiency_outlier" for v in ve6),
           f"E6: single-comparator 4x ratio should fire (>3x threshold), "
           f"got {ve6}")

    if failures:
        print(f"FAIL: {len(failures)} case(s)", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    print("OK: all self-test cases passed.", file=sys.stderr)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_dir", nargs="?")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)
    if a.self_test:
        return _self_test()
    if not a.run_dir:
        ap.error("run_dir required")
    v = gate_findings(a.run_dir)
    for x in v:
        print(f"[{x['severity']}] {x['kind']}: {x['claim'][:300]}")
    return 1 if v else 0


if __name__ == "__main__":
    sys.exit(main())
