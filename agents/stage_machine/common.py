"""Paths, run-file names, caps, the run state and its persistence.

Moved verbatim from agents/stage_machine.py by scripts/refactor/split_stage_machine.py.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import analysis_backend as AB
import findings_ledger as FL

from .edges import STATES


HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # agents/, as before the split
REPO = os.path.dirname(HERE)
for _p in (REPO, os.path.join(REPO, "scripts"), HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

STATE_FILE = "run_state.json"
SIGNOFF_FILE = "decision_spec.signoff.json"
SCOPE_SIGNOFF = "scope_signoff.json"      # Phase 49 β (PR #85)
SIGNOFF_SHEET = "decision_spec.signoff_sheet.md"   # v2-derived-inputs: what signing accepts beyond the spec's text
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


class AwaitingBudget(Exception):
    """Raised by Machine._task when the next paid task's allowance is below MIN_TASK_ALLOWANCE_USD; step() pauses."""
# Findings whose resolution bears on the primary decision: Phase 49 β's owner rule decides
# whether they may be declared. Without that rule they are not declarable (fail closed).
DECISION_BEARING_KINDS = ("decision_test_changes_plan",)
MAX_SPEC_TRIES = 3
MAX_SCHEMA_REPAIRS = 2
MAX_SIGN_REPAIRS = 2          # a refused lead sign-off goes back to EVIDENCE or SPEC at most this often, then waits


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


_remedy, short_label = FL.remedy, FL.short_label


DECLARED_FILE = "declared_findings.yaml"


def _sha256(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


AWAITING_SPEC_AMENDMENT = "awaiting_spec_amendment"
SPEC_RETHINK_REQUEST = "spec_rethink_request.json"


def _write_json(run_dir: str, rel: str, obj) -> None:
    """A harness record in the run dir, published atomically with no link followed (lib.safe_io)."""
    from lib.safe_io import write_nofollow
    write_nofollow(run_dir, rel.replace(os.sep, "/"), json.dumps(obj, indent=1, default=str).encode(), atomic=True)


ACCEPTED_STATUSES = ("completed", "completed_with_report_restored")


# A relaunch with reuse (a new run dir from another one's spec and stored rows) names its parent here; the launcher
# sets it. main.py gains a --parent-run flag after v1 retirement, without changing the record.
PARENT_ENV = "MA_PARENT_RUN"
