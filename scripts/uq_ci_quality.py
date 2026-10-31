#!/usr/bin/env python3
"""Uncertainty-report interval quality (STAGE 7 gate check).

    python3 scripts/uq_ci_quality.py <run_dir>
    python3 scripts/uq_ci_quality.py --self-test

Phase 46 β moved this check out of scripts/validate_critique_yaml.py
unchanged (Phase 4 α, Phase 21 β, Phase 45 α A11-A13). The validator calls
`check(<run>/uncertainty_report.yaml)` from `_check_core_rigor_reports`, after
the uq_report_missing test and before the producer-stamp test, so the order of
findings is what it was. `gate_findings(run_dir)` is the same check on the
run's uncertainty report (none when the report is absent: the core-reports
check owns uq_report_missing).

Findings: ci_degenerate (HIGH, per output family, not scope-declarable),
uq_declaration_contradicted (MEDIUM), uq_mean_outside_ci (MEDIUM),
uq_output_nonfinite (HIGH), uncertainty_report_invalid (HIGH),
uq_declarations_invalid (HIGH). See `check` for the rules.
"""
from __future__ import annotations

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:   # loaded by the validator, scripts/ is already there
    sys.path.insert(0, HERE)
from lib.rigor_artifacts import produced_path  # noqa: E402
from lib.safe_io import finite_number, safe_load_mapping  # noqa: E402

# Phase 4 Commit α: zero-width CI detector.
# Threshold rationale: 0.5% relative width catches optimizer-bounded
# outputs (e.g., total_cost when greedy fills budget) and hardcoded
# calibration targets (CI ~ 1e-7 of the target value). Looser would miss
# them; tighter would flag legitimately well-constrained outputs.
_CI_DEGENERATE_THRESHOLD = 0.005
# Phase 45 α (A11): variants named in one family finding before "+N more".
_UQ_FAMILY_LIST_CAP = 6


def _uq_invalid_declaration(claim: str) -> dict:
    return {"kind": "uq_declarations_invalid", "severity": "HIGH",
            "stage": "UQ", "scope_declarable": False,
            "claim": f"models/uq_declarations.yaml: {claim}"}


def _load_uq_declarations(run_dir: str) -> tuple[
        dict[str, str], dict[str, tuple[str, float]], list[dict]]:
    """Phase 21 β / Phase 45 α: outputs the modeler declares fixed by
    design, read from `models/uq_declarations.yaml`:

        deterministic_outputs:        # output name → reason
          planned_cost: "Fixed by the plan; no sampled input enters it."
        pinned_by_constraint:         # output name or family → constraint
          total_cost:
            constraint: budget_envelope
            value: 320000000

    Returns (deterministic, pinned, findings). A `deterministic_outputs`
    entry without a non-empty reason is ignored (the output then flags).
    A `pinned_by_constraint` entry needs a non-empty `constraint` and a
    finite, non-zero `value`; it exempts an output only when the output's
    mean and 95% bounds sit at that value (see `_uq_pinned_at`), so the
    reason alone never exempts. An output pinned at zero is not held there
    by a constraint — declare it deterministic with a reason, or report it
    as a rare event. Fails closed: an unparseable file, a section that is
    not a mapping, or a malformed pinned entry is HIGH
    `uq_declarations_invalid` and the entry exempts nothing.
    """
    deterministic: dict[str, str] = {}
    pinned: dict[str, tuple[str, float]] = {}
    path = os.path.join(run_dir, "models", "uq_declarations.yaml")
    doc, findings = safe_load_mapping(path, "uq_declarations", stage="UQ",
                                      display_path="models/uq_declarations.yaml")
    findings = [{**f, "scope_declarable": False} for f in findings]
    if not doc:
        return deterministic, pinned, findings
    decl = doc.get("deterministic_outputs")
    if isinstance(decl, dict):
        for k, v in decl.items():
            if isinstance(v, str) and v.strip():
                deterministic[str(k)] = v.strip()
    elif decl is not None:
        findings.append(_uq_invalid_declaration(
            f"`deterministic_outputs` must map output → reason, got "
            f"{type(decl).__name__}."))
    pins = doc.get("pinned_by_constraint")
    if isinstance(pins, dict):
        for k, v in pins.items():
            spec = v if isinstance(v, dict) else {}
            name = str(spec.get("constraint") or "").strip()
            value = finite_number(spec.get("value"))
            if not name or value is None or value == 0:
                findings.append(_uq_invalid_declaration(
                    f"`pinned_by_constraint.{k}` needs `constraint: <name>` "
                    f"and a finite non-zero `value:` (the number the "
                    f"constraint fixes the output at), got {v!r}. The entry "
                    f"exempts nothing."))
                continue
            pinned[str(k)] = (name, value)
    elif pins is not None:
        findings.append(_uq_invalid_declaration(
            f"`pinned_by_constraint` must map output or family → "
            f"{{constraint, value}}, got {type(pins).__name__}."))
    return deterministic, pinned, findings


def _uq_pinned_at(value: float, *points: float) -> bool:
    """Every point (mean, ci_low, ci_high) within the degeneracy threshold
    of the declared constraint value, relative to the value."""
    return all(abs(x - value) <= _CI_DEGENERATE_THRESHOLD * abs(value)
               for x in points)


def _uq_constant(mean: float, lo: float, hi: float) -> bool:
    """Phase 45 α: mean and both 95% bounds equal within the degeneracy
    threshold of the mean (absolute 1e-6 floor near zero) — what a
    `deterministic_outputs` declaration must be true of to exempt."""
    tol = max(1e-6, _CI_DEGENERATE_THRESHOLD * abs(mean))
    return abs(lo - mean) <= tol and abs(hi - mean) <= tol


def _uq_output_families(names: list[str]) -> dict[str, str]:
    """Phase 45 α (A11): output name → family name, so one defect repeated
    across plan variants is one finding. The family is the name after the
    last `__` (`planA__total_cost` → `total_cost`); a name without `__` is
    its own family. Nothing else is inferred: `alt_total_cost` and
    `total_cost` stay separate families."""
    out = {}
    for n in names:
        prefix, sep, base = n.rpartition("__")
        out[n] = base if sep and base and prefix else n
    return out


def _uq_family_list(outputs: list[str]) -> str:
    shown = ", ".join(outputs[:_UQ_FAMILY_LIST_CAP])
    more = len(outputs) - _UQ_FAMILY_LIST_CAP
    return shown + (f", +{more} more" if more > 0 else "")


def check(uq_report_path: str) -> list[dict]:
    """Phase 4 Commit α / Phase 21 β / Phase 45 α: scan
    uncertainty_report.yaml for degenerate and non-finite intervals.

    `ci_degenerate` fires when an output's 95% CI relative width
    (ci_high - ci_low) / |mean| is below 0.5% (or the width is exactly 0)
    while more than one draw was run. Phase 21 β raised it from MEDIUM to
    HIGH and made it NOT scope-declarable: 2026-05-03_235631 shipped 9/9
    zero-width outputs as MEDIUM while the report printed a wide CI for
    the same quantity. An output that is deterministic by design is
    exempted only by an explicit declaration (`_load_uq_declarations`):
    `deterministic_outputs:` with a reason, or per-output `deterministic:
    true` + `deterministic_reason:` — exempt only while the summary is
    constant (`_uq_constant`, and p_nonzero, if reported, is 0 or 1), else
    MEDIUM `uq_declaration_contradicted` and the usual rules apply — or
    `pinned_by_constraint:` whose stated value the output's mean and
    bounds actually sit at.

    Phase 45 α: findings are grouped per output family
    (`_uq_output_families`, the name after the last `__`) — one
    `ci_degenerate` per family, listing its degenerate variants. Inverted
    bounds are degenerate before any exemption. The rare-event fields
    propagate_uncertainty.py writes (`p_nonzero` ...) never relax the
    width rule: summary fields cannot show that an event probability is
    well estimated, which needs the per-draw values. A degenerate output
    with 0 < p_nonzero < 1 stays HIGH, and its claim says to quote
    P(output > 0) with its SE instead of a quantile interval.
    `uq_mean_outside_ci` (MEDIUM, per family) fires when a mean lies
    outside its own [ci_low, ci_high] — a summary that misreports a
    zero-inflated output.

    `uq_output_nonfinite` (HIGH) fires on NaN/inf mean or bounds; an
    unparseable / non-mapping report is `uncertainty_report_invalid` (HIGH).
    """
    report, violations = safe_load_mapping(
        uq_report_path, "uncertainty_report", stage="UQ",
        required_keys=["scalar_outputs"],
        expected_types={"scalar_outputs": dict},
        display_path="uncertainty_report.yaml")
    if report is None:
        return violations

    run_dir = os.path.dirname(uq_report_path)
    declared, pinned, decl_findings = _load_uq_declarations(run_dir)
    violations.extend(decl_findings)
    n_draws = finite_number(report.get("n_successful"))
    if n_draws is None:
        n_draws = finite_number(report.get("n_draws"))
    multi_draw = n_draws is None or n_draws > 1
    n_txt = int(n_draws) if n_draws is not None else "?"
    # Phase 21 δ: propagate_uncertainty.py now lists degenerate outputs
    # itself; trust-but-verify — honor its list AND keep the width check
    # for reports that predate the field.
    flagged = report.get("degenerate_outputs")
    flagged = {str(x) for x in flagged} if isinstance(flagged, list) else set()
    family_of = _uq_output_families([str(k) for k in report["scalar_outputs"]])
    degenerate_by_family: dict[str, list[tuple[str, str]]] = {}
    outside_by_family: dict[str, list[tuple[str, str]]] = {}
    contradicted_by_family: dict[str, list[tuple[str, str]]] = {}

    for output_name, stats in report["scalar_outputs"].items():
        output_name = str(output_name)
        family = family_of[output_name]
        if not isinstance(stats, dict):
            violations.append({
                "kind": "uncertainty_report_invalid",
                "severity": "HIGH",
                "stage": "UQ",
                "scope_declarable": False,
                "claim": (f"scalar_outputs.{output_name} must be a mapping "
                          f"with mean/ci_low/ci_high, got "
                          f"{type(stats).__name__}."),
            })
            continue
        raw = {k: stats.get(k) for k in ("mean", "ci_low", "ci_high")}
        if any(v is None for v in raw.values()):
            violations.append({
                "kind": "uncertainty_report_invalid",
                "severity": "HIGH",
                "stage": "UQ",
                "scope_declarable": False,
                "claim": (f"scalar_outputs.{output_name} is missing "
                          f"{[k for k, v in raw.items() if v is None]}."),
            })
            continue
        vals = {k: finite_number(v) for k, v in raw.items()}
        if any(v is None for v in vals.values()):
            violations.append({
                "kind": "uq_output_nonfinite",
                "severity": "HIGH",
                "stage": "UQ",
                "scope_declarable": False,
                "claim": (f"{output_name}: non-finite/non-numeric UQ "
                          f"summary {raw}. NaN draws must be excluded and "
                          f"counted as failures, not summarised."),
            })
            continue
        mean_f, lo_f, hi_f = vals["mean"], vals["ci_low"], vals["ci_high"]
        # Phase 45 α (A12): p_nonzero only shapes the message (and, below,
        # keeps a deterministic declaration from exempting a varying
        # output); it never exempts. A value that is not a probability is
        # ignored.
        p = finite_number(stats.get("p_nonzero"))
        p = p if p is not None and 0 <= p <= 1 else None
        rare = p is not None and 0 < p < 1
        # Phase 45 α (A12): a mean outside its own interval is a reporting
        # defect for any output (inverted bounds are ci_degenerate below).
        # The absolute floor absorbs float residue (a mean of 2e-9 over
        # all-zero bounds is a summation artefact, not a reporting defect).
        eps = max(1e-6, 1e-9 * max(abs(lo_f), abs(hi_f)))
        if lo_f <= hi_f and not lo_f - eps <= mean_f <= hi_f + eps:
            outside_by_family.setdefault(family, []).append((output_name, (
                f"{output_name} mean {mean_f:.6g} vs 95% CI "
                f"[{lo_f:.6g}, {hi_f:.6g}]"
                + (f", p_nonzero {p:.3g}" if rare else ""))))
        if not multi_draw:
            continue  # effort floors own the n_draws minimum
        if lo_f > hi_f:
            # Inverted bounds are a broken summary: no declaration or pin
            # can exempt them.
            degenerate_by_family.setdefault(family, []).append((output_name, (
                f"{output_name} 95% CI [{lo_f:.6g}, {hi_f:.6g}] is inverted "
                f"(ci_low > ci_high)")))
            continue
        if output_name in declared or (
                stats.get("deterministic") is True
                and str(stats.get("deterministic_reason") or "").strip()):
            # Phase 45 α: a deterministic declaration exempts only an output
            # whose summary is constant; one the draws contradict (attempt 8
            # declared rare-event cuts "deterministic") is judged as usual.
            if _uq_constant(mean_f, lo_f, hi_f) and p in (None, 0.0, 1.0):
                continue
            contradicted_by_family.setdefault(family, []).append((output_name, (
                f"{output_name} mean {mean_f:.6g}, 95% CI "
                f"[{lo_f:.6g}, {hi_f:.6g}]"
                + (f", p_nonzero {p:.3g}" if p is not None else ""))))
        # Exact output name, or the exact `__` family base the declaration
        # names; never a family inferred any other way.
        pin = pinned.get(output_name) or pinned.get(family)
        pin_note = ""
        if pin is not None:
            if _uq_pinned_at(pin[1], mean_f, lo_f, hi_f):
                continue
            pin_note = (f"; declared pinned_by_constraint {pin[0]} = "
                        f"{pin[1]:.6g}, but the output does not sit at it")
        width = hi_f - lo_f
        if abs(mean_f) < 1e-9:
            degenerate = width <= 0
            rel_txt = "zero width"
        else:
            relative_width = width / abs(mean_f)
            degenerate = width <= 0 or relative_width < _CI_DEGENERATE_THRESHOLD
            rel_txt = f"{relative_width * 100:.3f}% of mean ({mean_f:.6g})"
        if output_name in flagged or stats.get("degenerate") is True:
            degenerate = True
            diag = str(stats.get("degenerate_diagnostic") or "").strip()
            if diag:
                rel_txt += f"; producer diagnostic: {diag}"
        if degenerate and rare:
            n_out = finite_number(stats.get("n")) or n_draws
            se_txt = (f", SE {(p * (1 - p) / n_out) ** 0.5:.2g}"
                      if n_out and n_out > 0 else "")
            rel_txt += (f". It is a rare event (P(output > 0) = {p:.3g}"
                        f"{se_txt}): the report and prose must quote "
                        f"P(output > 0) with its SE, and the size when it "
                        f"occurs, instead of a quantile interval; it cannot "
                        f"be declared away")
        if degenerate:
            degenerate_by_family.setdefault(family, []).append((output_name, (
                f"{output_name} 95% CI [{lo_f:.6g}, {hi_f:.6g}] is "
                f"{rel_txt}{pin_note}")))

    for family, items in degenerate_by_family.items():
        outputs = [n for n, _ in items]
        example = next((d for _, d in items if "pinned_by_constraint" in d),
                       items[0][1])
        violations.append({
            "kind": "ci_degenerate",
            "severity": "HIGH",
            "stage": "UQ",
            "scope_declarable": False,
            "family": family,
            "outputs": outputs,
            "claim": (
                f"{family}: degenerate 95% CI in {len(outputs)} output(s) "
                f"after {n_txt} draws ({_uq_family_list(outputs)}). "
                f"Example: {example}. "
                f"Either the sampled parameters never reach this "
                f"output (frozen in outcome_fn, surrogate flattening, "
                f"parameters not wired through), or it is "
                f"deterministic by design — then declare it in "
                f"models/uq_declarations.yaml (`deterministic_outputs:` "
                f"with a reason, or `pinned_by_constraint:` with the "
                f"binding constraint and the value it fixes) and report "
                f"it as a point estimate. Any interval quoted elsewhere "
                f"for this output is unsupported."
            ),
        })
    for family, items in contradicted_by_family.items():
        outputs = [n for n, _ in items]
        violations.append({
            "kind": "uq_declaration_contradicted",
            "severity": "MEDIUM",
            "stage": "UQ",
            "family": family,
            "outputs": outputs,
            "claim": (
                f"{family}: declared deterministic in "
                f"models/uq_declarations.yaml, but the draws vary in "
                f"{len(outputs)} output(s) ({_uq_family_list(outputs)}). "
                f"Example: {items[0][1]}. The declaration does not exempt "
                f"them; they are judged by the width rule. "
                f"Remove the declaration, and report an output that is "
                f"zero in most draws by its p_nonzero and mean_if_nonzero."
            ),
        })
    for family, items in outside_by_family.items():
        outputs = [n for n, _ in items]
        violations.append({
            "kind": "uq_mean_outside_ci",
            "severity": "MEDIUM",
            "stage": "UQ",
            "family": family,
            "outputs": outputs,
            "claim": (
                f"{family}: the mean lies outside its own 95% CI in "
                f"{len(outputs)} output(s) ({_uq_family_list(outputs)}). "
                f"Example: {items[0][1]}. Quoting mean [ci_low, ci_high] "
                f"for such an output misreports it. For an output that is "
                f"zero in most draws, report its p_nonzero and "
                f"mean_if_nonzero from uncertainty_report.yaml instead."
            ),
        })
    return violations


def gate_findings(run_dir: str) -> list[dict]:
    """`check` on the run's uncertainty_report.yaml; [] when it is absent."""
    path = produced_path("outcome_fn", run_dir)
    return check(path) if os.path.exists(path) else []



def _self_test() -> int:
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
        va = check(uq_a)
        ok(any(v["kind"] == "ci_degenerate" for v in va),
           f"A1: expected ci_degenerate on optimizer-bounded cost, got {va}")
        # Phase 21 β: raised to HIGH, not scope-declarable.
        ok(all(v["severity"] == "HIGH" and v.get("scope_declarable") is False
               for v in va),
           f"A1: ci_degenerate must be HIGH non-declarable, got {va}")
        # A1b: explicit deterministic declaration with a reason → silent.
        os.makedirs(os.path.join(d, "models"), exist_ok=True)
        with open(os.path.join(d, "models", "uq_declarations.yaml"), "w") as f:
            f.write("deterministic_outputs:\n"
                    "  total_cost_3yr: 'Budget constraint binds.'\n")
        ok(check(uq_a) == [],
           "A1b: declared deterministic output must be silent")
        os.remove(os.path.join(d, "models", "uq_declarations.yaml"))

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
        vb = check(uq_b)
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
        vc = check(uq_c)
        # Phase 21 β: an exactly-zero-width interval is degenerate even at
        # mean 0; a zero-mean output with real spread is not.
        ok(any(v["kind"] == "ci_degenerate" for v in vc),
           f"A3: zero-mean zero-width output must fire, got {vc}")
        uq_c2 = os.path.join(d, "uq_c2.yaml")
        with open(uq_c2, "w") as f:
            f.write("scalar_outputs:\n  net_change:\n    mean: 0\n"
                    "    ci_low: -5\n    ci_high: 5\n")
        ok(check(uq_c2) == [],
           "A3b: zero-mean output with spread is silent")

        # Phase 21 β cases A5-A9.
        def _uq(name, text):
            pth = os.path.join(d, name)
            with open(pth, "w") as f:
                f.write(text)
            return check(pth)
        v5 = _uq("uq5.yaml", "scalar_outputs:\n  x:\n    mean: .nan\n"
                             "    ci_low: 1\n    ci_high: 2\n")
        ok([v["kind"] for v in v5] == ["uq_output_nonfinite"],
           f"A5: NaN mean → uq_output_nonfinite, got {v5}")
        v6 = _uq("uq6.yaml", "n_draws: 1\nscalar_outputs:\n  x:\n"
                             "    mean: 3\n    ci_low: 3\n    ci_high: 3\n")
        ok(v6 == [], f"A6: single draw → no degenerate finding, got {v6}")
        v7 = _uq("uq7.yaml", "- not a mapping\n")
        ok([v["kind"] for v in v7] == ["uncertainty_report_invalid"],
           f"A7: list top-level → invalid, got {v7}")
        v8 = _uq("uq8.yaml", "scalar_outputs:\n  x:\n    mean: 3\n"
                             "    ci_low: 3\n    ci_high: 3\n"
                             "    deterministic: true\n"
                             "    deterministic_reason: fixed by design\n")
        ok(v8 == [], f"A8: per-output deterministic + reason silent, got {v8}")
        v9 = _uq("uq9.yaml", "scalar_outputs:\n  x:\n    mean: 3\n"
                             "    ci_low: 3\n    ci_high: 3\n"
                             "    deterministic: true\n")
        ok(any(v["kind"] == "ci_degenerate" for v in v9),
           f"A9: deterministic without reason still fires, got {v9}")
        v10 = _uq("uq10.yaml", "degenerate_outputs: [x]\nscalar_outputs:\n"
                               "  x:\n    mean: 3\n    ci_low: 1\n"
                               "    ci_high: 5\n")
        ok(any(v["kind"] == "ci_degenerate" for v in v10),
           f"A10: producer-flagged degenerate output fires, got {v10}")

        # Phase 45 α (A11-A13): family grouping, rare events, pinned outputs.
        def _decl(text):
            os.makedirs(os.path.join(d, "models"), exist_ok=True)
            pth = os.path.join(d, "models", "uq_declarations.yaml")
            if text is None:
                if os.path.exists(pth):
                    os.remove(pth)
                return
            with open(pth, "w") as f:
                f.write(text)

        def _kinds(vs):
            return sorted(v["kind"] for v in vs)
        pinned_cost = ("    mean: 320000000\n    ci_low: 319987000\n"
                       "    ci_high: 320000000\n    n: 200\n")
        fam_report = ("n_successful: 200\nscalar_outputs:\n"
                      + "".join(f"  {n}:\n{pinned_cost}" for n in (
                          "total", "planA__total", "planB_alt__total",
                          "alt_total"))
                      + "  net_cost:\n    mean: 5\n    ci_low: 5\n"
                        "    ci_high: 5\n"
                        "  planA__overrun:\n    mean: 0\n    ci_low: 0\n"
                        "    ci_high: 0\n")
        _decl(None)
        g1 = _uq("uq_g1.yaml", fam_report)
        fams = {v["family"]: v["outputs"] for v in g1
                if v["kind"] == "ci_degenerate"}
        ok(fams == {"total": ["total", "planA__total", "planB_alt__total"],
                    "alt_total": ["alt_total"],
                    "net_cost": ["net_cost"], "overrun": ["planA__overrun"]}
           and all(v["severity"] == "HIGH" and v["scope_declarable"] is False
                   for v in g1),
           f"G1: one HIGH finding per `__` family; no leading-token "
           f"inference; got {fams}")
        ok(_uq_output_families(["cost", "net_cost", "plan_net__benefit"]) ==
           {"cost": "cost", "net_cost": "net_cost",
            "plan_net__benefit": "benefit"},
           "G2: net_cost never joins family cost")
        _decl("pinned_by_constraint:\n  cost:\n    constraint: budget\n"
              "    value: 10\n")
        g3 = _uq("uq_g3.yaml", "n_successful: 200\nscalar_outputs:\n"
                 "  cost:\n    mean: 10\n    ci_low: 10\n    ci_high: 10\n"
                 "  net_cost:\n    mean: 4\n    ci_low: 4\n    ci_high: 4\n"
                 "  plan_net__benefit:\n    mean: 1\n    ci_low: 0\n"
                 "    ci_high: 2\n")
        ok([v.get("family") for v in g3] == ["net_cost"],
           f"G3: a pin on cost does not exempt a frozen net_cost; got {g3}")
        _decl("pinned_by_constraint:\n  x:\n    constraint: budget\n"
              "    value: 100\n")
        i1 = _uq("uq_i1.yaml", "n_successful: 200\nscalar_outputs:\n"
                 "  x:\n    mean: 100\n    ci_low: 100.1\n    ci_high: 99.9\n"
                 "  y:\n    mean: 0.16\n    ci_low: 1\n    ci_high: 0\n"
                 "    n: 200\n    p_nonzero: 0.02\n")
        ok(sorted(v["family"] for v in i1 if v["kind"] == "ci_degenerate")
           == ["x", "y"] and all("inverted" in v["claim"] for v in i1
                                 if v["kind"] == "ci_degenerate"),
           f"I1: inverted bounds are HIGH even under a matching pin or "
           f"with p_nonzero; got {i1}")
        _decl(None)
        r1 = _uq("uq_r1.yaml", "n_successful: 200\nscalar_outputs:\n"
                 "  x:\n    mean: 100\n    ci_low: 100\n    ci_high: 100\n"
                 "    n: 200\n    p_nonzero: 0.2\n")
        ok(_kinds(r1) == ["ci_degenerate"] and "P(output > 0)" in r1[0]["claim"],
           f"R1: p_nonzero never relaxes the width rule; a degenerate rare "
           f"event stays HIGH and says to quote P(output > 0); got {r1}")
        z1 = _uq("uq_z1.yaml", "n_successful: 200\nscalar_outputs:\n"
                 "  cuts:\n    mean: 0.16\n    ci_low: 0\n    ci_high: 0\n"
                 "    n: 200\n    p_nonzero: 0.02\n")
        ok(_kinds(z1) == ["ci_degenerate", "uq_mean_outside_ci"]
           and all(v["severity"] == ("HIGH" if v["kind"] == "ci_degenerate"
                                     else "MEDIUM") for v in z1),
           f"Z1: zero-inflated output with CI [0, 0] → HIGH ci_degenerate + "
           f"MEDIUM uq_mean_outside_ci; got {z1}")
        z2 = _uq("uq_z2.yaml", "n_successful: 200\nscalar_outputs:\n"
                 "  cuts:\n    mean: 1.0\n    median: 2.0\n    ci_low: 0.0\n"
                 "    ci_high: 2.0\n    n: 200\n    rare_event: true\n"
                 "    p_nonzero: 0.5\n    p_nonzero_se: 0.0354\n"
                 "    mean_if_nonzero: 2.0\n")
        ok(z2 == [],
           f"Z2: producer output at exactly 50% zeros is not invalid; got {z2}")
        _decl("pinned_by_constraint:\n  total:\n    constraint: budget\n"
              "    value: 320000000\n")
        c1 = _uq("uq_g1.yaml", fam_report)
        ok({v.get("family") for v in c1} == {"net_cost", "overrun",
                                             "alt_total"},
           f"C1: family pinned at its stated constraint value is exempt; "
           f"overrun (downstream of the rule) still flags; got {c1}")
        _decl("pinned_by_constraint:\n  total:\n    constraint: budget\n"
              "    value: 300000000\n")
        c2 = _uq("uq_g1.yaml", fam_report)
        ok(any(v.get("family") == "total" and "does not sit at it" in v["claim"]
               for v in c2),
           f"C2: declared value the draws do not sit at → still flagged; "
           f"got {c2}")
        _decl("pinned_by_constraint:\n  total: 'budget binds'\n"
              "  net_cost:\n    constraint: cap\n    value: 0\n")
        c3 = _uq("uq_g1.yaml", fam_report)
        ok(_kinds(c3).count("uq_declarations_invalid") == 2
           and {v.get("family") for v in c3 if v["kind"] == "ci_degenerate"}
           == {"total", "alt_total", "net_cost", "overrun"},
           f"C3: free-text or zero-value pin is invalid and exempts "
           f"nothing; got {_kinds(c3)}")
        _decl("pinned_by_constraint: [total\n")
        c4 = _uq("uq_g1.yaml", fam_report)
        ok(any(v["kind"] == "uq_declarations_invalid" and v["severity"] == "HIGH"
               and v.get("scope_declarable") is False
               for v in c4)
           and sum(v["kind"] == "ci_degenerate" for v in c4) == 4,
           f"C4: unparseable declarations fail closed; got {_kinds(c4)}")
        _decl("deterministic_outputs:\n  fixed: 'set by the plan'\n"
              "  cuts: 'zero at both percentiles'\n  wide: 'deterministic'\n")
        d1 = _uq("uq_d1.yaml", "n_successful: 200\nscalar_outputs:\n"
                 "  fixed:\n    mean: 7\n    ci_low: 7\n    ci_high: 7\n"
                 "  cuts:\n    mean: 0.16\n    ci_low: 0\n    ci_high: 0\n"
                 "  wide:\n    mean: 1\n    ci_low: 0\n    ci_high: 5\n"
                 "    p_nonzero: 0.3\n")
        ok(_kinds(d1) == ["ci_degenerate", "uq_declaration_contradicted",
                          "uq_declaration_contradicted",
                          "uq_mean_outside_ci"]
           and {v["family"] for v in d1 if v["kind"] == "ci_degenerate"}
           == {"cuts"}
           and all(v["severity"] == "MEDIUM" for v in d1
                   if v["kind"] == "uq_declaration_contradicted"),
           f"D1: constant declared output exempt; declared outputs the "
           f"draws contradict are judged as usual + MEDIUM; got {d1}")
        _decl(None)

    with tempfile.TemporaryDirectory() as d:
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
        vd = check(uq_d)
        ok(any(v["kind"] == "ci_degenerate" for v in vd),
           f"A4: hardcoded calibration target should fire, got {vd}")

    if failures:
        print(f"FAIL: {len(failures)} case(s)", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    print("OK: all self-test cases passed.", file=sys.stderr)
    return 0


def main(argv=None) -> int:
    from lib import gate_cli
    return gate_cli.run(argv, __doc__, gate_findings, _self_test, fails=gate_cli.any_high)


if __name__ == "__main__":
    sys.exit(main())
