"""The state table: STATES, EDGES and the gate's routing (route_gate).

Moved verbatim from agents/stage_machine.py by scripts/refactor/split_stage_machine.py.
"""
from __future__ import annotations



STATES = ("SPEC", "EVIDENCE", "SIGN", "ENGINE_CHOICE", "PREREGISTER", "CONFIGURE", "ANALYZE", "SCORE", "CRITIQUE",
          "GATE", "FIX_LOOP", "OWNER_DECISION", "WRITE", "PRESENTATION", "FINAL")
# The state table: every transition the machine may take. A transition outside it is a bug
# (raised, never taken).
EDGES = {
    # v2 autonomy 1B-ii (D1): EVIDENCE drafts the evidence dossier before SIGN, so the signer sees it (a run signed
    # under the earlier order, SIGN before EVIDENCE, goes back to SIGN, which checks its signature); SIGN -> EVIDENCE
    # | SPEC is the lead's repair of a refused sign-off (at most MAX_SIGN_REPAIRS), only before any signature.
    "SPEC": {"EVIDENCE", "SPEC", "FINAL"},
    "EVIDENCE": {"SIGN", "SPEC", "FINAL"},
    "SIGN": {"ENGINE_CHOICE", "EVIDENCE", "SPEC", "SIGN", "FINAL"},
    "ENGINE_CHOICE": {"PREREGISTER", "CONFIGURE", "FINAL"},
    # The world model (docs/design/v2c-orchestration.md §14): beliefs anchored before the engine batch, scored after
    # the analysis. Fail soft: neither state can end the run or change what the analysis computes.
    "PREREGISTER": {"CONFIGURE", "FINAL"},
    "CONFIGURE": {"ANALYZE", "FINAL"},
    # A refused calibration goes back to SPEC (its revision), at most MAX_CALIBRATION_REFUSALS times
    "ANALYZE": {"SCORE", "CRITIQUE", "SPEC", "FINAL"},
    "SCORE": {"CRITIQUE", "FINAL"},
    "CRITIQUE": {"GATE", "WRITE", "FINAL"},
    "GATE": {"WRITE", "FIX_LOOP", "SPEC", "ENGINE_CHOICE", "OWNER_DECISION", "CRITIQUE", "FINAL"},
    "FIX_LOOP": {"CRITIQUE", "SPEC", "ENGINE_CHOICE", "WRITE", "OWNER_DECISION", "FINAL"},
    "OWNER_DECISION": {"GATE", "OWNER_DECISION", "FINAL"},
    # hb1 cause G: a draft revised for write-up findings as a new round is re-reviewed (CRITIQUE) before the gate
    "WRITE": {"PRESENTATION", "GATE", "FINAL"},
    "PRESENTATION": {"GATE", "CRITIQUE", "FINAL"},
    "FINAL": set(),
}
LLM_STATES = ("SPEC", "ENGINE_CHOICE", "CRITIQUE", "FIX_LOOP", "PRESENTATION")


# ------------------------------------------------------------------ gate routing (pure)
def rethink_target(decision: dict) -> str:
    """RETHINK_STRUCTURAL back-edge: SPEC when a structural blocker targets PLAN (the decision
    spec is wrong), else ENGINE_CHOICE (the engine or its configuration cannot represent it)."""
    for b in decision.get("unresolved_high") or []:
        if isinstance(b, dict) and b.get("category") == "STRUCTURAL" and b.get("target_stage") == "PLAN":
            return "SPEC"
    return "ENGINE_CHOICE"


def route_gate(decision: dict | None, post_write: bool) -> tuple[str, str | None, str | None]:
    """(next state, terminal status or None, write mode or None) for a gate decision.

    The rules of lead prompt STAGE 7 Step 3 and STAGE 8.9, as code."""
    if not isinstance(decision, dict):
        return "FINAL", "completed_gate_error", None
    if decision.get("schema_errors"):
        return "CRITIQUE", None, None
    a = decision.get("action")
    if a == "ACCEPT":
        return ("FINAL", "completed", None) if post_write else ("WRITE", None, "accepted")
    if a == "DECLARE_SCOPE":
        return ("FINAL", "completed_scope_declared", None) if post_write else ("WRITE", None, "declared")
    if a == "RUN_FAILED":
        owner_declined = any(isinstance(u, dict) and u.get("kind") == "scope_declaration_declined"
                             for u in decision.get("unresolved_high") or [])
        status = "completed_scope_declined" if owner_declined else "run_failed"
        return ("FINAL", status, None) if post_write else ("WRITE", None, "not_accepted")
    if a == "PATCH_OR_RETHINK":
        return "FIX_LOOP", None, None
    if a == "RETHINK_STRUCTURAL":
        return rethink_target(decision), None, None
    if a == "AWAIT_OWNER":
        return "OWNER_DECISION", None, None
    return "FINAL", "completed_gate_error", None


POST_CHOICE_STATES = ("PREREGISTER", "CONFIGURE", "ANALYZE", "SCORE", "CRITIQUE", "GATE", "FIX_LOOP", "OWNER_DECISION",
                      "WRITE", "PRESENTATION")
