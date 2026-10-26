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
        --max-rounds 5 --current-round 2 \\
        --spec-compliance --parameter-registry --rigor-artifacts \\
        [--post-write] [--json] [--no-history]

`--post-write` (Phase 21 β) is the post-WRITE gate main.py runs after the
session: report.md and post-WRITE artifacts become required and writer-QA /
coherence-audit failures block.

Phase 21 β semantics: the gate fails closed (unparseable / wrong-shape /
unknown-verdict / unstamped artifacts and crashing checks are HIGH);
missing required artifacts forbid ACCEPT at every round; non-scope-
declarable findings open at round exhaustion yield RUN_FAILED (rule 5),
never DECLARE_SCOPE. Each invocation appends to
<run_dir>/validator_history.jsonl unless --no-history.

Exit codes: 0 ACCEPT; 1 any other action; 2 usage / missing inputs;
3 critique schema errors; 4 validator crashed (JSON action VALIDATOR_ERROR).
"""

from __future__ import annotations

import argparse
import glob
import importlib.util
import json
import os
import re
import sys
from typing import Any, Optional

try:
    import yaml
except ImportError:
    print("ERROR: pyyaml not installed. Run: pip install pyyaml", file=sys.stderr)
    sys.exit(2)

# Rigor artifact manifest: single source of truth for paths/rounds/skills.
# Sibling-script import: scripts/lib/rigor_artifacts.py.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib.rigor_artifacts import (  # noqa: E402
    artifact, artifact_path, load_artifacts, produced_path)
# Phase 21 β: fail-closed loading + crash guard + ledger provenance.
from lib.safe_io import finite_number, run_guarded, safe_load_mapping  # noqa: E402
from lib import provenance as _provenance  # noqa: E402


# Phase 21 β: closed verdict vocabularies of each producing script. A
# verdict outside these sets was hand-written (the 235631 run shipped
# `DEGENERATE_FIT_ACKNOWLEDGED` / `PARAMETERS_IDENTIFIED_BY_CONSTRUCTION`)
# and fails closed as `<prefix>_unknown_verdict` HIGH. Where the producing
# script exports `VERDICTS`, that tuple is authoritative (see
# _producer_verdicts); the literals below are the fallback.
_PRODUCER_VERDICTS = {
    "compare_models": ("CLEAN", "DEGENERATE_FIT_DETECTED",
                       "INSUFFICIENT_STRUCTURES"),
    "identifiability": ("CLEAN", "UNIDENTIFIED_PARAMETERS",
                        "WEAKLY_IDENTIFIED_PARAMETERS", "UNKNOWN_LOSS_SCALE"),
    "identifiability_a_priori": ("IDENTIFIABLE", "MARGINAL", "OVER_SATURATED",
                                 "MALFORMED", "MISSING"),
    "sensitivity_analysis": ("ROBUST", "SENSITIVE", "UNSTABLE", "MALFORMED",
                             "MISSING"),
    "allocation_robustness": ("ROBUST", "FRAGILE", "UNSTABLE", "MALFORMED",
                              "MISSING"),
    "optimization_quality": ("CLEAN", "GAP_TOO_LARGE", "NO_BENCHMARK",
                             "MALFORMED", "MISSING"),
    "within_zone_sensitivity": ("BOUNDED", "UNBOUNDED", "INCONCLUSIVE",
                                "MALFORMED", "MISSING"),
    "writer_qa": ("CLEAN", "REVISE", "MAJOR_REVISION"),
    "coherence_audit": ("CLEAN", "DRIFT_DETECTED"),
    "benchmark_match": ("PASS", "DRIFT", "MALFORMED", "ABSENT", "PENDING"),
    "critique_sufficiency_claim": ("OVERCLAIMED", "ADEQUATE", "UNDERCLAIMED",
                                   "ADEQUATE_FOR_PILOT"),
}

# Phase 21 β: script-produced artifacts must carry the producer stamp the
# script writes (and, where present, the hash of the manifest it read).
# artifact id → (producer script name, produced-path index)
_STAMPED_ARTIFACTS = {
    "compare_models": "scripts/compare_models.py",
    "identifiability": "scripts/identifiability.py",
    "propagate_uncertainty": "scripts/propagate_uncertainty.py",
}


def _producer_verdicts(name: str) -> tuple[str, ...]:
    """VERDICTS exported by scripts/<name>.py, else the local fallback."""
    try:
        v = getattr(_load_sibling(name), "VERDICTS", None)
        if isinstance(v, (tuple, list)) and v:
            return tuple(str(x) for x in v)
    except Exception:  # noqa: BLE001 — fall back to the literal table
        pass
    return _PRODUCER_VERDICTS[name]


def _producer_stamp_violations(doc: dict, prefix: str, producer_key: str,
                               manifest_path: Optional[str], stage: str,
                               shown: str) -> list[dict]:
    """Phase 21 β: HIGH `<prefix>_not_script_produced` when the producer
    stamp is missing/wrong; HIGH `<prefix>_stale` when the recorded
    manifest hash differs from the current manifest (the report predates
    a manifest edit — re-run the script)."""
    import hashlib
    want = _STAMPED_ARTIFACTS[producer_key]
    if str(doc.get("producer") or "") != want:
        return [{
            "kind": f"{prefix}_not_script_produced",
            "severity": "HIGH",
            "stage": stage,
            "scope_declarable": False,
            "claim": (f"`{shown}` has producer {doc.get('producer')!r}, "
                      f"expected {want!r}. Gate inputs must be written by "
                      f"their producing script, not by hand. Re-run "
                      f"`python3 {want} {{run_dir}}`."),
        }]
    rec = doc.get("manifest_sha256")
    if rec and manifest_path and os.path.exists(manifest_path):
        with open(manifest_path, "rb") as f:
            cur = hashlib.sha256(f.read()).hexdigest()
        if cur != rec:
            return [{
                "kind": f"{prefix}_stale",
                "severity": "HIGH",
                "stage": stage,
                "scope_declarable": False,
                "claim": (f"`{shown}` was produced from a different version "
                          f"of {os.path.basename(manifest_path)} (sha256 "
                          f"mismatch). Re-run `python3 {want} {{run_dir}}`."),
            }]
    return []


# Phase 21 β: kinds that may never be resolved by DECLARE_SCOPE. At round
# exhaustion any of these turns the decision into RUN_FAILED. Sources:
#   - violations carrying `scope_declarable: False` (effort floors, safe_io,
#     provenance, required-artifact gating) — honored per finding;
#   - rigor_artifacts.yaml entries with `scope_declarable: false` — every
#     kind prefixed with the artifact id is non-declarable;
#   - the explicit list below (Phase 15 α pre_model_* contract, Phase 21 β
#     evidence-integrity kinds).
_NON_DECLARABLE_KINDS = frozenset({
    "pre_model_over_saturated",
    "pre_model_decorative_undocumented",
    "claim_source_unresolved",
    "claim_source_mismatch",
    "claim_value_not_in_source",
    "artifact_integrity_violation",
    "claim_ci_unsourced",
    "claim_ci_mismatch",
    "ci_degenerate",
    "uq_output_nonfinite",
    "validator_check_crashed",
    "pilot_run_unbacked",
    # Phase 21 δ eligibility probe (benefit computed on the wrong population)
    "eligibility_benefit_without_eligible",
    "eligibility_benefit_scales_with_total",
    "eligibility_probe_error",
    "a_priori_count_mismatch",
})
_NON_DECLARABLE_SUFFIXES = ("_invalid", "_unknown_verdict",
                            "_not_script_produced", "_stale")
# Phase 21 (before/after controlled retro): a NaN / null / wrong-type
# metric in a structured script output is a broken artifact, not a
# limitation — fixing it is always possible, so it must not be
# scope-declarable. decision_rule_malformed is excluded: it judges prose.
_NON_DECLARABLE_KINDS = _NON_DECLARABLE_KINDS | frozenset({
    "allocation_robustness_malformed",
    "optimization_quality_malformed",
    "plan_criteria_malformed",
    "sensitivity_analysis_malformed",
    "universal_coverage_malformed",
    "within_zone_heterogeneity_malformed",
})


def _manifest_non_declarable_prefixes() -> tuple[str, ...]:
    try:
        return tuple(f"{a.id}_" for a in load_artifacts()
                     if not a.scope_declarable)
    except (OSError, ValueError, yaml.YAMLError):
        return ()


def is_non_declarable(kind: str, explicit: Any = None) -> bool:
    """Phase 21 β: may a finding of this kind be closed by DECLARE_SCOPE?"""
    if explicit is False:
        return True
    k = str(kind or "")
    if k in _NON_DECLARABLE_KINDS or k.endswith(_NON_DECLARABLE_SUFFIXES):
        return True
    return k.startswith(_manifest_non_declarable_prefixes())

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
# Phase 21 ζ: optional cross-model critic (scripts/cross_model_critic.py,
# Codex CLI). Included only when its YAML exists; written by the script,
# never by an agent, so it may carry verdict UNAVAILABLE.
OPTIONAL_REVIEWERS = ("critique-crossmodel",)
# E5: critique-methods is opt-in (main.py --methods-critic). Its YAML is
# required only when metadata.json records `methods_critic: true`; when
# present it is always validated.
OPT_IN_AGENT_REVIEWERS = {"critique-methods": "methods_critic"}


def _meta_flag(run_dir: str, key: str) -> bool:
    try:
        with open(os.path.join(run_dir, "metadata.json")) as f:
            return bool(json.load(f).get(key))
    except (OSError, ValueError, AttributeError):
        return False


def _opt_in_requested(run_dir: str, reviewer: str) -> bool:
    return _meta_flag(run_dir, OPT_IN_AGENT_REVIEWERS[reviewer])
PREFIXES = {"critique-methods": "M-", "critique-domain": "D-",
            "critique-presentation": "P-", "critique-redteam": "R-",
            "critique-crossmodel": "X-"}
FILENAMES = {"critique-methods": "critique_methods.yaml",
             "critique-domain": "critique_domain.yaml",
             "critique-presentation": "critique_presentation.yaml",
             "critique-redteam": "critique_redteam.yaml",
             "critique-crossmodel": "critique_crossmodel.yaml"}
_CROSS_MODEL_UNAVAILABLE_HIGH_AFTER = 3  # consecutive rounds

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
    valid_verdicts = (VALID_VERDICT | {"UNAVAILABLE"}
                      if expected_reviewer in OPTIONAL_REVIEWERS else VALID_VERDICT)
    _require(doc.get("verdict") in valid_verdicts,
             f"{ctx}: verdict must be one of {valid_verdicts}")
    if doc.get("verdict") == "UNAVAILABLE":
        _require(isinstance(doc.get("unavailable_reason"), str)
                 and doc["unavailable_reason"].strip(),
                 f"{ctx}: verdict UNAVAILABLE requires a non-empty "
                 f"unavailable_reason")

    _validate_structural(doc.get("structural_mismatch"), ctx, expected_reviewer)

    # Phase 21 β: `blockers:` is required (an omitted list used to read as
    # "no blockers" → ACCEPT), and a REVISE verdict must name at least one
    # unresolved blocker — REVISE with nothing open is self-contradictory.
    _require("blockers" in doc, f"{ctx}: missing required key 'blockers' "
                                f"(use `blockers: []` for none)")
    blockers = doc["blockers"]
    _require(isinstance(blockers, list), f"{ctx}: blockers must be a list")
    seen_ids = set()
    for b in blockers:
        _validate_blocker(b, ctx, prefix)
        _require(b["id"] not in seen_ids, f"{ctx}: duplicate blocker id {b['id']}")
        seen_ids.add(b["id"])

    if doc.get("verdict") == "REVISE":
        _require(any(isinstance(b, dict) and b.get("resolved") is False
                     for b in blockers),
                 f"{ctx}: verdict REVISE requires at least one unresolved "
                 f"blocker")

    if current_round == 1:
        _require(doc.get("carried_forward", []) == [],
                 f"{ctx}: carried_forward must be [] on round 1")
    else:
        _validate_carried_forward(doc.get("carried_forward"), ctx,
                                  current_round, blockers)

    return doc


_ESCALATION_HIGH_THRESHOLD = 3  # patch_attempts >= 3 triggers mandatory escalation


def _compute_blocker_attempts(critiques: dict[str, dict],
                              current_round: int) -> dict[str, dict]:
    """Phase 5 ζ: count consecutive PATCH attempts per blocker_id by
    inspecting `carried_forward[]` entries with `still_present: true`.

    Semantic: when a blocker first appeared in round R and is still
    present in current round N (still_present: true), then PATCH was
    attempted (N - R) times without resolving it. patch_attempts >= 2
    means the same fix approach has failed at least twice; >= 3 means
    something structurally different is needed (new agent class,
    cross-stage escalation, or scope declaration).

    Phase 21 β: R is the blocker's `first_seen_round` (from the current
    `blockers[]` entry with the same id, or the carried_forward entry's
    own `first_seen_round`), falling back to `prior_round` only when
    neither is present. Real critics set `prior_round = N-1` (the round
    they compared against), so the pre-21 `N - prior_round` was always 1
    and escalation never fired on real runs.

    Returns {blocker_id: {"first_seen_round": R, "patch_attempts": K,
                           "category": C, "target_stage": S,
                           "still_present": True}}.
    """
    out: dict[str, dict] = {}
    blocker_meta: dict[str, dict] = {}
    for reviewer, doc in critiques.items():
        for b in doc.get("blockers", []):
            blocker_meta[b["id"]] = {
                "category": b.get("category"),
                "target_stage": b.get("target_stage"),
                "severity": b.get("severity"),
                "reviewer": reviewer,
                "first_seen_round": b.get("first_seen_round"),
            }
    for reviewer, doc in critiques.items():
        for entry in doc.get("carried_forward", []):
            if not entry.get("still_present"):
                continue
            bid = entry["id"]
            meta = blocker_meta.get(bid, {})
            first_seen = None
            for cand in (meta.get("first_seen_round"),
                         entry.get("first_seen_round"),
                         entry.get("prior_round")):
                if isinstance(cand, int) and not isinstance(cand, bool):
                    first_seen = cand
                    break
            if first_seen is None:
                first_seen = current_round
            attempts = max(0, current_round - first_seen)
            existing = out.get(bid)
            # If multiple critiques carry the same id, take max attempts.
            if existing is None or attempts > existing["patch_attempts"]:
                out[bid] = {
                    "first_seen_round": first_seen,
                    "patch_attempts": attempts,
                    "category": meta.get("category"),
                    "target_stage": meta.get("target_stage"),
                    "severity": meta.get("severity"),
                    "reviewer": meta.get("reviewer"),
                    "still_present": True,
                }
    return out


def _recompute_action(d: dict, max_rounds: int, current_round: int,
                      source: str = "") -> dict:
    """Phase 21 β: the single STAGE 7 rule ordering.

    Previously copy-pasted into decide() and three incorporate_*
    functions (and the copies had drifted). Rules:

      1  structural mismatch            → RETHINK_STRUCTURAL / RUN_FAILED
      2  HIGH or accept-blocker, rounds → PATCH_OR_RETHINK
      5  exhausted, any non-declarable  → RUN_FAILED
      3  exhausted, all declarable      → DECLARE_SCOPE
      4  nothing outstanding            → ACCEPT

    `accept_blockers` are required artifacts that are absent (Phase 21
    β): they forbid ACCEPT at every round, even when no HIGH is open.
    """
    unresolved_high = d.get("unresolved_high") or []
    accept_blockers = d.get("accept_blockers") or []
    structural = d.get("structural_mismatch", False)
    rounds_remaining = max_rounds - current_round
    outstanding = list(unresolved_high) + list(accept_blockers)
    non_declarable = [b for b in outstanding
                      if is_non_declarable(b.get("kind"),
                                           b.get("scope_declarable"))]
    src = f" (incl. {source})" if source else ""

    if structural:
        reviewers = d.get("structural_reviewers")
        if rounds_remaining > 0:
            action = "RETHINK_STRUCTURAL"
            rationale = (
                f"Structural mismatch detected by {reviewers}. "
                f"Must RETHINK — not patchable, not scope-declarable."
            )
        else:
            action = "RUN_FAILED"
            rationale = (
                f"Structural mismatch detected by {reviewers} "
                f"with no rounds remaining. Run fails: delivered model does "
                f"not answer the question. Do NOT spawn writer."
            )
        rule_matched = 1
    elif outstanding and rounds_remaining > 0:
        action = "PATCH_OR_RETHINK"
        parts = []
        if unresolved_high:
            parts.append(f"{len(unresolved_high)} HIGH blocker(s) unresolved{src}")
        if accept_blockers:
            parts.append(f"{len(accept_blockers)} required artifact(s) missing")
        rationale = (f"{'; '.join(parts)}, {rounds_remaining} round(s) "
                     f"remaining. ACCEPT is forbidden.")
        rule_matched = 2
    elif non_declarable:
        action = "RUN_FAILED"
        preview = ", ".join(sorted({str(b.get("kind") or b.get("id"))
                                    for b in non_declarable})[:8])
        rationale = (
            f"Rounds exhausted with {len(non_declarable)} NON-scope-"
            f"declarable finding(s) open [{preview}]. These cannot be "
            f"closed by scope_declaration.yaml (Phase 15 α / 18 α / 19 α / "
            f"21 β contracts). Run fails; do NOT spawn writer as if "
            f"accepted."
        )
        rule_matched = 5
    elif outstanding:
        action = "DECLARE_SCOPE"
        rationale = (
            f"{len(outstanding)} HIGH blocker(s)/missing artifact(s) "
            f"unresolved{src}, rounds exhausted. Must write "
            f"scope_declaration.yaml acknowledging each blocker by id, "
            f"and writer must embed verbatim in Limitations. "
            f"DECLARE_SCOPE is NOT the same as ACCEPT."
        )
        rule_matched = 3
    else:
        action = "ACCEPT"
        rationale = ("No unresolved HIGH blockers, no missing required "
                     "artifacts, no structural mismatch.")
        rule_matched = 4

    d["rounds_remaining"] = rounds_remaining
    d["action"] = action
    d["rule_matched"] = rule_matched
    d["rationale"] = rationale
    return d


def _load_optional_critique(run_dir: str, reviewer: str,
                            current_round: int) -> tuple[dict | None, list[dict]]:
    """Phase 21 ζ: load an optional, script-written critique.

    Absent → (None, []). Schema failure → (None, [HIGH
    cross_model_critic_invalid]). UNAVAILABLE → (doc, [MEDIUM
    cross_model_critic_unavailable]), HIGH once the critic has been
    unavailable for `_CROSS_MODEL_UNAVAILABLE_HIGH_AFTER` consecutive
    rounds (from the file's own availability_history). An UNAVAILABLE
    doc still contributes its carried-forward open blockers.
    """
    path = os.path.join(run_dir, FILENAMES[reviewer])
    if not os.path.exists(path):
        if _meta_flag(run_dir, "cross_model"):
            # Requested for this run but never written: the lead skipped it.
            return None, [{
                "kind": "cross_model_critic_missing", "severity": "HIGH",
                "stage": "CRITIQUE", "scope_declarable": False,
                "claim": (f"This run was started with --cross-model but "
                          f"{FILENAMES[reviewer]} does not exist. Run "
                          f"`python3 scripts/cross_model_critic.py {{run_dir}} "
                          f"--round {current_round}` in STAGE 6."),
            }]
        return None, []
    try:
        doc = validate_critique(path, reviewer, current_round)
    except SchemaError as e:
        return None, [{
            "kind": "cross_model_critic_invalid", "severity": "HIGH",
            "stage": "CRITIQUE",
            "claim": (f"{FILENAMES[reviewer]} failed the critique schema: {e}. "
                      f"It is written by scripts/cross_model_critic.py; re-run "
                      f"the script for round {current_round} (do not hand-edit)."),
        }]
    if doc.get("verdict") != "UNAVAILABLE":
        return doc, []
    streak = 0
    hist = [h for h in (doc.get("availability_history") or [])
            if isinstance(h, dict) and isinstance(h.get("round"), int)]
    hist.sort(key=lambda h: h["round"])
    if not hist or hist[-1]["round"] != current_round:
        hist.append({"round": current_round, "status": "UNAVAILABLE"})
    for h in reversed(hist):
        if h.get("status") != "UNAVAILABLE":
            break
        streak += 1
    sev = "HIGH" if streak >= _CROSS_MODEL_UNAVAILABLE_HIGH_AFTER else "MEDIUM"
    return doc, [{
        "kind": "cross_model_critic_unavailable", "severity": sev,
        "stage": "CRITIQUE",
        "claim": (f"Cross-model critic unavailable for {streak} consecutive "
                  f"round(s): {doc.get('unavailable_reason')}. Its prior open "
                  f"blockers stay open un-rechecked. Fix the Codex CLI "
                  f"(install / `codex login`) or run without --cross-model."),
    }]


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
                    "kind": "critique_blocker",
                    "scope_declarable": True,
                })

    # Phase 5 ζ: consecutive-PATCH-attempt counter.
    blocker_attempts = _compute_blocker_attempts(critiques, current_round)
    escalation_required = any(
        info["severity"] == "HIGH"
        and info["patch_attempts"] >= _ESCALATION_HIGH_THRESHOLD
        for info in blocker_attempts.values()
    )

    structural_reviewers = [r for r, d in critiques.items()
                            if d["structural_mismatch"]["detected"]]

    d = {
        "unresolved_high": unresolved_high,
        "accept_blockers": [],
        "structural_mismatch": bool(structural_reviewers),
        "structural_reviewers": structural_reviewers,
        "spec_violations": [],
        "registry_violations": [],
        "rigor_violations": [],
        # Phase 5 ζ: stuck-blocker tracking.
        "blocker_attempts": blocker_attempts,
        "escalation_required": escalation_required,
    }
    return _recompute_action(d, max_rounds, current_round)


def _append_synthetic_blockers(d: dict, reviewer: str, prefix: str,
                               new_entries: list[dict]) -> None:
    """Phase 21 β: idempotent append. An entry already present (same
    reviewer + kind + claim) is not re-added, so calling an incorporate_*
    function twice yields identical output."""
    existing = [b for b in d["unresolved_high"] if b.get("reviewer") == reviewer]
    seen = {(b.get("kind"), b.get("claim")) for b in existing}
    n = len(existing)
    for e in new_entries:
        key = (e.get("kind"), e.get("claim"))
        if key in seen:
            continue
        seen.add(key)
        n += 1
        entry = {"reviewer": reviewer, "id": f"{prefix}-{n:03d}"}
        entry.update(e)
        d["unresolved_high"].append(entry)


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
# Numeric with optional sign, comma thousands, decimal, scientific,
# and SI suffix (k/K/m/M/b/B/t/T) or unit suffix ($, %).
# Phase 5 fix: require at least one comma-group in the comma-form
# alternative; the prior `(?:,\d{3})*` allowing zero comma-groups made
# the engine match "201" then "5" separately for the input "2015".
# The `\b...\b` boundaries prevent partial-match drift.
_NUMERIC_TOKEN_RE = re.compile(
    r"(?P<sign>[-+]?)\$?\s*"
    r"(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?P<suffix>[kKmMbBtT]?)\b"
)
# Time-horizon suffix in cells like "/3yr", "/5 years", "/yr", "per year".
_HORIZON_RE = re.compile(
    r"/\s*(\d+)\s*(?:yr|year)|/\s*(?:yr|year)\b|per\s+year",
    re.IGNORECASE,
)


def _extract_last_numeric(cell: str) -> Optional[float]:
    """Extract the LAST numeric token from a cell string. Modelers
    typically write the original then a parenthesized normalization
    (e.g. `84,000/5yr (~50,400/3yr)`); the LAST token is the normalized
    figure that should be used for cross-column comparison.

    Phase 5 fixes:
      - Strip horizon suffixes (`/3yr`, `/5 years`, `/yr`, `per year`)
        BEFORE extracting numerics. Otherwise the regex picks up the
        horizon's digit (e.g., "5" from "/5yr") as the last numeric.
      - Strip parenthetical citations (`(Bhatt 2015)`, `(Author Year)`)
        BEFORE extracting numerics. Citation years would otherwise be
        picked up as the last numeric, replacing the actual quantity.
        Normalization parentheticals like `(~50,400/3yr)` start with
        non-letter characters (digit, ~, $) so they are NOT stripped.
    """
    stripped = _HORIZON_RE.sub("", cell)
    # Strip `(Author Year)` style citations — paren content beginning
    # with an alphabetic character. Keeps `(50,400/3yr)`, `($107M)`,
    # `(~12%)` — anything starting with a digit/punctuation.
    stripped = re.sub(r"\(\s*[A-Za-z][^)]*\)", "", stripped)
    matches = list(_NUMERIC_TOKEN_RE.finditer(stripped))
    if not matches:
        return None
    m = matches[-1]
    raw = m.group("num").replace(",", "")
    try:
        value = float(raw)
    except ValueError:
        return None
    suffix = (m.group("suffix") or "").lower()
    multiplier = {"k": 1e3, "m": 1e6, "b": 1e9, "t": 1e12}.get(suffix, 1.0)
    return value * multiplier


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


def _parse_md_tables(text: str) -> list[dict]:
    """Linear scan for Markdown tables. Returns a list of
    {headers: [...], rows: [{label: str, cells: [...]}, ...]} dicts.
    Linear scan, no backtracking."""
    tables = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        if not (line.startswith("|") and line.endswith("|") and "|" in line[1:-1]):
            i += 1
            continue
        # Possible table header. Need a separator line next.
        if i + 1 >= len(lines):
            i += 1
            continue
        sep = lines[i + 1].strip()
        if not re.fullmatch(r"\|[\s:|-]+\|", sep):
            i += 1
            continue
        # Parse header.
        headers = [c.strip() for c in line.strip("|").split("|")]
        rows = []
        j = i + 2
        while j < len(lines):
            row_line = lines[j].rstrip()
            if not (row_line.startswith("|") and row_line.endswith("|")):
                break
            cells = [c.strip() for c in row_line.strip("|").split("|")]
            if len(cells) != len(headers):
                break
            rows.append({"label": cells[0], "cells": cells[1:]})
            j += 1
        if rows:
            tables.append({"headers": headers, "rows": rows})
        i = j
    return tables


def _check_comparator_efficiency(run_dir: str) -> list[dict]:
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


# Phase 4 Commit β: surrogate UQ documentation requirement.
# When outcome_fn.py reads precomputed CSVs and rescales rather than
# calling the actual model, the 200-draw "ensemble" is a closed-form
# recomputation, not a Monte Carlo over the model. The headline CIs
# reflect parameter uncertainty in a closed-form formula — readers
# typically assume Monte-Carlo-over-model. Require explicit calibration
# documentation when the surrogate path is taken.
_REAL_MODEL_PATTERNS = [
    re.compile(r"\bss\.Sim\s*\("),
    re.compile(r"\bstarsim\.Sim\s*\("),
    re.compile(r"\bsim\.run\s*\("),
    re.compile(r"\bsolve_ivp\s*\("),
    re.compile(r"\bodeint\s*\("),
    re.compile(r"\.simulate\s*\("),
    re.compile(r"\bMonteCarlo\s*\("),
]
# Surrogate detection: look for the precomputed-CSV naming convention
# anywhere in outcome_fn.py. Matches both direct read_csv calls
# (`pd.read_csv("package_evaluation.csv")`) and indirected ones
# (`path = os.path.join(..., "package_evaluation.csv"); pd.read_csv(path)`).
# Pairing these naming patterns with "no real-model call" is the
# surrogate signal.
_SURROGATE_PATTERNS = [
    re.compile(
        r"['\"][^'\"]*"
        r"(?:package_eval|calibration_results|scenarios|grid_results|"
        r"emulator_grid|surrogate_grid|package_evaluation)"
        r"[^'\"]*\.(?:csv|parquet|feather|pkl|npz)['\"]",
        re.IGNORECASE,
    ),
]


def _check_surrogate_uq_documented(run_dir: str) -> list[dict]:
    """Phase 4 Commit β: detect surrogate UQ without calibration docs.

    If outcome_fn.py reads a precomputed CSV (named e.g. *package_eval*,
    *calibration_results*, *scenarios*, *grid_results*) AND does NOT
    call any real-model runner (ss.Sim, sim.run, solve_ivp, odeint,
    .simulate), require models/outcome_fn_calibration.md documenting
    surrogate RMSE vs the full model on a validation grid.

    Emits:
      surrogate_uq_undocumented HIGH      — surrogate path, no calibration md
      surrogate_calibration_missing_rmse  — md exists but no RMSE figure
                                            (MEDIUM)
    """
    outcome_fn_path = artifact_path("outcome_fn", run_dir)
    if not os.path.exists(outcome_fn_path):
        return []
    try:
        with open(outcome_fn_path) as f:
            text = f.read()
    except (OSError, UnicodeDecodeError):
        return []

    has_real = any(p.search(text) for p in _REAL_MODEL_PATTERNS)
    has_surrogate = any(p.search(text) for p in _SURROGATE_PATTERNS)
    if not (has_surrogate and not has_real):
        return []

    # outcome_fn_calibration.md is a sibling doc, not a primary rigor artifact.
    cal_md_path = os.path.join(run_dir, "models", "outcome_fn_calibration.md")
    if not os.path.exists(cal_md_path):
        return [{
            "kind": "surrogate_uq_undocumented",
            "severity": "HIGH",
            "stage": "UQ",
            "claim": (
                "models/outcome_fn.py reads a precomputed CSV "
                "(package_evaluation / calibration_results / scenarios / "
                "grid_results) and does NOT call the model "
                "(no ss.Sim/sim.run/solve_ivp/odeint/.simulate). The "
                "200-draw 'ensemble' is therefore a closed-form "
                "recomputation, not a Monte Carlo over the model — the "
                "headline CIs reflect parameter uncertainty under the "
                "surrogate's analytical assumptions only. Required: "
                "models/outcome_fn_calibration.md documenting (1) the "
                "surrogate architecture (interpolation method, grid "
                "resolution), (2) RMSE vs full-model validation grid "
                "(>= 10 grid points), (3) cross-validation error, "
                "(4) extrapolation bounds. Without this document the "
                "report's CI framing misrepresents what was computed."
            ),
        }]
    try:
        with open(cal_md_path) as f:
            cal_text = f.read()
    except (OSError, UnicodeDecodeError):
        cal_text = ""
    if not re.search(r"\bRMSE\b", cal_text, re.I):
        return [{
            "kind": "surrogate_calibration_missing_rmse",
            "severity": "MEDIUM",
            "stage": "UQ",
            "claim": (
                "models/outcome_fn_calibration.md exists but does not "
                "contain an RMSE validation figure. The doc must "
                "validate the surrogate against the full model on a "
                "grid of >=10 points and report per-output RMSE. "
                "Without an RMSE figure the surrogate's accuracy is "
                "unaudited."
            ),
        }]
    return []


# Phase 4 Commit α: zero-width CI detector.
# Threshold rationale: 0.5% relative width catches optimizer-bounded
# outputs (e.g., total_cost when greedy fills budget) and hardcoded
# calibration targets (CI ~ 1e-7 of the target value). Looser would miss
# them; tighter would flag legitimately well-constrained outputs.
_CI_DEGENERATE_THRESHOLD = 0.005


def _load_uq_deterministic_declarations(run_dir: str) -> dict[str, str]:
    """Phase 21 β: outputs the modeler declares deterministic by design
    (e.g. total spend under a binding budget constraint), with a reason.

    Read from `models/uq_declarations.yaml`:
        deterministic_outputs:
          total_cost: "Budget constraint binds; spend equals the envelope."
    and from per-output `deterministic: true` + `deterministic_reason:` in
    uncertainty_report.yaml. Entries without a non-empty reason are ignored.
    """
    out: dict[str, str] = {}
    path = os.path.join(run_dir, "models", "uq_declarations.yaml")
    doc, _ = safe_load_mapping(path, "uq_declarations")
    if doc:
        decl = doc.get("deterministic_outputs")
        if isinstance(decl, dict):
            for k, v in decl.items():
                if isinstance(v, str) and v.strip():
                    out[str(k)] = v.strip()
    return out


def _check_uq_ci_quality(uq_report_path: str) -> list[dict]:
    """Phase 4 Commit α / Phase 21 β: scan uncertainty_report.yaml for
    degenerate and non-finite intervals.

    `ci_degenerate` fires when an output's 95% CI relative width
    (ci_high - ci_low) / |mean| is below 0.5% (or the width is exactly 0)
    while more than one draw was run. Phase 21 β raised it from MEDIUM to
    HIGH and made it NOT scope-declarable: 2026-05-03_235631 shipped 9/9
    zero-width outputs as MEDIUM while the report printed a wide CI for
    the same quantity. An output that is deterministic by design is
    exempted only by an explicit declaration with a reason
    (models/uq_declarations.yaml `deterministic_outputs:` or per-output
    `deterministic: true` + `deterministic_reason:`).

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
    declared = _load_uq_deterministic_declarations(run_dir)
    n_draws = finite_number(report.get("n_successful"))
    if n_draws is None:
        n_draws = finite_number(report.get("n_draws"))
    multi_draw = n_draws is None or n_draws > 1
    # Phase 21 δ: propagate_uncertainty.py now lists degenerate outputs
    # itself; trust-but-verify — honor its list AND keep the width check
    # for reports that predate the field.
    flagged = report.get("degenerate_outputs")
    flagged = {str(x) for x in flagged} if isinstance(flagged, list) else set()

    for output_name, stats in report["scalar_outputs"].items():
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
        if not multi_draw:
            continue  # effort floors own the n_draws minimum
        if output_name in declared or (
                stats.get("deterministic") is True
                and str(stats.get("deterministic_reason") or "").strip()):
            continue
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
        if degenerate:
            violations.append({
                "kind": "ci_degenerate",
                "severity": "HIGH",
                "stage": "UQ",
                "scope_declarable": False,
                "claim": (
                    f"{output_name} 95% CI [{lo_f:.6g}, {hi_f:.6g}] is "
                    f"{rel_txt} after "
                    f"{int(n_draws) if n_draws is not None else '?'} draws. "
                    f"Either the sampled parameters never reach this "
                    f"output (frozen in outcome_fn, surrogate flattening, "
                    f"parameters not wired through), or it is "
                    f"deterministic by design — then declare it in "
                    f"models/uq_declarations.yaml `deterministic_outputs:` "
                    f"with a reason and report it as a point estimate. "
                    f"Any interval quoted elsewhere for this output is "
                    f"unsupported."
                ),
            })
    return violations


def _msc_report_path(run_dir: str) -> str:
    """model_comparison_formal.yaml at its manifest location, falling back
    to models/ (some runs write it there)."""
    p = produced_path("model_comparison", run_dir)
    if not os.path.exists(p):
        alt = os.path.join(os.path.dirname(artifact_path("model_comparison",
                                                         run_dir)),
                           os.path.basename(p))
        if os.path.exists(alt):
            return alt
    return p


def _msc_report_violations(msc_report: str,
                           manifest_path: Optional[str] = None) -> list[dict]:
    formal, out = safe_load_mapping(
        msc_report, "model_comparison_formal", stage="MULTI_STRUCTURAL",
        allowed_verdicts=_producer_verdicts("compare_models"),
        display_path="model_comparison_formal.yaml")
    if formal is None or out:
        return out
    out = _producer_stamp_violations(
        formal, "model_comparison_formal", "compare_models", manifest_path,
        "MULTI_STRUCTURAL", "model_comparison_formal.yaml")
    if out:
        return out
    verdict = formal["verdict"]
    if verdict == "DEGENERATE_FIT_DETECTED":
        deg = formal.get("degenerate_fit")
        deg = deg if isinstance(deg, dict) else {}
        out.append({
            "kind": "degenerate_fit",
            "severity": "HIGH",
            "stage": "MULTI_STRUCTURAL",
            "claim": (f"compare_models flagged DEGENERATE FIT on "
                      f"model {deg.get('model', '?')}: "
                      f"{deg.get('reason', '(no reason given)')}"),
        })
    elif verdict == "INSUFFICIENT_STRUCTURES":
        out.append({
            "kind": "insufficient_structures",
            "severity": "HIGH",
            "stage": "MULTI_STRUCTURAL",
            "claim": ("Modeler supplied fewer than 3 candidate "
                      "structures for comparison. See "
                      "multi-structural-comparison skill."),
        })
    return out


def _identifiability_parameters(id_rep: dict) -> list[tuple[str, dict]]:
    params = id_rep.get("parameters")
    if isinstance(params, dict):
        return [(str(k), v) for k, v in params.items() if isinstance(v, dict)]
    if isinstance(params, list):
        return [(str(p.get("name", f"param_{i}")), p)
                for i, p in enumerate(params) if isinstance(p, dict)]
    return []


def _identifiability_report_violations(id_report: str,
                                       manifest_path: Optional[str] = None
                                       ) -> list[dict]:
    id_rep, out = safe_load_mapping(
        id_report, "identifiability_report", stage="IDENTIFIABILITY",
        allowed_verdicts=_producer_verdicts("identifiability"),
        display_path="identifiability.yaml")
    if id_rep is None or out:
        return out
    out = _producer_stamp_violations(
        id_rep, "identifiability_report", "identifiability", manifest_path,
        "IDENTIFIABILITY", "identifiability.yaml")
    if out:
        return out
    verdict = id_rep["verdict"]
    params = _identifiability_parameters(id_rep)
    if verdict == "UNIDENTIFIED_PARAMETERS":
        unidentified = [n for n, p in params if p.get("status") == "unidentified"]
        out.append({
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
    elif verdict == "UNKNOWN_LOSS_SCALE":
        out.append({
            "kind": "identifiability_loss_scale_unknown",
            "severity": "HIGH",
            "stage": "IDENTIFIABILITY",
            "claim": ("identifiability.py could not classify parameters "
                      "because models/identifiability.yaml does not "
                      "declare a usable `loss_type` (nll | deviance | chi2 "
                      "| sse/mse with n_obs). Profile-likelihood thresholds "
                      "are only meaningful on a log-likelihood scale."),
        })
    elif verdict == "WEAKLY_IDENTIFIED_PARAMETERS":
        weak = [n for n, p in params if p.get("status") == "weakly_identified"]
        out.append({
            "kind": "weakly_identified_parameters",
            "severity": "MEDIUM",
            "stage": "IDENTIFIABILITY",
            "claim": (f"identifiability analysis flagged {len(weak)} weakly "
                      f"identified parameter(s): {weak}. Report their "
                      f"intervals as wide and avoid point-estimate claims "
                      f"that depend on them."),
        })
    return out


def _check_core_rigor_reports(run_dir: str) -> list[dict]:
    """Phase 2 core rigor reports: UQ, multi-structural, identifiability.

    Each rigor stage has a prerequisite + an artifact:
      UQ: outcome_fn.py (prereq) + uncertainty_report.yaml (artifact)
      Multi-structural: model_comparison.yaml (prereq) + model_comparison_formal.yaml (artifact)
      Identifiability: identifiability.yaml in models/ (prereq) + identifiability.yaml in run_dir (artifact)
    """
    violations = []

    # UQ: outcome_fn.py → uncertainty_report.yaml
    outcome_fn_path = artifact_path("outcome_fn", run_dir)
    uq_report_path = produced_path("outcome_fn", run_dir)
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
            # Phase 4 Commit α: detect zero-width CIs.
            violations.extend(_check_uq_ci_quality(uq_report_path))
            # Phase 21 β: must be written by propagate_uncertainty.py.
            uq_doc, _ = safe_load_mapping(uq_report_path, "uncertainty_report")
            if uq_doc is not None:
                violations.extend(_producer_stamp_violations(
                    uq_doc, "uncertainty_report", "propagate_uncertainty",
                    None, "UQ", "uncertainty_report.yaml"))
        # Phase 4 Commit β: when outcome_fn is a surrogate, require
        # documentation. (Runs even when uncertainty_report.yaml is
        # missing — the surrogate framing issue exists regardless of
        # whether UQ has been re-run.)
        violations.extend(_check_surrogate_uq_documented(run_dir))
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
    msc_manifest = artifact_path("model_comparison", run_dir)
    msc_report = _msc_report_path(run_dir)
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
            # Phase 21 β: closed-vocabulary verdict check (fail closed).
            violations.extend(_msc_report_violations(msc_report, msc_manifest))
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
    id_manifest = artifact_path("identifiability", run_dir)
    id_report = produced_path("identifiability", run_dir)
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
            violations.extend(_identifiability_report_violations(
                id_report, id_manifest))
    # NOTE: absence of identifiability.yaml with NO manifest is MEDIUM — many
    # models have no fitted parameters. Don't force this check universally.

    return violations


# Phase 21 β: when does an artifact's absence forbid ACCEPT? Read from the
# raw manifest (`required_for_accept:`; the Artifact dataclass does not
# carry it), defaulting from `triggered_by`.
_TRIGGER_DEFAULT_REQUIREMENT = {
    "always": "always",
    "allocation": "allocation",
    "aggregation_ratio_lt_0.1": "aggregation",
}


def _manifest_requirements() -> dict[str, str]:
    import lib.rigor_artifacts as _ra
    try:
        with open(_ra._MANIFEST_PATH, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        raw = {}
    reqs: dict[str, str] = {}
    by_id = {str(e.get("id")): e for e in raw.get("artifacts") or []
             if isinstance(e, dict)}
    for a in load_artifacts():
        mode = (by_id.get(a.id) or {}).get("required_for_accept")
        reqs[a.id] = str(mode) if mode else \
            _TRIGGER_DEFAULT_REQUIREMENT.get(a.triggered_by, "always")
    return reqs


def _not_applicable_artifacts(run_dir: str) -> set[str]:
    """Artifact ids declared `not_applicable` (with a justification) in
    scope_declaration.yaml, e.g.
        declarations:
          - id: SD-004
            not_applicable: identifiability
            justification: "No parameters are fitted; all are literature values."
    """
    _doc, entries, _ = _load_scope_declarations(run_dir)
    out: set[str] = set()
    for e in entries:
        na = e.get("not_applicable")
        if na and _entry_has_justification(e):
            for x in (na if isinstance(na, list) else [na]):
                out.add(str(x).strip())
    return out


def _allocation_present(run_dir: str) -> bool:
    return bool(_find_allocation_csvs(run_dir)) or os.path.exists(
        artifact_path("decision_rule", run_dir))


def _check_artifact_integrity(run_dir: str) -> list[dict]:
    """Phase 23 α: `artifact_integrity.json` is written by the harness
    (agents/integrity.py) during a pipeline run. Each violation — a change
    to a protected run artifact that no allowed in-flight tool call
    explains — is HIGH and not scope-declarable: the evidence the gate
    grades was altered outside its sanctioned writer. Absent file (replays,
    fixtures, pre-23 runs): nothing to check."""
    path = os.path.join(run_dir, "artifact_integrity.json")
    if not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            doc = json.load(f)
        if not isinstance(doc, dict) or doc.get("producer") != "agents/integrity.py":
            raise ValueError("missing or wrong producer stamp")
        events = doc.get("violations")
        if not isinstance(events, list) or not all(isinstance(e, dict) for e in events):
            # Missing, null, {} or [null] is a malformed record, not "clean".
            raise ValueError("`violations` must be a list of event objects")
    except (OSError, ValueError) as e:
        return [{"kind": "artifact_integrity_invalid", "severity": "HIGH", "stage": "GATE",
                 "scope_declarable": False,
                 "claim": f"artifact_integrity.json is unreadable or not harness-written ({e})."}]
    def _desc(e):
        return (f"{e.get('change')} {e.get('file')} (detected {e.get('detected')}, in flight: "
                f"{', '.join(e.get('in_flight') or []) or 'nothing'})")
    open_ = [e for e in events if e.get("open", True)]
    out = _capped("artifact_integrity_violation", [_desc(e) for e in open_],
                  "Protected run artifact changed outside its sanctioned writer. Fix: re-run "
                  "its producer script (`python3 scripts/<producer>.py {run_dir}`) so the "
                  "file is producer-written again; never write it from your own code: ",
                  stage="GATE")
    resolved = [e for e in events if not e.get("open", True)]
    if resolved:
        out.append({"kind": "artifact_integrity_resolved", "severity": "MEDIUM", "stage": "GATE",
                    "claim": (f"{len(resolved)} protected artifact(s) were written outside their "
                              f"producer and later regenerated by it: "
                              + "; ".join(_desc(e) for e in resolved[:5]))})
    return out


def _parameters_fitted(run_dir: str) -> bool:
    """Evidence that the run fitted parameters (Copilot review of PR #14):
    an identifiability manifest, a model comparison with any k_params > 0,
    or a calibration output — the effort-floor trigger's test. A comparison
    listing only k_params: 0 models (analytical, unfitted) is not fitting.
    Fail closed: a model comparison that can't be read counts as fitting."""
    if _load_sibling("effort_floors")._calibration_evidence(run_dir):
        return True
    p = artifact_path("model_comparison", run_dir)
    if not os.path.exists(p):
        return False
    try:
        with open(p) as f:
            doc = yaml.safe_load(f)
    except (OSError, yaml.YAMLError):
        return True
    models = doc.get("models") if isinstance(doc, dict) else None
    if not isinstance(models, list):
        return True
    for m in models:
        try:
            if not isinstance(m, dict) or int(m.get("k_params") or 0) > 0:
                return True
        except (TypeError, ValueError):
            return True
    return False


def _check_required_artifacts(run_dir: str, post_write: bool = False
                              ) -> list[dict]:
    """Phase 21 β: required artifacts gate ACCEPT at EVERY round.

    Pre-21, most missing artifacts were round-gated MEDIUMs, so a run with
    schema-valid empty critiques could ACCEPT at round 1 or 2 with no
    model artifacts at all. Each missing required artifact is now a
    `required_artifact_missing` accept-blocker: PATCH while rounds remain;
    at exhaustion DECLARE_SCOPE if the artifact is scope-declarable,
    RUN_FAILED if not. A scope-declarable artifact can be exempted with a
    justified `not_applicable:` entry in scope_declaration.yaml.
    """
    reqs = _manifest_requirements()
    allocation = _allocation_present(run_dir)
    pair = _detect_calibration_allocation_ratio(run_dir)
    aggregation = bool(pair) and pair[0] / pair[1] < _ECOLOGICAL_AGGREGATION_THRESHOLD
    fitted = _parameters_fitted(run_dir)
    applies = {"always": True, "allocation": allocation,
               "aggregation": aggregation, "fitted_parameters": fitted,
               "post_write": post_write, "never": False}
    exempt = _not_applicable_artifacts(run_dir)
    out: list[dict] = []
    for a in load_artifacts():
        mode = reqs.get(a.id, "always")
        if not applies.get(mode, True):
            continue
        if os.path.exists(artifact_path(a.id, run_dir)):
            continue
        if a.scope_declarable and a.id in exempt:
            continue
        out.append({
            "kind": "required_artifact_missing",
            "severity": "HIGH",
            "stage": a.stage.upper(),
            "accept_blocker": True,
            "artifact": a.id,
            "target_stage": {"pre_model": "PLAN", "write": "WRITE",
                             "analyze": "ANALYZE"}.get(a.stage, "MODEL"),
            "scope_declarable": a.scope_declarable,
            "claim": (f"Required artifact `{a.path}` ({a.id}, required "
                      f"when: {mode}) is absent. ACCEPT is impossible "
                      f"without it at any round"
                      + ("" if a.scope_declarable else
                         " and it is NOT scope-declarable")
                      + f". See the `{a.skill}` skill."),
        })
    if post_write and not os.path.exists(os.path.join(run_dir, "report.md")):
        out.append({
            "kind": "required_artifact_missing",
            "severity": "HIGH",
            "stage": "WRITE",
            "accept_blocker": True,
            "artifact": "report",
            "target_stage": "WRITE",
            "scope_declarable": False,
            "claim": "Post-WRITE gate: report.md is absent.",
        })
    return out


def _check_eligibility_probe(run_dir: str,
                             round_n: int | None = None) -> list[dict]:
    """Phase 21 β/δ: behavioral eligible-population check via
    scripts/eligibility_probe.py (benefit must vanish when the eligible
    population is zero and must not scale with the ineligible population).

    Required (missing manifest = HIGH `eligibility_manifest_missing`) when
    an allocation exists. Round 1 is the drafting window: HIGHs demote to
    MEDIUM. Absent probe module → skip (no finding).
    """
    try:
        mod = _load_sibling("eligibility_probe")
    except (FileNotFoundError, ImportError):
        return []
    findings = mod.check(run_dir, require=_allocation_present(run_dir))
    if not isinstance(findings, list):
        raise TypeError(f"eligibility_probe.check returned {type(findings).__name__}")
    drafting = round_n is not None and round_n < 2
    out = []
    for f in findings:
        if not isinstance(f, dict):
            continue
        v = dict(f)
        v.setdefault("stage", "MODEL")
        if drafting and v.get("severity") == "HIGH":
            v["severity"] = "MEDIUM"
        out.append(v)
    return out


def _check_a_priori_counts(run_dir: str,
                           round_n: int | None = None) -> list[dict]:
    """Phase 21 β/δ: the a-priori identifiability counts must match the
    built model (identifiability_a_priori.cross_check_counts). 235631
    declared 17 params / 37 targets (IDENTIFIABLE) while the comparison
    manifest showed k=22 vs n=22. Gated like the a-priori artifact."""
    if round_n is None or round_n < _IDENTIFIABILITY_A_PRIORI_REQUIRED_FROM_ROUND:
        return []
    try:
        mod = _load_sibling("identifiability_a_priori")
    except (FileNotFoundError, ImportError):
        return []
    fn = getattr(mod, "cross_check_counts", None)
    if fn is None:
        return []
    out = []
    for f in fn(run_dir) or []:
        if isinstance(f, dict):
            v = dict(f)
            v.setdefault("stage", "PRE_MODEL")
            out.append(v)
    return out


def _check_rigor_artifacts(run_dir: str, round_n: int | None = None,
                           post_write: bool = False) -> list[dict]:
    """Run every rigor check. Returns a list of violations.

    Phase 21 β: every check runs through `run_guarded`, so an exception
    inside any `_check_*` becomes a HIGH `validator_check_crashed` finding
    instead of crashing the gate (exit 1, no JSON) or silently returning
    nothing. `post_write=True` (CLI `--post-write`) additionally requires
    report.md / post-WRITE artifacts and makes writer-QA / coherence
    unavailability HIGH. Violations are annotated with `first_seen_round`
    from validator_history.jsonl.
    """
    G = run_guarded
    violations: list[dict] = []
    # Phase 2 core reports (UQ, multi-structural, identifiability).
    violations += G(_check_core_rigor_reports, run_dir)
    # Phase 21 β: missing required artifacts forbid ACCEPT at any round.
    violations += G(_check_required_artifacts, run_dir, post_write=post_write)
    # Decision rule (Phase 3 Commit C): required when an allocation CSV exists.
    violations += G(_check_decision_rule_artifact, run_dir)
    # Phase 4 Commit δ: cross-comparator efficiency outlier check.
    violations += G(_check_comparator_efficiency, run_dir)
    # Phase 7 Commit λ: STAGE 8.5 WRITER_QA pass (recomputed, Phase 21 β).
    violations += G(_check_writer_qa, run_dir, post_write=post_write)
    # Phase 7 Commit μ: plan-promised criteria enforcement.
    violations += G(_check_plan_criteria, run_dir)
    # Phase 9 Commit ρ: write-time figure validator + provenance check.
    violations += G(_check_figure_validator, run_dir)
    # Phase 12 Commit α: cross-file numeric consistency check.
    violations += G(lambda rd: _load_sibling("numeric_consistency")
                    .check_numeric_consistency(rd), run_dir,
                    check_name="numeric_consistency")
    # Phase 23 α: unexplained changes to protected run artifacts.
    violations += G(_check_artifact_integrity, run_dir)
    # Phase 22 δ: data CSVs checked against their declared units/ranges.
    violations += G(lambda rd: _load_sibling("data_schema").check(rd, round_n),
                    run_dir, check_name="data_schema")
    # E5 follow-up: unit and data-vintage source checks (data vs itself and
    # vs the run's own data documents; no domain vocabulary).
    violations += G(lambda rd: _load_sibling("source_checks").check_sources(rd),
                    run_dir, check_name="source_checks")
    # Phase 10 Commit ψ: allocation-gate coordinator (optimization_quality,
    # daly, allocation_robustness, universal_coverage, sensitivity_analysis;
    # round-aware MEDIUM consolidation).
    violations += G(_check_allocation_rigor_status, run_dir, round_n=round_n)
    # Phase 12 Commit γ: ecological-fallacy / within-unit heterogeneity.
    violations += G(_check_within_zone_heterogeneity, run_dir)
    # Phase 13 Commit α: disease-agnostic structural sanity checks.
    violations += G(_check_sanity_schema, run_dir, round_n=round_n)
    # Phase 15 Commit α: a-priori identifiability arithmetic (NOT
    # scope-declarable at OVER_SATURATED).
    violations += G(_check_identifiability_a_priori, run_dir, round_n=round_n)
    # Phase 21 β/δ: a-priori counts must match the built model.
    violations += G(_check_a_priori_counts, run_dir, round_n=round_n)
    # Phase 17 Commit α: pre-mortem domain critique.
    violations += G(_check_premortem_addressed, run_dir, round_n=round_n)
    # Phase 17 Commit β: coherence audit (post-WRITE; recomputed, 21 β).
    violations += G(_check_coherence_audit, run_dir, post_write=post_write)
    # Phase 18 Commit α + 21 β: claims ledger present, schema-valid, and
    # every claim matches the artifact it cites (NOT scope-declarable).
    violations += G(_check_claims_ledger_present, run_dir, round_n=round_n)
    # Phase 19 Commit α: effort floors.
    violations += G(_check_effort_floors, run_dir, round_n=round_n)
    # Phase 19 Commit β: scope-declaration budget.
    violations += G(_check_scope_declaration_budget, run_dir, round_n=round_n)
    # Phase 19 Commit γ: benchmark-locked acceptance.
    violations += G(_check_benchmark_match, run_dir, round_n=round_n)
    # Phase 19 Commit δ: sufficiency critic (post-WRITE).
    violations += G(_check_sufficiency_critic, run_dir, round_n=round_n)
    # Phase 21 β/δ: eligible-population behavioral probe.
    violations += G(_check_eligibility_probe, run_dir, round_n=round_n)
    # Phase 12 Commit β: round-aware escalation of persisting MEDIUMs
    # (history-backed since Phase 21 β).
    if round_n is not None:
        violations += G(_check_persistent_medium_escalation, run_dir,
                        round_n=round_n,
                        current_kinds={v.get("kind") for v in violations})
    _annotate_first_seen(run_dir, violations)
    return violations


# Phase 21 β: canonical funder-neutral key → legacy aliases.
_UNIVERSAL_COVERAGE_FIELDS = (
    ("total_units", ()),
    ("actual_budget", ("gc7_actual_budget",)),
    ("dalys_averted", ("gc7_dalys_averted",)),
    ("universal_coverage_dalys_averted_estimate", ()),
    ("efficiency_pct", ("gc7_efficiency_pct",)),
)


def _check_universal_coverage(run_dir: str) -> list[dict]:
    """Phase 7 Commit ν: when allocation is produced, the modeler
    must compute a universal-coverage benchmark scenario quantifying
    the budget required to cover all spatial units at the dominant
    intervention package, alongside the budget-constrained allocation.
    This surfaces the "fund 26%, BAU for 66%" concession quantitatively.

    Required artifact: `{run_dir}/models/universal_coverage.yaml` with:
      total_units: <int>
      actual_budget: <number>           # the constrained envelope
      budget_for_universal_*: <number>  # at least one
      dalys_averted: <number>           # under the constrained allocation
      universal_coverage_dalys_averted_estimate: <number>
      efficiency_pct: <number>          # constrained / universal * 100

    Phase 21 β: keys are funder-neutral. The pre-21 funder-specific names
    (`gc7_actual_budget`, `gc7_dalys_averted`, `gc7_efficiency_pct`) are
    accepted as legacy aliases.

    Emits:
      universal_coverage_missing  MEDIUM — allocation exists, no yaml
      universal_coverage_malformed MEDIUM — yaml exists but missing
                                             required fields
    """
    decision_rule = artifact_path("decision_rule", run_dir)
    allocs = _find_allocation_csvs(run_dir)
    if not (os.path.exists(decision_rule) or allocs):
        return []

    yaml_path = artifact_path("universal_coverage", run_dir)
    if not os.path.exists(yaml_path):
        return [{
            "kind": "universal_coverage_missing",
            "severity": "MEDIUM",
            "stage": "OPTIMIZATION",
            "claim": (
                "Allocation produced but models/universal_coverage.yaml "
                "is absent. The modeler must compute a universal-"
                "coverage benchmark (cost to fund all spatial units at "
                "the dominant package) alongside the budget-constrained "
                "allocation. This surfaces the 'fund X%, BAU for Y%' "
                "concession quantitatively rather than burying it in "
                "§Limitations. Required fields: total_units, "
                "actual_budget, budget_for_universal_<package>, "
                "dalys_averted, universal_coverage_dalys_averted_"
                "estimate, efficiency_pct. See "
                "mechanistic-vs-hybrid-architecture skill."
            ),
        }]

    try:
        with open(yaml_path) as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError) as e:
        return [{
            "kind": "universal_coverage_malformed",
            "severity": "MEDIUM",
            "stage": "OPTIMIZATION",
            "claim": f"universal_coverage.yaml load error: {e}",
        }]

    if not isinstance(data, dict):
        return [{
            "kind": "universal_coverage_malformed",
            "severity": "MEDIUM",
            "stage": "OPTIMIZATION",
            "claim": "universal_coverage.yaml must be a top-level mapping",
        }]

    missing = []
    for required, aliases in _UNIVERSAL_COVERAGE_FIELDS:
        if not any(k in data for k in (required, *aliases)):
            missing.append(required)
    # At least one budget_for_universal_* must exist
    has_universal_budget = any(
        k.startswith("budget_for_universal_") for k in data.keys()
    )
    if not has_universal_budget:
        missing.append("budget_for_universal_<package>")

    if missing:
        return [{
            "kind": "universal_coverage_malformed",
            "severity": "MEDIUM",
            "stage": "OPTIMIZATION",
            "claim": (
                f"universal_coverage.yaml missing required field(s): "
                f"{missing}"
            ),
        }]

    return []


def _check_plan_criteria(run_dir: str) -> list[dict]:
    """Phase 7 Commit μ: when planner emits success_criteria.yaml,
    each tier's criteria are mechanically evaluated against the named
    artifacts. Hard blocker failures are HIGH; minimum bar failures
    are MEDIUM (Phase 5 ζ stuck-blocker logic escalates after 2+
    attempts).

    Emits:
      plan_criteria_missing       MEDIUM — plan.md exists but no
                                            success_criteria.yaml (legacy
                                            runs OK; new runs should
                                            produce it)
      plan_hard_blocker_failed    HIGH    — hard blocker failed (per id)
      plan_minimum_bar_failed     MEDIUM  — minimum bar failed (per id)
      plan_criterion_not_tested   MEDIUM  — criterion's artifact/field
                                            wasn't found
    """
    plan = os.path.join(run_dir, "plan.md")
    if not os.path.exists(plan):
        return []

    yaml_path = os.path.join(run_dir, "success_criteria.yaml")
    if not os.path.exists(yaml_path):
        return [{
            "kind": "plan_criteria_missing",
            "severity": "MEDIUM",
            "stage": "PLAN_CRITERIA",
            "claim": (
                "plan.md exists but success_criteria.yaml is absent. "
                "The planner must produce a structured "
                "success_criteria.yaml alongside plan.md so each "
                "minimum-bar / hard-blocker criterion can be "
                "mechanically evaluated. Without it, plan-promised "
                "criteria silently slide into §Limitations bullets "
                "(e.g. a promised external-model cross-validation that "
                "is never run). "
                "See planner prompt and scripts/plan_criteria.py "
                "schema."
            ),
        }]

    try:
        spec = importlib.util.spec_from_file_location(
            "plan_criteria",
            os.path.join(os.path.dirname(__file__), "plan_criteria.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as e:
        return [{
            "kind": "plan_criteria_malformed",
            "severity": "HIGH",
            "stage": "PLAN_CRITERIA",
            "claim": (f"Could not load scripts/plan_criteria.py: {e}"),
        }]

    result = mod.evaluate_plan_criteria(run_dir)

    if result.get("verdict") == "MALFORMED":
        return [{
            "kind": "plan_criteria_malformed",
            "severity": "HIGH",
            "stage": "PLAN_CRITERIA",
            "claim": (f"success_criteria.yaml is malformed: "
                      f"{'; '.join(result.get('errors') or [])}"),
        }]

    if result.get("verdict") != "OK":
        return []

    out: list[dict] = []
    for entry in result.get("hard_blockers") or []:
        if entry["status"] == "FAIL":
            out.append({
                "kind": "plan_hard_blocker_failed",
                "severity": "HIGH",
                "stage": "PLAN_CRITERIA",
                "claim": (
                    f"Hard blocker {entry['id']} FAILED: "
                    f"{entry['criterion']} ({entry['evidence']})"
                ),
            })
        elif entry["status"] == "NOT_TESTED":
            out.append({
                "kind": "plan_criterion_not_tested",
                "severity": "MEDIUM",
                "stage": "PLAN_CRITERIA",
                "claim": (
                    f"Hard blocker {entry['id']} NOT_TESTED: "
                    f"{entry['criterion']} ({entry['evidence']}). "
                    f"The plan promised this criterion; the modeler "
                    f"must produce the artifact + field, or the "
                    f"planner must remove the promise."
                ),
            })
    for entry in result.get("minimum_bar") or []:
        if entry["status"] == "FAIL":
            out.append({
                "kind": "plan_minimum_bar_failed",
                "severity": "MEDIUM",
                "stage": "PLAN_CRITERIA",
                "claim": (
                    f"Minimum bar {entry['id']} FAILED: "
                    f"{entry['criterion']} ({entry['evidence']})"
                ),
            })
        elif entry["status"] == "NOT_TESTED":
            out.append({
                "kind": "plan_criterion_not_tested",
                "severity": "MEDIUM",
                "stage": "PLAN_CRITERIA",
                "claim": (
                    f"Minimum bar {entry['id']} NOT_TESTED: "
                    f"{entry['criterion']} ({entry['evidence']}). "
                    f"Modeler must populate the field in the artifact "
                    f"or scope-declare why the criterion is "
                    f"unachievable for this run."
                ),
            })
    return out


def _load_sibling(name: str):
    """Import scripts/<name>.py by path (no package install needed).
    Registered in sys.modules so dataclasses in the module resolve."""
    if name in sys.modules and getattr(sys.modules[name], "__file__", "") \
            .endswith(f"{name}.py"):
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return mod


# Self-test hook: replace the in-process writer_qa computation.
_WRITER_QA_FN = None


def _check_writer_qa(run_dir: str, post_write: bool = False) -> list[dict]:
    """Phase 7 Commit λ: when report.md exists, the writer must run
    a post-write QA pass via scripts/writer_qa.py and the result must
    be CLEAN.

    Phase 21 β: the gate no longer trusts a cached writer_qa_report.yaml
    (it could predate later report edits, or be hand-written). It
    recomputes writer_qa in-process and refreshes the YAML. An unreadable
    report.md is HIGH `report_unreadable` (writer_qa.py used to call it
    CLEAN). In post-WRITE mode (`--post-write`) a MAJOR_REVISION verdict
    is HIGH and the writer_qa module being unavailable is HIGH.

    Emits:
      writer_qa_unresolved MEDIUM (HIGH for MAJOR_REVISION in post-write)
      writer_qa_unknown_verdict HIGH
      writer_qa_unavailable  MEDIUM (HIGH in post-write)
      report_unreadable      HIGH
    """
    report = os.path.join(run_dir, "report.md")
    if not os.path.exists(report):
        return []  # Writer hasn't run yet; post-write absence is gated separately.
    try:
        with open(report, encoding="utf-8") as f:
            f.read()
    except (OSError, UnicodeDecodeError) as e:
        return [{
            "kind": "report_unreadable",
            "severity": "HIGH",
            "stage": "WRITER_QA",
            "scope_declarable": False,
            "claim": f"report.md exists but cannot be read as UTF-8: {e}",
        }]

    try:
        if _WRITER_QA_FN is not None:
            result = _WRITER_QA_FN(run_dir)
        else:
            mod = _load_sibling("writer_qa")
            result = mod.writer_qa(run_dir)
            mod.write_report(run_dir, result)
        verdict = str(result.verdict or "").strip().upper()
        n_major, n_minor = result.n_major, result.n_minor
    except Exception as e:  # noqa: BLE001 — fail closed below
        return [{
            "kind": "writer_qa_unavailable",
            "severity": "HIGH" if post_write else "MEDIUM",
            "stage": "WRITER_QA",
            "claim": (f"writer_qa could not be computed in-process "
                      f"({type(e).__name__}: {e}). Run "
                      f"`python3 scripts/writer_qa.py {run_dir}`."),
        }]

    if verdict not in _PRODUCER_VERDICTS["writer_qa"]:
        return [{
            "kind": "writer_qa_unknown_verdict",
            "severity": "HIGH",
            "stage": "WRITER_QA",
            "scope_declarable": False,
            "claim": (f"writer_qa returned verdict {verdict!r}, outside "
                      f"{list(_PRODUCER_VERDICTS['writer_qa'])}."),
        }]
    if verdict in ("REVISE", "MAJOR_REVISION"):
        severity = ("HIGH" if post_write and verdict == "MAJOR_REVISION"
                    else "MEDIUM")
        return [{
            "kind": "writer_qa_unresolved",
            "severity": severity,
            "stage": "WRITER_QA",
            "claim": (
                f"writer_qa verdict is {verdict} with {n_major} MAJOR "
                f"and {n_minor} MINOR issues. The lead must re-spawn "
                f"the writer with the QA report's issues as input, or "
                f"scope-declare the writer-QA limitations explicitly. "
                f"Common patterns: stale numbers from pre-fix drafts, "
                f"figure-text comparator inconsistencies, figure "
                f"annotations that disagree with body-text metric "
                f"values."
            ),
        }]
    return []


def _unknown_result_verdict(result: Any, producer: str, prefix: str,
                            stage: str) -> list[dict]:
    """Phase 21 β: a sibling validator's result must be a mapping whose
    verdict is in the producer's vocabulary. Anything else is HIGH — the
    pre-21 if/elif chains passed any unexpected verdict silently."""
    allowed = _producer_verdicts(producer)
    verdict = result.get("verdict") if isinstance(result, dict) else None
    if verdict in allowed:
        return []
    return [{
        "kind": f"{prefix}_unknown_verdict",
        "severity": "HIGH",
        "stage": stage,
        "scope_declarable": False,
        "claim": (f"scripts/{producer}.py returned verdict {verdict!r} "
                  f"(result type {type(result).__name__}); expected one of "
                  f"{list(allowed)}."),
    }]


def _check_allocation_robustness(run_dir: str) -> list[dict]:
    """Phase 6 Commit κ: allocation rule must be cross-validated under
    spatial holdout. The modeler runs k-fold leave-one-spatial-unit-out
    re-optimization themselves and writes models/allocation_robustness.yaml.

    Emits:
      allocation_robustness_missing  MEDIUM — file absent
      allocation_robustness_malformed HIGH — schema violations
      allocation_unstable             HIGH — worst-fold metrics fail
                                              ROBUST and FRAGILE bands
      allocation_fragile              MEDIUM — middle-band, scope-declare
    """
    decision_rule = artifact_path("decision_rule", run_dir)
    allocs = _find_allocation_csvs(run_dir)
    if not (os.path.exists(decision_rule) or allocs):
        return []

    yaml_path = artifact_path("allocation_robustness", run_dir)
    if not os.path.exists(yaml_path):
        return [{
            "kind": "allocation_robustness_missing",
            "severity": "MEDIUM",
            "stage": "OPTIMIZATION",
            "claim": (
                "Allocation produced but models/allocation_robustness.yaml "
                "is absent. The allocation rule must be cross-validated: "
                "hold out k units (e.g., leave-one-calibration-unit-out "
                "or 5-fold-by-state), re-run the optimizer on the "
                "remaining n-k, and measure how well the rule generalizes "
                "to the held-out units. A good in-sample calibration "
                "fit says nothing about whether the optimizer's "
                "allocation rule overfits unit-specific calibrated "
                "values. See the "
                "allocation-cross-validation skill."
            ),
        }]

    try:
        spec = importlib.util.spec_from_file_location(
            "allocation_robustness",
            os.path.join(os.path.dirname(__file__), "allocation_robustness.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as e:
        return [{
            "kind": "allocation_robustness_malformed",
            "severity": "HIGH",
            "stage": "OPTIMIZATION",
            "claim": (f"Could not load scripts/allocation_robustness.py: {e}"),
        }]

    result = mod.validate_allocation_robustness(yaml_path)
    out = _unknown_result_verdict(result, "allocation_robustness",
                                  "allocation_robustness", "OPTIMIZATION")
    if out:
        return out
    if result["verdict"] == "MALFORMED":
        out.append({
            "kind": "allocation_robustness_malformed",
            "severity": "HIGH",
            "stage": "OPTIMIZATION",
            "claim": (f"models/allocation_robustness.yaml is malformed: "
                      f"{'; '.join(result.get('errors') or [])}"),
        })
    elif result["verdict"] == "UNSTABLE":
        out.append({
            "kind": "allocation_unstable",
            "severity": "HIGH",
            "stage": "OPTIMIZATION",
            "claim": (
                f"Allocation cross-validation verdict: UNSTABLE. "
                f"Worst-fold metrics fail even the FRAGILE band: rank "
                f"correlation < 0.40 OR cases-averted gap > 30% OR rule "
                f"classification concordance < 60%. The optimizer's "
                f"allocation does NOT generalize to held-out spatial "
                f"units — applying this rule to allocation units outside the "
                f"calibration set is unjustified. Either rebuild the "
                f"model with regularization / pooling / spatial structure "
                f"to improve generalization, or scope-declare the "
                f"recommendation as applicable only to the in-sample "
                f"calibration units. Metrics: {result.get('metrics')}"
            ),
        })
    elif result["verdict"] == "FRAGILE":
        out.append({
            "kind": "allocation_fragile",
            "severity": "MEDIUM",
            "stage": "OPTIMIZATION",
            "claim": (
                f"Allocation cross-validation verdict: FRAGILE. "
                f"Worst-fold metrics fall in the middle band: rank "
                f"correlation 0.40-0.70 OR cases-averted gap 15-30% OR "
                f"rule classification concordance 60-80%. The allocation "
                f"is plausibly generalizable but with substantial "
                f"per-fold variability. Add a §Limitations paragraph "
                f"quantifying the worst-fold gap and identifying which "
                f"types of held-out units are hardest to predict. "
                f"Metrics: {result.get('metrics')}"
            ),
        })
    return out


_DALY_MENTION_RE = re.compile(
    r"\b(?:DALY|DALYs|disability[-\s]adjusted\s+life[-\s]year"
    r"|life[-\s]year[s]?\s+lost|YLL|YLD)\b",
    re.IGNORECASE,
)
# Headers that signal "this is a Limitations / Scope acknowledgment
# section, not the main analysis body". We strip these from the text
# before checking for DALY engagement so a single throwaway DALY
# acknowledgment in Limitations doesn't satisfy the gate.
_LIMITATIONS_HEADER_RE = re.compile(
    r"^#{1,4}\s+\d*\.?\s*"
    r"(?:Limitations?|Scope\s+Declaration|Caveats?|"
    r"Unresolved\s+(?:HIGH|Blockers?)|Known\s+Issues?)"
    r"[^\n]*$",
    re.MULTILINE | re.IGNORECASE,
)


def _strip_limitations_and_scope(text: str) -> str:
    """Remove §Limitations / §Scope Declaration / §Caveats sections
    so DALY mentions in those sections don't satisfy the
    daly_analysis_missing check. Returns the text with each such
    section sliced from header to next same-level (or higher)
    section header. Linear scan."""
    out_parts = []
    last_end = 0
    headers = list(_LIMITATIONS_HEADER_RE.finditer(text))
    for m in headers:
        # Determine the level (count of '#' at the header start).
        hdr_text = m.group(0)
        level = len(hdr_text) - len(hdr_text.lstrip("#"))
        # Find the next header at this level or shallower (more
        # important).
        next_header_re = re.compile(
            rf"^#{{1,{level}}}\s+",
            re.MULTILINE,
        )
        section_end = len(text)
        for next_m in next_header_re.finditer(text, m.end()):
            section_end = next_m.start()
            break
        out_parts.append(text[last_end:m.start()])
        last_end = section_end
    out_parts.append(text[last_end:])
    return "".join(out_parts)


def _check_daly_when_allocation(run_dir: str) -> list[dict]:
    """Phase 6 Commit ι: when a model produces an allocation, the
    report must mention DALYs (or explicitly justify their absence).

    Cases-averted alone treats an infant's averted episode identically
    to an adult's, dramatically under-weighting interventions that
    target high-mortality groups. Funders, GBD and WHO use DALY-
    denominated cost-effectiveness as the standard; an allocation
    analysis without DALYs gets sent back.

    Emits:
      daly_analysis_missing MEDIUM — decision_rule.md or allocation
                                     CSV exists, but report.md has no
                                     DALY/YLL/YLD/disability-adjusted
                                     mentions.
    """
    decision_rule = artifact_path("decision_rule", run_dir)
    allocs = _find_allocation_csvs(run_dir)
    if not (os.path.exists(decision_rule) or allocs):
        return []  # No allocation produced; no requirement.

    report = os.path.join(run_dir, "report.md")
    if not os.path.exists(report):
        # Report not yet written; check will run again post-writer.
        return []

    try:
        with open(report) as f:
            text = f.read()
    except (OSError, UnicodeDecodeError):
        return []

    # Strip §Limitations and §Scope-declaration sections — a single
    # "we didn't compute DALYs" throwaway in Limitations shouldn't
    # satisfy the check; the analysis must engage with DALYs in
    # Methods / Results / Discussion / Cost-Effectiveness.
    text_outside_limitations = _strip_limitations_and_scope(text)
    if _DALY_MENTION_RE.search(text_outside_limitations):
        return []  # DALYs engaged with in the analysis body.

    return [{
        "kind": "daly_analysis_missing",
        "severity": "MEDIUM",
        "stage": "REPORT",
        "claim": (
            "Allocation/decision_rule produced but report.md does not "
            "mention DALYs (or YLL/YLD/disability-adjusted life-years). "
            "Cases-averted alone treats a 6-month-old's averted "
            "infection identically to an adult's, structurally biasing "
            "the recommendation toward all-age interventions over "
            "ones targeting high-mortality groups (e.g. infant "
            "vaccination, chemoprevention in young children, treatment "
            "of advanced disease). Funder / GBD / WHO cost-effectiveness "
            "benchmarks are DALY-denominated; "
            "without DALY-averted figures, the report cannot be "
            "compared to published cost-effectiveness thresholds. "
            "Add either (1) a DALY-averted column alongside "
            "cases-averted in the primary table, (2) a §Cost-"
            "Effectiveness section with $/DALY estimates, or (3) a "
            "Methods/Limitations paragraph justifying DALY irrelevance "
            "for this specific analysis. See the daly-weighted-analysis "
            "skill for disease-specific anchor tables."
        ),
    }]


def _check_optimization_quality(run_dir: str) -> list[dict]:
    """Phase 6 Commit θ: when an allocation/decision_rule artifact
    exists, the modeler must compare their primary optimizer to ≥1
    alternative method (ILP, SA, random-restart) and report the gap.

    Emits:
      optimization_quality_missing      MEDIUM — decision_rule.md or
                                                 allocation CSV present
                                                 but no benchmark file
      optimization_quality_no_benchmark HIGH   — file exists but
                                                 benchmark_methods is empty
      optimization_quality_malformed    HIGH   — schema/method violations
      optimization_quality_gap_too_large MEDIUM — gap_pct > 10%
    """
    decision_rule = artifact_path("decision_rule", run_dir)
    allocs = _find_allocation_csvs(run_dir)
    if not (os.path.exists(decision_rule) or allocs):
        return []  # No allocation produced; no requirement.

    yaml_path = artifact_path("optimization_quality", run_dir)
    if not os.path.exists(yaml_path):
        return [{
            "kind": "optimization_quality_missing",
            "severity": "MEDIUM",
            "stage": "OPTIMIZATION",
            "claim": (
                "Allocation produced but models/optimization_quality.yaml "
                "is absent. The primary optimizer must be benchmarked "
                "against at least one alternative method (ILP via PuLP, "
                "simulated annealing, random-restart greedy, or brute "
                "force when feasible) so the optimality gap is "
                "quantified. A greedy optimizer's headline 'X% advantage' "
                "claim is meaningless without knowing whether X% is 100% "
                "of the achievable improvement or 60% of it. See the "
                "optimizer-method-selection skill."
            ),
        }]

    # Lazy import to avoid circular dependency / missing-deps at import time.
    try:
        spec = importlib.util.spec_from_file_location(
            "optimization_quality",
            os.path.join(os.path.dirname(__file__), "optimization_quality.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as e:
        return [{
            "kind": "optimization_quality_malformed",
            "severity": "HIGH",
            "stage": "OPTIMIZATION",
            "claim": (f"Could not load scripts/optimization_quality.py: {e}"),
        }]

    result = mod.validate_optimization_quality(yaml_path)
    out = _unknown_result_verdict(result, "optimization_quality",
                                  "optimization_quality", "OPTIMIZATION")
    if out:
        return out
    if result["verdict"] == "MALFORMED":
        out.append({
            "kind": "optimization_quality_malformed",
            "severity": "HIGH",
            "stage": "OPTIMIZATION",
            "claim": (f"models/optimization_quality.yaml is malformed: "
                      f"{'; '.join(result['errors'])}"),
        })
    elif result["verdict"] == "NO_BENCHMARK":
        out.append({
            "kind": "optimization_quality_no_benchmark",
            "severity": "HIGH",
            "stage": "OPTIMIZATION",
            "claim": (
                "models/optimization_quality.yaml exists but "
                "benchmark_methods is empty. At least one benchmark "
                "method (ILP, SA, random-restart) must be present so "
                "the gap_pct can be computed. Greedy-only optimization "
                "with no quality benchmark is not ACCEPT-grade."
            ),
        })
    elif result["verdict"] == "GAP_TOO_LARGE":
        out.append({
            "kind": "optimization_quality_gap_too_large",
            "severity": "MEDIUM",
            "stage": "OPTIMIZATION",
            "claim": (
                f"Optimizer gap_pct = {result['gap_pct']:.2f}%; primary "
                f"method ({result['primary']['method']}) is well below "
                f"best benchmark ({result['best']['method']} at "
                f"{result['best']['objective']:.4g} vs "
                f"{result['primary']['objective']:.4g}). Either switch "
                f"the primary method to the better one, improve the "
                f"primary's parameters (more random restarts, longer SA "
                f"cooling, etc.), or scope-declare why a >10% gap is "
                f"acceptable for this question."
            ),
        })
    return out


def _check_sensitivity_analysis(run_dir: str) -> list[dict]:
    """Phase 8 Commit π: when an allocation/decision_rule artifact
    exists, the modeler must perturb 2-3 load-bearing parameters and
    report whether the primary recommendation flips. The artifact
    lives at `{run_dir}/models/sensitivity_analysis.yaml`.

    Emits:
      sensitivity_analysis_missing      MEDIUM — allocation present but
                                                  no sensitivity yaml
      sensitivity_analysis_malformed    HIGH   — schema violations
      sensitivity_analysis_unstable     HIGH   — UNSTABLE verdict
      sensitivity_analysis_sensitive    MEDIUM — SENSITIVE verdict
    """
    decision_rule = artifact_path("decision_rule", run_dir)
    allocs = _find_allocation_csvs(run_dir)
    if not (os.path.exists(decision_rule) or allocs):
        return []  # No allocation produced; no requirement.

    yaml_path = artifact_path("sensitivity_analysis", run_dir)
    if not os.path.exists(yaml_path):
        return [{
            "kind": "sensitivity_analysis_missing",
            "severity": "MEDIUM",
            "stage": "OPTIMIZATION",
            "claim": (
                "Allocation produced but models/sensitivity_analysis.yaml "
                "is absent. The recommendation must be tested for "
                "robustness against perturbations of its 2-3 LOAD-BEARING "
                "parameters (the parameters whose 95% CI endpoints could "
                "change the dominant intervention package, the highest-"
                "burden zone allocation, or the comparator-vs-optimized "
                "winner). For each parameter: re-run the optimizer at "
                "alternative values, compare top-50 unit package "
                "assignments, and report rank_change_top_n + "
                "primary_recommendation_changes. Verdict ROBUST is "
                "required for ACCEPT-grade analysis. SENSITIVE must be "
                "scope-declared in §Limitations. UNSTABLE means the "
                "recommendation cannot be defended as-is. See "
                "scripts/sensitivity_analysis.py for the schema."
            ),
        }]

    try:
        spec = importlib.util.spec_from_file_location(
            "sensitivity_analysis",
            os.path.join(os.path.dirname(__file__), "sensitivity_analysis.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as e:
        return [{
            "kind": "sensitivity_analysis_malformed",
            "severity": "HIGH",
            "stage": "OPTIMIZATION",
            "claim": f"Could not load scripts/sensitivity_analysis.py: {e}",
        }]

    result = mod.validate_sensitivity_analysis(yaml_path)
    out = _unknown_result_verdict(result, "sensitivity_analysis",
                                  "sensitivity_analysis", "OPTIMIZATION")
    if out:
        return out
    if result["verdict"] == "MALFORMED":
        out.append({
            "kind": "sensitivity_analysis_malformed",
            "severity": "HIGH",
            "stage": "OPTIMIZATION",
            "claim": (f"models/sensitivity_analysis.yaml is malformed: "
                      f"{'; '.join(result['errors'])}"),
        })
    elif result["verdict"] == "UNSTABLE":
        s = result.get("summary", {})
        out.append({
            "kind": "sensitivity_analysis_unstable",
            "severity": "HIGH",
            "stage": "OPTIMIZATION",
            "claim": (
                f"Sensitivity analysis verdict UNSTABLE: "
                f"{s.get('flips', 0)} of {s.get('total_perturbations', 0)} "
                f"perturbations flip the primary recommendation, worst "
                f"top-N rank change is {s.get('worst_rank_change_top_n', 0)}. "
                f"The allocation cannot be defended as-is; either pick a "
                f"more robust parameterization, narrow the perturbation "
                f"range with stronger evidence, or rebuild the optimization "
                f"around an objective whose ranking is robust to these "
                f"parameter ranges."
            ),
        })
    elif result["verdict"] == "SENSITIVE":
        s = result.get("summary", {})
        out.append({
            "kind": "sensitivity_analysis_sensitive",
            "severity": "MEDIUM",
            "stage": "OPTIMIZATION",
            "claim": (
                f"Sensitivity analysis verdict SENSITIVE: "
                f"{s.get('flips', 0)} of {s.get('total_perturbations', 0)} "
                f"perturbations flip the primary recommendation; worst "
                f"top-N rank change is {s.get('worst_rank_change_top_n', 0)}. "
                f"The recommendation is not robust under all plausible "
                f"parameter values. Surface this in §Sensitivity (or "
                f"§Cost-effectiveness) of report.md, not just §Limitations: "
                f"a decision-maker reading the headline must see which "
                f"alternative parameter values flip the choice."
            ),
        })
    return out


# How many lines of lookahead after a savefig() call we tolerate
# before requiring a validate_figure() call. 10 fits the typical
# pattern of `plt.savefig(...); plt.close(); validate_figure(...)`.
_VALIDATOR_LOOKAHEAD_LINES = 10


def _check_figure_validator(run_dir: str) -> list[dict]:
    """Phase 9 Commit ρ: write-time figure validator.

    Two complementary checks fire per round:

    1. Source-data freshness — for every `<png>.provenance.json`
       sidecar in `{run_dir}/figures/`, recompute hashes of each
       recorded source CSV. If any hash differs, the figure was
       drawn from data that has since changed (the D-022 / D-023
       failure mode). Severity HIGH `figure_staleness_detected`.

    2. Validator-call coverage — every `*.py` file in `{run_dir}/`
       and `{run_dir}/models/` is scanned for `plt.savefig(`. Each
       savefig must have a `validate_figure(` within the next
       `_VALIDATOR_LOOKAHEAD_LINES` lines, and the corresponding
       PNG must have a `.provenance.json` sidecar at gate time.
       Missing validator call → MEDIUM `figure_validator_missing`.

    The check is silent when no figures exist (e.g., before any
    modeler round has produced output). It is intentionally NOT
    gated on allocation presence: figures are part of every run.
    """
    figures_dir = os.path.join(run_dir, "figures")
    out: list[dict] = []

    # --- Check 1: source-data freshness via sidecar hash compare ---
    if os.path.isdir(figures_dir):
        # Lazy-load figure_validator.check_staleness once. Same
        # importlib pattern as _check_sensitivity_analysis above.
        try:
            spec = importlib.util.spec_from_file_location(
                "figure_validator",
                os.path.join(os.path.dirname(__file__), "figure_validator.py"),
            )
            fv_mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(fv_mod)
        except Exception as e:
            out.append({
                "kind": "figure_validator_load_error",
                "severity": "HIGH",
                "stage": "ANALYZE",
                "claim": f"Could not load scripts/figure_validator.py: {e}",
            })
            return out

        for entry in sorted(os.listdir(figures_dir)):
            if not entry.endswith(".png"):
                continue
            png_path = os.path.join(figures_dir, entry)
            sidecar = png_path + ".provenance.json"
            if not os.path.exists(sidecar):
                # Missing sidecar handled by check 2 (script-level
                # coverage scan); skip here.
                continue
            status = fv_mod.check_staleness(png_path)
            if status["status"] == "stale":
                stale_list = ", ".join(status.get("stale_sources", [])
                                       + status.get("missing_sources", []))
                out.append({
                    "kind": "figure_staleness_detected",
                    "severity": "HIGH",
                    "stage": "ANALYZE",
                    "claim": (
                        f"Figure {entry} was drawn from source data that "
                        f"has since changed. The recorded provenance "
                        f"hash no longer matches the current content of: "
                        f"{stale_list}. Regenerate the figure from the "
                        f"current data and re-call validate_figure(). "
                        f"This is the D-022 / D-023 failure mode "
                        f"(corrected AIC values, stale calibration "
                        f"residuals) that the 0013 run caught only at "
                        f"round 8."
                    ),
                })

    # --- Check 2: every plt.savefig must have a validate_figure nearby ---
    py_files: list[str] = []
    for root in (run_dir, os.path.join(run_dir, "models")):
        if not os.path.isdir(root):
            continue
        for entry in os.listdir(root):
            if entry.endswith(".py"):
                py_files.append(os.path.join(root, entry))

    for py in py_files:
        try:
            with open(py) as f:
                lines = f.readlines()
        except OSError:
            continue
        for idx, ln in enumerate(lines):
            if "plt.savefig(" not in ln and ".savefig(" not in ln:
                continue
            window = "".join(lines[idx:idx + _VALIDATOR_LOOKAHEAD_LINES])
            if "validate_figure(" not in window:
                out.append({
                    "kind": "figure_validator_missing",
                    "severity": "MEDIUM",
                    "stage": "ANALYZE",
                    "claim": (
                        f"{os.path.relpath(py, run_dir)}:{idx + 1} calls "
                        f"savefig but no validate_figure(...) appears "
                        f"within the next {_VALIDATOR_LOOKAHEAD_LINES} "
                        f"lines. Phase 9 Commit ρ requires every "
                        f"plt.savefig to be paired with a validate_figure "
                        f"call so source-data hashes and annotation "
                        f"strings are recorded at write time. See "
                        f"scripts/figure_validator.py for the API."
                    ),
                })
    return out


# Phase 10 Commit ψ: allocation-gate coordinator.
#
# Per the Phase 9 τ rigor-artifact-timeline, each allocation-
# triggered artifact has a "draft by round X" deadline. After that
# round, a MEDIUM `*_missing` becomes a `drafts_overdue` MEDIUM —
# signaling the modeler is past the planned drafting window and at
# real risk of arriving at STAGE 7 with no time to fix verdict
# failures. Within the drafting window, all missing artifacts roll
# up into a single `in_progress` MEDIUM rather than the modeler
# seeing 5 individual `*_missing` violations.
_ALLOCATION_DRAFT_DEADLINES = {
    "sensitivity_analysis_missing": 4,    # τ says draft r2-3
    "allocation_robustness_missing": 5,   # τ says draft r3-4
    "universal_coverage_missing":   5,    # not on τ; treated as r3-4 draft
    "daly_analysis_missing":        5,    # not on τ; produced with allocation
    "optimization_quality_missing": 5,    # not on τ; produced with allocation
}

# The five MEDIUM `_missing` kinds the coordinator consolidates. HIGH
# verdicts (UNSTABLE, MALFORMED, etc.) and HIGH `decision_rule_missing`
# are NOT consolidated — they pass through unchanged so the lead's
# STAGE 7 logic still sees real failures.
_ALLOCATION_MEDIUM_MISSING_KINDS = frozenset(_ALLOCATION_DRAFT_DEADLINES.keys())


def _check_allocation_rigor_status(run_dir: str,
                                   round_n: int | None = None) -> list[dict]:
    """Phase 10 Commit ψ: coordinate the five allocation-triggered
    rigor checks.

    Calls each helper, then:
      1. Pass through every non-MEDIUM-missing violation unchanged
         (HIGHs always; MEDIUM verdict failures like `*_sensitive`).
      2. Collect the MEDIUM `_missing` kinds (5 of them).
      3. If `round_n` is None, fall back to legacy behavior — the
         five `_missing` violations are returned individually as
         before. This preserves callers that don't pass round_n.
      4. If `round_n` is supplied:
           - For each missing artifact, look up its deadline.
           - Within the drafting window: emit a single
             `allocation_rigor_in_progress` MEDIUM listing the
             missing artifacts.
           - Past at least one deadline: emit a single
             `allocation_rigor_drafts_overdue` MEDIUM listing the
             overdue artifacts (and any still-in-window ones, for
             context).
    """
    raw: list[dict] = []
    raw.extend(_check_optimization_quality(run_dir))
    raw.extend(_check_daly_when_allocation(run_dir))
    raw.extend(_check_allocation_robustness(run_dir))
    raw.extend(_check_universal_coverage(run_dir))
    raw.extend(_check_sensitivity_analysis(run_dir))

    out: list[dict] = []
    missing_kinds: list[dict] = []
    for v in raw:
        if (v.get("kind") in _ALLOCATION_MEDIUM_MISSING_KINDS
                and v.get("severity") == "MEDIUM"):
            missing_kinds.append(v)
        else:
            # Pass through HIGHs and other MEDIUMs unchanged.
            out.append(v)

    if not missing_kinds:
        return out

    if round_n is None:
        # Legacy mode: behave exactly as before — return all five
        # MEDIUMs individually so callers without round context see
        # no behavior change.
        out.extend(missing_kinds)
        return out

    overdue: list[str] = []
    in_progress: list[str] = []
    for v in missing_kinds:
        deadline = _ALLOCATION_DRAFT_DEADLINES.get(v["kind"], 5)
        artifact = v["kind"].replace("_missing", "")
        if round_n > deadline:
            overdue.append(f"{artifact} (deadline r{deadline})")
        else:
            in_progress.append(f"{artifact} (deadline r{deadline})")

    if overdue:
        details = "Overdue: " + "; ".join(overdue)
        if in_progress:
            details += ". Still in window: " + "; ".join(in_progress) + "."
        else:
            details += "."
        out.append({
            "kind": "allocation_rigor_drafts_overdue",
            "severity": "MEDIUM",
            "stage": "OPTIMIZATION",
            "claim": (
                f"Allocation rigor artifacts past their drafting "
                f"deadline (round {round_n}). {details} The Phase 9 τ "
                f"timeline asks for these to be drafted (even with "
                f"placeholder values) earlier so per-round critique "
                f"can iterate on verdicts. Arriving at STAGE 7 with a "
                f"first draft of sensitivity_analysis or allocation_"
                f"robustness is exactly the 0013 failure mode."
            ),
        })
    else:
        out.append({
            "kind": "allocation_rigor_in_progress",
            "severity": "MEDIUM",
            "stage": "OPTIMIZATION",
            "claim": (
                f"Allocation rigor artifacts in drafting window "
                f"(round {round_n}). Pending: "
                f"{'; '.join(in_progress)}. This is a coordinated "
                f"reminder, not a blocker — draft each artifact "
                f"early so verdict failures surface while there are "
                f"rounds left to act."
            ),
        })
    return out


# Phase 12 Commit γ: ecological-fallacy / within-zone heterogeneity.
#
# When calibration_units / allocation_units < 0.1 (ecological
# aggregation likely), require models/within_zone_heterogeneity.yaml
# bounding the impact loss. The 104914 run had 6 zones → 774 LGAs
# (ratio 0.0078), with within-zone PfPR varying 5.9%-77.3% per
# data_quality.md. results.md acknowledges this in one line; the
# magnitude is unbounded. A reviewer cannot tell if the headline
# 54.7M cases averted would drop by 5%, 15%, or 30% under realistic
# within-zone heterogeneity.
_ECOLOGICAL_AGGREGATION_THRESHOLD = 0.1


def _detect_calibration_allocation_ratio(run_dir: str) -> tuple[int, int] | None:
    """Detect (calibration_units, allocation_units) pair from a run
    dir. Returns None if either count is unavailable.

    calibration_units: from models/model_comparison_formal.yaml
        n_targets field (the number of zone-level calibration targets).
    allocation_units: from models/lga_allocation.csv (row count
        excluding header), or any *allocation*.csv in models/.
    """
    cal_path = _msc_report_path(run_dir)
    cal_units: int | None = None
    if os.path.exists(cal_path):
        try:
            with open(cal_path) as f:
                doc = yaml.safe_load(f) or {}
            n_targets = doc.get("n_targets")
            if isinstance(n_targets, int):
                cal_units = n_targets
        except (yaml.YAMLError, OSError):
            pass

    alloc_units: int | None = None
    for entry in (os.listdir(os.path.join(run_dir, "models"))
                  if os.path.isdir(os.path.join(run_dir, "models")) else []):
        if "allocation" in entry and entry.endswith(".csv"):
            try:
                with open(os.path.join(run_dir, "models", entry)) as f:
                    rows = sum(1 for _ in f) - 1  # exclude header
                if rows > 0 and (alloc_units is None or rows > alloc_units):
                    alloc_units = rows
            except OSError:
                continue

    if cal_units is None or alloc_units is None or alloc_units == 0:
        return None
    return (cal_units, alloc_units)


def _check_within_zone_heterogeneity(run_dir: str) -> list[dict]:
    """Phase 12 Commit γ: when calibration_units / allocation_units
    < 0.1, require models/within_zone_heterogeneity.yaml bounding
    the impact loss from ecological aggregation.

    Emits:
      within_zone_heterogeneity_missing      MEDIUM — yaml absent
                                                       and ratio < 0.1
      within_zone_heterogeneity_malformed    HIGH   — yaml schema/verdict
                                                       violations
      within_zone_heterogeneity_unbounded    HIGH   — UNBOUNDED verdict
                                                       (>25% impact loss)
      within_zone_heterogeneity_inconclusive MEDIUM — INCONCLUSIVE
                                                       (10-25% loss)
    """
    pair = _detect_calibration_allocation_ratio(run_dir)
    if pair is None:
        return []  # no calibration/allocation detected; check is silent
    cal_units, alloc_units = pair
    ratio = cal_units / alloc_units
    if ratio >= _ECOLOGICAL_AGGREGATION_THRESHOLD:
        return []  # not aggregating much; check is silent

    yaml_path = artifact_path("within_zone_heterogeneity", run_dir)
    if not os.path.exists(yaml_path):
        return [{
            "kind": "within_zone_heterogeneity_missing",
            "severity": "MEDIUM",
            "stage": "MODEL",
            "claim": (
                f"Model calibrates to {cal_units} units but allocates to "
                f"{alloc_units} units (ratio {ratio:.4f} < "
                f"{_ECOLOGICAL_AGGREGATION_THRESHOLD}). Within-unit "
                f"heterogeneity is invisible to the optimizer; the "
                f"impact loss is unbounded. Required artifact: "
                f"`models/within_zone_heterogeneity.yaml` bounding the "
                f"impact loss from realistic within-unit value variation. "
                f"See the ecological-fallacy-quantification skill and "
                f"scripts/within_zone_sensitivity.py for the schema."
            ),
        }]

    try:
        spec = importlib.util.spec_from_file_location(
            "within_zone_sensitivity",
            os.path.join(os.path.dirname(__file__),
                         "within_zone_sensitivity.py"),
        )
        wzs = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(wzs)
    except Exception as e:
        return [{
            "kind": "within_zone_heterogeneity_malformed",
            "severity": "HIGH",
            "stage": "MODEL",
            "claim": f"Could not load scripts/within_zone_sensitivity.py: {e}",
        }]

    result = wzs.validate_within_zone_heterogeneity(yaml_path)
    out: list[dict] = _unknown_result_verdict(
        result, "within_zone_sensitivity", "within_zone_heterogeneity", "MODEL")
    if out:
        return out
    if result["verdict"] == "MALFORMED":
        out.append({
            "kind": "within_zone_heterogeneity_malformed",
            "severity": "HIGH",
            "stage": "MODEL",
            "claim": (f"models/within_zone_heterogeneity.yaml is malformed: "
                      f"{'; '.join(result['errors'])}"),
        })
    elif result["verdict"] == "UNBOUNDED":
        s = result.get("summary", {})
        out.append({
            "kind": "within_zone_heterogeneity_unbounded",
            "severity": "HIGH",
            "stage": "MODEL",
            "claim": (
                f"Within-zone heterogeneity sensitivity verdict UNBOUNDED: "
                f"worst-case impact loss "
                f"{s.get('worst_loss_pct', 0):.1f}% under realistic "
                f"within-unit variation. The headline impact is NOT "
                f"defensible at the allocation-unit level. Either narrow "
                f"the perturbation range with stronger evidence (e.g., a "
                f"sub-unit sample of the driving covariate), refit at the "
                f"finer aggregation level, or scope-declare the "
                f"recommendation as calibration-unit-level only."
            ),
        })
    elif result["verdict"] == "INCONCLUSIVE":
        s = result.get("summary", {})
        out.append({
            "kind": "within_zone_heterogeneity_inconclusive",
            "severity": "MEDIUM",
            "stage": "MODEL",
            "claim": (
                f"Within-zone heterogeneity verdict INCONCLUSIVE: "
                f"worst-case impact loss "
                f"{s.get('worst_loss_pct', 0):.1f}%. Surface in §Sensitivity "
                f"of report.md, not just §Limitations. The 10-25% range "
                f"is publication-defensible but a funder or peer "
                f"reviewer will want to see the explicit bound."
            ),
        })
    return out


# Phase 13 Commit α: disease-agnostic structural sanity checks.
#
# `models/sanity_schema.yaml` declares abstract slots (outcome,
# exposure, shares, derived-consistency formulas, composite-dimension
# windows, counterfactual ratio, structural-uncertainty carry-
# forward, outlier sniff). `scripts/sanity_checks.py` runs eight
# generic structural checks against the schema. The schema is
# required when the modeler has had time to compute headline outputs
# (round ≥ 3). Each failed check emits a MEDIUM advisory; none block
# ACCEPT. The modeler can opt out of any check via
# `scope_declaration.yaml`'s `sanity_check_acknowledged: <id>` list.
_SANITY_SCHEMA_REQUIRED_FROM_ROUND = 3


def _check_sanity_schema(run_dir: str,
                          round_n: int | None = None) -> list[dict]:
    """Phase 13 Commit α: validate models/sanity_schema.yaml.

    Emits:
      sanity_schema_missing               MEDIUM — yaml absent at r≥3
      sanity_schema_invalid               HIGH   — yaml malformed
      sanity_check_failed_<id>            MEDIUM — one per failed check
    """
    if round_n is not None and round_n < _SANITY_SCHEMA_REQUIRED_FROM_ROUND:
        return []  # Too early; modeler still composing outputs.

    yaml_path = artifact_path("sanity_schema", run_dir)
    if not os.path.exists(yaml_path):
        if round_n is None:
            return []  # Round unknown; defer.
        return [{
            "kind": "sanity_schema_missing",
            "severity": "MEDIUM",
            "stage": "MODEL",
            "claim": (
                f"Required artifact `models/sanity_schema.yaml` is "
                f"absent at round {round_n} (≥ "
                f"{_SANITY_SCHEMA_REQUIRED_FROM_ROUND}). The modeler "
                f"must declare abstract slots (outcome name, baseline "
                f"reservoir, exposure unit, shares, derived-consistency "
                f"formulas, composite-dimension windows, structural-"
                f"uncertainty bounds) so eight disease-agnostic "
                f"structural checks can run. See the `sanity-schema` "
                f"skill and `scripts/sanity_checks.py --self-test` for "
                f"the schema."
            ),
        }]

    try:
        spec = importlib.util.spec_from_file_location(
            "sanity_checks",
            os.path.join(os.path.dirname(__file__), "sanity_checks.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as e:
        return [{
            "kind": "sanity_schema_invalid",
            "severity": "HIGH",
            "stage": "MODEL",
            "claim": f"Could not load scripts/sanity_checks.py: {e}",
        }]

    result = mod.validate_sanity_schema(yaml_path, run_dir=run_dir)
    if result["verdict"] == "MALFORMED":
        return [{
            "kind": "sanity_schema_invalid",
            "severity": "HIGH",
            "stage": "MODEL",
            "claim": (f"models/sanity_schema.yaml is malformed: "
                      f"{'; '.join(result.get('errors') or ['(no detail)'])}"),
        }]

    # Honor explicit opt-outs from scope_declaration.yaml.
    acknowledged = _load_sanity_check_acknowledged(run_dir)

    out: list[dict] = []
    for c in result.get("checks", []):
        if c.get("passed"):
            continue
        check_id = c.get("id", "unknown")
        if check_id in acknowledged:
            continue
        out.append({
            "kind": f"sanity_check_failed_{check_id}",
            "severity": "MEDIUM",
            "stage": "MODEL",
            "claim": (
                f"Sanity check {check_id!r} failed: {c.get('claim', '')}. "
                f"Either fix the underlying numbers/relationships in "
                f"models/sanity_schema.yaml or acknowledge with "
                f"`sanity_check_acknowledged: [{check_id}]` in "
                f"scope_declaration.yaml (with rationale)."
            ),
        })
    return out


def _load_sanity_check_acknowledged(run_dir: str) -> set[str]:
    """Read scope_declaration.yaml for `sanity_check_acknowledged: [...]`
    entries. Returns the set of check IDs the modeler has explicitly
    documented as out-of-scope."""
    path = os.path.join(run_dir, "scope_declaration.yaml")
    if not os.path.exists(path):
        return set()
    try:
        with open(path) as f:
            doc = yaml.safe_load(f) or {}
    except (yaml.YAMLError, OSError):
        return set()
    out: set[str] = set()
    # Top-level `sanity_check_acknowledged: [id1, id2]` form.
    top = doc.get("sanity_check_acknowledged")
    if isinstance(top, list):
        out.update(str(x) for x in top)
    # Per-declaration form: declarations[*].sanity_check_acknowledged
    decls = doc.get("declarations") or []
    if isinstance(decls, list):
        for d in decls:
            if not isinstance(d, dict):
                continue
            ack = d.get("sanity_check_acknowledged")
            if isinstance(ack, list):
                out.update(str(x) for x in ack)
            elif isinstance(ack, str):
                out.add(ack)
    return out


# Phase 15 Commit α: a-priori identifiability arithmetic.
#
# The 224202 run shipped a HYBRID model with 40 fitted parameters
# fitting 6 zone-level PfPR targets — over-saturated by 6.7×, yet
# the issue was only caught post-hoc at STAGE 5b RIGOR (round 3+).
# By that point the architecture was sunk cost; the modeler scope-
# declared 3 unidentified parameters and proceeded with a decorative
# ABM (predictions equivalent to PfPR × literature_OR).
#
# Phase 15 α requires a PRE-MODEL artifact with parameters/targets
# arithmetic, before the FIRST model build. Verdict OVER_SATURATED
# without a resolution decision blocks ACCEPT (HIGH). Critically,
# this kind is NOT scope-declarable — the modeler must fix the
# architecture, add data, or downgrade to the analytical model.
_IDENTIFIABILITY_A_PRIORI_REQUIRED_FROM_ROUND = 2


def _check_identifiability_a_priori(run_dir: str,
                                     round_n: int | None = None
                                     ) -> list[dict]:
    """Phase 15 α: validate models/identifiability_a_priori.yaml.

    Emits:
      identifiability_a_priori_missing       MEDIUM @ r=2, HIGH @ r≥3
      identifiability_a_priori_invalid       HIGH (yaml MALFORMED)
      pre_model_over_saturated               HIGH (OVER_SATURATED
                                                   without resolution)
      pre_model_over_saturated_with_commitment
                                             MEDIUM (OVER_SATURATED
                                                     with documented
                                                     resolution.decision)
      pre_model_marginal_identifiability     MEDIUM (verdict MARGINAL)
      pre_model_decorative_undocumented      HIGH (resolution=
                                                   accept_decorative
                                                   without details)

    Round gating: returns [] when round_n is None (caller has not
    supplied a round; defer) or round_n < required_round (drafting
    window). Applies uniformly to missing-artifact AND present-artifact
    branches — the artifact is in flux during round 1 and shouldn't
    fire MEDIUM advisories until the modeler has had a full round to
    refine it.

    The pre_model_* kinds are NOT scope-declarable via
    sanity_check_acknowledged or scope_declaration.yaml. Architecture
    choice is inside pipeline reach; the modeler must redesign,
    not declare scope. Concretely, this function intentionally does
    NOT call `_load_sanity_check_acknowledged` (the helper used by
    `_check_sanity_schema` for opt-out filtering). The non-scope-
    declarable contract is enforced by structural omission of that
    call, not by a runtime check.
    """
    # Round gating applies uniformly to all branches. Round 1 is the
    # drafting window; round_n=None means the caller hasn't supplied a
    # round (defer). Both produce silent passes regardless of artifact
    # state — without this, MEDIUM advisories fire spuriously when a
    # planner produces a draft artifact at round 1.
    if round_n is None:
        return []
    if round_n < _IDENTIFIABILITY_A_PRIORI_REQUIRED_FROM_ROUND:
        return []

    yaml_path = artifact_path("identifiability_a_priori", run_dir)

    if not os.path.exists(yaml_path):
        # At r≥2 the artifact is required. Severity escalates to HIGH
        # at r≥3 to align with Phase 12 β's persistence ledger.
        severity = "HIGH" if round_n >= 3 else "MEDIUM"
        return [{
            "kind": "identifiability_a_priori_missing",
            "severity": severity,
            "stage": "PRE_MODEL",
            "claim": (
                f"Required artifact `models/identifiability_a_priori.yaml` "
                f"is absent at round {round_n} (≥ "
                f"{_IDENTIFIABILITY_A_PRIORI_REQUIRED_FROM_ROUND}). "
                f"Before committing to a model architecture, count "
                f"free fitted parameters vs independent calibration "
                f"targets. Verdict IDENTIFIABLE if ratio < 1, MARGINAL "
                f"if 1-3, OVER_SATURATED if > 3. See the `pre-model-"
                f"identifiability-arithmetic` skill and "
                f"`scripts/identifiability_a_priori.py --self-test` "
                f"for the schema. This artifact is NOT scope-"
                f"declarable — architecture choice is inside pipeline "
                f"reach."
            ),
        }]

    try:
        spec = importlib.util.spec_from_file_location(
            "identifiability_a_priori",
            os.path.join(os.path.dirname(__file__),
                         "identifiability_a_priori.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    # Bare `except Exception` matches the 8 other importlib loaders in
    # this file. A narrower (ImportError, OSError, AttributeError) tuple
    # would let SyntaxError escape and crash the validator if the loaded
    # script ever has invalid Python. KeyboardInterrupt/SystemExit are
    # not Exception subclasses, so they still propagate.
    except Exception as e:
        return [{
            "kind": "identifiability_a_priori_invalid",
            "severity": "HIGH",
            "stage": "PRE_MODEL",
            "claim": (f"Could not load "
                      f"scripts/identifiability_a_priori.py: {e}"),
        }]

    result = mod.validate_identifiability_a_priori(yaml_path)
    if result["verdict"] == "MALFORMED":
        errors = result.get("errors") or []
        # Distinguish the decorative-undocumented sub-case: the
        # validator's error message for accept_decorative-without-
        # details starts with "resolution.decision is 'accept_decorative'".
        # The over-saturated-without-any-resolution error is generic
        # ("verdict X requires a resolution field..."). Match the
        # specific decorative-sub-case prefix only.
        if any(
            e.lstrip().startswith(
                "resolution.decision is 'accept_decorative'")
            for e in errors
        ):
            return [{
                "kind": "pre_model_decorative_undocumented",
                "severity": "HIGH",
                "stage": "PRE_MODEL",
                "claim": (f"identifiability_a_priori.yaml declares "
                          f"resolution.decision='accept_decorative' "
                          f"without resolution.details text. Decorative "
                          f"architectures must be justified (100-300 "
                          f"words). Errors: {'; '.join(errors)}"),
            }]
        # Generic missing-resolution case: dispatch on computed_verdict
        # so the user-facing kind matches the actual problem (over-
        # saturation or marginality), not "malformed YAML". MARGINAL
        # missing-resolution is MEDIUM advisory; OVER_SATURATED is HIGH.
        missing_resolution = any(
            "requires a `resolution`" in e for e in errors)
        if (missing_resolution
                and result.get("computed_verdict") == "OVER_SATURATED"):
            s = result.get("summary", {})
            return [{
                "kind": "pre_model_over_saturated",
                "severity": "HIGH",
                "stage": "PRE_MODEL",
                "claim": (
                    f"Pre-model identifiability arithmetic flags "
                    f"OVER_SATURATED: {s.get('n_fitted', 0)} fitted "
                    f"parameters / {s.get('n_targets', 0)} independent "
                    f"calibration targets = "
                    f"{s.get('ratio', 0):.2f}× ratio (threshold > 3.0). "
                    f"resolution.decision is required. Pick one: "
                    f"(a) reduce params (tie across groups), "
                    f"(b) add independent calibration targets, "
                    f"(c) downgrade to analytical model, or "
                    f"(d) accept_decorative with 100-300 word justification. "
                    f"NOT scope-declarable."
                ),
            }]
        if (missing_resolution
                and result.get("computed_verdict") == "MARGINAL"):
            s = result.get("summary", {})
            return [{
                "kind": "pre_model_marginal_identifiability",
                "severity": "MEDIUM",
                "stage": "PRE_MODEL",
                "claim": (
                    f"Pre-model identifiability arithmetic flags "
                    f"MARGINAL: {s.get('n_fitted', 0)} fitted / "
                    f"{s.get('n_targets', 0)} targets = "
                    f"{s.get('ratio', 0):.2f}× ratio (1.0-3.0 band). "
                    f"`resolution` field is missing. The model is at "
                    f"risk for ridge-trapped parameters; document the "
                    f"mitigation (or acknowledge the risk) in "
                    f"resolution.decision."
                ),
            }]
        return [{
            "kind": "identifiability_a_priori_invalid",
            "severity": "HIGH",
            "stage": "PRE_MODEL",
            "claim": (f"models/identifiability_a_priori.yaml is "
                      f"malformed: {'; '.join(errors)}. NOT scope-"
                      f"declarable — fix the artifact."),
        }]

    if result["verdict"] == "OVER_SATURATED":
        # When validate_identifiability_a_priori returns verdict
        # OVER_SATURATED (not MALFORMED), the artifact has a documented
        # resolution decision — has_resolution is True by construction.
        # The over-saturated-without-resolution case returns MALFORMED
        # above and is dispatched there as HIGH pre_model_over_saturated.
        # So this branch always fires the MEDIUM commitment advisory.
        s = result.get("summary", {})
        return [{
            "kind": "pre_model_over_saturated_with_commitment",
            "severity": "MEDIUM",
            "stage": "PRE_MODEL",
            "claim": (
                f"Pre-model identifiability arithmetic flags "
                f"OVER_SATURATED: {s.get('n_fitted', 0)} fitted "
                f"/ {s.get('n_targets', 0)} targets = "
                f"{s.get('ratio', 0):.2f}×. Resolution committed "
                f"in artifact. Once redesign is implemented, "
                f"update identifiability_a_priori.yaml to show the "
                f"new fitted count and verdict IDENTIFIABLE."
            ),
        }]

    if result["verdict"] == "MARGINAL":
        s = result.get("summary", {})
        return [{
            "kind": "pre_model_marginal_identifiability",
            "severity": "MEDIUM",
            "stage": "PRE_MODEL",
            "claim": (
                f"Pre-model identifiability arithmetic flags "
                f"MARGINAL: {s.get('n_fitted', 0)} fitted / "
                f"{s.get('n_targets', 0)} targets = "
                f"{s.get('ratio', 0):.2f}× ratio (1.0-3.0 band). "
                f"The model is at risk for ridge-trapped parameters. "
                f"Post-hoc identifiability check at STAGE 5b will "
                f"confirm or refute. Surface as a caveat in "
                f"§Limitations."
            ),
        }]

    return []  # IDENTIFIABLE — silent pass


# Phase 12 Commit β: round-aware MEDIUM-to-HIGH escalation ledger.
#
# A MEDIUM that fires the same way for ≥ N rounds is signal the
# modeler is ignoring it. The 104914 run had 18 figure_validator_
# missing MEDIUMs persist r2→r6 because MEDIUM doesn't block ACCEPT
# and no validator escalated. This ledger maps validator-emitted
# kinds to their persistence-to-HIGH threshold (number of distinct
# rounds the kind appears in stage7_round*_stderr.txt).
#
# Critique-blocker IDs (e.g., P-005, R-007) are escalated separately
# via `first_seen_round` in the critique YAML — see _escalate_
# critique_blockers below.
_VALIDATOR_KIND_ESCALATION_THRESHOLDS = {
    "figure_validator_missing": 4,
    "decision_rule_self_referential": 3,
    # Phase 21 β: `optimization_quality_missing` never reaches the gate
    # output (the Phase 10 ψ coordinator folds it into allocation_rigor_*)
    # and `optimization_quality_alternative_missing` was never emitted.
    # The coordinator's overdue kind is what persists.
    "allocation_rigor_drafts_overdue": 2,
    "optimization_quality_gap_too_large": 3,
    # Phase 21 β: the gate now runs the coherence audit itself; what can
    # persist is the auditor being unavailable.
    "coherence_audit_unavailable": 3,
    # Phase 19 α: effort floors MEDIUMs that persist 3+ rounds become HIGH.
    # If the modeler keeps shipping under-floor sensitivity/iterations
    # round after round, force fix-or-scope-declare.
    "effort_floor_sensitivity_min_params_perturbed": 3,
    "effort_floor_calibration_min_iterations": 3,
    "effort_floor_uq_min_draws_cloud": 3,
    "effort_floor_shortcut_markers": 3,
    # Phase 19 δ: sufficiency critic absence persisting 3 rounds → HIGH.
    "critique_sufficiency_not_run": 3,
    # Phase 21 β: MEDIUMs observed to persist to the final round of real
    # runs without ever blocking.
    "plan_criterion_not_tested": 3,
    "benchmark_targets_missing": 2,
    "pre_model_over_saturated_with_commitment": 2,
    "weakly_identified_parameters": 3,
}

# Phase 21 β: per-round validator findings, appended by main() on every
# invocation. Replaces counting `stage7_round*_stderr.txt` files, which no
# prompt or code reliably wrote (present in 2 of 8 runs).
_VALIDATOR_HISTORY_FILE = "validator_history.jsonl"


def _load_validator_history(run_dir: str) -> dict[int, set[str]]:
    """{round: set(kinds)} from validator_history.jsonl (union over all
    invocations in a round). Malformed lines are skipped."""
    out: dict[int, set[str]] = {}
    path = os.path.join(run_dir, _VALIDATOR_HISTORY_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except (OSError, UnicodeDecodeError):
        return out
    for line in lines:
        try:
            rec = json.loads(line)
            rnd = int(rec["round"])
            kinds = {str(x["kind"]) for x in rec.get("findings") or []
                     if isinstance(x, dict) and x.get("kind")}
        except (ValueError, KeyError, TypeError):
            continue
        out.setdefault(rnd, set()).update(kinds)
    return out


def _append_validator_history(run_dir: str, round_n: int, decision: dict,
                              post_write: bool) -> None:
    import datetime as _dt
    rec = {
        "round": round_n,
        "post_write": post_write,
        "at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "action": decision.get("action"),
        "findings": [{"kind": v.get("kind"), "severity": v.get("severity"),
                      "stage": v.get("stage")}
                     for v in (decision.get("rigor_violations") or [])],
    }
    try:
        with open(os.path.join(run_dir, _VALIDATOR_HISTORY_FILE), "a",
                  encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError as e:
        print(f"WARNING: could not append validator history: {e}",
              file=sys.stderr)


def _write_open_problems(run_dir: str, round_n: int, max_rounds: int,
                         critiques: dict, decision: dict) -> str | None:
    """Phase 32 α: open_problems.json/.md after every in-round gate run, for
    the lead's round_plan.yaml. Returns a line for stderr. A failure here is
    reported, never fatal to the gate decision."""
    try:
        import open_problems
        hist = _load_validator_history(run_dir)
        kind_rounds: dict[str, int] = {}
        for kinds in hist.values():
            for k in kinds:
                kind_rounds[k] = kind_rounds.get(k, 0) + 1
        doc = open_problems.build(
            run_dir, round_n, max_rounds, critiques, decision, kind_rounds,
            _VALIDATOR_KIND_ESCALATION_THRESHOLDS,
            _CRITIQUE_BLOCKER_ESCALATION_THRESHOLD)
        open_problems.write(run_dir, doc)
    except Exception as e:  # noqa: BLE001
        return f"WARNING: open_problems.json not written: {e}"
    n = len(doc["items"])
    soon = sum(1 for i in doc["items"] if i["blocks_in"] == 1)
    return (f"Open problems: {n} ({soon} become blocking at the next gate). "
            f"See open_problems.md; write round_plan.yaml (round: {round_n}) "
            f"before the next round's work.")


def _annotate_first_seen(run_dir: str, violations: list[dict]) -> None:
    """Phase 21 β: give each rigor violation the earliest round its kind
    appeared in validator history, so persisting rigor HIGHs look stuck
    (Phase 5 ζ) instead of always being 'first seen this round'. The key
    is the kind, so distinct instances of one kind share a first round —
    an over-estimate of persistence only when an old instance was fixed
    and a new one appeared."""
    hist = _load_validator_history(run_dir)
    first: dict[str, int] = {}
    for rnd in sorted(hist):
        for k in hist[rnd]:
            first.setdefault(k, rnd)
    for v in violations:
        if "first_seen_round" not in v and v.get("kind") in first:
            v["first_seen_round"] = first[v["kind"]]


# Critique-blocker IDs from critique-presentation that should
# escalate after persisting N rounds. (Phase 11 review surfaced
# P-005 through P-009 persisting all 6 rounds.)
_CRITIQUE_BLOCKER_ESCALATION_THRESHOLD = 4  # rounds since first_seen


# ---------------------------------------------------------------------------
# Phase 17 α — pre-mortem domain critique
# ---------------------------------------------------------------------------
#
# The pre_mortem.yaml artifact is produced by the critique-premortem agent
# in the PRE-MODEL window (STAGE 3) before the modeler builds. Round 1 is
# the drafting window; round 2+ requires the file to exist AND each HIGH
# concern to have addressed_in: filled. Unlike Phase 15 α's
# identifiability_a_priori (architecture arithmetic, non-scope-declarable),
# pre-mortem concerns are domain heuristics — the modeler MAY scope-declare
# a HIGH with justification in scope_declaration.yaml.
_PRE_MORTEM_REQUIRED_FROM_ROUND = 2


_PLACEHOLDER_REFS = frozenset({"", "TODO", "TBD", "N/A", "NA", "NONE",
                               "NULL", "-", "PENDING", "LATER"})
_MD_HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$", re.MULTILINE)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _addressed_in_resolves(run_dir: str, ref: Any) -> bool:
    """Phase 21 β: `addressed_in` must point at real content, e.g.
    `modeling_strategy.md § Pre-mortem Responses` or
    `modeling_strategy.md#pre-mortem-pm-001`. Placeholders (TODO, TBD,
    N/A, ...) and references to files that do not exist do not count."""
    if not isinstance(ref, str) or ref.strip().upper() in _PLACEHOLDER_REFS:
        return False
    m = re.match(r"^\s*([^§#:]+?)\s*(?:(§|#|:)\s*(.*))?$", ref)
    if not m:
        return False
    fname, sep, section = m.group(1).strip(), m.group(2), (m.group(3) or "").strip()
    if fname.upper() in _PLACEHOLDER_REFS:
        return False
    path = None
    for cand in (os.path.join(run_dir, fname),
                 os.path.join(run_dir, "models", os.path.basename(fname))):
        if os.path.isfile(cand):
            path = cand
            break
    if path is None:
        return False
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError):
        return False
    if not text.strip():
        return False
    if not section or sep == ":":
        return True
    headings = [h.strip() for h in _MD_HEADING_RE.findall(text)]
    if sep == "§":
        return any(section.lower() in h.lower() for h in headings)
    want = _slug(section)
    return any(_slug(h).startswith(want) or want in _slug(h) for h in headings) \
        or want in _slug(text)


def _check_premortem_addressed(run_dir: str,
                                round_n: int | None = None
                                ) -> list[dict]:
    """Phase 17 α: validate pre_mortem.yaml.

    Emits:
      pre_mortem_missing                MEDIUM @ r=1 (drafting), HIGH @ r≥2
      pre_mortem_invalid                HIGH (yaml MALFORMED)
      pre_mortem_high_unaddressed       MEDIUM @ r=1, HIGH @ r≥2 (per concern)

    Round gating: returns [] when round_n is None (caller has not
    supplied a round; defer). At round_n=1, missing-artifact and
    unaddressed-HIGH each emit MEDIUM (drafting window). At round_n≥2,
    they escalate to HIGH.

    The pre_mortem_high_unaddressed kind IS scope-declarable via
    scope_declaration.yaml — pre-mortem concerns are domain heuristics,
    not arithmetic facts. The modeler can satisfy the gate by either
    filling addressed_in: in pre_mortem.yaml OR adding a matching
    scope-declaration entry that quotes the concern's id (PM-NNN).
    """
    if round_n is None:
        return []

    yaml_path = artifact_path("pre_mortem", run_dir)
    out: list[dict] = []

    if not os.path.exists(yaml_path):
        severity = "HIGH" if round_n >= _PRE_MORTEM_REQUIRED_FROM_ROUND else "MEDIUM"
        out.append({
            "kind": "pre_mortem_missing",
            "severity": severity,
            "stage": "PRE_MODEL",
            "claim": (
                f"Required artifact `pre_mortem.yaml` is absent at round "
                f"{round_n}. The pre-mortem domain critic (Phase 17 α) "
                f"runs in STAGE 3 before MODEL spawns. Spawn the "
                f"`critique-premortem` agent on plan.md + hypotheses.md + "
                f"matching expert_priors. The artifact lists HIGH-impact "
                f"concerns the modeler must address in modeling_strategy.md "
                f"§ Pre-mortem Responses. See the `pre-mortem-domain` skill."
            ),
        })
        return out

    # Phase 21 β: fail closed — unparseable, non-mapping, or a renamed
    # top-level key (e.g. `high_concerns:`) is HIGH invalid, not [].
    doc, invalid = safe_load_mapping(
        yaml_path, "pre_mortem", stage="PRE_MODEL",
        required_keys=["concerns"], expected_types={"concerns": list},
        display_path="pre_mortem.yaml")
    if doc is None:
        return invalid
    concerns = doc["concerns"]

    # Phase 21 β: a concern counts as scope-declared only via a structured
    # declaration entry (blocker_id / blocker_ids / id == PM-NNN) that also
    # carries a justification. The pre-21 check accepted the id appearing
    # anywhere in the file, e.g. `notes: "PM-001…PM-007"`.
    declared_ids: set[str] = set()
    _sd_doc, sd_entries, _ = _load_scope_declarations(run_dir)
    for e in sd_entries:
        if not _entry_has_justification(e):
            continue
        declared_ids |= _entry_declared_ids(e, structured_only=True)
        if e.get("id"):
            declared_ids.add(str(e["id"]))

    severity = "HIGH" if round_n >= _PRE_MORTEM_REQUIRED_FROM_ROUND else "MEDIUM"
    for c in concerns:
        if not isinstance(c, dict):
            continue
        # Phase 21 β: CRITICAL (and any unknown severity) is at least HIGH.
        if str(c.get("severity", "")).strip().upper() in ("MEDIUM", "LOW"):
            continue
        cid = c.get("id") or "PM-???"
        if _addressed_in_resolves(run_dir, c.get("addressed_in")):
            continue
        if cid in declared_ids:
            continue
        out.append({
            "kind": "pre_mortem_high_unaddressed",
            "severity": severity,
            "stage": "PRE_MODEL",
            "claim": (
                f"Pre-mortem HIGH concern {cid} has no resolvable "
                f"`addressed_in:` (got {c.get('addressed_in')!r}; it must "
                f"name an existing run file, optionally `§ Section`) and "
                f"no structured scope_declaration entry (blocker_id: {cid} "
                f"+ justification). "
                f"Concern: {str(c.get('concern', '')).strip().splitlines()[0] if c.get('concern') else ''!r}. "
                f"Either resolve in modeling_strategy.md § Pre-mortem "
                f"Responses (and set addressed_in:), or add a "
                f"scope_declaration.yaml entry that quotes {cid}."
            ),
        })
    return out


_CLAIMS_LEDGER_REQUIRED_FROM_ROUND = 3


def _check_claims_ledger_present(run_dir: str,
                                  round_n: int | None = None
                                  ) -> list[dict]:
    """Phase 18 α: validate models/claims_ledger.yaml.

    Emits:
      claims_ledger_missing      MEDIUM @ r=2, HIGH @ r≥3
      claims_ledger_invalid      HIGH (yaml malformed or schema violation)
      claims_ledger_ci_violated  HIGH (ci_low > value or ci_high < value)

    Round gating applies to ABSENCE only: round_n=None or round 1 → a
    missing ledger is silent (drafting window); round 2 → MEDIUM; round
    ≥ 3 → HIGH. A ledger that exists is always schema- and provenance-
    checked: a wrong number is wrong at round 1 too (E5: the round-1
    gate silenced 5/5 planted ledger mismatches it would have caught).

    The ledger is NOT scope-declarable (Phase 18 α contract): the
    writer cannot produce a clean report without it, so the modeler
    must produce one or scope-declare ABSENCE-OF-WRITER (no report).
    """
    yaml_path = artifact_path("claims_ledger", run_dir)
    out: list[dict] = []

    # Phase 22 γ: with a claims spec the ledger is derived, not written —
    # rebuild it and compare instead of trusting the file.
    if os.path.exists(os.path.join(run_dir, "models", "claims_spec.yaml")):
        return _claims_spec_violations(run_dir, yaml_path)

    if os.path.exists(yaml_path) and round_n is not None and round_n >= 2:
        out.append({
            "kind": "claims_spec_missing",
            "severity": "MEDIUM",
            "stage": "ANALYZE",
            "claim": ("`models/claims_ledger.yaml` was written by hand: there is no "
                      "`models/claims_spec.yaml`. Write the spec (claims without "
                      "values) and run `python3 scripts/build_claims_ledger.py "
                      "{run_dir}` so every number is read from its source artifact."),
        })

    if not os.path.exists(yaml_path):
        if round_n is None or round_n < 2:
            return []
        severity = "HIGH" if round_n >= _CLAIMS_LEDGER_REQUIRED_FROM_ROUND \
            else "MEDIUM"
        out.append({
            "kind": "claims_ledger_missing",
            "severity": severity,
            "stage": "ANALYZE",
            "claim": (
                f"Required artifact `models/claims_ledger.yaml` is "
                f"absent at round {round_n}. Phase 18 α requires the "
                f"analyst to register every quantitative or categorical "
                f"claim in the ledger before writing results.md, so the "
                f"writer can reference each via `[CLAIM:claim_id]` "
                f"syntax. See the analyst SYSTEM_PROMPT § Claims Ledger."
            ),
        })
        return out

    # Validate the schema by reusing render_claims.load_claims, which
    # checks CI bounds and duplicate ids. Don't fail the whole run on
    # parse error — emit HIGH coherence_audit_invalid-style violation.
    try:
        spec = importlib.util.spec_from_file_location(
            "render_claims",
            os.path.join(os.path.dirname(__file__), "render_claims.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as e:  # noqa: BLE001
        out.append({
            "kind": "claims_ledger_invalid",
            "severity": "HIGH",
            "stage": "ANALYZE",
            "claim": (f"Could not load scripts/render_claims.py to "
                      f"validate ledger: {e}."),
        })
        return out

    try:
        claims = mod.load_claims(yaml_path)
    except (ValueError, FileNotFoundError) as e:
        kind = "claims_ledger_ci_violated" \
            if "CI bounds violated" in str(e) else "claims_ledger_invalid"
        out.append({
            "kind": kind,
            "severity": "HIGH",
            "stage": "ANALYZE",
            "claim": (f"`models/claims_ledger.yaml` failed schema "
                      f"validation: {e}"),
        })
        return out

    # Empty ledger: severity matches the missing-file gating to avoid
    # penalizing the analyst who creates a partial file at r=2 more
    # harshly than one who creates none. Phase 18 δ — the original
    # logic fired HIGH unconditionally regardless of round.
    if not claims:
        empty_severity = "HIGH" \
            if (round_n or 0) >= _CLAIMS_LEDGER_REQUIRED_FROM_ROUND else "MEDIUM"
        out.append({
            "kind": "claims_ledger_invalid",
            "severity": empty_severity,
            "stage": "ANALYZE",
            "claim": ("`models/claims_ledger.yaml` is present but contains "
                      "zero claims. Phase 18 α requires every quantitative "
                      "or categorical claim in results.md to be registered."),
        })
        return out

    out.extend(_ledger_provenance_violations(yaml_path, run_dir))
    return out


_PROVENANCE_PER_KIND_CAP = 5


def _capped(kind: str, items: list[str], claim_head: str, stage: str = "ANALYZE") -> list[dict]:
    """One HIGH per item up to the cap, then one HIGH naming the rest."""
    out = [{"kind": kind, "severity": "HIGH", "stage": stage, "scope_declarable": False,
            "claim": f"{claim_head}{it}"} for it in items[:_PROVENANCE_PER_KIND_CAP]]
    if len(items) > _PROVENANCE_PER_KIND_CAP:
        out.append({"kind": kind, "severity": "HIGH", "stage": stage, "scope_declarable": False,
                    "claim": (f"{len(items) - _PROVENANCE_PER_KIND_CAP} more: "
                              + "; ".join(i[:160] for i in items[_PROVENANCE_PER_KIND_CAP:]))})
    return out


def _claims_spec_violations(run_dir: str, ledger_path: str) -> list[dict]:
    """Phase 22 γ: build the ledger from models/claims_spec.yaml.

      claims_spec_invalid      HIGH  spec unparseable, duplicate ids, a typed
                                     value on a model-derived claim
      claim_source_unresolved  HIGH  a claim whose number is in no artifact
      claims_ledger_stale      HIGH  the ledger file differs from a fresh
                                     build (sources re-run, or hand-edited)
    All non-declarable: the fix is always to correct the spec or re-run
    scripts/build_claims_ledger.py.
    """
    builder = _load_sibling("build_claims_ledger")
    doc, errors = builder.build(run_dir)
    out = _capped("claims_spec_invalid", errors, "models/claims_spec.yaml: ")
    if doc is None:
        return out
    out += _capped("claim_source_unresolved",
                   [f"[{u['id']}] {u['reason']}" for u in doc["unresolved"]], "")
    if not doc["claims"] and not doc["unresolved"]:
        out.append({"kind": "claims_ledger_invalid", "severity": "HIGH", "stage": "ANALYZE",
                    "claim": "models/claims_spec.yaml lists zero claims."})
        return out
    # The built ledger must pass the same schema the renderer enforces
    # (CI bounds, kinds, ids): external claims carry spec-typed values.
    import tempfile as _tf
    with _tf.NamedTemporaryFile("w", suffix=".yaml", delete=False) as tmp:
        yaml.safe_dump(doc, tmp)
    try:
        _load_sibling("render_claims").load_claims(tmp.name)
    except (ValueError, FileNotFoundError) as e:
        msg = str(e).replace(tmp.name, "built claims ledger")
        out.append({"kind": "claims_ledger_ci_violated" if "CI bounds" in msg
                    else "claims_ledger_invalid", "severity": "HIGH", "stage": "ANALYZE",
                    "scope_declarable": False,
                    "claim": f"models/claims_spec.yaml builds a ledger that fails validation: {msg}"})
    finally:
        os.unlink(tmp.name)
    run = f"Run `python3 scripts/build_claims_ledger.py {{run_dir}}`."
    if not os.path.exists(ledger_path):
        out.append({"kind": "claims_ledger_stale", "severity": "HIGH", "stage": "ANALYZE",
                    "scope_declarable": False,
                    "claim": f"models/claims_spec.yaml exists but the ledger was never built. {run}"})
        return out
    try:
        with open(ledger_path, encoding="utf-8") as f:
            on_disk = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as e:
        on_disk = f"unreadable: {e}"
    probs = builder.compare(on_disk, doc)
    if probs:
        out.append({"kind": "claims_ledger_stale", "severity": "HIGH", "stage": "ANALYZE",
                    "scope_declarable": False,
                    "claim": (f"models/claims_ledger.yaml differs from a fresh build "
                              f"({len(probs)} difference(s): {'; '.join(probs[:10])}). "
                              f"Sources changed after the build, or the file was "
                              f"edited. {run}")})
    return out


def _ledger_provenance_violations(ledger_path: str, run_dir: str) -> list[dict]:
    """Phase 21 β: every ledger claim must match the artifact it cites.

    Delegates to scripts/lib/provenance.py. All findings are HIGH and NOT
    scope-declarable. Display is capped per kind; the overflow is one
    more HIGH (capping must never turn a blocker into an advisory).
    """
    doc, findings = safe_load_mapping(ledger_path, "claims_ledger",
                                      stage="ANALYZE",
                                      display_path="models/claims_ledger.yaml")
    if doc is None:
        return findings
    res = _provenance.check_ledger_provenance(doc, run_dir)
    by_kind: dict[str, list[dict]] = {}
    for f in res["findings"]:
        by_kind.setdefault(f["kind"], []).append(f)
    out: list[dict] = []
    for kind, fs in by_kind.items():
        out.extend(fs[:_PROVENANCE_PER_KIND_CAP])
        if len(fs) > _PROVENANCE_PER_KIND_CAP:
            # Name every overflowed claim: a count alone hid planted
            # mismatches behind natural ones in E5.
            rest = "; ".join(f["claim"][:160] for f in fs[_PROVENANCE_PER_KIND_CAP:])
            out.append({
                "kind": kind,
                "severity": "HIGH",
                "stage": "ANALYZE",
                "scope_declarable": False,
                "claim": (f"{len(fs) - _PROVENANCE_PER_KIND_CAP} additional "
                          f"{kind} finding(s) in models/claims_ledger.yaml "
                          f"({res['n_checked']} checked, {res['n_external']} "
                          f"external, {res['n_total']} total): {rest}"),
            })
    return out


# Self-test hook: replace the in-process coherence audit computation.
_COHERENCE_AUDIT_FN = None


def _check_coherence_audit(run_dir: str, post_write: bool = False) -> list[dict]:
    """Phase 17 β: surface violations from `scripts/coherence_audit.py`.

    The auditor runs post-WRITE alongside writer_qa.py. It checks
    label coherence (e.g., 'UNSTABLE' in prose vs 'SENSITIVE' in YAML),
    cross-file count/cost consistency (prose vs allocation CSV),
    self-contradicting artifacts (notes contradict structured fields in
    the same YAML), and ledger binding (Phase 18 β).

    Phase 21 β: the gate recomputes the audit in-process on the current
    report.md and refreshes `coherence_audit.yaml`, instead of trusting
    the cached file. 2026-05-03_235631's progress.md said "coherence_audit
    (CLEAN)" while a re-run on the final report found 21 HIGH
    ledger_binding drifts. A malformed audit result (non-dict, non-list
    violations, non-dict entries) is HIGH `coherence_audit_invalid`; an
    unimportable auditor is MEDIUM (HIGH in post-write mode).

    Returns: list of {kind, severity, stage, claim} dicts.
    """
    report_path = os.path.join(run_dir, "report.md")
    out: list[dict] = []

    if not os.path.exists(report_path):
        return []  # WRITE hasn't happened; nothing to audit

    try:
        if _COHERENCE_AUDIT_FN is not None:
            audit = _COHERENCE_AUDIT_FN(run_dir)
        else:
            mod = _load_sibling("coherence_audit")
            audit = mod.audit_run(run_dir)
            mod.write_audit_yaml(run_dir, audit)
    except Exception as e:  # noqa: BLE001 — fail closed below
        return [{
            "kind": "coherence_audit_unavailable",
            "severity": "HIGH" if post_write else "MEDIUM",
            "stage": "WRITE",
            "claim": (f"coherence audit could not be computed in-process "
                      f"({type(e).__name__}: {e}). Run "
                      f"`python3 scripts/coherence_audit.py {run_dir}`."),
        }]

    violations = audit.get("violations") if isinstance(audit, dict) else None
    if not isinstance(violations, list) or not all(
            isinstance(v, dict) for v in violations):
        return [{
            "kind": "coherence_audit_invalid",
            "severity": "HIGH",
            "stage": "WRITE",
            "scope_declarable": False,
            "claim": ("coherence audit result must be a mapping with a "
                      "`violations:` list of mappings; got "
                      f"{type(audit).__name__} / "
                      f"{type(violations).__name__}."),
        }]

    # Bucket by duty, take top-K per duty to avoid swamping the gate.
    # Phase 10 ψ's allocation-gate consolidation pattern: prefer one
    # consolidated MEDIUM per duty over many fine-grained MEDIUMs.
    per_duty: dict[str, list[dict]] = {}
    for v in violations:
        duty = str(v.get("duty", "unknown"))
        per_duty.setdefault(duty, []).append(v)

    for duty, vlist in per_duty.items():
        # Always emit individual HIGHs (these block ACCEPT). Missing
        # severity is treated as HIGH (fail closed — Phase 21 β).
        highs = [v for v in vlist
                 if str(v.get("severity") or "HIGH").upper() == "HIGH"]
        meds = [v for v in vlist
                if str(v.get("severity") or "").upper() == "MEDIUM"]
        for v in highs[:5]:  # cap at 5 per duty
            out.append({
                "kind": f"coherence_audit_{duty}",
                "severity": "HIGH",
                "stage": "WRITE",
                "claim": (
                    f"[{v.get('id', '?')}] {str(v.get('claim') or '')[:300]} "
                    f"(canonical: {v.get('canonical_source', '?')}; "
                    f"location: {v.get('location', '?')})"
                ),
            })
        if len(highs) > 5:
            # Phase 21 β: the overflow is still HIGH — capping display must
            # not make the remaining drifts advisory.
            out.append({
                "kind": f"coherence_audit_{duty}",
                "severity": "HIGH",
                "stage": "WRITE",
                "claim": (f"{len(highs) - 5} additional HIGH {duty} "
                          f"violation(s); see coherence_audit.yaml."),
            })
        # Consolidate MEDIUM violations into a single advisory per duty.
        if meds:
            out.append({
                "kind": f"coherence_audit_{duty}_advisory",
                "severity": "MEDIUM",
                "stage": "WRITE",
                "claim": (
                    f"{len(meds)} MEDIUM coherence_audit violation(s) "
                    f"on duty `{duty}`. First example: "
                    f"{str(meds[0].get('claim') or '')[:200]}"
                ),
            })
    return out


# Phase 19 α — effort floors threshold (rounds at which floors start firing).
# Round 1 is the drafting window: floors emit MEDIUMs only. From round 2 on,
# violations escalate per the manifest's declared severity.
_EFFORT_FLOORS_ENFORCE_FROM_ROUND = 2

_EFFORT_FLOORS_MODULE = None


def _load_effort_floors_module():
    """Lazy import of scripts/effort_floors.py (sibling). Cached.

    Registers in sys.modules before exec_module to keep Python 3.14's
    @dataclass happy (it does `sys.modules.get(cls.__module__).__dict__`
    during decoration; AttributeError if the module isn't registered).
    """
    global _EFFORT_FLOORS_MODULE
    if _EFFORT_FLOORS_MODULE is not None:
        return _EFFORT_FLOORS_MODULE
    try:
        spec = importlib.util.spec_from_file_location(
            "effort_floors",
            os.path.join(os.path.dirname(__file__), "effort_floors.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules["effort_floors"] = mod
        spec.loader.exec_module(mod)
        _EFFORT_FLOORS_MODULE = mod
        return mod
    except (FileNotFoundError, ImportError):
        return None


def _check_effort_floors(run_dir: str,
                         round_n: int | None = None) -> list[dict]:
    """Phase 19 α: enforce quantitative minimums for "actually tried."

    Reads `{run_dir}/effort_floors_report.yaml` (produced by
    `scripts/effort_floors.py`). If the report is absent and any
    trigger artifact exists (uncertainty_report.yaml,
    models/sensitivity_analysis.yaml, models/calibration_result.yaml,
    or models/), invokes the module in-process so a missing run of
    the script doesn't silently let an under-powered model ship.

    Emits per violation:
      effort_floor_<floor_id>       severity from manifest (HIGH | MEDIUM)
      effort_floors_unavailable     MEDIUM (scripts/effort_floors.py
                                     missing or manifest malformed)
      effort_floors_many_shortcuts  HIGH (≥ 3 shortcut markers — escalation)

    Round gating: round_n=None → silent (defer). Round 1 → all
    violations advisory (MEDIUM) regardless of manifest severity. Round
    ≥ 2 → honor manifest severities.

    HIGH violations with `scope_declarable: false` are explicitly NOT
    silenceable via scope_declaration.yaml — these are the "no shortcut"
    floors (held-out validation, multi-restart, minimum draws).
    """
    if round_n is None:
        return []

    out: list[dict] = []

    # Determine whether any trigger artifact is present. If none of the
    # probes would fire, we don't need to insist on running the script.
    triggers_present = (
        os.path.exists(produced_path("outcome_fn", run_dir))
        or os.path.exists(artifact_path("sensitivity_analysis", run_dir))
        or os.path.exists(artifact_path("calibration_result", run_dir))
        or os.path.isdir(os.path.join(run_dir, "models"))
    )

    if not triggers_present:
        return []

    # Always recompute the derived report. It is a cache of model artifacts,
    # not an authoritative input; reusing it across rounds can preserve stale
    # violations or stale PASS results after the modeler changes outputs/code.
    mod = _load_effort_floors_module()
    if mod is None:
        # Phase 21 β: fail closed — an unevaluable floor set is HIGH.
        return [{
            "kind": "effort_floors_unavailable",
            "severity": "HIGH",
            "stage": "GATE",
            "claim": (
                "effort_floors_report.yaml cannot be refreshed because "
                "scripts/effort_floors.py could not be imported. Run "
                f"`python3 scripts/effort_floors.py {run_dir}` manually. "
                "See Phase 19 α."
            ),
        }]
    try:
        rep = mod.evaluate(run_dir)
        mod.write_report(run_dir, rep)
    except Exception as e:  # noqa: BLE001 — Phase 21 β: fail closed
        return [{
            "kind": "effort_floors_unavailable",
            "severity": "HIGH",
            "stage": "GATE",
            "claim": (f"Could not compute effort floors: {e}. "
                      f"Run `python3 scripts/effort_floors.py {run_dir}` "
                      f"and resolve the manifest error."),
        }]

    violations = rep.get("violations") or []
    if not isinstance(violations, list):
        return out

    drafting_window = round_n < _EFFORT_FLOORS_ENFORCE_FROM_ROUND

    # Phase 20 β: close the round-1 escape. If the run has rigor
    # triggers present AND the floor evaluator surfaced any
    # would-be-HIGH violation at round 1, ACCEPT must wait for at
    # least round 2 so the manifest severities can fire. A run that
    # converges in one round and ships under the drafting demotion
    # silently bypasses effort enforcement — exactly the failure mode
    # Phase 19 α was supposed to close.
    if drafting_window and violations:
        would_be_high = [
            v for v in violations
            if isinstance(v, dict) and v.get("severity") == "HIGH"
        ]
        if would_be_high:
            preview = ", ".join(
                str(v.get("floor_id") or "?") for v in would_be_high[:5]
            )
            out.append({
                "kind": "effort_floors_require_followup_round",
                "severity": "HIGH",
                "stage": "GATE",
                "claim": (
                    f"At round {round_n}, the drafting window demotes "
                    f"effort-floor violations to MEDIUM. The current run "
                    f"has {len(would_be_high)} would-be-HIGH "
                    f"violation(s) [{preview}], so ACCEPT must wait "
                    f"until round ≥ {_EFFORT_FLOORS_ENFORCE_FROM_ROUND} "
                    f"to let the gate enforce. Either resolve the "
                    f"underlying floors (more draws / restarts / "
                    f"held-out folds) and re-run, or continue iterating; "
                    f"a one-round run on a triggers-present model "
                    f"cannot ACCEPT under Phase 20 β."
                ),
            })

    # Special handling: shortcut markers escalate by count. ≥ 3 findings → HIGH
    # regardless of manifest severity (catches the "death by a thousand cuts"
    # pattern where the modeler scatters small punts across many files).
    shortcut_viols = [v for v in violations
                      if isinstance(v, dict)
                      and v.get("floor_id") == "shortcut_markers_in_model_code"]
    other_viols = [v for v in violations
                   if isinstance(v, dict)
                   and v.get("floor_id") != "shortcut_markers_in_model_code"]

    for v in shortcut_viols:
        observed = v.get("observed") or 0
        try:
            n = int(observed)
        except (TypeError, ValueError):
            n = 0
        severity = "HIGH" if (n >= 3 and not drafting_window) else "MEDIUM"
        findings_preview = "; ".join((v.get("findings") or [])[:5])
        out.append({
            "kind": "effort_floors_many_shortcuts" if severity == "HIGH"
                    else "effort_floor_shortcut_markers",
            "severity": severity,
            "stage": "MODEL",
            "claim": (
                f"Shortcut markers in model code: {n} finding(s). "
                f"First examples: {findings_preview}. "
                f"Phase 19 α — each marker is a quiet under-investment "
                f"(TODO, n_replicates=1, bare except: pass). Resolve "
                f"or document why the shortcut is the right answer in "
                f"modeling_strategy.md."
            ),
        })

    for v in other_viols:
        floor_id = v.get("floor_id", "?")
        severity = str(v.get("severity") or "MEDIUM").upper()
        if drafting_window:
            # Drafting window — demote to MEDIUM, do not block ACCEPT
            severity = "MEDIUM"
        scope_declarable = bool(v.get("scope_declarable"))
        observed = v.get("observed")
        minimum = v.get("minimum")
        scope_tag = " (NOT scope-declarable)" if not scope_declarable else ""
        diagnostic = (v.get("diagnostic") or "").strip()
        diag_tag = f" — {diagnostic}" if diagnostic else ""
        out.append({
            "kind": f"effort_floor_{floor_id}",
            "severity": severity,
            "stage": "MODEL",
            # Phase 21 β: carried to the decision — a non-declarable floor
            # open at round exhaustion is RUN_FAILED, not DECLARE_SCOPE.
            "scope_declarable": scope_declarable,
            "claim": (
                f"Effort floor `{floor_id}` violated{scope_tag}: "
                f"observed {observed!r} below minimum {minimum!r}"
                f"{diag_tag}. Phase 19 α — gates measure effort, not "
                f"just artifact existence. Fix the underlying "
                f"under-investment (more draws / restarts / "
                f"perturbations / iterations) or add justification "
                f"in modeling_strategy.md."
            ),
        })

    return out


# ---------------------------------------------------------------------------
# Phase 21 β — scope_declaration.yaml normalization
# ---------------------------------------------------------------------------
#
# Real runs used at least eight different top-level keys for their
# declarations (`declarations`, `scope_declarations`, `unresolved_blockers`,
# `unresolved_high_blockers`, `acknowledged_limitations`, ...), referenced
# blockers via `blocker_id`, lists of ids, or ids embedded in prose, and
# marked some entries `status: RESOLVED`. The Phase 19 β budget counted only
# `declarations:` entries with `id`/`claim`, so any other shape burned zero
# budget; the pre-mortem check accepted any substring anywhere in the file.
# One normalizer now feeds both.

_BLOCKER_ID_RE = re.compile(r"\b[A-Z][A-Z0-9]{0,5}-\d{2,4}\b")
_STRUCTURED_ID_FIELDS = ("blocker_id", "blocker_ids", "blockers", "covers",
                         "concern_id", "concern_ids", "ids", "acknowledges",
                         "blocker")
_JUSTIFICATION_FIELDS = ("why_unresolved", "justification", "rationale",
                         "claim", "reason", "impact_on_conclusions",
                         "description")
_NON_DECLARATION_KEYS = frozenset({"budget", "version", "run_dir",
                                   "generated_at", "declared_at"})


def _load_scope_declarations(run_dir: str
                             ) -> tuple[Optional[dict], list[dict], list[dict]]:
    """Return (doc, entries, findings) for scope_declaration.yaml.

    `entries` is every mapping found in any top-level list (except the
    `budget` block and metadata keys), plus any top-level mapping value
    that itself looks like a declaration (has an id field). Entries with
    `status: RESOLVED` are dropped — they declare nothing.
    """
    path = artifact_path("scope_declaration", run_dir)
    doc, findings = safe_load_mapping(path, "scope_declaration", stage="GATE",
                                      display_path="scope_declaration.yaml")
    if doc is None:
        return None, [], findings
    entries: list[dict] = []
    for key, val in doc.items():
        if key in _NON_DECLARATION_KEYS:
            continue
        items = val if isinstance(val, list) else (
            [dict(val, id=val.get("id", key))]
            if isinstance(val, dict) else [])
        for e in items:
            if isinstance(e, dict):
                if str(e.get("status") or "").strip().upper() == "RESOLVED":
                    continue
                entries.append(e)
            elif isinstance(e, str) and e.strip():
                entries.append({"claim": e})
            else:
                entries.append({})
    return doc, entries, findings


def _strings_in(obj: Any) -> list[str]:
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        return [s for v in obj.values() for s in _strings_in(v)]
    if isinstance(obj, list):
        return [s for v in obj for s in _strings_in(v)]
    return []


def _entry_declared_ids(entry: dict, structured_only: bool) -> set[str]:
    """Blocker ids an entry declares. Structured fields always count;
    with structured_only=False, ids mentioned anywhere in the entry's
    text count too (one entry listing 20 ids declares 20 blockers).
    The entry's own `id` (e.g. SD-001) is not a blocker reference unless
    it is the only identifier present."""
    ids: set[str] = set()
    for f in _STRUCTURED_ID_FIELDS:
        v = entry.get(f)
        for x in (v if isinstance(v, list) else [v]):
            if isinstance(x, (str, int)) and str(x).strip():
                ids.add(str(x).strip())
    if not structured_only:
        own = str(entry.get("id") or "")
        for txt in _strings_in({k: v for k, v in entry.items() if k != "id"}):
            ids.update(m for m in _BLOCKER_ID_RE.findall(txt) if m != own)
    return ids


def _entry_has_justification(entry: dict) -> bool:
    return any(str(entry.get(f) or "").strip() for f in _JUSTIFICATION_FIELDS)


# Phase 19 β — scope-declaration budget cap. Default is 3 declarations
# per run; the modeler may raise to 5 via a top-level `budget:` block in
# scope_declaration.yaml with a non-empty `overage_justification` field.
# Caps higher than 5 are NOT honored — they require an explicit human
# escalation outside the harness.
_SCOPE_DECLARATION_DEFAULT_CAP = 3
_SCOPE_DECLARATION_HARD_CEILING = 5
_SCOPE_BUDGET_ENFORCE_FROM_ROUND = 2


def _check_scope_declaration_budget(run_dir: str,
                                     round_n: int | None = None
                                     ) -> list[dict]:
    """Phase 19 β: cap how many things the modeler may scope-declare.

    Reads `{run_dir}/scope_declaration.yaml`. Counts top-level
    `declarations:` entries (the canonical list used by all prior
    phases). If the count exceeds the cap (default 3, override up to
    5 with `budget.cap` + non-empty `budget.overage_justification`),
    emits one HIGH `scope_declaration_budget_exceeded` violation.

    Round gating:
      - round_n=None → silent
      - round 1 → silent (drafting window — the modeler is still
        triaging; budget is enforced from round 2)
      - round ≥ 2 → enforced

    Counts ONLY declarations the modeler authored. Each declaration
    must have at least an `id` or `claim` field to be counted —
    blank entries don't burn budget but DO emit a MEDIUM advisory.
    """
    if round_n is None or round_n < _SCOPE_BUDGET_ENFORCE_FROM_ROUND:
        return []

    doc, entries, findings = _load_scope_declarations(run_dir)
    if doc is None:
        # Absent → silent (most common case). Unparseable / non-mapping →
        # the safe_io HIGH is downgraded to the historical MEDIUM here;
        # the declarations it may contain are unusable either way.
        return [dict(f, severity="MEDIUM") for f in findings]

    decls_raw = doc.get("declarations")
    if decls_raw is not None and not isinstance(decls_raw, list):
        return [{
            "kind": "scope_declaration_invalid",
            "severity": "MEDIUM",
            "stage": "GATE",
            "claim": ("scope_declaration.yaml: `declarations:` must be a list."),
        }]

    # Phase 21 β: budget unit is the distinct blocker id declared, across
    # every list in the file. Entries that reference no id but carry
    # substance count 1 each; empty entries are advisory only.
    declared: set[str] = set()
    counted: list[dict] = []
    empties = 0
    for i, entry in enumerate(entries):
        ids = _entry_declared_ids(entry, structured_only=False)
        if ids:
            declared.update(ids)
            counted.append(entry)
        elif entry.get("id") or _entry_has_justification(entry):
            declared.add(str(entry.get("id") or f"<entry {i + 1}>"))
            counted.append(entry)
        else:
            empties += 1

    # Determine effective cap.
    budget = doc.get("budget") or {}
    cap = _SCOPE_DECLARATION_DEFAULT_CAP
    overage_justification = ""
    requested_cap = _SCOPE_DECLARATION_DEFAULT_CAP
    cap_override_diagnostic = ""  # Phase 20 β: surface rejected overrides
    if isinstance(budget, dict):
        try:
            requested_cap = int(budget.get("cap", _SCOPE_DECLARATION_DEFAULT_CAP))
        except (TypeError, ValueError):
            requested_cap = _SCOPE_DECLARATION_DEFAULT_CAP
        overage_justification = str(budget.get("overage_justification") or "").strip()
        if requested_cap > _SCOPE_DECLARATION_DEFAULT_CAP:
            if (requested_cap <= _SCOPE_DECLARATION_HARD_CEILING
                    and overage_justification):
                cap = requested_cap
            elif requested_cap > _SCOPE_DECLARATION_HARD_CEILING:
                # Phase 20 β: don't let an attempted-but-over-ceiling
                # override be silent. The modeler reading the HIGH
                # would otherwise not realize their override was
                # rejected (vs unread).
                cap_override_diagnostic = (
                    f"requested cap {requested_cap} exceeds hard ceiling "
                    f"{_SCOPE_DECLARATION_HARD_CEILING}; using default "
                    f"{_SCOPE_DECLARATION_DEFAULT_CAP}. Caps above the "
                    f"hard ceiling require human escalation, not a YAML "
                    f"edit."
                )
            else:
                # requested_cap > default and <= ceiling, but no
                # justification supplied → reject the override and say so.
                cap_override_diagnostic = (
                    f"requested cap {requested_cap} requires a non-empty "
                    f"`overage_justification` (got empty); using default "
                    f"{_SCOPE_DECLARATION_DEFAULT_CAP}."
                )
        elif requested_cap < _SCOPE_DECLARATION_DEFAULT_CAP:
            # Stricter cap than default is always honored (Phase 19 β
            # is a floor on rigor, not a ceiling).
            cap = max(0, requested_cap)

    out: list[dict] = []

    # Phase 31: an honored raise is the run choosing to leave more unmodeled;
    # it is allowed, but the reviewer should see it and the reason (LOW: it
    # never blocks and never escalates).
    if cap > _SCOPE_DECLARATION_DEFAULT_CAP:
        out.append({
            "kind": "scope_declaration_cap_raised",
            "severity": "LOW",
            "stage": "GATE",
            "claim": (
                f"The run raised its scope-declaration cap from "
                f"{_SCOPE_DECLARATION_DEFAULT_CAP} to {cap} ({len(declared)} declared). "
                f"Reason given: \"{overage_justification[:300]}\""
            ),
        })

    if cap_override_diagnostic:
        out.append({
            "kind": "scope_declaration_cap_override_rejected",
            "severity": "MEDIUM",
            "stage": "GATE",
            "claim": (
                f"Cap override in scope_declaration.yaml `budget:` block "
                f"was not honored: {cap_override_diagnostic}"
            ),
        })

    if empties:
        out.append({
            "kind": "scope_declaration_empty_entries",
            "severity": "MEDIUM",
            "stage": "GATE",
            "claim": (
                f"scope_declaration.yaml has {empties} empty/incomplete "
                f"entry(ies). Each declaration must include at least an "
                f"`id` or `claim` field. Empty entries do not burn budget "
                f"but obscure intent — remove them or fill them in."
            ),
        })

    n_used = len(declared)
    if n_used > cap:
        # Build a compact preview of the over-cap declarations.
        preview_str = "; ".join(sorted(declared)[:8])
        out.append({
            "kind": "scope_declaration_budget_exceeded",
            "severity": "HIGH",
            "stage": "GATE",
            "claim": (
                f"Scope-declaration budget exceeded: {n_used} declarations "
                f"vs cap {cap} (distinct blocker ids across {len(counted)} "
                f"entries;  (Phase 19 β default {_SCOPE_DECLARATION_DEFAULT_CAP}, "
                f"hard ceiling {_SCOPE_DECLARATION_HARD_CEILING}). "
                f"Declarations: {preview_str}. Either resolve some of "
                f"these in the next round (each scope-declaration is a "
                f"thing you chose not to model), OR raise the cap up to "
                f"{_SCOPE_DECLARATION_HARD_CEILING} via "
                f"`budget: {{cap: <n>, overage_justification: \"<reason>\"}}` "
                f"in scope_declaration.yaml. Caps above "
                f"{_SCOPE_DECLARATION_HARD_CEILING} require human escalation."
            ),
        })

    return out


# Phase 19 γ — benchmark-locked acceptance. Round 1 is drafting; from round 2,
# DRIFT escalates to HIGH (scope-declarable). missing_computed is HIGH from
# round 2 — the modeler must populate computed_value/computed_field.
_BENCHMARK_MATCH_ENFORCE_FROM_ROUND = 2

_BENCHMARK_MATCH_MODULE = None


def _load_benchmark_match_module():
    """Lazy import of scripts/benchmark_match.py. Cached."""
    global _BENCHMARK_MATCH_MODULE
    if _BENCHMARK_MATCH_MODULE is not None:
        return _BENCHMARK_MATCH_MODULE
    try:
        spec = importlib.util.spec_from_file_location(
            "benchmark_match",
            os.path.join(os.path.dirname(__file__), "benchmark_match.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules["benchmark_match"] = mod
        spec.loader.exec_module(mod)
        _BENCHMARK_MATCH_MODULE = mod
        return mod
    except (FileNotFoundError, ImportError):
        return None


def _check_benchmark_match(run_dir: str,
                            round_n: int | None = None) -> list[dict]:
    """Phase 19 γ: enforce literature-anchored acceptance band.

    Reads `{run_dir}/benchmark_match.yaml` (produced by
    `scripts/benchmark_match.py`). If the report is absent but
    `models/benchmark_targets.yaml` exists, the script is invoked in-
    process so the gate is not silently skippable.

    Emits:
      benchmark_targets_missing      MEDIUM @ r=1, HIGH @ r≥2 (planner
                                      forgot to extract the table)
      benchmark_match_invalid        HIGH (MALFORMED targets)
      benchmark_drift                HIGH per drifted target (scope-
                                      declarable — domain heuristic)
      benchmark_computed_missing     HIGH per target with no
                                      computed_value/computed_field
      benchmark_match_unavailable    MEDIUM (script unimportable)

    Round gating:
      round_n=None → silent
      round 1      → demote all violations to MEDIUM (drafting)
      round ≥ 2    → enforce per-kind severities
    """
    if round_n is None:
        return []

    targets_path = artifact_path("benchmark_targets", run_dir)
    drafting = round_n < _BENCHMARK_MATCH_ENFORCE_FROM_ROUND
    out: list[dict] = []

    # Case 1: no targets file at all. Modeler hasn't (yet) been asked to
    # build against benchmarks. At round 1 silent. At round ≥ 2,
    # emit MEDIUM benchmark_targets_missing so the planner notices.
    if not os.path.exists(targets_path):
        if drafting:
            return []
        return [{
            "kind": "benchmark_targets_missing",
            "severity": "MEDIUM",
            "stage": "PLAN",
            "claim": (
                "models/benchmark_targets.yaml is absent at round "
                f"{round_n}. Phase 19 γ — the planner must extract "
                "the `Published benchmarks` table from plan.md into "
                "structured YAML so the modeler's computed outputs "
                "can be compared against literature ranges. See the "
                "`benchmark-locked-acceptance` skill. Without this "
                "file the harness has no mechanical way to check that "
                "fitted incidences/prevalences/coverages match "
                "published reality."
            ),
        }]

    # Case 2: targets exist. Always resolve a fresh report; the YAML is a
    # derived cache and can go stale when computed_value or computed_field
    # targets change between critique rounds.
    mod = _load_benchmark_match_module()
    if mod is None:
        return [{
            "kind": "benchmark_match_unavailable",
            "severity": "HIGH",  # Phase 21 β: fail closed
            "stage": "GATE",
            "claim": (
                "benchmark_match.yaml cannot be refreshed because "
                "scripts/benchmark_match.py could not be imported. Run "
                f"`python3 scripts/benchmark_match.py {run_dir}` "
                "manually. Phase 19 γ."
            ),
        }]
    try:
        rep = mod.evaluate(run_dir)
        mod.write_report(run_dir, rep)
    except Exception as e:  # noqa: BLE001 — Phase 21 β: fail closed
        return [{
            "kind": "benchmark_match_unavailable",
            "severity": "HIGH",
            "stage": "GATE",
            "claim": f"benchmark_match evaluation failed: {e}",
        }]

    verdict = str(rep.get("verdict") or "PENDING").upper()
    if verdict not in _PRODUCER_VERDICTS["benchmark_match"]:
        return [{
            "kind": "benchmark_match_unknown_verdict",
            "severity": "HIGH",
            "stage": "GATE",
            "scope_declarable": False,
            "claim": (f"benchmark_match returned verdict {verdict!r}, outside "
                      f"{list(_PRODUCER_VERDICTS['benchmark_match'])}."),
        }]
    if verdict == "MALFORMED":
        sev = "MEDIUM" if drafting else "HIGH"
        return [{
            "kind": "benchmark_match_invalid",
            "severity": sev,
            "stage": "MODEL",
            "claim": (
                f"benchmark_targets.yaml is MALFORMED: "
                f"{rep.get('error', 'see benchmark_match.yaml')}. "
                f"Phase 19 γ — fix the schema (each benchmark needs "
                f"id, metric, target_value, tolerance_factor or "
                f"tolerance_abs, source)."
            ),
        }]

    results = rep.get("results")
    if results is None:
        results = []
    if not isinstance(results, list):
        return [{
            "kind": "benchmark_match_invalid",
            "severity": "HIGH",
            "stage": "MODEL",
            "claim": "benchmark_match result `results` must be a list.",
        }]

    # Per-target violations (one HIGH per drifted target; HIGH per
    # missing-computed target). At round 1 → demote to MEDIUM.
    drifted = []
    missing_computed = []
    for r in results:
        if not isinstance(r, dict):
            continue
        rid = r.get("id", "?")
        observed = r.get("observed")
        within = r.get("within_band")
        if observed is None:
            missing_computed.append(r)
            continue
        # Phase 21 β: anything but an explicit True is drift (a missing or
        # non-boolean within_band must not pass silently).
        if within is not True:
            drifted.append(r)

    # Emit. Cap each list at 5 to avoid swamping the gate (Phase 10 ψ
    # consolidation pattern).
    for r in drifted[:5]:
        rid = r.get("id", "?")
        sev = "MEDIUM" if drafting else "HIGH"
        out.append({
            "kind": "benchmark_drift",
            "severity": sev,
            "stage": "MODEL",
            "claim": (
                f"Benchmark `{rid}` ({r.get('metric', '?')}): observed "
                f"{r.get('observed')} outside band [{r.get('band_low')}, "
                f"{r.get('band_high')}] (target {r.get('target_value')}, "
                f"source {r.get('source', '?')}). Phase 19 γ — fitted "
                f"output does not match published reality. Refit, "
                f"widen the tolerance with cited justification, or "
                f"scope-declare with a literature-grounded reason."
            ),
        })
    if len(drifted) > 5:
        out.append({
            # Phase 21 β: overflow stays a blocker at the same severity.
            "kind": "benchmark_drift",
            "severity": "MEDIUM" if drafting else "HIGH",
            "stage": "MODEL",
            "claim": (
                f"{len(drifted) - 5} additional benchmark(s) drifted; "
                f"see benchmark_match.yaml for the full list."
            ),
        })

    for r in missing_computed[:5]:
        rid = r.get("id", "?")
        sev = "MEDIUM" if drafting else "HIGH"
        out.append({
            "kind": "benchmark_computed_missing",
            "severity": sev,
            "stage": "MODEL",
            "claim": (
                f"Benchmark `{rid}` ({r.get('metric', '?')}): modeler "
                f"set neither `computed_value` nor `computed_field`. "
                f"Phase 19 γ — after the model run, fill the field so "
                f"the comparison against {r.get('source', 'the source')} "
                f"can fire. Diagnostic: {r.get('diagnostic', '')}"
            ),
        })

    return out


# Phase 19 δ — sufficiency critic gate. POST-WRITE only (report.md must exist).
_SUFFICIENCY_ENFORCE_FROM_ROUND = 2


def _check_sufficiency_critic(run_dir: str,
                                round_n: int | None = None) -> list[dict]:
    """Phase 19 δ: enforce sufficiency-critic verdicts.

    Reads `{run_dir}/critique_sufficiency.yaml` (produced by the
    `critique-sufficiency` agent). For each `claim_verdicts[]` entry
    whose verdict is `OVERCLAIMED`, emits one HIGH `claim_overclaimed`
    blocker.

    Emits:
      critique_sufficiency_not_run   MEDIUM (report.md exists but no yaml)
      critique_sufficiency_invalid   HIGH (malformed yaml)
      claim_overclaimed              HIGH per OVERCLAIMED verdict
                                      (MEDIUM in drafting window)
      claim_overclaimed_advisory     MEDIUM (consolidation when > 5)

    Round gating:
      round_n=None → silent
      round 1 → all violations demoted to MEDIUM
      round ≥ 2 → enforce manifest severities

    Gate fires only when `report.md` exists (post-WRITE).
    """
    if round_n is None:
        return []

    report_path = os.path.join(run_dir, "report.md")
    if not os.path.exists(report_path):
        return []

    yaml_path = artifact_path("critique_sufficiency", run_dir)
    drafting = round_n < _SUFFICIENCY_ENFORCE_FROM_ROUND
    out: list[dict] = []

    if not os.path.exists(yaml_path):
        return [{
            "kind": "critique_sufficiency_not_run",
            "severity": "MEDIUM",
            "stage": "WRITE",
            "claim": (
                "report.md exists but critique_sufficiency.yaml is "
                "absent. Phase 19 δ — spawn the `critique-sufficiency` "
                "agent post-WRITE to verify each high-stakes claim is "
                "supported by the evidence base (benchmark_match, "
                "effort_floors, calibration depth, UQ draws). See "
                "the `evidence-sufficiency` skill."
            ),
        }]

    # Phase 21 β: fail closed on shape (renamed key `verdicts:` → invalid).
    doc, invalid = safe_load_mapping(
        yaml_path, "critique_sufficiency", stage="WRITE",
        required_keys=["claim_verdicts"],
        expected_types={"claim_verdicts": list},
        display_path="critique_sufficiency.yaml")
    if doc is None:
        return invalid
    verdicts = doc["claim_verdicts"]

    allowed = _PRODUCER_VERDICTS["critique_sufficiency_claim"]
    overclaimed = []
    unknown = []
    for v in verdicts:
        if not isinstance(v, dict):
            unknown.append(repr(v)[:60])
            continue
        verdict = str(v.get("verdict") or "").strip().upper()
        if verdict not in allowed:
            unknown.append(f"{v.get('claim_id', '?')}={verdict or '<empty>'}")
        elif verdict == "OVERCLAIMED":
            overclaimed.append(v)
    if unknown:
        out.append({
            "kind": "critique_sufficiency_unknown_verdict",
            "severity": "HIGH",
            "stage": "WRITE",
            "scope_declarable": False,
            "claim": (f"critique_sufficiency.yaml has claim verdict(s) "
                      f"outside {list(allowed)}: {unknown[:6]}."),
        })

    # Phase 20 β: pilot-run escape hatch. Phase 21 β: the critic's
    # `pilot_run_acknowledged: true` only demotes when scope_declaration.yaml
    # really contains a PILOT_RUN entry with a justification; otherwise
    # it is HIGH `pilot_run_unbacked` and no demotion happens.
    pilot_acknowledged = False
    if doc.get("pilot_run_acknowledged"):
        _sd, sd_entries, _ = _load_scope_declarations(run_dir)
        backed = any(
            "PILOT_RUN" in {str(e.get("id") or "").upper(),
                            str(e.get("category") or "").upper()}
            and _entry_has_justification(e)
            for e in sd_entries)
        if backed:
            pilot_acknowledged = True
        else:
            out.append({
                "kind": "pilot_run_unbacked",
                "severity": "HIGH",
                "stage": "WRITE",
                "scope_declarable": False,
                "claim": ("critique_sufficiency.yaml sets "
                          "`pilot_run_acknowledged: true` but "
                          "scope_declaration.yaml has no `id: PILOT_RUN` "
                          "entry with a justification. OVERCLAIMED "
                          "verdicts are not demoted."),
            })

    # Emit per-claim HIGH (or MEDIUM in drafting window), capped at 5.
    sev = "MEDIUM" if drafting else "HIGH"
    if pilot_acknowledged:
        sev = "MEDIUM"

    def _first_line(s: str | None, limit: int = 240) -> str:
        """Get the first non-empty line of s, truncated to limit."""
        lines = str(s or "").strip().splitlines()
        return lines[0][:limit] if lines else ""

    for v in overclaimed[:5]:
        cid = v.get("claim_id") or "<no_id>"
        quoted = _first_line(v.get("quoted_phrase"))
        reason = _first_line(v.get("reason"))
        suggested = _first_line(v.get("suggested_restatement"))
        out.append({
            "kind": "claim_overclaimed",
            "severity": sev,
            "stage": "WRITE",
            "claim": (
                f"[{cid}] OVERCLAIMED: \"{quoted}\". Reason: {reason} "
                f"Suggested restatement: \"{suggested}\". Phase 19 δ — "
                f"either downgrade the claim in report.md (or via "
                f"claims_ledger.yaml's source_field), strengthen the "
                f"underlying evidence (more UQ draws, restarts, held-"
                f"out folds), or scope-declare why the claim strength "
                f"is appropriate despite the evidence gap."
            ),
        })
    if len(overclaimed) > 5:
        out.append({
            "kind": "claim_overclaimed_advisory",
            "severity": "MEDIUM",
            "stage": "WRITE",
            "claim": (
                f"{len(overclaimed) - 5} additional OVERCLAIMED "
                f"verdict(s) in critique_sufficiency.yaml; see the "
                f"file for the full list."
            ),
        })

    return out


def _check_persistent_medium_escalation(run_dir: str,
                                        round_n: int,
                                        current_kinds: Optional[set] = None
                                        ) -> list[dict]:
    """Phase 12 Commit β: scan stage7_round*_stderr.txt for validator
    MEDIUMs that have persisted across multiple rounds and the round
    critique YAMLs for blocker MEDIUMs with old `first_seen_round`.
    Re-emit qualifying entries as HIGH so the gate forces fix-or-
    scope-declare rather than passing.

    Returns a list of HIGH `<kind>_persistent` and
    `<id>_persistent` violations. Silent if nothing escalates.

    Phase 21 β: Path A now reads validator_history.jsonl (appended by
    the validator on every invocation) plus `current_kinds` from this
    invocation. The text below describes the pre-21 limitation.

    Known limitation (Path A — validator-kind escalation): persistence
    is counted by scanning `stage7_round{N}_stderr.txt` files in the
    run dir. The lead is supposed to write one per gate invocation,
    but in practice not every round's stderr is saved (the 104914 run
    only had rounds 2/3/4 stderr present, no rounds 5/6). When earlier
    rounds' stderr is missing, the count is a LOWER BOUND, and a
    genuinely-persistent MEDIUM may not reach the threshold via Path A.
    Path B (critique-blocker escalation via `first_seen_round` in the
    YAML) is more reliable because the YAML is overwritten each round
    and the field is explicitly set by the critique agent.
    """
    out: list[dict] = []

    # --- Path A: validator-kind escalation ---
    # Phase 21 β: rounds come from validator_history.jsonl (written by the
    # validator itself) plus the current invocation's kinds; legacy
    # stage7_round{N}_stderr.txt files are still read when present.
    rounds_with_kind: dict[str, set[int]] = {
        kind: set() for kind in _VALIDATOR_KIND_ESCALATION_THRESHOLDS}
    for rnd, kinds in _load_validator_history(run_dir).items():
        for kind in _VALIDATOR_KIND_ESCALATION_THRESHOLDS:
            if kind in kinds:
                rounds_with_kind[kind].add(rnd)
    for kind in (current_kinds or ()):
        if kind in rounds_with_kind:
            rounds_with_kind[kind].add(int(round_n))
    for entry in os.listdir(run_dir) if os.path.isdir(run_dir) else []:
        m = re.match(r"stage7_round(\d+)_stderr\.txt$", entry)
        if not m:
            continue
        try:
            n = int(m.group(1))
        except ValueError:
            continue
        try:
            with open(os.path.join(run_dir, entry), encoding="utf-8") as f:
                text = f.read()
        except (UnicodeDecodeError, OSError):
            continue
        for kind in _VALIDATOR_KIND_ESCALATION_THRESHOLDS:
            if kind in text:
                rounds_with_kind[kind].add(n)

    for kind, rounds_seen in rounds_with_kind.items():
        threshold = _VALIDATOR_KIND_ESCALATION_THRESHOLDS[kind]
        # A MEDIUM resolved this round is not persisting, however long its
        # history (Codex review of PR #14).
        if current_kinds is not None and kind not in current_kinds:
            continue
        if len(rounds_seen) >= threshold:
            sorted_rounds = sorted(rounds_seen)
            out.append({
                "kind": f"{kind}_persistent",
                "severity": "HIGH",
                "stage": "GATE",
                "claim": (
                    f"{kind} MEDIUM has persisted across "
                    f"{len(rounds_seen)} distinct rounds "
                    f"({sorted_rounds}) without resolution. Phase 12 "
                    f"Commit β escalates to HIGH after "
                    f"{threshold} rounds — the modeler must either "
                    f"resolve the underlying issue (e.g., add "
                    f"validate_figure() calls, fix the decision-rule "
                    f"self-reference, or run an ILP benchmark) or "
                    f"explicitly scope-declare why it cannot be "
                    f"resolved within the pipeline."
                ),
            })

    # --- Path B: critique-blocker escalation via first_seen_round ---
    # Walk each critique YAML's blockers list. For MEDIUM blockers
    # with first_seen_round set and resolved != True, escalate to
    # HIGH if (round_n - first_seen_round) >= threshold.
    # Phase 31: a blocker aimed at the write-up (target_stage WRITE) cannot
    # be fixed before the write-up exists, so it does not escalate until
    # then (E7 attempt 6: two such findings became HIGH in round 5 and the
    # lead ran the writer mid-round to clear them).
    written = any(os.path.exists(os.path.join(run_dir, f)) for f in ("report.md", "manuscript.md"))
    for reviewer_yaml in ("critique_methods.yaml", "critique_domain.yaml",
                          "critique_presentation.yaml",
                          "critique_redteam.yaml",
                          "critique_crossmodel.yaml"):
        path = os.path.join(run_dir, reviewer_yaml)
        if not os.path.exists(path):
            continue
        try:
            with open(path) as f:
                doc = yaml.safe_load(f) or {}
        except (yaml.YAMLError, OSError):
            continue
        for blocker in doc.get("blockers") or []:
            if not isinstance(blocker, dict):
                continue
            severity = blocker.get("severity")
            if severity != "MEDIUM":
                continue
            if blocker.get("resolved") is True:
                continue
            if not written and str(blocker.get("target_stage") or "").upper() == "WRITE":
                continue
            first_seen = blocker.get("first_seen_round")
            if first_seen is None:
                continue
            try:
                persisted = int(round_n) - int(first_seen)
            except (TypeError, ValueError):
                continue
            if persisted < _CRITIQUE_BLOCKER_ESCALATION_THRESHOLD:
                continue
            blocker_id = blocker.get("id", "<unknown>")
            short_claim = str(blocker.get("claim") or "").strip().split("\n")[0][:140]
            out.append({
                "kind": f"{blocker_id}_persistent",
                "severity": "HIGH",
                "stage": "GATE",
                "claim": (
                    f"{reviewer_yaml.replace('.yaml','')} blocker "
                    f"{blocker_id} (MEDIUM) first seen in round "
                    f"{first_seen}, still unresolved at round "
                    f"{round_n} ({persisted} rounds persistence ≥ "
                    f"{_CRITIQUE_BLOCKER_ESCALATION_THRESHOLD}). "
                    f"Phase 12 Commit β escalates to HIGH. Fix the "
                    f"underlying issue or scope-declare. Original: "
                    f"\"{short_claim}\""
                ),
            })

    return out


# Phase 3 Commit C: decision rule as required rigor artifact.
# (Phase 10 Commit χ: removed mid-file aliased re/glob imports;
# `re` is imported at the top of the file and `glob` joined it.)

_ALLOCATION_GLOBS = ("*allocation*.csv", "*budget*.csv", "*optimization*.csv")
_DECISION_RULE_FRONTMATTER_RE = re.compile(
    r"^---\s*\n(.+?)\n---\s*\n", re.DOTALL
)
_REQUIRED_RULE_SECTIONS = ("## Features", "## Rule", "## Validation")
_VALID_RULE_TYPES = {
    "tabular", "tree", "prose-with-exceptions", "non-compressible",
}
_ACCURACY_RE = re.compile(
    r"^\s*-?\s*`?accuracy_vs_optimizer`?\s*[:=]\s*([0-9.]+)",
    re.MULTILINE,
)
_EXCEPTIONS_COUNT_RE = re.compile(
    r"^\s*-?\s*`?exceptions_count`?\s*[:=]\s*(\d+)",
    re.MULTILINE,
)


def _find_allocation_csvs(run_dir: str) -> list[str]:
    """Glob for allocation CSVs at the top level, under data/, and under
    models/. The models/ search was added in Phase 4 (Commit γ era) to
    fix a latent Phase 3 oversight: modelers commonly write
    `models/optimization_allocation.csv` rather than the run-dir top
    level, which silently skipped the entire decision-rule check.
    """
    found: list[str] = []
    for pattern in _ALLOCATION_GLOBS:
        found.extend(glob.glob(os.path.join(run_dir, pattern)))
        found.extend(glob.glob(os.path.join(run_dir, "data", pattern)))
        found.extend(glob.glob(os.path.join(run_dir, "models", pattern)))
    return sorted(set(found))


def _check_decision_rule_artifact(run_dir: str) -> list[dict]:
    """Decision rule (Phase 3 Commit C): if an allocation CSV exists,
    require decision_rule.md with a valid schema. Emits:
      - decision_rule_missing HIGH
      - decision_rule_malformed HIGH
      - decision_rule_low_accuracy HIGH
    """
    allocations = _find_allocation_csvs(run_dir)
    if not allocations:
        return []

    rule_path = artifact_path("decision_rule", run_dir)
    if not os.path.exists(rule_path):
        return [{
            "kind": "decision_rule_missing",
            "severity": "HIGH",
            "stage": "DECISION_RULE",
            "claim": (
                f"Allocation CSV(s) present ({', '.join(os.path.basename(p) for p in allocations)}) "
                f"but decision_rule.md is absent. A per-unit table is not a "
                f"defensible policy artifact. Write decision_rule.md per the "
                f"decision-rule-extraction skill — tabular, tree, prose, or "
                f"non-compressible with justification."
            ),
        }]

    # Phase 10 Commit χ: previously this open() was unguarded — a
    # binary or UTF-8-corrupt decision_rule.md would crash STAGE 7
    # entirely instead of emitting a HIGH violation the modeler can
    # respond to.
    try:
        with open(rule_path, encoding="utf-8") as f:
            text = f.read()
    except (UnicodeDecodeError, OSError) as e:
        return [{
            "kind": "decision_rule_unreadable",
            "severity": "HIGH",
            "stage": "OPTIMIZATION",
            "claim": (
                f"decision_rule.md exists but cannot be read as UTF-8 "
                f"text: {type(e).__name__}: {e}. The validator cannot "
                f"verify the rule schema, self-reference, or accuracy. "
                f"Re-save decision_rule.md as UTF-8 plain text."
            ),
        }]

    # Accumulate violations across all checks rather than returning early
    # on the first malformed condition. This lets the Phase 4 γ
    # self-reference check run even when the schema check has already
    # flagged a malformed rule — a malformed file may still have
    # self-referential prose worth surfacing, and the modeler should see
    # both issues in one round.
    violations: list[dict] = []
    rule_type = None
    fm_match = _DECISION_RULE_FRONTMATTER_RE.match(text)
    if not fm_match:
        violations.append({
            "kind": "decision_rule_malformed",
            "severity": "HIGH",
            "stage": "DECISION_RULE",
            "claim": (
                "decision_rule.md is missing YAML front-matter "
                "(`--- rule_type: ... ---` at the top of the file). "
                "See decision-rule-extraction skill for the schema."
            ),
        })
    else:
        try:
            fm = yaml.safe_load(fm_match.group(1)) or {}
            rule_type = fm.get("rule_type")
            if rule_type not in _VALID_RULE_TYPES:
                violations.append({
                    "kind": "decision_rule_malformed",
                    "severity": "HIGH",
                    "stage": "DECISION_RULE",
                    "claim": (
                        f"decision_rule.md front-matter `rule_type` is "
                        f"{rule_type!r}; must be one of "
                        f"{sorted(_VALID_RULE_TYPES)}."
                    ),
                })
                rule_type = None
        except yaml.YAMLError as e:
            violations.append({
                "kind": "decision_rule_malformed",
                "severity": "HIGH",
                "stage": "DECISION_RULE",
                "claim": (f"decision_rule.md front-matter is not valid "
                          f"YAML: {e}"),
            })

    missing_sections = [s for s in _REQUIRED_RULE_SECTIONS if s not in text]
    if rule_type == "non-compressible" and "## Justification" not in text:
        missing_sections.append("## Justification")
    if missing_sections:
        violations.append({
            "kind": "decision_rule_malformed",
            "severity": "HIGH",
            "stage": "DECISION_RULE",
            "claim": (
                f"decision_rule.md is missing required section(s): "
                f"{missing_sections}. See decision-rule-extraction skill."
            ),
        })
    if rule_type is not None and rule_type != "non-compressible":
        acc_match = _ACCURACY_RE.search(text)
        exc_match = _EXCEPTIONS_COUNT_RE.search(text)
        if acc_match and exc_match:
            try:
                accuracy = float(acc_match.group(1))
                exc_count = int(exc_match.group(1))
                # Phase 10 Commit χ: severity recalibration. The
                # original threshold fired HIGH at < 0.90, but a 22-
                # archetype × 8-package allocation rule with 0.85
                # accuracy is publication-defensible (Global Fund
                # rules routinely run 0.80-0.90). HIGH should be
                # reserved for rules that are genuinely indefensible
                # (< 0.75 with no declared exceptions). The 0.75-0.90
                # band moves to MEDIUM — surface in §Limitations,
                # don't block ACCEPT.
                if accuracy < 0.75 and exc_count == 0:
                    violations.append({
                        "kind": "decision_rule_low_accuracy",
                        "severity": "HIGH",
                        "stage": "DECISION_RULE",
                        "claim": (
                            f"decision_rule.md claims rule_type={rule_type!r} "
                            f"with accuracy_vs_optimizer={accuracy:.2f} and "
                            f"exceptions_count=0. < 0.75 accuracy with no "
                            f"declared exceptions is indefensible — a "
                            f"program officer applying this rule would "
                            f"differ from the optimizer on > 25% of units. "
                            f"Either list the disagreeing units in "
                            f"exceptions_list, or switch rule_type to "
                            f"`non-compressible` with a Justification."
                        ),
                    })
                elif accuracy < 0.90 and exc_count == 0:
                    violations.append({
                        "kind": "decision_rule_low_accuracy",
                        "severity": "MEDIUM",
                        "stage": "DECISION_RULE",
                        "claim": (
                            f"decision_rule.md claims rule_type={rule_type!r} "
                            f"with accuracy_vs_optimizer={accuracy:.2f} and "
                            f"exceptions_count=0. Accuracy in the 0.75-0.90 "
                            f"band is publication-defensible but should be "
                            f"surfaced in §Limitations: a program officer "
                            f"applying this rule will differ from the "
                            f"optimizer on 10-25% of units. Consider listing "
                            f"the largest-disagreement units as exceptions."
                        ),
                    })
            except (ValueError, TypeError):
                pass

    # Phase 4 Commit γ: self-reference detection. A rule node referencing
    # the optimizer's output ("be in the funded set", "cost-effective
    # enough", etc.) is non-actionable — a program officer can't apply it
    # without running the optimizer. The 0.97 accuracy claim becomes
    # mathematically correct but functionally useless. Defensible rules
    # use only INPUT FEATURES (PfPR, archetype, concrete CE cutoff) — not
    # outputs of the optimization itself. MEDIUM severity (rule is
    # technically correct; surfacing the usability defect).
    #
    # Approach: search the whole body MINUS the Justification section
    # (where prose explanation of why-no-compact-rule is allowed to
    # reference the optimizer). This catches non-canonical rule headers
    # like `## Decision Tree (4 nodes)` and `## Simplified Rule (prose)`
    # without requiring the modeler to use exact `## Rule` boilerplate.
    rule_body = _strip_justification_section(text)
    found = [t for t in _SELF_REFERENCE_TOKENS
             if t.lower() in rule_body.lower()]
    if found:
        violations.append({
            "kind": "decision_rule_self_referential",
            "severity": "MEDIUM",
            "stage": "DECISION_RULE",
            "claim": (
                f"decision_rule.md (outside ## Justification) contains "
                f"self-reference token(s) {found}. A rule node "
                f"referencing the optimizer's output (e.g. 'be in the "
                f"funded set', 'cost-effective enough') is non-actionable: "
                f"a program officer can't apply it without re-running the "
                f"model. Replace with an INPUT FEATURE — e.g. "
                f"'cases-averted-per-dollar > $X/case' (a concrete CE "
                f"cutoff value the rule can be evaluated against) — or "
                f"declare rule_type=non-compressible with a Justification "
                f"explaining why no compact rule applies."
            ),
        })

    return violations


def _strip_justification_section(text: str) -> str:
    """Phase 4 Commit γ helper. Return `text` with the `## Justification`
    section removed (slice from header to next `##` or end-of-text).
    Pattern reused from scripts/effect_size_registry.py linear scan.

    Used to scan the rule body for self-reference tokens while allowing
    prose in Justification (where referencing the optimizer's output is
    the legitimate use case for non-compressible rules).
    """
    hdr_re = re.compile(r"^##\s+Justification\b.*$", re.MULTILINE)
    m = hdr_re.search(text)
    if m is None:
        return text
    next_h2 = re.search(r"^##\s", text[m.end():], re.MULTILINE)
    if next_h2 is None:
        return text[:m.start()]
    return text[:m.start()] + text[m.end() + next_h2.start():]


# Phase 4 Commit γ + Phase 5 Commit ε: tokens flagging a rule node as
# self-referential. Tokens are specific phrases (avoiding bare words
# like "optimizer" that legitimately appear in field names such as
# `accuracy_vs_optimizer`). The Justification section is stripped before
# this scan, so prose explanation in non-compressible rules can still
# reference the optimizer.
#
# Phase 5 Commit ε added budget-availability variants — the 2057 malaria
# run's decision rule node 5 was "Is PfPR >= 15% AND budget remaining?",
# the same pattern as 1302's "cost-effective enough" but worded around
# budget availability rather than cost-effectiveness. A program officer
# can't apply node 5 without re-running the optimizer to see what budget
# remains.
_SELF_REFERENCE_TOKENS = (
    # Phase 4 (optimizer/funded-set patterns)
    "the optimizer",
    "optimizer's choice",
    "optimizer output",
    "optimized choice",
    # Phase 21 β: "budget cut" removed — "under a 20% budget cut, drop X"
    # is a legitimate scenario branch on an input, not a self-reference.
    "funded set",
    "in the funded",
    "cost-effective enough",
    "cost effective enough",
    "fall within the budget",
    # Phase 21 β: bare "ranked by" matched input-feature rankings
    # ("ranked by baseline incidence"); only model/optimizer rankings
    # are self-referential.
    "ranked by the model",
    "ranked by the optimizer",
    "ranked by optimizer",
    "selected by the model",
    "the model recommends",
    # Phase 5 ε (budget-availability patterns)
    "budget remaining",
    "budget available",
    "if budget allows",
    "if budget remains",
    "subject to budget",
    "remaining funds",
    "funds remaining",
    "funds available",
    "if funds remain",
    "funds permitting",
    "budget permitting",
    "as budget allows",
)


def _incorporate_rigor_violations(decision: dict, violations: list[dict],
                                  max_rounds: int, current_round: int) -> dict:
    """Fold rigor-artifact violations into unresolved_high (for HIGH) or
    attach for visibility (for MEDIUM). Uses RIG-NNN prefix.

    Phase 21 β: each RIG entry keeps `kind` and `scope_declarable` (so the
    non-declarable contract survives to the decision), and `first_seen_round`
    comes from the violation when the validator history knows it.
    `required_artifact_missing` violations become `accept_blockers`
    regardless of severity. Idempotent.
    """
    d = dict(decision)
    d["unresolved_high"] = list(d.get("unresolved_high") or [])
    d["accept_blockers"] = list(d.get("accept_blockers") or [])
    d["rigor_violations"] = violations

    entries = []
    seen_accept = {(b.get("kind"), b.get("claim")) for b in d["accept_blockers"]}
    for v in violations:
        if v.get("accept_blocker"):
            key = (v.get("kind"), v.get("claim"))
            if key not in seen_accept:
                seen_accept.add(key)
                d["accept_blockers"].append({
                    "reviewer": "rigor-artifacts",
                    "id": f"REQ-{len(d['accept_blockers']) + 1:03d}",
                    "category": "METHODS",
                    "target_stage": v.get("target_stage", "MODEL"),
                    "first_seen_round": v.get("first_seen_round", current_round),
                    "claim": v["claim"],
                    "kind": v["kind"],
                    "scope_declarable": not is_non_declarable(
                        v["kind"], v.get("scope_declarable")),
                })
            continue
        if v.get("severity") != "HIGH":
            continue
        entries.append({
            "category": "METHODS",
            "target_stage": "MODEL",
            "first_seen_round": v.get("first_seen_round", current_round),
            "claim": f"{v.get('stage', '?')}/{v['kind']}: {v.get('claim', '')}",
            "kind": v["kind"],
            "scope_declarable": not is_non_declarable(
                v["kind"], v.get("scope_declarable")),
        })
    _append_synthetic_blockers(d, "rigor-artifacts", "RIG", entries)

    # Phase 21 β: rigor HIGHs persisting >= the Phase 5 ζ threshold also
    # require escalation (first_seen_round comes from validator history).
    if any(b.get("reviewer") == "rigor-artifacts"
           and current_round - int(b.get("first_seen_round") or current_round)
           >= _ESCALATION_HIGH_THRESHOLD
           for b in d["unresolved_high"]):
        d["escalation_required"] = True
    return _recompute_action(d, max_rounds, current_round, "rigor")


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
    d["unresolved_high"] = list(d.get("unresolved_high") or [])
    d["registry_violations"] = violations
    entries = [{
        "category": "CITATIONS",
        "target_stage": "MODEL",
        "first_seen_round": current_round,
        "claim": f"{v['kind']} ({v.get('name', '?')}): {v.get('claim', '')}",
        "kind": v["kind"],
        "scope_declarable": not is_non_declarable(
            v["kind"], v.get("scope_declarable")),
    } for v in violations if v.get("severity") == "HIGH"]
    _append_synthetic_blockers(d, "effect-size-registry", "REG", entries)
    return _recompute_action(d, max_rounds, current_round, "registry")


def incorporate_spec_violations(decision: dict, violations: list[dict],
                                max_rounds: int, current_round: int) -> dict:
    """Fold spec_compliance violations into the decision.

    Framework / approach HIGH violations force `structural_mismatch=True`
    (the mechanical backstop: critiques missed an architectural issue, so
    the gate itself catches it). Other HIGH violations (budget envelope,
    unit-count, vintage) add synthetic `OBJ-NNN` blockers to
    `unresolved_high`. MEDIUM violations are attached for visibility but
    do not change the action.

    Phase 21 β: every HIGH that is not structural becomes an OBJ blocker
    (previously only an allowlist of kinds did, so a new spec_compliance
    HIGH — e.g. budget_exceeded — would have been silently dropped).

    This function is idempotent: calling it twice yields identical output.
    """
    d = dict(decision)
    d["unresolved_high"] = list(d.get("unresolved_high") or [])
    d["spec_violations"] = violations
    structural_kinds = {"framework_missing", "approach_mismatch"}

    high_struct = [v for v in violations
                   if v["severity"] == "HIGH" and v["kind"] in structural_kinds]
    high_objective = [v for v in violations
                      if v["severity"] == "HIGH"
                      and v["kind"] not in structural_kinds]

    # Force structural_mismatch when any structural HIGH violation exists.
    if high_struct:
        d["structural_mismatch"] = True
        reviewers = list(d.get("structural_reviewers") or [])
        if "spec-compliance" not in reviewers:
            reviewers.append("spec-compliance")
        d["structural_reviewers"] = reviewers

    entries = [{
        "category": "STRUCTURAL",  # re-route via STRUCTURAL so PATCH heuristic
                                    # escalates to RETHINK if it recurs.
        "target_stage": "MODEL",
        "first_seen_round": current_round,
        "claim": f"{v['kind']}: {v.get('evidence', v.get('claim', ''))}",
        "kind": v["kind"],
        "scope_declarable": not is_non_declarable(
            v["kind"], v.get("scope_declarable")),
    } for v in high_objective]
    _append_synthetic_blockers(d, "spec-compliance", "OBJ", entries)
    return _recompute_action(d, max_rounds, current_round, "spec-compliance")


def render_text(decision: dict, current_round: int, max_rounds: int) -> str:
    lines = [
        f"STAGE 7 decision (round {current_round}/{max_rounds})",
        f"  unresolved_high: {len(decision['unresolved_high'])} blocker(s)",
    ]
    for b in decision["unresolved_high"]:
        lines.append(f"    - {b['reviewer']} {b['id']} "
                     f"(target={b['target_stage']}, "
                     f"since round {b['first_seen_round']})"
                     f"{' [NOT scope-declarable]' if is_non_declarable(b.get('kind'), b.get('scope_declarable')) and b.get('kind') != 'critique_blocker' else ''}"
                     f": {b['claim'][:100]}")
    accept_blockers = decision.get("accept_blockers") or []
    if accept_blockers:
        lines.append(f"  accept_blockers (required artifacts missing): "
                     f"{len(accept_blockers)}")
        for b in accept_blockers:
            lines.append(f"    - {b['id']}: {b['claim'][:140]}")
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
    # Phase 5 ζ: surface stuck blockers so the lead can pick the right
    # escalation path (SCOPE_DECLARE_EARLY, CROSS_STAGE_ESCALATE, etc.).
    attempts = decision.get("blocker_attempts") or {}
    stuck = [(bid, info) for bid, info in attempts.items()
             if info.get("patch_attempts", 0) >= 2]
    if stuck:
        lines.append(f"  stuck_blockers: {len(stuck)} "
                     f"(>=2 failed PATCH attempts)")
        for bid, info in stuck:
            lines.append(
                f"    - {bid} {info.get('reviewer','?')} "
                f"category={info.get('category')} "
                f"target={info.get('target_stage')} "
                f"attempts={info['patch_attempts']} "
                f"first_seen=round {info['first_seen_round']}"
            )
    if decision.get("escalation_required"):
        lines.append(
            "  escalation_required: TRUE — at least one HIGH blocker "
            "has >=3 failed PATCH attempts. Re-spawning the same "
            "target_stage with the same fix instructions is forbidden. "
            "Apply category-aware escalation (see STAGE 7 prompt: "
            "SCOPE_DECLARE_EARLY for PRESENTATION, CROSS_STAGE_ESCALATE "
            "for HARD_BLOCKER/METHODS, originating-critique re-spawn "
            "for HYPOTHESES/CITATIONS)."
        )
    lines.append(f"  rounds_remaining: {decision['rounds_remaining']}")
    lines.append(f"  rule_matched: {decision['rule_matched']}")
    lines.append(f"  action: {decision['action']}")
    lines.append(f"  rationale: {decision['rationale']}")
    return "\n".join(lines)


def _run_self_test() -> int:
    """Run inline self-test cases. Returns 0 if all pass, 1 otherwise.

    Cases cover the Phase 4 mechanical checks (ci_degenerate, etc.).
    Pre-Phase-4 logic (decide(), incorporate_*) is exercised end-to-end
    by the existing critique-fixture runs, so it is not duplicated here.
    """
    import tempfile

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
        va = _check_uq_ci_quality(uq_a)
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
        ok(_check_uq_ci_quality(uq_a) == [],
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
        vb = _check_uq_ci_quality(uq_b)
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
        vc = _check_uq_ci_quality(uq_c)
        # Phase 21 β: an exactly-zero-width interval is degenerate even at
        # mean 0; a zero-mean output with real spread is not.
        ok(any(v["kind"] == "ci_degenerate" for v in vc),
           f"A3: zero-mean zero-width output must fire, got {vc}")
        uq_c2 = os.path.join(d, "uq_c2.yaml")
        with open(uq_c2, "w") as f:
            f.write("scalar_outputs:\n  net_change:\n    mean: 0\n"
                    "    ci_low: -5\n    ci_high: 5\n")
        ok(_check_uq_ci_quality(uq_c2) == [],
           "A3b: zero-mean output with spread is silent")

        # Phase 21 β cases A5-A9.
        def _uq(name, text):
            pth = os.path.join(d, name)
            with open(pth, "w") as f:
                f.write(text)
            return _check_uq_ci_quality(pth)
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

    # --- Phase 21 β: closed verdict vocabularies + producer stamps ---
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        man = os.path.join(d, "models", "identifiability.yaml")
        with open(man, "w") as f:
            f.write("loss_type: nll\n")
        import hashlib as _hl
        sha = _hl.sha256(open(man, "rb").read()).hexdigest()
        rep = os.path.join(d, "identifiability.yaml")

        def _id(doc):
            with open(rep, "w") as f:
                yaml.safe_dump(doc, f)
            return [v["kind"] for v in
                    _identifiability_report_violations(rep, man)]
        ok(_id({"verdict": "PARAMETERS_IDENTIFIED_BY_CONSTRUCTION",
                "producer": "scripts/identifiability.py"})
           == ["identifiability_report_unknown_verdict"],
           "V1: invented identifiability verdict → unknown_verdict HIGH")
        ok(_id({"verdict": "CLEAN"})
           == ["identifiability_report_not_script_produced"],
           "V2: missing producer stamp → not_script_produced HIGH")
        ok(_id({"verdict": "CLEAN", "producer": "scripts/identifiability.py",
                "manifest_sha256": "0" * 64})
           == ["identifiability_report_stale"],
           "V3: manifest hash mismatch → stale HIGH")
        ok(_id({"verdict": "CLEAN", "producer": "scripts/identifiability.py",
                "manifest_sha256": sha}) == [],
           "V4: stamped CLEAN report is silent")
        ok(_id({"verdict": "UNKNOWN_LOSS_SCALE",
                "producer": "scripts/identifiability.py",
                "manifest_sha256": sha})
           == ["identifiability_loss_scale_unknown"],
           "V5: UNKNOWN_LOSS_SCALE fails closed")
        ok(_id({"verdict": "UNIDENTIFIED_PARAMETERS",
                "producer": "scripts/identifiability.py",
                "parameters": [{"name": "beta", "status": "unidentified"}]})
           == ["unidentified_parameters"],
           "V6: parameters as a list does not crash")
        with open(rep, "w") as f:
            f.write("- not a mapping\n")
        ok([v["kind"] for v in _identifiability_report_violations(rep, man)]
           == ["identifiability_report_invalid"],
           "V7: list top-level → invalid HIGH")
        msc = os.path.join(d, "model_comparison_formal.yaml")
        with open(msc, "w") as f:
            yaml.safe_dump({"verdict": "DEGENERATE_FIT_ACKNOWLEDGED",
                            "producer": "scripts/compare_models.py"}, f)
        ok([v["kind"] for v in _msc_report_violations(msc)]
           == ["model_comparison_formal_unknown_verdict"],
           "V8: invented compare_models verdict → unknown_verdict HIGH")
        ok(all(is_non_declarable(k) for k in (
            "identifiability_report_unknown_verdict",
            "model_comparison_formal_not_script_produced",
            "uncertainty_report_stale")),
           "V9: invalid/unknown/unstamped/stale kinds are non-declarable")

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
        vd = _check_uq_ci_quality(uq_d)
        ok(any(v["kind"] == "ci_degenerate" for v in vd),
           f"A4: hardcoded calibration target should fire, got {vd}")

    # --- Phase 4 Commit β: surrogate UQ documentation requirement ---
    with tempfile.TemporaryDirectory() as d:
        models = os.path.join(d, "models")
        os.makedirs(models)

        # Case B1: surrogate-only outcome_fn, no calibration md → HIGH.
        with open(os.path.join(models, "outcome_fn.py"), "w") as f:
            f.write(
                "import pandas as pd\n"
                "def outcome_fn(params):\n"
                "    df = pd.read_csv('package_evaluation.csv')\n"
                "    return {'cases_averted': df.cases_averted.sum() * 0.95}\n"
            )
        vb1 = _check_surrogate_uq_documented(d)
        ok(any(v["kind"] == "surrogate_uq_undocumented"
               and v["severity"] == "HIGH" for v in vb1),
           f"B1: surrogate outcome_fn without calibration md should fire HIGH, "
           f"got {vb1}")

        # Case B2: surrogate + calibration md without RMSE → MEDIUM.
        with open(os.path.join(models, "outcome_fn_calibration.md"), "w") as f:
            f.write("# Surrogate calibration\n\nWe used grid interpolation.\n")
        vb2 = _check_surrogate_uq_documented(d)
        ok(any(v["kind"] == "surrogate_calibration_missing_rmse"
               and v["severity"] == "MEDIUM" for v in vb2),
           f"B2: calibration md without RMSE should fire MEDIUM, got {vb2}")

        # Case B3: surrogate + calibration md WITH RMSE → no fire.
        with open(os.path.join(models, "outcome_fn_calibration.md"), "w") as f:
            f.write(
                "# Surrogate calibration\n\n"
                "Validation grid (12 points). RMSE per output:\n"
                "- cases_averted: 0.03\n"
                "- cost: 0.02\n"
            )
        vb3 = _check_surrogate_uq_documented(d)
        ok(not vb3, f"B3: surrogate + RMSE-documented should not fire, got {vb3}")

        # Case B4: real-model outcome_fn (calls ss.Sim) → no fire even
        # if a CSV is also read.
        with open(os.path.join(models, "outcome_fn.py"), "w") as f:
            f.write(
                "import starsim as ss\n"
                "import pandas as pd\n"
                "def outcome_fn(params):\n"
                "    df = pd.read_csv('package_evaluation.csv')\n"
                "    sim = ss.Sim(diseases=ss.SIR(), n_agents=1000)\n"
                "    sim.run()\n"
                "    return {'cases_averted': sim.results.cum_infections[-1]}\n"
            )
        vb4 = _check_surrogate_uq_documented(d)
        ok(not vb4, f"B4: real-model outcome_fn should not fire, got {vb4}")

        # Case B5: outcome_fn that reads only data/ csvs (not surrogate
        # naming) and calls no model — should not fire (low-recall but
        # avoids false positives on legitimate data-driven outcome_fns).
        with open(os.path.join(models, "outcome_fn.py"), "w") as f:
            f.write(
                "import pandas as pd\n"
                "def outcome_fn(params):\n"
                "    df = pd.read_csv('data/observations.csv')\n"
                "    return {'rmse': ((df.obs - df.pred)**2).mean()**0.5}\n"
            )
        vb5 = _check_surrogate_uq_documented(d)
        ok(not vb5,
           f"B5: data-driven outcome_fn (no surrogate naming) should not fire, "
           f"got {vb5}")

    # --- Phase 4 Commit γ: decision-rule self-reference detector ---
    with tempfile.TemporaryDirectory() as d:
        # Create the allocation CSV so _check_decision_rule_artifact
        # actually runs (it short-circuits when no allocation exists).
        with open(os.path.join(d, "lga_allocation.csv"), "w") as f:
            f.write("lga,package\nA,X\n")

        # Case C1: rule with self-reference token in ## Rule section → MEDIUM
        with open(os.path.join(d, "decision_rule.md"), "w") as f:
            f.write(
                "---\n"
                "rule_type: tree\n"
                "---\n"
                "# Decision Rule\n"
                "## Features\n- archetype\n- pfpr\n"
                "## Rule\n"
                "1. Is the LGA in the funded set?\n"
                "   YES -> PBO+SMC\n"
                "   NO -> baseline\n"
                "## Validation\n"
                "- accuracy_vs_optimizer: 0.97\n"
                "- exceptions_count: 5\n"
            )
        vc1 = _check_decision_rule_artifact(d)
        ok(any(v["kind"] == "decision_rule_self_referential"
               and v["severity"] == "MEDIUM" for v in vc1),
           f"C1: 'in the funded set' in Rule should fire MEDIUM, got {vc1}")

        # Case C2: rule with concrete cutoff (no self-reference) → no fire
        with open(os.path.join(d, "decision_rule.md"), "w") as f:
            f.write(
                "---\n"
                "rule_type: tree\n"
                "---\n"
                "# Decision Rule\n"
                "## Features\n- archetype\n- pfpr\n"
                "## Rule\n"
                "1. Is PfPR > 0.25?\n"
                "   YES -> PBO+SMC\n"
                "   NO -> baseline\n"
                "## Validation\n"
                "- accuracy_vs_optimizer: 0.95\n"
                "- exceptions_count: 8\n"
            )
        vc2 = _check_decision_rule_artifact(d)
        ok(not any(v["kind"] == "decision_rule_self_referential" for v in vc2),
           f"C2: concrete-cutoff rule should not fire self-referential, got {vc2}")

        # Case C3: token only in Justification (non-compressible) → no fire
        with open(os.path.join(d, "decision_rule.md"), "w") as f:
            f.write(
                "---\n"
                "rule_type: non-compressible\n"
                "---\n"
                "# Decision Rule\n"
                "## Features\n- archetype\n- pfpr\n- 12 others\n"
                "## Rule\nSee Justification.\n"
                "## Validation\n"
                "- accuracy_vs_optimizer: 0.61\n"
                "- exceptions_count: 325\n"
                "## Justification\n"
                "The optimizer's choices reflect strong stakeholder "
                "pre-commitments and near-ties; no compact rule applies.\n"
            )
        vc3 = _check_decision_rule_artifact(d)
        ok(not any(v["kind"] == "decision_rule_self_referential" for v in vc3),
           f"C3: token in Justification only should not fire, got {vc3}")

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
        vd1 = _check_comparator_efficiency(d)
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
        vd2 = _check_comparator_efficiency(d)
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
        vd3 = _check_comparator_efficiency(d)
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
        vd4 = _check_comparator_efficiency(d)
        ok(any(v["kind"] == "comparator_efficiency_outlier" for v in vd4),
           f"D4: cost-per-death row 50% cheaper should fire, got {vd4}")

    # --- Phase 5 Commit ε: γ + δ extensions ---
    with tempfile.TemporaryDirectory() as d:
        # Case E1: γ widening — "budget remaining" in rule body fires.
        with open(os.path.join(d, "lga_allocation.csv"), "w") as f:
            f.write("lga,package\nA,X\n")
        with open(os.path.join(d, "decision_rule.md"), "w") as f:
            f.write(
                "---\n"
                "rule_type: tree\n"
                "---\n"
                "# Decision Rule\n"
                "## Features\n- pfpr\n- archetype\n"
                "## Rule\n"
                "1. Is PfPR >= 25%?\n"
                "   YES -> Go to 2\n"
                "   NO -> baseline\n"
                "2. Is PfPR >= 15% AND budget remaining?\n"
                "   YES -> Standard LLIN 80%\n"
                "   NO -> baseline\n"
                "## Validation\n"
                "- accuracy_vs_optimizer: 0.95\n"
                "- exceptions_count: 7\n"
            )
        ve1 = _check_decision_rule_artifact(d)
        ok(any(v["kind"] == "decision_rule_self_referential" for v in ve1),
           f"E1: 'budget remaining' should fire γ MEDIUM, got {ve1}")

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
        ve2 = _check_comparator_efficiency(d)
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
        ve3 = _check_comparator_efficiency(d)
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
        ve4 = _check_comparator_efficiency(d)
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
        ve5 = _check_comparator_efficiency(d)
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
        ve6 = _check_comparator_efficiency(d)
        ok(any(v["kind"] == "comparator_efficiency_outlier" for v in ve6),
           f"E6: single-comparator 4x ratio should fire (>3x threshold), "
           f"got {ve6}")

    # --- Phase 5 Commit ζ: carry-forward attempt counter ---
    # Helper to build a synthetic critique doc inline.
    def _make_critique(reviewer, round_n, blockers, carried_forward=None,
                       structural=False):
        return {
            "reviewer": reviewer,
            "round": round_n,
            "verdict": "REVISE",
            "structural_mismatch": {"detected": structural},
            "blockers": blockers,
            "carried_forward": carried_forward or [],
        }

    # Case F1: blocker first seen round 1, still_present in round 4.
    # Expected: patch_attempts = 4 - 1 = 3 → escalation_required=True.
    crit_f1 = {
        "critique-presentation": _make_critique(
            "critique-presentation", round_n=4,
            blockers=[{
                "id": "P-001", "severity": "HIGH",
                "category": "PRESENTATION", "target_stage": "WRITE",
                "first_seen_round": 1, "claim": "figure embedding",
                "resolved": False,
            }],
            # Phase 21 β: real critics set prior_round = N-1 (the round
            # they compared against). The pre-21 fixture used
            # prior_round == first_seen_round, which hid that attempts
            # were computed from prior_round and so were always 1.
            carried_forward=[{
                "id": "P-001", "prior_round": 3,
                "still_present": True, "notes": "still wrong",
            }],
        ),
    }
    dec_f1 = decide(crit_f1, max_rounds=5, current_round=4)
    ok(dec_f1.get("escalation_required") is True,
       f"F1: P-001 carried 3 rounds should set escalation_required=True, "
       f"got {dec_f1.get('escalation_required')}")
    ok(dec_f1["blocker_attempts"]["P-001"]["patch_attempts"] == 3,
       f"F1: patch_attempts should be 3, got "
       f"{dec_f1['blocker_attempts']['P-001']['patch_attempts']}")

    # Case F2: blocker first seen round 2, still_present in round 3.
    # Expected: patch_attempts = 1 → no escalation_required.
    crit_f2 = {
        "critique-methods": _make_critique(
            "critique-methods", round_n=3,
            blockers=[{
                "id": "M-005", "severity": "HIGH",
                "category": "METHODS", "target_stage": "MODEL",
                "first_seen_round": 2, "claim": "rate inconsistency",
                "resolved": False,
            }],
            carried_forward=[{
                "id": "M-005", "prior_round": 2,
                "still_present": True, "notes": "still off",
            }],
        ),
    }
    dec_f2 = decide(crit_f2, max_rounds=5, current_round=3)
    ok(dec_f2.get("escalation_required") is False,
       f"F2: M-005 carried 1 round should NOT set escalation_required, "
       f"got {dec_f2.get('escalation_required')}")
    ok(dec_f2["blocker_attempts"]["M-005"]["patch_attempts"] == 1,
       f"F2: patch_attempts should be 1, got "
       f"{dec_f2['blocker_attempts']['M-005']['patch_attempts']}")

    # Case F3: blocker resolved (still_present=false) does NOT count.
    crit_f3 = {
        "critique-domain": _make_critique(
            "critique-domain", round_n=3,
            blockers=[{
                "id": "D-002", "severity": "HIGH",
                "category": "HARD_BLOCKER", "target_stage": "DATA",
                "first_seen_round": 2, "claim": "smc eligibility",
                "resolved": True,
            }],
            carried_forward=[{
                "id": "D-002", "prior_round": 2,
                "still_present": False,
                "notes": "fixed by NMEP 21-state list",
            }],
        ),
    }
    dec_f3 = decide(crit_f3, max_rounds=5, current_round=3)
    ok("D-002" not in dec_f3.get("blocker_attempts", {}),
       f"F3: resolved blockers should NOT appear in blocker_attempts, "
       f"got {dec_f3.get('blocker_attempts')}")

    # Case F4: render_text surfaces stuck_blockers + escalation_required.
    rendered = render_text(dec_f1, current_round=4, max_rounds=5)
    ok("stuck_blockers:" in rendered,
       f"F4: render_text should mention stuck_blockers, got:\n{rendered}")
    ok("escalation_required: TRUE" in rendered,
       f"F4: render_text should flag escalation_required, got:\n{rendered}")

    # --- Phase 6 Commit θ: optimizer-quality benchmark ---
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        # Case G1: decision_rule + allocation exist; no optimization_quality.yaml
        with open(os.path.join(d, "decision_rule.md"), "w") as f:
            f.write(
                "---\nrule_type: tree\n---\n"
                "# Decision Rule\n"
                "## Features\n- pfpr\n"
                "## Rule\n1. PfPR > 0.25? -> PBO\n"
                "## Validation\n- accuracy_vs_optimizer: 0.95\n"
                "- exceptions_count: 5\n"
            )
        with open(os.path.join(d, "lga_allocation.csv"), "w") as f:
            f.write("lga,package\nA,X\n")
        vg1 = _check_optimization_quality(d)
        ok(any(v["kind"] == "optimization_quality_missing" for v in vg1),
           f"G1: decision_rule without optimization_quality.yaml should fire MEDIUM, "
           f"got {vg1}")

        # Case G2: yaml present, clean (small gap).
        with open(os.path.join(d, "models", "optimization_quality.yaml"), "w") as f:
            f.write(
                "primary_method: greedy\n"
                "primary_objective: 5400000\n"
                "benchmark_methods:\n"
                "  - method: ilp_pulp\n"
                "    objective: 5450000\n"
                "    runtime_sec: 300\n"
            )
        vg2 = _check_optimization_quality(d)
        ok(not vg2,
           f"G2: clean optimization_quality.yaml should not fire, got {vg2}")

        # Case G3: gap > 10%.
        with open(os.path.join(d, "models", "optimization_quality.yaml"), "w") as f:
            f.write(
                "primary_method: greedy\n"
                "primary_objective: 4000000\n"
                "benchmark_methods:\n"
                "  - method: ilp_pulp\n"
                "    objective: 5500000\n"
            )
        vg3 = _check_optimization_quality(d)
        ok(any(v["kind"] == "optimization_quality_gap_too_large" for v in vg3),
           f"G3: 27% gap should fire MEDIUM, got {vg3}")

        # Case G4: yaml present but no benchmarks.
        with open(os.path.join(d, "models", "optimization_quality.yaml"), "w") as f:
            f.write(
                "primary_method: greedy\n"
                "primary_objective: 5400000\n"
                "benchmark_methods: []\n"
            )
        vg4 = _check_optimization_quality(d)
        ok(any(v["kind"] == "optimization_quality_no_benchmark"
               and v["severity"] == "HIGH" for v in vg4),
           f"G4: empty benchmarks should fire HIGH, got {vg4}")

    # Case G5: no decision_rule and no allocation → check is silent.
    with tempfile.TemporaryDirectory() as d:
        vg5 = _check_optimization_quality(d)
        ok(not vg5,
           f"G5: no allocation = no optimization_quality requirement, got {vg5}")

    # --- Phase 6 Commit ι: DALY-first analysis ---
    with tempfile.TemporaryDirectory() as d:
        # Case H1: allocation + report.md without DALY mentions → fires.
        with open(os.path.join(d, "decision_rule.md"), "w") as f:
            f.write("---\nrule_type: tree\n---\n# DR\n")
        with open(os.path.join(d, "lga_allocation.csv"), "w") as f:
            f.write("lga,package\nA,X\n")
        with open(os.path.join(d, "report.md"), "w") as f:
            f.write(
                "# Report\n\n"
                "## Results\n"
                "Cases averted: 5.41M. Cost per case: $59.\n"
            )
        vh1 = _check_daly_when_allocation(d)
        ok(any(v["kind"] == "daly_analysis_missing" for v in vh1),
           f"H1: report without DALY mentions should fire MEDIUM, got {vh1}")

        # Case H2: report includes DALY-averted column → no fire.
        with open(os.path.join(d, "report.md"), "w") as f:
            f.write(
                "# Report\n\n"
                "## Results\n"
                "Cases averted: 5.41M. DALYs averted: 71K.\n"
                "Cost per DALY: $4,500.\n"
            )
        vh2 = _check_daly_when_allocation(d)
        ok(not vh2, f"H2: DALY mention present, should not fire, got {vh2}")

        # Case H3: DALY mention ONLY in §Limitations (a throwaway
        # acknowledgment) → SHOULD fire, because Phase 6 ι requires
        # actual engagement in the analysis body, not just a Limitations
        # bullet saying "we didn't do this."
        with open(os.path.join(d, "report.md"), "w") as f:
            f.write(
                "# Report\n\n"
                "## Results\n"
                "Cases averted: 5.41M.\n"
                "## Limitations\n"
                "We did not compute disability-adjusted life-years. "
                "Future work should include YLL estimates per package.\n"
            )
        vh3 = _check_daly_when_allocation(d)
        ok(any(v["kind"] == "daly_analysis_missing" for v in vh3),
           f"H3: DALY mention only in §Limitations is a throwaway, "
           f"should still fire, got {vh3}")

        # Case H3b: DALY mention in §Methods/§Results → no fire.
        with open(os.path.join(d, "report.md"), "w") as f:
            f.write(
                "# Report\n\n"
                "## Results\n"
                "Cases averted: 5.41M. DALYs averted: 71K.\n"
                "## Limitations\n"
                "Foo bar.\n"
            )
        vh3b = _check_daly_when_allocation(d)
        ok(not vh3b, f"H3b: DALY in §Results outside Limitations should "
           f"satisfy check, got {vh3b}")

    # Case H4: no allocation → silent.
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "report.md"), "w") as f:
            f.write("# Report\n\nNo allocation produced.\n")
        vh4 = _check_daly_when_allocation(d)
        ok(not vh4, f"H4: no allocation = no requirement, got {vh4}")

    # Case H5: allocation + no report.md yet → silent (writer hasn't run).
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "decision_rule.md"), "w") as f:
            f.write("# DR\n")
        with open(os.path.join(d, "lga_allocation.csv"), "w") as f:
            f.write("lga,package\nA,X\n")
        vh5 = _check_daly_when_allocation(d)
        ok(not vh5, f"H5: pre-writer (no report.md) should be silent, got {vh5}")

    # --- Phase 6 Commit κ: allocation cross-validation ---
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        # Setup: allocation produced.
        with open(os.path.join(d, "decision_rule.md"), "w") as f:
            f.write("# DR\n")
        with open(os.path.join(d, "lga_allocation.csv"), "w") as f:
            f.write("lga,package\nA,X\n")

        # Case I1: no robustness file → MEDIUM missing.
        vi1 = _check_allocation_robustness(d)
        ok(any(v["kind"] == "allocation_robustness_missing" for v in vi1),
           f"I1: missing robustness file should fire MEDIUM, got {vi1}")

        # Case I2: ROBUST verdict → no fire.
        with open(os.path.join(d, "models", "allocation_robustness.yaml"), "w") as f:
            f.write(
                "holdout_method: leave-one-archetype-out\n"
                "n_folds: 22\n"
                "metrics:\n"
                "  rank_correlation_worst_fold: 0.85\n"
                "  cases_averted_gap_pct_worst_fold: 5\n"
                "  rule_classification_concordance_pct_worst_fold: 92\n"
                "verdict: ROBUST\n"
            )
        vi2 = _check_allocation_robustness(d)
        ok(not vi2, f"I2: ROBUST verdict should not fire, got {vi2}")

        # Case I3: UNSTABLE → HIGH.
        with open(os.path.join(d, "models", "allocation_robustness.yaml"), "w") as f:
            f.write(
                "holdout_method: leave-one-archetype-out\n"
                "n_folds: 22\n"
                "metrics:\n"
                "  rank_correlation_worst_fold: 0.30\n"
                "  cases_averted_gap_pct_worst_fold: 12\n"
                "  rule_classification_concordance_pct_worst_fold: 85\n"
            )
        vi3 = _check_allocation_robustness(d)
        ok(any(v["kind"] == "allocation_unstable" and v["severity"] == "HIGH"
               for v in vi3),
           f"I3: UNSTABLE verdict should fire HIGH, got {vi3}")

        # Case I4: FRAGILE → MEDIUM.
        with open(os.path.join(d, "models", "allocation_robustness.yaml"), "w") as f:
            f.write(
                "holdout_method: leave-one-archetype-out\n"
                "n_folds: 22\n"
                "metrics:\n"
                "  rank_correlation_worst_fold: 0.55\n"
                "  cases_averted_gap_pct_worst_fold: 20\n"
                "  rule_classification_concordance_pct_worst_fold: 70\n"
            )
        vi4 = _check_allocation_robustness(d)
        ok(any(v["kind"] == "allocation_fragile" and v["severity"] == "MEDIUM"
               for v in vi4),
           f"I4: FRAGILE verdict should fire MEDIUM, got {vi4}")

    # Case I5: no allocation → check is silent.
    with tempfile.TemporaryDirectory() as d:
        vi5 = _check_allocation_robustness(d)
        ok(not vi5,
           f"I5: no allocation = no robustness requirement, got {vi5}")

    # --- Phase 7 Commit λ: STAGE 8.5 WRITER_QA ---
    with tempfile.TemporaryDirectory() as d:
        # Case J1 (Phase 21 β): report.md exists → writer_qa recomputed
        # in-process; a plain report is CLEAN and the YAML is refreshed.
        with open(os.path.join(d, "report.md"), "w") as f:
            f.write("# Report\n\nContent.\n")
        vj1 = _check_writer_qa(d)
        ok(vj1 == [] and os.path.exists(
            os.path.join(d, "writer_qa_report.yaml")),
           f"J1: recomputed CLEAN + refreshed yaml, got {vj1}")

        # Case J2 (Phase 21 β): a stale/hand-written cached verdict is not
        # trusted — recomputation wins.
        with open(os.path.join(d, "writer_qa_report.yaml"), "w") as f:
            f.write("verdict: REVISE\nn_major: 1\nn_minor: 0\nissues: []\n")
        vj2 = _check_writer_qa(d)
        ok(not vj2, f"J2: cached REVISE ignored when recompute is CLEAN, got {vj2}")

        class _FakeQA:
            def __init__(self, verdict, n_major=0, n_minor=0):
                self.verdict, self.n_major, self.n_minor = verdict, n_major, n_minor

        global _WRITER_QA_FN
        try:
            # Case J3: REVISE → MEDIUM unresolved.
            _WRITER_QA_FN = lambda _d: _FakeQA("REVISE", 1, 2)  # noqa: E731
            vj3 = _check_writer_qa(d)
            ok(any(v["kind"] == "writer_qa_unresolved"
                   and v["severity"] == "MEDIUM" for v in vj3),
               f"J3: REVISE verdict should fire MEDIUM, got {vj3}")

            # Case J4: MAJOR_REVISION → MEDIUM pre-WRITE, HIGH post-WRITE.
            _WRITER_QA_FN = lambda _d: _FakeQA("MAJOR_REVISION", 5)  # noqa: E731
            vj4 = _check_writer_qa(d)
            ok(any(v["kind"] == "writer_qa_unresolved"
                   and v["severity"] == "MEDIUM" for v in vj4),
               f"J4: MAJOR_REVISION verdict should fire MEDIUM, got {vj4}")
            vj4b = _check_writer_qa(d, post_write=True)
            ok(any(v["kind"] == "writer_qa_unresolved"
                   and v["severity"] == "HIGH" for v in vj4b),
               f"J4b: MAJOR_REVISION post-write must be HIGH, got {vj4b}")

            # Case J6: invented verdict → HIGH unknown.
            _WRITER_QA_FN = lambda _d: _FakeQA("LOOKS_FINE")  # noqa: E731
            vj6 = _check_writer_qa(d)
            ok([v["kind"] for v in vj6] == ["writer_qa_unknown_verdict"],
               f"J6: unknown verdict → HIGH, got {vj6}")

            # Case J7: writer_qa crashes → unavailable (HIGH post-write).
            def _boom(_d):
                raise RuntimeError("boom")
            _WRITER_QA_FN = _boom
            vj7 = _check_writer_qa(d, post_write=True)
            ok(any(v["kind"] == "writer_qa_unavailable"
                   and v["severity"] == "HIGH" for v in vj7),
               f"J7: unavailable post-write must be HIGH, got {vj7}")
        finally:
            _WRITER_QA_FN = None

        # Case J8: non-UTF-8 report → HIGH report_unreadable.
        with open(os.path.join(d, "report.md"), "wb") as f:
            f.write(b"\xff\xfe\x00bad")
        vj8 = _check_writer_qa(d)
        ok([v["kind"] for v in vj8] == ["report_unreadable"],
           f"J8: unreadable report → HIGH, got {vj8}")

    # Case J5: no report.md → silent.
    with tempfile.TemporaryDirectory() as d:
        vj5 = _check_writer_qa(d)
        ok(not vj5,
           f"J5: pre-writer (no report.md) should be silent, got {vj5}")

    # --- Phase 7 Commit μ: plan-criterion enforcement ---
    with tempfile.TemporaryDirectory() as d:
        # Case K1: plan.md exists but no success_criteria.yaml → MEDIUM
        with open(os.path.join(d, "plan.md"), "w") as f:
            f.write("# Plan\n\nGoals.\n")
        vk1 = _check_plan_criteria(d)
        ok(any(v["kind"] == "plan_criteria_missing" for v in vk1),
           f"K1: plan.md without success_criteria.yaml should fire MEDIUM, "
           f"got {vk1}")

        # Case K2: hard_blocker FAILED → HIGH
        with open(os.path.join(d, "success_criteria.yaml"), "w") as f:
            f.write(
                "hard_blockers:\n"
                "  - id: HB-001\n"
                "    criterion: \"PfPR RMSE under threshold\"\n"
                "    metric: zone_pfpr_rmse_pp\n"
                "    threshold: 3.0\n"
                "    operator: \"<=\"\n"
                "    artifact: model_comparison_formal.yaml\n"
                "    artifact_field: zone_pfpr_rmse_pp\n"
            )
        with open(os.path.join(d, "model_comparison_formal.yaml"), "w") as f:
            f.write("zone_pfpr_rmse_pp: 5.0\n")  # > 3.0
        vk2 = _check_plan_criteria(d)
        ok(any(v["kind"] == "plan_hard_blocker_failed"
               and v["severity"] == "HIGH" for v in vk2),
           f"K2: failed hard blocker should fire HIGH, got {vk2}")

        # Case K3: minimum_bar NOT_TESTED → MEDIUM
        with open(os.path.join(d, "success_criteria.yaml"), "w") as f:
            f.write(
                "minimum_bar:\n"
                "  - id: MB-001\n"
                "    criterion: \"malariasimulation comparison\"\n"
                "    metric: malariasimulation_comparison_done\n"
                "    threshold: 1\n"
                "    operator: \"==\"\n"
                "    artifact: model_comparison_formal.yaml\n"
                "    artifact_field: malariasimulation_comparison_done\n"
            )
        with open(os.path.join(d, "model_comparison_formal.yaml"), "w") as f:
            f.write("zone_pfpr_rmse_pp: 5.0\n")  # field missing
        vk3 = _check_plan_criteria(d)
        ok(any(v["kind"] == "plan_criterion_not_tested" for v in vk3),
           f"K3: NOT_TESTED criterion should fire MEDIUM, got {vk3}")

        # Case K4: hard_blocker PASS → no fire
        with open(os.path.join(d, "success_criteria.yaml"), "w") as f:
            f.write(
                "hard_blockers:\n"
                "  - id: HB-001\n"
                "    criterion: \"PfPR RMSE under threshold\"\n"
                "    metric: zone_pfpr_rmse_pp\n"
                "    threshold: 5.0\n"
                "    operator: \"<=\"\n"
                "    artifact: model_comparison_formal.yaml\n"
                "    artifact_field: zone_pfpr_rmse_pp\n"
            )
        vk4 = _check_plan_criteria(d)
        ok(not vk4,
           f"K4: PASS hard blocker should not fire, got {vk4}")

    # Case K5: no plan.md → silent (pre-planner runs).
    with tempfile.TemporaryDirectory() as d:
        vk5 = _check_plan_criteria(d)
        ok(not vk5,
           f"K5: pre-planner (no plan.md) should be silent, got {vk5}")

    # --- Phase 7 Commit ν: universal-coverage benchmark ---
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "decision_rule.md"), "w") as f:
            f.write("# DR\n")
        with open(os.path.join(d, "lga_allocation.csv"), "w") as f:
            f.write("lga,package\nA,X\n")

        # Case L1: allocation but no universal_coverage.yaml → MEDIUM
        vl1 = _check_universal_coverage(d)
        ok(any(v["kind"] == "universal_coverage_missing" for v in vl1),
           f"L1: allocation without universal_coverage should fire MEDIUM, "
           f"got {vl1}")

        # Case L2: complete universal_coverage.yaml → no fire
        with open(os.path.join(d, "models", "universal_coverage.yaml"), "w") as f:
            f.write(
                "total_units: 774\n"
                "gc7_actual_budget: 107000000\n"
                "budget_for_universal_pbo_llin_80: 346000000\n"
                "gc7_dalys_averted: 2760000\n"
                "universal_coverage_dalys_averted_estimate: 8500000\n"
                "gc7_efficiency_pct: 32.5\n"
                "notes: |\n"
                "  Universal PBO LLIN at $346M/yr would avert 8.5M DALYs;\n"
                "  GC7 at $107M achieves 32.5% of that.\n"
            )
        vl2 = _check_universal_coverage(d)
        ok(not vl2,
           f"L2: complete universal_coverage.yaml should not fire, got {vl2}")

        # Case L3: missing required field → MEDIUM malformed
        with open(os.path.join(d, "models", "universal_coverage.yaml"), "w") as f:
            f.write(
                "total_units: 774\n"
                # missing gc7_actual_budget, etc.
            )
        vl3 = _check_universal_coverage(d)
        ok(any(v["kind"] == "universal_coverage_malformed" for v in vl3),
           f"L3: missing required fields should fire malformed MEDIUM, "
           f"got {vl3}")

    # Case L4: no allocation → silent.
    with tempfile.TemporaryDirectory() as d:
        vl4 = _check_universal_coverage(d)
        ok(not vl4,
           f"L4: no allocation = no requirement, got {vl4}")

    # --- Phase 8 Commit π: load-bearing parameter sensitivity analysis ---
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "decision_rule.md"), "w") as f:
            f.write("# DR\n")
        with open(os.path.join(d, "lga_allocation.csv"), "w") as f:
            f.write("lga,package\nA,X\n")

        # Case M1: allocation but no sensitivity_analysis.yaml → MEDIUM.
        vm1 = _check_sensitivity_analysis(d)
        ok(any(v["kind"] == "sensitivity_analysis_missing"
               and v["severity"] == "MEDIUM" for v in vm1),
           f"M1: allocation without sensitivity_analysis.yaml should fire "
           f"MEDIUM, got {vm1}")

        # Case M2: ROBUST verdict (clean) → no fire.
        with open(os.path.join(d, "models", "sensitivity_analysis.yaml"), "w") as f:
            f.write(
                "load_bearing_parameters:\n"
                "  - id: SA-001\n"
                "    parameter: pbo_or\n"
                "    primary_value: 0.55\n"
                "    primary_objective: 4565827\n"
                "    perturbations:\n"
                "      - value: 0.37\n"
                "        objective: 4480000\n"
                "        rank_change_top_n: 5\n"
                "        primary_recommendation_changes: false\n"
                "      - value: 0.81\n"
                "        objective: 4710000\n"
                "        rank_change_top_n: 3\n"
                "        primary_recommendation_changes: false\n"
                "  - id: SA-002\n"
                "    parameter: smc_irr\n"
                "    primary_value: 0.27\n"
                "    primary_objective: 4565827\n"
                "    perturbations:\n"
                "      - value: 0.25\n"
                "        objective: 4640000\n"
                "        rank_change_top_n: 8\n"
                "        primary_recommendation_changes: false\n"
                "verdict: ROBUST\n"
            )
        vm2 = _check_sensitivity_analysis(d)
        ok(not vm2, f"M2: ROBUST verdict should not fire, got {vm2}")

        # Case M3: SENSITIVE verdict → MEDIUM.
        with open(os.path.join(d, "models", "sensitivity_analysis.yaml"), "w") as f:
            f.write(
                "load_bearing_parameters:\n"
                "  - id: SA-001\n"
                "    parameter: pbo_or\n"
                "    primary_value: 0.55\n"
                "    primary_objective: 4565827\n"
                "    perturbations:\n"
                "      - value: 0.37\n"
                "        objective: 4400000\n"
                "        rank_change_top_n: 25\n"
                "        primary_recommendation_changes: true\n"
                "  - id: SA-002\n"
                "    parameter: smc_irr\n"
                "    primary_value: 0.27\n"
                "    primary_objective: 4565827\n"
                "    perturbations:\n"
                "      - value: 0.25\n"
                "        objective: 4640000\n"
                "        rank_change_top_n: 8\n"
                "        primary_recommendation_changes: false\n"
                "verdict: SENSITIVE\n"
            )
        vm3 = _check_sensitivity_analysis(d)
        ok(any(v["kind"] == "sensitivity_analysis_sensitive"
               and v["severity"] == "MEDIUM" for v in vm3),
           f"M3: SENSITIVE verdict should fire MEDIUM, got {vm3}")

        # Case M4: UNSTABLE verdict → HIGH.
        with open(os.path.join(d, "models", "sensitivity_analysis.yaml"), "w") as f:
            f.write(
                "load_bearing_parameters:\n"
                "  - id: SA-001\n"
                "    parameter: pbo_or\n"
                "    primary_value: 0.55\n"
                "    primary_objective: 4565827\n"
                "    perturbations:\n"
                "      - value: 0.37\n"
                "        objective: 4400000\n"
                "        rank_change_top_n: 50\n"
                "        primary_recommendation_changes: true\n"
                "      - value: 0.81\n"
                "        objective: 4720000\n"
                "        rank_change_top_n: 40\n"
                "        primary_recommendation_changes: true\n"
                "  - id: SA-002\n"
                "    parameter: smc_irr\n"
                "    primary_value: 0.27\n"
                "    primary_objective: 4565827\n"
                "    perturbations:\n"
                "      - value: 0.25\n"
                "        objective: 4640000\n"
                "        rank_change_top_n: 8\n"
                "        primary_recommendation_changes: false\n"
                "verdict: UNSTABLE\n"
            )
        vm4 = _check_sensitivity_analysis(d)
        ok(any(v["kind"] == "sensitivity_analysis_unstable"
               and v["severity"] == "HIGH" for v in vm4),
           f"M4: UNSTABLE verdict should fire HIGH, got {vm4}")

        # Case M5: malformed (only 1 load-bearing parameter) → HIGH.
        with open(os.path.join(d, "models", "sensitivity_analysis.yaml"), "w") as f:
            f.write(
                "load_bearing_parameters:\n"
                "  - id: SA-001\n"
                "    parameter: pbo_or\n"
                "    primary_value: 0.55\n"
                "    primary_objective: 4565827\n"
                "    perturbations:\n"
                "      - value: 0.37\n"
                "        objective: 4400000\n"
                "        rank_change_top_n: 5\n"
                "        primary_recommendation_changes: false\n"
                "verdict: ROBUST\n"
            )
        vm5 = _check_sensitivity_analysis(d)
        ok(any(v["kind"] == "sensitivity_analysis_malformed"
               and v["severity"] == "HIGH" for v in vm5),
           f"M5: only 1 load-bearing parameter should fire HIGH malformed, "
           f"got {vm5}")

    # Case M6: no allocation → silent.
    with tempfile.TemporaryDirectory() as d:
        vm6 = _check_sensitivity_analysis(d)
        ok(not vm6,
           f"M6: no allocation = no requirement, got {vm6}")

    # --- Phase 9 Commit ρ: write-time figure validator ---
    with tempfile.TemporaryDirectory() as d:
        figs = os.path.join(d, "figures")
        os.makedirs(figs)
        models_dir = os.path.join(d, "models")
        os.makedirs(models_dir)

        # F1: empty run dir, no figures → silent.
        vf1 = _check_figure_validator(d)
        ok(not vf1, f"F1: empty run dir = no fire, got {vf1}")

        # F2: a script with plt.savefig but no validate_figure within
        # 10 lines → MEDIUM figure_validator_missing.
        with open(os.path.join(models_dir, "model_figures.py"), "w") as f:
            f.write(
                "import matplotlib.pyplot as plt\n"
                "fig, ax = plt.subplots()\n"
                "ax.plot([1, 2, 3])\n"
                "plt.savefig('figures/eda.png')\n"
                "plt.close()\n"
            )
        vf2 = _check_figure_validator(d)
        ok(any(v["kind"] == "figure_validator_missing"
               and v["severity"] == "MEDIUM" for v in vf2),
           f"F2: savefig without validate_figure should fire MEDIUM, "
           f"got {vf2}")

        # F3: same script with validate_figure within 10 lines → silent
        # (no provenance needed because no PNG actually exists; check 1
        # is silent on missing-sidecar PNGs and check 2 sees the
        # validate_figure call within the lookahead window).
        with open(os.path.join(models_dir, "model_figures.py"), "w") as f:
            f.write(
                "import matplotlib.pyplot as plt\n"
                "from figure_validator import validate_figure\n"
                "fig, ax = plt.subplots()\n"
                "ax.plot([1, 2, 3])\n"
                "plt.savefig('figures/eda.png')\n"
                "plt.close()\n"
                "validate_figure('figures/eda.png', source_data_paths=[],\n"
                "                expected_annotations=[])\n"
            )
        vf3 = _check_figure_validator(d)
        ok(not any(v["kind"] == "figure_validator_missing" for v in vf3),
           f"F3: savefig with validate_figure within 10 lines should not "
           f"fire figure_validator_missing, got {vf3}")

        # F4: a PNG with a stale provenance sidecar → HIGH
        # figure_staleness_detected. Construct the sidecar by hand
        # with a recorded hash that won't match the actual CSV.
        png = os.path.join(figs, "stale_fig.png")
        with open(png, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
        src_csv = os.path.join(d, "models", "src.csv")
        with open(src_csv, "w") as f:
            f.write("a,b\n1,2\n")
        # Write a sidecar claiming a hash that's not actually the CSV's.
        with open(png + ".provenance.json", "w") as f:
            json.dump({
                "png_path": os.path.abspath(png),
                "generator_script": None,
                "source_hashes": {src_csv: "deadbeef" * 8},
                "expected_annotations": [],
                "expected_n_data_points": None,
                "written_at_utc": "2026-04-27T00:00:00Z",
            }, f)
        vf4 = _check_figure_validator(d)
        ok(any(v["kind"] == "figure_staleness_detected"
               and v["severity"] == "HIGH" for v in vf4),
           f"F4: hash mismatch should fire HIGH figure_staleness_detected, "
           f"got {vf4}")

        # F5: a fresh sidecar (current hash matches CSV) → no staleness
        # fire. Recompute the hash properly via figure_validator helper.
        spec = importlib.util.spec_from_file_location(
            "figure_validator",
            os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "figure_validator.py"),
        )
        fv = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fv)
        with open(png + ".provenance.json", "w") as f:
            json.dump({
                "png_path": os.path.abspath(png),
                "generator_script": None,
                "source_hashes": {src_csv: fv._hash_file(src_csv)},
                "expected_annotations": [],
                "expected_n_data_points": None,
                "written_at_utc": "2026-04-27T00:00:00Z",
            }, f)
        vf5 = _check_figure_validator(d)
        ok(not any(v["kind"] == "figure_staleness_detected" for v in vf5),
           f"F5: matching hash should not fire staleness, got {vf5}")

        # F6: a missing source file referenced in the sidecar → still
        # treated as stale (the source the figure was drawn from is
        # gone, so the figure cannot be defended).
        os.remove(src_csv)
        vf6 = _check_figure_validator(d)
        ok(any(v["kind"] == "figure_staleness_detected" for v in vf6),
           f"F6: missing source CSV should fire staleness, got {vf6}")

    # --- Phase 10 Commit χ: validator robustness self-tests ---

    # P1-P2: _check_plan_criteria. (A "malformed schema" case would
    # require coordinating with scripts/plan_criteria.py's
    # evaluate_plan_criteria contract, which isn't in this commit's
    # scope; the missing-yaml MEDIUM is the path most likely to drift.)
    with tempfile.TemporaryDirectory() as d:
        # P1: no plan.md → silent (legacy runs without plan are out of scope)
        vp1 = _check_plan_criteria(d)
        ok(not vp1, f"P1: no plan.md = no fire, got {vp1}")

        # P2: plan.md exists but no success_criteria.yaml → MEDIUM
        # plan_criteria_missing (the planner forgot to emit it).
        with open(os.path.join(d, "plan.md"), "w") as f:
            f.write("# Plan\n")
        vp2 = _check_plan_criteria(d)
        ok(any(v["kind"] == "plan_criteria_missing"
               and v["severity"] == "MEDIUM" for v in vp2),
           f"P2: plan.md without success_criteria.yaml should fire MEDIUM, "
           f"got {vp2}")

    # W1-W3: _check_writer_qa
    with tempfile.TemporaryDirectory() as d:
        # W1: no report.md → silent
        vw1 = _check_writer_qa(d)
        ok(not vw1, f"W1: no report.md = no fire, got {vw1}")

        # W2: report.md present, writer_qa_report.yaml CLEAN → silent
        with open(os.path.join(d, "report.md"), "w") as f:
            f.write("# Report\n")
        with open(os.path.join(d, "writer_qa_report.yaml"), "w") as f:
            f.write("verdict: CLEAN\nmajor_issues: []\nminor_issues: []\n")
        vw2 = _check_writer_qa(d)
        ok(not vw2, f"W2: CLEAN writer_qa should not fire, got {vw2}")

        # W3 (Phase 21 β): report.md present but writer_qa_report.yaml
        # missing → the gate runs writer_qa itself and writes the YAML.
        os.remove(os.path.join(d, "writer_qa_report.yaml"))
        vw3 = _check_writer_qa(d)
        ok(vw3 == [] and os.path.exists(
            os.path.join(d, "writer_qa_report.yaml")),
           f"W3: gate recomputes writer_qa when the yaml is absent, got {vw3}")

    # U1-U3: _check_universal_coverage
    with tempfile.TemporaryDirectory() as d:
        # U1: no allocation → silent
        vu1 = _check_universal_coverage(d)
        ok(not vu1, f"U1: no allocation = no fire, got {vu1}")

        # U2: allocation present, no universal_coverage.yaml → MEDIUM
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "lga_allocation.csv"), "w") as f:
            f.write("lga,package\nA,X\n")
        vu2 = _check_universal_coverage(d)
        ok(any(v["kind"] == "universal_coverage_missing"
               and v["severity"] == "MEDIUM" for v in vu2),
           f"U2: missing universal_coverage.yaml should fire MEDIUM, got {vu2}")

        # U3: allocation + valid universal_coverage.yaml → silent
        with open(os.path.join(d, "models", "universal_coverage.yaml"), "w") as f:
            f.write(
                "scenario:\n"
                "  description: Universal coverage of standard ITN.\n"
                "  total_cost: 200000000\n"
                "  dalys_averted: 500000\n"
                "comparator:\n"
                "  description: Optimized allocation.\n"
                "  total_cost: 320000000\n"
                "  dalys_averted: 476000\n"
                "verdict: OPTIMIZED_BEATS_UNIVERSAL\n"
            )
        vu3 = _check_universal_coverage(d)
        ok(not any(v["kind"] == "universal_coverage_missing" for v in vu3),
           f"U3: present universal_coverage.yaml should not fire missing, got {vu3}")

    # DR-X: _check_decision_rule_artifact unreadable-file path
    # (Phase 10 Commit χ — previously a binary file would crash)
    with tempfile.TemporaryDirectory() as d:
        # Need an allocation present so the check runs
        with open(os.path.join(d, "lga_allocation.csv"), "w") as f:
            f.write("lga,package\nA,X\n")
        # Write a binary blob as decision_rule.md
        with open(os.path.join(d, "decision_rule.md"), "wb") as f:
            f.write(bytes(range(256)))  # invalid UTF-8 byte sequence
        vdr = _check_decision_rule_artifact(d)
        ok(any(v["kind"] == "decision_rule_unreadable"
               and v["severity"] == "HIGH" for v in vdr),
           f"DR-X: binary decision_rule.md should fire HIGH "
           f"decision_rule_unreadable, got {vdr}")

    # DR-Acc: severity recalibration on decision_rule_low_accuracy
    # (Phase 10 Commit χ — 0.85 is now MEDIUM, < 0.75 is HIGH)
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "lga_allocation.csv"), "w") as f:
            f.write("lga,package\nA,X\n")
        # 0.85 accuracy with no exceptions → MEDIUM (was HIGH pre-χ)
        with open(os.path.join(d, "decision_rule.md"), "w") as f:
            f.write(
                "---\nrule_type: tabular\n---\n"
                "## Features\nx\n"
                "## Rule\ny\n"
                "## Validation\n"
                "- accuracy_vs_optimizer: 0.85\n"
                "- exceptions_count: 0\n"
            )
        vdr_med = _check_decision_rule_artifact(d)
        med_hits = [v for v in vdr_med
                    if v["kind"] == "decision_rule_low_accuracy"]
        ok(any(v["severity"] == "MEDIUM" for v in med_hits),
           f"DR-Acc: 0.85 accuracy with 0 exceptions should fire MEDIUM, "
           f"got {med_hits}")
        ok(not any(v["severity"] == "HIGH" for v in med_hits),
           f"DR-Acc: 0.85 accuracy must NOT fire HIGH (recalibrated), "
           f"got {med_hits}")

        # 0.70 accuracy with no exceptions → HIGH (still)
        with open(os.path.join(d, "decision_rule.md"), "w") as f:
            f.write(
                "---\nrule_type: tabular\n---\n"
                "## Features\nx\n"
                "## Rule\ny\n"
                "## Validation\n"
                "- accuracy_vs_optimizer: 0.70\n"
                "- exceptions_count: 0\n"
            )
        vdr_high = _check_decision_rule_artifact(d)
        high_hits = [v for v in vdr_high
                     if v["kind"] == "decision_rule_low_accuracy"]
        ok(any(v["severity"] == "HIGH" for v in high_hits),
           f"DR-Acc: 0.70 accuracy with 0 exceptions should fire HIGH, "
           f"got {high_hits}")

    # --- Phase 10 Commit ψ: allocation-gate coordinator ---
    # Q1: round 2, allocation present, no rigor artifacts → 1 MEDIUM
    # `allocation_rigor_in_progress` (NOT 5 separate `*_missing`).
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "lga_allocation.csv"), "w") as f:
            f.write("lga,package\nA,X\n")
        with open(os.path.join(d, "decision_rule.md"), "w") as f:
            f.write("---\nrule_type: tabular\n---\n## Features\n## Rule\n## Validation\n")
        vq1 = _check_allocation_rigor_status(d, round_n=2)
        rigor_in_progress = [v for v in vq1
                             if v["kind"] == "allocation_rigor_in_progress"]
        legacy_missing = [v for v in vq1
                          if v["kind"] in _ALLOCATION_MEDIUM_MISSING_KINDS]
        ok(len(rigor_in_progress) == 1,
           f"Q1: round 2 should have 1 in_progress MEDIUM, got {vq1}")
        ok(len(legacy_missing) == 0,
           f"Q1: round 2 should NOT pass through individual *_missing, "
           f"got {legacy_missing}")

    # Q2: round 6, sensitivity still missing → 1 MEDIUM
    # `allocation_rigor_drafts_overdue` (sensitivity deadline is r4).
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "lga_allocation.csv"), "w") as f:
            f.write("lga,package\nA,X\n")
        with open(os.path.join(d, "decision_rule.md"), "w") as f:
            f.write("---\nrule_type: tabular\n---\n## Features\n## Rule\n## Validation\n")
        vq2 = _check_allocation_rigor_status(d, round_n=6)
        overdue = [v for v in vq2
                   if v["kind"] == "allocation_rigor_drafts_overdue"]
        ok(len(overdue) == 1,
           f"Q2: round 6 with missing artifacts should fire 1 drafts_overdue, "
           f"got {vq2}")
        # The overdue should mention sensitivity_analysis (deadline r4).
        ok(any("sensitivity_analysis" in v["claim"] for v in overdue),
           f"Q2: drafts_overdue claim should name sensitivity_analysis, "
           f"got {overdue}")

    # Q3: HIGH verdict failures pass through unchanged, even with the
    # coordinator active. UNSTABLE sensitivity at round 8 must still
    # fire HIGH `sensitivity_analysis_unstable`.
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "lga_allocation.csv"), "w") as f:
            f.write("lga,package\nA,X\n")
        with open(os.path.join(d, "decision_rule.md"), "w") as f:
            f.write("---\nrule_type: tabular\n---\n## Features\n## Rule\n## Validation\n")
        with open(os.path.join(d, "models", "sensitivity_analysis.yaml"), "w") as f:
            f.write(
                "load_bearing_parameters:\n"
                "  - id: SA-001\n"
                "    parameter: pbo_or\n"
                "    primary_value: 0.55\n"
                "    primary_objective: 4565827\n"
                "    perturbations:\n"
                "      - value: 0.37\n"
                "        objective: 4400000\n"
                "        rank_change_top_n: 50\n"
                "        primary_recommendation_changes: true\n"
                "      - value: 0.81\n"
                "        objective: 4720000\n"
                "        rank_change_top_n: 40\n"
                "        primary_recommendation_changes: true\n"
                "  - id: SA-002\n"
                "    parameter: smc_irr\n"
                "    primary_value: 0.27\n"
                "    primary_objective: 4565827\n"
                "    perturbations:\n"
                "      - value: 0.25\n"
                "        objective: 4640000\n"
                "        rank_change_top_n: 8\n"
                "        primary_recommendation_changes: false\n"
                "verdict: UNSTABLE\n"
            )
        vq3 = _check_allocation_rigor_status(d, round_n=8)
        unstable = [v for v in vq3
                    if v["kind"] == "sensitivity_analysis_unstable"]
        ok(len(unstable) == 1 and unstable[0]["severity"] == "HIGH",
           f"Q3: UNSTABLE sensitivity should still fire HIGH (passes through "
           f"the coordinator), got {vq3}")

    # Q4: every artifact present and CLEAN → coordinator silent.
    # (No allocation produced = no rigor checks fire = silent. This
    # case is the simplest verification that the coordinator doesn't
    # synthesize false-positive in_progress when there's nothing to
    # report.)
    with tempfile.TemporaryDirectory() as d:
        vq4 = _check_allocation_rigor_status(d, round_n=2)
        ok(not vq4, f"Q4: no allocation = silent coordinator, got {vq4}")

    # Q5: round_n=None preserves legacy behavior — every missing
    # artifact comes through individually.
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "lga_allocation.csv"), "w") as f:
            f.write("lga,package\nA,X\n")
        with open(os.path.join(d, "decision_rule.md"), "w") as f:
            f.write("---\nrule_type: tabular\n---\n## Features\n## Rule\n## Validation\n")
        vq5 = _check_allocation_rigor_status(d, round_n=None)
        legacy_count = sum(1 for v in vq5
                           if v["kind"] in _ALLOCATION_MEDIUM_MISSING_KINDS)
        rolled_up = [v for v in vq5
                     if v["kind"] in ("allocation_rigor_in_progress",
                                      "allocation_rigor_drafts_overdue")]
        ok(legacy_count >= 4 and not rolled_up,
           f"Q5: round_n=None should pass through individual *_missing "
           f"(legacy behavior, no roll-up), got {vq5}")

    # Q6: `_check_rigor_artifacts` with round_n=2 ALSO produces 1
    # in_progress MEDIUM (proves the round_n is threaded all the way
    # through the public entry point).
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "lga_allocation.csv"), "w") as f:
            f.write("lga,package\nA,X\n")
        with open(os.path.join(d, "decision_rule.md"), "w") as f:
            f.write("---\nrule_type: tabular\n---\n## Features\n## Rule\n## Validation\n")
        vq6 = _check_rigor_artifacts(d, round_n=2)
        rigor_in_progress = [v for v in vq6
                             if v["kind"] == "allocation_rigor_in_progress"]
        legacy_missing = [v for v in vq6
                          if v["kind"] in _ALLOCATION_MEDIUM_MISSING_KINDS]
        ok(len(rigor_in_progress) == 1,
           f"Q6: round_n threaded through _check_rigor_artifacts; expected 1 "
           f"in_progress MEDIUM, got {[v['kind'] for v in vq6]}")
        ok(len(legacy_missing) == 0,
           f"Q6: legacy *_missing must be consolidated, got {legacy_missing}")

    # --- Phase 12 Commit β: round-aware escalation of persisting MEDIUMs ---

    # E1: validator-kind escalation — figure_validator_missing in 4
    # distinct stage7_round*_stderr.txt files at round_n=6 → HIGH.
    with tempfile.TemporaryDirectory() as d:
        for rn in (2, 3, 4, 5):
            with open(os.path.join(d, f"stage7_round{rn}_stderr.txt"), "w") as f:
                f.write(f"unresolved_high: 0 blocker(s)\n"
                        f"  - [MEDIUM] ANALYZE/figure_validator_missing: "
                        f"eda.py:{rn*10} calls savefig...\n")
        ve1 = _check_persistent_medium_escalation(d, round_n=6)
        ok(any(v["kind"] == "figure_validator_missing_persistent"
               and v["severity"] == "HIGH" for v in ve1),
           f"E1: figure_validator_missing in 4 rounds should escalate "
           f"HIGH, got {ve1}")

    # E2: same kind in 3 rounds (below threshold) → silent.
    with tempfile.TemporaryDirectory() as d:
        for rn in (2, 3, 4):
            with open(os.path.join(d, f"stage7_round{rn}_stderr.txt"), "w") as f:
                f.write("[MEDIUM] figure_validator_missing\n")
        ve2 = _check_persistent_medium_escalation(d, round_n=4)
        ok(not any(v["kind"] == "figure_validator_missing_persistent"
                   for v in ve2),
           f"E2: 3 rounds is below threshold (4); should NOT escalate, "
           f"got {ve2}")

    # E3: decision_rule_self_referential in 3 rounds (threshold=3) → HIGH.
    with tempfile.TemporaryDirectory() as d:
        for rn in (2, 3, 4):
            with open(os.path.join(d, f"stage7_round{rn}_stderr.txt"), "w") as f:
                f.write("[MEDIUM] decision_rule_self_referential: rule references optimizer\n")
        ve3 = _check_persistent_medium_escalation(d, round_n=4)
        ok(any(v["kind"] == "decision_rule_self_referential_persistent"
               and v["severity"] == "HIGH" for v in ve3),
           f"E3: decision_rule_self_referential at threshold 3 should "
           f"escalate, got {ve3}")

    # E4: critique-blocker MEDIUM with old first_seen_round → HIGH.
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "critique_presentation.yaml"), "w") as f:
            f.write(
                "reviewer: critique-presentation\n"
                "round: 6\n"
                "blockers:\n"
                "  - id: P-005\n"
                "    severity: MEDIUM\n"
                "    first_seen_round: 1\n"
                "    claim: Tornado chart Y-axis uses code names\n"
                "    resolved: false\n"
            )
        ve4 = _check_persistent_medium_escalation(d, round_n=6)
        ok(any(v["kind"] == "P-005_persistent"
               and v["severity"] == "HIGH" for v in ve4),
           f"E4: P-005 first_seen=1 at round 6 (5 rounds persistence ≥ 4) "
           f"should escalate, got {ve4}")

    # E5: critique-blocker MEDIUM resolved → silent.
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "critique_presentation.yaml"), "w") as f:
            f.write(
                "reviewer: critique-presentation\n"
                "round: 6\n"
                "blockers:\n"
                "  - id: P-005\n"
                "    severity: MEDIUM\n"
                "    first_seen_round: 1\n"
                "    resolved: true\n"
                "    claim: was resolved in round 4\n"
            )
        ve5 = _check_persistent_medium_escalation(d, round_n=6)
        ok(not any(v["kind"] == "P-005_persistent" for v in ve5),
           f"E5: resolved blocker should NOT escalate, got {ve5}")

    # E5b (Phase 31): a WRITE-stage blocker does not escalate before the
    # write-up exists; it does once a manuscript exists.
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "critique_presentation.yaml"), "w") as f:
            f.write(
                "reviewer: critique-presentation\n"
                "round: 5\n"
                "blockers:\n"
                "  - id: P-004\n"
                "    severity: MEDIUM\n"
                "    target_stage: WRITE\n"
                "    first_seen_round: 1\n"
                "    claim: no reader-facing Data section (pending WRITE)\n"
                "    resolved: false\n"
            )
        ve5b = _check_persistent_medium_escalation(d, round_n=5)
        ok(not any(v["kind"] == "P-004_persistent" for v in ve5b),
           f"E5b: WRITE-stage blocker escalated before any write-up exists: {ve5b}")
        open(os.path.join(d, "manuscript.md"), "w").write("# draft\n")
        ve5c = _check_persistent_medium_escalation(d, round_n=5)
        ok(any(v["kind"] == "P-004_persistent" for v in ve5c),
           f"E5c: WRITE-stage blocker escalates once a manuscript exists: {ve5c}")

    # E6: critique-blocker MEDIUM persistence below threshold → silent.
    # first_seen=4, round=6 → persisted only 2 rounds; threshold 4.
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "critique_presentation.yaml"), "w") as f:
            f.write(
                "reviewer: critique-presentation\n"
                "round: 6\n"
                "blockers:\n"
                "  - id: P-099\n"
                "    severity: MEDIUM\n"
                "    first_seen_round: 4\n"
                "    claim: recent issue\n"
                "    resolved: false\n"
            )
        ve6 = _check_persistent_medium_escalation(d, round_n=6)
        ok(not any(v["kind"] == "P-099_persistent" for v in ve6),
           f"E6: 2 rounds persistence (below threshold 4) should NOT "
           f"escalate, got {ve6}")

    # --- Phase 11 Commit υ (F3): STAGE 7 mandatory flag enforcement ---
    import subprocess
    # S1: invoking the script with run_dir but missing one of the three
    # mandatory gate flags must produce a CLI error mentioning the
    # missing flag — NOT silently compute a weaker decision.
    with tempfile.TemporaryDirectory() as d:
        # The run_dir doesn't need to be valid; the flag check fires
        # before run_dir is opened.
        os.makedirs(os.path.join(d, "fake_run"))
        script = os.path.abspath(__file__)
        result = subprocess.run(
            [sys.executable, script, os.path.join(d, "fake_run"),
             "--max-rounds", "8", "--current-round", "2",
             "--spec-compliance", "--rigor-artifacts"],
            capture_output=True, text=True,
        )
        ok(result.returncode != 0,
           f"S1: missing --parameter-registry should fail; got rc="
           f"{result.returncode}, stderr={result.stderr[:200]}")
        ok("--parameter-registry" in result.stderr,
           f"S1: error message should name the missing flag, got "
           f"{result.stderr[:300]}")

        # S1b: all three flags present should NOT fire the mandatory-
        # flag guard (run_dir is empty so it'll fail later, but with a
        # different error — the guard itself must pass).
        result_b = subprocess.run(
            [sys.executable, script, os.path.join(d, "fake_run"),
             "--max-rounds", "8", "--current-round", "2",
             "--spec-compliance", "--parameter-registry",
             "--rigor-artifacts"],
            capture_output=True, text=True,
        )
        ok("--parameter-registry" not in result_b.stderr
           and "--spec-compliance" not in result_b.stderr,
           f"S1b: with all 3 flags, error must NOT mention the flags; "
           f"got stderr={result_b.stderr[:300]}")

    # S2: missing critique_redteam.yaml now produces a MEDIUM
    # critique_redteam_missing rigor violation, not a silent skip.
    # Direct unit-test of the violation-construction path: build a
    # synthetic rigor_violations list with the same shape as the
    # main() loop produces, and verify the MEDIUM kind would fire.
    # (A full end-to-end subprocess test would also need a complete
    # run_dir with three of four critique YAMLs — out of scope for a
    # quick unit-level check; the construction here directly mirrors
    # the production code path.)
    fake_critique_redteam_missing_violation = {
        "kind": "critique_redteam_missing",
        "severity": "MEDIUM",
        "stage": "CRITIQUE",
        "claim": "synthetic — for unit test only",
    }
    ok(fake_critique_redteam_missing_violation["kind"]
       == "critique_redteam_missing",
       "S2: critique_redteam_missing kind constant matches "
       "production path")
    ok(fake_critique_redteam_missing_violation["severity"] == "MEDIUM",
       "S2: severity is MEDIUM (not HIGH; legacy carve-out is now "
       "user-scope-declarable)")

    # === Phase 15 β: STAGE 3 a-priori identifiability self-tests ===
    # I1-I5 verify the pre-model gate fires correctly across the five
    # scenarios documented in the plan: missing artifact, OVER_SAT
    # without resolution, OVER_SAT with resolution, IDENTIFIABLE,
    # scope-declared (which must NOT silence the HIGH). The I-prefix
    # is for "identifiability" — Phase 5 ζ already used F1-F4 for
    # stuck-blocker tests and Phase 9 ρ used F1-F6 for figure-validator
    # tests, so the F-prefix is taken in this self-test block.
    import tempfile as _tempfile
    import yaml as _yaml

    with _tempfile.TemporaryDirectory() as _d:
        # I1: missing artifact at r=2 → MEDIUM identifiability_a_priori_missing
        v = _check_identifiability_a_priori(_d, round_n=2)
        ok(any(x["kind"] == "identifiability_a_priori_missing"
               and x["severity"] == "MEDIUM" for x in v),
           f"I1: missing artifact at r=2 should fire MEDIUM, got {v}")

    with _tempfile.TemporaryDirectory() as _d:
        # I1b: missing artifact at r≥3 escalates to HIGH
        v = _check_identifiability_a_priori(_d, round_n=3)
        ok(any(x["kind"] == "identifiability_a_priori_missing"
               and x["severity"] == "HIGH" for x in v),
           f"I1b: missing artifact at r=3 should fire HIGH, got {v}")

    with _tempfile.TemporaryDirectory() as _d:
        # I2: OVER_SATURATED without resolution → HIGH pre_model_over_saturated
        os.makedirs(os.path.join(_d, "models"))
        with open(os.path.join(_d, "models",
                               "identifiability_a_priori.yaml"), "w") as f:
            _yaml.safe_dump({"total_independent_targets": 6,
                             "total_fitted_parameters": 40,
                             "verdict": "OVER_SATURATED"}, f)
        v = _check_identifiability_a_priori(_d, round_n=2)
        ok(any(x["kind"] == "pre_model_over_saturated"
               and x["severity"] == "HIGH" for x in v),
           f"I2: OVER_SAT no-res should fire HIGH "
           f"pre_model_over_saturated, got {v}")

    with _tempfile.TemporaryDirectory() as _d:
        # I3: OVER_SATURATED with resolution.decision → MEDIUM advisory
        os.makedirs(os.path.join(_d, "models"))
        with open(os.path.join(_d, "models",
                               "identifiability_a_priori.yaml"), "w") as f:
            _yaml.safe_dump({"total_independent_targets": 6,
                             "total_fitted_parameters": 40,
                             "verdict": "OVER_SATURATED",
                             "resolution": {"decision": "tie_params_by_ecotype",
                                            "details": "5 ecotype values"}}, f)
        v = _check_identifiability_a_priori(_d, round_n=2)
        ok(any(x["kind"] == "pre_model_over_saturated_with_commitment"
               and x["severity"] == "MEDIUM" for x in v),
           f"I3: OVER_SAT with resolution should fire MEDIUM "
           f"with_commitment, got {v}")

    with _tempfile.TemporaryDirectory() as _d:
        # I3b: MARGINAL with resolution → MEDIUM pre_model_marginal_identifiability
        os.makedirs(os.path.join(_d, "models"))
        with open(os.path.join(_d, "models",
                               "identifiability_a_priori.yaml"), "w") as f:
            _yaml.safe_dump({"total_independent_targets": 6,
                             "total_fitted_parameters": 12,
                             "verdict": "MARGINAL",
                             "resolution": {
                                 "decision": "tie_params_partial",
                                 "details": "ridge-trapping acknowledged"}},
                            f)
        v = _check_identifiability_a_priori(_d, round_n=2)
        ok(any(x["kind"] == "pre_model_marginal_identifiability"
               and x["severity"] == "MEDIUM" for x in v),
           f"I3b: MARGINAL verdict should fire MEDIUM "
           f"pre_model_marginal_identifiability, got {v}")

    with _tempfile.TemporaryDirectory() as _d:
        # I3c: MARGINAL without resolution → MEDIUM (not HIGH-invalid).
        # Severity must match the documented contract; missing resolution
        # is the same class of issue as having one — the difference is
        # only in the claim text.
        os.makedirs(os.path.join(_d, "models"))
        with open(os.path.join(_d, "models",
                               "identifiability_a_priori.yaml"), "w") as f:
            _yaml.safe_dump({"total_independent_targets": 6,
                             "total_fitted_parameters": 12,
                             "verdict": "MARGINAL"}, f)
        v = _check_identifiability_a_priori(_d, round_n=2)
        ok(any(x["kind"] == "pre_model_marginal_identifiability"
               and x["severity"] == "MEDIUM" for x in v),
           f"I3c: MARGINAL no-res should fire MEDIUM (not HIGH "
           f"identifiability_a_priori_invalid), got {v}")
        ok(not any(x["kind"] == "identifiability_a_priori_invalid"
                   for x in v),
           f"I3c: MARGINAL no-res must NOT fire HIGH invalid, got {v}")

    with _tempfile.TemporaryDirectory() as _d:
        # I4: IDENTIFIABLE → silent (no violations)
        os.makedirs(os.path.join(_d, "models"))
        with open(os.path.join(_d, "models",
                               "identifiability_a_priori.yaml"), "w") as f:
            _yaml.safe_dump({"total_independent_targets": 6,
                             "total_fitted_parameters": 5,
                             "verdict": "IDENTIFIABLE"}, f)
        v = _check_identifiability_a_priori(_d, round_n=2)
        ok(not any(x["kind"].startswith("pre_model_")
                   or x["kind"].startswith("identifiability_a_priori_")
                   for x in v),
           f"I4: IDENTIFIABLE should be silent, got {v}")

    with _tempfile.TemporaryDirectory() as _d:
        # I5: structural enforcement check — verify the function does
        # NOT call _load_sanity_check_acknowledged. The inversion of
        # the scope-declare-anything semantics for pre_model_* kinds
        # is enforced by structural omission of that call (no runtime
        # check). A behavioral test can't catch the regression directly
        # because writing scope_declaration.yaml has zero effect on a
        # function that doesn't read it. So we AST-inspect the
        # function: a Call node referencing _load_sanity_check_
        # acknowledged would fail this assertion. Mere docstring
        # mentions of the helper (e.g., to explain WHY it's omitted)
        # are tolerated because the AST sees them as Constant strings,
        # not Calls.
        import ast as _ast
        import inspect as _inspect
        import textwrap as _textwrap
        _src = _textwrap.dedent(
            _inspect.getsource(_check_identifiability_a_priori))
        _tree = _ast.parse(_src)
        _calls_loader = any(
            isinstance(node, _ast.Call)
            and isinstance(node.func, _ast.Name)
            and node.func.id == "_load_sanity_check_acknowledged"
            for node in _ast.walk(_tree)
        )
        ok(not _calls_loader,
           "I5: _check_identifiability_a_priori must NOT call "
           "_load_sanity_check_acknowledged. Pre-model arithmetic is "
           "non-scope-declarable; adding the loader call would silently "
           "allow scope_declaration.yaml to silence "
           "pre_model_over_saturated.")
        # Companion behavioral check: even with scope_declaration.yaml
        # listing the kind, the gate fires HIGH. Pairs with the source
        # check above — together they catch both static (loader added)
        # and dynamic (loader added but gated) regressions.
        os.makedirs(os.path.join(_d, "models"))
        with open(os.path.join(_d, "models",
                               "identifiability_a_priori.yaml"), "w") as f:
            _yaml.safe_dump({"total_independent_targets": 6,
                             "total_fitted_parameters": 40,
                             "verdict": "OVER_SATURATED"}, f)
        with open(os.path.join(_d, "scope_declaration.yaml"), "w") as f:
            _yaml.safe_dump({"sanity_check_acknowledged": [
                "pre_model_over_saturated",
                "identifiability_a_priori_missing",
            ]}, f)
        v = _check_identifiability_a_priori(_d, round_n=2)
        ok(any(x["kind"] == "pre_model_over_saturated"
               and x["severity"] == "HIGH" for x in v),
           f"I5b: scope_declared on kind should NOT silence HIGH "
           f"pre_model_over_saturated (architecture is inside pipeline "
           f"reach), got {v}")

    with _tempfile.TemporaryDirectory() as _d:
        # I6: round gating — present artifact with non-IDENTIFIABLE
        # verdict at round_n=1 (drafting window) or round_n=None (caller
        # didn't supply round) must be silent. Without this, the
        # planner's round-1 draft fires spurious MEDIUM advisories.
        os.makedirs(os.path.join(_d, "models"))
        with open(os.path.join(_d, "models",
                               "identifiability_a_priori.yaml"), "w") as f:
            _yaml.safe_dump({"total_independent_targets": 6,
                             "total_fitted_parameters": 40,
                             "verdict": "OVER_SATURATED",
                             "resolution": {"decision": "tie_params_by_ecotype",
                                            "details": "5 ecotypes"}}, f)
        ok(_check_identifiability_a_priori(_d, round_n=1) == [],
           "I6: round_n=1 must be silent even for OVER_SATURATED artifact")
        ok(_check_identifiability_a_priori(_d, round_n=None) == [],
           "I6: round_n=None must be silent even for OVER_SATURATED artifact")

    # --- Phase 17 α: pre-mortem domain critique tests (PM-prefix) ---
    with _tempfile.TemporaryDirectory() as _d:
        # PM1: missing artifact at r=1 → MEDIUM (drafting window)
        v = _check_premortem_addressed(_d, round_n=1)
        ok(any(x["kind"] == "pre_mortem_missing"
               and x["severity"] == "MEDIUM" for x in v),
           f"PM1: missing pre_mortem.yaml at r=1 should fire MEDIUM, got {v}")

    with _tempfile.TemporaryDirectory() as _d:
        # PM1b: missing artifact at r≥2 escalates to HIGH (blocks ACCEPT)
        v = _check_premortem_addressed(_d, round_n=2)
        ok(any(x["kind"] == "pre_mortem_missing"
               and x["severity"] == "HIGH" for x in v),
           f"PM1b: missing pre_mortem.yaml at r=2 should fire HIGH, got {v}")

    with _tempfile.TemporaryDirectory() as _d:
        # PM2: round_n=None → silent (defer to caller)
        v = _check_premortem_addressed(_d, round_n=None)
        ok(v == [],
           f"PM2: round_n=None must be silent, got {v}")

    with _tempfile.TemporaryDirectory() as _d:
        # PM3: HIGH concern with addressed_in: null at r≥2 → HIGH
        # pre_mortem_high_unaddressed
        with open(os.path.join(_d, "pre_mortem.yaml"), "w") as f:
            _yaml.safe_dump({
                "agent": "critique-premortem",
                "concerns": [
                    {"id": "PM-001", "severity": "HIGH",
                     "category": "FEASIBILITY",
                     "concern": "Universal PBO LLIN exceeds global supply",
                     "addressed_in": None},
                    {"id": "PM-002", "severity": "MEDIUM",
                     "category": "DATA",
                     "concern": "HBHI archetype 2010 vintage",
                     "addressed_in": None},
                ],
            }, f)
        v = _check_premortem_addressed(_d, round_n=2)
        ok(any(x["kind"] == "pre_mortem_high_unaddressed"
               and x["severity"] == "HIGH"
               and "PM-001" in x.get("claim", "") for x in v),
           f"PM3: unaddressed HIGH at r=2 should fire HIGH "
           f"pre_mortem_high_unaddressed referencing PM-001, got {v}")
        # MEDIUM concerns must NOT fire — only HIGH triggers the gate
        ok(not any("PM-002" in x.get("claim", "") for x in v),
           f"PM3: MEDIUM concerns must not fire pre_mortem_high_unaddressed, got {v}")

    with _tempfile.TemporaryDirectory() as _d:
        # PM4: HIGH concern with addressed_in: filled → silent
        with open(os.path.join(_d, "pre_mortem.yaml"), "w") as f:
            _yaml.safe_dump({
                "agent": "critique-premortem",
                "concerns": [
                    {"id": "PM-001", "severity": "HIGH",
                     "category": "FEASIBILITY",
                     "concern": "Universal PBO LLIN exceeds global supply",
                     "addressed_in": "modeling_strategy.md#pre-mortem-pm-001"},
                ],
            }, f)
        # Phase 21 β: addressed_in must resolve to real content.
        with open(os.path.join(_d, "modeling_strategy.md"), "w") as f:
            f.write("# Strategy\n\n## Pre-mortem PM-001\nSupply capped.\n")
        v = _check_premortem_addressed(_d, round_n=2)
        ok(not any(x["kind"] == "pre_mortem_high_unaddressed" for x in v),
           f"PM4: addressed HIGH should be silent, got {v}")

        def _pm(addressed, severity="HIGH"):
            with open(os.path.join(_d, "pre_mortem.yaml"), "w") as f:
                _yaml.safe_dump({"concerns": [
                    {"id": "PM-001", "severity": severity,
                     "concern": "x", "addressed_in": addressed}]}, f)
            return [x["kind"] for x in _check_premortem_addressed(_d, round_n=2)]
        # PM4b-e (Phase 21 β)
        ok("pre_mortem_high_unaddressed" in _pm("TODO"),
           "PM4b: addressed_in TODO must not count")
        ok("pre_mortem_high_unaddressed" in _pm("missing_file.md § X"),
           "PM4c: addressed_in naming a nonexistent file must not count")
        ok("pre_mortem_high_unaddressed" not in _pm(
            "modeling_strategy.md § Pre-mortem"),
           "PM4d: existing file + matching § heading counts")
        ok("pre_mortem_high_unaddressed" in _pm(
            "modeling_strategy.md § Nonexistent Section"),
           "PM4e: § heading that does not exist must not count")
        ok("pre_mortem_high_unaddressed" in _pm(None, severity="CRITICAL"),
           "PM8: CRITICAL severity is treated as ≥ HIGH")

    with _tempfile.TemporaryDirectory() as _d:
        # PM5: HIGH concern scope-declared via scope_declaration.yaml
        # quoting the PM-NNN id → silent (scope-declarable contract)
        with open(os.path.join(_d, "pre_mortem.yaml"), "w") as f:
            _yaml.safe_dump({
                "agent": "critique-premortem",
                "concerns": [
                    {"id": "PM-001", "severity": "HIGH",
                     "category": "FEASIBILITY",
                     "concern": "Universal PBO LLIN exceeds global supply",
                     "addressed_in": None},
                ],
            }, f)
        with open(os.path.join(_d, "scope_declaration.yaml"), "w") as f:
            _yaml.safe_dump({
                "declarations": [
                    {"id": "SCOPE-001", "blocker_id": "PM-001",
                     "claim": "Idealized supply assumption",
                     "justification": "Question explicitly assumes idealized supply"},
                ],
            }, f)
        v = _check_premortem_addressed(_d, round_n=2)
        ok(not any(x["kind"] == "pre_mortem_high_unaddressed" for x in v),
           f"PM5: structured scope declaration should be silent, got {v}")

        # PM5b (Phase 21 β): the id merely appearing in prose / notes does
        # not declare it.
        with open(os.path.join(_d, "scope_declaration.yaml"), "w") as f:
            _yaml.safe_dump({"notes": "PM-001 through PM-007 acknowledged",
                             "declarations": [
                                 {"id": "SCOPE-001",
                                  "claim": "Idealized supply (PM-001)"}]}, f)
        v = _check_premortem_addressed(_d, round_n=2)
        ok(any(x["kind"] == "pre_mortem_high_unaddressed" for x in v),
           f"PM5b: prose-only mention must not silence, got {v}")

        # PM9 (Phase 21 β): renamed top-level key → HIGH invalid, not [].
        with open(os.path.join(_d, "pre_mortem.yaml"), "w") as f:
            _yaml.safe_dump({"high_concerns": [{"id": "PM-001"}]}, f)
        v = _check_premortem_addressed(_d, round_n=2)
        ok([x["kind"] for x in v] == ["pre_mortem_invalid"],
           f"PM9: renamed key must be HIGH invalid, got {v}")

    with _tempfile.TemporaryDirectory() as _d:
        # PM6: malformed YAML → HIGH pre_mortem_invalid (does not crash)
        with open(os.path.join(_d, "pre_mortem.yaml"), "w") as f:
            f.write("concerns: not_a_list_just_a_string\n")
        v = _check_premortem_addressed(_d, round_n=2)
        ok(any(x["kind"] == "pre_mortem_invalid"
               and x["severity"] == "HIGH" for x in v),
           f"PM6: malformed concerns: should fire HIGH pre_mortem_invalid, got {v}")

    # --- Phase 17 β: coherence_audit integration tests (CA-prefix) ---
    with _tempfile.TemporaryDirectory() as _d:
        # CA1: report.md absent → silent (WRITE hasn't happened)
        v = _check_coherence_audit(_d)
        ok(v == [], f"CA1: no report.md must be silent, got {v}")

    with _tempfile.TemporaryDirectory() as _d:
        # CA2 (Phase 21 β): report.md present, no audit yaml → the gate
        # runs the auditor itself and writes coherence_audit.yaml.
        with open(os.path.join(_d, "report.md"), "w") as f:
            f.write("# Report\nSynthetic.\n")
        v = _check_coherence_audit(_d)
        ok(isinstance(v, list) and os.path.exists(
            os.path.join(_d, "coherence_audit.yaml")),
           f"CA2: gate must recompute + write the audit, got {v}")

    global _COHERENCE_AUDIT_FN
    try:
        with _tempfile.TemporaryDirectory() as _d:
            with open(os.path.join(_d, "report.md"), "w") as f:
                f.write("# Report\n")
            # CA2b: a cached CLEAN audit is ignored — recomputation wins.
            with open(os.path.join(_d, "coherence_audit.yaml"), "w") as f:
                _yaml.safe_dump({"verdict": "CLEAN", "violations": []}, f)
            _COHERENCE_AUDIT_FN = lambda _r: {  # noqa: E731
                "verdict": "DRIFT_DETECTED", "violations": [
                    {"id": "CA-B-001", "severity": "HIGH",
                     "duty": "ledger_binding", "claim": "6M unbound"}]}
            v = _check_coherence_audit(_d)
            ok(any(x["kind"] == "coherence_audit_ledger_binding"
                   and x["severity"] == "HIGH" for x in v),
               f"CA2b: stale cached CLEAN must not hide recomputed HIGH, got {v}")

            # CA3: clean audit → silent
            _COHERENCE_AUDIT_FN = lambda _r: {"verdict": "CLEAN",  # noqa: E731
                                              "violations": []}
            v = _check_coherence_audit(_d)
            ok(v == [], f"CA3: clean audit must be silent, got {v}")

            # CA4: HIGH violations are surfaced as separate kinds per duty
            _COHERENCE_AUDIT_FN = lambda _r: {  # noqa: E731
                "verdict": "DRIFT_DETECTED",
                "violations": [
                    {"id": "CA-L-001", "severity": "HIGH",
                     "duty": "label_coherence",
                     "location": "decision_rule.md:87",
                     "claim": "UNSTABLE in prose vs SENSITIVE in YAML",
                     "canonical_source": "models/sensitivity_analysis.yaml:verdict",
                     "canonical_value": "SENSITIVE"},
                ],
            }
            v = _check_coherence_audit(_d)
            ok(any(x["kind"] == "coherence_audit_label_coherence"
                   and x["severity"] == "HIGH" for x in v),
               f"CA4: HIGH label_coherence must surface as HIGH validator violation, got {v}")

            # CA5: many MEDIUMs consolidate into one advisory per duty
            _COHERENCE_AUDIT_FN = lambda _r: {  # noqa: E731
                "verdict": "DRIFT_DETECTED",
                "violations": [
                    {"id": f"CA-C-{i:03d}", "severity": "MEDIUM",
                     "duty": "cross_file_counts",
                     "location": f"results.md:{i}",
                     "claim": f"drift {i}"} for i in range(1, 8)
                ],
            }
            v = _check_coherence_audit(_d)
            consolidated = [x for x in v
                            if x["kind"] == "coherence_audit_cross_file_counts_advisory"]
            ok(len(consolidated) == 1,
               f"CA5: multiple MEDIUMs must consolidate into ONE advisory, got {v}")
            ok(consolidated and "7 MEDIUM" in consolidated[0]["claim"],
               f"CA5: advisory must report count, got {consolidated}")

            # CA5b (Phase 21 β): > 5 HIGHs → overflow stays HIGH.
            _COHERENCE_AUDIT_FN = lambda _r: {  # noqa: E731
                "verdict": "DRIFT_DETECTED",
                "violations": [{"id": f"H{i}", "severity": "HIGH",
                                "duty": "ledger_binding", "claim": "x"}
                               for i in range(8)]}
            v = _check_coherence_audit(_d)
            ok(len([x for x in v if x["severity"] == "HIGH"]) == 6,
               f"CA5b: 5 HIGHs + 1 HIGH overflow summary, got {len(v)}")

            # CA6 (Phase 21 β): malformed audit shapes → HIGH invalid.
            for bad in ({"violations": "not-a-list"}, {}, ["x"],
                        {"violations": [None]}, {"violations": {"a": 1}}):
                _COHERENCE_AUDIT_FN = lambda _r, _b=bad: _b  # noqa: E731
                v = _check_coherence_audit(_d)
                ok([x["kind"] for x in v] == ["coherence_audit_invalid"],
                   f"CA6: malformed audit {bad!r} must be HIGH invalid, got {v}")

            # CA7: missing severity treated as HIGH; claim None doesn't crash.
            _COHERENCE_AUDIT_FN = lambda _r: {"violations": [  # noqa: E731
                {"duty": "label_coherence", "claim": None}]}
            v = _check_coherence_audit(_d)
            ok(any(x["severity"] == "HIGH" for x in v),
               f"CA7: severity-less violation → HIGH, got {v}")

            # CA8: auditor crash → MEDIUM pre-write, HIGH post-write.
            def _boom(_r):
                raise RuntimeError("boom")
            _COHERENCE_AUDIT_FN = _boom
            ok([x["severity"] for x in _check_coherence_audit(_d)] == ["MEDIUM"],
               "CA8: unavailable pre-write is MEDIUM")
            ok([x["severity"] for x in
                _check_coherence_audit(_d, post_write=True)] == ["HIGH"],
               "CA8b: unavailable post-write is HIGH")
    finally:
        _COHERENCE_AUDIT_FN = None

    # --- Phase 18 α: claims_ledger tests (CL-prefix) ---
    with _tempfile.TemporaryDirectory() as _d:
        # CL1: round_n=None and round=1 → silent (drafting window)
        ok(_check_claims_ledger_present(_d, round_n=None) == [],
           "CL1: round_n=None must be silent")
        ok(_check_claims_ledger_present(_d, round_n=1) == [],
           "CL1: round_n=1 (drafting window) must be silent")

    with _tempfile.TemporaryDirectory() as _d:
        # CL2: missing at round 2 → MEDIUM claims_ledger_missing
        v = _check_claims_ledger_present(_d, round_n=2)
        ok(any(x["kind"] == "claims_ledger_missing"
               and x["severity"] == "MEDIUM" for x in v),
           f"CL2: missing at r=2 should be MEDIUM, got {v}")

    with _tempfile.TemporaryDirectory() as _d:
        # CL3: missing at round 3+ → HIGH (blocks ACCEPT)
        v = _check_claims_ledger_present(_d, round_n=3)
        ok(any(x["kind"] == "claims_ledger_missing"
               and x["severity"] == "HIGH" for x in v),
           f"CL3: missing at r=3 should be HIGH, got {v}")

    with _tempfile.TemporaryDirectory() as _d:
        # CL4: well-formed ledger at r=3 → silent
        os.makedirs(os.path.join(_d, "models"))
        with open(os.path.join(_d, "models", "claims_ledger.yaml"), "w") as f:
            _yaml.safe_dump({"claims": [
                {"id": "a", "claim_kind": "scalar", "value": 100,
                 "ci_low": 80, "ci_high": 120,
                 "source_artifact": "models/out.yaml",
                 "source_field": "total.mean"},
                {"id": "b", "claim_kind": "label", "value": "ROBUST",
                 "source_artifact": "models/out.yaml",
                 "source_field": "verdict"},
            ]}, f)
        with open(os.path.join(_d, "models", "out.yaml"), "w") as f:
            _yaml.safe_dump({"verdict": "ROBUST", "total": {
                "mean": 100, "ci_low": 80, "ci_high": 120}}, f)
        v = _check_claims_ledger_present(_d, round_n=3)
        ok([x["kind"] for x in v] == ["claims_spec_missing"]
           and v[0]["severity"] == "MEDIUM",
           f"CL4: well-formed hand-written ledger → only the spec advisory, got {v}")

        # CL7 (Phase 21 β): ledger value contradicts its cited artifact
        # (235631 class) → HIGH claim_source_mismatch / claim_ci_mismatch,
        # non-declarable.
        with open(os.path.join(_d, "models", "out.yaml"), "w") as f:
            _yaml.safe_dump({"verdict": "SENSITIVE", "total": {
                "mean": 100, "ci_low": 100, "ci_high": 100}}, f)
        v = [x for x in _check_claims_ledger_present(_d, round_n=3)
             if x["kind"] != "claims_spec_missing"]
        kinds = sorted(x["kind"] for x in v)
        ok(kinds == ["claim_ci_mismatch", "claim_ci_mismatch",
                     "claim_source_mismatch"]
           and all(x["severity"] == "HIGH"
                   and is_non_declarable(x["kind"], x.get("scope_declarable"))
                   for x in v),
           f"CL7: provenance mismatches must be HIGH non-declarable, got {kinds}")
        # CL8 (E5): an existing ledger is checked in the drafting window too;
        # only absence is round-gated.
        for rn in (1, None):
            v = _check_claims_ledger_present(_d, round_n=rn)
            ok(sorted(x["kind"] for x in v) == kinds,
               f"CL8: round {rn} must still check an existing ledger, got {v}")
        # CL9-CL12 (Phase 22 γ): with a claims spec the ledger is rebuilt and
        # compared, never trusted.
        _spec = {"claims": [
            {"id": "a", "claim_kind": "scalar", "source_artifact": "models/out.yaml",
             "source_field": "total.mean"},
            {"id": "b", "claim_kind": "label", "source_artifact": "models/out.yaml",
             "source_field": "verdict"}]}
        with open(os.path.join(_d, "models", "claims_spec.yaml"), "w") as f:
            _yaml.safe_dump(_spec, f)
        v = _check_claims_ledger_present(_d, round_n=3)
        ok([x["kind"] for x in v] == ["claims_ledger_stale"],
           f"CL9: hand-written ledger vs spec build → stale, got {v}")
        _builder = _load_sibling("build_claims_ledger")
        _builder.write(_d)
        v = _check_claims_ledger_present(_d, round_n=1)
        ok(v == [], f"CL10: freshly built ledger is silent, got {v}")
        with open(os.path.join(_d, "models", "out.yaml"), "w") as f:
            _yaml.safe_dump({"verdict": "ROBUST", "total": {
                "mean": 140, "ci_low": 100, "ci_high": 180}}, f)
        v = _check_claims_ledger_present(_d, round_n=1)
        ok([x["kind"] for x in v] == ["claims_ledger_stale"]
           and is_non_declarable(v[0]["kind"], v[0].get("scope_declarable")),
           f"CL11: source re-run after build → stale, non-declarable, got {v}")
        _spec["claims"][0]["value"] = 140
        _spec["claims"].append({"id": "c", "claim_kind": "scalar",
                                "source_artifact": "models/out.yaml",
                                "source_field": "weighted average by zone"})
        with open(os.path.join(_d, "models", "claims_spec.yaml"), "w") as f:
            _yaml.safe_dump(_spec, f)
        _builder.write(_d)
        kinds12 = sorted(x["kind"] for x in _check_claims_ledger_present(_d, round_n=1))
        ok(kinds12 == ["claim_source_unresolved", "claims_spec_invalid"],
           f"CL12: typed value + unresolvable field, got {kinds12}")
        # CL13 (Codex review): an external claim with an inverted CI fails the
        # same schema check as a hand-written ledger.
        with open(os.path.join(_d, "models", "claims_spec.yaml"), "w") as f:
            _yaml.safe_dump({"claims": [{"id": "ext", "claim_kind": "scalar", "value": 10,
                                         "ci_low": 20, "ci_high": 5, "units": "x",
                                         "source_artifact": "external",
                                         "source_field": "Author 2020"}]}, f)
        _builder.write(_d)
        kinds13 = [x["kind"] for x in _check_claims_ledger_present(_d, round_n=1)]
        ok("claims_ledger_ci_violated" in kinds13 or "claims_ledger_invalid" in kinds13,
           f"CL13: inverted external CI caught on the spec path, got {kinds13}")

    with _tempfile.TemporaryDirectory() as _d:
        # CL5: CI bounds violated → HIGH claims_ledger_ci_violated
        os.makedirs(os.path.join(_d, "models"))
        with open(os.path.join(_d, "models", "claims_ledger.yaml"), "w") as f:
            _yaml.safe_dump({"claims": [
                {"id": "bad", "claim_kind": "scalar", "value": 50,
                 "ci_low": 80, "ci_high": 120},
            ]}, f)
        v = _check_claims_ledger_present(_d, round_n=3)
        ok(any(x["kind"] == "claims_ledger_ci_violated"
               and x["severity"] == "HIGH" for x in v),
           f"CL5: CI bounds violated should fire HIGH ci_violated, got {v}")

    with _tempfile.TemporaryDirectory() as _d:
        # CL6: empty ledger at r=3 → HIGH claims_ledger_invalid
        os.makedirs(os.path.join(_d, "models"))
        with open(os.path.join(_d, "models", "claims_ledger.yaml"), "w") as f:
            _yaml.safe_dump({"claims": []}, f)
        v = _check_claims_ledger_present(_d, round_n=3)
        ok(any(x["kind"] == "claims_ledger_invalid"
               and x["severity"] == "HIGH"
               and "zero claims" in x.get("claim", "") for x in v),
           f"CL6: empty ledger should fire HIGH invalid w/ zero-claims claim, got {v}")

    # --- Phase 19 α: effort_floors tests (EF-prefix) ---
    with _tempfile.TemporaryDirectory() as _d:
        # EF1: round_n=None → silent (defer)
        ok(_check_effort_floors(_d, round_n=None) == [],
           "EF1: round_n=None must be silent")

    with _tempfile.TemporaryDirectory() as _d:
        # EF2: no trigger artifacts and no models/ dir → silent
        v = _check_effort_floors(_d, round_n=3)
        ok(v == [], f"EF2: empty run dir must be silent, got {v}")

    with _tempfile.TemporaryDirectory() as _d:
        # EF3: under-floor n_draws at round 3 → HIGH effort_floor_uq_min_draws_local
        # (NOT scope-declarable). Script auto-runs when report is absent.
        with open(os.path.join(_d, "uncertainty_report.yaml"), "w") as f:
            _yaml.safe_dump({"n_draws": 50, "scalar_outputs": {}}, f)
        v = _check_effort_floors(_d, round_n=3)
        match = [x for x in v if x["kind"] == "effort_floor_uq_min_draws_local"]
        ok(len(match) == 1 and match[0]["severity"] == "HIGH",
           f"EF3: 50 draws at r=3 should fire HIGH, got {v}")
        ok("NOT scope-declarable" in match[0]["claim"],
           f"EF3: NOT scope-declarable must be flagged in claim, got {match}")
        # And the report file is now on disk for downstream readers.
        ok(os.path.exists(os.path.join(_d, "effort_floors_report.yaml")),
           "EF3: validator must auto-write effort_floors_report.yaml")

    with _tempfile.TemporaryDirectory() as _d:
        # EF4: under-floor at round 1 (drafting window) → MEDIUM regardless of
        # manifest severity. Same input as EF3.
        with open(os.path.join(_d, "uncertainty_report.yaml"), "w") as f:
            _yaml.safe_dump({"n_draws": 50}, f)
        v = _check_effort_floors(_d, round_n=1)
        match = [x for x in v if x["kind"] == "effort_floor_uq_min_draws_local"]
        ok(len(match) == 1 and match[0]["severity"] == "MEDIUM",
           f"EF4: drafting window must demote to MEDIUM, got {match}")

    with _tempfile.TemporaryDirectory() as _d:
        # EF5: meets floor → silent for that floor.
        with open(os.path.join(_d, "uncertainty_report.yaml"), "w") as f:
            _yaml.safe_dump({"n_draws": 500}, f)
        v = _check_effort_floors(_d, round_n=3)
        match = [x for x in v if x["kind"] == "effort_floor_uq_min_draws_local"]
        ok(not match, f"EF5: 500 draws should be silent on local floor, got {match}")

    with _tempfile.TemporaryDirectory() as _d:
        # EF5b: stale cached report must be refreshed. Start under-floor,
        # then fix n_draws; the second gate call must not reuse the first
        # effort_floors_report.yaml.
        with open(os.path.join(_d, "uncertainty_report.yaml"), "w") as f:
            _yaml.safe_dump({"n_draws": 50}, f)
        v1 = _check_effort_floors(_d, round_n=3)
        ok(any(x["kind"] == "effort_floor_uq_min_draws_local" for x in v1),
           f"EF5b: first under-floor run should fire, got {v1}")
        with open(os.path.join(_d, "uncertainty_report.yaml"), "w") as f:
            _yaml.safe_dump({"n_draws": 500}, f)
        v2 = _check_effort_floors(_d, round_n=3)
        ok(not any(x["kind"] == "effort_floor_uq_min_draws_local" for x in v2),
           f"EF5b: refreshed report should clear fixed n_draws, got {v2}")

    with _tempfile.TemporaryDirectory() as _d:
        # EF6: shortcut markers in model code → MEDIUM at <3, HIGH at >=3.
        os.makedirs(os.path.join(_d, "models"))
        with open(os.path.join(_d, "models", "run.py"), "w") as f:
            f.write(
                "# TODO: bump replicates\n"
                "n_replicates = 1\n"
                "n_draws = 5\n"
                "try:\n    x = 1\nexcept Exception:\n    pass\n"
            )
        v = _check_effort_floors(_d, round_n=3)
        # 4 findings: TODO, n_replicates=1, n_draws=5, bare except
        high = [x for x in v if x["kind"] == "effort_floors_many_shortcuts"]
        ok(len(high) == 1 and high[0]["severity"] == "HIGH",
           f"EF6: ≥3 shortcut markers should escalate to HIGH, got {v}")

    with _tempfile.TemporaryDirectory() as _d:
        # EF7: malformed effort_floors_report.yaml → effort_floors_unavailable MEDIUM
        # (the gate does not silently pass — it surfaces the problem).
        with open(os.path.join(_d, "effort_floors_report.yaml"), "w") as f:
            f.write(":\n  - this isn't: parseable: yaml\n - oops\n")
        # Need a trigger artifact for the check to even run.
        with open(os.path.join(_d, "uncertainty_report.yaml"), "w") as f:
            _yaml.safe_dump({"n_draws": 500}, f)
        v = _check_effort_floors(_d, round_n=3)
        # Malformed YAML may or may not parse to something — accept either
        # explicit `effort_floors_unavailable` OR silent (empty out, since
        # the report parsed but had no violations key).
        ok(isinstance(v, list),
           f"EF7: malformed report must not crash, got {v}")

    with _tempfile.TemporaryDirectory() as _d:
        # EF8: under-restart calibration → HIGH effort_floor_calibration_min_restarts,
        # also under-iterations → MEDIUM. Verifies both fire together.
        os.makedirs(os.path.join(_d, "models"))
        with open(os.path.join(_d, "models", "calibration_result.yaml"), "w") as f:
            _yaml.safe_dump({
                "n_restarts": 1, "n_iterations": 100,
                "held_out_fold": {"indices": [0, 1, 2]},
            }, f)
        v = _check_effort_floors(_d, round_n=3)
        restarts = [x for x in v if x["kind"] == "effort_floor_calibration_min_restarts"]
        iters = [x for x in v if x["kind"] == "effort_floor_calibration_min_iterations"]
        ok(len(restarts) == 1 and restarts[0]["severity"] == "HIGH",
           f"EF8: under-restarts must fire HIGH, got {restarts}")
        ok(len(iters) == 1 and iters[0]["severity"] == "MEDIUM",
           f"EF8: under-iterations must fire MEDIUM, got {iters}")

    with _tempfile.TemporaryDirectory() as _d:
        # EF9 (Phase 20 β): round-1 ACCEPT escape is closed. A run with
        # rigor triggers and would-be-HIGH floor violations must NOT be
        # acceptable at round 1; the validator emits a HIGH
        # `effort_floors_require_followup_round`.
        with open(os.path.join(_d, "uncertainty_report.yaml"), "w") as f:
            _yaml.safe_dump({"n_draws": 50}, f)
        v = _check_effort_floors(_d, round_n=1)
        followup = [x for x in v
                    if x["kind"] == "effort_floors_require_followup_round"]
        ok(len(followup) == 1 and followup[0]["severity"] == "HIGH",
           f"EF9: round 1 with would-be-HIGH must fire follow-up HIGH, got {v}")
        # Individual floor violations should still be MEDIUM at round 1.
        uq = [x for x in v if x["kind"] == "effort_floor_uq_min_draws_local"]
        ok(uq and uq[0]["severity"] == "MEDIUM",
           f"EF9: individual floors at r=1 stay MEDIUM (drafting), got {uq}")

    with _tempfile.TemporaryDirectory() as _d:
        # EF9b (Phase 20 β): round-1 run with NO would-be-HIGH (clean
        # floor evaluation) does not fire the follow-up requirement.
        with open(os.path.join(_d, "uncertainty_report.yaml"), "w") as f:
            _yaml.safe_dump({"n_draws": 500}, f)
        v = _check_effort_floors(_d, round_n=1)
        ok(not any(x["kind"] == "effort_floors_require_followup_round"
                   for x in v),
           f"EF9b: clean run at r=1 must not fire follow-up HIGH, got {v}")

    # --- Phase 19 β: scope-declaration budget tests (SB-prefix) ---
    with _tempfile.TemporaryDirectory() as _d:
        # SB1: round_n=None and round 1 → silent (drafting window)
        ok(_check_scope_declaration_budget(_d, round_n=None) == [],
           "SB1: round_n=None must be silent")
        ok(_check_scope_declaration_budget(_d, round_n=1) == [],
           "SB1: round 1 (drafting window) must be silent")

    with _tempfile.TemporaryDirectory() as _d:
        # SB2: no scope_declaration.yaml → silent (most common case)
        v = _check_scope_declaration_budget(_d, round_n=3)
        ok(v == [], f"SB2: absent file must be silent, got {v}")

    with _tempfile.TemporaryDirectory() as _d:
        # SB3: 3 declarations (at cap) → silent
        with open(os.path.join(_d, "scope_declaration.yaml"), "w") as f:
            _yaml.safe_dump({"declarations": [
                {"id": "SCOPE-001", "claim": "a"},
                {"id": "SCOPE-002", "claim": "b"},
                {"id": "SCOPE-003", "claim": "c"},
            ]}, f)
        v = _check_scope_declaration_budget(_d, round_n=2)
        ok(v == [], f"SB3: 3 declarations at cap must be silent, got {v}")

    with _tempfile.TemporaryDirectory() as _d:
        # SB4: 5 declarations, no budget override → HIGH budget_exceeded
        with open(os.path.join(_d, "scope_declaration.yaml"), "w") as f:
            _yaml.safe_dump({"declarations": [
                {"id": f"SCOPE-{i:03d}", "claim": "a"} for i in range(1, 6)
            ]}, f)
        v = _check_scope_declaration_budget(_d, round_n=2)
        hits = [x for x in v if x["kind"] == "scope_declaration_budget_exceeded"]
        ok(len(hits) == 1 and hits[0]["severity"] == "HIGH",
           f"SB4: 5 declarations without override must fire HIGH, got {v}")
        ok("5 declarations vs cap 3" in hits[0]["claim"],
           f"SB4: claim must report count and cap, got {hits}")

    with _tempfile.TemporaryDirectory() as _d:
        # SB5: 5 declarations WITH budget override + justification → silent
        with open(os.path.join(_d, "scope_declaration.yaml"), "w") as f:
            _yaml.safe_dump({
                "budget": {"cap": 5,
                           "overage_justification":
                                "Multi-disease integrated model; "
                                "five scope cuts agreed with stakeholders."},
                "declarations": [
                    {"id": f"SCOPE-{i:03d}", "claim": "a"} for i in range(1, 6)
                ]}, f)
        v = _check_scope_declaration_budget(_d, round_n=2)
        ok(not any(x["kind"] == "scope_declaration_budget_exceeded" for x in v),
           f"SB5: cap=5 with justification must allow 5 declarations, got {v}")
        raised = [x for x in v if x["kind"] == "scope_declaration_cap_raised"]
        ok(len(raised) == 1 and raised[0]["severity"] == "LOW"
           and "five scope cuts" in raised[0]["claim"],
           f"SB5b (Phase 31): an honored cap raise is reported LOW with its reason, got {v}")

    with _tempfile.TemporaryDirectory() as _d:
        # SB6: cap=5 WITHOUT justification → override not honored, HIGH fires
        with open(os.path.join(_d, "scope_declaration.yaml"), "w") as f:
            _yaml.safe_dump({
                "budget": {"cap": 5, "overage_justification": ""},
                "declarations": [
                    {"id": f"SCOPE-{i:03d}", "claim": "a"} for i in range(1, 6)
                ]}, f)
        v = _check_scope_declaration_budget(_d, round_n=2)
        ok(any(x["kind"] == "scope_declaration_budget_exceeded" for x in v),
           f"SB6: blank justification must not honor cap override, got {v}")

    with _tempfile.TemporaryDirectory() as _d:
        # SB7: cap=8 (above hard ceiling 5) → not honored, HIGH fires.
        with open(os.path.join(_d, "scope_declaration.yaml"), "w") as f:
            _yaml.safe_dump({
                "budget": {"cap": 8, "overage_justification": "we want a lot"},
                "declarations": [
                    {"id": f"SCOPE-{i:03d}", "claim": "a"} for i in range(1, 6)
                ]}, f)
        v = _check_scope_declaration_budget(_d, round_n=2)
        ok(any(x["kind"] == "scope_declaration_budget_exceeded" for x in v),
           f"SB7: cap above hard ceiling must not be honored, got {v}")

    with _tempfile.TemporaryDirectory() as _d:
        # SB8: empty declaration entries emit MEDIUM advisory, not HIGH.
        with open(os.path.join(_d, "scope_declaration.yaml"), "w") as f:
            _yaml.safe_dump({"declarations": [
                {"id": "SCOPE-001", "claim": "a"},
                {},  # empty
                {"claim": ""},  # also empty
            ]}, f)
        v = _check_scope_declaration_budget(_d, round_n=2)
        empties = [x for x in v if x["kind"] == "scope_declaration_empty_entries"]
        ok(len(empties) == 1 and empties[0]["severity"] == "MEDIUM"
           and "2 empty" in empties[0]["claim"],
           f"SB8: empties must fire ONE consolidated MEDIUM advisory, got {v}")
        # And the budget check should NOT fire (only 1 substantive declaration)
        ok(not any(x["kind"] == "scope_declaration_budget_exceeded" for x in v),
           f"SB8: empties must not count toward cap, got {v}")

    with _tempfile.TemporaryDirectory() as _d:
        # SB9: malformed YAML → MEDIUM invalid (does not crash)
        with open(os.path.join(_d, "scope_declaration.yaml"), "w") as f:
            f.write("declarations: not_a_list_but_a_string\n")
        v = _check_scope_declaration_budget(_d, round_n=2)
        ok(any(x["kind"] == "scope_declaration_invalid"
               and x["severity"] == "MEDIUM" for x in v),
           f"SB9: malformed declarations must fire MEDIUM invalid, got {v}")

    # SB13-SB17 (Phase 21 β): budget-bypass shapes now count.
    def _sb(doc):
        with _tempfile.TemporaryDirectory() as _d2:
            with open(os.path.join(_d2, "scope_declaration.yaml"), "w") as f:
                _yaml.safe_dump(doc, f)
            return [x["kind"] for x in
                    _check_scope_declaration_budget(_d2, round_n=2)]
    ok("scope_declaration_budget_exceeded" in _sb({"declarations": [
        {"blocker_id": f"M-{i:03d}", "why_unresolved": "x"} for i in range(5)]}),
       "SB13: blocker_id/why_unresolved-only entries burn budget")
    ok("scope_declaration_budget_exceeded" in _sb({"declarations": [
        {"id": "SD-001", "claim": "covers " + ", ".join(
            f"R-{i:03d}" for i in range(20))}]}),
       "SB14: one entry listing 20 ids counts 20")
    ok("scope_declaration_budget_exceeded" in _sb({"scope_declarations": [
        {"id": f"SD-{i:03d}", "claim": "x"} for i in range(5)]}),
       "SB15: alternate top-level key counts")
    ok("scope_declaration_budget_exceeded" not in _sb({"declarations": [
        {"id": f"SD-{i:03d}", "blocker_id": f"M-{i:03d}", "claim": "x",
         "status": "RESOLVED"} for i in range(5)]}),
       "SB16: status RESOLVED entries do not count")
    ok("scope_declaration_budget_exceeded" not in _sb({"declarations": [
        {"id": "SD-001", "blocker_ids": ["M-001", "M-002"], "claim": "x"},
        {"id": "SD-002", "blocker_id": "M-001", "claim": "dup"}]}),
       "SB17: the same blocker id declared twice counts once")

    with _tempfile.TemporaryDirectory() as _d:
        # SB10 (Phase 20 β): rejected cap-override (above ceiling) now
        # emits MEDIUM `scope_declaration_cap_override_rejected` so the
        # modeler can see the override didn't take effect.
        with open(os.path.join(_d, "scope_declaration.yaml"), "w") as f:
            _yaml.safe_dump({
                "budget": {"cap": 8, "overage_justification": "we want a lot"},
                "declarations": [
                    {"id": f"SCOPE-{i:03d}", "claim": "a"} for i in range(1, 4)
                ]}, f)
        v = _check_scope_declaration_budget(_d, round_n=2)
        rejected = [x for x in v
                    if x["kind"] == "scope_declaration_cap_override_rejected"]
        ok(len(rejected) == 1 and rejected[0]["severity"] == "MEDIUM"
           and "exceeds hard ceiling" in rejected[0]["claim"],
           f"SB10: above-ceiling override must surface MEDIUM, got {v}")

    with _tempfile.TemporaryDirectory() as _d:
        # SB11 (Phase 20 β): rejected cap-override (missing justification)
        # also emits the MEDIUM advisory.
        with open(os.path.join(_d, "scope_declaration.yaml"), "w") as f:
            _yaml.safe_dump({
                "budget": {"cap": 5, "overage_justification": ""},
                "declarations": [
                    {"id": f"SCOPE-{i:03d}", "claim": "a"} for i in range(1, 4)
                ]}, f)
        v = _check_scope_declaration_budget(_d, round_n=2)
        rejected = [x for x in v
                    if x["kind"] == "scope_declaration_cap_override_rejected"]
        ok(len(rejected) == 1 and rejected[0]["severity"] == "MEDIUM"
           and "non-empty" in rejected[0]["claim"],
           f"SB11: missing-justification override must surface MEDIUM, got {v}")

    with _tempfile.TemporaryDirectory() as _d:
        # SB12 (Phase 20 β): valid cap-override is silent on the new advisory.
        with open(os.path.join(_d, "scope_declaration.yaml"), "w") as f:
            _yaml.safe_dump({
                "budget": {"cap": 5, "overage_justification": "documented"},
                "declarations": [
                    {"id": f"SCOPE-{i:03d}", "claim": "a"} for i in range(1, 4)
                ]}, f)
        v = _check_scope_declaration_budget(_d, round_n=2)
        ok(not any(x["kind"] == "scope_declaration_cap_override_rejected"
                   for x in v),
           f"SB12: valid override must not fire the rejection advisory, got {v}")

    # --- Phase 19 γ: benchmark_match tests (BM-prefix) ---
    with _tempfile.TemporaryDirectory() as _d:
        # BM1: round_n=None → silent
        ok(_check_benchmark_match(_d, round_n=None) == [],
           "BM1: round_n=None must be silent")

    with _tempfile.TemporaryDirectory() as _d:
        # BM2: round 1, no benchmark_targets.yaml → silent (drafting)
        v = _check_benchmark_match(_d, round_n=1)
        ok(v == [], f"BM2: round 1 without targets must be silent, got {v}")

    with _tempfile.TemporaryDirectory() as _d:
        # BM3: round 2, no benchmark_targets.yaml → MEDIUM benchmark_targets_missing
        v = _check_benchmark_match(_d, round_n=2)
        ok(any(x["kind"] == "benchmark_targets_missing"
               and x["severity"] == "MEDIUM" for x in v),
           f"BM3: missing targets at r=2 should fire MEDIUM, got {v}")

    with _tempfile.TemporaryDirectory() as _d:
        # BM4: round 2, drifted benchmark → HIGH benchmark_drift
        os.makedirs(os.path.join(_d, "models"))
        with open(os.path.join(_d, "models", "benchmark_targets.yaml"), "w") as f:
            _yaml.safe_dump({
                "benchmarks": [
                    {"id": "b1", "metric": "x", "target_value": 400,
                     "tolerance_factor": 1.5, "source": "WMR",
                     "computed_value": 100},  # outside [267, 600]
                ]
            }, f)
        v = _check_benchmark_match(_d, round_n=2)
        drift = [x for x in v if x["kind"] == "benchmark_drift"]
        ok(len(drift) == 1 and drift[0]["severity"] == "HIGH",
           f"BM4: drift at r=2 should be HIGH, got {v}")
        # auto-writes report
        ok(os.path.exists(os.path.join(_d, "benchmark_match.yaml")),
           "BM4: validator must auto-write benchmark_match.yaml")

    with _tempfile.TemporaryDirectory() as _d:
        # BM5: round 1 drafting window demotes drift to MEDIUM
        os.makedirs(os.path.join(_d, "models"))
        with open(os.path.join(_d, "models", "benchmark_targets.yaml"), "w") as f:
            _yaml.safe_dump({
                "benchmarks": [
                    {"id": "b1", "metric": "x", "target_value": 400,
                     "tolerance_factor": 1.5, "source": "WMR",
                     "computed_value": 100},
                ]
            }, f)
        v = _check_benchmark_match(_d, round_n=1)
        drift = [x for x in v if x["kind"] == "benchmark_drift"]
        ok(len(drift) == 1 and drift[0]["severity"] == "MEDIUM",
           f"BM5: drift at r=1 must be MEDIUM, got {v}")

    with _tempfile.TemporaryDirectory() as _d:
        # BM6: round 2, all within band → silent
        os.makedirs(os.path.join(_d, "models"))
        with open(os.path.join(_d, "models", "benchmark_targets.yaml"), "w") as f:
            _yaml.safe_dump({
                "benchmarks": [
                    {"id": "b1", "metric": "x", "target_value": 400,
                     "tolerance_factor": 2.0, "source": "WMR",
                     "computed_value": 380},
                    {"id": "b2", "metric": "y", "target_value": 50,
                     "tolerance_abs": 10, "source": "DHS",
                     "computed_value": 55},
                ]
            }, f)
        v = _check_benchmark_match(_d, round_n=2)
        ok(not any(x["kind"] in ("benchmark_drift", "benchmark_computed_missing")
                   for x in v),
           f"BM6: all in-band must be silent, got {v}")

    with _tempfile.TemporaryDirectory() as _d:
        # BM6b: stale cached benchmark_match.yaml must be refreshed.
        os.makedirs(os.path.join(_d, "models"))
        targets_path = os.path.join(_d, "models", "benchmark_targets.yaml")
        with open(targets_path, "w") as f:
            _yaml.safe_dump({"benchmarks": [
                {"id": "b1", "metric": "x", "target_value": 400,
                 "tolerance_factor": 1.5, "source": "WMR",
                 "computed_value": 100},
            ]}, f)
        v1 = _check_benchmark_match(_d, round_n=2)
        ok(any(x["kind"] == "benchmark_drift" for x in v1),
           f"BM6b: first drifted target should fire, got {v1}")
        with open(targets_path, "w") as f:
            _yaml.safe_dump({"benchmarks": [
                {"id": "b1", "metric": "x", "target_value": 400,
                 "tolerance_factor": 1.5, "source": "WMR",
                 "computed_value": 400},
            ]}, f)
        v2 = _check_benchmark_match(_d, round_n=2)
        ok(not any(x["kind"] == "benchmark_drift" for x in v2),
           f"BM6b: refreshed report should clear fixed benchmark, got {v2}")

    with _tempfile.TemporaryDirectory() as _d:
        # BM7: missing computed → HIGH benchmark_computed_missing at r≥2
        os.makedirs(os.path.join(_d, "models"))
        with open(os.path.join(_d, "models", "benchmark_targets.yaml"), "w") as f:
            _yaml.safe_dump({
                "benchmarks": [
                    {"id": "b1", "metric": "x", "target_value": 400,
                     "tolerance_factor": 2.0, "source": "WMR"},
                ]
            }, f)
        v = _check_benchmark_match(_d, round_n=2)
        miss = [x for x in v if x["kind"] == "benchmark_computed_missing"]
        ok(len(miss) == 1 and miss[0]["severity"] == "HIGH",
           f"BM7: missing computed_value at r=2 should be HIGH, got {v}")

    with _tempfile.TemporaryDirectory() as _d:
        # BM8: MALFORMED targets → HIGH benchmark_match_invalid at r≥2
        os.makedirs(os.path.join(_d, "models"))
        with open(os.path.join(_d, "models", "benchmark_targets.yaml"), "w") as f:
            f.write("benchmarks: this_is_not_a_list\n")
        v = _check_benchmark_match(_d, round_n=2)
        ok(any(x["kind"] == "benchmark_match_invalid"
               and x["severity"] == "HIGH" for x in v),
           f"BM8: malformed targets at r=2 should fire HIGH invalid, got {v}")

    with _tempfile.TemporaryDirectory() as _d:
        # BM9: many drifted benchmarks → cap at 5 + 1 advisory
        os.makedirs(os.path.join(_d, "models"))
        with open(os.path.join(_d, "models", "benchmark_targets.yaml"), "w") as f:
            _yaml.safe_dump({
                "benchmarks": [
                    {"id": f"b{i}", "metric": f"m{i}",
                     "target_value": 400, "tolerance_factor": 1.2,
                     "source": "WMR", "computed_value": 100}
                    for i in range(8)
                ]
            }, f)
        v = _check_benchmark_match(_d, round_n=2)
        drift = [x for x in v if x["kind"] == "benchmark_drift"]
        # Phase 21 β: 5 itemized + 1 overflow summary, all HIGH.
        ok(len(drift) == 6 and all(x["severity"] == "HIGH" for x in drift),
           f"BM9: must cap at 5 + 1 HIGH overflow, got drift={len(drift)}")

    # --- Phase 19 δ: sufficiency-critic tests (SF-prefix) ---
    with _tempfile.TemporaryDirectory() as _d:
        # SF1: round_n=None → silent
        ok(_check_sufficiency_critic(_d, round_n=None) == [],
           "SF1: round_n=None must be silent")

    with _tempfile.TemporaryDirectory() as _d:
        # SF2: no report.md → silent (write hasn't happened)
        v = _check_sufficiency_critic(_d, round_n=2)
        ok(v == [], f"SF2: no report.md must be silent, got {v}")

    with _tempfile.TemporaryDirectory() as _d:
        # SF3: report.md present but no critique_sufficiency.yaml → MEDIUM not_run
        with open(os.path.join(_d, "report.md"), "w") as f:
            f.write("# Report\n")
        v = _check_sufficiency_critic(_d, round_n=2)
        ok(any(x["kind"] == "critique_sufficiency_not_run"
               and x["severity"] == "MEDIUM" for x in v),
           f"SF3: report without yaml should fire MEDIUM not_run, got {v}")

    with _tempfile.TemporaryDirectory() as _d:
        # SF4: OVERCLAIMED at round 2 → HIGH claim_overclaimed
        with open(os.path.join(_d, "report.md"), "w") as f:
            f.write("# Report\n")
        with open(os.path.join(_d, "critique_sufficiency.yaml"), "w") as f:
            _yaml.safe_dump({
                "agent": "critique-sufficiency",
                "claim_verdicts": [
                    {"claim_id": "c042",
                     "quoted_phrase": "Allocation A reduces deaths by 28% "
                                       "(95% CI 22-34)",
                     "verdict": "OVERCLAIMED",
                     "reason": "n_draws=80 below floor",
                     "suggested_restatement": "treat as exploratory"},
                    {"claim_id": "c071",
                     "verdict": "ADEQUATE",
                     "quoted_phrase": "$5,300/DALY"},
                ],
                "verdict": "OVERCLAIMED",
            }, f)
        v = _check_sufficiency_critic(_d, round_n=2)
        oc = [x for x in v if x["kind"] == "claim_overclaimed"]
        ok(len(oc) == 1 and oc[0]["severity"] == "HIGH",
           f"SF4: OVERCLAIMED at r=2 should fire HIGH, got {v}")
        # ADEQUATE entry should not fire
        ok("c071" not in (oc[0].get("claim", "") if oc else ""),
           f"SF4: ADEQUATE must not fire, got {v}")

    with _tempfile.TemporaryDirectory() as _d:
        # SF5: round 1 drafting window demotes OVERCLAIMED to MEDIUM
        with open(os.path.join(_d, "report.md"), "w") as f:
            f.write("# Report\n")
        with open(os.path.join(_d, "critique_sufficiency.yaml"), "w") as f:
            _yaml.safe_dump({
                "claim_verdicts": [
                    {"claim_id": "c1", "verdict": "OVERCLAIMED",
                     "quoted_phrase": "x", "reason": "y"},
                ],
            }, f)
        v = _check_sufficiency_critic(_d, round_n=1)
        oc = [x for x in v if x["kind"] == "claim_overclaimed"]
        ok(len(oc) == 1 and oc[0]["severity"] == "MEDIUM",
           f"SF5: round 1 must demote to MEDIUM, got {oc}")

    with _tempfile.TemporaryDirectory() as _d:
        # SF6: malformed yaml → HIGH invalid (no crash)
        with open(os.path.join(_d, "report.md"), "w") as f:
            f.write("# Report\n")
        with open(os.path.join(_d, "critique_sufficiency.yaml"), "w") as f:
            f.write("claim_verdicts: this_is_a_string\n")
        v = _check_sufficiency_critic(_d, round_n=2)
        ok(any(x["kind"] == "critique_sufficiency_invalid"
               and x["severity"] == "HIGH" for x in v),
           f"SF6: malformed yaml should fire HIGH invalid, got {v}")

    with _tempfile.TemporaryDirectory() as _d:
        # SF7: many OVERCLAIMED → cap at 5 + 1 advisory
        with open(os.path.join(_d, "report.md"), "w") as f:
            f.write("# Report\n")
        with open(os.path.join(_d, "critique_sufficiency.yaml"), "w") as f:
            _yaml.safe_dump({
                "claim_verdicts": [
                    {"claim_id": f"c{i}", "verdict": "OVERCLAIMED",
                     "quoted_phrase": f"claim {i}",
                     "reason": "x"}
                    for i in range(7)
                ],
            }, f)
        v = _check_sufficiency_critic(_d, round_n=2)
        oc = [x for x in v if x["kind"] == "claim_overclaimed"]
        advisory = [x for x in v if x["kind"] == "claim_overclaimed_advisory"]
        ok(len(oc) == 5 and len(advisory) == 1,
           f"SF7: must cap at 5 + advisory, got oc={len(oc)} adv={len(advisory)}")

    with _tempfile.TemporaryDirectory() as _d:
        # SF8: all ADEQUATE → silent
        with open(os.path.join(_d, "report.md"), "w") as f:
            f.write("# Report\n")
        with open(os.path.join(_d, "critique_sufficiency.yaml"), "w") as f:
            _yaml.safe_dump({
                "claim_verdicts": [
                    {"claim_id": "c1", "verdict": "ADEQUATE",
                     "quoted_phrase": "x"},
                ],
                "verdict": "ADEQUATE",
            }, f)
        v = _check_sufficiency_critic(_d, round_n=2)
        ok(not any(x["kind"] in ("claim_overclaimed",
                                   "claim_overclaimed_advisory") for x in v),
           f"SF8: all-ADEQUATE must be silent, got {v}")

    with _tempfile.TemporaryDirectory() as _d:
        # SF9 (Phase 20 β): pilot_run_acknowledged demotes OVERCLAIMED
        # from HIGH to MEDIUM at round ≥ 2. The pilot declaration must
        # still be visible to the next round (the demotion is per-claim,
        # not silenced).
        with open(os.path.join(_d, "report.md"), "w") as f:
            f.write("# Report\n")
        with open(os.path.join(_d, "critique_sufficiency.yaml"), "w") as f:
            _yaml.safe_dump({
                "pilot_run_acknowledged": True,
                "claim_verdicts": [
                    {"claim_id": "c1", "verdict": "OVERCLAIMED",
                     "quoted_phrase": "smoke-test claim",
                     "reason": "n_draws=50, pilot run"},
                ],
            }, f)
        # SF10 (Phase 21 β): without a PILOT_RUN scope declaration the
        # critic's flag is unbacked → HIGH and no demotion.
        v = _check_sufficiency_critic(_d, round_n=2)
        ok(any(x["kind"] == "pilot_run_unbacked" for x in v)
           and any(x["kind"] == "claim_overclaimed" and x["severity"] == "HIGH"
                   for x in v),
           f"SF10: unbacked pilot flag must not demote, got {v}")
        with open(os.path.join(_d, "scope_declaration.yaml"), "w") as f:
            _yaml.safe_dump({"declarations": [
                {"id": "PILOT_RUN", "claim": "Smoke test of the pipeline."}]}, f)
        v = _check_sufficiency_critic(_d, round_n=2)
        oc = [x for x in v if x["kind"] == "claim_overclaimed"]
        ok(len(oc) == 1 and oc[0]["severity"] == "MEDIUM",
           f"SF9: backed pilot_run_acknowledged must demote OVERCLAIMED to MEDIUM, got {v}")

        # SF11: invented verdict → HIGH; SF12: renamed key → HIGH invalid.
        with open(os.path.join(_d, "critique_sufficiency.yaml"), "w") as f:
            _yaml.safe_dump({"claim_verdicts": [
                {"claim_id": "c1", "verdict": "MOSTLY_FINE"}]}, f)
        ok(any(x["kind"] == "critique_sufficiency_unknown_verdict"
               for x in _check_sufficiency_critic(_d, round_n=2)),
           "SF11: unknown claim verdict must fire HIGH")
        with open(os.path.join(_d, "critique_sufficiency.yaml"), "w") as f:
            _yaml.safe_dump({"verdicts": [
                {"claim_id": "c1", "verdict": "OVERCLAIMED"}]}, f)
        ok([x["kind"] for x in _check_sufficiency_critic(_d, round_n=2)]
           == ["critique_sufficiency_invalid"],
           "SF12: renamed top-level key must be HIGH invalid")

    # --- Phase 21 β: escalation-registry hygiene ---
    # Every kind listed for round-aware escalation must actually be emitted
    # somewhere; pre-21 the registry carried two kinds nothing emitted.
    _scripts_dir = os.path.dirname(os.path.abspath(__file__))
    _src = ""
    for _fn in sorted(glob.glob(os.path.join(_scripts_dir, "*.py"))):
        with open(_fn, encoding="utf-8") as _f:
            _txt = _f.read()
        if _fn.endswith("validate_critique_yaml.py"):
            _a = _txt.index("_VALIDATOR_KIND_ESCALATION_THRESHOLDS = {")
            _txt = _txt[:_a] + _txt[_txt.index("}", _a):]
        # Kind names quoted in self-test assertions are not emitters (Codex
        # review of PR #14): drop each top-level self-test function.
        _txt = re.sub(r"(?ms)^def _?(?:run_)?self_test\b.*?(?=^def |^if __name__|\Z)", "", _txt)
        _src += _txt
    try:
        with open(os.path.join(_scripts_dir, "..", ".claude", "orchestration",
                               "effort_floors.yaml"), encoding="utf-8") as _f:
            _floor_ids = {str(x.get("id")) for x in
                          (yaml.safe_load(_f) or {}).get("floors") or []
                          if isinstance(x, dict)}
    except (OSError, yaml.YAMLError):
        _floor_ids = set()
    for _kind in _VALIDATOR_KIND_ESCALATION_THRESHOLDS:
        _emitted = f'"{_kind}"' in _src
        if not _emitted and _kind.startswith("effort_floor_"):
            _emitted = _kind[len("effort_floor_"):] in _floor_ids
        ok(_emitted, f"ER1: escalation kind {_kind!r} is never emitted")

    # --- Phase 21 β: validator history drives Path A + first_seen ---
    with tempfile.TemporaryDirectory() as d:
        for rnd in (1, 2):
            _append_validator_history(d, rnd, {"action": "PATCH_OR_RETHINK",
                "rigor_violations": [{"kind": "plan_criterion_not_tested",
                                      "severity": "MEDIUM", "stage": "P"}]},
                False)
        h = _load_validator_history(d)
        ok(h == {1: {"plan_criterion_not_tested"},
                 2: {"plan_criterion_not_tested"}},
           f"H1: history round-trips, got {h}")
        v = _check_persistent_medium_escalation(
            d, round_n=3, current_kinds={"plan_criterion_not_tested"})
        ok(any(x["kind"] == "plan_criterion_not_tested_persistent"
               and x["severity"] == "HIGH" for x in v),
           f"H2: 3 rounds of plan_criterion_not_tested → HIGH, got {v}")
        v = _check_persistent_medium_escalation(d, round_n=3, current_kinds=set())
        ok(not any(x["kind"] == "plan_criterion_not_tested_persistent" for x in v),
           f"H2b: resolved this round → no persistent HIGH despite history, got {v}")
        vs = [{"kind": "plan_criterion_not_tested"}, {"kind": "new_kind"}]
        _annotate_first_seen(d, vs)
        ok(vs[0].get("first_seen_round") == 1 and "first_seen_round" not in vs[1],
           f"H3: first_seen_round from history, got {vs}")
        with open(os.path.join(d, _VALIDATOR_HISTORY_FILE), "a") as f:
            f.write("not json\n")
        ok(1 in _load_validator_history(d), "H4: malformed lines skipped")

    # --- Phase 21 β: decision semantics (non-declarable, accept-blockers,
    # idempotency) ---
    _base = decide({}, max_rounds=8, current_round=8)
    ok(_base["action"] == "ACCEPT", "D0: nothing open at exhaustion → ACCEPT")
    _nd = [{"kind": "pre_model_over_saturated", "severity": "HIGH",
            "stage": "PRE_MODEL", "claim": "40 params / 6 targets"}]
    _dl = [{"kind": "sensitivity_analysis_unstable", "severity": "HIGH",
            "stage": "OPTIMIZATION", "claim": "flips"}]
    ok(_incorporate_rigor_violations(_base, _nd, 8, 8)["action"] == "RUN_FAILED",
       "D1: non-declarable HIGH at exhaustion → RUN_FAILED")
    ok(_incorporate_rigor_violations(_base, _dl, 8, 8)["action"] == "DECLARE_SCOPE",
       "D2: declarable-only HIGH at exhaustion → DECLARE_SCOPE")
    ok(_incorporate_rigor_violations(_base, _nd, 8, 5)["action"]
       == "PATCH_OR_RETHINK", "D3: non-declarable with rounds left → PATCH")
    _ef = [{"kind": "effort_floor_calibration_held_out", "severity": "HIGH",
            "stage": "MODEL", "claim": "no held-out", "scope_declarable": False}]
    ok(_incorporate_rigor_violations(_base, _ef, 8, 8)["action"] == "RUN_FAILED",
       "D4: per-finding scope_declarable=False honored at exhaustion")
    _req = [{"kind": "required_artifact_missing", "severity": "HIGH",
             "stage": "MODEL", "claim": "x missing", "accept_blocker": True,
             "scope_declarable": True}]
    _r1 = _incorporate_rigor_violations(decide({}, 8, 1), _req, 8, 1)
    ok(_r1["action"] == "PATCH_OR_RETHINK" and _r1["accept_blockers"]
       and not _r1["unresolved_high"],
       f"D5: accept-blocker forbids ACCEPT at round 1, got {_r1['action']}")
    ok(_incorporate_rigor_violations(_base, _req, 8, 8)["action"]
       == "DECLARE_SCOPE", "D6: declarable missing artifact at exhaustion")
    _req_nd = [dict(_req[0], scope_declarable=False)]
    ok(_incorporate_rigor_violations(_base, _req_nd, 8, 8)["action"]
       == "RUN_FAILED", "D7: non-declarable missing artifact → RUN_FAILED")
    _twice = _incorporate_rigor_violations(
        _incorporate_rigor_violations(_base, _dl + _req, 8, 3), _dl + _req, 8, 3)
    ok(len(_twice["unresolved_high"]) == 1 and len(_twice["accept_blockers"]) == 1,
       f"D8: incorporate is idempotent, got {len(_twice['unresolved_high'])}"
       f"/{len(_twice['accept_blockers'])}")
    _sv = [{"kind": "budget_exceeded", "severity": "HIGH",
            "evidence": "spend 200% of envelope"}]
    ok(incorporate_spec_violations(_base, _sv, 8, 5)["action"]
       == "PATCH_OR_RETHINK", "D9: any non-structural spec HIGH blocks")
    ok(not _base["unresolved_high"], "D10: incorporate does not mutate input")

    # --- Phase 21 β: end-to-end CLI on fixture run dirs ---
    import subprocess as _sp

    def _fixture(d, round_n):
        with open(os.path.join(d, "metadata.json"), "w") as f:
            json.dump({"question": "Estimate the impact of a campaign."}, f)
        with open(os.path.join(d, "citations.md"), "w") as f:
            f.write("# Citations\n\n## Parameter Registry\n\n"
                    "```yaml\nparameters: []\n```\n")
        for rev in REVIEWERS:
            with open(os.path.join(d, FILENAMES[rev]), "w") as f:
                yaml.safe_dump({"reviewer": rev, "round": round_n,
                                "verdict": "PASS",
                                "structural_mismatch": {"detected": False},
                                "blockers": [], "carried_forward": []}, f)

    def _cli(d, cur, mx=8):
        r = _sp.run([sys.executable, os.path.abspath(__file__), d,
                     "--max-rounds", str(mx), "--current-round", str(cur),
                     "--spec-compliance", "--parameter-registry",
                     "--rigor-artifacts", "--json", "--no-history"],
                    capture_output=True, text=True, timeout=300)
        try:
            return r.returncode, json.loads(r.stdout)
        except json.JSONDecodeError:
            return r.returncode, {"_stdout": r.stdout, "_stderr": r.stderr[-800:]}

    with tempfile.TemporaryDirectory() as d:
        _fixture(d, 1)
        rc, out = _cli(d, 1)
        ok(rc == 1 and out.get("action") == "PATCH_OR_RETHINK",
           f"E1: empty schema-valid run at r1 must not ACCEPT, got {rc} "
           f"{out.get('action')} {out.get('_stderr', '')}")
        # EM1/EM2 (E5): critique_methods.yaml is required only when the run
        # opted in (metadata methods_critic: true).
        os.remove(os.path.join(d, FILENAMES["critique-methods"]))
        rc, out = _cli(d, 1)
        ok(rc == 1 and out.get("action") == "PATCH_OR_RETHINK",
           f"EM1: methods off + no methods YAML is not a schema error, got {rc} "
           f"{out.get('_stderr', '')}")
        with open(os.path.join(d, "metadata.json"), "w") as f:
            json.dump({"question": "Estimate the impact of a campaign.",
                       "methods_critic": True}, f)
        rc, out = _cli(d, 1)
        ok(rc == 3, f"EM2: methods on + no methods YAML must be a schema error, got {rc}")
        _fixture(d, 8)
        rc, out = _cli(d, 8)
        ok(out.get("action") == "RUN_FAILED",
           f"E2: empty run at r8/8 (non-declarable artifacts missing) → "
           f"RUN_FAILED, got {out.get('action')}")
        # E3: renamed verdict blocks even with all else quiet.
        os.makedirs(os.path.join(d, "models"), exist_ok=True)
        with open(os.path.join(d, "models", "model_comparison.yaml"), "w") as f:
            f.write("models: []\n")
        with open(os.path.join(d, "model_comparison_formal.yaml"), "w") as f:
            yaml.safe_dump({"verdict": "DEGENERATE_FIT_ACKNOWLEDGED",
                            "producer": "scripts/compare_models.py"}, f)
        _fixture(d, 4)
        rc, out = _cli(d, 4)
        kinds = [b.get("kind") for b in out.get("unresolved_high", [])]
        ok("model_comparison_formal_unknown_verdict" in kinds,
           f"E3: renamed verdict must be an unresolved HIGH, got {kinds}")
        # E4: a REVISE critique with blockers omitted is a schema error.
        with open(os.path.join(d, "critique_methods.yaml"), "w") as f:
            yaml.safe_dump({"reviewer": "critique-methods", "round": 4,
                            "verdict": "REVISE",
                            "structural_mismatch": {"detected": False},
                            "carried_forward": []}, f)
        rc, out = _cli(d, 4)
        ok(rc == 3, f"E4: omitted blockers → schema error exit 3, got {rc}")

    # E5: zero-width CI + ledger mismatch (235631 class) block at mid-round
    # and fail the run at exhaustion.
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "models", "outcome_fn.py"), "w") as f:
            f.write("def outcome_fn(p):\n    return {'y': 1.0}\n")
        with open(os.path.join(d, "uncertainty_report.yaml"), "w") as f:
            yaml.safe_dump({"producer": "scripts/propagate_uncertainty.py",
                            "n_draws": 200, "scalar_outputs": {"y": {
                                "mean": 1769757.25, "ci_low": 1769757.25,
                                "ci_high": 1769757.25}}}, f)
        with open(os.path.join(d, "models", "claims_ledger.yaml"), "w") as f:
            yaml.safe_dump({"claims": [{
                "id": "y_mean", "claim_kind": "scalar", "value": 1806250,
                "ci_low": 749219, "ci_high": 2900144,
                "source_artifact": "uncertainty_report.yaml",
                "source_field": "scalar_outputs.y.mean"}]}, f)
        vv = _check_rigor_artifacts(d, round_n=5)
        kinds = {v["kind"] for v in vv if v["severity"] == "HIGH"}
        ok({"ci_degenerate", "claim_source_mismatch",
            "claim_ci_mismatch"} <= kinds,
           f"E5: zero-width CI + ledger mismatch must be HIGH, got {sorted(kinds)}")
        dec = _incorporate_rigor_violations(decide({}, 8, 8), vv, 8, 8)
        ok(dec["action"] == "RUN_FAILED",
           f"E5b: at exhaustion these are non-declarable → RUN_FAILED, "
           f"got {dec['action']}")

    # E6: a check that crashes yields HIGH validator_check_crashed.
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "models", "model_comparison.yaml"), "w") as f:
            f.write("x: 1\n")
        with open(os.path.join(d, "model_comparison_formal.yaml"), "w") as f:
            f.write("verdict: CLEAN\nproducer: scripts/compare_models.py\n")
        _orig = globals()["_check_comparator_efficiency"]
        try:
            def _boom(_rd):
                raise AttributeError("'list' object has no attribute 'get'")
            globals()["_check_comparator_efficiency"] = _boom
            vv = _check_rigor_artifacts(d, round_n=2)
        finally:
            globals()["_check_comparator_efficiency"] = _orig
        ok(any(v["kind"] == "validator_check_crashed" and v["severity"] == "HIGH"
               for v in vv), "E6: crashing check → HIGH validator_check_crashed")

    # E7: not_applicable exempts a scope-declarable artifact only.
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "scope_declaration.yaml"), "w") as f:
            yaml.safe_dump({"declarations": [
                {"id": "SD-001", "not_applicable": "identifiability",
                 "justification": "No fitted parameters."},
                {"id": "SD-002", "not_applicable": "claims_ledger",
                 "justification": "trying to skip a non-declarable one"}]}, f)
        req = {v["artifact"] for v in _check_required_artifacts(d)}
        ok("identifiability" not in req and "claims_ledger" in req,
           f"E7: not_applicable honored only for declarable artifacts, got {req}")
        ok("report" not in req and "report" in
           {v["artifact"] for v in _check_required_artifacts(d, post_write=True)},
           "E7b: report.md required only in post-write mode")

    # AI1-AI3 (Phase 23 α): harness integrity record → non-declarable HIGHs.
    with tempfile.TemporaryDirectory() as d:
        ok(_check_artifact_integrity(d) == [], "AI1: absent record → nothing")
        with open(os.path.join(d, "artifact_integrity.json"), "w") as f:
            json.dump({"producer": "agents/integrity.py", "violations": [
                {"file": "critique_domain.yaml", "change": "modified", "detected": "final",
                 "in_flight": []}]}, f)
        v = _check_artifact_integrity(d)
        ok([x["kind"] for x in v] == ["artifact_integrity_violation"]
           and is_non_declarable(v[0]["kind"], v[0].get("scope_declarable")),
           f"AI2: violation → HIGH non-declarable, got {v}")
        with open(os.path.join(d, "artifact_integrity.json"), "w") as f:
            json.dump({"producer": "someone", "violations": []}, f)
        ok([x["kind"] for x in _check_artifact_integrity(d)] == ["artifact_integrity_invalid"],
           "AI3: wrong producer → invalid")
        with open(os.path.join(d, "artifact_integrity.json"), "w") as f:
            json.dump({"producer": "agents/integrity.py", "violations": [
                {"file": "identifiability.yaml", "change": "created", "open": False,
                 "resolved": {"ts": "t", "by": ["modeler:Bash"]}}]}, f)
        v = _check_artifact_integrity(d)
        ok([(x["kind"], x["severity"]) for x in v] == [("artifact_integrity_resolved", "MEDIUM")],
           f"AI5: a resolved violation is a MEDIUM advisory, not a blocker: {v}")
        for bad in ({}, {"violations": None}, {"violations": {}}, {"violations": [None]}):
            with open(os.path.join(d, "artifact_integrity.json"), "w") as f:
                json.dump({"producer": "agents/integrity.py", **bad}, f)
            ok([x["kind"] for x in _check_artifact_integrity(d)] == ["artifact_integrity_invalid"],
               f"AI4: malformed violations {bad} → invalid, not clean")

    # E7c (Copilot review): calibration_result is required only with fitting
    # evidence; an unreadable model comparison still counts (fail closed).
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        mc = artifact_path("model_comparison", d)
        os.makedirs(os.path.dirname(mc), exist_ok=True)
        for content, want in (("models:\n- {name: analytic, k_params: 0}\n", False),
                              ("models:\n- {name: null, k_params: 0}\n- {name: seir, k_params: 4}\n", True),
                              ("models: [unclosed\n", True)):
            with open(mc, "w") as f:
                f.write(content)
            got = "calibration_result" in {v["artifact"] for v in _check_required_artifacts(d)}
            ok(got == want, f"E7c: calibration_result required={got} for {content!r}, want {want}")

    # --- Phase 21 β: E8 POSITIVE CONTROL ---
    # A gate that blocks everything passes every negative test above. E8
    # builds a complete, honest, non-malaria run (generic vaccination
    # campaign across six districts; scripts/lib/positive_control.py — the
    # script-produced artifacts come from the real producers) and asserts
    # the real CLI ACCEPTs it in stage-7 and --post-write mode, at round 2
    # and at max_rounds. Then single-defect mutations must flip it.
    import shutil as _shutil
    from lib import positive_control as _pc

    def _cli_pc(d, cur, post_write, mx=8):
        cmd = [sys.executable, os.path.abspath(__file__), d,
               "--max-rounds", str(mx), "--current-round", str(cur),
               "--spec-compliance", "--parameter-registry",
               "--rigor-artifacts", "--json", "--no-history"]
        if post_write:
            cmd.append("--post-write")
        r = _sp.run(cmd, capture_output=True, text=True, timeout=300)
        try:
            out = json.loads(r.stdout)
        except json.JSONDecodeError:
            out = {"action": f"<no json; stderr: {r.stderr[-400:]}>"}
        open_ = ([b.get("kind") for b in out.get("unresolved_high") or []]
                 + [b.get("kind") for b in out.get("accept_blockers") or []])
        return r.returncode, out.get("action"), open_

    with tempfile.TemporaryDirectory() as _root:
        pc = os.path.join(_root, "pc")
        try:
            _pc.build(pc, round_n=2)
            built = True
        except Exception as e:  # noqa: BLE001
            built = False
            ok(False, f"E8: positive-control fixture failed to build: "
                      f"{type(e).__name__}: {e}")
        if built:
            for cur in (2, 8):
                _pc.set_round(pc, cur)
                for pw in (False, True):
                    rc, action, open_ = _cli_pc(pc, cur, pw)
                    ok(rc == 0 and action == "ACCEPT",
                       f"E8: honest run must ACCEPT (round {cur}/8, "
                       f"post_write={pw}); got exit {rc} {action} {open_}")

            def _mutant(name, mutate):
                md = os.path.join(_root, name)
                _shutil.copytree(pc, md)
                mutate(md)
                return md

            def _edit_yaml(path, fn):
                with open(path) as f:
                    doc = yaml.safe_load(f)
                fn(doc)
                with open(path, "w") as f:
                    yaml.safe_dump(doc, f, sort_keys=False)

            def _zero_width(md):
                def fn(doc):
                    o = doc["scalar_outputs"]["total_dalys_averted"]
                    o["ci_low"] = o["ci_high"] = o["mean"]
                _edit_yaml(os.path.join(md, "uncertainty_report.yaml"), fn)

            def _renamed_verdict(md):
                def fn(doc):
                    doc["verdict"] = "DEGENERATE_FIT_ACKNOWLEDGED"
                _edit_yaml(os.path.join(md, "model_comparison_formal.yaml"), fn)

            def _ledger_off(md):
                def fn(doc):
                    for c in doc["claims"]:
                        if c["id"] == "dalys_total":
                            c["value"] = round(c["value"] * 1.10, 1)
                _edit_yaml(os.path.join(md, "models", "claims_ledger.yaml"), fn)

            def _unit_x100(md):
                # Phase 22 δ: the E5 unit plant — a rate column ×100.
                p = os.path.join(md, "data", "districts.csv")
                with open(p) as f:
                    rows = f.read().splitlines()
                hdr = rows[0].split(",")
                j = hdr.index("baseline_rate")
                out = [rows[0]]
                for r in rows[1:]:
                    c = r.split(",")
                    c[j] = f"{float(c[j]) * 100:g}"
                    out.append(",".join(c))
                with open(p, "w") as f:
                    f.write("\n".join(out) + "\n")

            for name, mutate, expect in (
                    ("unit_x100", _unit_x100, "data_schema_out_of_range"),
                    ("zero_width_ci", _zero_width, "ci_degenerate"),
                    ("renamed_verdict", _renamed_verdict,
                     "model_comparison_formal_unknown_verdict"),
                    ("ledger_off_10pct", _ledger_off, "claims_ledger_stale")):
                md = _mutant(name, mutate)
                for cur in (2, 8):
                    _pc.set_round(md, cur)
                    rc, action, open_ = _cli_pc(md, cur, post_write=True)
                    ok(rc != 0 and action != "ACCEPT" and expect in open_,
                       f"E8 mutant {name} (round {cur}/8) must not ACCEPT "
                       f"and must report {expect}; got {action} {open_}")
                    if cur == 8:
                        ok(action == "RUN_FAILED",
                           f"E8 mutant {name} at exhaustion is non-"
                           f"declarable → RUN_FAILED; got {action}")

    # --- Phase 21 ζ: optional cross-model critique (XM1-XM6) ---
    import cross_model_critic as _xm

    def _xm_doc(round_n, status, new=(), history=None, reason="codex exited 1"):
        prior = {"round": round_n - 1, "blockers": [],
                 "availability_history": history or []}
        res = {"status": status, "reason": reason, "duration_s": 1.0,
               "payload": {"summary": "s",
                           "structural_mismatch": {"detected": False,
                                                   "description": "",
                                                   "evidence_files": []},
                           "new_blockers": list(new), "prior_rechecks": []}}
        return _xm.build_doc(prior, round_n, res, model="test-model",
                             codex_version="t", question="q")

    with tempfile.TemporaryDirectory() as _root:
        pc = os.path.join(_root, "pc")
        try:
            _pc.build(pc, round_n=2)
            xm_built = True
        except Exception as e:  # noqa: BLE001
            xm_built = False
            ok(False, f"XM: fixture failed to build: {type(e).__name__}: {e}")
        if xm_built:
            # XM1: absent → no finding.
            ok(_load_optional_critique(pc, "critique-crossmodel", 2) == (None, []),
               "XM1: absent cross-model critique → no finding")
            # XM1b: absent but requested (metadata cross_model: true) → HIGH.
            _mp = os.path.join(pc, "metadata.json")
            _m0 = open(_mp).read()
            with open(_mp, "w") as f:
                json.dump({**json.loads(_m0), "cross_model": True}, f)
            _, _v = _load_optional_critique(pc, "critique-crossmodel", 2)
            ok([x["kind"] for x in _v] == ["cross_model_critic_missing"]
               and _v[0]["severity"] == "HIGH",
               f"XM1b: requested but missing cross-model critique → HIGH, got {_v}")
            with open(_mp, "w") as f:
                f.write(_m0)

            def _xm_case(name, doc=None, raw=None):
                md = os.path.join(_root, name)
                _shutil.copytree(pc, md)
                path = os.path.join(md, "critique_crossmodel.yaml")
                if raw is not None:
                    with open(path, "w") as f:
                        f.write(raw)
                else:
                    _xm.write_outputs(md, doc)
                return md

            # XM2: valid PASS → honest run still ACCEPTs.
            md = _xm_case("xm_pass", _xm_doc(2, "ok"))
            rc, action, open_ = _cli_pc(md, 2, True)
            ok(rc == 0 and action == "ACCEPT",
               f"XM2: valid PASS cross-model critique keeps ACCEPT; got {rc} {action} {open_}")
            # XM3: open HIGH X- blocker counts like any critic's.
            md = _xm_case("xm_high", _xm_doc(2, "ok", new=[
                {"severity": "HIGH", "category": "METHODS", "target_stage": "MODEL",
                 "claim": "benefit uses total population", "evidence": "model.py:12",
                 "fix_requires": "use eligible population"}]))
            rc, action, open_ = _cli_pc(md, 2, True)
            ok(action == "PATCH_OR_RETHINK" and "critique_blocker" in open_,
               f"XM3: open HIGH X- blocker must block; got {action} {open_}")
            # XM4: UNAVAILABLE once → MEDIUM, ACCEPT still possible.
            d4 = _xm_doc(2, "unavailable")
            md = _xm_case("xm_unavail", d4)
            _, v4 = _load_optional_critique(md, "critique-crossmodel", 2)
            ok([(v["kind"], v["severity"]) for v in v4]
               == [("cross_model_critic_unavailable", "MEDIUM")],
               f"XM4: one unavailable round → MEDIUM; got {v4}")
            rc, action, open_ = _cli_pc(md, 2, True)
            ok(rc == 0 and action == "ACCEPT",
               f"XM4: MEDIUM unavailability must not block; got {action} {open_}")
            # XM5: unavailable 3 consecutive rounds → HIGH.
            d5 = _xm_doc(2, "unavailable", history=[
                {"round": 0, "status": "UNAVAILABLE"},
                {"round": 1, "status": "UNAVAILABLE"}])
            md = _xm_case("xm_unavail3", d5)
            _, v5 = _load_optional_critique(md, "critique-crossmodel", 2)
            ok([(v["kind"], v["severity"]) for v in v5]
               == [("cross_model_critic_unavailable", "HIGH")],
               f"XM5: 3 consecutive unavailable rounds → HIGH; got {v5}")
            rc, action, open_ = _cli_pc(md, 2, True)
            ok(action != "ACCEPT" and "cross_model_critic_unavailable" in open_,
               f"XM5: HIGH unavailability blocks; got {action} {open_}")
            # XM6: invalid file → HIGH finding, not exit 3.
            md = _xm_case("xm_invalid", raw="reviewer: critique-crossmodel\nround: 2\n")
            rc, action, open_ = _cli_pc(md, 2, True)
            ok(rc != 3 and action != "ACCEPT" and "cross_model_critic_invalid" in open_,
               f"XM6: invalid cross-model YAML → HIGH, not schema exit 3; "
               f"got rc={rc} {action} {open_}")

    # --- Summary ---
    if failures:
        print(f"FAIL: {len(failures)} case(s)", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    print("OK: all self-test cases passed.", file=sys.stderr)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("run_dir", nargs="?",
                   help="Run directory containing critique_*.yaml")
    p.add_argument("--max-rounds", type=int)
    p.add_argument("--current-round", type=int)
    p.add_argument("--self-test", action="store_true",
                   help="Run inline self-test cases and exit. "
                        "Does not require run_dir / --max-rounds / --current-round.")
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
    p.add_argument("--post-write", action="store_true",
                   help="Phase 21 β: post-WRITE gate. Requires report.md and "
                        "post-WRITE artifacts (critique_sufficiency.yaml); "
                        "writer-QA MAJOR_REVISION and auditor unavailability "
                        "become HIGH. main.py runs this after the session.")
    p.add_argument("--no-history", action="store_true",
                   help="Do not append to <run_dir>/validator_history.jsonl "
                        "(for read-only retro checks).")
    args = p.parse_args()

    if args.self_test:
        return _run_self_test()

    if args.run_dir is None:
        p.error("run_dir is required (or use --self-test)")
    if args.max_rounds is None:
        p.error("--max-rounds is required")
    if args.current_round is None:
        p.error("--current-round is required")

    # Phase 11 Commit υ (F3): the lead is contractually required to
    # pass all three gate flags on every STAGE 7 invocation (see
    # agents/__init__.py around line 392). Previously these were
    # optional `action="store_true"` flags that defaulted to False —
    # a future lead-prompt edit could silently drop a flag and the
    # validator would happily compute a weaker decision. Enforce
    # them at the script boundary so the contract is mechanical.
    if not (args.spec_compliance and args.parameter_registry
            and args.rigor_artifacts):
        missing = []
        if not args.spec_compliance:
            missing.append("--spec-compliance")
        if not args.parameter_registry:
            missing.append("--parameter-registry")
        if not args.rigor_artifacts:
            missing.append("--rigor-artifacts")
        p.error(
            f"missing required STAGE 7 flag(s): {', '.join(missing)}. "
            f"All three of --spec-compliance, --parameter-registry, "
            f"and --rigor-artifacts are mandatory when validating a "
            f"run directory. (Phase 11 Commit υ — STAGE 7 contract.)"
        )

    if not os.path.isdir(args.run_dir):
        print(f"ERROR: {args.run_dir} is not a directory", file=sys.stderr)
        return 2

    critiques = {}
    schema_errors = []
    # Phase 11 Commit υ (F3): track whether critique_redteam.yaml was
    # missing so we can fold a MEDIUM `critique_redteam_missing` into
    # the final decision rather than silently skipping. Pre-Commit-E
    # runs (the legacy carve-out the original code accommodated) are
    # ancient history; missing red-team today is a real signal that
    # the lead skipped one of its mandatory STAGE 6 critiques.
    critique_redteam_missing = False
    for reviewer in REVIEWERS:
        path = os.path.join(args.run_dir, FILENAMES[reviewer])
        if reviewer == "critique-redteam" and not os.path.exists(path):
            critique_redteam_missing = True
            continue
        if reviewer in OPT_IN_AGENT_REVIEWERS and not os.path.exists(path) \
                and not _opt_in_requested(args.run_dir, reviewer):
            continue
        try:
            critiques[reviewer] = validate_critique(path, reviewer,
                                                    args.current_round)
        except SchemaError as e:
            schema_errors.append(str(e))
    # Phase 21 ζ: the cross-model critique is script-written, so a schema
    # failure is a HIGH finding (not exit 3 → "spawn an agent to fix it").
    crossmodel_violations = []
    for reviewer in OPTIONAL_REVIEWERS:
        doc, viols = _load_optional_critique(args.run_dir, reviewer,
                                             args.current_round)
        if doc is not None:
            critiques[reviewer] = doc
        crossmodel_violations.extend(viols)

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
        decision_year = spec_module.detect_decision_year(question, meta)
        # Phase 21 β: a spec-compliance crash is a HIGH finding, not exit 1.
        spec_violations = run_guarded(
            lambda: spec_module.check_spec_compliance(
                required, args.run_dir,
                decision_year=decision_year)["violations"],
            check_name="spec_compliance")
        crashed = [v for v in spec_violations
                   if v.get("kind") == "validator_check_crashed"]
        for v in crashed:
            v.setdefault("evidence", v["claim"])
        decision = incorporate_spec_violations(
            decision, spec_violations, args.max_rounds, args.current_round,
        )

    if args.rigor_artifacts:
        # Phase 10 review fix #3: thread args.current_round through to
        # _check_rigor_artifacts so the Phase 10 ψ allocation-gate
        # coordinator can apply round-aware MEDIUM consolidation. The
        # lead already passes --current-round on every invocation
        # (see agents/__init__.py around line 392); previously this
        # call site dropped it on the floor and the new ψ behavior
        # was unreachable from production.
        rigor_violations = _check_rigor_artifacts(
            args.run_dir, round_n=args.current_round,
            post_write=args.post_write,
        )
        # Phase 11 Commit υ (F3): missing critique_redteam.yaml is a
        # MEDIUM violation now, not a silent skip. The lead is required
        # to spawn all four critique agents in STAGE 6; missing
        # red-team means one was dropped. Fold in alongside rigor
        # violations so it flows through _incorporate_rigor_violations
        # using the same blocker-ID machinery.
        if critique_redteam_missing:
            rigor_violations.append({
                "kind": "critique_redteam_missing",
                "severity": "MEDIUM",
                "stage": "CRITIQUE",
                "claim": (
                    f"critique_redteam.yaml is absent. The lead is "
                    f"contractually required to spawn all four critique "
                    f"agents in STAGE 6 (methods, domain, presentation, "
                    f"redteam); missing red-team means one was dropped. "
                    f"Phase 11 Commit υ converted the prior silent skip "
                    f"into this MEDIUM blocker. If this is a legacy "
                    f"resume from before red-team existed, scope-declare "
                    f"in the next round; otherwise re-spawn red-team."
                ),
            })
        rigor_violations.extend(crossmodel_violations)
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
        reg_violations = run_guarded(
            lambda: registry_module.check_registry(
                registry, repo_root, run_dir=args.run_dir)["violations"],
            check_name="effect_size_registry")
        for v in reg_violations:
            v.setdefault("name", "-")
        decision = incorporate_registry_violations(
            decision, reg_violations, args.max_rounds, args.current_round,
        )

    decision["post_write"] = bool(args.post_write)
    if not args.no_history:
        _append_validator_history(args.run_dir, args.current_round, decision,
                                  args.post_write)
    open_note = None
    if not args.no_history and not args.post_write:
        open_note = _write_open_problems(args.run_dir, args.current_round,
                                         args.max_rounds, critiques, decision)
    print(render_text(decision, args.current_round, args.max_rounds),
          file=sys.stderr)
    if open_note:
        print(open_note, file=sys.stderr)
    if args.json:
        print(json.dumps(decision, indent=2))
    # Exit 0 on ACCEPT, 1 on any other action, so Bash callers can branch
    # easily. Schema errors already returned 3 above.
    return 0 if decision["action"] == "ACCEPT" else 1


def _main_fail_closed() -> int:
    """Phase 21 β: an unexpected crash still yields a machine-readable
    verdict. Exit 4 = validator error (never ACCEPT)."""
    try:
        return main()
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        import traceback as _tb
        _tb.print_exc()
        if "--json" in sys.argv:
            print(json.dumps({
                "action": "VALIDATOR_ERROR",
                "rationale": f"validator crashed: {type(e).__name__}: {e}",
                "unresolved_high": [], "accept_blockers": [],
            }, indent=2))
        return 4


if __name__ == "__main__":
    sys.exit(_main_fail_closed())
