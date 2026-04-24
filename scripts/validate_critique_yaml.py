#!/usr/bin/env python3
"""
Validate and evaluate critique YAML files for the STAGE 7 decision gate.

Reads critique_methods.yaml, critique_domain.yaml, critique_presentation.yaml
from a run directory. Validates them against the schema defined in the
`critique-blockers-schema` skill. Computes:

    unresolved_high   -- HIGH-severity blockers with resolved=false, across
                         all three critiques
    structural        -- True if any critique has structural_mismatch.detected
    rounds_remaining  -- max_rounds - current_round

Then applies the fixed rule ordering from LEAD_SYSTEM_PROMPT STAGE 7 and
emits a decision: RETHINK_STRUCTURAL | RUN_FAILED | PATCH_OR_RETHINK |
DECLARE_SCOPE | ACCEPT.

Usage:
    python scripts/validate_critique_yaml.py <run_dir> \\
        --max-rounds 5 --current-round 2 [--json]

Exits non-zero on schema violations so the lead's Bash call surfaces the
error rather than swallowing it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

try:
    import yaml
except ImportError:
    print("ERROR: pyyaml not installed. Run: pip install pyyaml", file=sys.stderr)
    sys.exit(2)

# spec_compliance is a sibling module; import is lazy so validation without
# the --spec-compliance flag doesn't require it to be present.
_SPEC_COMPLIANCE_AVAILABLE = None  # tri-state: None=unchecked, True=ok, False=missing

def _load_spec_compliance():
    global _SPEC_COMPLIANCE_AVAILABLE
    if _SPEC_COMPLIANCE_AVAILABLE is False:
        return None
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import spec_compliance  # noqa: E402
        _SPEC_COMPLIANCE_AVAILABLE = True
        return spec_compliance
    except ImportError:
        _SPEC_COMPLIANCE_AVAILABLE = False
        return None


_REGISTRY_AVAILABLE = None

def _load_effect_size_registry():
    global _REGISTRY_AVAILABLE
    if _REGISTRY_AVAILABLE is False:
        return None
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import effect_size_registry  # noqa: E402
        _REGISTRY_AVAILABLE = True
        return effect_size_registry
    except ImportError:
        _REGISTRY_AVAILABLE = False
        return None


REVIEWERS = ("critique-methods", "critique-domain", "critique-presentation",
             "critique-redteam")
PREFIXES = {"critique-methods": "M-", "critique-domain": "D-",
            "critique-presentation": "P-", "critique-redteam": "R-"}
FILENAMES = {"critique-methods": "critique_methods.yaml",
             "critique-domain": "critique_domain.yaml",
             "critique-presentation": "critique_presentation.yaml",
             "critique-redteam": "critique_redteam.yaml"}

VALID_SEVERITY = {"HIGH", "MEDIUM", "LOW"}
VALID_VERDICT = {"PASS", "REVISE"}
VALID_CATEGORY = {"HARD_BLOCKER", "METHODS", "CAUSAL", "HYPOTHESES",
                  "CITATIONS", "PRESENTATION", "DATA", "STRUCTURAL"}
VALID_TARGET_STAGE = {"PLAN", "DATA", "MODEL", "ANALYZE", "WRITE"}


class SchemaError(Exception):
    pass


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise SchemaError(msg)


def _load(path: str) -> dict:
    try:
        with open(path) as f:
            doc = yaml.safe_load(f)
    except FileNotFoundError:
        raise SchemaError(f"missing file: {path}")
    except yaml.YAMLError as e:
        raise SchemaError(f"invalid YAML in {path}: {e}")
    _require(isinstance(doc, dict), f"{path}: top-level must be a mapping")
    return doc


def _validate_blocker(b: Any, ctx: str, prefix: str) -> None:
    _require(isinstance(b, dict), f"{ctx}: each blocker must be a mapping")
    for key in ("id", "severity", "category", "target_stage",
                "first_seen_round", "claim", "evidence", "fix_requires",
                "resolved"):
        _require(key in b, f"{ctx}: blocker missing required key '{key}'")
    _require(isinstance(b["id"], str) and b["id"].startswith(prefix),
             f"{ctx}: blocker id '{b.get('id')}' must start with '{prefix}'")
    _require(b["severity"] in VALID_SEVERITY,
             f"{ctx}: blocker {b['id']} severity must be one of {VALID_SEVERITY}")
    _require(b["category"] in VALID_CATEGORY,
             f"{ctx}: blocker {b['id']} category must be one of {VALID_CATEGORY}")
    _require(b["target_stage"] in VALID_TARGET_STAGE,
             f"{ctx}: blocker {b['id']} target_stage must be one of {VALID_TARGET_STAGE}")
    _require(isinstance(b["first_seen_round"], int) and b["first_seen_round"] >= 1,
             f"{ctx}: blocker {b['id']} first_seen_round must be int >= 1")
    _require(isinstance(b["resolved"], bool),
             f"{ctx}: blocker {b['id']} resolved must be bool")
    if b["resolved"]:
        for key in ("resolved_in_round", "resolved_evidence"):
            _require(key in b,
                     f"{ctx}: resolved blocker {b['id']} missing '{key}'")


def _validate_structural(sm: Any, ctx: str, reviewer: str) -> None:
    _require(isinstance(sm, dict),
             f"{ctx}: structural_mismatch must be a mapping")
    _require("detected" in sm and isinstance(sm["detected"], bool),
             f"{ctx}: structural_mismatch.detected must be bool")
    if reviewer == "critique-presentation":
        _require(sm["detected"] is False,
                 f"{ctx}: critique-presentation must not set structural_mismatch.detected=true; "
                 f"use a HIGH blocker instead")
    if sm["detected"]:
        for key in ("description", "evidence_files", "fix_requires"):
            _require(key in sm,
                     f"{ctx}: structural_mismatch.detected=true requires '{key}'")
        _require(sm["fix_requires"] == "RETHINK",
                 f"{ctx}: structural_mismatch.fix_requires must be 'RETHINK'")


def _validate_carried_forward(cf: Any, ctx: str, current_round: int,
                              blockers: list[dict]) -> None:
    _require(isinstance(cf, list),
             f"{ctx}: carried_forward must be a list")
    blocker_ids = {b["id"]: b for b in blockers}
    for entry in cf:
        _require(isinstance(entry, dict),
                 f"{ctx}: each carried_forward entry must be a mapping")
        for key in ("id", "prior_round", "still_present", "notes"):
            _require(key in entry,
                     f"{ctx}: carried_forward entry missing '{key}'")
        _require(isinstance(entry["still_present"], bool),
                 f"{ctx}: carried_forward.still_present must be bool")
        # cross-check with blockers
        bid = entry["id"]
        if entry["still_present"]:
            _require(bid in blocker_ids and blocker_ids[bid]["resolved"] is False,
                     f"{ctx}: carried_forward {bid} still_present=true, "
                     f"but no matching unresolved blocker in current blockers list")
        else:
            _require(bid in blocker_ids and blocker_ids[bid]["resolved"] is True,
                     f"{ctx}: carried_forward {bid} still_present=false, "
                     f"but no matching resolved blocker in current blockers list")


def validate_critique(path: str, expected_reviewer: str,
                      current_round: int) -> dict:
    doc = _load(path)
    ctx = os.path.basename(path)
    prefix = PREFIXES[expected_reviewer]

    _require(doc.get("reviewer") == expected_reviewer,
             f"{ctx}: reviewer must be '{expected_reviewer}', "
             f"got {doc.get('reviewer')!r}")
    _require(doc.get("round") == current_round,
             f"{ctx}: round must be {current_round}, got {doc.get('round')!r}")
    _require(doc.get("verdict") in VALID_VERDICT,
             f"{ctx}: verdict must be one of {VALID_VERDICT}")

    _validate_structural(doc.get("structural_mismatch"), ctx, expected_reviewer)

    blockers = doc.get("blockers", [])
    _require(isinstance(blockers, list), f"{ctx}: blockers must be a list")
    seen_ids = set()
    for b in blockers:
        _validate_blocker(b, ctx, prefix)
        _require(b["id"] not in seen_ids, f"{ctx}: duplicate blocker id {b['id']}")
        seen_ids.add(b["id"])

    if current_round == 1:
        _require(doc.get("carried_forward", []) == [],
                 f"{ctx}: carried_forward must be [] on round 1")
    else:
        _validate_carried_forward(doc.get("carried_forward"), ctx,
                                  current_round, blockers)

    return doc


def decide(critiques: dict[str, dict], max_rounds: int,
           current_round: int) -> dict:
    unresolved_high = []
    for reviewer, doc in critiques.items():
        for b in doc.get("blockers", []):
            if b["severity"] == "HIGH" and not b["resolved"]:
                unresolved_high.append({
                    "reviewer": reviewer,
                    "id": b["id"],
                    "category": b["category"],
                    "target_stage": b["target_stage"],
                    "first_seen_round": b["first_seen_round"],
                    "claim": b["claim"],
                })

    structural_reviewers = [r for r, d in critiques.items()
                            if d["structural_mismatch"]["detected"]]
    structural = bool(structural_reviewers)
    rounds_remaining = max_rounds - current_round

    # Rule ordering matches LEAD_SYSTEM_PROMPT STAGE 7.
    if structural:
        if rounds_remaining > 0:
            action = "RETHINK_STRUCTURAL"
            rationale = (
                f"Structural mismatch detected by {structural_reviewers}. "
                f"Must RETHINK — not patchable, not scope-declarable."
            )
        else:
            action = "RUN_FAILED"
            rationale = (
                f"Structural mismatch detected by {structural_reviewers} "
                f"with no rounds remaining. Run fails: delivered model does "
                f"not answer the question. Do NOT spawn writer."
            )
        rule_matched = 1
    elif unresolved_high and rounds_remaining > 0:
        action = "PATCH_OR_RETHINK"
        rationale = (
            f"{len(unresolved_high)} HIGH blocker(s) unresolved, "
            f"{rounds_remaining} round(s) remaining. "
            f"ACCEPT is forbidden."
        )
        rule_matched = 2
    elif unresolved_high and rounds_remaining <= 0:
        action = "DECLARE_SCOPE"
        rationale = (
            f"{len(unresolved_high)} HIGH blocker(s) unresolved, rounds "
            f"exhausted. Must write scope_declaration.yaml acknowledging "
            f"each blocker by id, and writer must embed verbatim in "
            f"Limitations. DECLARE_SCOPE is NOT the same as ACCEPT."
        )
        rule_matched = 3
    else:
        action = "ACCEPT"
        rationale = "No unresolved HIGH blockers, no structural mismatch."
        rule_matched = 4

    return {
        "unresolved_high": unresolved_high,
        "structural_mismatch": structural,
        "structural_reviewers": structural_reviewers,
        "rounds_remaining": rounds_remaining,
        "action": action,
        "rule_matched": rule_matched,
        "rationale": rationale,
        "spec_violations": [],
        "registry_violations": [],
        "rigor_violations": [],
    }


def _check_rigor_artifacts(run_dir: str) -> list[dict]:
    """Check for Phase 2 rigor artifacts. Returns a list of violations.

    Each rigor stage has a prerequisite + an artifact:
      UQ: outcome_fn.py (prereq) + uncertainty_report.yaml (artifact)
      Multi-structural: model_comparison.yaml (prereq) + model_comparison_formal.yaml (artifact)
      Identifiability: identifiability.yaml in models/ (prereq) + identifiability.yaml in run_dir (artifact)

    Missing artifact when prereq exists → HIGH blocker. If neither exists,
    the modeler didn't engage with that stage at all — SEPARATE MEDIUM
    blocker flagging the missing prerequisite.
    """
    violations = []

    # UQ: outcome_fn.py → uncertainty_report.yaml
    outcome_fn_path = os.path.join(run_dir, "models", "outcome_fn.py")
    uq_report_path = os.path.join(run_dir, "uncertainty_report.yaml")
    if os.path.exists(outcome_fn_path):
        if not os.path.exists(uq_report_path):
            violations.append({
                "kind": "uq_report_missing",
                "severity": "HIGH",
                "stage": "UQ",
                "claim": ("models/outcome_fn.py exists but "
                          "uncertainty_report.yaml is missing. Run "
                          "`python3 scripts/propagate_uncertainty.py {run_dir}` "
                          "to generate it. See uncertainty-quantification skill."),
            })
    else:
        violations.append({
            "kind": "outcome_fn_missing",
            "severity": "MEDIUM",
            "stage": "UQ",
            "claim": ("models/outcome_fn.py is absent — modeler did not expose "
                      "a deterministic outcome function for uncertainty "
                      "propagation. See uncertainty-quantification skill."),
        })

    # Multi-structural: models/model_comparison.yaml → model_comparison_formal.yaml
    msc_manifest = os.path.join(run_dir, "models", "model_comparison.yaml")
    msc_report = os.path.join(run_dir, "model_comparison_formal.yaml")
    if os.path.exists(msc_manifest):
        if not os.path.exists(msc_report):
            violations.append({
                "kind": "msc_report_missing",
                "severity": "HIGH",
                "stage": "MULTI_STRUCTURAL",
                "claim": ("models/model_comparison.yaml exists but "
                          "model_comparison_formal.yaml is missing. Run "
                          "`python3 scripts/compare_models.py {run_dir}`. "
                          "See multi-structural-comparison skill."),
            })
        else:
            # Additionally check the formal report for DEGENERATE_FIT_DETECTED
            try:
                with open(msc_report) as f:
                    formal = yaml.safe_load(f) or {}
                verdict = formal.get("verdict", "")
                if verdict == "DEGENERATE_FIT_DETECTED":
                    deg = formal.get("degenerate_fit", {})
                    violations.append({
                        "kind": "degenerate_fit",
                        "severity": "HIGH",
                        "stage": "MULTI_STRUCTURAL",
                        "claim": (f"compare_models flagged DEGENERATE FIT on "
                                  f"model {deg.get('model', '?')}: "
                                  f"{deg.get('reason', '(no reason given)')}"),
                    })
                elif verdict == "INSUFFICIENT_STRUCTURES":
                    violations.append({
                        "kind": "insufficient_structures",
                        "severity": "HIGH",
                        "stage": "MULTI_STRUCTURAL",
                        "claim": ("Modeler supplied fewer than 3 candidate "
                                  "structures for comparison. See "
                                  "multi-structural-comparison skill."),
                    })
            except (yaml.YAMLError, OSError):
                pass
    else:
        violations.append({
            "kind": "msc_manifest_missing",
            "severity": "MEDIUM",
            "stage": "MULTI_STRUCTURAL",
            "claim": ("models/model_comparison.yaml is absent — modeler did "
                      "not produce a multi-structural comparison. See "
                      "multi-structural-comparison skill."),
        })

    # Identifiability: models/identifiability.yaml → identifiability.yaml (run_dir)
    id_manifest = os.path.join(run_dir, "models", "identifiability.yaml")
    id_report = os.path.join(run_dir, "identifiability.yaml")
    if os.path.exists(id_manifest):
        if not os.path.exists(id_report):
            violations.append({
                "kind": "identifiability_report_missing",
                "severity": "HIGH",
                "stage": "IDENTIFIABILITY",
                "claim": ("models/identifiability.yaml exists but "
                          "identifiability.yaml is missing. Run "
                          "`python3 scripts/identifiability.py {run_dir}`. "
                          "See identifiability-analysis skill."),
            })
        else:
            try:
                with open(id_report) as f:
                    id_rep = yaml.safe_load(f) or {}
                verdict = id_rep.get("verdict", "")
                if verdict == "UNIDENTIFIED_PARAMETERS":
                    unidentified = [
                        name for name, p in id_rep.get("parameters", {}).items()
                        if p.get("status") == "unidentified"
                    ]
                    violations.append({
                        "kind": "unidentified_parameters",
                        "severity": "HIGH",
                        "stage": "IDENTIFIABILITY",
                        "claim": (f"identifiability analysis flagged "
                                  f"{len(unidentified)} ridge-trapped "
                                  f"parameter(s): {unidentified}. See "
                                  f"identifiability.yaml for profile-likelihood "
                                  f"details. Resolve via partial pooling, tied "
                                  f"parameters, or explicit scope declaration."),
                    })
            except (yaml.YAMLError, OSError):
                pass
    # NOTE: absence of identifiability.yaml with NO manifest is MEDIUM — many
    # models have no fitted parameters. Don't force this check universally.

    return violations


def _incorporate_rigor_violations(decision: dict, violations: list[dict],
                                  max_rounds: int, current_round: int) -> dict:
    """Fold rigor-artifact violations into unresolved_high (for HIGH) or
    attach for visibility (for MEDIUM). Uses RIG-NNN prefix."""
    d = dict(decision)
    d["rigor_violations"] = violations

    base_id = len([b for b in d["unresolved_high"]
                   if b.get("reviewer") == "rigor-artifacts"])
    for i, v in enumerate(
            [x for x in violations if x["severity"] == "HIGH"],
            start=base_id + 1):
        d["unresolved_high"].append({
            "reviewer": "rigor-artifacts",
            "id": f"RIG-{i:03d}",
            "category": "METHODS",
            "target_stage": "MODEL",
            "first_seen_round": current_round,
            "claim": f"{v['stage']}/{v['kind']}: {v['claim']}",
        })

    # Recompute action (same rule ordering).
    unresolved_high = d["unresolved_high"]
    structural = d.get("structural_mismatch", False)
    rounds_remaining = max_rounds - current_round

    if structural:
        action = "RETHINK_STRUCTURAL" if rounds_remaining > 0 else "RUN_FAILED"
        rule_matched = 1
    elif unresolved_high and rounds_remaining > 0:
        action = "PATCH_OR_RETHINK"
        rule_matched = 2
    elif unresolved_high and rounds_remaining <= 0:
        action = "DECLARE_SCOPE"
        rule_matched = 3
    else:
        action = "ACCEPT"
        rule_matched = 4

    d["action"] = action
    d["rule_matched"] = rule_matched
    if rule_matched in (1, 2):
        d["rationale"] = (
            f"{len(unresolved_high)} HIGH blocker(s) unresolved (incl. rigor), "
            f"{rounds_remaining} round(s) remaining. ACCEPT is forbidden."
        )
    return d


def incorporate_registry_violations(decision: dict, violations: list[dict],
                                    max_rounds: int, current_round: int) -> dict:
    """Fold effect-size-registry violations into the decision.

    HIGH violations (or_rr_conflation, registry_value_mismatch,
    cost_crosscheck_mismatch, param_not_in_code, param_frozen_in_uq)
    add synthetic `REG-NNN` blockers to `unresolved_high`. MEDIUM
    violations (registry_missing_ref, param_unregistered,
    subgroup_mismatch) are attached for visibility only.

    param_frozen_in_uq (Phase 3 Commit A2) is the mechanical R-022
    detector: registered parameter appears in code (e.g. as an
    UPPER_CASE constant in optimization.py) but is not referenced as
    params['NAME'] / params.get('NAME') in any UQ entry point, so its
    uncertainty is drawn from priors but never propagated through the
    outcome calculation.

    Registry violations do NOT force `structural_mismatch=True` — they are
    parameter-provenance issues, not architectural mismatches. A failed
    registry check at round N means the run cannot ACCEPT until the modeler
    patches the parameter or the registry entry.

    Idempotent: calling twice yields identical output.
    """
    d = dict(decision)
    d["registry_violations"] = violations

    base_id = len([b for b in d["unresolved_high"]
                   if b.get("reviewer") == "effect-size-registry"])
    for i, v in enumerate(
            [x for x in violations if x["severity"] == "HIGH"],
            start=base_id + 1):
        d["unresolved_high"].append({
            "reviewer": "effect-size-registry",
            "id": f"REG-{i:03d}",
            "category": "CITATIONS",
            "target_stage": "MODEL",
            "first_seen_round": current_round,
            "claim": f"{v['kind']} ({v['name']}): {v['claim']}",
        })

    # Recompute action using the same rule ordering. If spec-compliance has
    # also been run and set structural=True, the structural rule still fires
    # first; registry violations just add HIGHs underneath.
    unresolved_high = d["unresolved_high"]
    structural = d.get("structural_mismatch", False)
    rounds_remaining = max_rounds - current_round

    if structural:
        action = "RETHINK_STRUCTURAL" if rounds_remaining > 0 else "RUN_FAILED"
        rule_matched = 1
        rationale = d.get("rationale", "")
    elif unresolved_high and rounds_remaining > 0:
        action = "PATCH_OR_RETHINK"
        rationale = (
            f"{len(unresolved_high)} HIGH blocker(s) unresolved "
            f"(incl. registry), {rounds_remaining} round(s) remaining. "
            f"ACCEPT is forbidden."
        )
        rule_matched = 2
    elif unresolved_high and rounds_remaining <= 0:
        action = "DECLARE_SCOPE"
        rationale = (
            f"{len(unresolved_high)} HIGH blocker(s) unresolved, rounds "
            f"exhausted. Must write scope_declaration.yaml."
        )
        rule_matched = 3
    else:
        action = "ACCEPT"
        rationale = "No unresolved HIGH blockers, no structural mismatch."
        rule_matched = 4

    d["action"] = action
    d["rule_matched"] = rule_matched
    d["rationale"] = rationale
    return d


def incorporate_spec_violations(decision: dict, violations: list[dict],
                                max_rounds: int, current_round: int) -> dict:
    """Fold spec_compliance violations into the decision.

    Framework / approach HIGH violations force `structural_mismatch=True`
    (the mechanical backstop: critiques missed an architectural issue, so
    the gate itself catches it). Budget / archetype HIGH violations add
    synthetic `OBJ-NNN` blockers to `unresolved_high`. MEDIUM violations
    are attached for visibility but do not change the action.

    This function is idempotent: calling it twice yields identical output.
    """
    # Start from the existing decision; we'll mutate a shallow copy.
    d = dict(decision)
    d["spec_violations"] = violations
    structural_kinds = {"framework_missing", "approach_mismatch"}
    objective_kinds = {"budget_underutilized", "archetype_aggregation_unvalidated"}

    high_struct = [v for v in violations
                   if v["severity"] == "HIGH" and v["kind"] in structural_kinds]
    high_objective = [v for v in violations
                      if v["severity"] == "HIGH" and v["kind"] in objective_kinds]

    # Force structural_mismatch when any structural HIGH violation exists.
    if high_struct:
        d["structural_mismatch"] = True
        reviewers = list(d.get("structural_reviewers") or [])
        if "spec-compliance" not in reviewers:
            reviewers.append("spec-compliance")
        d["structural_reviewers"] = reviewers

    # Add synthetic blockers for objective HIGH violations.
    base_id = len([b for b in d["unresolved_high"]
                   if b.get("reviewer") == "spec-compliance"])
    for i, v in enumerate(high_objective, start=base_id + 1):
        d["unresolved_high"].append({
            "reviewer": "spec-compliance",
            "id": f"OBJ-{i:03d}",
            "category": "STRUCTURAL",  # re-route via STRUCTURAL so PATCH heuristic
                                        # escalates to RETHINK if it recurs.
            "target_stage": "MODEL",
            "first_seen_round": current_round,
            "claim": f"{v['kind']}: {v['evidence']}",
        })

    # Recompute action from the adjusted state (same rule ordering as decide()).
    unresolved_high = d["unresolved_high"]
    structural = d["structural_mismatch"]
    rounds_remaining = max_rounds - current_round

    if structural:
        if rounds_remaining > 0:
            action = "RETHINK_STRUCTURAL"
            rationale = (
                f"Structural mismatch detected by {d['structural_reviewers']}. "
                f"Must RETHINK — not patchable, not scope-declarable."
            )
        else:
            action = "RUN_FAILED"
            rationale = (
                f"Structural mismatch detected by {d['structural_reviewers']} "
                f"with no rounds remaining. Run fails: delivered model does "
                f"not answer the question. Do NOT spawn writer."
            )
        rule_matched = 1
    elif unresolved_high and rounds_remaining > 0:
        action = "PATCH_OR_RETHINK"
        rationale = (
            f"{len(unresolved_high)} HIGH blocker(s) unresolved "
            f"(incl. spec-compliance), {rounds_remaining} round(s) "
            f"remaining. ACCEPT is forbidden."
        )
        rule_matched = 2
    elif unresolved_high and rounds_remaining <= 0:
        action = "DECLARE_SCOPE"
        rationale = (
            f"{len(unresolved_high)} HIGH blocker(s) unresolved, rounds "
            f"exhausted. Must write scope_declaration.yaml acknowledging "
            f"each blocker by id, and writer must embed verbatim in "
            f"Limitations. DECLARE_SCOPE is NOT the same as ACCEPT."
        )
        rule_matched = 3
    else:
        action = "ACCEPT"
        rationale = "No unresolved HIGH blockers, no structural mismatch."
        rule_matched = 4

    d["action"] = action
    d["rule_matched"] = rule_matched
    d["rationale"] = rationale
    return d


def render_text(decision: dict, current_round: int, max_rounds: int) -> str:
    lines = [
        f"STAGE 7 decision (round {current_round}/{max_rounds})",
        f"  unresolved_high: {len(decision['unresolved_high'])} blocker(s)",
    ]
    for b in decision["unresolved_high"]:
        lines.append(f"    - {b['reviewer']} {b['id']} "
                     f"(target={b['target_stage']}, "
                     f"since round {b['first_seen_round']}): {b['claim'][:100]}")
    lines.append(f"  structural_mismatch: {decision['structural_mismatch']}"
                 + (f" (from: {decision['structural_reviewers']})"
                    if decision["structural_mismatch"] else ""))
    spec = decision.get("spec_violations") or []
    if spec:
        lines.append(f"  spec_violations: {len(spec)}")
        for v in spec:
            lines.append(f"    - [{v['severity']}] {v['kind']}: "
                         f"{v['evidence'][:120]}")
    reg = decision.get("registry_violations") or []
    if reg:
        lines.append(f"  registry_violations: {len(reg)}")
        for v in reg:
            lines.append(f"    - [{v['severity']}] {v['kind']} "
                         f"({v['name']}): {v['claim'][:120]}")
    rig = decision.get("rigor_violations") or []
    if rig:
        lines.append(f"  rigor_violations: {len(rig)}")
        for v in rig:
            lines.append(f"    - [{v['severity']}] {v['stage']}/{v['kind']}: "
                         f"{v['claim'][:120]}")
    lines.append(f"  rounds_remaining: {decision['rounds_remaining']}")
    lines.append(f"  rule_matched: {decision['rule_matched']}")
    lines.append(f"  action: {decision['action']}")
    lines.append(f"  rationale: {decision['rationale']}")
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("run_dir", help="Run directory containing critique_*.yaml")
    p.add_argument("--max-rounds", type=int, required=True)
    p.add_argument("--current-round", type=int, required=True)
    p.add_argument("--json", action="store_true",
                   help="Emit machine-readable JSON to stdout (in addition "
                        "to human summary on stderr)")
    p.add_argument("--spec-compliance", action="store_true",
                   help="Run spec-compliance checks (framework/approach/"
                        "budget/archetype) against the research question "
                        "in metadata.json and fold HIGH violations into "
                        "the gate decision. See scripts/spec_compliance.py.")
    p.add_argument("--parameter-registry", action="store_true",
                   help="Run effect-size-registry checks against the "
                        "`## Parameter Registry` section of citations.md "
                        "and fold HIGH violations (OR/RR conflation, "
                        "value mismatch, cost crosscheck) into the gate "
                        "decision. See scripts/effect_size_registry.py.")
    p.add_argument("--repo-root", default=None,
                   help="Repo root for resolving code_refs "
                        "(defaults to current working directory)")
    p.add_argument("--rigor-artifacts", action="store_true",
                   help="Check for Phase 2 rigor artifacts: "
                        "uncertainty_report.yaml, model_comparison_formal.yaml, "
                        "identifiability.yaml. HIGH blocker if any is missing "
                        "when its prerequisites exist (outcome_fn.py, "
                        "model_comparison.yaml, identifiability.yaml). See the "
                        "uncertainty-quantification, multi-structural-comparison, "
                        "and identifiability-analysis skills.")
    args = p.parse_args()

    if not os.path.isdir(args.run_dir):
        print(f"ERROR: {args.run_dir} is not a directory", file=sys.stderr)
        return 2

    critiques = {}
    schema_errors = []
    for reviewer in REVIEWERS:
        path = os.path.join(args.run_dir, FILENAMES[reviewer])
        # critique-redteam is optional: runs predating Commit E won't have
        # critique_redteam.yaml. If the file is absent, skip without error.
        # If it's present, validate it normally. This is NOT a permission to
        # skip red-team going forward — the lead is still required to spawn
        # all four critique agents in STAGE 6.
        if reviewer == "critique-redteam" and not os.path.exists(path):
            continue
        try:
            critiques[reviewer] = validate_critique(path, reviewer,
                                                    args.current_round)
        except SchemaError as e:
            schema_errors.append(str(e))

    if schema_errors:
        print("SCHEMA ERRORS (fix these before proceeding):", file=sys.stderr)
        for e in schema_errors:
            print(f"  - {e}", file=sys.stderr)
        if args.json:
            print(json.dumps({"schema_errors": schema_errors}, indent=2))
        return 3

    decision = decide(critiques, args.max_rounds, args.current_round)

    if args.spec_compliance:
        spec_module = _load_spec_compliance()
        if spec_module is None:
            print("ERROR: --spec-compliance requested but scripts/"
                  "spec_compliance.py could not be imported.", file=sys.stderr)
            return 2
        meta_path = os.path.join(args.run_dir, "metadata.json")
        if not os.path.exists(meta_path):
            print(f"ERROR: --spec-compliance requires {meta_path} "
                  f"but it does not exist.", file=sys.stderr)
            return 2
        with open(meta_path) as f:
            meta = json.load(f)
        # Accept either 'question' (the canonical key written by main.py)
        # or 'research_question' (which some lead agents rewrite it to
        # while populating other metadata). Both carry the same value.
        question = meta.get("question") or meta.get("research_question") or ""
        if not question:
            print(f"ERROR: {meta_path} has no 'question' or "
                  f"'research_question' field; cannot run spec-compliance "
                  f"check.", file=sys.stderr)
            return 2
        required = spec_module.detect_required_spec(question)
        check_result = spec_module.check_spec_compliance(required, args.run_dir)
        decision = incorporate_spec_violations(
            decision, check_result["violations"],
            args.max_rounds, args.current_round,
        )

    if args.rigor_artifacts:
        rigor_violations = _check_rigor_artifacts(args.run_dir)
        if rigor_violations:
            decision = _incorporate_rigor_violations(
                decision, rigor_violations,
                args.max_rounds, args.current_round,
            )

    if args.parameter_registry:
        registry_module = _load_effect_size_registry()
        if registry_module is None:
            print("ERROR: --parameter-registry requested but "
                  "scripts/effect_size_registry.py could not be imported.",
                  file=sys.stderr)
            return 2
        citations_path = os.path.join(args.run_dir, "citations.md")
        if not os.path.exists(citations_path):
            print(f"ERROR: --parameter-registry requires {citations_path} "
                  f"but it does not exist.", file=sys.stderr)
            return 2
        repo_root = args.repo_root or os.getcwd()
        try:
            registry = registry_module.load_priors(citations_path)
        except (ValueError, FileNotFoundError) as e:
            print(f"ERROR loading registry: {e}", file=sys.stderr)
            return 2
        reg_result = registry_module.check_registry(
            registry, repo_root, run_dir=args.run_dir)
        decision = incorporate_registry_violations(
            decision, reg_result["violations"],
            args.max_rounds, args.current_round,
        )

    print(render_text(decision, args.current_round, args.max_rounds),
          file=sys.stderr)
    if args.json:
        print(json.dumps(decision, indent=2))
    # Exit 0 on ACCEPT, 1 on any other action, so Bash callers can branch
    # easily. Schema errors already returned 3 above.
    return 0 if decision["action"] == "ACCEPT" else 1


if __name__ == "__main__":
    sys.exit(main())
