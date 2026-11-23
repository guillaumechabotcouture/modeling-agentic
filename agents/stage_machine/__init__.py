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
# split_stage_machine: the code lives in the package agents/stage_machine/
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

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # agents/, as before
_REPO = os.path.dirname(_HERE)
for _p in (_REPO, os.path.join(_REPO, "scripts"), _HERE):     # the module's own order
    if _p not in sys.path:
        sys.path.insert(0, _p)

import analysis_backend as AB  # noqa: E402,F401
import findings_ledger as FL  # noqa: E402,F401
import fix_loop as FX  # noqa: E402,F401
from .common import (  # noqa: E402,F401
    ACCEPTED_STATUSES, AWAITING_BUDGET, AWAITING_SPEC_AMENDMENT, AwaitingBudget, CRITICS, DECISION_BEARING_KINDS,
    DECLARED_FILE, HERE, MAX_SCHEMA_REPAIRS, MAX_SIGN_REPAIRS, MAX_SPEC_TRIES, MIN_TASK_ALLOWANCE_USD, PARENT_ENV,
    PROTECTED_BY_FIX, READBACK_CAP_USD, REPO, RunState, SCOPE_SIGNOFF, SIGNOFF_FILE, SIGNOFF_SHEET,
    SPEC_RETHINK_REQUEST, STATE_FILE, TASK_CAP_USD, _now, _put, _quarantine_report, _remedy, _sha256, _state_bytes,
    _withhold_report, _write_json, load_state, save_state, short_label,
)
from .edges import EDGES, LLM_STATES, POST_CHOICE_STATES, STATES, rethink_target, route_gate  # noqa: E402,F401
from .amend import (  # noqa: E402,F401
    AMENDED_PROVISIONAL, AMENDMENT_RECORD, AMEND_LOCK, AmendmentLocked, _LOCK_HELD, _amend_lock, _child_of,
    _kept_child, _made_by, _own_spec_digest, _parent_of, _state_or_none, handoff_chain, handoff_child,
    iter_handoff_chain,
)
from .signoff import (  # noqa: E402,F401
    _signoff, _spec_signed_sha, evidence_gate, sign, signed_hash, spec_signed_once, spec_signoff_findings,
)
from .findings import (  # noqa: E402,F401
    _SUFFIX, deliverable_problems, given_values, harness_owned, owned_path, report_acceptance_findings,
    report_claim_findings, typed_numbers,
)
from .owner import (  # noqa: E402,F401
    OWNER_REQUEST, OWNER_REQUEST_PRODUCER, _answer_counts, request_owner_amendments,
)
from .report import (  # noqa: E402,F401
    STATUS_MARK, _status_line, _write_deliverable, amendment_lines, provisional_lines, render_report,
)
from .failures import (  # noqa: E402,F401
    BATCH_DIR, JC_DIR, _KILL_SIGNALS, _RESUME_WHY, _TB_EXC, _TB_FRAME, _TB_HEAD, _infra_batch, _pre_engine_check,
    _rep_error, _step_error, _with_abs_paths, analysis_failure_cause, resumable_failure,
)
from .machine import Machine  # noqa: E402,F401
from .states.spec import (  # noqa: E402,F401
    CARRIED, LEGACY_BRIEF_SIGNOFF, PROVENANCE, _INPUT_EXT, _LEADING_PUNCT, _TRAILING_PUNCT, _carry_feedback,
    _copy_nofollow, _draft_file_problems, _path_candidate, _provenance, _question_paths, _safe_dest,
    _special_files, answer_key_path, record_cited_inputs, rethink_request, stage_inputs,
)
from .states.analyze import (  # noqa: E402,F401
    CALIBRATION_REFUSED, MAX_CALIBRATION_REFUSALS, calibration_refusal, conflicts_policy_stop,
    record_calibration_refusal,
)
from .states.gate import (  # noqa: E402,F401
    MAX_LANGUAGE_REPAIRS, OWNER_LIFECYCLE_WIRED, _bearing_item, _earlier_round, _owner_rule, apply_medium_rule,
    apply_owner_declines, apply_owner_rule, drop_declined, legacy_gate, open_findings, owner_accepted_now,
    pending_amendments, pending_conflicts, require_judged, v2_gate, write_declared,
)
from .states.fix_loop import _Journaled, owner_held  # noqa: E402,F401
from .states.owner_decision import _plan_changing  # noqa: E402,F401
from .run import (  # noqa: E402,F401
    _parent_record, _record_invocation, _run_v2_chain, _run_v2_once, _runtime_ids, init_run, run_v2,
)
from . import namespace as _namespace  # noqa: E402

_namespace.share(sys.modules[__name__])     # an assignment to this module reaches the package


def _self_test() -> int:
    """The cases live in tests/stage_machine/ (moved there verbatim by scripts/refactor/split_stage_machine.py);
    they run against this module, imported there as _stage_machine_under_test."""
    sys.modules["_stage_machine_under_test"] = sys.modules[__name__]
    d = os.path.join(REPO, "tests", "stage_machine")
    for k in ('sm_selftest_auto1a', 'sm_selftest_calibration_refused', 'sm_selftest_common', 'sm_selftest_core', 'sm_selftest_derived', 'sm_selftest_evidence', 'sm_selftest_geometry', 'sm_selftest_hb2_spec', 'sm_selftest_lead_amend', 'sm_selftest_lead_amend_r3', 'sm_selftest_lead_amend_r4', 'sm_selftest_lead_amend_r5', 'sm_selftest_resume', 'sm_selftest_spec_p1s', 'sm_selftest_writeup_rounds'):     # the generated case modules, whatever is cached under their names: loaded afresh here
        sys.modules.pop(k, None)
    while d in sys.path:
        sys.path.remove(d)
    sys.path.insert(0, d)           # this checkout's cases, not another's
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
