#!/usr/bin/env python3
"""v2 C (S4): the code-owned run state machine.

    python3 agents/stage_machine.py status <run_dir>
    python3 agents/stage_machine.py sign <run_dir> --by owner [--note "..."]      # the owner signs the spec
    python3 agents/stage_machine.py --self-test

    main.py --v2 "<question>" ...     # a real run: agents/v2_sdk_tasks.py fills the LLM tasks

Design: docs/design/v2c-orchestration.md. Code owns the flow: the state, every
transition and every number. LLMs do bounded tasks (agents/v2_tasks.py). The state is
persisted in run_state.json after every transition, so a run resumes where it stopped.

States: SPEC -> SIGN -> EVIDENCE (stub) -> ENGINE_CHOICE -> CONFIGURE -> ANALYZE ->
CRITIQUE -> GATE -> FIX_LOOP | WRITE | OWNER_DECISION | back-edges -> ... -> WRITE ->
PRESENTATION -> GATE (post-write) -> FINAL.

The gate's rules are not reimplemented. GATE calls today's validator: the CLI exactly
as main.py's final gate does (`gate_mode: legacy`, for runs with today's artifacts), or
the same rule functions in-process on the v2 findings (`gate_mode: v2`):
validate_critique_yaml.decide, _apply_done_when, _defer_write_findings and
_incorporate_rigor_violations, which end in _recompute_action. `route_gate` maps the
action to the next state, following lead prompt STAGE 7 Step 3 and STAGE 8.9.
"""
# split_stage_machine: self-tests moved to tests/stage_machine/
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
for _p in (REPO, os.path.join(REPO, "scripts"), HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import analysis_backend as AB  # noqa: E402
import findings_ledger as FL  # noqa: E402
import fix_loop as FX  # noqa: E402

STATE_FILE = "run_state.json"
SIGNOFF_FILE = "decision_spec.signoff.json"
SCOPE_SIGNOFF = "scope_signoff.json"      # Phase 49 β (PR #85)
SIGNOFF_SHEET = "decision_spec.signoff_sheet.md"   # v2-derived-inputs: what signing accepts beyond the spec's text
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
CRITICS = ("critique-domain", "critique-redteam")
# The owner signs the whole decision_spec.yaml (its sha256). Fix tasks never edit it: they edit
# analysis_settings.yaml, which holds only computational leaves (analysis_backend.SETTINGS_PATHS;
# workstream B's schema.SETTINGS_PATHS), and the engine the harness chose is in engine_choice.json.
PROTECTED_BY_FIX = (AB.SPEC_FILE, AB.SETTINGS_FILE)
TASK_CAP_USD = 3.0            # one bounded task's allowance, at most what the run has left
READBACK_CAP_USD = 0.25       # one crash read-back (resume for one tool-less turn), at most what is left
# F6 run 3: a draft_spec started with a $0.13 allowance failed at once. A paid task is not started below this
# allowance: the run pauses for the owner (paused:awaiting_budget, resumable with a higher --max-budget-usd).
MIN_TASK_ALLOWANCE_USD = 0.50
AWAITING_BUDGET = "awaiting_budget"
OWNER_REQUEST = "scope_signoff_request.json"   # scripts/scope_signoff.py REQUEST: `show` / `sign` read it


class AwaitingBudget(Exception):
    """Raised by Machine._task when the next paid task's allowance is below MIN_TASK_ALLOWANCE_USD; step() pauses."""
# Findings whose resolution bears on the primary decision: Phase 49 β's owner rule decides
# whether they may be declared. Without that rule they are not declarable (fail closed).
DECISION_BEARING_KINDS = ("decision_test_changes_plan",)
MAX_SPEC_TRIES = 3
MAX_SCHEMA_REPAIRS = 2
MAX_SIGN_REPAIRS = 2          # a refused lead sign-off goes back to EVIDENCE or SPEC at most this often, then waits
MAX_CALIBRATION_REFUSALS = 2  # a refused calibration goes back to SPEC at most this often, then the run ends run_failed
CALIBRATION_REFUSED = "calibration_refused"      # its findings-ledger kind and id


@dataclass
class RunState:
    question: str
    state: str = "SPEC"
    round: int = 1
    max_rounds: int = 3
    gate_mode: str = "v2"                 # v2 | legacy
    post_write: bool = False              # the next GATE runs in post-write mode
    round_pending: bool = False           # the next CRITIQUE starts a new round
    write_mode: str | None = None         # accepted | declared | not_accepted
    signoff: str = "owner"                # owner | lead | none
    engine: str | None = None
    budget_usd: float | None = None
    spent_usd: float = 0.0
    status: str | None = None
    forced_status: str | None = None      # budget_exhausted: set when the budget, not the gate, ends the run
    paused: str | None = None
    tries: dict = field(default_factory=dict)
    task_cap_usd: float = TASK_CAP_USD
    # v2's own gate rules on top of today's (structured done_when as schema, open MEDIUMs fixed or
    # declared, never accepted). False only to replay stored v1 gates (scripts/golden_traces.py).
    v2_rules: bool = True
    # docs/design/v2-derived-inputs.md: the spec this machine drafts reads derived or staged data only, made by
    # derivations the harness runs in a sandbox (init_run sets it for every new run; False for older states).
    derived_inputs: bool = False
    # The development flag (owner, 2026-11-01): where there is no sandbox (macOS) derivations run unsandboxed, the
    # record says so, and the gate holds a HIGH non-declarable finding: such a run cannot be accepted.
    derive_unsandboxed: bool = False
    # Every paid task: key -> {task, status started|completed, allowance_usd, usd, session_id,
    # started, completed, ok, output, note}. Written before and after each call, so a resumed run
    # never repeats a completed task or loses its charge, and charges an interrupted one its allowance.
    journal: dict = field(default_factory=dict)
    # §14: the world-model stages run when true (init_run sets it for every new run; False for older states), and
    # what they recorded: {status, decision_id, anchor, preregistered, scored, counts}.
    world_model_stages: bool = False
    world_model: dict = field(default_factory=dict)
    last_gate: dict = field(default_factory=dict)
    history: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_state(run_dir: str) -> RunState | None:
    p = os.path.join(run_dir, STATE_FILE)
    if not os.path.exists(p):
        return None
    with open(p) as f:
        d = json.load(f)
    st = RunState(**{k: v for k, v in d.items() if k in RunState.__dataclass_fields__})
    if st.state not in STATES:
        raise ValueError(f"{STATE_FILE}: unknown state {st.state!r}")   # fail closed
    return st


def _withhold_report(run_dir: str) -> None:
    """A report an earlier gate rendered is not delivered under a failed status: a regular report.md is moved to
    report_withheld.md; a link at that name is removed (never followed)."""
    p = os.path.join(run_dir, "report.md")
    if os.path.islink(p):
        from lib.safe_io import remove_nofollow
        remove_nofollow(run_dir, "report.md")
    elif os.path.isfile(p):
        _quarantine_report(run_dir)


def _quarantine_report(run_dir: str) -> None:
    """F6 S1-7: move report.md to report_withheld.md through lib.safe_io (no link followed at either name;
    a linked report.md is refused, a link at the destination is replaced)."""
    from lib.safe_io import rename_nofollow
    rename_nofollow(run_dir, "report.md", "report_withheld.md")


def _put(run_dir: str, rel: str, text: str) -> None:
    """A harness file in the run dir, published atomically with no link followed (lib.safe_io; review round 4)."""
    from lib.safe_io import write_nofollow
    write_nofollow(run_dir, rel.replace(os.sep, "/"), text.encode("utf-8"), atomic=True)


def _state_bytes(st: RunState) -> bytes:
    """run_state.json's bytes for `st` (a lead hand-off binds its child to them before they are saved: Codex r3)."""
    return json.dumps(st.to_dict(), indent=1, default=str).encode()


def save_state(run_dir: str, st: RunState) -> None:
    """run_state.json, published atomically with no link followed (lib.safe_io; review round 4, P1-2)."""
    from lib.safe_io import write_nofollow
    write_nofollow(run_dir, STATE_FILE, _state_bytes(st), atomic=True)


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


# ------------------------------------------------------------------ gates
def legacy_gate(run_dir: str, round_n: int, max_rounds: int, post_write: bool, history: bool = True,
                timeout_s: int = 1800) -> dict | None:
    """Today's STAGE 7 validator CLI, with the final gate's exact flags."""
    from agents.run_finish import gate_command
    cmd = gate_command(run_dir, max_rounds, round_n, post_write, history=history)
    try:
        p = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, timeout=timeout_s)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"action": None, "error": f"{type(e).__name__}: {e}"}
    out = p.stdout.strip()
    try:
        d = json.loads(out[out.index("{"):])
    except ValueError:
        return {"action": None, "error": (p.stderr.strip().splitlines() or ["no JSON"])[-1][:400],
                "exit_code": p.returncode}
    d["exit_code"] = p.returncode
    return d


def spec_signoff_findings(run_dir: str, signoff: str, backend=None) -> list[dict]:
    """decision_spec.yaml must not change after sign-off (non-declarable)."""
    if signoff == "none":
        return []
    rec = _signoff(run_dir)
    if backend is not None and getattr(backend, "sign", None):
        st = backend.signoff_status(run_dir)
        rec = {"spec_sha256": signed_hash(run_dir)} if st == "signed" else (None if st != "changed" else {"spec_sha256": "x"})
    if rec is None:
        return [{"kind": "spec_unsigned", "severity": "HIGH", "stage": "PLAN", "target_stage": "PLAN", "key": "spec",
                 "scope_declarable": False, "claim": "decision_spec.yaml has no sign-off."}]
    if rec.get("spec_sha256") != signed_hash(run_dir):
        return [{"kind": "spec_changed_after_signoff", "severity": "HIGH", "stage": "PLAN", "target_stage": "PLAN",
                 "key": "spec", "scope_declarable": False,
                 "claim": "decision_spec.yaml changed after sign-off. It is the owner's; restore it or ask for a "
                          "new sign-off. Computational settings belong in analysis_settings.yaml."}]
    from toolkit.spec import review_conditions as RC     # W0-G (Codex r2 P1-5): the conditions the signature bound
    bad = RC.problems(run_dir, None, None)
    if bad:
        return [{"kind": "review_conditions_changed_after_signoff", "severity": "HIGH", "stage": "PLAN",
                 "target_stage": "PLAN", "key": "review_conditions", "scope_declarable": False,
                 "claim": "; ".join(bad)[:600]}]
    return []


def deliverable_problems(run_dir: str) -> list[str]:
    """The spec's `deliverable_file.path` must be a plain file inside the run directory: relative,
    no `..`, no symlink on the way, and not a file the harness, the gate or a critic owns."""
    import yaml
    try:
        spec = yaml.safe_load(open(os.path.join(run_dir, AB.SPEC_FILE))) or {}
    except OSError:
        return []
    df = spec.get("deliverable_file")
    if df is None:
        return []
    rel = df.get("path") if isinstance(df, dict) else None
    if not isinstance(rel, str) or not rel.strip() or not isinstance(df.get("columns"), dict):
        return ["deliverable_file needs a path and columns"]
    if os.path.isabs(rel) or ".." in rel.replace("\\", "/").split("/"):
        return [f"deliverable path {rel!r} must be relative and inside the run directory"]
    root = os.path.realpath(run_dir)
    target = os.path.join(root, rel)
    if not os.path.realpath(target).startswith(root + os.sep):
        return [f"deliverable path {rel!r} resolves outside the run directory"]
    cur = root
    for part in rel.replace("\\", "/").split("/"):
        cur = os.path.join(cur, part)
        if os.path.islink(cur):
            return [f"deliverable path {rel!r} goes through a symlink ({os.path.relpath(cur, root)})"]
    norm = os.path.relpath(target, root).replace(os.sep, "/")
    # v2 autonomy 1B-ii: the signed evidence dossier the spec names, compared as a literal path (Codex r2 P1-1: a
    # name such as x[1].yaml is not its own glob) and by identity
    drel = spec["evidence"].get("dossier") if isinstance(spec.get("evidence"), dict) else None
    if isinstance(drel, str) and drel.strip():
        dn = os.path.normpath(drel).replace(os.sep, "/").lower()
        dp = os.path.join(root, drel)
        try:
            same = os.path.exists(dp) and os.path.exists(target) and os.path.samefile(dp, target)
        except OSError:
            same = True
        if dn == norm.lower() or same:
            return [f"deliverable path {rel!r} is the evidence dossier the spec names"]
    if owned_path(root, norm, harness_owned()):
        return [f"deliverable path {rel!r} is a file the harness, the gate or a critic owns"]
    return []


def harness_owned() -> tuple:
    """The run files the harness, the gate or a critic owns (glob patterns): no deliverable and no supplied evidence
    dossier may be one of them."""
    import pipeline as P
    return tuple(P._reserved_patterns()) + PROTECTED_BY_FIX + (
        STATE_FILE, SIGNOFF_FILE, SCOPE_SIGNOFF, "spec_rethink_request.json", FL.LEDGER, FX.IMPACT, "report.md", "manuscript.md", "metadata.json",
        "report_render.json", "report_withheld.md", "final_gate.json", AB.ENGINE_CHOICE, "validator_history.jsonl", "gate_r*", "fix_plan_r*", "fix_progress_r*",
        "analysis/*", "models/pipeline*", "evidence/*", "data/*", "world_model", "world_model/*",
        "evidence_dossier.yaml", SIGNOFF_SHEET,    # v2 autonomy 1B-ii: the default dossier and the sign-off sheet
        AMENDMENT_RECORD)                          # lead-gaps β (Codex r1): toolkit/spec/amend.py's record


def owned_path(root: str, rel: str, owned) -> bool:
    """Is `rel` (inside `root`) one of the owned paths? Compared case-insensitively (APFS and NTFS
    are case-insensitive by default: Report.MD is report.md), and, when the file exists, by
    identity: the same inode as an existing owned file, or any hard link (st_nlink > 1)."""
    import glob
    import pipeline as P
    low = rel.lower()
    if any(P.path_match(low, str(pat).lower()) for pat in owned):
        return True
    target = os.path.join(root, rel)
    if not os.path.exists(target):
        return False
    try:
        if os.path.isfile(target) and os.stat(target).st_nlink > 1:
            return True
        for pat in owned:
            for hit in glob.glob(os.path.join(root, str(pat)), recursive=True):
                if os.path.exists(hit) and os.path.samefile(hit, target):
                    return True
    except OSError:
        return True                  # fail closed
    return False


# Phase 49 β's sign-off lifecycle in v2 (autonomy 1F): a fingerprinted request (scope_signoff.record) written on the
# AWAIT_OWNER route, kept in run_state.json, and the answers taken through the harness (harness_record), honouring the
# run's signoff mode: the owner's under --signoff owner; under --signoff lead the lead's, provisionally (D2), so the run
# never waits; under --signoff none decision-bearing findings stay non-declarable.
OWNER_LIFECYCLE_WIRED = True


def _owner_rule():
    """Phase 49 β's owner rule (scripts/scope_signoff.py with the validator's AWAIT_OWNER path),
    or None when this checkout does not have both, or v2's sign-off lifecycle is not wired."""
    if not OWNER_LIFECYCLE_WIRED:
        return None
    import inspect
    import validate_critique_yaml as V
    try:
        import scope_signoff
    except ImportError:
        return None
    try:
        if "AWAIT_OWNER" not in inspect.getsource(V._recompute_action):
            return None
    except (OSError, TypeError):
        return None
    return scope_signoff


def _bearing_item(run_dir: str, v: dict) -> dict:
    """What the owner is asked about a decision-bearing finding: its claim and the analysis's record of the decision
    test, fingerprinted (an answer binds to both: a changed result is asked again). The record is the producer's: the
    toolkit's verified row (with its result file's hash) for a toolkit finding, the spike's analysis.json otherwise, so
    a stale file of the other backend never stands in for it (Codex r1 of #327)."""
    ev = None
    if v.get("source") == "toolkit/analysis/contract.py":      # lead-gaps α: analysis/decision_tests.yaml, verified
        try:
            from toolkit.analysis import contract as _CT
            ev = _CT.decision_test_record(run_dir, v.get("key"))
        except Exception:  # noqa: BLE001 -- no evidence: the item binds the claim alone, as before
            ev = None
    else:
        try:
            with open(os.path.join(run_dir, AB.ANALYSIS_JSON)) as fh:
                tests = (json.load(fh) or {}).get("decision_tests") or []
            ev = next((t for t in tests if isinstance(t, dict) and str(t.get("id")) == str(v.get("key"))), None)
        except (OSError, ValueError, AttributeError, TypeError):
            ev = None
    gid = FL.gate_id(v)
    fp = hashlib.sha256(json.dumps({"finding": gid, "claim": v.get("claim", ""), "evidence": ev}, sort_keys=True,
                                   default=str).encode()).hexdigest()[:16]
    return {"finding": gid, "kind": v.get("kind"), "reason": "a measured decision test changes the plan",
            "claim": v.get("claim", ""), "declarations": [], "declared": False, "fingerprint": fp}


def _answer_counts(a: dict, mode: str) -> bool:
    """Whose answer releases an owner item: the owner's; the lead's only in a --signoff lead run (D2), and only a
    provisional one (the harness's, listed in the report: Codex r1 of #316)."""
    return a.get("by") == "owner" or (a.get("by") == "lead" and mode == "lead" and a.get("provisional") is True)


def apply_owner_rule(run_dir: str, round_n: int, viols: list[dict], rule="auto", mode: str = "owner") -> list[dict]:
    """Decision-bearing findings (a measured decision test that changes the plan) go through
    Phase 49 β's owner rule before they can be declared: marked for the owner (an item the
    AWAIT_OWNER route requests) until a recorded answer counts (`mode`: the run's sign-off).
    Without the rule, or under --signoff none, they are not scope-declarable, so the run can
    neither declare them away nor finish around them."""
    import validate_critique_yaml as V
    rule = _owner_rule() if rule == "auto" else rule
    bearing = [v for v in viols if v.get("decision_bearing") or v.get("kind") in DECISION_BEARING_KINDS]
    if not bearing:
        if rule is None or mode == "none":
            return []
        # Codex r2 of #316 (P1): an owner's recorded decline holds the gate whatever else is present; nothing new is
        # put to the owner here (the policy kinds go to the owner only beside a decision-bearing finding, as before)
        marked = {id(v) for v in viols if v.get("owner_signoff")}
        try:
            out = [f for f in rule.gate_findings(run_dir, round_n, viols, non_declarable=V.is_non_declarable,
                                                 mode=mode, provisional_only=True) or []
                   if f.get("kind") == "scope_declaration_declined"]
        except Exception:  # noqa: BLE001 -- as before this rule: nothing marked, nothing declined
            out = []
        for v in viols:
            if v.get("owner_signoff") and id(v) not in marked:
                v.pop("owner_signoff", None)
                v.pop("owner_item", None)
        return out
    if rule is not None and mode != "none":
        out = []
        try:
            answers = (getattr(rule, "verified_answers", None) or (lambda rd: ({}, [])))(run_dir)[0]
            for v in bearing:
                item = _bearing_item(run_dir, v)
                a = answers.get(item["fingerprint"]) or {}
                if str(a.get("finding")) == item["finding"] and _answer_counts(a, mode):
                    if a.get("decision") == "accept":
                        v["owner_signed"] = {"by": a.get("by"), "at": a.get("at"), "fingerprint": item["fingerprint"],
                                             **({"provisional": True} if a.get("provisional") else {})}
                        continue
                    v["scope_declarable"], v["owner_declined"] = False, True
                    out.append({"kind": "scope_declaration_declined", "severity": "HIGH", "stage": "GATE",
                                "target_stage": "MODEL", "scope_declarable": False, "key": item["finding"],
                                "claim": f"The {a.get('by')} declined the declaration of `{item['finding']}` "
                                         f"({a.get('at')}): {str(a.get('note') or 'no note')[:300]}."})
                    continue
                v["owner_signoff"], v["owner_item"] = True, item
            return out + list(rule.gate_findings(run_dir, round_n, viols, non_declarable=V.is_non_declarable,
                                                 mode=mode, provisional_only=True) or [])
        except Exception as e:  # noqa: BLE001 -- fail closed below
            reason = f"the owner rule failed ({type(e).__name__}: {e})"
    elif rule is not None:
        reason = "under --signoff none nobody can accept a finding that bears on the decision"
    else:
        reason = "the owner rule (Phase 49 β, scripts/scope_signoff.py) is not available"
    for v in bearing:
        v["scope_declarable"] = False
        v.pop("owner_signoff", None)
        v.pop("owner_signed", None)
        v["claim"] = f"{v.get('claim', '')} Not declarable: {reason}."
    return []


def _earlier_round(path: str, round_n: int) -> int:
    """The round a critique file states when it is an earlier one than `round_n`; else `round_n` (a file that
    cannot be read, or states no earlier round, is validated against the gate's round as usual)."""
    import yaml
    try:
        with open(path) as fh:
            r = (yaml.safe_load(fh) or {}).get("round")
    except Exception:  # noqa: BLE001 -- validate_critique reports it
        return round_n
    return r if isinstance(r, int) and not isinstance(r, bool) and 1 <= r < round_n else round_n


def v2_gate(run_dir: str, round_n: int, max_rounds: int, post_write: bool, extra: list[dict],
            history: bool = True, critics=CRITICS, spec_violations=None, registry_violations=None,
            strict_done_when: bool = False, append_history: bool | None = None, signoff: str = "owner") -> dict:
    """Today's rule functions, in today's order, on the v2 findings: the critic YAMLs, the
    harness's checks (`extra`), and optionally spec-compliance and registry findings. `signoff`:
    the run's mode, for whose answers to owner items count (apply_owner_rule)."""
    import done_when
    import validate_critique_yaml as V
    critiques, schema_errors, refused = {}, [], {}
    draft = any(os.path.exists(os.path.join(run_dir, f)) for f in ("report.md", "manuscript.md"))
    extra = list(extra)
    names = list(critics) + [r for r in V.REVIEWERS if r not in critics and r != "critique-presentation"
                             and os.path.exists(os.path.join(run_dir, V.FILENAMES[r]))]
    names += ["critique-presentation"] if draft else []
    for reviewer in names:
        path = os.path.join(run_dir, V.FILENAMES[reviewer])
        if not os.path.exists(path):
            if reviewer == "critique-presentation":
                extra.append({"kind": "critique_presentation_missing", "severity": "MEDIUM", "stage": "WRITE",
                              "target_stage": "WRITE", "scope_declarable": True,
                              "claim": "A draft exists but critique_presentation.yaml does not."})
                continue
            schema_errors.append(f"{V.FILENAMES[reviewer]} is missing")
            continue
        rn = round_n
        if reviewer == "critique-presentation" and not post_write:
            # A pre-write gate with a draft follows a post-write modelling fix, one round on: PRESENTATION has not
            # run since, and it reviews the rewritten draft before the next post-write gate. The last review is
            # read as of the round it was written in (its findings stay open and listed), not refused for that
            # round, which cost a schema repair and a paid review of a draft about to be replaced.
            rn = _earlier_round(path, round_n)
        try:
            critiques[reviewer] = V.validate_critique(path, reviewer, rn)
        except V.SchemaError as e:
            # Every error in the file, so one repair fixes all of them (T74-T75). The refused file's
            # blockers still have their done_when tests checked below, for the same reason.
            schema_errors.extend(getattr(e, "errors", None) or [str(e)])
            import yaml
            try:
                with open(path) as fh:
                    raw = yaml.safe_load(fh)
                if isinstance(raw, dict) and isinstance(raw.get("blockers"), list):
                    refused[reviewer] = raw
            except Exception:                                # unreadable: its schema errors stand
                pass
    if strict_done_when:
        # v2blind1: critics wrote done_when as prose, done_when.py counted every test as missing,
        # and the open MEDIUMs were accepted. A HIGH or MEDIUM blocker's test is part of the schema
        # here: one that does not parse is repaired like any schema error (the T37 path).
        import done_when as DW
        for reviewer, doc in list(critiques.items()) + list(refused.items()):
            for b in doc.get("blockers") or []:
                if not isinstance(b, dict) or b.get("resolved") is True \
                        or str(b.get("severity")) not in ("HIGH", "MEDIUM"):
                    continue
                try:
                    probs = DW.problems(b["done_when"]) if b.get("done_when") is not None else ["missing"]
                except Exception as e:                       # a test that cannot be checked is a problem
                    probs = [f"done_when could not be checked ({type(e).__name__})"]
                if probs:
                    schema_errors.append(f"{V.FILENAMES[reviewer]}: blocker {b.get('id')} done_when: "
                                         f"{'; '.join(probs)[:300]}")
    for reviewer in V.OPTIONAL_REVIEWERS:
        doc, vs = V._load_optional_critique(run_dir, reviewer, round_n)
        if doc is not None:
            critiques[reviewer] = doc
        extra += vs
    if schema_errors:
        return {"schema_errors": schema_errors, "action": None}
    d = V.decide(critiques, max_rounds, round_n)
    d["declared"] = V._declared_ids(run_dir)
    d["declared_prev"] = V._previous_declared(run_dir, round_n)
    d = V._apply_brief_scope(d, critiques, run_dir, max_rounds, round_n)
    d, dw = V._apply_done_when(d, critiques, run_dir, max_rounds, round_n, record=history)
    d = V._defer_write_findings(d, run_dir, max_rounds, round_n)
    if spec_violations is not None:
        d = V.incorporate_spec_violations(d, list(spec_violations), max_rounds, round_n)
    viols = extra + done_when.gate_findings(dw or {})
    viols += apply_owner_rule(run_dir, round_n, viols, mode=signoff)
    for v in viols:
        k = FL.gate_key(v)
        if k:
            v["stall_key"] = k
    d = V._incorporate_rigor_violations(d, viols, max_rounds, round_n)
    if registry_violations is not None:
        d = V.incorporate_registry_violations(d, list(registry_violations), max_rounds, round_n)
    d["post_write"] = bool(post_write)
    if history if append_history is None else append_history:
        V._append_validator_history(run_dir, round_n, d, post_write)
    return d




def open_findings(run_dir: str, decision: dict) -> list[dict]:
    """Every HIGH or MEDIUM finding still open after a gate: the critics' unresolved blockers (not
    closed by their done_when test), and the harness checks' findings (their test is the check
    itself). `tested`: the finding has a test the fix loop can work to. A write-up finding waits
    for the draft."""
    import done_when as DW
    import open_problems as OP
    import validate_critique_yaml as V
    draft = os.path.exists(os.path.join(run_dir, "manuscript.md"))
    drafted = draft or os.path.exists(os.path.join(run_dir, "report.md"))     # the gate's own test for a draft
    closed = set(map(str, decision.get("resolved_by_test") or [])) | set(map(str, decision.get("outside_brief") or []))
    out = []
    for reviewer, name in V.FILENAMES.items():
        p = os.path.join(run_dir, name)
        if not os.path.exists(p) or (reviewer == "critique-presentation" and not drafted):
            continue
        try:
            import yaml
            with open(p) as fh:
                doc = yaml.safe_load(fh)
            # No defaults before the check (Codex on #264, P1): an empty or truncated file, or one without a
            # `blockers` list (a required key), is unreadable, never "no blockers".
            why = None if isinstance(doc, dict) and isinstance(doc.get("blockers"), list) else \
                f"top level is {type(doc).__name__}, or it has no `blockers` list"
        except Exception as e:  # noqa: BLE001
            why = f"{type(e).__name__}: {str(e)[:160]}"
        if why is not None:
            # The gate refuses to act on such a file (a schema error), so this is not reached from GATE; kept fail
            # closed for any other caller: an open HIGH, never skipped (external review 2026-11-17).
            out.append({"id": f"critique_unreadable:{reviewer}", "source": "harness", "severity": "HIGH",
                        "tested": False, "claim": f"{name} could not be read as a critique ({why})",
                        "fix_requires": "the critic rewrites its YAML"})
            continue
        for b in doc.get("blockers") or []:
            if (not isinstance(b, dict) or b.get("resolved") or str(b.get("severity")) not in ("HIGH", "MEDIUM")
                    or str(b.get("id")) in closed):
                continue
            if not drafted and OP.after_write_item(b):     # the gate's own rule (V._defer_write_findings)
                continue
            dw = b.get("done_when")
            out.append({"id": str(b.get("id")), "source": reviewer, "severity": b.get("severity"),
                        "tested": dw is not None and not DW.problems(dw), "claim": str(b.get("claim") or ""),
                        "fix_requires": _remedy(b)})
    critic_open = {o["id"] for o in out}
    seen = set()
    for v in decision.get("rigor_violations") or []:
        if not (isinstance(v, dict) and str(v.get("severity")) in ("HIGH", "MEDIUM")):
            continue
        fid = FL.gate_id(v)
        # Every gate finding counts (a resolved blocker whose test fails is done_when_unmet; a missing
        # presentation review is critique_presentation_missing); only one that restates an open
        # critic blocker (same id as its key) is a duplicate.
        # (a regression is its own HIGH finding beside the open one it is keyed by)
        if (str(v.get("key") or "") in critic_open and v.get("kind") != "done_when_regressed") or fid in seen:
            continue
        seen.add(fid)
        out.append({"id": fid, "source": "harness", "severity": v.get("severity"),
                    "tested": not str(v.get("kind") or "").startswith("critique_done_when_missing"),
                    "claim": str(v.get("claim") or ""), "fix_requires": _remedy(v)})
    return out


_remedy, short_label = FL.remedy, FL.short_label
MAX_LANGUAGE_REPAIRS = 2


def require_judged(d: dict, opened: list | None = None) -> dict:
    """F6 S1-7: passed, failed and not-judged tests are counted apart (`check_summary`), and a required
    test (an open HIGH or MEDIUM finding's) that is not judged cannot yield ACCEPT: the finding is
    declared (rule 3j), like an open finding with no test the fix loop can work to (rule 3m)."""
    import done_when as DW
    summ = DW.summary(d.get("done_when_status") or {}, required=("HIGH", "MEDIUM"))
    d = dict(d, check_summary=summ)
    nj = summ["required_not_judged"]
    if d.get("action") != "ACCEPT" or not nj:
        return d
    if opened is not None:
        have = {o["id"] for o in opened}
        opened += [{"id": i, "source": "done-when", "severity": (d["done_when_status"].get(i) or {}).get("severity"),
                    "tested": False, "claim": str((d["done_when_status"].get(i) or {}).get("detail") or ""),
                    "fix_requires": None} for i in nj if i not in have]
    d.update(action="DECLARE_SCOPE", rule_matched="3j", declared_open=list(nj),
             rationale=(f"{len(nj)} required test(s) are not judged ({', '.join(nj)}): not judged is not a pass, "
                        "so they are declared in the write-up (declared_findings.yaml), not accepted."))
    return d


def owner_held(ledger: dict) -> set[str]:
    """Findings the owner has answered (hb1 cause H): accepted by the owner (or provisionally by the lead). No fix task
    can work to their test. One still awaiting the owner's answer is not among them (Codex r1 of #344, P1-1): the gate
    sends it to OWNER_DECISION before anything declares it."""
    return {f.id for f in ledger.values() if f.status == "owner_accepted"}


def owner_accepted_now(run_dir: str, opened: list[dict], lead_ok: bool) -> set[str]:
    """The open findings whose current claim and remedy, as this gate reads them, carry the owner's (or, in a lead run,
    the lead's provisional) accept: the answer binds to that fingerprint, so a finding revised since it was answered is
    not among them (Codex r2 of #344, P1-2: the ledger may keep an older remedy)."""
    answers = FL.owner_answers(run_dir, lead_ok)
    out = set()
    for o in opened:
        fid = str(o.get("id"))
        a = answers.get(FL.answer_fingerprint(fid, str(o.get("claim") or ""), o.get("fix_requires")))
        if a and str(a.get("finding")) == fid and a.get("decision") == "accept":
            out.add(fid)
    return out


def apply_medium_rule(decision: dict, opened: list[dict], rounds_remaining: int, budget_ok: bool,
                      owner_held=()) -> dict:
    """v2: the gate never accepts an open HIGH or MEDIUM finding. Today's rules ACCEPT when no HIGH
    is open (rule 4); in v2 an open finding with a test goes to the fix loop while rounds and
    budget remain (rule 4m) and is declared, with the write-up saying so, when they do not (3m).
    A finding the owner holds (`owner_held`) never goes to the fix loop, which has no task for it: it is
    declared (harness baseline hb1, malaria R2: two critique rounds ran for an accepted finding).
    The decision's action and rationale say what the machine then does."""
    if decision.get("action") != "ACCEPT" or not opened:
        return decision
    d = dict(decision)
    held = set(map(str, owner_held or ()))
    tested = [f["id"] for f in opened if f["tested"] and str(f["id"]) not in held]
    if tested and rounds_remaining > 0 and budget_ok:
        d.update(action="PATCH_OR_RETHINK", rule_matched="4m", open_tested=tested,
                 rationale=(f"No HIGH finding is open, but {len(tested)} finding(s) with a test are "
                            f"({', '.join(tested)[:300]}) and {rounds_remaining} round(s) remain: they go to the "
                            "fix loop before ACCEPT."))
    else:
        why = "no rounds remain" if rounds_remaining <= 0 else ("the budget is used" if not budget_ok
                                                                 else "none has a test the fix loop can work to")
        mine = sorted(str(f["id"]) for f in opened if str(f["id"]) in held)
        if mine and rounds_remaining > 0 and budget_ok:
            why = f"the owner's answer, not a fix task, decides {', '.join(mine)[:200]}"
        d.update(action="DECLARE_SCOPE", rule_matched="3m", declared_open=[f["id"] for f in opened],
                 rationale=(f"{len(opened)} HIGH/MEDIUM finding(s) are still open and {why}: they are declared "
                            "in the write-up (declared_findings.yaml), not accepted."))
    return d


DECLARED_FILE = "declared_findings.yaml"


def write_declared(run_dir: str, opened: list[dict], decision: dict) -> None:
    import yaml
    _put(run_dir, DECLARED_FILE, yaml.safe_dump({
        "rule": decision.get("rule_matched"), "rationale": decision.get("rationale"),
        "declared": [{"id": o["id"], "severity": o["severity"], "source": o["source"],
                      "label": short_label(o["claim"]), "claim": o["claim"],
                      "required_remedy": o.get("fix_requires"), "tested": bool(o.get("tested"))}
                     for o in opened]}, sort_keys=False, width=10**6))


# ---- provenance of inputs
_INPUT_EXT = (".csv", ".tsv", ".json", ".yaml", ".yml", ".txt", ".xlsx", ".parquet")
_TRAILING_PUNCT = ".,;:!?)]}>\"'`"
_LEADING_PUNCT = "([{<\"'`"
PROVENANCE = "data/inputs_provenance.json"


def _sha256(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _provenance(run_dir: str) -> dict:
    try:
        return json.load(open(os.path.join(run_dir, PROVENANCE)))
    except (OSError, ValueError):
        return {"inputs": {}, "cited": {}}


def answer_key_path(rel: str) -> bool:
    """A repository path into the evaluation tree. Compared case-insensitively: APFS and NTFS are
    case-insensitive by default, so EVALS/q/rubric.yaml is evals/q/rubric.yaml."""
    parts = [p.lower() for p in rel.replace(os.sep, "/").split("/") if p not in ("", ".")]
    return bool(parts) and (parts[0] == "evals" or "_truth" in parts)


def _path_candidate(tok: str) -> str | None:
    """A question token as a data file name, or None. A token that ends in a data extension is kept as written (a
    quoted "[draft] x.csv" keeps its bracket); otherwise its sentence punctuation comes off first ("... yields.csv."
    ends a sentence), unless a repository file has the exact name (never substituted by another). Only a leading
    "./" is dropped: "../x.csv" and "/x.csv" stay as written, so stage_inputs refuses them as outside the
    repository."""
    t = tok.strip()
    if not t.lower().endswith(_INPUT_EXT):
        if os.path.isfile(os.path.join(REPO, t)):
            return None
        t = t.rstrip(_TRAILING_PUNCT).lstrip(_LEADING_PUNCT).strip()
    while t.startswith("./"):
        t = t[2:]
    return t if t.lower().endswith(_INPUT_EXT) else None


def _question_paths(question: str) -> list[str]:
    """File names in the question, each once: quoted or backticked ones (which may hold spaces), and bare tokens. The
    text of a quoted name is not scanned again for bare tokens (a quoted evals/x y.csv never stages a repo-root
    y.csv); a quoted span that names no file (between two apostrophes) still is."""
    import re
    q = question or ""
    out, bare = [], list(q)
    for m in re.finditer(r"[`\"']([^`\"'\n]+)[`\"']", q):
        c = _path_candidate(m.group(1))
        if c is not None:
            out.append(c)
            bare[m.start():m.end()] = " " * (m.end() - m.start())
    out += [c for c in map(_path_candidate, re.findall(r"[A-Za-z0-9_./-]+", "".join(bare))) if c is not None]
    return list(dict.fromkeys(out))


def _safe_dest(run_dir: str, rel: str) -> str:
    """The destination path inside the run, refusing any symlink on the way and any existing link."""
    root = os.path.realpath(run_dir)
    cur = root
    for part in rel.split("/"):
        cur = os.path.join(cur, part)
        if os.path.islink(cur):
            raise ValueError(f"{rel}: {os.path.relpath(cur, root)} is a symlink")
    if os.path.exists(cur) and os.stat(cur).st_nlink > 1:
        raise ValueError(f"{rel}: the destination is a hard link")
    if not os.path.realpath(cur).startswith(root + os.sep):
        raise ValueError(f"{rel}: outside the run directory")
    return cur


def _copy_nofollow(src: str, dest: str, run_dir: str) -> None:
    """Copy a repo file into the run dir, published through lib.safe_io (no link at any component, atomic)."""
    data = open(src, "rb").read()
    from lib.safe_io import write_nofollow
    write_nofollow(run_dir, os.path.relpath(dest, run_dir).replace(os.sep, "/"), data, atomic=True)


def stage_inputs(run_dir: str, question: str) -> list[str]:
    """Copy every repository file the question names into data/inputs/ (same relative path), with its
    sha256 in data/inputs_provenance.json. Never the evaluation tree (evals/, _truth, any case), and
    never through a link: a symlinked directory or a linked destination is refused (ValueError)."""
    staged = []
    prov = {"inputs": {}, "cited": _provenance(run_dir).get("cited") or {}}
    root = os.path.realpath(REPO)
    for tok in _question_paths(question):
        src = os.path.realpath(os.path.join(root, tok))
        rel = os.path.relpath(src, root).replace(os.sep, "/")
        if (not src.startswith(root + os.sep) or not os.path.isfile(src) or answer_key_path(rel)
                or answer_key_path(tok) or rel.lower().startswith("runs/")):
            continue
        dest_rel = f"data/inputs/{rel}"
        dest = _safe_dest(run_dir, dest_rel)
        parent = os.path.dirname(dest)
        os.makedirs(parent, exist_ok=True)
        _safe_dest(run_dir, dest_rel)                      # nothing linked was created on the way
        _copy_nofollow(src, os.path.join(os.path.realpath(run_dir), dest_rel), os.path.realpath(run_dir))
        prov["inputs"][dest_rel] = {"source": rel, "sha256": _sha256(src), "bytes": os.path.getsize(src)}
        staged.append(dest_rel)
    _safe_dest(run_dir, PROVENANCE)
    os.makedirs(os.path.join(run_dir, "data"), exist_ok=True)
    _write_json(run_dir, PROVENANCE, prov)
    return staged


def record_cited_inputs(run_dir: str) -> list[str]:
    """Every file decision_spec.yaml cites must be inside the run directory; each is recorded with
    its sha256 in data/inputs_provenance.json. Returns the problems (pre-sign)."""
    import yaml
    import pipeline as P
    try:
        spec = yaml.safe_load(open(os.path.join(run_dir, AB.SPEC_FILE))) or {}
    except Exception as e:  # noqa: BLE001
        return [f"{AB.SPEC_FILE} unreadable: {type(e).__name__}"]
    found = P.cited_paths(spec, run_dir)       # one walk, shared with the derived-input checks
    prov, probs, root = _provenance(run_dir), [], os.path.realpath(run_dir)
    # The staged copies must still be the files the question named (the draft task may not touch
    # them; this checks it).
    for dest, e in (prov.get("inputs") or {}).items():
        p = os.path.join(run_dir, dest)
        src = os.path.join(REPO, e.get("source", ""))
        if not os.path.isfile(p) or os.path.islink(p) or _sha256(p) != e.get("sha256"):
            probs.append(f"the staged input {dest} was changed or removed after it was copied")
        elif os.path.isfile(src) and _sha256(src) != e.get("sha256"):
            probs.append(f"the staged input {dest} no longer matches its source {e.get('source')}")
    for rel in sorted(set(found)):
        p = os.path.realpath(os.path.join(run_dir, rel))
        if not p.startswith(root + os.sep):
            probs.append(f"the spec cites {rel}, outside the run directory: copy it into data/")
        elif os.path.isdir(p):
            prov.setdefault("cited", {})[os.path.relpath(p, root).replace(os.sep, "/") + "/"] = {"dir": True}
        elif not os.path.isfile(p):
            probs.append(f"the spec cites {rel}, which does not exist")
        else:
            prov.setdefault("cited", {})[os.path.relpath(p, root).replace(os.sep, "/")] = {"sha256": _sha256(p)}
    os.makedirs(os.path.join(run_dir, "data"), exist_ok=True)
    _write_json(run_dir, PROVENANCE, prov)
    return probs


# ---- claims in the write-up
def report_claim_findings(run_dir: str) -> list[dict]:
    """The report must show values, never claim ids: an unresolved [CLAIM:id], or a claims-ledger id
    left bare in the text the writer wrote, is a HIGH write-up finding (not declarable)."""
    import re
    rp, lp = os.path.join(run_dir, "manuscript.md"), os.path.join(run_dir, "models", "claims_ledger.yaml")
    if not (os.path.exists(rp) and os.path.exists(lp)):
        return []
    try:
        import render_claims as RCL
        claims, invalid = RCL.load_claims_tolerant(lp)
    except Exception as e:  # noqa: BLE001 -- fail closed
        return [{"kind": "report_claims_unreadable", "severity": "HIGH", "stage": "WRITE", "target_stage": "WRITE",
                 "key": "claims", "scope_declarable": False, "claim": f"claims ledger unreadable: {e}"}]
    # The writer's own text, all of it: what the harness appends is not in manuscript.md, so no
    # marker the writer could copy can hide anything.
    text = open(rp).read()
    # One invalid claim is its own finding (the valid ones still render: render_report); still HIGH and not
    # declarable, as the whole-ledger refusal it replaces was.
    out = [{"kind": "report_claim_invalid", "severity": "HIGH", "stage": "WRITE", "target_stage": "WRITE", "key": cid,
            "scope_declarable": False, "claim": f"The claims ledger's entry {cid} is invalid and cannot be rendered: {why}"}
           for cid, why in sorted(invalid.items())]
    # A reference is read as the renderer reads it (render_claims.render_text strips the id).
    refs = {c if "\n" in c else c.strip() for c in re.findall(r"\[CLAIM:([^\]]*)\]", text)}   # no id spans lines
    for cid in sorted(refs - set(claims) - set(invalid)):
        out.append({"kind": "report_claim_unresolved", "severity": "HIGH", "stage": "WRITE", "target_stage": "WRITE",
                    "key": cid, "scope_declarable": False,
                    "claim": f"The report has [CLAIM:{cid}], which is not in the claims ledger."})
    bare = re.sub(r"\[CLAIM:[^\]\n]*\]", " ", text)       # an id inside a reference is not a bare id
    for cid in sorted(c for c in claims if "." in c or "_" in c):     # a plain word is not an id
        if re.search(r"(?<![A-Za-z0-9_.:\-])" + re.escape(cid) + r"(?![A-Za-z0-9_\-]|\.[A-Za-z0-9_])", bare):
            out.append({"kind": "report_claim_id_raw", "severity": "HIGH", "stage": "WRITE", "target_stage": "WRITE",
                        "key": cid, "scope_declarable": False,
                        "claim": f"The report shows the claim id {cid} instead of its value: write [CLAIM:{cid}]."})
    typed = typed_numbers(text, given_values(run_dir, claims))
    rp2 = os.path.join(run_dir, "report.md")
    if os.path.exists(rp2):
        left = re.findall(r"\[CLAIM:([^\]]*)\]", open(rp2).read())
        if left:
            out.append({"kind": "report_claim_unrendered", "severity": "HIGH", "stage": "WRITE", "target_stage": "WRITE",
                        "key": "unrendered", "scope_declarable": False,
                        "claim": f"The report still shows {len(left)} claim reference(s) ({', '.join(sorted(set(left))[:5])}), "
                                 "e.g. inside code; write [CLAIM:id] in plain text."})
    if typed:
        # A text heuristic with a measured false-positive rate of 45% or more (design note §13): LOW,
        # advisory. It is reported (findings.jsonl, the presentation critic's input) but never blocks,
        # goes to the fix loop or is declared. It goes back up only after structural exemptions for its
        # false-positive classes and a new measurement. The structural cases above stay HIGH.
        out.append({"kind": "report_number_typed", "severity": "LOW", "stage": "WRITE", "target_stage": "WRITE",
                    "key": "numbers", "scope_declarable": True,
                    "claim": f"The writer typed {len(typed)} number(s) ({', '.join(typed[:8])}) that are not in the spec, "
                             "the claims ledger or the question; write each as a [CLAIM:id] the harness renders."})
    return out


def report_acceptance_findings(run_dir: str, write_mode: str | None) -> list[dict]:
    """F6 S1-7: a declared-scope or not-accepted write-up uses no acceptance language (the terms are in
    .claude/orchestration/acceptance_language.yaml; the check is scripts/writer_qa.py's). MEDIUM: a
    write-up fix the loop can make, declared when it cannot."""
    if write_mode not in ("declared", "not_accepted"):
        return []
    try:
        text = open(os.path.join(run_dir, "manuscript.md")).read()
    except OSError:
        return []
    try:
        import writer_qa as WQ
        hits = WQ.acceptance_language_hits(text, "completed_scope_declared" if write_mode == "declared" else "not_accepted")
    except Exception as e:  # noqa: BLE001 -- fail closed
        hits = [f"the check could not run ({type(e).__name__}: {e})"]
    if not hits:
        return []
    return [{"kind": "report_acceptance_language", "severity": "MEDIUM", "stage": "WRITE", "target_stage": "WRITE",
             "key": "acceptance", "scope_declarable": True,
             "claim": (f"The write-up is {'declared scope' if write_mode == 'declared' else 'not accepted'} but uses "
                       f"acceptance language ({len(hits)}): " + "; ".join(hits)
                       + ". Say what was tested and what is declared.")}]


_SUFFIX = {"k": 1e3, "m": 1e6, "b": 1e9, "bn": 1e9}


def given_values(run_dir: str, claims: dict | None = None) -> list[float]:
    """Numbers a report may show as typed: those in the signed spec and the question (the owner's
    facts), and every number in the claims ledger, the interval level included (0.95 is also 95)."""
    import re
    texts = []
    try:
        texts.append(open(os.path.join(run_dir, AB.SPEC_FILE)).read())
    except OSError:
        pass
    try:   # the question only: not timestamps or other run metadata
        texts.append(str(json.load(open(os.path.join(run_dir, "metadata.json"))).get("question") or ""))
    except (OSError, ValueError):
        pass
    vals = []
    for t in texts:
        vals += [float(g.replace(",", "")) for g in re.findall(r"\d[\d,]*(?:\.\d+)?", t)]

    def walk(x):
        if isinstance(x, bool):
            return
        if isinstance(x, (int, float)):
            vals.append(float(x))
        elif isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, (list, tuple)):
            for v in x:
                walk(v)
    walk(list((claims or {}).values()))
    vals += [v * 100 for v in list(vals) if 0 < abs(v) <= 1]          # proportions written as percentages
    return vals


def typed_numbers(text: str, given: list[float]) -> list[str]:
    """Numbers typed in `text` outside [CLAIM:] references that match none of `given` at the precision
    they are written with. Not years or year ranges, list or heading numbering, or digits inside an
    identifier (D09, DT1, R-004)."""
    import math
    import re
    bare = re.sub(r"\[CLAIM:[^\]]*\]", " ", text)
    bare = re.sub(r"(?m)^\s*(#+\s*)?\d+[.)]\s", " ", bare)
    num = re.compile(r"(?<![A-Za-z0-9_.])[-+\u2212]?\d[\d,]*(?:\.\d+)?(?:[eE][-+]?\d+)?(?:\s?(?:[kKmMbB]n?|%))?(?![A-Za-z0-9_])")
    out = []
    for m_ in num.finditer(bare):
        t = m_.group(0).rstrip(",")
        before = bare[max(0, m_.start() - 1):m_.start()]
        if t[0] in "-+\u2212" and before and (before.isalnum() or before == "_"):
            continue                                  # an identifier such as R-004
        mant = re.match(r"[-+\u2212]?([\d,]*(?:\.\d+)?)", t).group(1).replace(",", "")
        rest = t[len(re.match(r"[-+\u2212]?[\d,]*(?:\.\d+)?", t).group(0)):].strip()
        if re.fullmatch(r"(19|20)\d\d", mant) and not rest:
            continue                                  # a year, or one end of a year range
        try:
            v = float(mant)
        except ValueError:
            continue
        exp = re.match(r"[eE]([-+]?\d+)", rest)
        if exp:
            v *= 10 ** int(exp.group(1))
            rest = rest[exp.end():].strip()
        mult = _SUFFIX.get(rest.lower(), 1.0)
        v *= mult
        digits = mant.replace(".", "").lstrip("0") or "0"
        sig = max(1, len(digits))
        def same(g):
            if g == 0 or v == 0:
                return g == v
            return float(f"{abs(g):.{sig}g}") == round(abs(v), 12) or math.isclose(abs(g), abs(v), rel_tol=1e-9)
        if any(same(g) for g in given):
            continue
        out.append(t)
    return out


# ------------------------------------------------------------------ sign-off
def signed_hash(run_dir: str) -> str | None:
    """sha256 of decision_spec.yaml as a file (the owner signs all of it, as workstream B does)."""
    p = os.path.join(run_dir, AB.SPEC_FILE)
    if os.path.islink(p):
        return None
    try:
        with open(p, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def _signoff(run_dir: str) -> dict | None:
    try:
        return json.load(open(os.path.join(run_dir, SIGNOFF_FILE)))
    except (OSError, ValueError):
        return None


def sign(run_dir: str, by: str, note: str = "", expect_sha256: str | None = None) -> dict:
    """The spike adapter's sign-off. The spec is read once, never through a link (lib.safe_io); that same content is
    hashed into the record and frozen with its geometry inputs (Codex on 08f96a9)."""
    import yaml
    from lib.safe_io import LinkRefused, read_nofollow
    try:
        data = read_nofollow(run_dir, AB.SPEC_FILE)[0]
    except FileNotFoundError:
        raise ValueError(f"no {AB.SPEC_FILE} to sign") from None
    except (LinkRefused, OSError) as e:
        raise ValueError(f"{AB.SPEC_FILE}: {e}") from None
    rec = {"by": by, "note": note, "at": _now(), "spec_sha256": hashlib.sha256(data).hexdigest()}
    if expect_sha256 is not None and rec["spec_sha256"] != expect_sha256:
        raise ValueError(f"{AB.SPEC_FILE} changed since it was shown; read it again before signing")
    try:
        spec_doc = yaml.safe_load(data.decode("utf-8")) or {}
    except (yaml.YAMLError, UnicodeDecodeError) as e:
        raise ValueError(f"{AB.SPEC_FILE} does not parse: {e}") from None
    from toolkit.spec import preconditions as PC          # every signing path checks the preconditions (#215)
    from toolkit.spec import review_conditions as RC     # W0-G: read once, validated here and bound in the record
    rc_bytes = RC.read_bytes(run_dir)
    pre = PC.problems(run_dir, data, by, review_bytes=rc_bytes)
    if pre:
        raise ValueError("spec preconditions not met: " + "; ".join(pre[:10]))
    # engine parameter values without a basis, or chosen with a held-out benchmark, are refused whoever signs (as in
    # toolkit/spec/signoff.py sign and scripts/decision_brief.py sign)
    from toolkit.spec import engine_parameters as PV
    held = PV.signoff_refusals(spec_doc if isinstance(spec_doc, dict) else {}, run_dir=run_dir)
    if held:
        raise ValueError("spec not signable (engine_parameters): " + "; ".join(held[:10]))
    from toolkit.spec import signoff as SO      # F6 (#218): every registered precondition, recomputed, whoever signs
    ev = SO.sign_preconditions(run_dir, data)  # (the sheet must show this dossier: refused without one when needed)
    rec.update(evidence=ev["record"], evidence_sha256=ev["files"])
    rec[RC.RECORD_KEY] = RC.sha_of(rc_bytes)
    from toolkit.spec import geometry as GEO
    # the geometry declaration and input hashes of this content; the owner's approvals
    GEO.freeze(run_dir, by, spec=spec_doc if isinstance(spec_doc, dict) else {})
    _put(run_dir, SIGNOFF_FILE, json.dumps(rec, indent=1))
    return rec


POST_CHOICE_STATES = ("PREREGISTER", "CONFIGURE", "ANALYZE", "SCORE", "CRITIQUE", "GATE", "FIX_LOOP", "OWNER_DECISION",
                      "WRITE", "PRESENTATION")


def evidence_gate(run_dir: str, engine: tuple | None = None, signoff: str | None = None) -> list[str]:
    """F6 (#218): the registered preconditions of a signed run, recomputed from its files (the sign-off record
    compared; the engine chosen in engine_choice.json, or `engine`): checked at ENGINE_CHOICE on the engine picked and
    on every resume past it, before any compute. Empty: they hold. Fail closed on an error."""
    try:
        from toolkit.spec import preconditions as PC
        # signoff none (nobody signs): coverage without a record, the rule the launch preflight applies too
        return PC.problems(run_dir, None, None, mode="unsigned" if signoff == "none" else "resume",
                           engine=engine)          # every check: the benchmark verdict too
    except Exception as e:  # noqa: BLE001
        return [f"the preconditions could not be checked: {type(e).__name__}: {e}"]


AWAITING_SPEC_AMENDMENT = "awaiting_spec_amendment"
SPEC_RETHINK_REQUEST = "spec_rethink_request.json"
# lead-gaps β: under --signoff lead a rethink of the signed spec with a valid patch becomes a child run (toolkit/spec/
# amend.py), signed provisionally by the lead; the parent ends with this status and the run continues in the child.
AMENDED_PROVISIONAL = "amended_provisional"
AMENDMENT_RECORD = "amendment_record.json"           # toolkit/spec/amend.py RECORD
# held by a --signoff lead run across its amendment and hand-off; under analysis/ (reserved for every run, so no
# owner-mode deliverable path changes: Codex r3) and never copied into a child (.lock)
AMEND_LOCK = "analysis/spec_amendment.lock"
LEGACY_BRIEF_SIGNOFF = ("decision_brief.signoff.json",)      # scripts/decision_brief.py SIGNOFF (the v1 brief)


class AmendmentLocked(RuntimeError):
    """Another process holds the parent's amendment lock, or has already handed the run to its child: this resume
    stops with nothing written (owner decision on #330). `run_dir`: the run whose lock is held (main.py stores a
    parent it finished up to a child another process holds: Codex r1 of #349)."""

    def __init__(self, msg: str, run_dir: str | None = None):
        super().__init__(msg)
        self.run_dir = run_dir


_LOCK_HELD = __import__("threading").local()


class _amend_lock:
    """An exclusive, non-blocking lock on <run>/analysis/spec_amendment.lock (lib.safe_io, no link followed; flock is
    per open file, so two processes, or two machines in two threads, exclude each other). Nests within one thread (a
    lead run holds it from run_v2 through its amendment and hand-off). A second holder raises AmendmentLocked."""

    def __init__(self, run_dir: str):
        self.run_dir, self.f = run_dir, None
        self.key = os.path.realpath(os.path.join(run_dir, AMEND_LOCK))

    def __enter__(self):
        held = _LOCK_HELD.__dict__.setdefault("depth", {})
        if held.get(self.key):
            held[self.key] += 1
            return self
        import fcntl
        from lib.safe_io import open_append_nofollow
        f = open_append_nofollow(self.run_dir, AMEND_LOCK)
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            f.close()
            raise AmendmentLocked(f"another process is amending {self.run_dir} ({AMEND_LOCK} is held): this resume "
                                  "stops with nothing written; resume again once that process has ended",
                                  run_dir=self.run_dir) from None
        except BaseException:
            f.close()
            raise
        self.f = f
        held[self.key] = 1
        return self

    def __exit__(self, *exc):
        held = _LOCK_HELD.__dict__.setdefault("depth", {})
        held[self.key] -= 1
        if held[self.key]:
            return False
        del held[self.key]
        import fcntl
        try:
            fcntl.flock(self.f, fcntl.LOCK_UN)
        finally:
            self.f.close()
        return False


def _spec_signed_sha(run_dir: str) -> str | None:
    """The spec sha256 a sign-off record binds (the spec's own record, or a brief's that hashed the spec), or None."""
    import re as _re
    hexd = _re.compile(r"[0-9a-f]{64}")

    def nested(r):
        m = r.get("sha256")
        return m.get(AB.SPEC_FILE) if isinstance(m, dict) else (False if m is not None else None)
    def own(r):        # the spec's own record: spec_sha256 (spike) or the nested map (toolkit); a present but empty
        v = r.get("spec_sha256") or nested(r)          # spec_sha256 with no nested digest is not a binding: unreadable
        return False if v is None and "spec_sha256" in r else v
    for f, get in ((SIGNOFF_FILE, own),
                   ("decision_brief.signoff.json", nested)):
        p = os.path.join(run_dir, f)
        if not os.path.lexists(p):
            continue
        try:
            rec = json.load(open(p))
        except (OSError, ValueError):
            return "unreadable"          # fail closed: an unreadable record still protects the spec (#261 re-review)
        if not isinstance(rec, dict):
            return "unreadable"
        v = get(rec)
        if v is False or (v is not None and not (isinstance(v, str) and hexd.fullmatch(v))):
            return "unreadable"          # a malformed binding is not a signature (narrow Codex review of #272)
        if v:
            return v
    return None


def spec_signed_once(run_dir: str) -> bool:
    """A sign-off record binds the run's spec: it was signed, so only the owner may change it (a brief signed before
    any spec existed does not count: Codex review of #261 P2-5)."""
    return _spec_signed_sha(run_dir) is not None


def rethink_request(run_dir: str, round_n: int, signoff: str = "owner") -> dict:
    """spec_rethink_request.json: why the run asks for a spec amendment (the latest fix plan's rethink briefs, the
    open findings the spec owns, with their remedies) and the signed spec it would amend (its sha256)."""
    briefs = []
    try:          # this round's fix plan only: an earlier round's (an engine rethink) is not this request's (#261 P2-6)
        plan = json.load(open(os.path.join(run_dir, f"fix_plan_r{round_n}.json"))).get("plan") or {}
    except (OSError, ValueError, AttributeError):
        plan = {}
    briefs = [{"task": t.get("id"), "findings": t.get("findings"), "brief": t.get("brief")}
              for t in plan.get("tasks") or [] if isinstance(t, dict) and t.get("action") == "rethink"
              and t.get("owner_state") == "SPEC"]
    led = FL.load(run_dir)
    fs = [{"id": f.id, "severity": f.severity, "claim": f.claim, "fix_requires": f.fix_requires}
          for f in FL.open_findings(led) if f.owner_state == "SPEC" or any(f.id in (b["findings"] or []) for b in briefs)]
    try:
        prev = json.load(open(os.path.join(run_dir, SPEC_RETHINK_REQUEST)))
    except (OSError, ValueError):
        prev = None
    if isinstance(prev, dict) and prev.get("round") == round_n:
        return prev                  # a resume returns the request it made (Codex review of #261 P1-2)
    # lead-gaps β: the rethink's machine-readable amendment, under --signoff lead only (an owner keeps `amend --from-request
    # --patch` for their own patch: amend refuses a request patch beside --patch; Codex r1)
    patch = FX.spec_patch(plan) if signoff == "lead" else None
    doc = {"round": round_n, "requested_at": _now(), "signed_spec_sha256": _spec_signed_sha(run_dir),
           "working_spec_sha256": signed_hash(run_dir), "briefs": briefs, **({"patch": patch} if patch else {}),
           "findings": fs, "next": "amend decision_spec.yaml and relaunch with reuse (a new run dir from this one's "
                                    "spec and stored rows); this run does not redraft or re-sign its spec"}
    _write_json(run_dir, SPEC_RETHINK_REQUEST, doc)
    return doc


OWNER_REQUEST_PRODUCER = "agents/stage_machine.py FIX_LOOP (owner amendments)"


def pending_amendments(ledger: dict, resolved=()) -> list[str]:
    """Findings, open or declared, whose remedy is an owner amendment the owner has not answered (Codex #258 P1-10);
    not one whose test now passes (`resolved`: the gate's resolved_by_test)."""
    done = set(map(str, resolved or ()))
    return sorted(f.id for f in ledger.values() if f.status in ("open", "declared") and f.id not in done
                  and FX.needs_owner_amendment(f))


def pending_conflicts(ledger: dict) -> list[str]:
    """W0-F: held-out calibration targets whose automatic test changes the plan (or did not run), not yet answered by
    the owner; asked together with the owner amendments, once (an answer closes them)."""
    from toolkit.analysis import conflicts as CF
    return sorted(f.id for f in ledger.values() if f.kind == CF.FINDING_KIND and f.status == "open")


def drop_declined(run_dir: str, ids) -> None:
    """A finding the owner declined is not a declared limitation: it leaves declared_findings.yaml (Codex #258)."""
    import yaml
    p = os.path.join(run_dir, DECLARED_FILE)
    try:
        doc = yaml.safe_load(open(p)) or {}
    except (OSError, yaml.YAMLError):
        return
    if not isinstance(doc, dict) or not isinstance(doc.get("declared"), list):
        return
    keep = [x for x in doc["declared"] if not (isinstance(x, dict) and str(x.get("id")) in set(map(str, ids)))]
    if len(keep) != len(doc["declared"]):
        _put(run_dir, DECLARED_FILE, yaml.safe_dump(dict(doc, declared=keep), sort_keys=False, width=10**6))


def apply_owner_declines(decision: dict, ledger: dict) -> dict:
    """Codex review of #258 P1-11: a finding the owner declined (the run may not go on without it) holds the gate: a
    decision that would accept or declare is not accepted instead, with the decline as a non-declarable finding, so the
    run ends completed_scope_declined (route_gate) once the write-up is gated."""
    fixed = set(map(str, decision.get("resolved_by_test") or []))     # its test passes: the decline is moot
    declined = sorted(f.id for f in ledger.values() if f.status == "owner_declined" and f.id not in fixed)
    if not declined or decision.get("action") not in ("ACCEPT", "DECLARE_SCOPE"):
        return decision
    held = [{"kind": "scope_declaration_declined", "finding": fid, "severity": "HIGH", "scope_declarable": False,
             "claim": f"the owner declined to let the run go on without {fid}"} for fid in declined]
    return dict(decision, action="RUN_FAILED", rule_matched="owner_declined",
                unresolved_high=list(decision.get("unresolved_high") or []) + held,
                rationale=f"the owner declined {', '.join(declined)[:200]}: the run is not accepted or declared")


def request_owner_amendments(run_dir: str, round_n: int, ledger: dict, ids: list[str]) -> dict:
    """F6 run 3: the findings whose remedy is an owner amendment of the signed spec, written as a request in
    scripts/scope_signoff.py's format, so the owner reads it with `scope_signoff.py show` and answers with `sign`
    (accept: the run goes on without the amendment, declared; decline: the finding stays open, the owner's). Each item's
    fingerprint binds its claim and remedy."""
    items = []
    for fid in ids:
        f = ledger.get(fid)
        claim, fix = (f.claim, f.fix_requires) if f is not None else ("", None)
        fp = FL.answer_fingerprint(fid, claim, fix)       # the ledger binds the owner's answer to it
        conflict = getattr(f, "kind", None) == "evidence_conflict_declared"
        opts = {}
        if conflict:            # owner decision 2026-11-21: a refit is listed, with its estimated cost, never run
            from toolkit.analysis import conflicts as CF
            opts = {"options": CF.options(run_dir, getattr(f, "key", "")),
                    "conflict": CF.verdict(run_dir, getattr(f, "key", ""))}     # Codex r4 of #316: the structured verdict
        items.append({"finding": fid, "kind": getattr(f, "kind", None), "claim": claim, "fix_requires": fix, **opts,
                      "reason": ("a held-out calibration target's automatic decision test changes the plan or could "
                                 "not run (conflicts.policy declare_and_test)") if conflict else
                      "its required remedy is an owner amendment of the signed decision spec",
                      "declarations": [], "declared": False, "fingerprint": fp})
    doc = {"pending": True, "round": round_n, "requested_at": _now(), "items": items, "producer": OWNER_REQUEST_PRODUCER,
           "decision_rationale": "the fix loop does not change the owner-signed spec: amend and re-sign it, or answer "
                                 "accept (the run continues without the amendment) or decline"}
    from lib.safe_io import write_nofollow
    write_nofollow(run_dir, OWNER_REQUEST, json.dumps(doc, indent=1).encode(), atomic=True)
    return doc


# ------------------------------------------------------------------ the machine
class Machine:
    def __init__(self, run_dir: str, tasks, state: RunState | None = None, backend=None, log=print,
                 cross_engine_check: bool = True):
        self.run_dir = os.path.abspath(run_dir)
        self.tasks = tasks
        self.backend = backend or AB.backend()
        self.log = log
        self.cross = cross_engine_check
        st = load_state(self.run_dir)
        if (st or state) is not None and (st or state).signoff == "lead":
            # a lead run's state is read again and written back under its amendment lock, so a state read before
            # another process's hand-off is never written over it (Codex r3 on #342, P1-9)
            with _amend_lock(self.run_dir):
                self._load(load_state(self.run_dir), state)
        else:
            self._load(st, state)
        self.seed_fn = getattr(tasks, "world_model_seed", None)    # §14: the world model's seed task, when the tasks have one
        self.wm_repo = REPO                                         # where the beliefs are anchored (wm-trials)

    def _load(self, st: RunState | None, state: RunState | None) -> None:
        self.st = st or state
        if self.st is None:
            raise ValueError("no run_state.json and no initial state")
        self._settle_interrupted()
        save_state(self.run_dir, self.st)

    def persist(self) -> None:
        # Named persist, not save, only because scripts/write_audit.py WA3 reads any `<obj>.save()` call as PIL's
        # image save (a false positive): this writes run_state.json through save_state -> lib.safe_io.write_nofollow,
        # atomic. The heuristic's own-module exemption cannot see a method defined in another module (the caller is
        # agents/world_model_stage.py), so it was not changed.
        save_state(self.run_dir, self.st)

    def _settle_interrupted(self) -> None:
        """Every journal entry left `started` by a previous process was interrupted: charge its
        allowance once (the task cap when it had none) and mark it, before anything else runs."""
        for j in self.st.journal.values():
            if isinstance(j, dict) and j.get("status") == "started":
                self.st.spent_usd += self._interrupted_charge(j)
                j["interrupted_charged"] = j.get("interrupted_charged", 0) + 1
                j["status"] = "interrupted"

    def _interrupted_charge(self, j: dict) -> float:
        """What an interrupted task is charged: its allowance (the task cap when it had none) and, for a write-up
        task, the read-back allowance it was given (0 in an entry written before that was recorded)."""
        a, rb = j.get("allowance_usd"), j.get("readback_usd")
        return float(a if a is not None else self.st.task_cap_usd) + float(rb if isinstance(rb, (int, float)) else 0.0)

    # -------------------------------------------------- plumbing
    def _go(self, to: str, why: str = "", status: str | None = None, extra: dict | None = None,
            before_save=None) -> str:
        frm = self.st.state
        if to not in EDGES[frm]:
            raise RuntimeError(f"transition {frm} -> {to} is not in the state table")
        self.st.history.append({"at": _now(), "from": frm, "to": to, "why": why, "round": self.st.round,
                                "spent_usd": round(self.st.spent_usd, 4), **(extra or {})})
        self.st.state = to
        if to == "FINAL":
            self.st.status = self.st.forced_status or status or "completed_gate_error"
            self._withhold_if_acceptance_language()
            self._finish()
        if before_save is not None:
            before_save()
        save_state(self.run_dir, self.st)
        self.log(f"[machine] {frm} -> {to}" + (f" ({why})" if why else ""))
        return to

    def _charge(self, res) -> None:
        self.st.spent_usd += float(getattr(res, "usd", 0.0) or 0.0)

    def _allowance(self, writeup: bool = False) -> float | None:
        """What the next paid task may spend: the task cap, at most what the run has left less the
        write-up reserve (the write-up itself may use the reserve). None: no run cap."""
        cap = self.st.budget_usd
        if not cap:
            return None
        from agents.run_budget import writeup_reserve
        left = cap - self.st.spent_usd - (0.0 if writeup else writeup_reserve(cap))
        return round(min(self.st.task_cap_usd, left), 4)

    def _visit(self) -> int:
        return sum(1 for h in self.st.history if h.get("to") == self.st.state)

    def _task(self, key: str, name: str, *args, writeup: bool = False, **kwargs):
        """One paid task through the journal. Returns its TaskResult, or None when the run has no
        allowance left for it (the caller takes the budget path). A completed task is not run
        again (its result is replayed, already charged); a task found started but not completed
        was interrupted: it is charged its allowance once, then run again."""
        from v2_tasks import TaskResult
        j = self.st.journal.get(key)
        if j and j.get("status") == "completed":
            return TaskResult(bool(j.get("ok")), j.get("output"), 0.0, j.get("note", ""), j.get("session_id"))
        if j and j.get("status") == "started":
            self.st.spent_usd += self._interrupted_charge(j)
            j["interrupted_charged"] = j.get("interrupted_charged", 0) + 1
            j["status"] = "interrupted"
            save_state(self.run_dir, self.st)
        allowance = self._allowance(writeup)
        if allowance is not None and allowance <= 0:
            return None
        if allowance is not None and allowance < MIN_TASK_ALLOWANCE_USD:
            self.st.paused, self.st.status = AWAITING_BUDGET, None
            self.st.history.append({"at": _now(), "paused": AWAITING_BUDGET, "state": self.st.state, "task": name,
                                    "key": key, "allowance_usd": allowance, "floor_usd": MIN_TASK_ALLOWANCE_USD,
                                    "spent_usd": round(self.st.spent_usd, 4), "cap_usd": self.st.budget_usd})
            save_state(self.run_dir, self.st)
            self.log(f"[machine] {name} not started: its allowance ${allowance:.2f} is below the ${MIN_TASK_ALLOWANCE_USD:.2f} "
                     f"floor (spent ${self.st.spent_usd:.2f} of ${self.st.budget_usd}). Paused for the owner; resume with "
                     f"python3 main.py --v2 --resume {self.run_dir} --max-budget-usd <higher cap>")
            raise AwaitingBudget(name)
        a = allowance if allowance is not None else self.st.task_cap_usd
        b = self._budget(writeup)
        # A crash read-back is a paid call too: a small cap, from what is left after this allowance (for a
        # write-up task, left of the reserve it may use).
        rb = READBACK_CAP_USD if b is None else max(0.0, min(READBACK_CAP_USD, round(b["left"] - a, 4)))
        # A write-up task spends up to the cap itself, so its read-back allowance is part of what an interrupted
        # one is charged (_interrupted_charge): the cap holds on resume.
        j = dict(j or {}, task=name, status="started", started=_now(), allowance_usd=allowance, writeup=bool(writeup),
                 readback_usd=rb if writeup else 0.0)
        self.st.journal[key] = j
        save_state(self.run_dir, self.st)
        setter = getattr(self.tasks, "set_allowance", None)
        if setter:
            setter(a, readback_usd=rb)
        res = getattr(self.tasks, name)(*args, **kwargs)
        self._charge(res)
        out = res.output
        try:
            json.dumps(out)
        except (TypeError, ValueError):
            out = None
        j.update(status="completed", completed=_now(), ok=bool(res.ok), output=out, usd=float(res.usd or 0.0),
                 note=(res.note or "")[:300], session_id=getattr(res, "session_id", None))
        save_state(self.run_dir, self.st)
        return res

    def _budget_out(self, s: str):
        """The budget path from state `s` (no allowance for the next paid task)."""
        self.st.forced_status = "budget_exhausted"
        if s in ("CRITIQUE", "FIX_LOOP") and not self._has_draft():
            self.st.write_mode = "not_accepted"
            return self._go("WRITE", "budget used: the harness writes the report")
        if s in ("CRITIQUE", "FIX_LOOP", "PRESENTATION", "WRITE"):
            return self._go("FINAL", "budget used")
        return self._go("FINAL", "budget used before the model was built", "budget_exhausted")

    def _budget(self, writeup: bool = False) -> dict | None:
        """What is left under the cap: less the write-up reserve, except for the write-up's own tasks (WRITE and
        the PRESENTATION review of its draft), which the reserve is held for."""
        cap = self.st.budget_usd
        if not cap:
            return None
        from agents.run_budget import writeup_reserve
        left = cap - (0.0 if writeup else writeup_reserve(cap)) - self.st.spent_usd
        return {"spent": round(self.st.spent_usd, 4), "cap": cap, "left": round(left, 4)}

    def _out_of_budget(self) -> bool:
        b = self._budget()
        return b is not None and b["left"] <= 0

    def _has_draft(self) -> bool:
        return os.path.exists(os.path.join(self.run_dir, "manuscript.md"))

    def _spec(self, rd: str | None = None) -> dict:
        import yaml
        return yaml.safe_load(open(os.path.join(rd or self.run_dir, AB.SPEC_FILE))) or {}

    def harness_findings(self) -> list[dict]:
        out = self.backend.gate_findings(self.run_dir) + spec_signoff_findings(self.run_dir, self.st.signoff, self.backend)
        out += [{"kind": "deliverable_path_invalid", "severity": "HIGH", "stage": "PLAN", "target_stage": "PLAN",
                 "key": "deliverable", "scope_declarable": False, "claim": p} for p in deliverable_problems(self.run_dir)]
        if self.st.v2_rules:
            out += report_claim_findings(self.run_dir)
        if self.st.derived_inputs or os.path.exists(os.path.join(self.run_dir, "data", "derivations.yaml")):
            import pipeline as P
            try:
                spec = self._spec()
            except Exception:  # noqa: BLE001 -- the spec's own findings say why; the data refs need it
                spec = None
            out += P.derivation_findings(self.run_dir, spec, required=self.st.derived_inputs)
        out += self._geometry_findings()
        return out

    def _geometry_findings(self) -> list[dict]:
        """The standing input geometry check (toolkit/spec/geometry.py, docs/design/v2-input-geometry.md): the units'
        coordinates and areas against the declared boundaries, re-run on the current files at every gate."""
        if not os.path.exists(os.path.join(self.run_dir, AB.SPEC_FILE)):
            return []
        from toolkit.spec import geometry as GEO
        return GEO.findings(GEO.run(self.run_dir))

    def _geometry_problems(self, rd: str | None = None) -> list[str]:
        """Before SIGN: a HIGH geometry finding not accepted by name in the spec, or a check that failed on its
        declared inputs. Writes data/input_checks.json, which the sign-off sheet reads."""
        from toolkit.spec import geometry as GEO
        return [f"{p['code']}: {p['claim']}" for p in GEO.problems(GEO.run(rd or self.run_dir))]

    def _geometry_owner_pending(self) -> list[str]:
        """HIGH geometry findings the spec proposes as exceptions and the owner has not approved."""
        if not os.path.exists(os.path.join(self.run_dir, AB.SPEC_FILE)):
            return []
        from toolkit.spec import geometry as GEO
        return GEO.owner_pending(GEO.run(self.run_dir))

    def _signed_ref(self, run_dir: str):
        """The signed spec's sha256: from the backend's own sign-off (B) or the machine's."""
        if getattr(self.backend, "sign", None):
            return self.backend.signed_sha(run_dir)
        return (_signoff(run_dir) or {}).get("spec_sha256")

    def _sign(self, by: str, note: str = "") -> None:
        if getattr(self.backend, "sign", None):
            self.backend.sign(self.run_dir, by, note)
        else:
            sign(self.run_dir, by, note)

    def _signed_by(self, rd: str | None = None):
        """Who signed the current spec (of `rd`, the run dir by default), or None when it is unsigned or changed since."""
        rd = rd or self.run_dir
        if getattr(self.backend, "sign", None):
            if self.backend.signoff_status(rd) != "signed":
                return None
            try:
                from toolkit.spec import signoff
                rec = signoff._spec_record(rd) or {}
            except Exception:  # noqa: BLE001
                rec = {}
            return rec.get("signed_by") or rec.get("by") or "owner"
        rec = _signoff(rd)
        return rec.get("by") if rec and rec.get("spec_sha256") == signed_hash(rd) else None

    def fix_guard(self, run_dir: str) -> list[str]:
        """What a fix task may not do: change decision_spec.yaml (compared with its sign-off, or with
        its hash when the loop started), or leave a settings file the analysis would refuse."""
        probs = []
        ref = self._signed_ref(run_dir) if self.st.signoff != "none" else None
        ref = ref or self.st.journal.get(f"FIX_LOOP#{self._visit()}:spec_sha256", {}).get("output")
        if ref and signed_hash(run_dir) != ref:
            probs.append(f"{AB.SPEC_FILE} changed; it is signed (computational settings go in {AB.SETTINGS_FILE})")
        probs += [f"settings: {e}" for e in self.backend.validate(os.path.join(run_dir, AB.SPEC_FILE))]
        return probs

    def harness_ids(self, run_dir: str) -> set[str]:
        """The ledger ids the harness can re-check between gates: its own findings and, as h_GATE reads them, the
        backend's spec and registry checks. A fix task's finding absent from them passes its check; the gate's other
        findings (fix_loop.GATE_ONLY) are never among them and are left to the next gate."""
        vs = self.harness_findings()
        for name in ("spec_violations", "registry_violations"):
            fn = getattr(self.backend, name, None)
            vs += list(fn(run_dir) or []) if fn else []
        return {FL.gate_id(v) for v in vs if isinstance(v, dict)}

    def _rethink(self, to: str, why: str, planned: bool = False):
        """A RETHINK back-edge to SPEC or ENGINE_CHOICE, which is told why. From the gate (a structural mismatch): its
        rationale, each critic's description of the mismatch and its STRUCTURAL findings. From the fix loop
        (`planned`): this round's rethink briefs for that state and the findings they name. Either way, the open
        findings that state owns, each with its remedy. An unsigned spec is redrafted with them as its feedback and a
        fresh count of tries (a signed one is the owner's to amend: h_SPEC); the engine pick is told the engine the
        run left. Nothing read here can stop the back-edge: a file that is not what it should be adds no reason."""
        try:
            out = self._rethink_reasons(to, planned)
        except Exception as e:  # noqa: BLE001 -- advisory text: the back-edge is taken without it
            out = [f"the reasons could not be read ({type(e).__name__})"]
        if to == "SPEC" and not (self.st.tries.get("spec_amendment") or spec_signed_once(self.run_dir)):
            self.st.tries["spec"] = 0
            if out:
                self.st.tries["spec_feedback"] = [f"rethink after review round {self.st.round}: {x}" for x in out]
        elif to == "ENGINE_CHOICE":
            self.st.tries["rethink_engine"] = {"engine": self.st.engine, "round": self.st.round, "why": out}
        return self._go(to, why)

    def _rethink_reasons(self, to: str, planned: bool) -> list[str]:
        import yaml
        import validate_critique_yaml as V
        g, out, named, seen = self.st.last_gate or {}, [], set(), set()
        if not planned:
            if g.get("action") == "RETHINK_STRUCTURAL" and g.get("rationale"):
                out.append(f"gate: {g['rationale']}")
            for reviewer in V.FILENAMES:           # every reviewer the gate may have read
                try:
                    doc = yaml.safe_load(open(os.path.join(self.run_dir, V.FILENAMES[reviewer])))
                    sm = doc.get("structural_mismatch") or {}
                    if sm.get("detected"):
                        out.append(f"{reviewer}: {sm.get('description')}")
                    for b in doc.get("blockers") or []:       # its own text: not every reviewer's is in the ledger
                        if isinstance(b, dict) and b.get("category") == "STRUCTURAL" and not b.get("resolved"):
                            out.append(f"{b.get('id')}: {b.get('claim')}" + (f" Required: {_remedy(b)}" if _remedy(b) else ""))
                            seen.add(str(b.get("id")))
                except Exception:  # noqa: BLE001 -- the gate already judged the file; here it only adds reasons
                    continue
        else:
            try:
                plan = json.load(open(os.path.join(self.run_dir, f"fix_plan_r{self.st.round}.json")))["plan"]["tasks"]
            except Exception:  # noqa: BLE001
                plan = []
            for t in plan if isinstance(plan, list) else []:
                if isinstance(t, dict) and t.get("action") == "rethink" and t.get("owner_state") == to:
                    out.append(f"fix plan: {t.get('brief')}")
                    named |= {str(x) for x in (t.get("findings") if isinstance(t.get("findings"), list) else [])}
        try:
            led = FL.open_findings(FL.load(self.run_dir))
        except Exception as e:  # noqa: BLE001 -- the reasons read so far are kept
            led, out = [], out + [f"the findings ledger could not be read ({type(e).__name__})"]
        out += [f"{f.id}: {f.claim}" + (f" Required: {f.fix_requires}" if f.fix_requires else "")
                for f in led if (f.id in named or f.owner_state == to) and f.id not in seen]
        return [" ".join(str(x).split())[:600] for x in out][:20]

    def rebuild(self, run_dir: str) -> dict:
        """The S1 pipeline, then the report again when a draft exists (a write-up fix edits
        manuscript.md; its check must read the re-rendered report)."""
        import pipeline as P
        rep = P.run(run_dir)
        if os.path.exists(os.path.join(run_dir, "manuscript.md")):
            render_report(run_dir, self.backend)
        return rep

    # -------------------------------------------------- one step
    def step(self) -> str:
        s = self.st.state
        if s == "FINAL":
            return s
        if self.st.paused == AWAITING_SPEC_AMENDMENT:      # a state saved before the flag existed keeps its pause
            self.st.tries["spec_amendment"] = True
        self.st.paused = None
        if s == "SPEC" and (self.st.tries.get("spec_amendment") or spec_signed_once(self.run_dir)):
            return self.h_SPEC()                 # the amendment pause costs no model call (Codex review of #261 P1-3)
        # PRESENTATION is not checked here: its one task is a write-up task, and _task replays a completed review
        # before it looks at the budget (h_PRESENTATION takes the budget path when there is no allowance).
        if s in LLM_STATES and s != "PRESENTATION" and self._out_of_budget():
            return self._budget_out(s)
        try:
            return getattr(self, "h_" + s)()
        except AwaitingBudget:            # _task recorded the pause; the journal makes the handler safe to re-run
            return s

    def run(self, max_steps: int = 200) -> str:
        if self.st.state in POST_CHOICE_STATES:     # F6 (#218): a resume past ENGINE_CHOICE re-checks, before compute
            miss = evidence_gate(self.run_dir, signoff=self.st.signoff)
            if miss:
                _withhold_report(self.run_dir)       # never deliverable under run_failed (narrow Codex review of #272)
                self._go("FINAL", "the run's preconditions do not hold on resume: " + "; ".join(miss[:5]), "run_failed")
                save_state(self.run_dir, self.st)
                return self.st.status or self.st.state
        for _ in range(max_steps):
            before = (self.st.state, len(self.st.history))
            self.step()
            if self.st.state == "FINAL" or self.st.paused:
                break
            if (self.st.state, len(self.st.history)) == before:
                raise RuntimeError(f"state {self.st.state} made no progress")
        if self.st.paused and self.st.paused != AWAITING_BUDGET and self.st.state != "FINAL":   # _task: budget's own
            # the harness benchmark protocol (I1, owner-wait) reads every pause from history, not only the budget one
            self.st.history.append({"at": _now(), "paused": self.st.paused, "state": self.st.state,
                                    "round": self.st.round, "spent_usd": round(self.st.spent_usd, 4)})
        save_state(self.run_dir, self.st)
        return self.st.status or (f"paused:{self.st.paused}" if self.st.paused else self.st.state)

    # -------------------------------------------------- handlers
    def h_SPEC(self):
        if self.st.signoff == "lead" and (self.st.tries.get("spec_amendment") or spec_signed_once(self.run_dir)) \
                and not getattr(self, "_in_amend_lock", False):
            # owner decision on #330 (Codex r3): the request is written only under the amendment lock, so a resumer that
            # loses the lock writes nothing
            with _amend_lock(self.run_dir):
                self._in_amend_lock = True
                try:
                    return self.h_SPEC()
                finally:
                    self._in_amend_lock = False
        if self.st.tries.get("spec_amendment") or spec_signed_once(self.run_dir):
            # F6 run 3 (gap 2): a rethink of a signed spec is the owner's amendment, never a redraft in this run (an
            # amended spec cannot be re-signed in a signed run dir): the request, then a pause; the owner amends and
            # relaunches with reuse
            req = rethink_request(self.run_dir, self.st.round, self.st.signoff)
            sha = _spec_signed_sha(self.run_dir)
            ev = "present" if sha not in (None, "unreadable") else ("unreadable" if sha else "missing")
            if req.get("signing_evidence") != ev:        # a warning on the request, never a block (lead follow-up)
                req = dict(req, signing_evidence=ev)
                _write_json(self.run_dir, SPEC_RETHINK_REQUEST, req)
            if ev != "present":
                self.log(f"[machine] WARNING: the spec's sign-off record is {ev}; the amendment pause holds, but the "
                         "relaunch must start from a signed spec")
            if self.st.signoff == "lead" and ev == "present":
                done = self._lead_amend(req)
                if done:
                    return done
            self.st.tries["spec_amendment"] = True     # honoured on resume, whatever a restored copy lost (#261 P1-2)
            self.st.paused, self.st.status = AWAITING_SPEC_AMENDMENT, None
            save_state(self.run_dir, self.st)
            self.log(f"[machine] SPEC rethink of a signed spec: nothing redrafted. Read {SPEC_RETHINK_REQUEST} "
                     f"({len(req['findings'])} finding(s)); amend the spec and relaunch with reuse")
            return "SPEC"
        n = self.st.tries.get("spec", 0)
        if n >= MAX_SPEC_TRIES:
            return self._go("FINAL", "the spec did not validate", "run_failed")
        rd = self.run_dir
        stage_inputs(rd, self.st.question)
        fb = self.st.tries.get("spec_feedback")
        # The key is fixed before the call and the try is counted only once the call returns, so an
        # interrupted draft resumes under the same key (charged once by _settle_interrupted).
        res = self._task(f"SPEC#{self._visit()}:try{n + 1}", "draft_spec", self.st.question, rd, fb)
        self.st.tries["spec"] = n + 1
        if res is None:
            return self._budget_out("SPEC")
        errs = [f"draft_spec failed: {res.note}"] if not res.ok else []
        # baseline hb1: a try cut off (the turn cap) is checked on whatever draft is on disk, written or repaired in
        # this try or left by an earlier one: the checks say what holds now, with no before/after comparison to get
        # wrong on a resume or a rewrite (Codex r1 P1-3/P1-4, r2 P1-5/P1-7/P2-8). Only when the try's feedback is
        # what these checks wrote (or none): reasons they cannot verify (a rethink, a refused sign-off, the evidence
        # stage) are never cleared by an old draft that passes them; they are carried (Codex r3 P1-2).
        recheckable = (not fb or fb == self.st.tries.get("spec_feedback_checked")
                       or all(str(x).startswith("draft_spec failed:") for x in fb))   # r1 P1-5: a run saved before
        drafted = res.ok or (recheckable and os.path.lexists(os.path.join(rd, AB.SPEC_FILE)))
        if drafted:
            try:
                errs = self._spec_checks(rd)
                if not errs and self.st.derived_inputs:
                    self._write_sheet(rd)
            except Exception as e:  # noqa: BLE001
                errs = [f"spec unreadable: {e}"]
            if errs and not res.ok:
                errs = [f"draft_spec failed: {res.note} (its draft was checked)"] + errs
        if errs:
            self.st.tries["spec_feedback"] = _carry_feedback(errs, fb if not drafted else None)
            if drafted or recheckable:
                # these checks' own output (or, with no draft on disk, what they wrote before plus that note): a capped
                # next try may be re-checked against it (Codex r4 P1-3: a first try with no draft blocked every later one)
                self.st.tries["spec_feedback_checked"] = list(self.st.tries["spec_feedback"])
            else:
                self.st.tries.pop("spec_feedback_checked", None)
            return self._go("SPEC", f"spec invalid: {'; '.join(errs)[:200]}")
        self.st.tries.pop("spec_feedback", None)
        self.st.tries.pop("spec_feedback_checked", None)
        if not self._review_conditions(self.st.signoff, defer=False):
            # Codex narrow round P1: the conditions exist before the run ever asks for a signature (owner 2026-11-21)
            self.st.tries["spec_feedback"] = ["review conditions could not be prepared for signing"]
            return self._go("SPEC", "review conditions could not be prepared for signing")
        return self._go("EVIDENCE", "spec drafted and valid")

    def _spec_checks(self, rd: str) -> list[str]:
        """Every pre-sign check class on the draft, each run whatever the others found, so one try hears every problem
        (baseline hb1: the chain stopped at the first class that failed, and MAX_SPEC_TRIES is small). A class that
        raises is named in the problems and the others still run. A spec or settings file that is a link (symbolic or
        hard) or not a regular file, or a spec that does not parse, is the problem and nothing else runs (Codex r4
        P1-5: the checks read these files by path)."""
        import yaml
        bad = _draft_file_problems(rd) + _special_files(rd)     # before anything opens a file (Codex r1 on #350)
        if bad:
            return bad
        try:
            yaml.safe_load(open(os.path.join(rd, AB.SPEC_FILE)))
            from toolkit.analysis import conflicts as CF     # owner default D5: declare, test and continue
            if CF.ensure_default(rd):                        # after the link checks: it replaces the file
                self.log("[machine] the draft states no conflicts policy: declare_and_test (owner default D5)")
        except Exception as e:  # noqa: BLE001
            return [f"spec unreadable: {type(e).__name__}: {e}"]
        out: list[str] = []

        def run(name, fn) -> bool:
            try:
                got = list(fn() or [])
            except Exception as e:  # noqa: BLE001 -- fail closed, and the other classes still run
                got = [f"the {name} check could not run: {type(e).__name__}: {e}"]
            out.extend(x for x in got if x not in out)
            return not got
        if self.st.derived_inputs:
            run("derivation", lambda: self._derive(rd))
        run("cited-input", lambda: record_cited_inputs(rd))      # records the hashes before a check reads a cited file
        outside = self._cites_outside(rd)
        if outside:             # Codex r1 P1-1: no later check opens a file the cited-input check refused
            out.extend(x for x in outside if x not in out)       # r2 P1-2: never empty, so never a pass
            return out
        if self.st.derived_inputs:
            # Workstream B's schema runs the derived-input checks itself (and per candidate engine); the spike adapter
            # does not know them, so the harness runs them for it.
            if not hasattr(self.backend, "candidate_rows"):
                import pipeline as P
                run("derived-input", lambda: [f"{p['code']}: {p['claim']}" for p in P.derivation_problems(
                    rd, self._spec(rd), required=True)])
            signable = run("pre-sign", lambda: self.backend.pre_sign_problems(rd, derived=True))
        else:
            signable = run("pre-sign", lambda: self.backend.pre_sign_problems(rd))
        run("geometry", lambda: self._geometry_problems(rd))
        if signable and run("signed-configuration", lambda: self._config_problems(rd)):
            # Codex r1 P1-2, r2 P1-3: the assessment builds the engine, only after the pre-sign guards have passed; the
            # configuration guard's own problems go back to the drafter (r3 P1-1)
            run("evidence", lambda: self._evidence_assess(rd)[1]["spec_problems"])   # tb R2: heard one stage late
        return out

    @staticmethod
    def _config_problems(rd: str) -> list[str]:
        """The signed engine configuration's guard (analysis_backend.signed_config_problems) on the draft, whatever the
        backend: no engine is built from a draft it refuses (an error is reported by _spec_checks, fail closed)."""
        import yaml
        return list(AB.signed_config_problems(yaml.safe_load(open(os.path.join(rd, AB.SPEC_FILE))) or {}))

    @staticmethod
    def _cites_outside(rd: str) -> list[str]:
        """The spec's citations no later check may open, as problems: one that resolves outside the run directory
        (record_cited_inputs refuses it), or one that exists but is neither a regular file nor a directory (a FIFO or a
        device blocks the reader: Codex r4 P1-4). Fail closed: when the citations cannot be listed, that is the
        problem."""
        import stat
        import yaml
        import pipeline as P
        try:
            spec = yaml.safe_load(open(os.path.join(rd, AB.SPEC_FILE))) or {}
            root, out = os.path.realpath(rd), []
            for r in P.cited_paths(spec, rd):
                p = os.path.realpath(os.path.join(rd, r))
                if not p.startswith(root + os.sep):
                    out.append(f"the spec cites {r}, outside the run directory: copy it into data/")
                    continue
                try:
                    mode = os.stat(p).st_mode
                except FileNotFoundError:
                    continue                    # record_cited_inputs says it does not exist; nothing can open it
                if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                    out.append(f"the spec cites {r}, which is not a regular file: write the table as a plain file")
            return out
        except Exception as e:  # noqa: BLE001
            return [f"the spec's cited paths could not be listed, so no check reads them: {type(e).__name__}: {e}"]

    def _evidence_assess(self, rd: str) -> tuple[dict, dict]:
        """The spec and the evidence stage's assessment of it (toolkit/spec/evidence_draft.py assess), with a dossier
        path that the harness, the gate or a task writes added to its spec problems. SPEC and EVIDENCE share it."""
        from toolkit.spec import evidence_draft as EDR
        raw = EDR._spec(rd)
        a = EDR.assess(rd, raw)
        rel = a.get("rel")
        if rel and not a.get("draftable") and owned_path(rd, os.path.normpath(rel).replace(os.sep, "/"), tuple(
                p for p in harness_owned() if p not in ("data/*", EDR.ED.DOSSIER_FILE))):
            a["spec_problems"] = a["spec_problems"] + [   # Codex r2 P1-5: a file the harness or a task writes
                f"evidence.dossier {rel!r} is a file the harness, the gate or a task writes: name "
                f"{EDR.ED.DOSSIER_FILE}"]
        return raw, a

    def _lead_amend(self, req: dict) -> str | None:
        """The lead amendment under the parent's amendment lock (owner decision on #330): the request and the parent's
        state are read again once the lock is held, so a run another process already handed to its child, or is
        amending now, stops with AmendmentLocked and nothing written."""
        with _amend_lock(self.run_dir):
            disk = load_state(self.run_dir)
            if disk is not None and disk.status == AMENDED_PROVISIONAL:
                raise AmendmentLocked(f"{self.run_dir} was already handed to its child by another process: this "
                                      "resume stops with nothing written", run_dir=self.run_dir)
            try:
                cur = json.load(open(os.path.join(self.run_dir, SPEC_RETHINK_REQUEST)))
            except (OSError, ValueError):
                cur = None
            return self._lead_amend_locked(cur if isinstance(cur, dict) else req)

    def _lead_amend_locked(self, req: dict) -> str | None:
        """lead-gaps β (S7 under --signoff lead only): the rethink's machine-readable patch is applied by toolkit/spec/
        amend.py (--from-request) to a child run, the child's spec is signed by the lead, provisionally, and this run
        ends `amended_provisional`; run_v2 continues in the child, whose report heads with the amendment. None (the
        run pauses for the owner, as before) when there is no patch, it does not apply or validate, the amended spec
        would not sign, the parent's signature no longer holds, no budget is left, this run already continues a
        provisional amendment, or an earlier attempt was refused. The child's path is recorded before it is made, so
        an interrupted handoff is finished on resume, never repeated (Codex r1)."""
        from toolkit.spec import amend as AM, signoff as SO
        la, why, rec = req.get("lead_amendment") or {}, None, None
        child = la.get("child")
        if la.get("status") in ("started", "signed_provisionally"):
            # resolved beside this run only (a moved copy: r2); never the recorded path, which may be the original tree
            # of a restored copy (Codex r3 on #342, P1-5); and only the child this request made (Codex r1 of #349)
            child = _child_of(self.run_dir, child)
            child = child if child and _made_by(child, req, self.run_dir) else None
        if la.get("status") == "refused":
            return None                   # the first refusal stands, with its reason and any kept child
        if any(os.path.lexists(os.path.join(self.run_dir, f)) for f in LEGACY_BRIEF_SIGNOFF):
            # owner decision on #330: in v2 a legacy decision-brief signature (which may bind other files than the spec)
            # never satisfies the lead's check, also when recovering an interrupted hand-off (Codex r3); the owner decides
            why = "the parent carries a legacy decision brief signature, which a lead amendment never relies on in v2"
        elif la.get("status") in ("started", "signed_provisionally"):
            # an interrupted handoff: finished when the parent's signature still holds (Codex r3 on #342, P1-6) and the
            # child is signed by the lead, else refused (the owner decides)
            if self._signed_by() is None:
                why = "the parent's sign-off no longer holds (a signed input changed since): the owner decides"
            elif not (child and os.path.isfile(os.path.join(child, STATE_FILE)) and SO.status(child) == "signed"
                    and (SO._spec_record(child) or {}).get("signed_by") == "lead"):
                why = "an interrupted amendment left no lead-signed child"
            else:
                try:
                    rec = json.load(open(os.path.join(child, AMENDMENT_RECORD)))
                except (OSError, ValueError) as e:
                    why = f"the interrupted amendment's record cannot be read ({type(e).__name__})"
        elif not isinstance(req.get("patch"), dict):
            why = "the rethink wrote no machine-readable patch"
        elif self._provisional_amendment():
            why = "this run continues a provisional amendment; a second one waits for the owner"
        elif not callable(getattr(self.tasks, "for_run_dir", None)):
            why = "the run's tasks cannot continue in a child run"
        elif self._signed_by() is None:
            why = "the parent's sign-off no longer holds (a signed input changed since): the owner decides"
        elif isinstance(self.st.budget_usd, (int, float)) and round(self.st.budget_usd - self.st.spent_usd, 4) <= 0:
            why = "no budget is left for a child run"
        else:
            child = AM._next_child(self.run_dir)
            _write_json(self.run_dir, SPEC_RETHINK_REQUEST, dict(req, lead_amendment={"at": _now(), "status": "started",
                                                                                      "child": child}))
            self.st.tries["spec_amendment"] = True
            self.st.paused, self.st.status = AWAITING_SPEC_AMENDMENT, None     # amend amends a paused or finished run
            save_state(self.run_dir, self.st)
            try:
                rec = AM.amend(self.run_dir, from_request=True, out=child, signoff="lead", log=lambda *a: None)
                if not (rec.get("dry_sign") or {}).get("ok"):
                    why = f"the amended spec would not sign: {(rec.get('dry_sign') or {}).get('error')}"
                else:
                    self._mark_child(child, rec.get("id"))     # before the signature: a signed child always says so (r2)
                    SO.sign(child, "lead", f"Provisional (--signoff lead): amendment {rec['id']} of "
                            f"{self.run_dir}, made from its rethink request (round {req.get('round')}); the owner has "
                            f"not reviewed it. Reason: {' '.join(str(rec.get('reason')).split())[:500]}",
                            expect_sha256=rec["spec_sha256"], expect_sheet_sha256=rec["sheet_sha256"],
                            expect_derivations_sha256=rec["derivations_sha256"])
            except Exception as e:  # noqa: BLE001 -- AmendError (refused, no child) or a signing refusal: owner pause
                why = f"{type(e).__name__}: {e}"
        if why is None:
            try:
                self._mark_child(child, rec.get("id"))
            except Exception as e:  # noqa: BLE001 -- an unreadable child state: refused, the owner decides (r2)
                why = f"the child's run state cannot be read ({type(e).__name__})"
        kept = child if child and os.path.isdir(child) else None
        req = dict(req, lead_amendment={"at": _now(), "status": "refused" if why else "signed_provisionally",
                                        **({"why": why[:2000]} if why else {"id": rec.get("id")}),
                                        **({"child": kept} if kept else {})})
        _write_json(self.run_dir, SPEC_RETHINK_REQUEST, req)
        if why:
            self.log(f"[machine] the lead does not amend the signed spec ({why[:300]}): the run waits for the owner")
            return None
        self.st.paused = None
        # the child is bound to the parent's final state before that state is saved, so no interruption leaves a FINAL
        # parent whose child binds its paused state (Codex r3 on #342, P1-4); until that save the parent is at SPEC and
        # a resume finishes the hand-off again
        # a failure before the parent's FINAL state is saved (its metadata, the lineage) puts this machine and the
        # parent's metadata back as they were (Codex r2 and r3 of #349): a retry, on this machine or a new one, finishes
        # the hand-off
        import copy
        saved = copy.deepcopy(self.st)
        meta_p = os.path.join(self.run_dir, "metadata.json")
        meta0 = open(meta_p, "rb").read() if os.path.isfile(meta_p) else None
        try:
            return self._go("FINAL", f"provisional amendment {rec.get('id')} by the lead: the run continues in {child}",
                            AMENDED_PROVISIONAL, extra={"amended_to": child}, before_save=lambda: self._relink(child))
        except BaseException:
            disk = _state_or_none(self.run_dir)
            if disk is None or disk.status != AMENDED_PROVISIONAL:
                self.st = saved
                from lib.safe_io import remove_nofollow, write_nofollow
                if meta0 is not None:
                    write_nofollow(self.run_dir, "metadata.json", meta0, atomic=True)
                else:
                    remove_nofollow(self.run_dir, "metadata.json")
            raise

    def _mark_child(self, child: str, rid) -> None:
        """The child's state, once: not paused, what is left of the cap (always > 0 here), the handoff event."""
        cst = load_state(child)
        if cst is None:
            raise ValueError("no run state")
        if not any(isinstance(h, dict) and h.get("provisional_amendment") for h in cst.history):
            cst.paused = None
            if isinstance(self.st.budget_usd, (int, float)):    # one cap: the child gets what is left
                cst.budget_usd = round(self.st.budget_usd - self.st.spent_usd, 4)
            cst.history.append({"at": _now(), "provisional_amendment": rid, "by": "lead", "parent_run": self.run_dir})
            save_state(child, cst)

    def _provisional_amendment(self) -> bool:
        """This run was made by a lead's provisional amendment: its amendment record not signed by the owner, or the
        handoff event in its history (kept when the record is lost: Codex r1)."""
        return (os.path.lexists(os.path.join(self.run_dir, AMENDMENT_RECORD)) and self._signed_by() != "owner") or \
            any(isinstance(h, dict) and h.get("provisional_amendment") for h in self.st.history)

    def _relink(self, child: str) -> None:
        """The child's parent_run binds the parent's final run_state.json (amend recorded it while paused), so the
        chain still links (scripts/harness_metrics.py chains): the bytes about to be saved, from the state in memory. A
        failure is raised before the parent is saved FINAL, so a resume finishes the hand-off (Codex r1 of #349)."""
        sha = hashlib.sha256(_state_bytes(self.st)).hexdigest()
        for rel in ("metadata.json", AMENDMENT_RECORD):
            doc = json.load(open(os.path.join(child, rel)))
            if isinstance(doc.get("parent_run"), dict):
                doc["parent_run"]["run_state_sha256"] = sha
                _write_json(child, rel, doc)

    def _derive(self, rd: str | None = None) -> list[str]:
        """The draft's declared derivations, run by the harness (scripts/pipeline.py derive). Their problems go back
        to the planner with the script's stderr tail: the planner never needs a shell to learn whether its code
        works."""
        import pipeline as P
        rep = P.derive(rd or self.run_dir, unsandboxed_ok=self.st.derive_unsandboxed)
        return [f"{p['code']}: {p['claim']}" for p in rep["problems"]]

    def _write_sheet(self, rd: str | None = None) -> None:
        """The sign-off sheet, with the source hashes it was made from, published with no link followed (review
        round 3: signoff.write_sheet)."""
        from toolkit.spec import signoff
        signoff.write_sheet(rd or self.run_dir, getattr(self.backend, "candidates", None))

    def h_SIGN(self):
        mode = self.st.signoff
        from toolkit.spec import evidence_draft as EDR
        if not self._signed_before() and EDR.read_record(self.run_dir) is None:
            # Codex r1 P2-6: an unsigned run saved at SIGN under the earlier order has had no evidence stage
            return self._go("EVIDENCE", "earlier order: the evidence stage runs before sign-off")
        if mode == "none":
            return self._go("ENGINE_CHOICE", "sign-off not required (--signoff none)")
        by = self._signed_by()
        if not by and not self._review_conditions(mode):
            self.st.paused, self.st.status = "awaiting_signoff", None
            save_state(self.run_dir, self.st)
            return "SIGN"
        # A HIGH geometry finding the spec proposes as an exception is the owner's to approve (review of #131, P1):
        # a lead sign-off approves nothing, so the run waits for the owner here, as for a spec sign-off.
        pend = self._geometry_owner_pending()
        if by and (mode == "lead" or by == "owner") and not pend:
            return self._go("ENGINE_CHOICE", f"spec signed by {by}")
        if pend:
            how = ("python3 toolkit/spec/geometry.py approve" if by else "python3 agents/stage_machine.py sign")
            self.log(f"[machine] {len(pend)} HIGH geometry exception(s) wait for the owner ({', '.join(pend[:4])}): "
                     f"{how} {self.run_dir} --by owner")
        elif mode == "lead" and self._signed_before():
            # Codex r1 P1-1: a signature that no longer holds (the dossier or an input changed since) is never replaced
            # by a lead's: the record stays, and the owner re-signs
            self.st.paused, self.st.status = "awaiting_signoff", None
            save_state(self.run_dir, self.st)
            self.log("[machine] the spec's sign-off no longer holds (an input changed since, or the record is lost); "
                     "the record stays: restore the signed inputs, or amend the spec and relaunch (the owner signs)")
            return "SIGN"
        elif mode == "lead":
            res = self._task(f"SIGN#{self._visit()}:sign_spec", "sign_spec", self.run_dir)
            note = (_signoff(self.run_dir) or {}).get("note", "")
            if res is None:                 # Codex r2 P2-3: no allowance is the budget path, not an owner pause
                return self._budget_out("SIGN")
            if res.ok:
                if os.path.exists(os.path.join(self.run_dir, SIGNOFF_FILE)) and getattr(self.backend, "sign", None):
                    from lib.safe_io import remove_nofollow
                    remove_nofollow(self.run_dir, SIGNOFF_FILE)   # the lead's note; B writes its own record
                try:
                    self._sign("lead", (note + " " + self._evidence_note()).strip())   # the harness records the hash
                    return self._go("ENGINE_CHOICE", "spec signed by the lead")
                except ValueError as e:
                    self.log(f"[machine] the spec could not be signed: {e}")
                    back = self._sign_repair(str(e))
                    if back:
                        return back
        self.st.paused = "awaiting_signoff"
        self.st.status = None
        save_state(self.run_dir, self.st)
        self.log(f"[machine] waiting for the owner: python3 agents/stage_machine.py sign {self.run_dir} --by owner")
        return "SIGN"

    def _signed_before(self) -> bool:
        """The run's spec was signed: a sign-off record binds it (current or not), or the history records a signature
        (a record lost since does not make the run a draft again: Codex r1 P1-3)."""
        return spec_signed_once(self.run_dir) or any(
            h.get("from") == "SIGN" and str(h.get("why", "")).startswith("spec signed by") for h in self.st.history
        ) or os.path.lexists(os.path.join(self.run_dir, "decision_brief.signed", AB.SPEC_FILE))   # a frozen copy (r2)

    def _sign_repair(self, why: str) -> str | None:
        """v2 autonomy 1B-ii: a refused lead sign-off goes back with the refusal as feedback, to EVIDENCE when the
        evidence dossier is what was refused, else to SPEC (one more draft allowed), at most MAX_SIGN_REPAIRS times;
        then the run waits for the owner. The check is not loosened: the next SIGN runs it again on the files."""
        n = self.st.tries.get("sign_repair", 0)
        if n >= MAX_SIGN_REPAIRS:
            return None
        self.st.tries["sign_repair"] = n + 1
        if "evidence dossier" in why:
            self.st.tries["evidence_feedback"] = [why[:2000]]
            return self._go("EVIDENCE", f"sign-off refused (evidence dossier): repair {n + 1} of {MAX_SIGN_REPAIRS}")
        self.st.tries["spec_feedback"] = [why[:2000]]
        self.st.tries["spec"] = min(self.st.tries.get("spec", 0), MAX_SPEC_TRIES - 1)
        return self._go("SPEC", f"sign-off refused: repair {n + 1} of {MAX_SIGN_REPAIRS}")

    def _evidence_note(self) -> str:
        """The evidence stage's declared gaps, for the lead's sign-off note (counts; the record holds the inputs)."""
        from toolkit.spec import evidence_draft as EDR
        rec = EDR.read_record(self.run_dir) or {}
        if rec.get("status") in (None, "not_required"):
            return ""
        if rec.get("status") == "unreadable" or not EDR.record_current(self.run_dir, rec):
            return (f"Evidence dossier: provisional, not reviewed by the owner; {EDR.RECORD} could not be read or no "
                    "longer describes the dossier.")
        g = rec.get("gaps") if isinstance(rec.get("gaps"), dict) else {}
        parts = ", ".join(f"{len(g[k])} {k}" for k, _ in EDR.GAP_TEXT if g.get(k))
        return (f"Evidence dossier {'drafted by a run task' if rec.get('drafted_by_task') else 'supplied'}: "
                f"{len(rec.get('required') or [])} input(s); provisional, not reviewed by the owner; gaps declared in "
                f"{EDR.RECORD}" + (f" ({parts})." if parts else "."))

    def _review_conditions(self, mode: str, defer: bool = True) -> bool:
        """Owner decision 2026-11-21 (W0-G): before signing, the spec critique's undeclared HIGH findings become rows of
        review_conditions.yaml; under --signoff lead each open row is deferred to the owner, provisionally (D2), and
        the run proceeds. The sheet is regenerated so it shows them. False (the run waits for the owner) when the
        import cannot run: a signature never goes ahead without it."""
        from toolkit.spec import review_conditions as RC
        try:
            n = RC.import_critique(self.run_dir)
            d = RC.lead_defer(self.run_dir) if mode == "lead" and defer else 0
            sp_ = os.path.join(self.run_dir, SIGNOFF_SHEET)
            if os.path.exists(sp_):     # whenever the sheet does not show the file as it now is (Codex narrow round)
                from toolkit.spec import signoff as SO
                src = SO.sheet_sources(open(sp_, encoding="utf-8", errors="replace").read()) or {}
                if src.get(RC.SHEET_KEY, "none") != RC.sha(self.run_dir):
                    self._write_sheet()
        except Exception as e:  # noqa: BLE001 -- fail closed: the owner is asked
            self.log(f"[machine] review conditions could not be prepared for signing: {type(e).__name__}: {e}")
            return False
        if n or d:
            self.log(f"[machine] review conditions: {n} spec-critique finding(s) added"
                     + (f", {d} deferred to the owner by the lead (D2)" if d else ""))
        return True

    def h_EVIDENCE(self):
        """v2 autonomy 1B-ii (D1): the evidence dossier, before SIGN. No decision inputs needed: a pass with no task.
        Otherwise the harness scaffolds each missing entry with what execution uses (toolkit/spec/evidence_draft.py)
        and the bounded draft_evidence task fills the evidence, at most EDR.MAX_TRIES times in the run, each time with
        the signer's check's problems; then the record (evidence/dossier_draft.json) and the sign-off sheet, which
        binds the dossier's sha256. A spec signed under the earlier order (SIGN before EVIDENCE) is left as signed."""
        from toolkit.spec import evidence_draft as EDR
        rd = self.run_dir
        if self._signed_before():       # Codex r1 P1-2/3: SIGN checks the signature, its signer and the approvals
            return self._go("SIGN", "evidence: the spec was signed before this stage (earlier order); the dossier "
                                    "stays as signed")
        # the evidence service's registry (workstream V5), still a stub: the run console reads it
        _put(rd, "evidence/registry.yaml", "# evidence service stub (v2 C): no parameter has been fetched or "
             "matched yet\nstatus: stub\nentries: []\n")

        def spec_back(probs):
            self.st.tries["spec_feedback"] = probs
            return self._go("SPEC", "evidence: " + "; ".join(probs)[:200])
        try:
            raw, a = self._evidence_assess(rd)
            if a["spec_problems"]:
                return spec_back(a["spec_problems"])
            sc = EDR.scaffold(rd, a, raw)["scaffold"] if a["needed"] else {}
        except Exception as e:  # noqa: BLE001 -- fail closed: back to the draft, whose tries are bounded
            return spec_back([f"the evidence stage could not read the spec or the dossier: {type(e).__name__}: {e}"])
        notes = []
        fb = list(self.st.tries.get("evidence_feedback") or [])

        def drafts():       # Codex r1 P2-7, r2 P2-2: the drafts the journal charged (completed or interrupted)
            return sum(1 for k, j in self.st.journal.items() if k.startswith("EVIDENCE#")
                       and j.get("status") in ("completed", "interrupted"))
        if a["needed"]:
            a = EDR.assess(rd, raw)
            if a["problems"] and not a.get("draftable"):
                notes.append(f"the spec names {a['rel']}: only {EDR.ED.DOSSIER_FILE} is drafted in the run")
            while a["problems"] and a.get("draftable") and drafts() < EDR.MAX_TRIES:
                n = drafts()
                res = self._task(f"EVIDENCE#{self._visit()}:try{n + 1}", "draft_evidence", rd, a["rel"],
                                 EDR.packet(a), fb + a["problems"])
                self.st.tries["evidence"] = drafts()
                if res is None:
                    return self._budget_out("EVIDENCE")
                notes.append(f"try {n + 1}: " + (res.note if res.ok else f"failed: {res.note}")[:200])
                fb = []
                try:
                    EDR.scaffold(rd, a, raw)            # an entry the task dropped is scaffolded again
                    a = EDR.assess(rd, raw)
                except Exception as e:  # noqa: BLE001 -- the task left a dossier the harness cannot read
                    a = dict(a, problems=[f"the dossier cannot be read: {type(e).__name__}: {e}"])
        self.st.tries.pop("evidence_feedback", None)
        rec = EDR.finish(rd, a, sc, drafts(), raw, notes)
        if a["needed"]:
            try:
                if getattr(self.backend, "candidates", None) is None and hasattr(self.backend, "pre_sign_problems"):
                    # Codex r1 P2-12: a resumed process has no candidate rows from SPEC; the sheet would lose them
                    # (r2 P2-7: with h_SPEC's own arguments)
                    (self.backend.pre_sign_problems(rd, derived=True) if self.st.derived_inputs
                     else self.backend.pre_sign_problems(rd))
                self._write_sheet(rd)        # the sheet records the dossier's sha256: written after the dossier
            except Exception as e:  # noqa: BLE001 -- SIGN refuses without a current sheet, and says so
                self.log(f"[machine] the sign-off sheet could not be written: {type(e).__name__}: {e}")
        if rec["status"] == "incomplete":
            self.log(f"[machine] the evidence dossier still has {len(a['problems'])} problem(s) after "
                     f"{rec['tries']} draft(s): see {EDR.RECORD}")
        return self._go("SIGN", f"evidence: {rec['status']} ({len(rec['required'])} input(s))")

    def h_ENGINE_CHOICE(self):
        try:
            spec = self.backend.choice_spec(self.run_dir)
        except Exception as e:  # noqa: BLE001 -- fail closed
            return self._go("FINAL", f"the spec cannot be read for the engine choice: {e}", "run_failed")
        rows = self.backend.eligible_engines(spec)
        eligible = [r for r in rows if r.get("eligible")]
        rec = {"at": _now(), "candidates": rows}
        if not eligible:
            _write_json(self.run_dir, "engine_choice.json", rec)
            return self._go("FINAL", "no registered engine fits the spec", "run_failed")
        rt = self.st.tries.get("rethink_engine")         # set by a RETHINK back-edge (_rethink): the engine left, and why
        res = self._task(f"ENGINE_CHOICE#{self._visit()}:pick_engine", "pick_engine", spec, rows,
                         **({"rethink": rt} if rt else {}))
        if res is None:
            return self._budget_out("ENGINE_CHOICE")
        if rt:
            rec["rethink"] = rt
            self.st.tries.pop("rethink_engine", None)
        pick = (res.output or {}).get("engine") if res.ok else None
        if pick not in {r["ref"] for r in eligible}:
            pick, why = eligible[0]["ref"], f"harness: the pick {pick!r} is not eligible; first eligible engine"
        else:
            why = (res.output or {}).get("why", "")
        cfg = next((r.get("config") for r in eligible if r["ref"] == pick), None) or {}
        miss = evidence_gate(self.run_dir, engine=(pick, cfg), signoff=self.st.signoff)
        if miss:            # F6: the signed dossier must cover the engine actually chosen, before any compute
            _write_json(self.run_dir, AB.ENGINE_CHOICE, dict(rec, evidence_refused={"engine": pick, "why": miss}))
            return self._go("FINAL", f"the signed evidence dossier does not cover {pick}'s decision inputs: "
                                     + "; ".join(miss[:5]), "run_failed")
        rec.update(AB.engine_choice_record(pick, why, rows, config=cfg))
        if self.cross and len(eligible) > 1:
            try:
                rec["cross_check"] = self.backend.cross_check(
                    spec, [pick] + [r["ref"] for r in eligible if r["ref"] != pick][:1],
                    os.path.join(self.run_dir, "analysis", "cross_check"), run_dir=self.run_dir)
            except Exception as e:  # noqa: BLE001 -- recorded; the check is optional
                rec["cross_check"] = {"error": f"{type(e).__name__}: {e}"}
        _write_json(self.run_dir, AB.ENGINE_CHOICE, rec)     # the spec stays as signed
        self.st.engine = pick
        return self._go("PREREGISTER" if self.st.world_model_stages else "CONFIGURE", f"engine {pick}")

    def h_PREREGISTER(self):
        return self._go("CONFIGURE", self._world_model("preregister"))

    def _write_pipeline(self) -> str | None:
        """models/pipeline.yaml from the backend's steps (their limits from the compute budget as it is now), or why
        it could not be written (fail closed: an unreadable budget stops the run before any compute)."""
        import yaml
        try:
            steps = self.backend.pipeline_steps(os.path.join(self.run_dir, AB.SPEC_FILE), self.run_dir)
        except Exception as e:  # noqa: BLE001
            return f"the pipeline could not be written: {type(e).__name__}: {e}"
        _put(self.run_dir, "models/pipeline.yaml", yaml.safe_dump({"version": 1, "steps": steps}, sort_keys=False))
        return None

    def reopen_interrupted_analysis(self) -> str | None:
        """--resume of a run that ended run_failed because its analysis step timed out, was killed, or lost engine
        rows to the machine (resumable_failure): back to ANALYZE with the pipeline rewritten, so the step's limit follows the compute
        budget as it is now and the engine batch keeps the rows it had finished (scripts/compute.py reuses them).
        Recorded in the history as FINAL -> ANALYZE; outside the state table on purpose, since no handler may take
        it. Returns the cause, or None when the run is not resumable (it stays as it is)."""
        cause = resumable_failure(self.run_dir, self.st)
        if cause is None:
            return None
        err = self._write_pipeline()
        if err:
            self.log(f"[machine] not resumed: {err}")
            return None
        self.st.history.append({"at": _now(), "from": "FINAL", "to": "ANALYZE", "round": self.st.round,
                                "why": f"resumed: {_RESUME_WHY[cause]}",
                                "resumed_from": self.st.status, "spent_usd": round(self.st.spent_usd, 4)})
        self.st.state, self.st.status = "ANALYZE", None
        save_state(self.run_dir, self.st)
        self.log(f"[machine] FINAL -> ANALYZE (resumed after an analysis {cause})")
        return cause

    def h_CONFIGURE(self):
        import pipeline as P
        errs = self.backend.validate(os.path.join(self.run_dir, AB.SPEC_FILE))
        if errs:
            return self._go("FINAL", f"spec invalid for the engine: {'; '.join(errs)[:200]}", "run_failed")
        err = self._write_pipeline()
        if err:
            return self._go("FINAL", f"configure failed: {err}"[:300], "run_failed")
        steps, _errs = P.load(self.run_dir)
        if not any(s_.get("id") == "configure" for s_ in steps):    # workstream B: one analyze step
            return self._go("ANALYZE", "pipeline written (no separate configure step)")
        rep = P.run(self.run_dir, only="configure")
        if not rep.get("ok"):
            return self._go("FINAL", f"configure failed: {_rep_error(rep)}", "run_failed")
        return self._go("ANALYZE", "engine batch built")

    def h_ANALYZE(self):
        rep = self.rebuild(self.run_dir)
        if not rep.get("ok"):
            cause = analysis_failure_cause(rep, self.run_dir)
            why = f"analysis failed: {_rep_error(rep)}" + ("; resumable with --resume" if cause else "")
            ref = None if cause else calibration_refusal(rep, self.run_dir)
            if ref is not None and not conflicts_policy_stop(self.run_dir):
                # eval baseline: a refused calibration (a ridge, a fit pinned at its bounds) is a finding about the
                # spec's calibration, not the end of the run: it goes to the spec revision with the refusal reason and
                # the parameters at a bound, at most MAX_CALIBRATION_REFUSALS times (an unsigned spec is redrafted
                # with it; a signed one is the owner's to amend: h_SPEC). conflicts.policy: stop keeps the old stop.
                n = int(self.st.tries.get(CALIBRATION_REFUSED, 0) or 0)
                if n < MAX_CALIBRATION_REFUSALS:
                    self.st.tries[CALIBRATION_REFUSED] = n + 1
                    record_calibration_refusal(self.run_dir, ref, self.st.round)
                    return self._rethink("SPEC", f"calibration refused ({ref['code']}; try {n + 1} of "
                                                 f"{MAX_CALIBRATION_REFUSALS}): {ref['reason']}"[:300])
                why += f"; the calibration was refused {n} time(s) before (cap {MAX_CALIBRATION_REFUSALS})"
            return self._go("FINAL", why, "run_failed", extra={"resumable": cause} if cause else None)
        return self._go("SCORE" if self.st.world_model_stages else "CRITIQUE", "analysis built")

    def h_SCORE(self):
        return self._go("CRITIQUE", self._world_model("score_run"))

    def _world_model(self, fn: str) -> str:
        """§14, fail soft: a world-model step never raises into the machine. `refresh` (before CRITIQUE and WRITE)
        scores again when the fix loop has rebuilt the analysis since; it is recorded in the history."""
        if not self.st.world_model_stages:
            return ""
        try:
            import world_model_stage as WMS
            why = getattr(WMS, fn)(self) or ""
        except Exception as e:  # noqa: BLE001
            why = f"world model: {fn} failed: {type(e).__name__}: {e}"[:300]
        if why and fn == "refresh":
            self.st.history.append({"at": _now(), "world_model": why})
        return why

    def h_CRITIQUE(self):
        self._world_model("refresh")
        if self.st.round_pending:
            self.st.round += 1
            self.st.round_pending = False
        if self.st.round > self.st.max_rounds:
            return self._go("FINAL", "round cap passed (gate did not stop the run)", "completed_gate_error")
        v = self._visit()
        fb = self.st.tries.get("schema_feedback") or {}
        critics = list(CRITICS) if (not fb or "*" in fb) else [r for r in fb if r in CRITICS or r == "critique-presentation"]
        rereview = self._rereview_due()          # hb1 cause G: the domain and red-team critics, on the revised draft
        for reviewer in critics:
            args = (self.run_dir, reviewer, self.st.round) + ((fb.get(reviewer) or fb.get("*"),) if fb else ())
            res = self._task(f"CRITIQUE#{v}:r{self.st.round}:{reviewer}", "critique", *args)
            if res is None:
                return self._budget_out("CRITIQUE")
        self.st.tries.pop("schema_feedback", None)
        if rereview:
            self.st.tries.pop("rereview", None)
            return self._go("GATE", f"critique round {self.st.round} (re-review of the revised draft)")
        return self._go("GATE", f"critique round {self.st.round}")

    def h_GATE(self):
        pw = self.st.post_write
        waiting: list = []        # findings awaiting the owner's answer: no final disposition is published before it
        if self.st.gate_mode == "legacy":
            d = legacy_gate(self.run_dir, self.st.round, self.st.max_rounds, pw)
            # (owner amendments and declines are v2 rules: the legacy mode replays stored v1 gates and applies neither)
        else:
            sv = getattr(self.backend, "spec_violations", None)
            rv = getattr(self.backend, "registry_violations", None)
            d = v2_gate(self.run_dir, self.st.round, self.st.max_rounds, pw, self.harness_findings(),
                        spec_violations=sv(self.run_dir) if sv else None,
                        registry_violations=rv(self.run_dir) if rv else None,
                        strict_done_when=self.st.v2_rules and self.st.tries.get("schema", 0) < MAX_SCHEMA_REPAIRS,
                        append_history=False, signoff=self.st.signoff)
            opened = []
            if d.get("action") and self.st.v2_rules:
                opened = open_findings(self.run_dir, d)
                # synced with this decision, so a gate finding is read as this gate states it (Codex round 3 of #258),
                # and before the medium rule, which declares what the owner holds (hb1 cause H)
                led0 = FL.sync(self.run_dir, d, self.st.round, post_write=pw, lead_ok=self.st.signoff == "lead")[0]
                held_now = sorted(owner_accepted_now(self.run_dir, opened, self.st.signoff == "lead"))
                d = apply_medium_rule(d, opened, int(d.get("rounds_remaining") or 0), not self._out_of_budget(),
                                      owner_held=held_now)
                d["owner_held"] = held_now      # what this gate read as the owner's (FIX_LOOP's shortcut reads it here)
                d = require_judged(d, opened)
                # Codex re-review of #258 P1-11: the owner's declines hold this gate before anything is written from it
                d = apply_owner_declines(d, led0)
                waiting = sorted(set(pending_amendments(led0, d.get("resolved_by_test")) + pending_conflicts(led0)))
                if d.get("rule_matched") == "owner_declined":
                    drop_declined(self.run_dir, [h["finding"] for h in d["unresolved_high"]
                                                 if isinstance(h, dict) and h.get("kind") == "scope_declaration_declined"])
            if d.get("action"):
                import validate_critique_yaml as V
                V._append_validator_history(self.run_dir, self.st.round, d, pw)
            if self.st.v2_rules and d.get("action") == "DECLARE_SCOPE" and not waiting:
                # (not while a finding awaits the owner's answer: the gate re-runs after it; Codex r3 of #344, P1-2)
                # Every DECLARE_SCOPE (rule 3, 3a or 3m) lists what is still open in the write-up.
                write_declared(self.run_dir, opened, d)
            if self.st.v2_rules and pw and d.get("action") and self._has_draft():
                # The final status heads the report; a not-accepted one records any acceptance language. While a
                # finding awaits the owner's answer the status is pending (Codex r2 of #344, P1-1).
                fst = None if waiting else route_gate(d, pw)[1]
                render_report(self.run_dir, self.backend, status=fst or "pending")
                if fst is not None and fst not in ACCEPTED_STATUSES:
                    d["report_acceptance_language"] = self._language_check(fst)
        tag = f"gate_r{self.st.round}{'_post' if pw else ''}.json"
        _write_json(self.run_dir, tag, d or {})
        if pw and not waiting:
            _write_json(self.run_dir, "final_gate.json", d or {})
        elif pw:            # an earlier post-write gate's record is not the decision while the owner answers (Codex r3)
            from lib.safe_io import remove_nofollow
            remove_nofollow(self.run_dir, "final_gate.json")
        self.st.last_gate = {k: (d or {}).get(k) for k in ("action", "rule_matched", "rounds_remaining", "rationale")}
        self.st.last_gate.update(round=self.st.round, post_write=pw)
        if (d or {}).get("action"):
            FL.sync(self.run_dir, d, self.st.round, post_write=pw, lead_ok=self.st.signoff == "lead")
            if d.get("rule_matched") == "4m":           # these are why the loop runs: plan them
                led = FL.load(self.run_dir)
                for fid in d.get("open_tested") or []:
                    if fid in led:
                        led[fid].blocking = True
                FL.save(self.run_dir, led)
            if self.st.v2_rules:
                led = FL.load(self.run_dir)
                # Codex review of #258 P1-10: a finding whose remedy is an owner amendment goes to the owner on every
                # route, not only from the fix loop
                amend = sorted(set(pending_amendments(led, d.get("resolved_by_test")) + pending_conflicts(led)))
                if amend:
                    self._request_owner(led, amend)
                    return self._go("OWNER_DECISION", f"{len(amend)} finding(s) need an owner amendment "
                                                      f"({', '.join(amend)[:120]})")
        nxt, status, mode = route_gate(d, pw)
        if nxt == "OWNER_DECISION":
            self._request_scope(d)
        lang = (d or {}).get("report_acceptance_language") or {}
        if nxt == "FINAL" and lang.get("remaining"):
            # Not delivered with acceptance language in place (F6 S1-7): the writer repairs the draft, which then
            # goes through the whole post-write path again (presentation, every post-write check, this gate);
            # once the repairs are used, the report is withheld.
            if lang["repairs"] < MAX_LANGUAGE_REPAIRS:
                self.st.tries["language"] = lang["repairs"] + 1
                self.st.tries["language_feedback"] = lang["remaining"]
                self.st.write_mode = lang["mode"]
                # Quarantined until a repaired draft passes the whole post-write gate.
                _quarantine_report(self.run_dir)
                return self._go("WRITE", f"acceptance language in a {lang['mode']} report: the writer repairs it")
            _quarantine_report(self.run_dir)
            return self._go("FINAL", "the report kept acceptance language after its repairs: withheld",
                            "completed_no_report")
        if nxt == "CRITIQUE":                               # schema errors: re-run the critics
            n = self.st.tries.get("schema", 0) + 1
            self.st.tries["schema"] = n
            if n > MAX_SCHEMA_REPAIRS:
                return self._go("FINAL", "critique schema errors persist", "completed_gate_error")
            # Only the critics whose YAML failed are re-run, each told its own errors.
            import validate_critique_yaml as V
            by_file = {f: r for r, f in V.FILENAMES.items()}
            fb: dict = {}
            for e in d["schema_errors"]:
                f = str(e).split(":", 1)[0].strip().split()[0]
                fb.setdefault(by_file.get(f, "*"), []).append(str(e))
            self.st.tries["schema_feedback"] = fb
            return self._go("CRITIQUE", f"schema errors: {'; '.join(d['schema_errors'])[:200]}")
        self.st.tries.pop("schema", None)
        why = f"{(d or {}).get('action')} (rule {(d or {}).get('rule_matched')}){' post-write' if pw else ''}"
        if nxt in ("FIX_LOOP", "SPEC", "ENGINE_CHOICE"):
            self.st.round_pending = True
            self.st.post_write = False
        if nxt == "WRITE":
            self.st.write_mode = mode
        if nxt in ("SPEC", "ENGINE_CHOICE"):
            return self._rethink(nxt, why)
        return self._go(nxt, why, status)

    def _language_check(self, status: str) -> dict:
        """F6 S1-7: acceptance language in the whole manuscript of a report the final gate did not accept, with
        the repairs used so far (MAX_LANGUAGE_REPAIRS at most; each goes back through the post-write path)."""
        mode = "declared" if status == "completed_scope_declared" else "not_accepted"
        found = [f["claim"] for f in report_acceptance_findings(self.run_dir, mode)]
        return {"mode": mode, "repairs": int(self.st.tries.get("language", 0)), "remaining": found}

    def h_FIX_LOOP(self):
        v = self._visit()
        k = f"FIX_LOOP#{v}:spec_sha256"
        if k not in self.st.journal:                       # the reference a fix may not move
            self.st.journal[k] = {"task": "harness", "status": "completed", "output": signed_hash(self.run_dir)}
            save_state(self.run_dir, self.st)
        summ = FX.run_loop(self.run_dir, self.st.round, self.st.max_rounds, self._last_decision(),
                           _Journaled(self, f"FIX_LOOP#{v}:r{self.st.round}"),
                           rebuild=self.rebuild, harness_ids=self.harness_ids, headline=self.backend.headline,
                           budget=self._budget, log=self.log, guard=self.fix_guard, protect=PROTECTED_BY_FIX,
                           settings=getattr(self.backend, "settings_paths", AB.SETTINGS_PATHS))
        self.st.history.append({"at": _now(), "fix_loop": summ})
        if summ.get("owner_amendments"):
            # F6 run 3: their remedy is an owner amendment of the signed spec; the owner answers, no redraft runs.
            self._request_owner(FL.load(self.run_dir), summ["owner_amendments"])
            return self._go("OWNER_DECISION", f"{len(summ['owner_amendments'])} finding(s) need an owner amendment "
                                              f"({', '.join(summ['owner_amendments'])[:120]})")
        if summ.get("rethink"):
            return self._rethink(summ["rethink"], f"the planner chose a rethink ({summ['rethink']})", planned=True)
        if not summ["tasks"] and summ.get("write_up") and not summ.get("harness_gaps_blocking") and self._has_draft():
            # Every open finding is the write-up's: the writer revises the draft (with them) as the next round, and
            # the domain and red-team critics then re-review the revised draft at that round (hb1 cause G: they were
            # left on the round before, and the gate re-ran them through the schema repair).
            if self.st.round_pending:
                self.st.round += 1
                self.st.round_pending = False
            self.st.write_mode = self.st.write_mode or "not_accepted"
            self.st.tries["rereview"] = self.st.round
            return self._go("WRITE", f"every open finding is the write-up's ({', '.join(summ['write_up'])[:120]})")
        held_now = None if summ["tasks"] or self._drafted() or self._fixable_open() else self._shortcut_held()
        if held_now is not None:
            # hb1 cause H: pre-write, a loop with no task changed nothing, so another critique round can find nothing
            # new; every open finding the gate holds is the write-up's or the owner's. The draft is written, as the
            # gate's WRITE route would, in this round; the post-write gate then decides its status.
            self.st.round_pending = False
            # the post-write fix loop of this round writes its own plan: this one is kept apart (Codex r2 of #344, P1-3)
            if os.path.lexists(os.path.join(self.run_dir, f"fix_plan_r{self.st.round}.json")):
                from lib.safe_io import rename_nofollow        # moved with a source identity check (Codex r3, P1-4)
                rename_nofollow(self.run_dir, f"fix_plan_r{self.st.round}.json",
                                f"fix_plan_r{self.st.round}_prewrite.json")
            # the mode follows what this gate holds, not the whole ledger's history (Codex r2 of #344, P1-4)
            self.st.write_mode = "declared" if held_now else "accepted"
            return self._go("WRITE", "fix loop: no task; every open finding is the write-up's"
                                     + (f" or the owner's ({', '.join(held_now)[:120]})" if held_now else ""))
        return self._go("CRITIQUE", f"fix loop: {summ['rows']} task(s), {summ['passed']} passed their checks")

    def _drafted(self) -> bool:
        """The gate's own test for a draft (open_findings): a manuscript or a rendered report (Codex r1 of #344, P1-2)."""
        return self._has_draft() or os.path.exists(os.path.join(self.run_dir, "report.md"))

    def _shortcut_held(self) -> list[str] | None:
        """Codex r1 of #344 (P1-3, P2-4): the shortcut to WRITE only follows a medium-rule gate (4m) with no unresolved
        HIGH, whose tested findings are all the write-up's or the owner's answered ones; a gate that patches for any
        other reason (a HIGH, a declared finding it still holds) keeps its critique round. None: no shortcut; else the
        tested findings of that gate the owner holds."""
        try:        # this round's pre-write gate (the one that routed here; the round is raised at the next CRITIQUE)
            d = json.load(open(os.path.join(self.run_dir, f"gate_r{self.st.round}.json")))
        except (OSError, ValueError):
            return None
        if not isinstance(d, dict) or d.get("rule_matched") != "4m" or d.get("unresolved_high"):
            return None
        # the owner-held findings as this gate read them (their current fingerprint carries the accept), not the
        # ledger's status, which may keep an older remedy (Codex r3 of #344, P1-1); the medium rule kept them out of
        # open_tested, so the mode comes from that record (P2-5)
        held = d.get("owner_held")
        if not isinstance(held, list):
            return None
        led = FL.load(self.run_dir)
        ids = [str(i) for i in d.get("open_tested") or []]
        if not all(i in led and FX.is_write_up(led[i]) for i in ids):
            return None
        return sorted(map(str, held))

    def _fixable_open(self) -> list[str]:
        """Open findings a fix task could still work to: the gate holds them (blocking), and they are neither the
        write-up's nor the owner's."""
        led = FL.load(self.run_dir)
        held = owner_held(led)
        return [f.id for f in FL.open_findings(led) if f.blocking and not FX.is_write_up(f) and f.id not in held]

    def _request_owner(self, led: dict, ids: list[str]) -> None:
        """The request, and the fingerprints it asks, kept in run_state.json: the answers must be to them (Codex
        re-review of #258 P1-8), whatever request file is there later."""
        doc = request_owner_amendments(self.run_dir, self.st.round, led, ids)
        self.st.tries["owner_request"] = [i["fingerprint"] for i in doc["items"]]
        save_state(self.run_dir, self.st)

    def _request_scope(self, d: dict) -> None:
        """1F: the AWAIT_OWNER route's request (the decision-bearing items the gate marked), written with
        scope_signoff.record and its fingerprints kept in run_state.json, as _request_owner does. No item: none is
        kept, and OWNER_DECISION fails closed."""
        import scope_signoff as SS
        SS.record(self.run_dir, d, self.st.round, producer="agents/stage_machine.py GATE (decision-bearing findings)")
        try:
            req = json.load(open(os.path.join(self.run_dir, OWNER_REQUEST)))
        except (OSError, ValueError):
            req = {}
        fps = [i.get("fingerprint") for i in req.get("items") or [] if isinstance(i, dict)] \
            if isinstance(req, dict) and req.get("pending") else []
        if fps and all(fps):
            self.st.tries["owner_request"] = fps
        else:
            self.st.tries.pop("owner_request", None)
        save_state(self.run_dir, self.st)

    def h_OWNER_DECISION(self):
        """Released by the answers to every item of the pending request: the owner's (scripts/scope_signoff.py sign
        --by owner, from the owner's shell) or, under --signoff lead (D2), the lead's provisional ones, which the harness
        writes here so the run never waits (scope_signoff.provisional_answers); the machine then records them
        (harness_record), so the ledger counts them (Codex review of #258 P1-8). No recorded request: it fails closed.
        Under --signoff owner a lead's answer does not count: it waits."""
        import scope_signoff as SS
        # Codex r4 of #316: the request is read, answered, recorded and closed under the sign-off lock, so an owner's
        # sign that waited on it finds the request closed (and refuses) instead of reporting an answer never applied
        with SS.locked(self.run_dir):
            return self._owner_decision(SS)

    def _owner_decision(self, SS):
        try:
            req = json.load(open(os.path.join(self.run_dir, OWNER_REQUEST)))
        except (OSError, ValueError):
            req = None
        want = list(self.st.tries.get("owner_request") or [])
        if not want:
            # a pause with no request this machine recorded (an older run, or a route that records none): it cannot be
            # answered here, so it fails with how to go on, never waits (lead decision on Codex round 3 of #258); a
            # report an earlier gate rendered is not delivered under it (Codex round 4)
            _withhold_report(self.run_dir)
            return self._go("FINAL", "OWNER_DECISION has no recorded owner request: relaunch with reuse (a new run dir "
                                     "from this one's spec and stored rows)", "run_failed")
        answers, bad = SS.load_answers(self.run_dir)
        lead = self.st.signoff == "lead"

        def done(fp):
            a = answers.get(fp)
            return isinstance(a, dict) and _answer_counts(a, self.st.signoff) and a.get("decision") in SS.DECISIONS
        if lead and bad:        # never overwritten (Codex r1 of #316): an owner answer in it would be lost
            _withhold_report(self.run_dir)
            return self._go("FINAL", f"{SS.SIGNOFF} cannot be read, so the lead cannot answer: restore it and resume",
                            "run_failed")
        if lead and not all(done(fp) for fp in want):
            items = {i.get("fingerprint"): i for i in (req or {}).get("items") or [] if isinstance(i, dict)} \
                if isinstance(req, dict) else {}
            if any(fp not in items for fp in want):
                _withhold_report(self.run_dir)
                return self._go("FINAL", "the owner request cannot be read, so the lead cannot answer it: relaunch with "
                                         "reuse", "run_failed")
            todo = [items[fp] for fp in want if not done(fp)]
            SS.provisional_answers(self.run_dir, todo, self.st.round,
                                   plan_changing={i["fingerprint"]: _plan_changing(i) for i in todo})
            self.st.history.append({"at": _now(), "provisional": [i.get("finding") for i in todo], "by": "lead",
                                    "state": "OWNER_DECISION"})
            answers, bad = SS.load_answers(self.run_dir)
        answered = bool(want) and not bad and all(done(fp) for fp in want)
        if answered:
            SS.harness_record(self.run_dir)
            self.st.tries.pop("owner_request", None)
            who = "the lead answered provisionally" if lead and any(answers[fp].get("by") == "lead" for fp in want) \
                else "the owner answered"
            nxt = self._go("GATE", f"{who} the scope declarations")    # persisted before the request closes
            if isinstance(req, dict):
                from lib.safe_io import write_nofollow
                write_nofollow(self.run_dir, OWNER_REQUEST, json.dumps(dict(req, pending=False, answered_at=_now()),
                                                                       indent=1).encode(), atomic=True)
            return nxt
        self.st.paused = "awaiting_scope_signoff"
        save_state(self.run_dir, self.st)
        return "OWNER_DECISION"

    def h_WRITE(self):
        mode = self.st.write_mode or "not_accepted"
        self._world_model("refresh")
        fb = self.st.tries.get("language_feedback")             # a language repair (F6 S1-7)
        # F6 run 3: the write-up findings the fix loop left to this stage, with their remedies
        wf = [{"id": f.id, "claim": f.claim, "fix_requires": f.fix_requires}
              for f in FL.open_findings(FL.load(self.run_dir)) if FX.is_write_up(f)]
        res = self._task(f"WRITE#{self._visit()}:{mode}", "write", self.run_dir, mode, *([fb] if fb else []),
                         writeup=True, **({"findings": wf} if wf else {}))
        self.st.tries.pop("language_feedback", None)
        if res is None:
            return self._go("FINAL", "no budget left for the write-up", "completed_no_report")
        if not res.ok or not self._has_draft():
            return self._go("FINAL", "the writer produced no draft", "completed_no_report")
        render_report(self.run_dir, self.backend)
        self.st.post_write = True
        if mode == "not_accepted" and not self._rereview_due():
            # (a revised not-accepted draft gets its presentation review at the new round too: Codex r1 of #344, P2-5)
            return self._go("GATE", "not-accepted report written; final gate")
        return self._go("PRESENTATION", f"draft written ({mode})")

    def h_PRESENTATION(self):
        # The review of the draft is part of the write-up: like WRITE, it may use the write-up reserve (an accepted
        # run whose WRITE spent into the reserve ended budget_exhausted here, with most of the reserve unspent).
        res = self._task(f"PRESENTATION#{self._visit()}:r{self.st.round}", "critique", self.run_dir,
                         "critique-presentation", self.st.round, writeup=True)
        if res is None:
            return self._budget_out("PRESENTATION")
        if self._rereview_due():
            return self._go("CRITIQUE", f"re-review of the revised draft, round {self.st.round}")
        return self._go("GATE", "presentation review of the draft")

    def _rereview_due(self) -> bool:
        """hb1 cause G: a draft revised for write-up findings as a new round is re-reviewed by the domain and red-team
        critics at that round before the gate (set by FIX_LOOP; cleared by the re-review)."""
        return self.st.tries.get("rereview") == self.st.round and self.st.post_write

    def _last_decision(self) -> dict | None:
        for name in (f"gate_r{self.st.round - (1 if self.st.round_pending else 0)}_post.json",
                     f"gate_r{self.st.round - (1 if self.st.round_pending else 0)}.json"):
            try:
                return json.load(open(os.path.join(self.run_dir, name)))
            except (OSError, ValueError):
                continue
        return None

    def _withhold_if_acceptance_language(self) -> None:
        """F6 S1-7: whatever path ends the run, a report it did not accept is never left deliverable with
        acceptance language in it: report.md is moved to report_withheld.md."""
        rp = os.path.join(self.run_dir, "report.md")
        if (not self.st.v2_rules or self.st.status in ACCEPTED_STATUSES or not os.path.exists(rp)
                or not report_acceptance_findings(self.run_dir, "not_accepted")):
            return
        _quarantine_report(self.run_dir)
        if str(self.st.status).startswith("completed"):
            self.st.status = "completed_no_report"

    def _finish(self) -> None:
        meta_p = os.path.join(self.run_dir, "metadata.json")
        try:
            meta = json.load(open(meta_p))
        except (OSError, ValueError):
            meta = {}
        meta.update(status=self.st.status, pipeline="v2", engine=self.st.engine, rounds=self.st.round,
                    max_rounds=self.st.max_rounds, model_spend_usd=round(self.st.spent_usd, 4), finished=_now())
        _write_json(self.run_dir, "metadata.json", meta)


# ------------------------------------------------------------------ helpers
class _Journaled:
    """The machine's tasks for the fix loop, each call through the journal (Machine._task) under a
    key that is the same on resume: plan calls by their order, fix calls by task id and attempt.
    A call with no allowance left returns a failed result without being made."""

    def __init__(self, m: "Machine", prefix: str):
        self.m, self.prefix, self.n_plan = m, prefix, 0

    def plan_fixes(self, packet):
        self.n_plan += 1
        return self._call(f"{self.prefix}:plan{self.n_plan}", "plan_fixes", packet)

    def fix(self, task, run_dir, evidence):
        return self._call(f"{self.prefix}:fix:{task['id']}:a{1 if evidence is None else 2}", "fix", task, run_dir, evidence)

    def _call(self, key, name, *args):
        from v2_tasks import TaskResult
        res = self.m._task(key, name, *args)
        return res if res is not None else TaskResult(False, None, 0.0, "no budget left for this task")


def _write_json(run_dir: str, rel: str, obj) -> None:
    """A harness record in the run dir, published atomically with no link followed (lib.safe_io)."""
    from lib.safe_io import write_nofollow
    write_nofollow(run_dir, rel.replace(os.sep, "/"), json.dumps(obj, indent=1, default=str).encode(), atomic=True)


def _rep_error(rep: dict) -> str:
    errs = list(rep.get("errors") or []) + [f"{s.get('id')}: {_step_error(s.get('error'))}"
                                            for s in rep.get("steps") or [] if s.get("status") != "ok"]
    return "; ".join(str(e) for e in errs)[:300]


_TB_FRAME = re.compile(r'^[ |+]*File "([^"]+)", line (\d+)(?:, in (\S+))?\s*$')
_TB_EXC = re.compile(r"^[A-Za-z_][\w.]*(?::|$)")
_TB_HEAD = re.compile(r"[ |+]*(?:Exception Group )?Traceback \(most recent call last\):")


def _step_error(err) -> str:
    """A failed step's error, short enough for the history line. A Python traceback in it (the step's log tail; its
    header may have fallen outside the tail) is reduced to the frame that raised and the exception's first line,
    after whatever preceded the traceback (a timeout's own text); "Traceback at" keeps it read as a crash. The first
    300 characters had held only the outer frames, so T16's CI flakes recorded no exception (T9b). Any other error is
    kept as it is (T9c)."""
    text = str(err)
    lines = text.splitlines()
    fr = [(k, m) for k, m in ((k, _TB_FRAME.match(ln)) for k, ln in enumerate(lines)) if m]
    if not fr:
        return text
    k, m = fr[-1]
    depth = lambda ln: len(ln) - len(ln.lstrip(" |+-"))  # noqa: E731 -- indent, ExceptionGroup borders included
    exc = next((ln.lstrip(" |+-") for ln in lines[k + 1:] if depth(ln) < depth(lines[k])
                and _TB_EXC.match(ln.lstrip(" |+-"))), "no exception line in the log tail")
    hdr = _TB_HEAD.search(text)
    head = text[:hdr.start()] if hdr else ""      # a timeout's own text, before the traceback it shows
    where = f"{os.path.basename(m.group(1))}:{m.group(2)}" + (f" in {m.group(3)}" if m.group(3) else "")
    return f"{head.strip()[:120]} Traceback at {where}: {exc[:150]}".strip()


# Machine.reopen_interrupted_analysis's history line, per resumable cause.
_RESUME_WHY = {"timeout": "the analysis step had timed out", "killed": "the analysis step had been killed",
               "infra": "engine rows had failed on lost workers (kind infra)"}

# A step killed by these signals was stopped from outside (out of memory, a stop); a crash (SIGSEGV...) is not.
_KILL_SIGNALS = (9, 15, 2, 1)     # SIGKILL, SIGTERM, SIGINT, SIGHUP


def calibration_refusal(rep: dict, run_dir: str) -> dict | None:
    """{code, reason, at_bound} when the pipeline's failure is a refused calibration
    (toolkit/analysis/calibration.py CalibrationRefused), else None. A failed step's output names the exception; the
    analysis's own record (analysis/blocked.yaml, listed in this run's blocked manifest) adds its reason and the
    parameters at a bound when it is there. Anything else (another error, a stopped or skipped step) is not one."""
    import re
    if not isinstance(rep, dict) or rep.get("errors"):
        return None
    failed = [s for s in rep.get("steps") or [] if isinstance(s, dict) and s.get("status") == "failed"]
    hit = next((s for s in failed if "CalibrationRefused" in str(s.get("error") or "")), None)
    if hit is None:
        return None
    err = str(hit.get("error") or "")
    m = re.search(r"CalibrationRefused:\s*([a-z_]+):\s*(.*)", err)
    out = {"code": m.group(1) if m else "calibration_refused", "reason": " ".join((m.group(2) if m else err).split()),
           "at_bound": []}
    try:
        from toolkit.analysis import artifacts as AR
        man = json.load(open(os.path.join(run_dir, AR.MANIFEST)))
        rel = os.path.join("analysis", "blocked.yaml")
        ent = (man.get("artifacts") or {}).get(rel) or {}
        if man.get("status") == "blocked" and ent.get("sha256") == _sha256(os.path.join(run_dir, rel)):
            data = (AR.read(run_dir, rel) or {}).get("data") or {}
            if data.get("where") == "calibration" and data.get("exception") == "CalibrationRefused":
                out["code"] = str(data.get("code") or out["code"])
                out["reason"] = " ".join(str(data.get("reason") or out["reason"]).split())
                out["at_bound"] = [str(x) for x in data.get("at_bound") or []]
    except (OSError, ValueError, AttributeError, TypeError):
        pass                       # the step's own output still says what was refused
    if out["reason"].startswith(f"{out['code']}:"):       # the exception's text repeats its code
        out["reason"] = out["reason"][len(out["code"]) + 1:].strip()
    return out


def conflicts_policy_stop(run_dir: str) -> bool:
    """The spec's conflicts.policy is stop (toolkit/analysis/conflicts.py; its default when undeclared): a refused
    calibration then ends the run as before. An unreadable spec is read as stop (fail closed: the old behaviour)."""
    import yaml
    from toolkit.analysis import conflicts as CF
    try:
        raw = yaml.safe_load(open(os.path.join(run_dir, AB.SPEC_FILE))) or {}
        return CF.policy(raw)["policy"] == "stop"
    except Exception:  # noqa: BLE001
        return True


def record_calibration_refusal(run_dir: str, ref: dict, round_n) -> None:
    """The refusal as an open HIGH finding the spec owns (findings ledger), so the spec revision reads it: the
    redraft's feedback (Machine._rethink) or the owner's amendment request (rethink_request). It names the parameters
    at a bound and the remedy."""
    ab = list(ref.get("at_bound") or [])
    claim = (f"the calibration was refused ({ref.get('code')}): {ref.get('reason')}"
             + (f". Parameters at a bound: {', '.join(ab[:20])}" + (f" and {len(ab) - 20} more" if len(ab) > 20 else "")
                if ab else ""))
    led = FL.load(run_dir)
    f = led.get(CALIBRATION_REFUSED) or FL.Finding(id=CALIBRATION_REFUSED, kind=CALIBRATION_REFUSED, severity="HIGH",
                                                    source="harness-calibration", first_seen_round=round_n)
    f.severity, f.owner_state, f.target_stage, f.status, f.blocking = "HIGH", "SPEC", "PLAN", "open", True
    f.scope_declarable, f.last_seen_round, f.claim = False, round_n, claim[:2000]
    f.fix_requires = ("revise the spec's calibration: check calibration.scale and calibration.range against the "
                      "attribute's values (a log scale cannot reach negative values), then the parameterisation (a "
                      "reference package) or declared priors with method posterior")
    FL._event(f, round_n, "open", "harness-calibration", "calibration refused at ANALYZE")
    led[CALIBRATION_REFUSED] = f
    FL.save(run_dir, led)


def analysis_failure_cause(rep: dict, run_dir: str | None = None) -> str | None:
    """'timeout', 'killed' or 'infra' when the pipeline report's only failures are steps that ran out of wall-clock
    time (scripts/pipeline.py `_run_cmd`), died of an external signal, or exited with an error because their engine
    batch lost rows to the machine (`_infra_batch`, read from `run_dir`), with the steps below them skipped; None
    for anything else (another error exit, a crash, missing inputs, a validation error)."""
    if not isinstance(rep, dict) or rep.get("errors"):
        return None
    cause = None
    for s in rep.get("steps") or []:
        if not isinstance(s, dict) or s.get("status") == "ok":
            continue
        rc, err = s.get("returncode"), str(s.get("error") or "")
        if s.get("status") == "skipped" and err.startswith("upstream step not rebuilt"):
            continue
        if s.get("status") == "failed" and rc is None and err.startswith("timed out after "):
            cause = cause or "timeout"
        elif s.get("status") == "failed" and type(rc) is int and -rc in _KILL_SIGNALS:
            cause = cause or "killed"
        elif s.get("status") == "failed" and type(rc) is int and rc > 0 and run_dir and _infra_batch(run_dir, rep):
            cause = cause or "infra"
        else:
            return None
    return cause


# Where the analysis's engine batch keeps compute.py's per-call summaries (toolkit/analysis/batch.py).
BATCH_DIR = os.path.join("analysis", "batch")
# ... and the joint calibration's (toolkit/analysis/joint_calibration.py OUT_REL)
JC_DIR = os.path.join("analysis", "joint_calibration")


def _infra_batch(run_dir: str, rep: dict) -> bool:
    """2026-11-02 (Nigeria v2 full run F3): whether the engine batch this pipeline run computed failed only with
    kind infra (lost workers; scripts/compute.py has already retried them within the call). Read from the summaries
    compute.py wrote under analysis/batch/ during this run, i.e. after the run's start (the report's mtime minus its
    `seconds`, on the same clock as theirs): at least one has failed rows, and every one with failed rows has only
    infra ones. Never read from the error text. Anything unreadable is False (not resumable).
    F6 (2026-11-11): also the joint calibration's primary batches (JC_DIR: bench_call*_phase*.jsonl and
    call<N>/points.jsonl or results.jsonl directly under it; never sensitivity_* or another subdirectory, whose
    batches the failing step does not depend on). When the report carries `pipeline_run`, only summaries with the
    same id count (untagged ones are ignored), so a run dir copied or restored from a checkpoint (mtimes reset) is
    still classified; the mtime rule applies only to a report without an id."""
    run_id = rep.get("pipeline_run") if isinstance(rep, dict) else None
    try:
        started = os.path.getmtime(os.path.join(run_dir, "models", "pipeline_report.json")) - float(rep["seconds"]) - 2.0
    except (OSError, KeyError, TypeError, ValueError):
        return False
    import glob
    import re
    paths = sorted(glob.glob(os.path.join(run_dir, BATCH_DIR, "*.summary.json")))
    jc = os.path.join(run_dir, JC_DIR)   # Codex on #200: only the primary calibration's batches, never sensitivity_*
    paths += sorted(glob.glob(os.path.join(jc, "bench_call*_phase*.jsonl.summary.json")))
    paths += sorted(p for p in glob.glob(os.path.join(jc, "call*", "*.jsonl.summary.json"))
                    if re.fullmatch(r"call\d+", os.path.basename(os.path.dirname(p)))
                    and os.path.basename(p) in ("points.jsonl.summary.json", "results.jsonl.summary.json"))
    failed = 0
    for p in paths:
        try:
            with open(p) as f:
                summ = json.load(f)
            if run_id:                            # Codex on #200: the run id decides; an untagged summary is not ours
                if summ.get("pipeline_run") != run_id:
                    continue
            elif os.path.getmtime(p) < started:
                continue                          # an earlier pipeline run's batch (no run id: the mtime rule)
            kinds, n_failed = summ.get("failed_by_kind"), int(summ.get("n_failed") or 0)
        except (OSError, ValueError, TypeError, AttributeError):
            return False
        if not n_failed:
            continue
        if not isinstance(kinds, dict) or set(kinds) != {"infra"} or kinds["infra"] != n_failed:
            return False                          # a model error or a stopped row needs more than a rerun
        failed += n_failed
    return failed > 0


def resumable_failure(run_dir: str, st: RunState) -> str | None:
    """Why this run may be resumed at ANALYZE ('timeout' | 'killed' | 'infra'), or None: only a run that ended run_failed on
    its ANALYZE -> FINAL transition, whose pipeline report (models/pipeline_report.json, written by the harness's
    run) says the analysis was interrupted. Read from the report, so a run that failed before the history carried
    the `resumable` marker is classified the same way."""
    if st.state != "FINAL" or st.status != "run_failed" or not st.history:
        return None
    last = st.history[-1]
    if not isinstance(last, dict) or last.get("from") != "ANALYZE" or last.get("to") != "FINAL":
        return None
    try:
        with open(os.path.join(run_dir, "models", "pipeline_report.json")) as f:
            rep = json.load(f)
    except (OSError, ValueError):
        return None
    return analysis_failure_cause(rep, run_dir)


def _with_abs_paths(spec: dict, run_dir: str) -> dict:
    return AB._abs_spec(spec, run_dir)


def _pre_engine_check(spec: dict) -> list[str]:
    """Before an engine is chosen: the sections every engine needs are present."""
    from spike.analysis.spec import REQUIRED
    return [f"missing section {k}" for k in REQUIRED if k != "engine" and k not in spec]


ACCEPTED_STATUSES = ("completed", "completed_with_report_restored")
STATUS_MARK = "<!-- harness: run status -->"


def _draft_file_problems(run_dir: str) -> list[str]:
    """The spec (required) and analysis_settings.yaml (optional) must each be a regular file with one link: the pre-sign
    checks read them by path, so a symbolic or hard link would show them another file's content (Codex r4 P1-5)."""
    import stat
    out = []
    for rel, required in ((AB.SPEC_FILE, True), (AB.SETTINGS_FILE, False)):
        try:
            st = os.lstat(os.path.join(run_dir, rel))
        except FileNotFoundError:
            if required:
                out.append(f"spec unreadable: {rel} does not exist")
            continue
        except OSError as e:
            out.append(f"{rel} cannot be read: {type(e).__name__}")
            continue
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            out.append(f"{rel} is a link or not a regular file: write it as a plain file")
        elif st.st_nlink > 1:
            out.append(f"{rel} has {st.st_nlink} hard links: write it as its own file")
    return out


def _special_files(run_dir: str, limit: int = 20000) -> list[str]:
    """Every entry in the run directory that is not a regular file, a directory or a link to one (a FIFO, a device, a
    socket), as problems: a check or a derivation that opens one can block with no writer. The whole run is walked
    (no link followed), so the result does not depend on how a path in the spec is spelled (Codex r1 on #350). Fail
    closed past `limit` entries."""
    import stat
    out, n = [], 0
    for base, dirs, files in os.walk(run_dir):
        for name in dirs + files:
            n += 1
            if n > limit:
                return out + [f"the run directory has more than {limit} entries: its files were not checked"]
            p = os.path.join(base, name)
            try:
                mode = os.stat(p).st_mode           # a link: its target's type
            except OSError:
                continue                            # a dangling link opens nothing
            if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                out.append(f"{os.path.relpath(p, run_dir)!r} in the run is not a regular file: remove it, or write "
                           "a plain file")
    return out


CARRIED = "from an earlier try, still unresolved: "


def _carry_feedback(errs: list, earlier: list | None, cap: int = 40) -> list:
    """The SPEC feedback for the next try: this try's problems, then the earlier try's when there is no draft on
    disk to check (baseline hb1: a try lost to the turn cap replaced the validation errors with "max turns", so
    the next try repeated them). Earlier items are marked once and never repeated."""
    out = list(errs)
    seen = {x[len(CARRIED):] if x.startswith(CARRIED) else x for x in out}
    for x in earlier or []:
        core = x[len(CARRIED):] if x.startswith(CARRIED) else x
        if core not in seen and not core.startswith("draft_spec failed:"):
            seen.add(core)
            out.append(CARRIED + core)
    return out[:max(cap, len(errs))]          # the cap trims the carried items only (Codex r1 P2-6)


def _status_line(run_dir: str, status: str | None) -> str:
    """F6 S1-7: the run status, from .claude/orchestration/acceptance_language.yaml (a declared-scope
    run reads "declared scope, not accepted"). Without a status: the run state's, else the write mode's."""
    if status is None:
        try:
            st = load_state(run_dir)
        except (OSError, ValueError, TypeError):
            st = None
        status = (st.status or {"declared": "completed_scope_declared", "not_accepted": "not_accepted"}.get(
            st.write_mode or "", "pending")) if st else "pending"
    try:
        import writer_qa as WQ
        lines = WQ.load_acceptance_language()["status_lines"]
    except Exception:  # noqa: BLE001 -- fail closed: never reads as accepted
        lines = {}
    text = lines.get(status) or (lines.get("pending") if status == "pending" else lines.get("other")) or "not accepted"
    return f"{STATUS_MARK}\n**Run status: {text}.** (`{status}`; rendered by the harness)\n\n"


def _plan_changing(item: dict) -> bool:
    """An owner item whose evidence shows the plan changes: a decision-bearing kind, a declaration whose decision
    test changes the decision, or a W0-F evidence conflict whose verdict changes the plan (or whose test did not run:
    W0-F counts that as changing it; Codex r4 of #316)."""
    conf = item.get("conflict")
    if item.get("kind") == "evidence_conflict_declared" and not isinstance(conf, dict):
        return True     # a request saved before the verdict was carried: it holds only plan-changing or untested ones
    return item.get("kind") in DECISION_BEARING_KINDS or (isinstance(conf, dict) and conf.get("changes_plan") is True) \
        or any((d.get("test") or {}).get("decision_changes") is True
               for d in item.get("declarations") or [] if isinstance(d, dict))


def provisional_lines(run_dir: str) -> tuple[list[str], list[str]]:
    """(lines above the draft, a section after it): the lead's provisional decisions of a --signoff lead run (D2),
    the owner items answered (scripts/scope_signoff.py provisional) and the engine departures credited
    (`provisional_lead` in engine_choice.json). Those whose decision test changes the plan head the report."""
    import scope_signoff as SS
    try:
        rows = [(r.get("finding"), bool(r.get("plan_changing")), (r.get("review") or {}).get("decision"))
                for r in SS.provisional(run_dir)]
    except Exception as e:  # noqa: BLE001 -- fail closed: the report says the list could not be read
        return ([f"**Provisional decisions: the list could not be read ({type(e).__name__}).**\n\n"],
                ["", "## Provisional decisions (the lead's)", "", f"Could not be read: {type(e).__name__}: {e}"])
    try:
        ch = json.load(open(os.path.join(run_dir, AB.ENGINE_CHOICE)))
        deps = [d for d in ch.get("engine_departures") or [] if isinstance(d, dict) and d.get("status") == "provisional_lead"]
    except (OSError, ValueError, AttributeError):
        deps = []
    try:
        changes = {str(t.get("id")) for t in (json.load(open(os.path.join(run_dir, AB.ANALYSIS_JSON))) or {}).get(
            "decision_tests") or [] if isinstance(t, dict) and t.get("plan_changes")}
    except (OSError, ValueError, AttributeError, TypeError):
        changes = set()
    rows += [(f"engine departure {d.get('benchmark') or d.get('component')}", str(d.get("decision_test")) in changes,
              None) for d in deps]
    if not rows:
        return [], []
    flagged = [f for f, pc, _r in rows if pc]
    how = (f"`python3 scripts/scope_signoff.py review {run_dir} --finding <id> --confirm|--overturn`; an engine departure "
           "is confirmed by the owner's signature of the spec in a relaunch")
    top = [f"**Provisional decisions that change the plan.** This run was unattended (`--signoff lead`); the lead "
           f"answered, provisionally, {len(flagged)} decision(s) that are the owner's and whose decision test changes the "
           f"plan: {', '.join(map(str, flagged))}. The owner confirms or overturns each ({how}).\n\n"] if flagged else []
    sec = ["", "## Provisional decisions (the lead's, under --signoff lead; for the owner's review)", "",
           f"Each decision below is the owner's; the lead made it provisionally so the run could finish. Review: {how}.", ""]
    sec += [f"- **{f}**: accepted provisionally; plan changes: {'yes' if pc else 'no'}; owner review: "
            f"{r or 'not yet'}." for f, pc, r in rows]
    return top, sec


def amendment_lines(run_dir: str) -> list[str]:
    """lead-gaps β: a run made by an amendment its owner has not signed (the lead's, provisional) heads its report with
    the amendment: its id, reason and changes, from toolkit/spec/amend.py's record. Nothing once the owner signs. The
    S7 handoff event in run_state.json keeps the heading when the record is lost (Codex r1) and selects the wording."""
    p = os.path.join(run_dir, AMENDMENT_RECORD)
    try:
        st = load_state(run_dir)
    except (OSError, ValueError):
        st = None
    s7 = next((h for h in (st.history if st else []) if isinstance(h, dict) and h.get("provisional_amendment")), None)
    if not os.path.lexists(p) and s7 is None:
        return []
    try:
        from toolkit.spec import signoff as SO
        so = SO._spec_record(run_dir) if SO.status(run_dir) == "signed" else None
        by = (so or {}).get("signed_by") or (so or {}).get("by")
    except Exception:  # noqa: BLE001 -- unknown signer: listed as provisional
        by = None
    if by == "owner":
        return []
    try:
        rec = json.load(open(p))
        ch = "; ".join(f"{c.get('op')} `{c.get('path')}`" + ("" if c.get("op") == "remove" else
                                                             f" to {json.dumps(c.get('after'), default=str)[:160]}")
                       for c in rec.get("changes") or [] if isinstance(c, dict))
        parent = _parent_of(run_dir, (rec.get("parent_run") or {}).get("run_dir"))
    except (OSError, ValueError, AttributeError, TypeError) as e:
        return [f"**Provisional amendment of the signed spec ({(s7 or {}).get('provisional_amendment') or 'unnamed'}): "
                f"its record could not be read ({type(e).__name__}); the owner has not reviewed it.**\n\n"]
    if s7 is not None:
        how = "which the lead made from that run's rethink request and signed provisionally"
        cmd = (f"the owner confirms it by amending the parent with the same patch and signing that child "
               f"(`python3 -m toolkit.spec.amend {shlex.quote(str(parent))} --from-request --signoff owner`), or "
               "overturns it by amending the parent differently or not at all")
    else:          # a manual amendment (amend --patch) the lead signed
        how = "made with toolkit/spec/amend.py and not signed by the owner"
        cmd = "the owner confirms it by amending the parent with the same patch file and signing that child"
    return [f"**Provisional amendment of the signed spec.** This run continues `{parent}` under amendment "
            f"`{rec.get('id')}`, {how} (signed by {by or 'nobody yet'}); the owner has not reviewed it. Reason: "
            f"{' '.join(str(rec.get('reason')).split())[:500]} Changes: {ch or 'none recorded'}. {cmd[0].upper() + cmd[1:]}."
            "\n\n"]


def render_report(run_dir: str, backend=AB, status: str | None = None) -> str:
    """report.md = the run status line, the writer's draft, and the blocks the harness renders from
    artifacts: the headline numbers, the declared limits and open findings (in full, with their
    required remedy), and the deliverable file the spec names."""
    import yaml
    draft = open(os.path.join(run_dir, "manuscript.md")).read()
    lp = os.path.join(run_dir, "models", "claims_ledger.yaml")
    if os.path.exists(lp):
        # The writer writes [CLAIM:id]; the harness substitutes the value (no number is typed).
        import render_claims as RCL
        try:
            # the valid claims; an invalid one stays a reference, reported by report_claim_findings
            draft, _unresolved = RCL.render_text(draft, RCL.load_claims_tolerant(lp)[0])
        except Exception:  # noqa: BLE001 -- left unrendered; report_claim_findings flags it
            pass
    h = backend.headline(run_dir)
    lines = ["", "<!-- harness: rendered from analysis/analysis.json -->", "## Numbers (rendered by the harness)", "",
             "| quantity | value |", "|---|---|"]
    lines += [f"| {k} | {v:,.4g} |" if isinstance(v, (int, float)) else f"| {k} | {v} |" for k, v in h.items()]
    try:    # engine parameter values the signed spec sets, each with the card value it replaces (HIGH when outside)
        from toolkit.spec import engine_parameters as PV
        sv = PV.report_block(run_dir)
    except Exception as e:  # noqa: BLE001 -- fail closed: the report says the block could not be rendered
        sv = ["## Engine parameter values set by the signed spec", "",
              f"Could not be rendered from analysis/engine.yaml: {type(e).__name__}: {e}", ""]
    if sv:
        lines += [""] + sv[:-1]
    decl, sd_find = [], []
    if os.path.exists(os.path.join(run_dir, "scope_declaration.yaml")):
        try:   # every shape the gate reads, each entry's full text (lib.scope_declarations)
            from lib import scope_declarations as SD
            _doc, decl, sd_find = SD.load(run_dir)
        except Exception as e:  # noqa: BLE001 -- fail closed: the report says so
            SD, sd_find = None, [{"claim": f"{type(e).__name__}: {e}"}]
    if decl or sd_find:
        lines += ["", "## Declared limits (rendered in full from scope_declaration.yaml)", ""]
        lines += [f"- scope_declaration.yaml could not be read: {f.get('claim')}" for f in sd_find]
        for e in decl:
            ids = [str(x) for k in SD.STRUCTURED_ID_FIELDS
                   for x in (e.get(k) if isinstance(e.get(k), list) else [e.get(k)]) if x]
            text = " ".join(f"{f.replace('_', ' ').capitalize()}: {' '.join(str(e[f]).split())}"
                            for f in SD.JUSTIFICATION_FIELDS if e.get(f))
            fix = _remedy(e)
            lines.append(f"- **{e.get('id') or 'unnamed'}** ({', '.join(ids) or 'no finding named'}): "
                         f"{text or 'no text given.'}" + (f" Required remedy: {fix}" if fix else ""))
    try:
        dec2 = (yaml.safe_load(open(os.path.join(run_dir, DECLARED_FILE))) or {})
    except OSError:
        dec2 = {}
    if dec2.get("declared"):
        lines += ["", "## Open findings, declared (not resolved)", "", str(dec2.get("rationale") or ""), ""]
        lines += [f"- **{e['id']}** ({e['severity']}, {e['source']}): {e['claim']} Required remedy: "
                  f"{e.get('required_remedy') or 'not stated by the finding.'}" for e in dec2["declared"]]
    try:            # v2 autonomy 1B-ii: who drafted and signed the evidence dossier, and its declared gaps
        from toolkit.spec import evidence_draft as EDR
        so = {}
        for f in (SIGNOFF_FILE, "decision_brief.signoff.json"):     # Codex r1 P2-11: the record that binds the spec
            try:
                d_ = json.load(open(os.path.join(run_dir, f)))
            except (OSError, ValueError):
                continue
            binds = isinstance(d_, dict) and ((d_.get("sha256") or {}).get(AB.SPEC_FILE) if isinstance(
                d_.get("sha256"), dict) else None) or (isinstance(d_, dict) and d_.get("spec_sha256"))
            if binds and (d_.get("signed_by") or d_.get("by")):     # r2 P2-5: a brief that never bound the spec
                so = d_
                break
        eb = EDR.report_block(run_dir, so.get("signed_by") or so.get("by"))
    except Exception as e:  # noqa: BLE001 -- fail closed: the report says the block could not be rendered
        eb = ["## Evidence dossier", "", f"Could not be rendered from the evidence record: {type(e).__name__}.", ""]
    if eb:
        lines += [""] + eb[:-1]
    from toolkit.analysis import conflicts as CF       # W0-F: every held-out calibration target
    cl = CF.report_lines(run_dir)
    if cl:
        lines += [""] + cl[:-1]
    wmp = os.path.join(run_dir, "world_model", "world_model.md")    # §14: rendered by the harness from score.json
    try:
        if os.path.isfile(wmp) and not os.path.islink(wmp):
            lines += ["", open(wmp, encoding="utf-8").read().rstrip()]
    except (OSError, ValueError) as e:      # fail soft: the report is rendered without it, and says so
        lines += ["", f"## World model\n\nThe world-model section could not be read: {type(e).__name__}."]
    top, prov = provisional_lines(run_dir)
    lines += prov
    head = _status_line(run_dir, status) + "".join(amendment_lines(run_dir)) + "".join(top)
    body = draft.rstrip() + "\n"
    from toolkit.spec import review_conditions as RCN       # W0-G: conditions the lead deferred, at the top
    rcl = RCN.report_lines(run_dir)
    if rcl:
        head = head + "\n".join(rcl) + "\n"
    full = head + body + "\n".join(lines) + "\n"
    _put(run_dir, "report.md", full)
    # What the harness added, authenticated by the report's hash: writer_qa checks only the writer's span.
    _write_json(run_dir, "report_render.json", {"report_sha256": hashlib.sha256(full.encode("utf-8")).hexdigest(),
                                                "writer_span": [len(head), len(head) + len(body)]})
    _write_deliverable(run_dir)
    return os.path.join(run_dir, "report.md")


def _write_deliverable(run_dir: str) -> None:
    """The spec may name a deliverable file and its columns (from the allocation the harness
    computed): `deliverable_file: {path, columns: {out_col: allocation_col}, package_names: {...}}`."""
    import csv
    import yaml
    spec = yaml.safe_load(open(os.path.join(run_dir, AB.SPEC_FILE))) or {}
    df = spec.get("deliverable_file")
    src = os.path.join(run_dir, AB.ALLOCATION_CSV)
    if not isinstance(df, dict) or not os.path.exists(src) or deliverable_problems(run_dir):
        return                      # an invalid destination is a HIGH finding, never written
    rows = list(csv.DictReader(open(src)))
    names = df.get("package_names") or {}
    import io
    buf = io.StringIO(newline="")
    w = csv.writer(buf)
    cols = df["columns"]
    w.writerow(list(cols))
    for r in rows:
        w.writerow([names.get(r[c], r[c]) if c == "package" else r[c] for c in cols.values()])
    _put(run_dir, str(df["path"]), buf.getvalue())


# A relaunch with reuse (a new run dir from another one's spec and stored rows) names its parent here; the launcher
# sets it. main.py gains a --parent-run flag after v1 retirement, without changing the record.
PARENT_ENV = "MA_PARENT_RUN"


def _parent_record(path: str) -> dict:
    """metadata.json parent_run: the parent's dir name and its run_state.json sha256 (None, with the error, when it
    cannot be read: the link is then never trusted by scripts/harness_metrics.py)."""
    from lib.safe_io import read_nofollow
    rec = {"run_dir": os.path.basename(os.path.abspath(path)), "recorded_from": PARENT_ENV}
    try:
        rec["run_state_sha256"] = hashlib.sha256(read_nofollow(path, STATE_FILE)[0]).hexdigest()
    except Exception as e:  # noqa: BLE001 -- recorded, never raised: the run itself is not the parent's
        rec.update(run_state_sha256=None, error=type(e).__name__)
    return rec


def _runtime_ids() -> dict:
    """The model ids and the Agent SDK version this process runs with ("unknown" when they cannot be read)."""
    try:
        from agents.enforcement import MODEL_DEEP, MODEL_FAST
        models = {"deep": MODEL_DEEP, "fast": MODEL_FAST}
    except Exception:  # noqa: BLE001
        models = {"deep": "unknown", "fast": "unknown"}
    try:
        from importlib.metadata import version
        sdk = version("claude-agent-sdk")
    except Exception:  # noqa: BLE001
        sdk = "unknown"
    return {"models": models, "sdk_version": sdk}


def _record_invocation(run_dir: str, budget_usd, cap_usd) -> None:
    """This invocation's record in metadata.json: the latest model ids and SDK version at the top level, and on the
    invocation entry main.py appended (scripts/lib/pipeline_id.py stamp) the budget asked for, the cap in force, the
    model ids and the SDK version. No entry is invented when main.py wrote none. Read with no link followed; a
    metadata.json that cannot be read is left as it is (never replaced by a partial record)."""
    from lib.safe_io import read_nofollow
    try:
        meta = json.loads(read_nofollow(run_dir, "metadata.json")[0].decode())
    except Exception:  # noqa: BLE001
        return
    if not isinstance(meta, dict):
        return
    ids = _runtime_ids()
    meta.update(ids)
    inv = meta.get("invocations")
    if isinstance(inv, list) and inv and isinstance(inv[-1], dict) and "budget_usd" not in inv[-1]:
        inv[-1].update(budget_usd=budget_usd, cap_usd=cap_usd, **ids)
    _write_json(run_dir, "metadata.json", meta)


def init_run(run_dir: str, question: str, max_rounds: int = 3, signoff: str = "owner", gate_mode: str = "v2",
             budget_usd: float | None = None, derived_inputs: bool = True, derive_unsandboxed: bool = False,
             world_model: bool = True) -> RunState:
    os.makedirs(run_dir, exist_ok=True)
    st = load_state(run_dir)
    if st is not None:
        return st
    st = RunState(question=question, max_rounds=max_rounds, signoff=signoff, gate_mode=gate_mode, budget_usd=budget_usd,
                  derived_inputs=derived_inputs, derive_unsandboxed=derive_unsandboxed, world_model_stages=world_model)
    try:   # keep what main.py recorded first (run_dir, pipeline, commit, launcher: scripts/lib/pipeline_id.py)
        meta = json.load(open(os.path.join(run_dir, "metadata.json")))
    except (OSError, ValueError):
        meta = {}
    meta = meta if isinstance(meta, dict) else {}
    meta.update({"question": question, "started": meta.get("started") or _now(), "pipeline": "v2",
                 "max_rounds": max_rounds, "signoff": signoff})
    if os.environ.get(PARENT_ENV):
        meta["parent_run"] = _parent_record(os.environ[PARENT_ENV])
    _write_json(run_dir, "metadata.json", meta)
    save_state(run_dir, st)
    return st


def run_v2(question: str, run_dir: str, tasks, max_rounds: int = 3, signoff: str = "owner",
           budget_usd: float | None = None, log=print, derive_unsandboxed: bool = False, backend=None,
           cross_engine_check: bool = True, _depth: int = 0, _stamp: dict | None = None) -> str:
    """Entry point for main.py --v2 (and the dry run). Resumes when run_state.json exists, also a run that ended
    run_failed because its analysis step timed out, was killed or lost engine rows to the machine
    (Machine.reopen_interrupted_analysis). A --signoff lead run holds its amendment lock from before its state is read
    through its hand-off and the run of the child it handed to (owner decision on #330, Codex r3 on #330 and #342): a
    second process resuming it fails with AmendmentLocked before writing anything, also to that child."""
    try:
        prev = load_state(run_dir)
    except (OSError, ValueError):
        prev = None
    if signoff != "lead" and (prev is None or prev.signoff != "lead"):
        return _run_v2_chain(question, run_dir, tasks, max_rounds, signoff, budget_usd, log, derive_unsandboxed,
                             backend, cross_engine_check, _depth, _stamp)
    os.makedirs(run_dir, exist_ok=True)
    with _amend_lock(run_dir):     # held through the hand-off and the child's run (Codex r3 on #342, P1-8)
        return _run_v2_chain(question, run_dir, tasks, max_rounds, signoff, budget_usd, log, derive_unsandboxed,
                             backend, cross_engine_check, _depth, _stamp)


def _run_v2_chain(question, run_dir, tasks, max_rounds, signoff, budget_usd, log, derive_unsandboxed, backend,
                  cross_engine_check, _depth, _stamp) -> str:
    """One run dir, then the child its provisional amendment handed it to (run_v2, under the caller's lock)."""
    child, status = _run_v2_once(question, run_dir, tasks, max_rounds, signoff, budget_usd, log,
                                 derive_unsandboxed, backend, cross_engine_check, _stamp)
    if child is None or _depth >= 8:          # a bounded chain (a restored cycle cannot recurse: Codex r2)
        return status
    # lead-gaps β: the run continues in the child its provisional amendment made (same process, commit and launcher);
    # the child is stamped only once its own lock is held
    try:
        meta = json.load(open(os.path.join(run_dir, "metadata.json")))
        ident = {k: meta.get(k) for k in ("pipeline", "commit", "launcher")}
    except (OSError, ValueError, AttributeError) as e:
        log(f"[machine] the child run's identity was not read: {type(e).__name__}: {e}")
        ident = None
    cst = load_state(child)
    log(f"[machine] continuing in {child} (provisional amendment)")
    return run_v2(cst.question, child, tasks.for_run_dir(child), max_rounds=cst.max_rounds, signoff="lead",
                  budget_usd=cst.budget_usd, log=log, derive_unsandboxed=derive_unsandboxed, backend=backend,
                  cross_engine_check=cross_engine_check, _depth=_depth + 1, _stamp=ident)


def _run_v2_once(question, run_dir, tasks, max_rounds, signoff, budget_usd, log, derive_unsandboxed, backend,
                 cross_engine_check, stamp) -> tuple:
    """(the child to continue in or None, the status) of one run dir."""
    if stamp:
        try:
            from lib import pipeline_id
            pipeline_id.stamp(run_dir, stamp)
        except Exception as e:  # noqa: BLE001 -- the identity stamp only; the run records its own invocation below
            log(f"[machine] the child run's identity was not stamped: {type(e).__name__}: {e}")
    st = init_run(run_dir, question, max_rounds, signoff, "v2", budget_usd, derive_unsandboxed=derive_unsandboxed)
    if st.paused == AWAITING_BUDGET and budget_usd and budget_usd > (st.budget_usd or 0):
        # the owner resumes a run paused below the allowance floor with a higher cap: recorded, never lowered
        st.history.append({"at": _now(), "budget_raised": {"from": st.budget_usd, "to": budget_usd},
                           "spent_usd": round(st.spent_usd, 4)})
        st.budget_usd = budget_usd
        save_state(run_dir, st)
    _record_invocation(run_dir, budget_usd, st.budget_usd)
    m = Machine(run_dir, tasks, st, backend=backend, log=log, cross_engine_check=cross_engine_check)
    m.reopen_interrupted_analysis()
    status = m.run()
    return handoff_child(run_dir), status


def _child_of(run_dir: str, recorded, strict: bool = False) -> str | None:
    """The child dir a parent recorded, resolved beside the parent as it is now (a restored copy moves both: Codex r1),
    and only when the child's metadata names this parent (by directory name)."""
    if not isinstance(recorded, str) or not recorded:
        return None
    here = os.path.join(os.path.dirname(os.path.abspath(run_dir)), os.path.basename(recorded.rstrip(os.sep)))
    if os.path.islink(here):          # a link to another tree is not this run's child (Codex r1 of #349)
        return None
    try:
        pr = (json.load(open(os.path.join(here, "metadata.json"))) or {}).get("parent_run") or {}
    except (OSError, ValueError, AttributeError):
        return None
    ok_ = os.path.basename(str(pr.get("run_dir") or "").rstrip(os.sep)) == os.path.basename(os.path.abspath(run_dir))
    if ok_ and strict:      # a finished parent: the child binds its final state (r2); a missing digest binds none (r3)
        try:
            ok_ = isinstance(pr.get("run_state_sha256"), str) and hashlib.sha256(
                open(os.path.join(run_dir, STATE_FILE), "rb").read()).hexdigest() == pr["run_state_sha256"]
        except OSError:
            ok_ = False
    return here if ok_ and os.path.realpath(here) != os.path.realpath(run_dir) else None


def _parent_of(run_dir: str, recorded):
    """The parent a child run continues: its sibling of the recorded name when that sibling hands off to this child (a
    parent and child moved together: Codex r3 on #342, P2-12), else the recorded path."""
    if not isinstance(recorded, str) or not recorded:
        return recorded
    here = os.path.join(os.path.dirname(os.path.abspath(run_dir)), os.path.basename(recorded.rstrip(os.sep)))
    c = handoff_child(here)
    if c and os.path.realpath(c) == os.path.realpath(run_dir):
        return here
    # a manual amendment's parent keeps its status: the parent-state digest the child records (in its metadata or its
    # amendment record: Codex r2 of #349)
    for rel in ("metadata.json", AMENDMENT_RECORD):
        try:
            bound = ((json.load(open(os.path.join(run_dir, rel))) or {}).get("parent_run") or {}).get(
                "run_state_sha256")
            if not os.path.islink(here) and isinstance(bound, str) and bound == hashlib.sha256(
                    open(os.path.join(here, STATE_FILE), "rb").read()).hexdigest():
                return here
        except (OSError, ValueError, AttributeError):
            continue
    return recorded


def _own_spec_digest(run_dir: str):
    """The spec digest the run's own sign-off record binds (decision_spec.signoff.json; never a legacy brief's)."""
    from toolkit.spec import signoff as SO
    try:
        rec = json.load(open(os.path.join(run_dir, SO.SPEC_SIGNOFF)))
        m = rec.get("sha256")
        return (m.get(AB.SPEC_FILE) if isinstance(m, dict) else None) or rec.get("spec_sha256")
    except (OSError, ValueError, AttributeError):
        return None


def _made_by(child: str, req: dict, parent: str) -> bool:
    """`child` is the run toolkit/spec/amend.py made from this rethink request of `parent`: from the signed spec the
    request names, which the parent's own sign-off binds, with the request's patch (its id and content digest). A
    same-named child from another backup or another patch is not this request's (Codex r1 and r2 of #349)."""
    from toolkit.spec import amend as AM
    try:
        meta = json.load(open(os.path.join(child, "metadata.json")))
    except (OSError, ValueError):
        return False
    req = req if isinstance(req, dict) else {}
    sig, patch = req.get("signed_spec_sha256"), req.get("patch")
    if not (isinstance(meta, dict) and isinstance(sig, str) and isinstance(patch, dict)):
        return False
    return meta.get("parent_spec_sha256") == sig == _own_spec_digest(parent) \
        and meta.get("amendment") == str(patch.get("id") or "unnamed") \
        and meta.get("patch_sha256") == AM.patch_sha256(patch)


def handoff_child(run_dir: str) -> str | None:
    """The child run a provisional lead amendment handed this run to (`amended_provisional`), when it has a state."""
    st = _state_or_none(run_dir)
    if st is None or st.status != AMENDED_PROVISIONAL:
        return None
    child = _child_of(run_dir, next((h.get("amended_to") for h in reversed(st.history)
                                     if isinstance(h, dict) and h.get("amended_to")), None), strict=True)
    return child if child and os.path.isfile(os.path.join(child, STATE_FILE)) else None


def _state_or_none(run_dir: str):
    """The run's state, or None when it is missing or cannot be read as one (Codex r3 of #349: `[]` too)."""
    try:
        return load_state(run_dir)
    except Exception:  # noqa: BLE001 -- unreadable, or not a run state
        return None


def handoff_chain(run_dir: str, limit: int = 8, kept: bool = False) -> list[str]:
    """Every child run this run continued in, in order; with `kept`, also a child an amendment refused at signing
    left for the owner, or one an interrupted hand-off signed (main.py stores each with the run: Codex r1): only
    after a recorded refusal or an unfinished hand-off, and only the child this run's request made (Codex r3 on #342,
    P1-11; Codex r1 of #349)."""
    return list(iter_handoff_chain(run_dir, limit, kept))


def iter_handoff_chain(run_dir: str, limit: int = 8, kept: bool = False):
    """handoff_chain, one run at a time: main.py stores each run before it reads the next (Codex r2 of #349)."""
    out, cur = [], run_dir
    while len(out) < limit:
        nxt = handoff_child(cur)
        if nxt is None and kept:
            nxt = _kept_child(cur)
            if nxt is not None and nxt not in out:
                yield nxt
            return
        if nxt is None or nxt in out:
            return
        out.append(nxt)
        yield nxt
        cur = nxt


def _kept_child(run_dir: str) -> str | None:
    """A refused amendment's kept child, or the lead-signed child of a hand-off interrupted before the parent was saved
    FINAL; only the child this run's request made (Codex r1 and r2 of #349)."""
    try:
        req = json.load(open(os.path.join(run_dir, SPEC_RETHINK_REQUEST)))
    except (OSError, ValueError):
        return None
    la = req.get("lead_amendment") if isinstance(req, dict) else None
    if not isinstance(la, dict):
        return None
    status = la.get("status")
    if status in ("started", "signed_provisionally"):
        st = _state_or_none(run_dir)
        if st is None or st.status == AMENDED_PROVISIONAL:
            return None
    elif status != "refused":
        return None
    child = _child_of(run_dir, la.get("child"))
    if child is None or not _made_by(child, req, run_dir):
        return None
    if status != "refused":              # an unfinished hand-off: only a child with its state and the lead's signature
        from toolkit.spec import signoff as SO
        if not (_state_or_none(child) is not None and SO.status(child) == "signed"
                and (SO._spec_record(child) or {}).get("signed_by") == "lead"):
            return None
    return child


def _self_test() -> int:
    """The cases live in tests/stage_machine/ (moved there verbatim by scripts/refactor/split_stage_machine.py);
    they run against this module, imported there as _stage_machine_under_test."""
    sys.modules["_stage_machine_under_test"] = sys.modules[__name__]
    for k in [k for k in sys.modules if k.startswith("sm_selftest_")]:
        del sys.modules[k]          # a second call (another copy of this module) binds the cases to it afresh
    d = os.path.join(REPO, "tests", "stage_machine")
    if d not in sys.path:
        sys.path.append(d)
    import sm_selftest_core
    return sm_selftest_core._self_test()


def main(argv=None) -> int:
    import argparse
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--self-test"]:
        return _self_test()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
                                 allow_abbrev=False)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s1 = sub.add_parser("status", allow_abbrev=False)
    s1.add_argument("run_dir")
    s2 = sub.add_parser("sign", allow_abbrev=False)
    s2.add_argument("run_dir")
    s2.add_argument("--by", choices=["owner"], required=True)
    s2.add_argument("--note", default="")
    s2.add_argument("--expect-sha256", default=None,
                    help="the sha256 of the decision_spec.yaml the owner read; a different spec is not signed")
    s2.add_argument("--expect-derivations-sha256", default=None,
                    help="the sha256 of data/derivations_record.json when the sheet was shown ('none': there was none)")
    s2.add_argument("--expect-sheet-sha256", default=None,
                    help="the sha256 of the sign-off sheet the owner read ('none': there was none)")
    a = ap.parse_args(argv)
    if a.cmd == "status":
        st = load_state(a.run_dir)
        if st is None:
            print("no run_state.json")
            return 2
        print(json.dumps({k: v for k, v in st.to_dict().items() if k != "history"}, indent=1, default=str))
        for h in st.history[-15:]:
            print(h)
        return 0
    b = AB.backend()
    kw = {k: v for k, v in (("expect_sha256", a.expect_sha256),
                            ("expect_derivations_sha256", a.expect_derivations_sha256),
                            ("expect_sheet_sha256", a.expect_sheet_sha256)) if v}
    if not getattr(b, "sign", None) and set(kw) - {"expect_sha256"}:
        print("Not signed: this backend's sign-off records no derivation record or sheet hash", file=sys.stderr)
        return 1
    try:
        rec = b.sign(a.run_dir, a.by, a.note, **kw) if getattr(b, "sign", None) else sign(a.run_dir, a.by, a.note, **kw)
    except ValueError as e:
        print(f"Not signed: {e}", file=sys.stderr)
        return 1
    print(json.dumps(rec, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
