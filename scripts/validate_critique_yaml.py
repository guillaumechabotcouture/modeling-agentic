#!/usr/bin/env python3
"""
Validate and evaluate critique YAML files for the v2 decision gate.

Exposes the schema validator (`validate_critique`, `SchemaError`), decision
engine (`decide`, `_recompute_action`), declaration/done_when/brief-scope
helpers (`is_non_declarable`, `_declared_ids`, `_previous_declared`,
`_apply_brief_scope`, `_apply_done_when`, `_defer_write_findings`,
`after_write_item`), violation incorporators (`incorporate_*_violations`),
optional critique loader (`_load_optional_critique`), and validator history
writer (`_append_validator_history`).

Usage:
    python scripts/validate_critique_yaml.py --self-test
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

try:
    import yaml
except ImportError:
    print("ERROR: pyyaml not installed. Run: pip install pyyaml", file=sys.stderr)
    sys.exit(2)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib.scope_declarations import (  # noqa: E402
    CRITIC_DERIVED_PREFIXES as _CRITIC_DERIVED_PREFIXES, load as _load_scope_declarations,
    entry_declared_ids as _entry_declared_ids, entry_has_justification as _entry_has_justification)


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
    # Phase 49 α: a declared HIGH finding without a usable decision test, or
    # one whose test shows the decision changes.
    "scope_declaration_untested",
    "scope_declaration_test_invalid",
    "declared_finding_changes_decision",
    # Phase 49 β: the owner declined the declaration.
    "scope_declaration_declined",
    # v2 ACQUIRE (§§3.3, 5): quoted-span violations, unlisted acquired citations, acquired derivation failures
    "observation_quote_not_found",
    "observation_acquired_unbound",
    "acquired_input_unlisted",
    "derivation_failed_on_acquired",
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


_NON_DECLARABLE_MANIFEST_PREFIXES = (
    "data_schema_",
    "identifiability_a_priori_",
    "calibration_spec_",
    "headline_binding_",
    "mechanism_manifest_",
    "claims_ledger_",
    "calibration_result_",
    "eligibility_manifest_",
    "stall_diagnoses_",
    "decision_tests_",
    "scope_signoff_",
)


def _manifest_non_declarable_prefixes() -> tuple[str, ...]:
    return _NON_DECLARABLE_MANIFEST_PREFIXES


def is_non_declarable(kind: str, explicit: Any = None) -> bool:
    """Phase 21 β: may a finding of this kind be closed by DECLARE_SCOPE?"""
    if explicit is False:
        return True
    k = str(kind or "")
    if k in _NON_DECLARABLE_KINDS or k.endswith(_NON_DECLARABLE_SUFFIXES):
        return True
    return k.startswith(_manifest_non_declarable_prefixes())


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
VALID_TARGET_STAGE = {"PLAN", "DATA", "MODEL", "ANALYZE", "WRITE", "ENGINE"}


class SchemaError(Exception):
    """A critique file the gate refuses. `errors` lists every problem found in the file (the
    message is the first): a schema repair carries all of them at once, so a critic that fixes
    exactly what it is told converges in one pass (v2 eval runs 2026-11-05, T74-T75)."""

    def __init__(self, msg: str, errors: list[str] | None = None):
        super().__init__(msg)
        self.errors = list(errors) if errors else [msg]


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise SchemaError(msg)


def _one_of(v: Any, allowed: set) -> bool:
    """`v in allowed` that is False, not a TypeError, for an unhashable value (a list or mapping)."""
    return isinstance(v, str) and v in allowed


class _Errors(list):
    """Collects schema errors in check order; `need` records one and returns the condition."""

    def need(self, cond: bool, msg: str) -> bool:
        if not cond:
            self.append(msg)
        return bool(cond)


def _load(path: str) -> dict:
    try:
        with open(path) as f:
            doc = yaml.safe_load(f)
    except FileNotFoundError:
        raise SchemaError(f"missing file: {path}")
    except yaml.YAMLError as e:
        raise SchemaError(f"invalid YAML in {path}: {e}")
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError) as e:
        # A tag or scalar PyYAML cannot construct (`2026-99-99`, `!!bool nonsense`) is not a
        # YAMLError: still a refused file, never a crash (Codex review round 2).
        raise SchemaError(f"{os.path.basename(path)}: YAML that cannot be read: {type(e).__name__}: {e}"[:300])
    _require(isinstance(doc, dict), f"{path}: top-level must be a mapping")
    return doc


def _validate_blocker(b: Any, ctx: str, prefix: str, errs: _Errors) -> bool:
    """Record every problem with one blocker; True when it is well formed."""
    n0 = len(errs)
    if not errs.need(isinstance(b, dict), f"{ctx}: each blocker must be a mapping"):
        return False
    for key in ("id", "severity", "category", "target_stage",
                "first_seen_round", "claim", "evidence", "fix_requires",
                "resolved"):
        errs.need(key in b, f"{ctx}: blocker missing required key '{key}'")
    bid = b.get("id")
    if "id" in b:
        errs.need(isinstance(bid, str) and bid.startswith(prefix),
                  f"{ctx}: blocker id '{bid}' must start with '{prefix}'")
    if "severity" in b:
        errs.need(_one_of(b["severity"], VALID_SEVERITY),
                  f"{ctx}: blocker {bid} severity must be one of {VALID_SEVERITY}")
    if "category" in b:
        errs.need(_one_of(b["category"], VALID_CATEGORY),
                  f"{ctx}: blocker {bid} category must be one of {VALID_CATEGORY}")
    if "target_stage" in b:
        errs.need(_one_of(b["target_stage"], VALID_TARGET_STAGE),
                  f"{ctx}: blocker {bid} target_stage must be one of {VALID_TARGET_STAGE}")
    if "first_seen_round" in b:
        errs.need(isinstance(b["first_seen_round"], int) and b["first_seen_round"] >= 1,
                  f"{ctx}: blocker {bid} first_seen_round must be int >= 1")
    if "resolved" in b and errs.need(isinstance(b["resolved"], bool),
                                     f"{ctx}: blocker {bid} resolved must be bool") and b["resolved"]:
        for key in ("resolved_in_round", "resolved_evidence"):
            errs.need(key in b, f"{ctx}: resolved blocker {bid} missing '{key}'")
    return len(errs) == n0


def _validate_structural(sm: Any, ctx: str, reviewer: str, errs: _Errors) -> None:
    if not errs.need(isinstance(sm, dict), f"{ctx}: structural_mismatch must be a mapping"):
        return
    if not errs.need("detected" in sm and isinstance(sm["detected"], bool),
                     f"{ctx}: structural_mismatch.detected must be bool"):
        return
    if reviewer == "critique-presentation":
        errs.need(sm["detected"] is False,
                  f"{ctx}: critique-presentation must not set structural_mismatch.detected=true; "
                  f"use a HIGH blocker instead")
    if sm["detected"]:
        for key in ("description", "evidence_files", "fix_requires"):
            errs.need(key in sm, f"{ctx}: structural_mismatch.detected=true requires '{key}'")
        if "fix_requires" in sm:
            errs.need(sm["fix_requires"] == "RETHINK",
                      f"{ctx}: structural_mismatch.fix_requires must be 'RETHINK'")


def _validate_carried_forward(cf: Any, ctx: str, current_round: int,
                              blockers: list[dict], errs: _Errors) -> None:
    if not errs.need(isinstance(cf, list), f"{ctx}: carried_forward must be a list"):
        return
    blocker_ids = {b["id"]: b for b in blockers
                   if isinstance(b, dict) and isinstance(b.get("id"), str)}
    for entry in cf:
        if not errs.need(isinstance(entry, dict),
                         f"{ctx}: each carried_forward entry must be a mapping "
                         f"{{id, prior_round, still_present, notes}}, got {entry!r}"[:300]):
            continue
        for key in ("id", "prior_round", "still_present", "notes"):
            errs.need(key in entry, f"{ctx}: carried_forward entry missing '{key}'")
        if "still_present" not in entry or not errs.need(
                isinstance(entry["still_present"], bool),
                f"{ctx}: carried_forward.still_present must be bool"):
            continue
        # cross-check with blockers
        bid = entry.get("id")
        b = blocker_ids.get(bid) if isinstance(bid, str) else None
        if entry["still_present"]:
            errs.need(b is not None and b.get("resolved") is False,
                      f"{ctx}: carried_forward {bid} still_present=true, "
                      f"but no matching unresolved blocker in current blockers list")
        else:
            errs.need(b is not None and b.get("resolved") is True,
                      f"{ctx}: carried_forward {bid} still_present=false, "
                      f"but no matching resolved blocker in current blockers list")


def validate_critique(path: str, expected_reviewer: str,
                      current_round: int, checklist: bool = False) -> dict:
    """The parsed critique, or SchemaError listing every problem in the file (`.errors`).
    `checklist`: True for a v2 gate, where a checklist reviewer answers every item of the structural
    checklist; False (the legacy gate) requires none."""
    ctx = os.path.basename(path)
    if checklist and expected_reviewer in _checklist_module().CHECKLIST_REVIEWERS:
        # Codex r3 of #366: read with no duplicate key, and a parse failure named by fixed text (the parser's report
        # quotes the file; the repair's review message carries it as data, critique_checklist.repair_data).
        CL0 = _checklist_module()
        try:
            with open(path, "rb") as fh:
                raw0 = fh.read()
        except FileNotFoundError:
            raise SchemaError(f"missing file: {path}")
        except OSError:
            raise SchemaError(f"{ctx}: {CL0.LOAD_ERROR}")
        doc, diag = CL0.strict_load(raw0)
        if diag is not None:
            raise SchemaError(f"{ctx}: {CL0.LOAD_ERROR}")
        _require(isinstance(doc, dict), f"{ctx}: top-level must be a mapping")
    else:
        doc = _load(path)
    prefix = PREFIXES[expected_reviewer]
    errs = _Errors()

    errs.need(doc.get("reviewer") == expected_reviewer,
              f"{ctx}: reviewer must be '{expected_reviewer}', "
              f"got {doc.get('reviewer')!r}")
    errs.need(doc.get("round") == current_round,
              f"{ctx}: round must be {current_round}, got {doc.get('round')!r}")
    valid_verdicts = (VALID_VERDICT | {"UNAVAILABLE"}
                      if expected_reviewer in OPTIONAL_REVIEWERS else VALID_VERDICT)
    errs.need(_one_of(doc.get("verdict"), valid_verdicts),
              f"{ctx}: verdict must be one of {valid_verdicts}")
    if _one_of(doc.get("verdict"), {"UNAVAILABLE"}):
        errs.need(isinstance(doc.get("unavailable_reason"), str)
                  and doc["unavailable_reason"].strip(),
                  f"{ctx}: verdict UNAVAILABLE requires a non-empty "
                  f"unavailable_reason")

    _validate_structural(doc.get("structural_mismatch"), ctx, expected_reviewer, errs)

    # Phase 21 β: `blockers:` is required (an omitted list used to read as
    # "no blockers" → ACCEPT), and a REVISE verdict must name at least one
    # unresolved blocker — REVISE with nothing open is self-contradictory.
    blockers = doc.get("blockers")
    if errs.need("blockers" in doc, f"{ctx}: missing required key 'blockers' "
                                    f"(use `blockers: []` for none)") \
            and errs.need(isinstance(blockers, list), f"{ctx}: blockers must be a list"):
        seen_ids = set()
        for b in blockers:
            _validate_blocker(b, ctx, prefix, errs)
            bid = b.get("id") if isinstance(b, dict) else None
            if isinstance(bid, str):
                errs.need(bid not in seen_ids, f"{ctx}: duplicate blocker id {bid}")
                seen_ids.add(bid)
        if _one_of(doc.get("verdict"), {"REVISE"}):
            errs.need(any(isinstance(b, dict) and b.get("resolved") is False
                          for b in blockers),
                      f"{ctx}: verdict REVISE requires at least one unresolved "
                      f"blocker")
    else:
        blockers = []

    if current_round == 1:
        errs.need(doc.get("carried_forward", []) == [],
                  f"{ctx}: carried_forward must be [] on round 1")
    else:
        _validate_carried_forward(doc.get("carried_forward"), ctx,
                                  current_round, blockers, errs)

    # Owner 2026-11-23: the domain and red-team critics answer the structural checklist
    # (toolkit/spec/critique_checklist.py); a missing or malformed item is a schema error.
    if checklist:
        CL = _checklist_module()
        if expected_reviewer in CL.CHECKLIST_REVIEWERS:
            errs.extend(CL.problems(doc.get("checklist"), ctx,
                                    [b for b in blockers if isinstance(b, dict)]))

    if errs:
        raise SchemaError(errs[0], errs)
    return doc


def _checklist_module():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if root not in sys.path:
        sys.path.insert(0, root)
    from toolkit.spec import critique_checklist
    return critique_checklist


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
      3a every outstanding item declared at this gate and the last,
         none from a critic, all declarable → DECLARE_SCOPE (rounds left)
      2  HIGH or accept-blocker, rounds → PATCH_OR_RETHINK
      5  exhausted, any non-declarable  → RUN_FAILED
      3  exhausted, all declarable      → DECLARE_SCOPE
      4  nothing outstanding            → ACCEPT

    `accept_blockers` are required artifacts that are absent (Phase 21
    β): they forbid ACCEPT at every round, even when no HIGH is open.

    Harness baseline hb2 fix 1, and the owner's decision (b) of 2026-11-23:
    a HIGH whose resolution is the owner's (`owner_resolution`: a measured
    decision test that changes the plan; agents/stage_machine.py
    apply_owner_rule) is put to the owner at the first gate (rule 2o), not
    at the round cap; once the owner (or, under --signoff lead, the lead
    provisionally) accepts it, it no longer blocks ACCEPT: it moves from
    `unresolved_high` to `owner_accepted_high`, and the report states the
    dependence at its top. Declined or unanswered, it never accepts. Other
    owner items (policy-mapped kinds, scripts/scope_signoff.py) keep rule 3o.
    """
    moved = [b for b in d.get("unresolved_high") or [] if isinstance(b, dict) and b.get("owner_resolution")
             and b.get("owner_signed") and not b.get("owner_signoff") and not b.get("owner_declined")
             and not is_non_declarable(b.get("kind"), b.get("scope_declarable"))]
    if moved:
        d["unresolved_high"] = [b for b in d["unresolved_high"] if not any(b is m for m in moved)]
        held = list(d.get("owner_accepted_high") or [])
        seen = {(b.get("kind"), b.get("claim")) for b in held if isinstance(b, dict)}
        d["owner_accepted_high"] = held + [b for b in moved if (b.get("kind"), b.get("claim")) not in seen]
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
    elif rounds_remaining > 0 and any(b.get("owner_resolution") and b.get("owner_signoff") for b in outstanding):
        # hb2 fix 1: no fix task can change a measured decision test, so its owner is asked now; the gate re-runs
        # on the answers and the other findings keep their rules
        ask = [b for b in outstanding if b.get("owner_resolution") and b.get("owner_signoff")]
        action = "AWAIT_OWNER"
        rationale = (
            f"{len(ask)} HIGH finding(s) are the owner's to resolve "
            f"({', '.join(str((b.get('owner_item') or {}).get('finding') or b.get('kind')) for b in ask)[:300]}): "
            f"no fix task can change them, so the owner is asked now, with {rounds_remaining} round(s) left. The "
            f"gate wrote scope_signoff_request.json; it re-runs on the answers.")
        rule_matched = "2o"
    elif outstanding and rounds_remaining > 0 and _declared_early(outstanding, d):
        # Phase 39: a run whose only outstanding items are gate findings it
        # declared (with a justification) at this gate and the previous one
        # finishes now. E7 attempt 7 carried three such findings (two locked
        # benchmarks, an unstable sensitivity result) through every round, so it
        # could not finish before the last round however its modeling went.
        action = "DECLARE_SCOPE"
        rationale = (
            f"{len(outstanding)} outstanding item(s), all declared in scope_declaration.yaml at this gate "
            f"and the previous one, none from a critic; {rounds_remaining} round(s) left are not needed. "
            f"Write the report with each declaration verbatim in Limitations. DECLARE_SCOPE is NOT ACCEPT.")
        rule_matched = "3a"
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

    owner = [b for b in outstanding if b.get("owner_signoff")]
    if action == "DECLARE_SCOPE" and owner:
        # Phase 49 β (A21): these bear on the primary decision; only the owner may accept them
        # as declared limitations (scripts/scope_signoff.py).
        action = "AWAIT_OWNER"
        rationale = (
            f"Rule {rule_matched} would DECLARE_SCOPE, but {len(owner)} of the declared finding(s) bear on the "
            f"primary decision ({', '.join(str((b.get('owner_item') or {}).get('finding') or b.get('kind')) for b in owner)[:300]}). "
            f"The owner decides: the gate wrote scope_signoff_request.json. Do not spawn the writer. Owner mode: end "
            f"your turn with the line AWAITING_SIGNOFF; the harness resumes you with the answers. Lead mode: review "
            f"`python3 scripts/scope_signoff.py show {{run_dir}}`, write scope_signoff_answers.yaml and end your turn with "
            f"AWAITING_SIGNOFF; the harness signs.")
        rule_matched = f"{rule_matched}o"

    d["rounds_remaining"] = rounds_remaining
    d["action"] = action
    d["rule_matched"] = rule_matched
    d["rationale"] = rationale
    return d


# Gate kinds that carry a critic's finding under another name (never declared
# away early): _CRITIC_DERIVED_PREFIXES, from lib.scope_declarations.


def _declared_early(outstanding: list[dict], d: dict) -> bool:
    """Phase 39 rule 3a: every outstanding item is a declarable gate finding
    that scope_declaration.yaml declares, by kind, now and at the previous
    gate. Never with a required artifact missing, a critic's blocker, or a
    critic's finding carried under a gate kind (persistence escalation, a
    regressed or unmet finding test, the sufficiency critic). Gate ids
    (RIG-001...) are renumbered every run, so only the kind is matched.
    Phase 47 ε: a gate kind's `<kind>_persistent` counts as declared when it
    and its base kind both are; a stalled finding without a diagnosis never."""
    now, prev = set(d.get("declared") or ()), set(d.get("declared_prev") or ())
    if not now or not prev or d.get("accept_blockers"):
        return False
    for b in outstanding:
        kind = str(b.get("kind") or "")
        # Phase 47 ε: a gate kind's own persistence escalation may be declared like
        # its base kind (attempt 8 rounds 6-7); a critic blocker's (`D-014_persistent`) not.
        gate_persistent = (kind.endswith("_persistent")
                           and kind[:-len("_persistent")] in _VALIDATOR_KIND_ESCALATION_THRESHOLDS)
        if ((kind.endswith("_persistent") and not gate_persistent)
                or kind.endswith("_without_diagnosis") or kind.startswith(_CRITIC_DERIVED_PREFIXES)
                or str(b.get("source") or b.get("reviewer") or "").startswith("critique")):
            return False
        if is_non_declarable(kind, b.get("scope_declarable")):
            return False
        if not (kind in now and kind in prev):
            return False
        base = kind[:-len("_persistent")] if gate_persistent else kind
        if not (base in now and base in prev):
            return False
    return True


def _declared_ids(run_dir: str) -> list[str]:
    """Blocker ids / finding kinds that scope_declaration.yaml declares with a
    justification (structured fields only: prose mentions do not declare)."""
    _doc, entries, _f = _load_scope_declarations(run_dir)
    out: set[str] = set()
    for e in entries:
        if _entry_has_justification(e):
            out |= _entry_declared_ids(e, structured_only=True)
    return sorted(out)


def _previous_declared(run_dir: str, current_round: int) -> list[str]:
    """What the last in-round gate before this round recorded as declared."""
    prev = None
    try:
        with open(os.path.join(run_dir, _VALIDATOR_HISTORY_FILE), encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if (isinstance(rec, dict) and not rec.get("post_write") and isinstance(rec.get("round"), int)
                        and rec["round"] < current_round):
                    prev = rec
    except OSError:
        return []
    return list((prev or {}).get("declared") or [])


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
                    "fix_requires": b.get("fix_requires"),   # F6 S1-7: the remedy travels with the claim
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
    # (an entry the owner accepted was moved to owner_accepted_high: still present, hb2 fix 1)
    existing = [b for b in list(d["unresolved_high"]) + list(d.get("owner_accepted_high") or [])
                if isinstance(b, dict) and b.get("reviewer") == reviewer]
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
    # Phase 34: findings left without a done_when test for 3 rounds.
    "critique_done_when_missing": 3,
    "benchmark_targets_missing": 2,
    "pre_model_over_saturated_with_commitment": 2,
    "weakly_identified_parameters": 3,
}

# Phase 21 β: per-round validator findings, appended by main() on every
# invocation. Replaces counting `stage7_round*_stderr.txt` files, which no
# prompt or code reliably wrote (present in 2 of 8 runs).
_VALIDATOR_HISTORY_FILE = "validator_history.jsonl"


def _append_validator_history(run_dir: str, round_n: int, decision: dict,
                              post_write: bool) -> None:
    import datetime as _dt
    rec = {
        "round": round_n,
        "post_write": post_write,
        "at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "action": decision.get("action"),
        "declared": list(decision.get("declared") or []),
        "findings": [{"kind": v.get("kind"), "severity": v.get("severity"),
                      "stage": v.get("stage"),
                      **({"key": v["stall_key"]} if "stall_key" in v else {})}   # Phase 47 ε
                     for v in (decision.get("rigor_violations") or [])],
    }
    try:
        from lib.safe_io import append_text           # no link followed (follow-up to review round 5)
        append_text(os.path.join(run_dir, _VALIDATOR_HISTORY_FILE), json.dumps(rec) + "\n", run_dir)
    except OSError as e:
        print(f"WARNING: could not append validator history: {e}",
              file=sys.stderr)


def after_write_item(b: dict) -> bool:
    """Findings that wait for the first draft (target_stage WRITE or category PRESENTATION)."""
    return (str(b.get("target_stage") or "").upper() == "WRITE"
            or str(b.get("category") or "").upper() == "PRESENTATION")


_SCOPED_CRITICS = ("critique-domain", "critique-presentation", "critique-redteam", "critique-methods")


def _brief_scope_filter(critiques: dict, run_dir: str) -> set[str]:
    try:
        with open(os.path.join(run_dir, "decision_brief.signoff.json"), encoding="utf-8") as f:
            if not isinstance(json.load(f), dict):
                return set()
    except (OSError, ValueError):
        return set()
    out: set[str] = set()
    for reviewer, doc in (critiques or {}).items():
        if reviewer in _SCOPED_CRITICS:
            for b in (doc or {}).get("blockers") or []:
                if (isinstance(b, dict) and b.get("severity") in ("HIGH", "MEDIUM")
                        and b.get("resolved") is not True
                        and str(b.get("brief_item") or "").strip().upper() == "NONE"):
                    out.add(str(b.get("id")))
    return out


def _apply_brief_scope(decision: dict, critiques: dict, run_dir: str,
                       max_rounds: int, current_round: int) -> dict:
    """Once the brief is signed, a critic finding tied to NONE neither blocks nor escalates."""
    outside = _brief_scope_filter(critiques, run_dir)
    if not outside:
        return decision
    decision["unresolved_high"] = [
        b for b in decision.get("unresolved_high") or []
        if not (b.get("kind") == "critique_blocker" and str(b.get("id")) in outside)]
    decision["outside_brief"] = sorted(outside)
    _drop_attempts(decision, outside)
    return _recompute_action(decision, max_rounds, current_round)


def _drop_attempts(decision: dict, ids) -> None:
    """Gemini review: findings closed by their test or put outside the brief
    no longer count toward the stuck-blocker escalation."""
    ids = {str(i) for i in ids}
    att = {k: v for k, v in (decision.get("blocker_attempts") or {}).items() if str(k) not in ids}
    decision["blocker_attempts"] = att
    decision["escalation_required"] = any(
        info.get("severity") == "HIGH" and info.get("patch_attempts", 0) >= _ESCALATION_HIGH_THRESHOLD
        for info in att.values())


def _defer_write_findings(decision: dict, run_dir: str, max_rounds: int, current_round: int) -> dict:
    """Codex review (Phases 31-34): a HIGH critic finding aimed at the write-up
    cannot be fixed before a draft exists, and the writer waits for the gate,
    so before the first draft it is listed as `after_write`, not blocking.
    The post-write gate blocks on it as usual."""
    if any(os.path.exists(os.path.join(run_dir, f)) for f in ("report.md", "manuscript.md")):
        return decision
    # Phase 39 α: presentation polish (category PRESENTATION) waits for the draft too.
    later = [b for b in decision.get("unresolved_high") or []
             if b.get("kind") == "critique_blocker" and after_write_item(b)]
    if not later:
        return decision
    decision["unresolved_high"] = [b for b in decision["unresolved_high"] if b not in later]
    decision["after_write"] = sorted(str(b.get("id")) for b in later)
    return _recompute_action(decision, max_rounds, current_round)


def _apply_done_when(decision: dict, critiques: dict, run_dir: str, max_rounds: int,
                     current_round: int, record: bool = True) -> tuple[dict, dict | None]:
    """Phase 34: a critic finding whose done_when test passes is resolved,
    whatever the critic's `resolved`; tests that passed are re-run every gate
    (regressions and missing tests come back as rigor findings via
    done_when.gate_findings). A failure here is reported, never fatal."""
    try:
        import done_when
        res = done_when.run(run_dir, critiques, current_round, record=record,
                            exclude=decision.get("outside_brief") or ())
    except Exception as e:  # noqa: BLE001 — fail closed: a HIGH finding via gate_findings
        decision["done_when_error"] = f"{type(e).__name__}: {e}"
        return decision, {"error": decision["done_when_error"]}
    decision["done_when_status"] = res["status"]
    # F6 S1-7: passed, failed and not judged are counted apart; an open HIGH whose test is not judged
    # cannot yield ACCEPT (the v2 machine also requires MEDIUM: stage_machine.require_judged).
    decision["check_summary"] = done_when.summary(res["status"], required=("HIGH",))
    if not res["resolved"]:
        return _require_judged(decision, max_rounds, current_round), res
    passed = set(res["resolved"])
    decision["unresolved_high"] = [
        b for b in decision.get("unresolved_high") or []
        if not (b.get("kind") == "critique_blocker" and str(b.get("id")) in passed)]
    decision["resolved_by_test"] = sorted(passed)
    _drop_attempts(decision, passed)
    return _require_judged(_recompute_action(decision, max_rounds, current_round), max_rounds, current_round), res


def _require_judged(decision: dict, max_rounds: int, current_round: int) -> dict:
    """F6 S1-7: ACCEPT never stands on a required test that is not judged (a backstop: the finding is
    normally still open and blocks by itself)."""
    nj = (decision.get("check_summary") or {}).get("required_not_judged") or []
    if decision.get("action") != "ACCEPT" or not nj:
        return decision
    decision.setdefault("accept_blockers", []).append({
        "reviewer": "done-when", "id": "REQ-NJ", "kind": "required_check_not_judged", "target_stage": "CRITIQUE",
        "scope_declarable": True, "first_seen_round": current_round,
        "claim": f"Required test(s) not judged: {', '.join(nj)}. Not judged is not a pass."})
    return _recompute_action(decision, max_rounds, current_round, "required checks not judged")



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
                    # Phase 49 β: a missing decision analysis is the owner's to accept.
                    **({"owner_signoff": True, "owner_item": v.get("owner_item")} if v.get("owner_signoff") else {}),
                })
            continue
        if v.get("severity") != "HIGH":
            continue
        entries.append({
            "category": "METHODS",
            "target_stage": v.get("target_stage") or "MODEL",   # e.g. a done_when regression's own stage
            "first_seen_round": v.get("first_seen_round", current_round),
            "claim": f"{v.get('stage', '?')}/{v['kind']}: {v.get('claim', '')}",
            "kind": v["kind"],
            "scope_declarable": not is_non_declarable(
                v["kind"], v.get("scope_declarable")),
            # Phase 49 β: a finding the owner must sign off before it is declared.
            **({"owner_signoff": True, "owner_item": v.get("owner_item")} if v.get("owner_signoff") else {}),
            # hb2 fix 1: a finding whose resolution is the owner's, and the owner's recorded accept
            **({k: v[k] for k in ("owner_resolution", "owner_signed", "owner_declined") if v.get(k)}),
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

    # --- SE1-SE4 (v2 eval runs 2026-11-05): every schema error in a file at once ---
    with tempfile.TemporaryDirectory() as d:
        pth = os.path.join(d, "critique_domain.yaml")
        rb = {"id": "D-001", "severity": "MEDIUM", "category": "HYPOTHESES", "target_stage": "WRITE",
              "first_seen_round": 1, "claim": "c", "evidence": "e", "fix_requires": "f", "resolved": True}
        with open(pth, "w") as fh:
            yaml.safe_dump({"reviewer": "critique-domain", "round": 2, "verdict": "PASS",
                            "structural_mismatch": {"detected": False}, "carried_forward": ["D-001"],
                            "blockers": [rb, dict(rb, id="D-002", category="NOPE")]}, fh)
        try:
            validate_critique(pth, "critique-domain", 2)
            errs = []
        except SchemaError as e:
            errs = list(getattr(e, "errors", [str(e)]))
        joined = " | ".join(errs)
        ok(all(w in joined for w in ("D-001 missing 'resolved_in_round'", "D-001 missing 'resolved_evidence'",
                                     "D-002 category", "D-002 missing 'resolved_in_round'",
                                     "carried_forward entry must be a mapping")),
           f"SE1 a refused critique lists every schema error, not the first: {errs}")
        try:
            validate_critique(pth, "critique-domain", 2)
            msg = ""
        except SchemaError as e:
            msg = str(e)
        ok(msg == "critique_domain.yaml: resolved blocker D-001 missing 'resolved_in_round'" and errs and errs[0] == msg,
           f"SE2 the exception message is the first error, as before (other callers read it): {msg!r}")
        rb2 = dict(rb, resolved_in_round=2, resolved_evidence="manuscript.md Methods")
        with open(pth, "w") as fh:
            yaml.safe_dump({"reviewer": "critique-domain", "round": 2, "verdict": "PASS",
                            "structural_mismatch": {"detected": False},
                            "carried_forward": [{"id": "D-001", "prior_round": 1, "still_present": False,
                                                 "notes": "n"}], "blockers": [rb2]}, fh)
        ok(validate_critique(pth, "critique-domain", 2)["blockers"][0]["id"] == "D-001",
           "SE3 the repaired file passes")
        with open(pth, "w") as fh:
            yaml.safe_dump({"reviewer": "critique-domain", "round": 2, "verdict": "REVISE",
                            "structural_mismatch": "no", "blockers": "none"}, fh)
        with open(os.path.join(d, "critique_redteam.yaml"), "w") as fh:
            yaml.safe_dump({"reviewer": "critique-redteam", "round": 2, "verdict": ["PASS"],
                            "structural_mismatch": {"detected": False},
                            "carried_forward": [{"id": ["R-001"], "prior_round": 1, "still_present": True,
                                                 "notes": "n"}],
                            "blockers": [dict(rb, id=["R-001"], severity=["HIGH"], category={"a": 1},
                                              target_stage=["WRITE"], resolved=False)]}, fh)
        try:
            validate_critique(os.path.join(d, "critique_redteam.yaml"), "critique-redteam", 2)
            errs5 = []
        except SchemaError as e:
            errs5 = list(e.errors)
        ok(len(errs5) >= 5 and all(e.startswith("critique_redteam.yaml: ") for e in errs5),
           f"SE5 unhashable values (lists, mappings) are schema errors, not a crash: {errs5}")
        nb = {k: v for k, v in rb.items() if k != "claim"}
        with open(os.path.join(d, "critique_redteam.yaml"), "w") as fh:
            yaml.safe_dump({"reviewer": "critique-redteam", "round": 2, "verdict": "PASS",
                            "structural_mismatch": {"detected": False}, "carried_forward": [],
                            "blockers": [dict(nb, id="R-001", severity=["HIGH"])]}, fh)
        try:
            validate_critique(os.path.join(d, "critique_redteam.yaml"), "critique-redteam", 2)
            errs6 = []
        except SchemaError as e:
            errs6 = list(e.errors)
        ok(any("missing required key 'claim'" in e for e in errs6) and any("R-001 severity" in e for e in errs6),
           f"SE6 a missing key followed by an unhashable value: both are schema errors, no crash: {errs6}")
        try:
            validate_critique(pth, "critique-domain", 2)
            errs = []
        except SchemaError as e:
            errs = list(getattr(e, "errors", [str(e)]))
        ok(len(errs) >= 3 and any("structural_mismatch" in e for e in errs)
           and any("blockers must be a list" in e for e in errs) and any("carried_forward" in e for e in errs),
           f"SE4 a malformed file fails closed with each problem named, without crashing: {errs}")

    # SE7 (Codex review round 2): YAML PyYAML cannot construct is a refused file, not a crash.
    with tempfile.TemporaryDirectory() as d:
        bad = []
        for i, body in enumerate(("x: 2026-99-99\n", "x: !!bool nonsense\n", "x: !!timestamp nope\n")):
            pth = os.path.join(d, f"critique_domain_{i}.yaml")
            with open(pth, "w") as fh:
                fh.write("reviewer: critique-domain\nround: 1\n" + body)
            try:
                validate_critique(pth, "critique-domain", 1)
                bad.append((i, "accepted"))
            except SchemaError:
                pass
            except Exception as e:                            # noqa: BLE001 - the test is that it does not raise
                bad.append((i, repr(e)))
        ok(not bad, f"SE7 unconstructible YAML is a SchemaError: {bad}")

    # --- CK1-CK6 (owner 2026-11-23): the structural checklist of the domain and red-team critics ---
    with tempfile.TemporaryDirectory() as d:
        CLm = _checklist_module()
        pth = os.path.join(d, "critique_domain.yaml")
        ob = {"id": "D-001", "severity": "MEDIUM", "category": "METHODS", "target_stage": "ANALYZE",
              "first_seen_round": 1, "claim": "c", "evidence": "e", "fix_requires": "f", "resolved": False}
        mine = {i: {"answer": "not_applicable", "reason": "no burn-in"} for i in CLm.ITEMS}

        def errs_of(doc, reviewer="critique-domain", cl=False):
            with open(pth, "w") as fh:
                yaml.safe_dump(dict({"reviewer": reviewer, "round": 1, "verdict": "REVISE",
                                     "structural_mismatch": {"detected": False}, "carried_forward": [],
                                     "blockers": [dict(ob, id=PREFIXES[reviewer] + "001")]}, **doc), fh)
            try:
                validate_critique(pth, reviewer, 1, checklist=cl)
                return []
            except SchemaError as e:
                return list(e.errors)
        ok(errs_of({"checklist": mine}, cl=True) == [], "CK1 a domain critique answering every item is accepted")
        e1 = errs_of({}, cl=True)
        ok(len(e1) == 1 and "missing required key 'checklist'" in e1[0],
           f"CK2 a v2 domain critique without a checklist fails closed: {e1}")
        part = {k: v for k, v in mine.items() if k != "surveillance_ranking"}
        e3 = errs_of({"checklist": dict(part, burn_in_drift={"answer": "not_applicable", "reason": ""})}, cl=True)
        ok(len(e3) == 2 and any("surveillance_ranking" in x and "missing" in x for x in e3)
           and any("burn_in_drift needs a non-empty reason" in x for x in e3),
           f"CK3 a missing item and a not_applicable without a reason are schema errors, listed together: {e3}")
        fin = dict(mine, burn_in_drift={"answer": "finding", "reason": "drift", "blockers": ["D-001"]})
        ok(errs_of({"checklist": fin}, cl=True) == [], "CK4 a finding names an open blocker of the file")
        e4 = errs_of({"checklist": {k: v for k, v in fin.items() if k != "comparator_symmetry"}}, cl=True)
        ok(len(e4) == 1 and "comparator_symmetry" in e4[0],
           f"CK4b no item is computed by the harness: the critic answers comparator_symmetry too: {e4}")
        ok(errs_of({}) == [] and errs_of({}, reviewer="critique-presentation", cl=True) == [],
           "CK5 the legacy gate (checklist=False) and the presentation critic need no checklist")
        e6 = errs_of({"checklist": dict(mine, burn_in_drift={"answer": "finding", "reason": "r", "blockers": ["D-7"]})},
                     cl=True)
        ok(len(e6) == 1 and "[CK-B-burn_in_drift-1]" in e6[0] and "D-7" not in e6[0],
           f"CK6 a finding naming no blocker of the file is refused, by marker (critic text is never quoted): {e6}")
        # CK7-CK8 (Codex r3 of #366): a duplicate checklist key, and a value the parser cannot construct, are schema
        # errors in fixed text; the legacy gate keeps its old reading
        base_y = ("reviewer: critique-domain\nround: 1\nverdict: PASS\nstructural_mismatch: {detected: false}\n"
                  "carried_forward: []\nblockers: []\nchecklist:\n"
                  + "".join(f"  {i}: {{answer: pass, reason: r}}\n" for i in CLm.ITEMS))
        for tag, extra in (("CK7", "  burn_in_drift: {answer: finding, reason: r}\n"),
                           ("CK8", "  surveillance_ranking: {answer: pass, reason: !!bool observation_SECRET}\n")):
            body = base_y if tag == "CK8" else base_y + extra
            if tag == "CK8":
                body = base_y.replace("  surveillance_ranking: {answer: pass, reason: r}\n", extra)
            open(pth, "w").write(body)
            try:
                validate_critique(pth, "critique-domain", 1, checklist=True)
                ek = []
            except SchemaError as e:
                ek = list(e.errors)
            ok(len(ek) == 1 and "[CK-YAML]" in ek[0] and "observation_SECRET" not in ek[0],
               f"{tag} a {'duplicate key' if tag == 'CK7' else 'value the parser cannot construct'} is one schema "
               f"error in fixed text: {ek}")

    # --- Phase 39 α: presentation polish waits for the first draft ---
    with tempfile.TemporaryDirectory() as d:
        pres = {"kind": "critique_blocker", "id": "P-003", "category": "PRESENTATION", "target_stage": "ANALYZE",
                "severity": "HIGH", "scope_declarable": True}
        hard = {**pres, "id": "P-004", "category": "HARD_BLOCKER"}
        r = _defer_write_findings({"unresolved_high": [pres, hard]}, d, 8, 3)
        ok(r["after_write"] == ["P-003"] and [b["id"] for b in r["unresolved_high"]] == ["P-004"],
           f"PP1 a PRESENTATION finding waits for the draft; a HARD_BLOCKER from the same critic still blocks: {r}")
        open(os.path.join(d, "manuscript.md"), "w").write("draft")
        r = _defer_write_findings({"unresolved_high": [pres, hard]}, d, 8, 3)
        ok(len(r["unresolved_high"]) == 2, "PP2 once a draft exists presentation findings block as before")
    # --- Phase 39: early DECLARE_SCOPE when only long-declared gate findings remain ---
    bd = {"kind": "benchmark_drift", "id": "RIG-001", "severity": "HIGH", "scope_declarable": True}
    sa = {"kind": "sensitivity_analysis_unstable", "id": "RIG-002", "severity": "HIGH", "scope_declarable": True}
    cb = {"kind": "critique_blocker", "id": "D-014", "severity": "HIGH", "scope_declarable": True}
    both = ["benchmark_drift", "sensitivity_analysis_unstable"]

    def act(items, now, prev, cur=5):
        return _recompute_action({"unresolved_high": items, "declared": now, "declared_prev": prev}, 8, cur)
    r = act([bd, sa], both, both)
    ok(r["action"] == "DECLARE_SCOPE" and r["rule_matched"] == "3a",
       f"EF1 only gate findings declared at this gate and the last: finish now ({r['action']})")
    ok(act([bd, sa], both, ["benchmark_drift"])["action"] == "PATCH_OR_RETHINK",
       "EF2 a declaration new at this gate waits one round")
    ok(act([bd, sa, cb], both + ["D-014"], both + ["D-014"])["action"] == "PATCH_OR_RETHINK",
       "EF3 a critic's blocker is never declared away early")
    ok(act([bd, {**sa, "kind": "P-007_persistent"}], both + ["P-007_persistent"],
           both + ["P-007_persistent"])["action"] == "PATCH_OR_RETHINK",
       "EF4 a critic finding escalated for persistence is not declared away early")
    ok(act([bd, {**sa, "scope_declarable": False}], both, both)["action"] == "PATCH_OR_RETHINK",
       "EF5 a non-declarable finding blocks the early finish")
    ok(act([], both, both)["action"] == "ACCEPT", "EF6 nothing outstanding is still ACCEPT")
    for k in ("done_when_regressed", "claim_overclaimed", "done_when_unmet"):
        ok(act([bd, {**sa, "kind": k}], both + [k], both + [k])["action"] == "PATCH_OR_RETHINK",
           f"EF10 (Codex) a critic finding carried as {k} is not declared away early")
    ok(_recompute_action({"unresolved_high": [bd], "accept_blockers": [{"kind": "required_artifact_missing",
                          "scope_declarable": True}], "declared": both + ["required_artifact_missing"],
                          "declared_prev": both + ["required_artifact_missing"]}, 8, 5)["action"] == "PATCH_OR_RETHINK",
       "EF11 (Codex) no early finish while a required artifact is missing")
    ok(act([{**bd, "kind": "benchmark_computed_missing", "id": "RIG-001"}], ["RIG-001"], ["RIG-001"])["action"]
       == "PATCH_OR_RETHINK", "EF12 (Codex) renumbered gate ids never match a declaration; only kinds do")
    # Phase 47 ε: attempt 8 rounds 6-7 (all five open HIGHs declared at both gates).
    wp = {"kind": "weakly_identified_parameters_persistent", "id": "RIG-003", "severity": "HIGH"}
    wbase = ["weakly_identified_parameters", "weakly_identified_parameters_persistent"]
    r = act([bd, wp], both + wbase, both + wbase)
    ok(r["action"] == "DECLARE_SCOPE" and r["rule_matched"] == "3a",
       f"EF13 a gate kind's persistence escalation declared with its base kind: finish now ({r['action']})")
    ok(act([bd, wp], both + wbase[1:], both + wbase[1:])["action"] == "PATCH_OR_RETHINK",
       "EF13b ...not when only the escalation is declared")
    st = {"kind": "finding_stalled_without_diagnosis", "id": "RIG-004", "severity": "HIGH", "scope_declarable": True}
    ok(act([bd, st], both + [st["kind"]], both + [st["kind"]])["action"] == "PATCH_OR_RETHINK",
       "EF14 a stalled finding without a diagnosis is never declared away early")
    with tempfile.TemporaryDirectory() as d:
        _append_validator_history(d, 2, {"action": "PATCH_OR_RETHINK", "rigor_violations": [
            {"kind": "benchmark_drift", "severity": "HIGH", "stall_key": "B9"},
            {"kind": "scope_declaration_cap_raised", "severity": "LOW"}]}, False)
        rec = json.loads(open(os.path.join(d, _VALIDATOR_HISTORY_FILE)).read())
        ok(rec["findings"][0].get("key") == "B9" and "key" not in rec["findings"][1],
           f"SW2 the history records each tracked finding's key: {rec['findings']}")
    ok(act([bd, sa], both, both, cur=8)["action"] == "DECLARE_SCOPE" and act([bd, sa], both, both, cur=8)["rule_matched"] == 3,
       "EF7 in the last round the existing rule 3 applies")
    # Phase 49 α: an untested declaration blocks rule 3a, and rule 3 at exhaustion.
    ut = {"kind": "scope_declaration_untested", "id": "RIG-009", "severity": "HIGH",
          "scope_declarable": not is_non_declarable("scope_declaration_untested")}
    ok(act([bd, sa, ut], both, both)["action"] == "PATCH_OR_RETHINK"
       and act([bd, sa, ut], both, both, cur=8)["action"] == "RUN_FAILED",
       "DT-V1 an untested declaration: no early DECLARE_SCOPE, RUN_FAILED at exhaustion")
    # SO-V3 (Phase 49 β): a missing decision analysis (an accept blocker) is the owner's at exhaustion.
    _ra = {"kind": "required_artifact_missing", "severity": "HIGH", "accept_blocker": True, "scope_declarable": True,
           "artifact": "sensitivity_analysis", "claim": "absent", "owner_signoff": True,
           "owner_item": {"finding": "required_artifact_missing:sensitivity_analysis"}}
    _d3 = _incorporate_rigor_violations({"unresolved_high": [], "declared": [], "declared_prev": []}, [_ra], 8, 8)
    ok(_d3["action"] == "AWAIT_OWNER" and _d3["accept_blockers"][0].get("owner_signoff"),
       f"SO-V3 a missing required analysis at exhaustion waits for the owner: {_d3['action']}")
    # Phase 49 β: a DECLARE_SCOPE that relies on a decision-bearing finding waits for the owner.
    bdo = dict(bd, owner_signoff=True, owner_item={"finding": "benchmark_drift:B15"})
    ok(act([bdo, sa], both, both)["action"] == "AWAIT_OWNER" and act([bdo, sa], both, both)["rule_matched"] == "3ao"
       and act([bdo, sa], both, both, cur=8)["action"] == "AWAIT_OWNER",
       "SO-V1 rule 3a / rule 3 with an owner-marked finding -> AWAIT_OWNER")
    ok(act([bdo, sa, ut], both, both)["action"] == "PATCH_OR_RETHINK"
       and act([bdo, sa, ut], both, both, cur=8)["action"] == "RUN_FAILED",
       "SO-V2 the owner is asked only when the run would otherwise declare scope")
    # OR-V1..OR-V7 (harness baseline hb2 fix 1; owner decision (b), 2026-11-23): a HIGH whose resolution is the
    # owner's (`owner_resolution`: a measured decision test that changes the plan) is asked at the first gate, not at
    # the round cap; once accepted it no longer blocks ACCEPT; declined or unanswered it never accepts.
    _dt = {"kind": "decision_test_changes_plan", "severity": "HIGH", "stage": "ANALYZE", "key": "DT1",
           "claim": "DT1 changes the plan", "scope_declarable": True}
    _pend = dict(_dt, owner_signoff=True, owner_resolution=True,
                 owner_item={"finding": "decision_test_changes_plan:DT1", "fingerprint": "fp1"})
    _acc = dict(_dt, owner_resolution=True, owner_signed={"by": "owner", "at": "t", "fingerprint": "fp1"})
    _other = {"kind": "benchmark_drift", "severity": "HIGH", "stage": "MODEL", "key": "B1", "claim": "drift",
              "scope_declarable": True}
    _e = lambda: {"unresolved_high": [], "declared": [], "declared_prev": []}  # noqa: E731
    _o1 = _incorporate_rigor_violations(_e(), [dict(_pend)], 3, 1)
    ok(_o1["action"] == "AWAIT_OWNER" and _o1["rule_matched"] == "2o",
       f"OR-V1 an unanswered owner-resolution HIGH goes to the owner at the first gate ({_o1['action']}, "
       f"{_o1['rule_matched']})")
    _o2 = _incorporate_rigor_violations(_e(), [dict(_pend), dict(_other)], 3, 1)
    ok(_o2["action"] == "AWAIT_OWNER" and _o2["rule_matched"] == "2o",
       f"OR-V2 it is asked first, beside a HIGH a fix may close ({_o2['action']})")
    _o3 = _incorporate_rigor_violations(_e(), [dict(_acc)], 3, 1)
    _o3c = _incorporate_rigor_violations(_e(), [dict(_acc)], 3, 3)
    ok(_o3["action"] == "ACCEPT" and _o3c["action"] == "ACCEPT" and not _o3["unresolved_high"]
       and [b.get("kind") for b in _o3.get("owner_accepted_high") or []] == ["decision_test_changes_plan"]
       and (_o3["owner_accepted_high"][0].get("owner_signed") or {}).get("by") == "owner",
       f"OR-V3 an owner-accepted plan-changing test does not block ACCEPT, at round 1 or at the cap; the gate lists it "
       f"as accepted ({_o3['action']}, {_o3c['action']}, {_o3.get('owner_accepted_high')})")
    _o4 = _incorporate_rigor_violations(_e(), [dict(_acc), dict(_other)], 3, 1)
    _o4c = _incorporate_rigor_violations(_e(), [dict(_acc), dict(_other)], 3, 3)
    ok(_o4["action"] == "PATCH_OR_RETHINK" and _o4c["action"] == "DECLARE_SCOPE"
       and [b.get("kind") for b in _o4c["unresolved_high"]] == ["benchmark_drift"],
       f"OR-V4 the other HIGHs keep their rules ({_o4['action']}, {_o4c['action']})")
    _pol = dict(_other, owner_signed={"by": "owner", "at": "t", "fingerprint": "fp2"})
    _o5 = _incorporate_rigor_violations(_e(), [dict(_pol)], 3, 3)
    _o5p = _incorporate_rigor_violations(_e(), [dict(_other, owner_signoff=True, owner_item={"finding": "b"})], 3, 1)
    ok(_o5["action"] == "DECLARE_SCOPE" and _o5p["action"] == "PATCH_OR_RETHINK",
       f"OR-V5 a policy-mapped owner item (no owner_resolution) is unchanged: declared once accepted, asked only at "
       f"the cap ({_o5['action']}, {_o5p['action']})")
    _dec = dict(_dt, owner_resolution=True, owner_declined=True, scope_declarable=False)
    _o6 = _incorporate_rigor_violations(_e(), [dict(_dec)], 3, 3)
    _o6u = _incorporate_rigor_violations(_e(), [dict(_pend)], 3, 3)
    _o6s = _incorporate_rigor_violations(_e(), [dict(_acc, scope_declarable=False)], 3, 3)
    ok(_o6["action"] == "RUN_FAILED" and _o6u["action"] == "AWAIT_OWNER" and _o6s["action"] == "RUN_FAILED",
       f"OR-V6 a declined, unanswered or non-declarable plan-changing test never accepts ({_o6['action']}, "
       f"{_o6u['action']}, {_o6s['action']})")
    _o7 = _incorporate_rigor_violations(_incorporate_rigor_violations(_e(), [dict(_acc)], 3, 1), [dict(_acc)], 3, 1)
    ok(_o7["action"] == "ACCEPT" and len(_o7.get("owner_accepted_high") or []) == 1,
       f"OR-V7 idempotent: folding the same accepted finding twice lists it once ({_o7.get('owner_accepted_high')})")
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, _VALIDATOR_HISTORY_FILE), "w") as f:
            f.write(json.dumps({"round": 3, "action": "PATCH_OR_RETHINK", "declared": ["a"]}) + "\n")
            f.write(json.dumps({"round": 4, "action": "PATCH_OR_RETHINK", "declared": ["a", "b"]}) + "\n")
            f.write(json.dumps({"round": 4, "post_write": True, "declared": []}) + "\n")
            f.write(json.dumps({"round": 5, "action": "PATCH_OR_RETHINK", "declared": ["z"]}) + "\n")
        ok(_previous_declared(d, 5) == ["a", "b"], f"EF8 the last in-round gate before this round: {_previous_declared(d, 5)}")
        yaml.safe_dump({"declarations": [
            {"id": "SD-1", "blocker_id": "benchmark_drift", "why_unresolved": "locked target"},
            {"id": "SD-2", "blocker_id": "sensitivity_analysis_unstable"},
            {"id": "SD-3", "claim": "mentions benchmark_computed_missing and R-021 in prose only"}]},
            open(os.path.join(d, "scope_declaration.yaml"), "w"))
        ok(_declared_ids(d) == ["benchmark_drift"],
           f"EF9 only structured, justified declarations count: {_declared_ids(d)}")

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
                "first_seen_round": 2, "claim": "eligibility threshold",
                "resolved": True,
            }],
            carried_forward=[{
                "id": "D-002", "prior_round": 2,
                "still_present": False,
                "notes": "fixed by updated 21-unit list",
            }],
        ),
    }
    dec_f3 = decide(crit_f3, max_rounds=5, current_round=3)
    ok("D-002" not in dec_f3.get("blocker_attempts", {}),
       f"F3: resolved blockers should NOT appear in blocker_attempts, "
       f"got {dec_f3.get('blocker_attempts')}")


    # BS2: once the brief signoff exists, untied HIGH leaves unresolved_high
    with tempfile.TemporaryDirectory() as d:
        json.dump({"signed_by": "owner"}, open(os.path.join(d, "decision_brief.signoff.json"), "w"))
        crit = {"critique-redteam": {"blockers": [
            {"id": "R-003", "severity": "HIGH", "target_stage": "MODEL", "category": "METHODS",
             "first_seen_round": 5, "claim": "untied", "resolved": False, "brief_item": "NONE"},
            {"id": "R-004", "severity": "HIGH", "target_stage": "MODEL", "category": "METHODS",
             "first_seen_round": 5, "claim": "tied", "resolved": False, "brief_item": "HB-001"}],
            "carried_forward": [], "structural_mismatch": {"detected": False}}}
        dec = _apply_brief_scope(decide(crit, 8, 5), crit, d, 8, 5)
        ok([b["id"] for b in dec["unresolved_high"]] == ["R-004"] and dec["outside_brief"] == ["R-003"],
           f"BS2: untied HIGH leaves unresolved_high: {dec}")

    # DG1, DG4, DG4b, DG6, DG7: done_when test evaluation and write deferral
    with tempfile.TemporaryDirectory() as d:
        import done_when as _dw
        json.dump({"ci95": [10, 20]}, open(os.path.join(d, "out.json"), "w"))
        test = {"file": "out.json", "key": "ci95", "op": "width_gt", "value": 0}
        crit = {"critique-redteam": {"blockers": [
            {"id": "R-001", "severity": "HIGH", "target_stage": "MODEL", "category": "METHODS",
             "first_seen_round": 2, "claim": "zero CI", "resolved": False, "done_when": test},
            {"id": "R-002", "severity": "HIGH", "target_stage": "MODEL", "category": "METHODS",
             "first_seen_round": 2, "claim": "judged", "resolved": False,
             "done_when": {"judgement": "the critic reads the caveat"}},
            {"id": "R-004", "severity": "HIGH", "target_stage": "ANALYZE", "category": "METHODS",
             "first_seen_round": 2, "claim": "no table", "resolved": False,
             "done_when": {"file": "table.csv", "exists": True}}],
            "carried_forward": [], "structural_mismatch": {"detected": False}}}
        dec, res = _apply_done_when(decide(crit, 8, 3), crit, d, 8, 3)
        ok([b["id"] for b in dec["unresolved_high"]] == ["R-002", "R-004"] and dec["resolved_by_test"] == ["R-001"],
           f"DG1: the passing test resolves R-001, the judged one stays open: {dec['unresolved_high']}")
        json.dump({"ci95": [15, 15]}, open(os.path.join(d, "out.json"), "w"))
        crit["critique-redteam"]["blockers"][0]["resolved"] = True
        dec, res = _apply_done_when(decide(crit, 8, 4), crit, d, 8, 4)
        dec = _incorporate_rigor_violations(dec, _dw.gate_findings(res), 8, 4)
        reg = [b for b in dec["unresolved_high"] if b.get("kind") == "done_when_regressed"]
        ok(len(reg) == 1 and "R-001" in reg[0]["claim"] and "round 3" in reg[0]["claim"]
           and dec["action"] != "ACCEPT", f"DG4: a broken test reopens as a HIGH gate finding: {dec['unresolved_high']}")
        ecrit = {"critique-redteam": {"blockers": [], "carried_forward": [], "structural_mismatch": {"detected": False}}}
        dec7, res7 = _apply_done_when(decide(ecrit, 8, 5), ecrit, d, 8, 5, record=False)
        dec7 = _incorporate_rigor_violations(dec7, _dw.gate_findings(res7), 8, 5)
        ok(dec7["action"] != "ACCEPT" and any(b.get("kind") == "done_when_dropped" and "R-004" in b.get("claim", "")
                                              for b in dec7["unresolved_high"]),
           f"DG4b (review A5): a critique that stops listing its open findings does not reach ACCEPT: "
           f"{dec7['action']}, {[b.get('kind') for b in dec7['unresolved_high']]}")
        bad = _incorporate_rigor_violations(decide(crit, 8, 5), _dw.gate_findings({"error": "KeyError: x"}), 8, 5)
        ok(any(b.get("kind") == "done_when_unavailable" for b in bad["unresolved_high"]) and bad["action"] != "ACCEPT",
           "DG6: a crashed test evaluation is a HIGH finding (fail closed)")
        wcrit = {"critique-presentation": {"blockers": [
            {"id": "P-001", "severity": "HIGH", "target_stage": "WRITE", "category": "PRESENTATION",
             "first_seen_round": 1, "claim": "no captions", "resolved": False}],
            "carried_forward": [], "structural_mismatch": {"detected": False}}}
        pre = _defer_write_findings(decide(wcrit, 8, 2), d, 8, 2)
        open(os.path.join(d, "manuscript.md"), "w").write("draft")
        post = _defer_write_findings(decide(wcrit, 8, 2), d, 8, 2)
        ok(pre["action"] == "ACCEPT" and pre["after_write"] == ["P-001"]
           and [b["id"] for b in post["unresolved_high"]] == ["P-001"],
           f"DG7: a HIGH write-up finding waits for the first draft, then blocks: {pre['action']} {post['action']}")

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


    # --- Optional cross-model critique (XM1, XM1b, XM4, XM5) ---
    with tempfile.TemporaryDirectory() as _root:
        with open(os.path.join(_root, "metadata.json"), "w") as f:
            json.dump({"question": "Estimate the impact of an intervention."}, f)
        ok(_load_optional_critique(_root, "critique-crossmodel", 2) == (None, []),
           "XM1: absent cross-model critique -> no finding")
        with open(os.path.join(_root, "metadata.json"), "w") as f:
            json.dump({"question": "Estimate the impact of an intervention.", "cross_model": True}, f)
        _, _v = _load_optional_critique(_root, "critique-crossmodel", 2)
        ok([x["kind"] for x in _v] == ["cross_model_critic_missing"]
           and _v[0]["severity"] == "HIGH",
           f"XM1b: requested but missing cross-model critique -> HIGH, got {_v}")
        with open(os.path.join(_root, "critique_crossmodel.yaml"), "w") as f:
            yaml.safe_dump({
                "reviewer": "critique-crossmodel", "round": 2,
                "availability_status": "UNAVAILABLE", "availability_history": [],
                "verdict": "UNAVAILABLE", "unavailable_reason": "codex CLI unavailable",
                "structural_mismatch": {"detected": False},
                "blockers": [], "carried_forward": [],
            }, f)
        _, v4 = _load_optional_critique(_root, "critique-crossmodel", 2)
        ok([(v["kind"], v["severity"]) for v in v4]
           == [("cross_model_critic_unavailable", "MEDIUM")],
           f"XM4: one unavailable round -> MEDIUM; got {v4}")
        with open(os.path.join(_root, "critique_crossmodel.yaml"), "w") as f:
            yaml.safe_dump({
                "reviewer": "critique-crossmodel", "round": 2,
                "availability_status": "UNAVAILABLE",
                "availability_history": [
                    {"round": 0, "status": "UNAVAILABLE"},
                    {"round": 1, "status": "UNAVAILABLE"},
                ],
                "verdict": "UNAVAILABLE", "unavailable_reason": "codex CLI unavailable",
                "structural_mismatch": {"detected": False},
                "blockers": [], "carried_forward": [],
            }, f)
        _, v5 = _load_optional_critique(_root, "critique-crossmodel", 2)
        ok([(v["kind"], v["severity"]) for v in v5]
           == [("cross_model_critic_unavailable", "HIGH")],
           f"XM5: 3 consecutive unavailable rounds -> HIGH; got {v5}")

    # --- Summary ---
    if failures:
        print(f"FAIL: {len(failures)} case(s)", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    print("OK: all self-test cases passed.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_run_self_test())
    print(__doc__)
    sys.exit(2)
