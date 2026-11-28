#!/usr/bin/env python3
"""v2 C (S4): the code-owned run state machine.

    python3 agents/stage_machine.py status <run_dir>
    python3 agents/stage_machine.py sign <run_dir> --by owner [--note "..."]      # the owner signs the spec
    python3 agents/stage_machine.py --self-test

    main.py --v2 "<question>" ...     # a real run: agents/v2_sdk_tasks.py fills the LLM tasks

Design: docs/design/v2c-orchestration.md. Code owns the flow: the state, every
transition and every number. LLMs do bounded tasks (agents/v2_tasks.py). The state is
persisted in run_state.json after every transition, so a run resumes where it stopped.

States: ACQUIRE -> SPEC -> EVIDENCE -> SIGN -> ENGINE_CHOICE ->
ANALYZE -> CRITIQUE -> GATE -> FIX_LOOP | WRITE | OWNER_DECISION | back-edges -> FINAL.

The gate's rules live in agents/v2_gate.py, wrapping validate_critique_yaml, mechanical
done_when checks, and owner sign-off; report rendering lives in agents/report_render.py;
built-engine handlers and locking live in agents/engine_build_stage.py.
"""
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
from agents.engine_build_stage import (  # noqa: E402
    AWAITING_BUDGET,
    AWAITING_ENGINE_SIGNOFF,
    ENGINE_BUILD_CAP_MAX_USD,
    ENGINE_BUILD_CAP_USD,
    ENGINE_BUILD_TRY_CAP_USD,
    ENGINE_ADMIT_COMPUTE_CAP_USD,
    ENGINE_DECLINED,
    ENGINE_NOT_ADMITTED,
    ENGINE_REFERENCE_CAP_USD,
    ENGINE_REVIEW_CAP_USD,
    EngineBuildMixin,
    MAX_CRITIQUE_ROUNDS,
    MAX_ENGINE_BUILD_TRIES,
    MAX_ENGINE_REFERENCE_TRIES,
    MAX_ENGINE_REPAIRS,
    MIN_TASK_ALLOWANCE_USD,
    POST_CHOICE_STATES,
    POST_ENGINE_SIGN_STATES,
    RUN_LOCK,
    RunLocked,
    _now,
    _run_lock,
    spec_has_engine_build,
)
from agents.report_render import (  # noqa: E402
    ACCEPTED_STATUSES,
    AMENDMENT_RECORD,
    DECLARED_FILE,
    DELIVERED_STATUSES,
    PROTECTED_BY_FIX,
    SCOPE_SIGNOFF,
    SIGNOFF_FILE,
    SIGNOFF_SHEET,
    SPEC_RETHINK_REQUEST,
    STATE_FILE,
    STATUS_MARK,
    _STATUS_LINES,
    _draft_file_problems,
    _lead_may_accept,
    _ledger_claims,
    _plan_changing,
    _put,
    _quoted,
    _render_stale,
    _retire_stale_withheld,
    _spec_signed_sha,
    _special_files,
    _status_line,
    _withhold_report,
    _write_deliverable,
    _write_json,
    amendment_lines,
    deliverable_problems,
    dependence_lines,
    harness_owned,
    owned_path,
    provisional_lines,
    render_report,
    report_claim_findings,
    signed_hash,
    write_declared,
)
from agents.v2_gate import (  # noqa: E402
    CRITICS,
    DECISION_BEARING_KINDS,
    DT_DISPOSITION,
    INTERVAL_KIND,
    OWNER_LIFECYCLE_WIRED,
    _answer_counts,
    _bearing_fp,
    _bearing_item,
    _bearing_record,
    _owner_rule,
    apply_held_rule,
    apply_medium_rule,
    apply_owner_declines,
    apply_owner_rule,
    held_entry_id,
    open_findings,
    owner_accepted_now,
    owner_held,
    require_judged,
    rethink_target,
    route_gate,
    scope_request_view,
    v2_gate,
)

STATES = ("ACQUIRE", "SPEC", "EVIDENCE", "SIGN", "ENGINE_CHOICE",
          "ENGINE_BUILD", "ENGINE_ADMIT", "ENGINE_REVIEW", "ENGINE_SIGN",
          "ANALYZE", "CRITIQUE",
          "GATE", "FIX_LOOP", "OWNER_DECISION", "WRITE", "FINAL")
# The state table: every transition the machine may take. A transition outside it is a bug
# (raised, never taken).
EDGES = {
    "ACQUIRE": {"SPEC", "ACQUIRE", "FINAL"},
    "SPEC": {"EVIDENCE", "SPEC", "ACQUIRE", "FINAL"},
    "EVIDENCE": {"SIGN", "SPEC", "ACQUIRE", "FINAL"},
    "SIGN": {"ENGINE_CHOICE", "EVIDENCE", "SPEC", "SIGN", "FINAL"},
    "ENGINE_CHOICE": {"ENGINE_BUILD", "ANALYZE", "FINAL"},
    "ENGINE_BUILD": {"ENGINE_ADMIT", "ENGINE_BUILD", "FINAL"},
    "ENGINE_ADMIT": {"ENGINE_REVIEW", "ENGINE_BUILD", "SPEC", "FINAL"},
    "ENGINE_REVIEW": {"ENGINE_SIGN", "ENGINE_BUILD", "ENGINE_REVIEW", "FINAL"},
    "ENGINE_SIGN": {"ANALYZE", "ENGINE_SIGN", "ENGINE_BUILD", "FINAL"},
    "ANALYZE": {"CRITIQUE", "SPEC", "FINAL"},
    "CRITIQUE": {"GATE", "WRITE", "FINAL"},
    "GATE": {"WRITE", "FIX_LOOP", "SPEC", "ENGINE_CHOICE", "ENGINE_BUILD", "OWNER_DECISION", "CRITIQUE", "FINAL"},
    "FIX_LOOP": {"CRITIQUE", "SPEC", "ENGINE_CHOICE", "ENGINE_BUILD", "WRITE", "OWNER_DECISION", "FINAL"},
    "OWNER_DECISION": {"GATE", "OWNER_DECISION", "FINAL"},
    "WRITE": {"CRITIQUE", "GATE", "FINAL"},
    "FINAL": set(),
}
LLM_STATES = ("ACQUIRE", "SPEC", "ENGINE_CHOICE", "ENGINE_BUILD", "ENGINE_REVIEW", "CRITIQUE", "FIX_LOOP", "WRITE")
TASK_CAP_USD = 3.0            # one bounded task's allowance, at most what the run has left
READBACK_CAP_USD = 0.25       # one crash read-back (resume for one tool-less turn), at most what is left
OWNER_REQUEST = "scope_signoff_request.json"   # scripts/scope_signoff.py REQUEST: `show` / `sign` read it


class AwaitingBudget(Exception):
    """Raised by Machine._task when the next paid task's allowance is below MIN_TASK_ALLOWANCE_USD; step() pauses."""


MAX_SPEC_TRIES = 3
MAX_SCHEMA_REPAIRS = 2
MAX_CALIBRATION_REFUSALS = 2  # a refused calibration goes back to SPEC at most this often, then the run ends run_failed
CALIBRATION_REFUSED = "calibration_refused"      # its findings-ledger kind and id
# v2 ACQUIRE (docs/design/acquire.md §§2.1, 6.2)
MAX_ACQUIRE_ROUNDS = 2
MAX_ACQUIRE_RETRIES = 1
ACQUIRE_CAP_USD = 6.0
AWAITING_INPUTS = "awaiting_inputs"


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
    # Dedicated development flag (--dev-unsandboxed-engine) for agent-built engine execution on hosts without
    # bubblewrap; strictly separate from derive_unsandboxed and persists a HIGH non-declarable gate finding.
    engine_unsandboxed: bool = False
    # Every paid task: key -> {task, status started|completed, allowance_usd, usd, session_id,
    # started, completed, ok, output, note}. Written before and after each call, so a resumed run
    # never repeats a completed task or loses its charge, and charges an interrupted one its allowance.
    journal: dict = field(default_factory=dict)
    # #363 agent-built engine lane state: {id, kind, status, tries, repairs, spent_usd, compute_reserved_usd,
    # snapshot_sha256, admission_sha256, hidden_commitment}. Empty for runs using registered engines.
    engine_build: dict = field(default_factory=dict)
    engine_build_cap_usd: float = ENGINE_BUILD_CAP_USD
    # v2 ACQUIRE (docs/design/acquire.md §2.1): off | auto; older states without these fields load as off / {}
    acquire: str = "off"
    acquired: dict = field(default_factory=dict)
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


def _state_bytes(st: RunState) -> bytes:
    """run_state.json's bytes for `st`."""
    return json.dumps(st.to_dict(), indent=1, default=str).encode()


def save_state(run_dir: str, st: RunState) -> None:
    """run_state.json, published atomically with no link followed (lib.safe_io; review round 4, P1-2)."""
    from lib.safe_io import write_nofollow
    write_nofollow(run_dir, STATE_FILE, _state_bytes(st), atomic=True)



# ------------------------------------------------------------------ gates
def spec_signoff_findings(run_dir: str, signoff: str, backend=None) -> list[dict]:
    """decision_spec.yaml must not change after sign-off (non-declarable)."""
    if signoff == "none":
        return []
    backend = backend or AB.backend("toolkit")
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
    from agents import acquire as ACQ
    if rec.get("acquired_inventory_sha256") or ACQ.has_acquired(run_dir):
        try:
            v_acq = ACQ.verify_inventory(run_dir)
            cur_inv = v_acq.get("inventory_sha256")
        except ACQ.ArtifactTampered as e:
            return [{"kind": "spec_changed_after_signoff", "severity": "HIGH", "stage": "PLAN",
                     "target_stage": "PLAN", "key": "acquired_inventory", "scope_declarable": False,
                     "claim": f"acquired inventory changed after sign-off: {e}"[:600]}]
        if rec.get("acquired_inventory_sha256") != cur_inv:
            return [{"kind": "spec_changed_after_signoff", "severity": "HIGH", "stage": "PLAN",
                     "target_stage": "PLAN", "key": "acquired_inventory", "scope_declarable": False,
                     "claim": "acquired inventory changed after sign-off: restore the signed inputs or re-sign"}]
    return []


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
    prev = _provenance(run_dir)
    kept_acq = {k: v for k, v in (prev.get("inputs") or {}).items()
                if isinstance(v, dict) and v.get("origin") == "acquired"}
    prov = {"inputs": dict(kept_acq), "cited": prev.get("cited") or {}}
    if "acquired_inventory_sha256" in prev:
        prov["acquired_inventory_sha256"] = prev["acquired_inventory_sha256"]
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


def record_cited_inputs(run_dir: str, derived_inputs: bool = True) -> list[str]:
    """Every file decision_spec.yaml cites must be inside the run directory; each is recorded with
    its sha256 in data/inputs_provenance.json. Returns the problems (pre-sign). `derived_inputs`: the run derives its
    units table, so a missing one's hint names the staged file as a derivation's input (Codex r3 of #355)."""
    import yaml
    import pipeline as P
    try:
        spec = yaml.safe_load(open(os.path.join(run_dir, AB.SPEC_FILE))) or {}
    except Exception as e:  # noqa: BLE001
        return [f"{AB.SPEC_FILE} unreadable: {type(e).__name__}"]
    found = P.cited_paths(spec, run_dir)       # one walk, shared with the derived-input checks
    prov, probs, root = _provenance(run_dir), [], os.path.realpath(run_dir)
    # The staged copies must still be the files the question named (the draft task may not touch
    # them; this checks it). Acquired inputs have no repository source and are verified via verify_inventory (§4).
    from agents import acquire as ACQ
    saw_acq = False
    for dest, e in (prov.get("inputs") or {}).items():
        if isinstance(e, dict) and e.get("origin") == "acquired":
            saw_acq = True
            continue
        p = os.path.join(run_dir, dest)
        src = os.path.join(REPO, e.get("source", ""))
        if not os.path.isfile(p) or os.path.islink(p) or _sha256(p) != e.get("sha256"):
            probs.append(f"the staged input {dest} was changed or removed after it was copied")
        elif os.path.isfile(src) and _sha256(src) != e.get("sha256"):
            probs.append(f"the staged input {dest} no longer matches its source {e.get('source')}")
    if saw_acq or ACQ.has_acquired(run_dir):
        try:
            ACQ.verify_inventory(run_dir)
        except ACQ.ArtifactTampered as e:
            probs.append(f"acquired inventory failed verification: {e}")
    probs.extend(ACQ.unlisted_acquired_citations(run_dir, spec if isinstance(spec, dict) else {}))
    units = (spec.get("decision_units") or {}).get("csv") if isinstance(spec, dict) \
        and isinstance(spec.get("decision_units"), dict) else None
    for rel in sorted(set(found)):
        p = os.path.realpath(os.path.join(run_dir, rel))
        if not p.startswith(root + os.sep):
            probs.append(f"the spec cites {rel}, outside the run directory: copy it into data/")
        elif os.path.isdir(p):
            prov.setdefault("cited", {})[os.path.relpath(p, root).replace(os.sep, "/") + "/"] = {"dir": True}
        elif not os.path.isfile(p):
            probs.append(f"the spec cites {rel}, which does not exist"
                         + P.staged_copy_hint(rel, run_dir, derive=derived_inputs and isinstance(units, str)
                                              and units.strip() == rel))
        else:
            prov.setdefault("cited", {})[os.path.relpath(p, root).replace(os.sep, "/")] = {"sha256": _sha256(p)}
    os.makedirs(os.path.join(run_dir, "data"), exist_ok=True)
    _write_json(run_dir, PROVENANCE, prov)
    return probs


# ------------------------------------------------------------------ sign-off
def _signoff(run_dir: str) -> dict | None:
    try:
        return json.load(open(os.path.join(run_dir, SIGNOFF_FILE)))
    except (OSError, ValueError):
        return None


def sign(run_dir: str, by: str, note: str = "", expect_sha256: str | None = None,
         expect_derivations_sha256: str | None = None, expect_sheet_sha256: str | None = None) -> dict:
    """Sign the run's decision spec via the analysis backend (`toolkit/spec/signoff.py`)."""
    return AB.backend("toolkit").sign(
        run_dir, by, note, expect_sha256=expect_sha256,
        expect_derivations_sha256=expect_derivations_sha256,
        expect_sheet_sha256=expect_sheet_sha256)


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


def request_owner_amendments(run_dir: str, round_n: int, ledger: dict, ids: list[str]) -> dict:
    """F6 run 3: the findings whose remedy is an owner amendment of the signed spec, written as a request in
    scripts/scope_signoff.py's format, so the owner reads it with `scope_signoff.py show` and answers with `sign`
    (accept: the run goes on without the amendment, declared; decline: the finding stays open, the owner's). Each item's
    fingerprint binds its claim and remedy."""
    items = []
    for fid in ids:
        f = ledger.get(fid)
        claim, fix = (f.claim, f.fix_requires) if f is not None else ("", None)
        fp = FL.answer_fingerprint(fid, kind=getattr(f, "kind", None) or FL.CRITIC_KIND, fix_requires=fix,
                                   claim=claim)       # the ledger binds the owner's answer to it (hb2 fix 3)
        conflict = getattr(f, "kind", None) == "evidence_conflict_declared"
        opts = {}
        if conflict:            # owner decision 2026-11-21: a refit is listed, with its estimated cost, never run
            from toolkit.analysis import conflicts as CF
            opts = {"options": CF.options(run_dir, getattr(f, "key", "")),
                    "conflict": CF.verdict(run_dir, getattr(f, "key", ""))}     # Codex r4 of #316: the structured verdict
        items.append({"finding": fid, "kind": getattr(f, "kind", None) or FL.CRITIC_KIND,
                      "severity": getattr(f, "severity", None), "source": getattr(f, "source", None),
                      "claim": claim, "fix_requires": fix, **opts,
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
class Machine(EngineBuildMixin):
    def __init__(self, run_dir: str, tasks, state: RunState | None = None, backend=None, log=print,
                 cross_engine_check: bool = True):
        self.run_dir = os.path.abspath(run_dir)
        self.tasks = tasks
        self.backend = backend or AB.backend()
        self.log = log
        self.cross = cross_engine_check
        st = load_state(self.run_dir)
        eff = st or state
        need_lock = eff is not None and (
            eff.state in ("ENGINE_BUILD", "ENGINE_ADMIT", "ENGINE_REVIEW", "ENGINE_SIGN")
            or bool(getattr(eff, "engine_build", None))
            or spec_has_engine_build(self.run_dir)
        )
        if need_lock:
            # a built-engine run's state is read again and written back under _run_lock (BE31)
            with _run_lock(self.run_dir):
                self._load(load_state(self.run_dir), state)
        else:
            self._load(st, state)

    def _load(self, st: RunState | None, state: RunState | None) -> None:
        self.st = st or state
        if self.st is None:
            raise ValueError("no run_state.json and no initial state")
        self._settle_interrupted()
        save_state(self.run_dir, self.st)

    def _save(self) -> None:
        save_state(self.run_dir, self.st)

    def _charge_interrupted_entry(self, j: dict) -> None:
        chg = self._interrupted_charge(j)
        self.st.spent_usd += chg
        if (j.get("build_budget") or j.get("task") in ("build_engine", "build_reference", "critique_engine")) and isinstance(
            self.st.engine_build, dict
        ):
            self.st.engine_build["spent_usd"] = round(
                float(self.st.engine_build.get("spent_usd") or 0.0) + chg, 4
            )
        if (j.get("acquire_budget") or j.get("task") == "acquire") and isinstance(self.st.acquired, dict):
            self.st.acquired["spent_usd"] = round(
                float(self.st.acquired.get("spent_usd") or 0.0) + chg, 4
            )
        j["interrupted_charged"] = j.get("interrupted_charged", 0) + 1
        j["status"] = "interrupted"

    def _settle_interrupted(self) -> None:
        """Every journal entry left `started` by a previous process was interrupted: charge its
        allowance once (the task cap when it had none) to the run and (for build-lane tasks) the
        build budget, then mark it interrupted before anything else runs."""
        for j in self.st.journal.values():
            if isinstance(j, dict) and j.get("status") == "started":
                self._charge_interrupted_entry(j)

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
            if frm == "GATE" and (self.st.last_gate or {}).get("post_write") and self.st.last_gate.get("action") \
                    and self.st.status in DELIVERED_STATUSES:     # a gate decision that delivers the report (Codex r2)
                _retire_stale_withheld(self.run_dir)
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

    def _task(self, key: str, name: str, *args, writeup: bool = False,
              cap_override: float | None = None, build_budget: bool = False,
              acquire_budget: bool = False, **kwargs):
        """One paid task through the journal. Returns its TaskResult, or None when the run has no
        allowance left for it (the caller takes the budget path). A completed task is not run
        again (its result is replayed, already charged); a task found started but not completed
        was interrupted: it is charged its allowance once, then run again."""
        from v2_tasks import TaskResult
        tamper = self._engine_tamper_problems()
        if tamper:
            raise RuntimeError("artifact_tampered: " + "; ".join(tamper[:4]))
        acq_tamper = self._acquired_tamper_problem()
        if acq_tamper:
            raise RuntimeError("artifact_tampered: " + acq_tamper)
        j = self.st.journal.get(key)
        if j and j.get("status") == "completed":
            return TaskResult(bool(j.get("ok")), j.get("output"), 0.0, j.get("note", ""), j.get("session_id"))
        if j and j.get("status") == "started":
            self._charge_interrupted_entry(j)
            save_state(self.run_dir, self.st)
        allowance = self._allowance(writeup)
        if cap_override is not None:
            allowance = round(min(cap_override, allowance), 4) if allowance is not None else float(cap_override)
        bleft = None
        if build_budget:
            bcap = float(self.st.engine_build_cap_usd or ENGINE_BUILD_CAP_USD)
            bspent = float((self.st.engine_build or {}).get("spent_usd") or 0.0)
            bleft = round(bcap - bspent, 4)
            if bleft < MIN_TASK_ALLOWANCE_USD:
                self.st.paused, self.st.status = AWAITING_BUDGET, None
                self.st.history.append({"at": _now(), "paused": AWAITING_BUDGET, "reason": "engine_build_cap",
                                        "state": self.st.state, "task": name, "key": key,
                                        "allowance_usd": bleft, "floor_usd": MIN_TASK_ALLOWANCE_USD,
                                        "spent_usd": round(bspent, 4), "cap_usd": bcap})
                save_state(self.run_dir, self.st)
                self.log(f"[machine] {name} not started: engine build balance ${bleft:.2f} is below the "
                         f"${MIN_TASK_ALLOWANCE_USD:.2f} floor (spent ${bspent:.2f} of ${bcap:.2f}). Resume with "
                         f"--engine-build-usd <higher cap>")
                raise AwaitingBudget(name)
            allowance = round(min(bleft, allowance), 4) if allowance is not None else bleft
        aleft = None
        if acquire_budget:
            acap = float(ACQUIRE_CAP_USD)
            aspent = float((self.st.acquired or {}).get("spent_usd") or 0.0)
            aleft = round(acap - aspent, 4)
            if aleft < MIN_TASK_ALLOWANCE_USD:
                self.st.paused, self.st.status = AWAITING_BUDGET, None
                self.st.history.append({"at": _now(), "paused": AWAITING_BUDGET, "reason": "acquire_cap",
                                        "state": self.st.state, "task": name, "key": key,
                                        "allowance_usd": aleft, "floor_usd": MIN_TASK_ALLOWANCE_USD,
                                        "spent_usd": round(aspent, 4), "cap_usd": acap})
                save_state(self.run_dir, self.st)
                self.log(f"[machine] {name} not started: acquisition balance ${aleft:.2f} is below the "
                         f"${MIN_TASK_ALLOWANCE_USD:.2f} floor (spent ${aspent:.2f} of ${acap:.2f}).")
                raise AwaitingBudget(name)
            acq_cap_call = round(min(self.st.task_cap_usd, aleft), 4)
            allowance = round(min(acq_cap_call, allowance), 4) if allowance is not None else acq_cap_call
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
        a = allowance if allowance is not None else float(cap_override or self.st.task_cap_usd)
        b = self._budget(writeup)
        # A crash read-back is a paid call too: a small cap, from what is left after this allowance (for a
        # write-up task, left of the reserve it may use; for build/acquire tasks, also bounded by lane balance).
        rb = READBACK_CAP_USD if b is None else max(0.0, min(READBACK_CAP_USD, round(b["left"] - a, 4)))
        if bleft is not None:
            rb = max(0.0, min(rb, round(bleft - a, 4)))
        if aleft is not None:
            rb = max(0.0, min(rb, round(aleft - a, 4)))
        # A write-up, build-lane, or acquire task spends up to the cap itself, so its read-back allowance is part
        # of what an interrupted one is charged (_interrupted_charge): both run and lane caps hold on resume.
        j = dict(j or {}, task=name, status="started", started=_now(), allowance_usd=allowance, writeup=bool(writeup),
                 build_budget=bool(build_budget), acquire_budget=bool(acquire_budget),
                 readback_usd=rb if (writeup or build_budget or acquire_budget) else 0.0)
        self.st.journal[key] = j
        save_state(self.run_dir, self.st)
        setter = getattr(self.tasks, "set_allowance", None)
        if setter:
            setter(a, readback_usd=rb)
        res = getattr(self.tasks, name)(*args, **kwargs)
        self._charge(res)
        if build_budget and isinstance(self.st.engine_build, dict):
            self.st.engine_build["spent_usd"] = round(
                float(self.st.engine_build.get("spent_usd") or 0.0) + float(getattr(res, "usd", 0.0) or 0.0), 4
            )
        if acquire_budget and isinstance(self.st.acquired, dict):
            self.st.acquired["spent_usd"] = round(
                float(self.st.acquired.get("spent_usd") or 0.0) + float(getattr(res, "usd", 0.0) or 0.0), 4
            )
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
        if s in ("CRITIQUE", "FIX_LOOP", "WRITE"):
            return self._go("FINAL", "budget used")
        return self._go("FINAL", "budget used before the model was built", "budget_exhausted")

    def _budget(self, writeup: bool = False) -> dict | None:
        """What is left under the cap: less the write-up reserve, except for the write-up's own tasks (WRITE and its
        inline presentation review), which the reserve is held for."""
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
        import built_engine_gate as BEG
        out += BEG.gate_findings(self.run_dir)
        tamper = self._engine_tamper_problems()
        if tamper:
            out.append({
                "kind": "engine_snapshot_tampered",
                "severity": "HIGH",
                "stage": "GATE",
                "target_stage": "ENGINE",
                "key": "engine_snapshot",
                "scope_declarable": False,
                "claim": "agent-built engine snapshot failed verification: " + "; ".join(tamper[:4]),
            })
        from agents import acquire as ACQ
        if ACQ.has_acquired(self.run_dir):
            try:
                from toolkit.spec import evidence_dossier as ED
                from lib.safe_io import read_nofollow
                spec_q = self._spec() if os.path.exists(os.path.join(self.run_dir, AB.SPEC_FILE)) else {}
                drel = ED.dossier_reference(spec_q) or ED.DOSSIER_FILE
                if os.path.isfile(os.path.join(self.run_dir, drel)):
                    db = read_nofollow(self.run_dir, drel)[0]
                    out += ED.quote_findings(spec_q, self.run_dir, db)
            except Exception:  # noqa: BLE001 -- invalid dossier is reported by preconditions/evidence checks
                pass
        return out

    def _acquired_tamper_problem(self) -> str | None:
        """Verify acquired inventory integrity before any task reads or on resume (§§2.4, 4, T_ACQ_TAMPER_PRESPEC)."""
        from agents import acquire as ACQ
        rec_sha = (self.st.acquired or {}).get("inventory_sha256") if isinstance(self.st.acquired, dict) else None
        if not rec_sha and not ACQ.has_acquired(self.run_dir):
            return None
        try:
            v = ACQ.verify_inventory(self.run_dir)
        except ACQ.ArtifactTampered as e:
            return str(e)
        if rec_sha and v.get("inventory_sha256") != rec_sha:
            return f"acquired_inventory_sha256 mismatch ({v.get('inventory_sha256')} != {rec_sha})"
        return None

    def _acquire_closed(self) -> bool:
        """True once acquisition is closed (SIGN exited to ENGINE_CHOICE in any mode, or restored past SIGN)."""
        if isinstance(self.st.acquired, dict) and self.st.acquired.get("closed"):
            return True
        if self._signed_before():
            return True
        return any(h.get("from") == "SIGN" and h.get("to") == "ENGINE_CHOICE" for h in self.st.history)

    def _close_acquire(self) -> None:
        """Close acquisition permanently before the first exit from SIGN to ENGINE_CHOICE (§2.2, T_ACQ_CLOSED)."""
        if not isinstance(self.st.acquired, dict):
            self.st.acquired = {}
        if not self.st.acquired.get("closed"):
            self.st.acquired["closed"] = True
            save_state(self.run_dir, self.st)

    @staticmethod
    def _missing_inputs_from(probs: list[str]) -> list[str]:
        """Extract `missing_input: <wanted_kind>:<detail>` tokens from SPEC or EVIDENCE problems (§2.3)."""
        found: list[str] = []
        for p in probs or ():
            s = str(p or "")
            m = re.search(r"missing_input:\s*([A-Za-z0-9_.:/\-]+)", s)
            if m:
                tok = m.group(1).strip()
                if tok and tok not in found:
                    found.append(tok)
        return found

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
        """The signed spec's sha256 from the backend's sign-off."""
        return self.backend.signed_sha(run_dir)

    def _sign(self, by: str, note: str = "") -> None:
        self.backend.sign(self.run_dir, by, note)

    def _signed_by(self, rd: str | None = None):
        """Who signed the current spec (of `rd`, the run dir by default), or None when it is unsigned or changed since."""
        rd = rd or self.run_dir
        if self.backend.signoff_status(rd) != "signed":
            return None
        try:
            from toolkit.spec import signoff
            rec = signoff._spec_record(rd) or {}
        except Exception:  # noqa: BLE001
            rec = {}
        return rec.get("signed_by") or rec.get("by") or "owner"

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
                            rem = FL.remedy(b)
                            out.append(f"{b.get('id')}: {b.get('claim')}" + (f" Required: {rem}" if rem else ""))
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
        # §5.1: verify signed built-engine snapshot at every state entry after ENGINE_SIGN
        if s in POST_ENGINE_SIGN_STATES:
            tamper = self._engine_tamper_problems()
            if tamper:
                _withhold_report(self.run_dir)
                return self._go("FINAL", "agent-built engine snapshot tampered: " + "; ".join(tamper[:4]),
                                "artifact_tampered")
        # acquire.md §§2.4, 4: verify acquired inventory before any task reads in SPEC/EVIDENCE/SIGN/post-choice
        if s in ("SPEC", "EVIDENCE", "SIGN") or s in POST_CHOICE_STATES:
            acq_tamper = self._acquired_tamper_problem()
            if acq_tamper:
                _withhold_report(self.run_dir)
                return self._go("FINAL", f"acquired inventory tampered: {acq_tamper}", "artifact_tampered")
        if s in LLM_STATES and s != "WRITE" and self._out_of_budget():
            return self._budget_out(s)
        try:
            if s in ("ENGINE_BUILD", "ENGINE_ADMIT", "ENGINE_REVIEW", "ENGINE_SIGN") or (
                s == "ENGINE_CHOICE" and spec_has_engine_build(self.run_dir)
            ):
                with _run_lock(self.run_dir):
                    return getattr(self, "h_" + s)()
            return getattr(self, "h_" + s)()
        except AwaitingBudget:            # _task recorded the pause; the journal makes the handler safe to re-run
            return s

    def run(self, max_steps: int = 200) -> str:
        acq_tamper = self._acquired_tamper_problem()
        if acq_tamper:
            _withhold_report(self.run_dir)
            why = f"acquired inventory tampered on resume: {acq_tamper}"
            if self.st.state == "FINAL":
                self.st.status = "artifact_tampered"
                self.st.history.append({"at": _now(), "from": "FINAL", "to": "FINAL", "why": why,
                                        "round": self.st.round, "spent_usd": round(self.st.spent_usd, 4)})
                self._finish()
            else:
                self._go("FINAL", why, "artifact_tampered")
            save_state(self.run_dir, self.st)
            return self.st.status or self.st.state
        if self.st.state in POST_ENGINE_SIGN_STATES or (
            self.st.state == "FINAL" and (self.st.engine_build or {}).get("status") == "signed"
        ):
            tamper = self._engine_tamper_problems()
            if tamper:
                _withhold_report(self.run_dir)
                why = "agent-built engine snapshot tampered on resume: " + "; ".join(tamper[:4])
                if self.st.state == "FINAL":
                    self.st.status = "artifact_tampered"
                    self.st.history.append({"at": _now(), "from": "FINAL", "to": "FINAL", "why": why,
                                            "round": self.st.round, "spent_usd": round(self.st.spent_usd, 4)})
                    self._finish()
                else:
                    self._go("FINAL", why, "artifact_tampered")
                save_state(self.run_dir, self.st)
                return self.st.status or self.st.state
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
        if self.st.paused and self.st.state != "FINAL":
            # baseline hb3 §4.2: only FINAL wrote a status, so a paused run (flu R1) exited 2 with none in metadata.json
            try:
                self._write_meta(drop=("finished",), status=f"paused:{self.st.paused}", paused_state=self.st.state,
                                 paused_why=self._pause_why())
            except OSError as e:      # the pause stands (run_state.json holds it); said loudly, never a crash
                self.log(f"[machine] ERROR the pause was not recorded in metadata.json: {type(e).__name__}: {e}")
        return self.st.status or (f"paused:{self.st.paused}" if self.st.paused else self.st.state)

    def _pause_why(self) -> str:
        """Why the run is paused, for metadata.json: the reason it entered its state, the allowance a budget pause
        stopped at, and the lead's refusal of an amendment."""
        h = self.st.history
        why = next((x.get("why") for x in reversed(h) if x.get("to") == self.st.state and x.get("why")), "")
        if self.st.paused == AWAITING_BUDGET:
            b = next((x for x in reversed(h) if x.get("paused") == AWAITING_BUDGET), {})
            if b.get("reason") == "engine_build_cap":
                why = (f"engine_build_cap: build balance ${b.get('allowance_usd')} is below floor "
                       f"${b.get('floor_usd')} (cap ${b.get('cap_usd')}; resume with --engine-build-usd)")
            elif b.get("reason") == "engine_admit_compute":
                why = (f"engine_admit_compute: remaining budget ${b.get('left_usd')} is below admission compute "
                       f"reservation ${b.get('need_usd')} (resume with higher --max-budget-usd)")
            elif b.get("reason") == "acquire_cap":
                why = (f"acquire_cap: acquisition balance ${b.get('allowance_usd')} is below floor "
                       f"${b.get('floor_usd')} (cap ${b.get('cap_usd')})")
            else:
                why = (f"task {b.get('task')}: allowance ${b.get('allowance_usd')} is below the ${b.get('floor_usd')} "
                       f"floor (cap ${b.get('cap_usd')})")
        if self.st.paused == AWAITING_INPUTS:
            why = ("acquisition promoted no inputs: inspect acquire/inputs_request.md and relaunch with a question "
                   "that names the required input files")
        if self.st.paused == AWAITING_ENGINE_SIGNOFF:
            why = ("admitted agent-built engine awaits sign-off: inspect engine_build/admission_sheet.md and run "
                   f"`python3 toolkit/engines/built/signoff.py sign {self.run_dir} --by owner` (or `decline --note ...`)")
        if self.st.paused == "awaiting_scope_signoff":
            ent_idx = max((idx for idx, x in enumerate(h) if x.get("to") == "OWNER_DECISION"), default=-1)
            ref_idx = max((idx for idx, x in enumerate(h)
                           if x.get("state") == "OWNER_DECISION" and x.get("lead_refused_provisional")), default=-1)
            if ref_idx >= ent_idx and ref_idx >= 0 and h[ref_idx].get("why"):
                why = f"{why}; {h[ref_idx]['why']}".lstrip("; ")
        return str(why or f"paused at {self.st.state}")[:1000]

    def _write_meta(self, drop=(), **fields) -> None:
        """metadata.json with `fields` and the run's pipeline, engine, rounds and spend set, and `drop` removed. Read
        with no link followed, as it is written (Codex r1 on #374: a planted link's target was copied into it)."""
        from lib.safe_io import read_nofollow
        try:
            meta = json.loads(read_nofollow(self.run_dir, "metadata.json")[0])
        except (OSError, ValueError):
            meta = {}
        meta = meta if isinstance(meta, dict) else {}
        for k in drop:
            meta.pop(k, None)
        meta.update(pipeline="v2", engine=self.st.engine, rounds=self.st.round, max_rounds=self.st.max_rounds,
                    model_spend_usd=round(self.st.spent_usd, 4), **fields)
        acq = self.st.acquired if isinstance(self.st.acquired, dict) else {}
        if acq.get("inventory_sha256"):
            meta["acquired_inventory_sha256"] = acq["inventory_sha256"]
        if acq.get("allowlist_version") is not None:
            meta["allowlist_version"] = acq["allowlist_version"]
        if acq.get("allowlist_sha256"):
            meta["allowlist_sha256"] = acq["allowlist_sha256"]
        _write_json(self.run_dir, "metadata.json", meta)

    # -------------------------------------------------- handlers
    def _write_inputs_request(self, wanted: list[str], note: str = "") -> None:
        """Write `acquire/inputs_request.md` when acquisition promotes nothing (§2.2, T_ACQ_EMPTY)."""
        from agents import acquire as ACQ
        lines = [
            "# Public data acquisition promoted no inputs",
            "",
            "No usable public input files were promoted under the current allowlist.",
            "Recovery in v1: relaunch a new run whose question names the required input files.",
            "",
            "## Wanted kinds",
            "",
        ]
        lines += [f"- `{w}`" for w in (wanted or ["(unspecified)"])]
        if note:
            lines += ["", f"Note: {note}"]
        try:
            from lib.safe_io import read_nofollow
            raw_r, _ = read_nofollow(self.run_dir, ACQ.RECORD_REL)
            rec = json.loads(raw_r.decode("utf-8"))
        except Exception:  # noqa: BLE001
            rec = {}
        fetches = rec.get("fetches") if isinstance(rec, dict) and isinstance(rec.get("fetches"), dict) else {}
        rejected = rec.get("rejected") if isinstance(rec, dict) and isinstance(rec.get("rejected"), dict) else {}
        lines += ["", "## Queries tried", ""]
        if not fetches:
            lines.append("- (none)")
        for fid, r in sorted(fetches.items()):
            lines.append(f"- `{r.get('source_id')}.{r.get('endpoint')}` params=`{json.dumps(r.get('params') or {}, sort_keys=True)}` status=`{r.get('status')}`")
        lines += ["", "## Rejected fetches", ""]
        if not rejected:
            lines.append("- (none)")
        for fid, r in sorted(rejected.items()):
            lines.append(f"- `{fid[:12]}` `{r.get('code')}`: {r.get('reason')}")
        _put(self.run_dir, "acquire/inputs_request.md", "\n".join(lines) + "\n")

    def h_ACQUIRE(self):
        """Thin handler for ACQUIRE state (docs/design/acquire.md §2.2)."""
        from agents import acquire as ACQ
        if not isinstance(self.st.acquired, dict):
            self.st.acquired = {}
        # 1. Closed guard
        if self._acquire_closed():
            return self._go("FINAL", "acquisition is closed", "run_failed")
        rounds_done = int(self.st.acquired.get("rounds") or 0)
        # 2. Off-switch: if the question supplies repository files, turn ACQUIRE off at $0 cost
        if rounds_done == 0:
            staged = stage_inputs(self.run_dir, self.st.question)
            if staged:
                self.st.acquired.update(status="skipped_question_supplies_inputs", closed=True, rounds=0)
                self.st.acquire = "off"
                return self._go("SPEC", "question supplies inputs: acquisition skipped")
        else:
            tamp = self._acquired_tamper_problem()
            if tamp:
                _withhold_report(self.run_dir)
                return self._go("FINAL", f"acquired inventory tampered: {tamp}", "artifact_tampered")
        # 3. Plan & fetch
        al_path = getattr(self, "acquire_allowlist", None) or ACQ.DEFAULT_ALLOWLIST
        try:
            allowlist, al_sha = ACQ.load_allowlist(al_path)
        except ACQ.AllowlistError as e:
            return self._go("FINAL", f"acquire allowlist invalid: {e}", "run_failed")
        round_n = rounds_done + 1
        wanted = list(self.st.tries.get("acquire_wanted") or allowlist["wanted_kinds"])
        sources_summary = [
            {
                "id": s["id"],
                "holds": s["holds"],
                "endpoints": {
                    en: {"wanted_kinds": list(ep["wanted_kinds"]), "params": dict(ep["params"])}
                    for en, ep in s["endpoints"].items()
                },
            }
            for s in allowlist["sources"] if s["status"] == "enabled" and not s["requires_key"]
        ]
        packet = {
            "question": self.st.question,
            "round": round_n,
            "wanted_kinds": list(allowlist["wanted_kinds"]),
            "wanted": wanted,
            "sources": sources_summary,
        }
        att = int(self.st.tries.get("acquire_attempt") or 0)
        res = self._task(f"ACQUIRE#r{round_n}:a{att + 1}", "acquire", self.st.question, self.run_dir, packet,
                         acquire_budget=True)
        if res is None:
            return self._budget_out("ACQUIRE")
        if not res.ok:
            if att < MAX_ACQUIRE_RETRIES:
                self.st.tries["acquire_attempt"] = att + 1
                return self._go("ACQUIRE", f"acquire task failed (attempt {att + 1}): {res.note[:160]}")
            self.st.tries.pop("acquire_attempt", None)
            self._write_inputs_request(wanted, note=f"acquire task failed: {res.note}")
            self.st.acquired.update(status=AWAITING_INPUTS, rounds=round_n,
                                    allowlist_version=int(allowlist["version"]), allowlist_sha256=al_sha)
            self.st.paused, self.st.status = AWAITING_INPUTS, None
            save_state(self.run_dir, self.st)
            self.log(f"[machine] ACQUIRE paused ({AWAITING_INPUTS}): inspect acquire/inputs_request.md")
            return "ACQUIRE"
        # 4. Promote
        before_prov = ACQ._load_provenance(self.run_dir)
        before_paths = {p for p, e in (before_prov.get("inputs") or {}).items()
                        if isinstance(e, dict) and e.get("origin") == "acquired"}
        out_d = res.output if isinstance(res.output, dict) else {}
        keep = list(out_d.get("keep") or [])
        wanted_map = dict(out_d.get("wanted_map") or {}) if isinstance(out_d.get("wanted_map"), dict) else {}
        try:
            promo = ACQ.promote(self.run_dir, keep, wanted_map, allowlist_path=al_path)
        except ACQ.ArtifactTampered as e:
            _withhold_report(self.run_dir)
            return self._go("FINAL", f"acquired promotion tampered: {e}", "artifact_tampered")
        after_prov = ACQ._load_provenance(self.run_dir)
        after_acq = {p: e for p, e in (after_prov.get("inputs") or {}).items()
                     if isinstance(e, dict) and e.get("origin") == "acquired"}
        newly_promoted = sorted(set(after_acq) - before_paths)
        rec = ACQ._load_record(self.run_dir, int(allowlist["version"]), al_sha)
        self.st.acquired.update(
            rounds=round_n,
            fetches=len(rec.get("fetches") or {}),
            bytes=sum(int(e.get("bytes") or 0) for e in after_acq.values()),
            inventory_sha256=promo.get("inventory_sha256"),
            allowlist_version=int(allowlist["version"]),
            allowlist_sha256=al_sha,
        )
        self.st.tries.pop("acquire_attempt", None)
        # 5. Exit
        if not newly_promoted:
            self._write_inputs_request(wanted, note="no new files promoted")
            self.st.acquired["status"] = AWAITING_INPUTS
            self.st.paused, self.st.status = AWAITING_INPUTS, None
            save_state(self.run_dir, self.st)
            self.log(f"[machine] ACQUIRE promoted nothing: paused ({AWAITING_INPUTS}); see acquire/inputs_request.md")
            return "ACQUIRE"
        self.st.acquired["status"] = "promoted"
        self.st.tries.pop("acquire_wanted", None)
        src_stage = self.st.tries.pop("acquire_from", None)
        if round_n > 1:
            self.st.tries["spec_bonus"] = 1
            if src_stage == "EVIDENCE":
                self.st.tries["evidence_bonus"] = 1
        self._write_meta()
        return self._go("SPEC", f"acquired {len(newly_promoted)} file(s) (round {round_n})")
    def h_SPEC(self):
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
            self.st.tries["spec_amendment"] = True     # honoured on resume, whatever a restored copy lost (#261 P1-2)
            self.st.paused, self.st.status = AWAITING_SPEC_AMENDMENT, None
            save_state(self.run_dir, self.st)
            self.log(f"[machine] SPEC rethink of a signed spec: nothing redrafted. Read {SPEC_RETHINK_REQUEST} "
                     f"({len(req['findings'])} finding(s)); amend the spec and relaunch with reuse")
            return "SPEC"
        n = self.st.tries.get("spec", 0)
        max_spec = MAX_SPEC_TRIES + int(self.st.tries.get("spec_bonus") or 0)
        if n >= max_spec:
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
            missing = self._missing_inputs_from(errs)
            if (missing and self.st.acquire == "auto" and not self._acquire_closed()
                    and int((self.st.acquired or {}).get("rounds") or 0) < MAX_ACQUIRE_ROUNDS):
                self.st.tries["acquire_wanted"] = missing
                self.st.tries["acquire_from"] = "SPEC"
                self.st.tries["spec_feedback"] = _carry_feedback(errs, fb if not drafted else None)
                return self._go("ACQUIRE", f"missing input(s) at SPEC: {', '.join(missing[:4])}",
                                extra={"missing_inputs": missing})
            self.st.tries["spec_feedback"] = _carry_feedback(errs, fb if not drafted else None)
            if drafted or recheckable:
                # these checks' own output (or, with no draft on disk, what they wrote before plus that note): a capped
                # next try may be re-checked against it (Codex r4 P1-3: a first try with no draft blocked every later one)
                self.st.tries["spec_feedback_checked"] = list(self.st.tries["spec_feedback"])
            else:
                self.st.tries.pop("spec_feedback_checked", None)
            # baseline hb2 §4.6: the whole feedback of each failed try is kept in its history entry
            return self._go("SPEC", f"spec invalid: {'; '.join(errs)[:200]}",
                            extra={"feedback": list(self.st.tries["spec_feedback"])})
        self.st.tries.pop("spec_feedback", None)
        self.st.tries.pop("spec_feedback_checked", None)
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
            spec_doc = yaml.safe_load(open(os.path.join(rd, AB.SPEC_FILE)))
            from toolkit.analysis import conflicts as CF     # owner default D5: declare, test and continue
            if CF.ensure_default(rd):                        # after the link checks: it replaces the file
                self.log("[machine] the draft states no conflicts policy: declare_and_test (owner default D5)")
        except Exception as e:  # noqa: BLE001
            return [f"spec unreadable: {type(e).__name__}: {e}"]
        from toolkit.spec import schema as _schema
        if self.st.signoff == "none" and isinstance(spec_doc, dict) and _schema.engine_has_build(spec_doc.get("engine")):
            return ["an engine build needs a signed request (--signoff none is refused at SPEC)"]
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
        run("cited-input", lambda: record_cited_inputs(rd, bool(self.st.derived_inputs)))      # records the hashes before a check reads a cited file
        outside = self._cites_outside(rd)
        if outside:             # Codex r1 P1-1: no later check opens a file the cited-input check refused
            out.extend(x for x in outside if x not in out)       # r2 P1-2: never empty, so never a pass
            return out
        if self.st.derived_inputs:
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
            from toolkit.spec import schema as _schema
            try:
                _sdoc = self._spec(self.run_dir)
            except Exception:  # noqa: BLE001
                _sdoc = {}
            if isinstance(_sdoc, dict) and _schema.engine_has_build(_sdoc.get("engine")):
                return self._go("FINAL", "an engine build needs a signed request (--signoff none)", "run_failed")
            self._close_acquire()
            return self._go("ENGINE_CHOICE", "sign-off not required (--signoff none)")
        by = self._signed_by()
        # A HIGH geometry finding the spec proposes as an exception is the owner's to approve (review of #131, P1):
        # a lead sign-off approves nothing, so the run waits for the owner here, as for a spec sign-off.
        pend = self._geometry_owner_pending()
        if by and (mode == "lead" or by == "owner") and not pend:
            self._close_acquire()
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
                if os.path.exists(os.path.join(self.run_dir, SIGNOFF_FILE)):
                    from lib.safe_io import remove_nofollow
                    remove_nofollow(self.run_dir, SIGNOFF_FILE)   # the lead's note; B writes its own record
                try:
                    self._sign("lead", (note + " " + self._evidence_note()).strip())   # the harness records the hash
                    self._close_acquire()
                    return self._go("ENGINE_CHOICE", "spec signed by the lead")
                except ValueError as e:
                    self.log(f"[machine] the spec could not be signed: {e}")
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
            max_ev = EDR.MAX_TRIES + (1 if self.st.tries.get("evidence_bonus") else 0)
            while a["problems"] and a.get("draftable") and drafts() < max_ev:
                n = drafts()
                res = self._task(f"EVIDENCE#{self._visit()}:try{n + 1}", "draft_evidence", rd, a["rel"],
                                 EDR.packet(a), fb + a["problems"])
                self.st.tries["evidence"] = drafts()
                if res is None:
                    return self._budget_out("EVIDENCE")
                notes.append(f"try {n + 1}: " + (res.note if res.ok else f"failed: {res.note}")[:200])
                fb = []
                # Pre-SIGN factual discrepancy found in staged input files while drafting the dossier -> bounce to SPEC
                disc = (res.output or {}).get("spec_discrepancies") if (res.ok and isinstance(res.output, dict)) else None
                bounces = int(self.st.tries.get("evidence_spec_bounces") or 0)
                if isinstance(disc, list) and disc and bounces < 2:
                    disc_probs: list[str] = []
                    for item in disc[:10]:
                        if isinstance(item, dict):
                            disc_probs.append(
                                f"evidence discrepancy in {item.get('field', 'decision_spec.yaml')}: "
                                f"spec={item.get('spec_value')!r} vs file={item.get('file_value')!r} "
                                f"(source: {item.get('source', 'staged input')})"
                            )
                        elif item:
                            disc_probs.append(f"evidence discrepancy: {item}")
                    if disc_probs:
                        self.st.tries["evidence_spec_bounces"] = bounces + 1
                        self.st.tries["spec_bonus"] = int(self.st.tries.get("spec_bonus") or 0) + 1
                        self.st.tries.pop("evidence_feedback", None)
                        return spec_back(disc_probs)
                try:
                    EDR.scaffold(rd, a, raw)            # an entry the task dropped is scaffolded again
                    a = EDR.assess(rd, raw)
                except Exception as e:  # noqa: BLE001 -- the task left a dossier the harness cannot read
                    a = dict(a, problems=[f"the dossier cannot be read: {type(e).__name__}: {e}"])
            missing_ev = self._missing_inputs_from(a.get("problems") or [])
            if (missing_ev and self.st.acquire == "auto" and not self._acquire_closed()
                    and int((self.st.acquired or {}).get("rounds") or 0) < MAX_ACQUIRE_ROUNDS):
                self.st.tries["acquire_wanted"] = missing_ev
                self.st.tries["acquire_from"] = "EVIDENCE"
                return self._go("ACQUIRE", f"missing input(s) at EVIDENCE: {', '.join(missing_ev[:4])}",
                                extra={"missing_inputs": missing_ev})
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
        from toolkit.spec import schema as _SCH
        raw_eng = None
        try:
            raw_eng = (self._spec() or {}).get("engine")
        except Exception:  # noqa: BLE001
            raw_eng = spec.get("engine") if isinstance(spec, dict) else None
        has_build = _SCH.engine_has_build(raw_eng)
        has_auto = _SCH.engine_is_auto(raw_eng)
        rows = []
        eligible = []
        if not has_build or has_auto:
            rows = self.backend.eligible_engines(spec)
            eligible = [r for r in rows if r.get("eligible")]
        rec = {"at": _now(), "candidates": rows}
        if has_build and (not has_auto or not eligible):
            if self.st.signoff == "none":
                return self._go("FINAL", "an engine build needs a signed request (--signoff none refused)", "run_failed")
            prev_eb = self.st.engine_build if isinstance(self.st.engine_build, dict) else {}
            if (prev_eb.get("status") == "signed" or int(prev_eb.get("tries") or 0) > 0) and not self._engine_repairs_left():
                self.st.history.append({
                    "at": _now(), "state": "ENGINE_CHOICE", "round": self.st.round,
                    "engine_repair_refused": True,
                    "why": f"re-entering built engine via ENGINE_CHOICE refused (cap {MAX_ENGINE_REPAIRS})",
                })
                return self._go("FINAL", f"second engine repair refused (cap {MAX_ENGINE_REPAIRS})", "run_failed")
            from lib.safe_io import remove_nofollow
            from toolkit.engines.built import requested as BREQ
            build_decl = dict(raw_eng["build"])
            bid = str(build_decl["id"])
            req_rec = {
                "build": build_decl,
                "request_sha256": BREQ.request_sha256(build_decl),
                "signed_spec_sha256": _spec_signed_sha(self.run_dir),
                "recorded_at": _now(),
            }
            _write_json(self.run_dir, "engine_build/request.json", req_rec)
            rec["build"] = {"id": bid, "status": "requested"}
            _write_json(self.run_dir, AB.ENGINE_CHOICE, rec)
            miss = evidence_gate(self.run_dir, signoff=self.st.signoff)
            if miss:
                _write_json(self.run_dir, AB.ENGINE_CHOICE, dict(rec, evidence_refused={"build": bid, "why": miss}))
                return self._go("FINAL", f"the signed evidence dossier does not cover built engine {bid}'s decision "
                                         "inputs: " + "; ".join(miss[:5]), "run_failed")
            # Clear stale sign-off/review/seed state when entering or re-entering build via ENGINE_CHOICE
            for stale_rel in ("engine_build/signoff.json", "engine_build/decline.json",
                              "engine_build/review_status.json", "engine_build/.hidden/seed.json",
                              "critique_engine.yaml"):
                remove_nofollow(self.run_dir, stale_rel, missing_ok=True)
            prev_eb = self.st.engine_build if isinstance(self.st.engine_build, dict) else {}
            self.st.engine_build = {
                "id": bid,
                "kind": str(build_decl.get("kind") or "starsim_module"),
                "status": "requested",
                "tries": int(prev_eb.get("tries") or 0),
                "repairs": int(prev_eb.get("repairs") or 0),
                "spent_usd": float(prev_eb.get("spent_usd") or 0.0),
                "compute_reserved_usd": 0.0,
                "compute_reservation_id": None,
                "snapshot_sha256": None,
                "admission_sha256": None,
                "hidden_commitment": None,
                "hidden_seed_snapshot": None,
            }
            self.st.engine = None
            return self._go("ENGINE_BUILD", f"requested agent-built engine {bid}")
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
        return self._go("ANALYZE", f"engine {pick}")

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

    def h_ANALYZE(self):
        import pipeline as P
        errs = self.backend.validate(os.path.join(self.run_dir, AB.SPEC_FILE))
        if errs:
            return self._go("FINAL", f"spec invalid for the engine: {'; '.join(errs)[:200]}", "run_failed")
        err = self._write_pipeline()
        if err:
            return self._go("FINAL", f"analyze pipeline failed: {err}"[:300], "run_failed")
        steps, _errs = P.load(self.run_dir)
        if any(s_.get("id") == "configure" for s_ in steps):
            rep_c = P.run(self.run_dir, only="configure")
            if not rep_c.get("ok"):
                return self._go("FINAL", f"analyze pipeline failed: {_rep_error(rep_c)}", "run_failed")
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
        return self._go("CRITIQUE", "analysis built")

    def h_CRITIQUE(self):
        if self.st.round_pending:
            self.st.round += 1
            self.st.round_pending = False
        if self.st.round > self.st.max_rounds:
            return self._go("FINAL", "round cap passed (gate did not stop the run)", "completed_gate_error")
        v = self._visit()
        fb = self.st.tries.get("schema_feedback") or {}
        if not fb:
            critics = list(CRITICS)
        elif "*" in fb:
            critics = list(CRITICS) + (["critique-presentation"] if self.st.post_write else [])
        else:
            critics = [r for r in fb if r in CRITICS or r == "critique-presentation"]
        for reviewer in critics:
            args = (self.run_dir, reviewer, self.st.round) + ((fb.get(reviewer) or fb.get("*"),) if fb else ())
            res = self._task(f"CRITIQUE#{v}:r{self.st.round}:{reviewer}", "critique", *args,
                             writeup=(reviewer == "critique-presentation"))
            if res is None:
                return self._budget_out("CRITIQUE")
        self.st.tries.pop("schema_feedback", None)
        return self._go("GATE", f"critique round {self.st.round}")

    def h_GATE(self):
        pw = self.st.post_write
        waiting: list = []        # findings awaiting the owner's answer: no final disposition is published before it
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
            led0 = FL.sync(self.run_dir, d, self.st.round, post_write=pw, lead_ok=self.st.signoff == "lead")[0]
            held_now = sorted(owner_accepted_now(self.run_dir, opened, self.st.signoff == "lead"))
            d = apply_medium_rule(d, opened, int(d.get("rounds_remaining") or 0), not self._out_of_budget(),
                                  owner_held=held_now)
            d = apply_held_rule(d, led0, held_now, opened)
            d["owner_held"] = held_now      # what this gate read as the owner's (FIX_LOOP's shortcut reads it here)
            d["open_tested_ids"] = sorted(str(o["id"]) for o in opened if o.get("tested"))   # the shortcut reads it
            d = require_judged(d, opened)
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
            if fst in ACCEPTED_STATUSES and _render_stale(self.run_dir):
                # an accepted test's record changed between this gate's read and the report's (Codex r3 of #357,
                # P1-7): the report cannot state what was accepted, so the accept is not delivered
                _withhold_report(self.run_dir)
                return self._go("FINAL", "an accepted decision test's record changed after the gate read it: "
                                         "the report could not state the accepted dependence", "completed_gate_error")
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
            if d.get("rule_matched") == "4m" or (str(d.get("rule_matched")) == "2" and d.get("open_tested")):
                # these are why the loop runs: plan them
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
        nxt, status, mode = route_gate(
            d, pw, built_engine=self._has_built_engine(), engine_repairs_left=self._engine_repairs_left()
        )
        # Refuse a second post-analysis engine repair (§6.1, BE30): never loop ENGINE_CHOICE back into ENGINE_BUILD
        if (self._has_built_engine() and not self._engine_repairs_left()
                and any(isinstance(u, dict) and u.get("target_stage") == "ENGINE"
                        for u in (d or {}).get("unresolved_high") or [])):
            self.st.history.append({
                "at": _now(), "state": "GATE", "round": self.st.round,
                "engine_repair_refused": True,
                "why": f"second engine repair refused (cap {MAX_ENGINE_REPAIRS}); finding queues for next run",
            })
            if nxt in ("ENGINE_BUILD", "ENGINE_CHOICE"):
                nxt, status, mode = ("FINAL", "run_failed", None) if pw else ("WRITE", None, "not_accepted")
        if nxt == "OWNER_DECISION":
            self._request_scope(d)
        if nxt == "CRITIQUE":                               # schema errors: re-run the critics
            n = self.st.tries.get("schema", 0) + 1
            self.st.tries["schema"] = n
            if n > MAX_SCHEMA_REPAIRS:
                return self._go("FINAL", "critique schema errors persist", "completed_gate_error")
            # Only the critics whose YAML failed are re-run, each told its own errors. Prefer the validator's exact
            # per-reviewer attribution (`schema_errors_by_reviewer`) so a value quoting another filename inside an
            # error body can never misroute; fall back to the leading filename / "invalid YAML in <path>" prefix only.
            import validate_critique_yaml as V
            by_file = {f: r for r, f in V.FILENAMES.items()}
            by_rev = d.get("schema_errors_by_reviewer")
            if isinstance(by_rev, dict) and any(by_rev.values()):
                fb = {str(r): [str(x) for x in errs] for r, errs in by_rev.items() if errs}
            else:
                fb = {}
                for e in d["schema_errors"]:
                    s_err = str(e)
                    head = s_err.split(":", 1)[0].strip()
                    cand = os.path.basename(head[len("invalid YAML in "):].strip()) if head.startswith("invalid YAML in ") else head.split()[0]
                    fb.setdefault(by_file.get(cand, "*"), []).append(s_err)
            self.st.tries["schema_feedback"] = fb
            return self._go("CRITIQUE", f"schema errors: {'; '.join(d['schema_errors'])[:200]}")
        self.st.tries.pop("schema", None)
        why = f"{(d or {}).get('action')} (rule {(d or {}).get('rule_matched')}){' post-write' if pw else ''}"
        if nxt == "ENGINE_BUILD":
            eng_fs = [
                f"{u.get('id') or u.get('kind')}: {u.get('claim')}"
                for u in (d or {}).get("unresolved_high") or []
                if isinstance(u, dict) and u.get("target_stage") == "ENGINE"
            ]
            return self._begin_engine_repair(why, eng_fs)
        if nxt in ("FIX_LOOP", "SPEC", "ENGINE_CHOICE"):
            self.st.round_pending = True
            self.st.post_write = False
        if nxt == "WRITE":
            self.st.write_mode = mode
        if nxt in ("SPEC", "ENGINE_CHOICE"):
            return self._rethink(nxt, why)
        return self._go(nxt, why, status)

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
                           settings=getattr(self.backend, "settings_paths", AB.SETTINGS_PATHS),
                           built_engine=self._has_built_engine())
        self.st.history.append({"at": _now(), "fix_loop": summ})
        if summ.get("owner_amendments"):
            # F6 run 3: their remedy is an owner amendment of the signed spec; the owner answers, no redraft runs.
            self._request_owner(FL.load(self.run_dir), summ["owner_amendments"])
            return self._go("OWNER_DECISION", f"{len(summ['owner_amendments'])} finding(s) need an owner amendment "
                                              f"({', '.join(summ['owner_amendments'])[:120]})")
        if summ.get("rethink"):
            if summ["rethink"] == "ENGINE_CHOICE" and self._has_built_engine():
                from toolkit.spec import schema as _SCH
                try:
                    eng_decl = (self._spec() or {}).get("engine")
                except Exception:  # noqa: BLE001
                    eng_decl = None
                if not _SCH.engine_is_auto(eng_decl):
                    if self._engine_repairs_left():
                        return self._begin_engine_repair("the fix planner routed to ENGINE_CHOICE on a built-only run",
                                                         self._rethink_reasons("ENGINE_CHOICE", planned=True))
                    self.st.history.append({
                        "at": _now(), "state": "FIX_LOOP", "round": self.st.round,
                        "engine_repair_refused": True,
                        "why": f"second engine repair refused (cap {MAX_ENGINE_REPAIRS}); finding queues for next run",
                    })
                    return self._go("FINAL", f"second engine repair refused (cap {MAX_ENGINE_REPAIRS})", "run_failed")
            if summ["rethink"] == "ENGINE_BUILD":
                if not self._has_built_engine():
                    return self._go("FINAL", "ENGINE_BUILD rethink is invalid for a registered engine", "run_failed")
                if self._engine_repairs_left():
                    return self._begin_engine_repair("the fix planner routed to ENGINE_BUILD",
                                                     self._rethink_reasons("ENGINE_BUILD", planned=True))
                self.st.history.append({
                    "at": _now(), "state": "FIX_LOOP", "round": self.st.round,
                    "engine_repair_refused": True,
                    "why": f"second engine repair refused (cap {MAX_ENGINE_REPAIRS}); finding queues for next run",
                })
                return self._go("FINAL", f"second engine repair refused (cap {MAX_ENGINE_REPAIRS})", "run_failed")
            return self._rethink(summ["rethink"], f"the planner chose a rethink ({summ['rethink']})", planned=True)
        if not summ["tasks"] and summ.get("write_up") and not self._fixable_open() and self._has_draft():
            if self.st.round_pending:
                self.st.round += 1
                self.st.round_pending = False
            self.st.write_mode = self.st.write_mode or "not_accepted"
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
        """Pre-write shortcut to WRITE when FIX_LOOP has no tasks and every open finding is write-up or owner-held."""
        try:
            d = json.load(open(os.path.join(self.run_dir, f"gate_r{self.st.round}.json")))
        except (OSError, ValueError):
            return None
        if not isinstance(d, dict) or d.get("action") != "PATCH_OR_RETHINK" or str(d.get("rule_matched")) not in ("4m", "2") \
                or d.get("accept_blockers") or d.get("structural_mismatch"):
            return None
        held = d.get("owner_held")
        if not isinstance(held, list):
            return None
        led = FL.load(self.run_dir)
        keep = set(map(str, held))
        hi = d.get("unresolved_high") or []
        if str(d.get("rule_matched")) == "2" and not hi:
            return None
        for b in hi:
            i = held_entry_id(b, d) if isinstance(b, dict) else None
            if i is None:
                return None
            crit = str(b.get("reviewer") or "").startswith("critique")
            if not (i in led and ((crit and FX.is_write_up(led[i])) or i in keep)):
                return None
        ids = [str(i) for i in d.get("open_tested") or []]
        if not all(i in led and FX.is_write_up(led[i]) for i in ids):
            return None
        tested = d.get("open_tested_ids")
        if not isinstance(tested, list) or any(str(i) not in keep and not (str(i) in led and FX.is_write_up(led[str(i)]))
                                               for i in tested):
            return None
        return sorted(keep)

    def _fixable_open(self) -> list[str]:
        """Open findings a fix task could still work to: blocking, neither write-up nor owner-held."""
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
        SS.record(self.run_dir, scope_request_view(d), self.st.round,
                  producer="agents/stage_machine.py GATE (decision-bearing findings)")
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
            led = FL.load(self.run_dir)
            allow = [i for i in todo if _lead_may_accept(i, led)]
            blocked = [i for i in todo if not _lead_may_accept(i, led)]
            if allow:
                allow_ss = [
                    dict(i, severity=led[i["finding"]].severity)
                    if i.get("severity") is None and i.get("finding") in led
                    and getattr(led[i["finding"]], "severity", None) is not None else i
                    for i in allow]
                SS.provisional_answers(self.run_dir, allow_ss, self.st.round,
                                       plan_changing={i["fingerprint"]: _plan_changing(i) for i in allow_ss})
                self.st.history.append({"at": _now(), "provisional": [i.get("finding") for i in allow], "by": "lead",
                                        "state": "OWNER_DECISION"})
                answers, bad = SS.load_answers(self.run_dir)
            if blocked:
                b_ids = [str(i.get("finding")) for i in blocked]
                last = self.st.history[-1] if self.st.history else {}
                if not (last.get("state") == "OWNER_DECISION" and last.get("lead_refused_provisional") == b_ids
                        and last.get("round") == self.st.round):
                    self.st.history.append({
                        "at": _now(), "lead_refused_provisional": b_ids, "round": self.st.round,
                        "why": f"owner policy: the lead may not provisionally accept open critic HIGH finding(s) "
                               f"{', '.join(b_ids)}",
                        "state": "OWNER_DECISION"})
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

    def _critics_behind_round(self) -> bool:
        import yaml
        for reviewer in CRITICS:
            p = os.path.join(self.run_dir, FL.CRITIQUE_FILES.get(reviewer, ""))
            try:
                doc = yaml.safe_load(open(p)) if os.path.isfile(p) else None
            except Exception:
                return False
            if isinstance(doc, dict) and doc.get("round") != self.st.round:
                return True
        return False

    def h_WRITE(self):
        tamper = self._engine_tamper_problems()
        if tamper:
            _withhold_report(self.run_dir)
            return self._go("FINAL", "agent-built engine snapshot tampered before WRITE: " + "; ".join(tamper[:4]),
                            "artifact_tampered")
        mode = self.st.write_mode or "not_accepted"
        known = _ledger_claims(self.run_dir)
        wf = [{"id": f.id, "claim": _quoted(f.claim, known), "fix_requires": _quoted(f.fix_requires, known)}
              for f in FL.open_findings(FL.load(self.run_dir)) if FX.is_write_up(f)]
        res = self._task(f"WRITE#{self._visit()}:{mode}", "write", self.run_dir, mode,
                         writeup=True, **({"findings": wf} if wf else {}))
        if res is None:
            return self._go("FINAL", "no budget left for the write-up", "completed_no_report")
        if not res.ok or not self._has_draft():
            return self._go("FINAL", "the writer produced no draft", "completed_no_report")
        render_report(self.run_dir, self.backend)
        self.st.post_write = True
        if mode != "not_accepted":
            res_p = self._task(f"WRITE#{self._visit()}:r{self.st.round}:presentation", "critique",
                               self.run_dir, "critique-presentation", self.st.round, writeup=True)
            if res_p is None:
                return self._budget_out("WRITE")
        else:
            from lib.safe_io import remove_nofollow
            remove_nofollow(self.run_dir, "critique_presentation.yaml", missing_ok=True)
        if self._critics_behind_round():
            return self._go("CRITIQUE", f"critique round {self.st.round} of the revised draft")
        return self._go("GATE", "not-accepted report written; final gate" if mode == "not_accepted"
                        else f"draft written ({mode}) and presentation reviewed")

    def _last_decision(self) -> dict | None:
        for name in (f"gate_r{self.st.round - (1 if self.st.round_pending else 0)}_post.json",
                     f"gate_r{self.st.round - (1 if self.st.round_pending else 0)}.json"):
            try:
                return json.load(open(os.path.join(self.run_dir, name)))
            except (OSError, ValueError):
                continue
        return None

    def _finish(self) -> None:
        self._reveal_hidden_seed()
        self._write_meta(drop=("paused_state", "paused_why"), status=self.st.status, finished=_now())


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


def _relaunch_motivation(path: str) -> dict:
    """motivated_by of a relaunch (amend.motivation of the parent's rethink request); never raised: `unreadable`."""
    from lib.safe_io import read_nofollow
    from toolkit.spec import amend as AM
    try:
        if not os.path.isdir(path):
            raise FileNotFoundError(path)          # a missing parent is unreadable, not a parent with no request
        try:
            req = json.loads(read_nofollow(path, SPEC_RETHINK_REQUEST)[0])
        except FileNotFoundError:
            req = None
        if req is not None and not isinstance(req, dict):
            raise ValueError("the rethink request is not a mapping")      # malformed: unreadable, not unstated
        return AM.motivation(path, req)
    except Exception as e:  # noqa: BLE001
        return {"schema": AM.MOTIVE, "parent_run": os.path.basename(os.path.abspath(path)), "source": "unreadable",
                "findings": [], "error": type(e).__name__}


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
             engine_build_usd: float | None = None,
             engine_unsandboxed: bool = False, acquire: str = "off") -> RunState:
    if acquire not in ("off", "auto"):
        raise ValueError(f"acquire must be 'off' or 'auto', got {acquire!r}")
    os.makedirs(run_dir, exist_ok=True)
    st = load_state(run_dir)
    if st is not None:
        return st
    eb_cap = ENGINE_BUILD_CAP_USD if engine_build_usd is None else min(
        max(float(engine_build_usd), MIN_TASK_ALLOWANCE_USD), ENGINE_BUILD_CAP_MAX_USD
    )
    st = RunState(question=question, state="ACQUIRE" if acquire == "auto" else "SPEC",
                  max_rounds=max_rounds, signoff=signoff, gate_mode=gate_mode, budget_usd=budget_usd,
                  derived_inputs=derived_inputs, derive_unsandboxed=derive_unsandboxed,
                  engine_unsandboxed=engine_unsandboxed,
                  engine_build_cap_usd=eb_cap, acquire=acquire)
    try:   # keep what main.py recorded first (run_dir, pipeline, commit, launcher: scripts/lib/pipeline_id.py)
        meta = json.load(open(os.path.join(run_dir, "metadata.json")))
    except (OSError, ValueError):
        meta = {}
    meta = meta if isinstance(meta, dict) else {}
    meta.update({"question": question, "started": meta.get("started") or _now(), "pipeline": "v2",
                 "max_rounds": max_rounds, "signoff": signoff, "acquire": acquire})
    if os.environ.get(PARENT_ENV):
        meta["parent_run"] = _parent_record(os.environ[PARENT_ENV])
        meta["motivated_by"] = _relaunch_motivation(os.environ[PARENT_ENV])
    _write_json(run_dir, "metadata.json", meta)
    save_state(run_dir, st)
    return st


def run_v2(question: str, run_dir: str, tasks, max_rounds: int = 3, signoff: str = "owner",
           budget_usd: float | None = None, log=print, derive_unsandboxed: bool = False, backend=None,
           cross_engine_check: bool = True, _depth: int = 0, _stamp: dict | None = None,
           engine_build_usd: float | None = None, engine_unsandboxed: bool = False,
           acquire: str = "off") -> str:
    """Entry point for main.py --v2 (and the dry run). Resumes when run_state.json exists, also a run that ended
    run_failed because its analysis step timed out, was killed or lost engine rows to the machine
    (Machine.reopen_interrupted_analysis). Holds _run_lock across built-engine runs/states (BE31)."""
    try:
        prev = load_state(run_dir)
    except (OSError, ValueError):
        prev = None
    need_lock = (
        (prev is not None and (
            prev.state in ("ENGINE_BUILD", "ENGINE_ADMIT", "ENGINE_REVIEW", "ENGINE_SIGN")
            or bool(getattr(prev, "engine_build", None))
        ))
        or spec_has_engine_build(run_dir)
    )

    def _run_once() -> str:
        st = init_run(run_dir, question, max_rounds, signoff, "v2", budget_usd, derive_unsandboxed=derive_unsandboxed,
                      engine_build_usd=engine_build_usd, engine_unsandboxed=engine_unsandboxed, acquire=acquire)
        if st.paused == AWAITING_BUDGET and budget_usd and budget_usd > (st.budget_usd or 0):
            st.history.append({"at": _now(), "budget_raised": {"from": st.budget_usd, "to": budget_usd},
                               "spent_usd": round(st.spent_usd, 4)})
            st.budget_usd = budget_usd
            save_state(run_dir, st)
        if st.paused == AWAITING_BUDGET and engine_build_usd is not None:
            new_cap = min(float(engine_build_usd), ENGINE_BUILD_CAP_MAX_USD)
            if new_cap > float(st.engine_build_cap_usd or ENGINE_BUILD_CAP_USD):
                st.history.append({"at": _now(), "engine_build_cap_raised": {"from": st.engine_build_cap_usd, "to": new_cap}})
                st.engine_build_cap_usd = new_cap
                save_state(run_dir, st)
        _record_invocation(run_dir, budget_usd, st.budget_usd)
        m = Machine(run_dir, tasks, st, backend=backend, log=log, cross_engine_check=cross_engine_check)
        m.reopen_interrupted_analysis()
        return m.run()

    if not need_lock:
        return _run_once()
    os.makedirs(run_dir, exist_ok=True)
    with _run_lock(run_dir):
        return _run_once()


# ------------------------------------------------------------------ self-test
def _self_test() -> int:
    import tempfile
    import shutil
    from v2_tasks import StubTasks
    fails = []
    toolkit = AB.backend("toolkit")

    class _FastToyBackend:
        """Delegates spec/sign-off/choice/validate/headline to `toolkit`, using a fast deterministic
        toy `analyze` step so `signoff='none'` state-machine self-tests exercise orchestration without
        hitting `toolkit/analysis/run.py`'s production sign-off refusal."""
        settings_paths = getattr(toolkit, "settings_paths", AB.SETTINGS_PATHS)

        def __getattr__(self, name):
            return getattr(toolkit, name)

        def pipeline_steps(self, spec_path, run_dir):
            import shlex
            deps = ["decision_spec.yaml", "units.csv"]
            for rel in ("analysis_settings.yaml", "engine_choice.json"):
                if os.path.exists(os.path.join(run_dir, rel)):
                    deps.append(rel)
            code = (
                "import csv, json, os, yaml\n"
                "os.makedirs('analysis', exist_ok=True)\n"
                "spec = yaml.safe_load(open('decision_spec.yaml', encoding='utf-8')) or {}\n"
                "sett = (yaml.safe_load(open('analysis_settings.yaml', encoding='utf-8')) or {}) "
                "if os.path.exists('analysis_settings.yaml') else {}\n"
                "draws = int((sett.get('uncertainty') or {}).get('draws') "
                "or (spec.get('uncertainty') or {}).get('draws') or 24)\n"
                "outcome = (spec.get('objective') or {}).get('outcome') or 'dalys'\n"
                "ucsv = (spec.get('decision_units') or {}).get('csv') or 'units.csv'\n"
                "rows = list(csv.DictReader(open(ucsv, encoding='utf-8')))\n"
                "plan = {str(r['id']): ('a' if i % 2 == 0 else 'b') for i, r in enumerate(rows)}\n"
                "pt = {'schema': 'analysis.point/1', 'data': {\n"
                "    'plan': plan, 'averted': {outcome: 1000.0 + draws}, 'cost_allocated': 5000.0,\n"
                "    'mix': {'a': sum(v == 'a' for v in plan.values()), 'b': sum(v == 'b' for v in plan.values())},\n"
                "    'solver': {'status': 'optimal'}}}\n"
                "yaml.safe_dump(pt, open('analysis/point.yaml', 'w', encoding='utf-8'), sort_keys=False)\n"
                "uq = {'schema': 'analysis.uncertainty/1', 'data': {\n"
                "    'draws': draws, 'covers': ['engine_parameters'], 'excludes': [], 'label': '95% CI',\n"
                "    'fixed_plan': {outcome: {'ci_low': 800.0 + draws, 'median': 1000.0 + draws, 'ci_high': 1200.0 + draws}}}}\n"
                "yaml.safe_dump(uq, open('analysis/uncertainty.yaml', 'w', encoding='utf-8'), sort_keys=False)\n"
                "with open('analysis/allocation.csv', 'w', encoding='utf-8', newline='') as f:\n"
                "    w = csv.writer(f)\n"
                "    w.writerow(['id', 'package', f'{outcome}_averted'])\n"
                "    for u, pkg in plan.items():\n"
                "        w.writerow([u, pkg, 100.0 + draws])\n"
                "with open('allocation_optimized.csv', 'w', encoding='utf-8', newline='') as f:\n"
                "    w = csv.writer(f)\n"
                "    w.writerow(['unit', 'package', f'{outcome}_averted'])\n"
                "    for u, pkg in plan.items():\n"
                "        w.writerow([u, pkg, 100.0 + draws])\n"
                "open('results.md', 'w', encoding='utf-8').write(f'# Toy analysis\\n\\nDraws: {draws}\\n')\n"
                "man = {'status': 'complete', 'complete': True, "
                "'spec_signed': os.path.exists('decision_spec.signoff.json')}\n"
                "open('analysis/manifest.json', 'w', encoding='utf-8').write(json.dumps(man, sort_keys=True))\n"
            )
            return [{
                "id": "analyze",
                "run": f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}",
                "inputs": deps,
                "outputs": ["analysis/manifest.json", "analysis/point.yaml", "analysis/uncertainty.yaml",
                            "analysis/allocation.csv", "allocation_optimized.csv", "results.md"],
            }]

        def gate_findings(self, rd):
            return [] if os.path.exists(os.path.join(rd, "analysis", "manifest.json")) else [
                {"kind": "analysis_contract_missing", "severity": "HIGH", "stage": "ANALYZE",
                 "claim": "missing manifest", "key": "", "scope_declarable": False}]

    fast_toy = _FastToyBackend()

    def _ToolkitMachine(*a, **k):
        k.setdefault("backend", fast_toy)
        return Machine(*a, **k)

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)

    # T1-T9: route_gate, every action in both modes (the golden traces exercise these on stored runs).
    R = route_gate
    ok(R({"action": "ACCEPT"}, False)[:2] == ("WRITE", None) and R({"action": "ACCEPT"}, True)[:2] == ("FINAL", "completed"),
       "T1 ACCEPT: write, then completed after the post-write gate")
    ok(R({"action": "PATCH_OR_RETHINK"}, False)[0] == "FIX_LOOP" and R({"action": "PATCH_OR_RETHINK"}, True)[0] == "FIX_LOOP",
       "T2 PATCH_OR_RETHINK: the fix loop, also for a post-write HIGH")
    ok(R({"action": "RETHINK_STRUCTURAL"}, False)[0] == "ENGINE_CHOICE"
       and R({"action": "RETHINK_STRUCTURAL", "unresolved_high": [{"category": "STRUCTURAL", "target_stage": "PLAN"}]},
             False)[0] == "SPEC", "T3 RETHINK_STRUCTURAL is a back-edge: ENGINE_CHOICE, or SPEC for a PLAN target")
    ok(R({"action": "DECLARE_SCOPE", "rule_matched": "3a"}, False) == ("WRITE", None, "declared")
       and R({"action": "DECLARE_SCOPE"}, True)[:2] == ("FINAL", "completed_scope_declared"),
       "T4 DECLARE_SCOPE (incl. early finish): write with the declarations, then completed_scope_declared")
    ok(R({"action": "RUN_FAILED"}, False) == ("WRITE", None, "not_accepted") and R({"action": "RUN_FAILED"}, True)[:2]
       == ("FINAL", "run_failed"), "T5 RUN_FAILED: a not-accepted report, then run_failed")
    ok(R({"action": "AWAIT_OWNER"}, False)[0] == "OWNER_DECISION", "T6 AWAIT_OWNER (Phase 49 β) waits for the owner")
    ok(R({"action": "RUN_FAILED", "unresolved_high": [{"kind": "scope_declaration_declined"}]}, True)[1]
       == "completed_scope_declined", "T7 an owner decline at exhaustion ends completed_scope_declined")
    ok(R({"schema_errors": ["x"]}, False)[0] == "CRITIQUE" and R(None, False)[1] == "completed_gate_error"
       and R({"action": "VALIDATOR_ERROR"}, True)[1] == "completed_gate_error", "T8 schema errors re-run the critics; no verdict is a gate error")
    for a in ("ACCEPT", "PATCH_OR_RETHINK", "RETHINK_STRUCTURAL", "DECLARE_SCOPE", "RUN_FAILED", "AWAIT_OWNER", None):
        for pw in (False, True):
            nxt = R({"action": a}, pw)[0]
            if nxt not in EDGES["GATE"]:
                ok(False, f"T9 {a} routes outside the state table")
    ok(True, "T9 every route is an edge of the state table")
    # T9b (T16's CI flakes, 2026-11-20/21): a failed step's error is its log tail, a traceback whose first 300
    # characters are only its outer frames; the reason a run ended must keep the exception and where it was raised.
    root = "/home/runner/work/modeling-agentic/modeling-agentic"
    tb = ("Traceback (most recent call last):\n"
          f'  File "{root}/agents/analysis_backend.py", line 1556, in <module>\n    sys.exit(main())\n'
          "             ~~~~^^\n"
          f'  File "{root}/agents/analysis_backend.py", line 776, in main\n    A = _analyze(spec, out)\n'
          f'  File "{root}/agents/analysis_backend.py", line 515, in _analyze\n'
          "    return Analysis(spec, out, log=lambda *a: None).run()\n"
          f'  File "{root}/toolkit/analysis/run.py", line 120, in solve\n'
          "    raise RuntimeError(f\"optimizer status {r['status']}\")\n"
          "RuntimeError: optimizer status feasible\n")
    why = _rep_error({"ok": False, "steps": [{"id": "configure", "status": "ok"},
                                             {"id": "analyze", "status": "failed", "error": tb}]})
    ok("RuntimeError: optimizer status feasible" in why and "run.py:120 in solve" in why and len(why) <= 300,
       f"T9b a failed step's reason keeps its exception and the frame that raised it: {why!r}")
    why2 = _rep_error({"ok": False, "errors": ["upstream step not rebuilt: configure"],
                       "steps": [{"id": "batch", "status": "failed", "error": "timed out after 8s at its backstop"}]})
    ok(why2 == "upstream step not rebuilt: configure; batch: timed out after 8s at its backstop",
       f"T9c an error that is not a traceback is kept as it was: {why2!r}")
    # T9d (Codex r1 on the T9b fix): real interpreter output, as an 800-character log tail holds it.
    def tail_of(code, n=800):
        r = subprocess.run([sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                           timeout=60)
        return r.stdout[-n:]
    cases = {
        "trailing output": ("import atexit; atexit.register(print, 'cleanup complete')\n"
                            "def solve():\n    raise RuntimeError('boom')\nsolve()", "RuntimeError: boom", "in solve"),
        "multi-line message": ("def f():\n    raise ValueError('invalid row\\ncolumn 7')\nf()",
                               "ValueError: invalid row", "in f"),
        "syntax error": ("compile('x =', 'broken.py', 'exec')", "SyntaxError", "broken.py:1"),
        "frame text in message": ("def g():\n    raise ValueError('see File \"other.py\", line 97, in other')\ng()",
                                  "ValueError: see", "in g"),
        "exception group": ("raise ExceptionGroup('workers', [ValueError('bad row')])", "ExceptionGroup: workers",
                            "<string>:1"),
        "long message": ("def h():\n    raise ValueError('x' * 320)\nh()", "ValueError: xxx", "in h"),
        "header cut": ("".join(f"def f{i}():\n    f{i + 1}()\n" for i in range(40))
                       + "def f40():\n    raise RuntimeError('deep')\nf0()", "RuntimeError: deep", "in f40"),
    }
    for name, (code, want_exc, want_at) in cases.items():
        why_ = _rep_error({"ok": False, "steps": [{"id": "analyze", "status": "failed", "error": tail_of(code)}]})
        ok(want_exc in why_ and want_at in why_ and "Traceback at" in why_ and "at other.py" not in why_
           and "Exception Group Traceback" not in why_,
           f"T9d a traceback with {name} keeps its exception and raising frame: {why_!r}")
    to = "timed out after 8s at its backstop (process group killed); last output: " + tail_of(
        "def s():\n    raise RuntimeError('boom')\ns()", 300)
    why3 = _rep_error({"ok": False, "steps": [{"id": "analyze", "status": "failed", "error": to}]})
    ok(why3.startswith("analyze: timed out after 8s") and "RuntimeError: boom" in why3,
       f"T9e a timeout whose last output is a traceback still reads as the timeout first: {why3!r}")

    with tempfile.TemporaryDirectory() as td:
        def new_run(name, script, signoff="none", max_rounds=3, budget=None):
            import yaml as _y
            from toolkit.spec import schema as _schema
            d = os.path.join(td, name)
            os.makedirs(d)
            spec_p = _schema.toy_spec(d, n=12)
            with open(spec_p) as fh:
                spec = _schema.direct_toy(_y.safe_load(fh))
            os.unlink(spec_p)
            spec["engine"] = "auto"
            spec["uncertainty"].pop("draws", None)
            spec["uncertainty"].pop("seed", None)
            spec["deliverable_file"] = {"path": "allocation_optimized.csv",
                                        "columns": {"unit": "id", "package": "package", "dalys_averted": "dalys_averted"}}
            spec.update(script.pop("spec_extra", {}))
            script = dict({"spec": spec, "settings": {"uncertainty": {"draws": 24, "seed": 1}}}, **script)
            st = init_run(d, "toy question", max_rounds, signoff, "v2", budget, derived_inputs=False)   # pre-DI fixture
            return d, StubTasks(script), st

        # T10: the happy path with one critic finding fixed in the loop.
        blocker = {"id": "D-001", "severity": "HIGH", "target_stage": "ANALYZE", "claim": "too few draws",
                   "done_when": [{"file": "analysis_settings.yaml", "key": "uncertainty.draws", "op": ">=", "value": 32}]}
        d, T, st = new_run("happy", {"critics": {1: {"critique-domain": [blocker]}},
                                     "fixes": {"D-001": [("analysis_settings.yaml", "uncertainty.draws", 32)],
                                               "uncertainty_draws_low:draws": [("analysis_settings.yaml", "uncertainty.draws", 32)]}})
        status = _ToolkitMachine(d, T, st, log=lambda *a: None, cross_engine_check=False).run()
        hist = [h["to"] for h in load_state(d).history if "to" in h]
        ok(status == "completed", f"T10 toy run completes: {status} via {hist}")
        ok(hist[:5] == ["EVIDENCE", "SIGN", "ENGINE_CHOICE", "ANALYZE", "CRITIQUE"]
           and "FIX_LOOP" in hist
           and hist[-2:] == ["GATE", "FINAL"], f"T11 the path SPEC..FIX_LOOP..WRITE..FINAL {hist}")
        from toolkit.spec import evidence_draft as EDR_
        ok("draft_evidence" not in T.calls and (EDR_.read_record(d) or {}).get("status") == "not_required",
           f"T11b a spec whose options need no decision input passes EVIDENCE with no task ({EDR_.read_record(d)})")
        rows = FX.impact_rows(d)
        ok(rows and rows[0]["files_changed"] == ["analysis_settings.yaml"] and "analyze" in rows[0]["steps_rebuilt"]
           and rows[0]["headline_before"] != rows[0]["headline_after"] and rows[0]["passed"],
           f"T12 the fix has an impact row: settings changed, analysis rebuilt, headline moved, check passed")
        led = FL.load(d)
        ok(led["D-001"].status == "fixed" and os.path.exists(os.path.join(d, "allocation_optimized.csv"))
           and "Numbers (rendered by the harness)" in open(os.path.join(d, "report.md")).read(),
           "T13 the ledger closes the finding; report and deliverable are rendered by the harness")
        ok(json.load(open(os.path.join(d, "engine_choice.json")))["engine"]["ref"].endswith("ToyEngine")
           and AB.engine_choice_ref(d).endswith("ToyEngine") and "candidates" in json.load(open(os.path.join(d, AB.ENGINE_CHOICE)))
           and load_state(d).engine.endswith("ToyEngine"), "T14 the engine choice is automatic and recorded")
        ok(json.load(open(os.path.join(d, "metadata.json")))["status"] == "completed", "T15 metadata.json carries the status")

        # T16: resume after every state. Stop after k steps, rebuild the machine from disk, finish.
        d0, T0, st0 = new_run("ref", {"critics": {1: {"critique-domain": [blocker]}},
                                      "fixes": {"D-001": [("analysis_settings.yaml", "uncertainty.draws", 32)]}})
        ref_status = _ToolkitMachine(d0, T0, st0, log=lambda *a: None, cross_engine_check=False).run()
        ref_path = [h["to"] for h in load_state(d0).history if "to" in h]

        def _final_why(d_):     # the reason a run reached FINAL (a step's error carries its log tail): CI flakes of
            return [h.get("why") for h in load_state(d_).history if h.get("to") == "FINAL"][-1:]   # T16 named none
        ref_why = _final_why(d0) if ref_status != "completed" else []
        bad = []
        for k in range(1, len(ref_path)):
            dk, Tk, stk = new_run(f"resume{k}", {"critics": {1: {"critique-domain": [blocker]}},
                                                "fixes": {"D-001": [("analysis_settings.yaml", "uncertainty.draws", 32)]}})
            m = _ToolkitMachine(dk, Tk, stk, log=lambda *a: None, cross_engine_check=False)
            for _ in range(k):
                m.step()
            del m
            m2 = _ToolkitMachine(dk, StubTasks(Tk.s), None, log=lambda *a: None, cross_engine_check=False)   # from run_state.json
            s2 = m2.run()
            p2 = [h["to"] for h in load_state(dk).history if "to" in h]
            if s2 != ref_status or p2 != ref_path:
                bad.append((k, s2, p2, _final_why(dk)))
            shutil.rmtree(dk)
        ok(not bad, f"T16 a run stopped after any of {len(ref_path) - 1} transitions resumes to the same path and status {bad[:1]}"
           + (f" (reference run {ref_status}: {ref_why})" if ref_why else ""))

        # T17: owner sign-off pauses, persists, resumes.
        d, T, st = new_run("owner", {}, signoff="owner")
        m = _ToolkitMachine(d, T, st, log=lambda *a: None, cross_engine_check=False)
        s = m.run()
        ok(s == "paused:awaiting_signoff" and load_state(d).state == "SIGN", f"T17 owner mode pauses at SIGN ({s})")
        pz = [(h.get("paused"), h.get("state")) for h in load_state(d).history if "paused" in h]
        ok(pz == [("awaiting_signoff", "SIGN")], f"RR1 a SIGN pause writes one history entry ({pz})")
        _mp = json.load(open(os.path.join(d, "metadata.json")))     # PS1 (baseline hb3 §4.2: a pause wrote no status)
        ok(_mp.get("status") == "paused:awaiting_signoff" and _mp.get("paused_state") == "SIGN"
           and _mp.get("paused_why") and "finished" not in _mp,
           f"PS1 a paused run records paused:<reason>, its state and why in metadata.json, and no finish time "
           f"({[_mp.get(k) for k in ('status', 'paused_state', 'paused_why', 'finished')]})")
        toolkit.sign(d, "owner", "ok")
        ok(_ToolkitMachine(d, StubTasks(T.s), None, log=lambda *a: None, cross_engine_check=False).run() == "completed",
           "T18 after the owner signs, the resumed run completes")

        # T19: a fix that edits the signed spec, or puts a decision leaf in the settings file, is
        # undone before anything is rebuilt; the spec stays as signed.
        d, T, st = new_run("tamper", {"critics": {1: {"critique-redteam": [dict(blocker, id="R-001")]}},
                                      "fixes": {"R-001": [("decision_spec.yaml", "constraints.envelope_usd", 99999),
                                                          ("analysis_settings.yaml", "constraints.envelope_usd", 99999)]}},
                           signoff="lead", max_rounds=2)
        signed_before = None

        class Spy(StubTasks):
            def fix(self, task, run_dir, evidence):
                nonlocal signed_before
                signed_before = signed_before or signed_hash(run_dir)
                return super().fix(task, run_dir, evidence)
        s = _ToolkitMachine(d, Spy(T.s), st, log=lambda *a: None, cross_engine_check=False).run()
        rows = FX.impact_rows(d)
        led = FL.load(d)
        ok(rows and all(r.get("rejected") and not r.get("steps_rebuilt") for r in rows)
           and signed_hash(d) == signed_before and "constraints.envelope_usd" not in AB._leaves(
               __import__("yaml").safe_load(open(os.path.join(d, AB.SETTINGS_FILE))) or {})
           and "spec_changed_after_signoff:spec" not in led and s == "completed_scope_declared",
           f"T19 edits to the signed spec are rejected and undone before any rebuild ({s}, {[r.get('rejected') for r in rows][:1]})")

        # T20: a structural mismatch is a back-edge to ENGINE_CHOICE, then the run continues.
        d, T, st = new_run("rethink", {"structural": {1: "critique-domain"}})
        s = _ToolkitMachine(d, T, st, log=lambda *a: None, cross_engine_check=False).run()
        p = [h["to"] for h in load_state(d).history if "to" in h]
        i = p.index("GATE")
        ok(p[i + 1] == "ENGINE_CHOICE" and s == "completed" and load_state(d).round == 2,
           f"T20 RETHINK_STRUCTURAL goes back to ENGINE_CHOICE and the next critique is round 2 ({s})")

        # T21: an open blocker at the last round: DECLARE_SCOPE when declarable.
        nofix = {"id": "D-009", "severity": "HIGH", "target_stage": "ANALYZE", "claim": "cannot fix"}
        d, T, st = new_run("declare", {"critics": {1: {"critique-domain": [nofix]}}}, max_rounds=2)
        s = _ToolkitMachine(d, T, st, log=lambda *a: None, cross_engine_check=False).run()
        ok(s == "completed_scope_declared", f"T21 an unfixed critic blocker at exhaustion ends scope-declared ({s})")
        rep21 = open(os.path.join(d, "report.md")).read()
        ok(os.path.exists(os.path.join(d, DECLARED_FILE)) and "D-009" in rep21 and "Open findings, declared" in rep21,
           "T46 (Codex #95-5) an ordinary DECLARE_SCOPE (rule 3) also lists what is open in the report")

        # T22: the budget, not the gate, ends the run: the harness writes the report.
        d, T, st = new_run("budget", {"critics": {1: {"critique-domain": [nofix]}}, "usd": 30.0}, budget=100.0)
        s = _ToolkitMachine(d, T, st, log=lambda *a: None, cross_engine_check=False).run()
        p = [h["to"] for h in load_state(d).history if "to" in h]
        ok(s == "budget_exhausted" and "WRITE" in p and os.path.exists(os.path.join(d, "report.md")),
           f"T22 with the budget used the run ends budget_exhausted with a report ({s}, {p[-4:]})")

        # T25: an interrupted paid task. The redteam critique crashes once in round 1: on resume the
        # domain critique (completed) is not run again, and the interrupted one is charged its allowance.
        d, T, st = new_run("crash", {"crash": {"critique:critique-redteam:1": 1}, "usd": 1.0}, budget=100.0)
        m = _ToolkitMachine(d, T, st, log=lambda *a: None, cross_engine_check=False)
        try:
            m.run()
            ok(False, "T25 the stub crash propagates")
        except RuntimeError:
            pass
        spent_at_crash = load_state(d).spent_usd
        T2 = StubTasks(dict(T.s, crash={}))
        s = _ToolkitMachine(d, T2, None, log=lambda *a: None, cross_engine_check=False).run()
        j = load_state(d).journal
        dom = [k for k in j if k.endswith(":r1:critique-domain")]
        red = [k for k in j if k.endswith(":r1:critique-redteam")]
        ok(s == "completed" and "critique:critique-domain:1" not in T2.calls and T2.calls.count("critique:critique-redteam:1") == 1
           and dom and j[dom[0]]["status"] == "completed" and red and j[red[0]].get("interrupted_charged") == 1,
           f"T25 resume skips the completed critique and re-runs the interrupted one ({s}, {T2.calls[:3]})")
        ok(abs(load_state(d).spent_usd - spent_at_crash - 3.0 - 1.0 * len(T2.calls)) < 1e-9,
           f"T26 the interrupted task is charged its allowance ($3) once; completed ones are not charged again "
           f"({spent_at_crash} -> {load_state(d).spent_usd}, {len(T2.calls)} new calls)")

        # T27: each paid task gets an allowance from what is left (less the write-up reserve), set before
        # the call; the second critic of a pair is not called when the first used the budget.
        d, T, st = new_run("allow", {"usd": 9.0}, budget=30.0)
        st.task_cap_usd = 9.0
        save_state(d, st)
        s = _ToolkitMachine(d, T, st, log=lambda *a: None, cross_engine_check=False).run()
        ok(T.allowances == [9.0, 9.0, 2.0, 3.0] and "critique:critique-redteam:1" not in T.calls
           and s == "budget_exhausted",
           f"T27 allowances follow the budget and the pair stops when it is used ({T.allowances}, {s})")
        jj = load_state(d).journal
        ok(all("allowance_usd" in e for k, e in jj.items() if not k.endswith("spec_sha256")),
           "T28 every journal entry records the allowance it was given")

        # R3-2 (F6 run 3, gap 2; narrowed, lead decision): a SPEC rethink of a signed spec never redrafts it in the run.
        # The machine writes spec_rethink_request.json (why: the rethink's brief, the findings and their remedies) and
        # pauses for the owner, who amends the spec and relaunches with reuse; the signed files stay as signed.
        rk = {"id": "D-040", "severity": "HIGH", "target_stage": "PLAN", "claim": "the envelope is the wrong year",
              "fix_requires": "redraft the envelope for the decision year"}
        once40 = []

        def plan40(pk):
            if any(f["id"] == "D-040" for f in pk["findings"]) and not once40:
                once40.append(1)
                return {"tasks": [{"id": "T1", "findings": ["D-040"], "action": "rethink", "owner_state": "SPEC",
                                   "brief": "redraft the envelope"}]}
            return {"tasks": []}
        d, T, st = new_run("rethink_signed", {"critics": {1: {"critique-domain": [rk]}}, "plan": plan40},
                           signoff="owner", max_rounds=3)
        _ToolkitMachine(d, T, st, log=lambda *a: None, cross_engine_check=False).run()
        toolkit.sign(d, "owner", "first")
        signed_bytes = open(os.path.join(d, AB.SPEC_FILE), "rb").read()
        T2 = StubTasks(T.s)
        s = _ToolkitMachine(d, T2, None, log=lambda *a: None, cross_engine_check=False).run()
        try:
            rq = json.load(open(os.path.join(d, "spec_rethink_request.json")))
        except (OSError, ValueError):
            rq = {}
        ok(s == "paused:awaiting_spec_amendment" and "draft_spec" not in T2.calls
           and open(os.path.join(d, AB.SPEC_FILE), "rb").read() == signed_bytes
           and "D-040" in json.dumps(rq.get("findings")) and "redraft the envelope" in json.dumps(rq.get("briefs"))
           and rq.get("signed_spec_sha256") == hashlib.sha256(signed_bytes).hexdigest(),
           f"R3-2 a rethink of a signed spec writes the request and pauses; nothing is redrafted ({s}, {T2.calls[-3:]})")
        pz = [(h.get("paused"), h.get("state")) for h in load_state(d).history if "paused" in h]
        ok(pz[-1:] == [("awaiting_spec_amendment", "SPEC")], f"RR2 an amendment pause writes a history entry ({pz})")
        T3 = StubTasks(T.s)
        s2 = _ToolkitMachine(d, T3, None, log=lambda *a: None, cross_engine_check=False).run()
        ok(s2 == "paused:awaiting_spec_amendment" and "draft_spec" not in T3.calls
           and open(os.path.join(d, AB.SPEC_FILE), "rb").read() == signed_bytes,
           f"R3-2b a resume stays paused for the amendment and drafts nothing ({s2})")
        # R3-2c (Codex review of #261): the pause is honoured on resume even if a restored copy lost the sign-off record,
        # and keeps its first request; and it is written with no budget left (it costs no model call)
        rq0 = json.load(open(os.path.join(d, "spec_rethink_request.json")))
        os.remove(os.path.join(d, SIGNOFF_FILE))
        T4 = StubTasks(T.s)
        s3 = _ToolkitMachine(d, T4, None, log=lambda *a: None, cross_engine_check=False).run()
        ok(s3 == "paused:awaiting_spec_amendment" and "draft_spec" not in T4.calls
           and json.load(open(os.path.join(d, "spec_rethink_request.json")))["requested_at"] == rq0["requested_at"],
           f"R3-2c a resumed amendment pause is honoured without the sign-off record and keeps its request ({s3})")
        st5 = load_state(d)
        st5.budget_usd, st5.spent_usd = 20.0, 20.0
        save_state(d, st5)
        s5 = _ToolkitMachine(d, StubTasks(T.s), None, log=lambda *a: None, cross_engine_check=False).run()
        ok(s5 == "paused:awaiting_spec_amendment", f"R3-2d with no budget left the amendment pause still holds ({s5})")
        # R3-2e (narrow re-review of #261): an unreadable sign-off record still protects the spec; a persisted pause is
        # honoured even without the flag (a state saved before it existed)
        d6 = os.path.join(td, "r32e")
        os.makedirs(d6)
        open(os.path.join(d6, SIGNOFF_FILE), "w").write('{"by": "own')
        st6 = load_state(d)
        st6.tries.pop("spec_amendment", None)
        st6.paused = AWAITING_SPEC_AMENDMENT
        save_state(d, st6)
        T6 = StubTasks(T.s)
        m6 = _ToolkitMachine(d, T6, None, log=lambda *a: None, cross_engine_check=False)
        m6.step()
        ok(spec_signed_once(d6) and "draft_spec" not in T6.calls and load_state(d).paused == AWAITING_SPEC_AMENDMENT,
           "R3-2e a truncated sign-off record still protects the spec; a persisted amendment pause is honoured")
        # R3-2f (lead follow-up): the pause says when the signing evidence is missing or unreadable (a warning, no block)
        rq6 = json.load(open(os.path.join(d, "spec_rethink_request.json")))
        ok(rq6.get("signing_evidence") == "missing" and "requested_at" in rq6,
           f"R3-2f the amendment request notes missing signing evidence ({rq6.get('signing_evidence')})")

        # R3-1 (F6 run 3): a write-up finding is never a FIX_LOOP task; the writer gets it with the draft.
        wup = {"id": "R-030", "severity": "HIGH", "target_stage": "WRITE", "claim": "the report overstates the effect"}
        d, T, st = new_run("writeup", {"critics": {1: {"critique-redteam": [wup]}}}, max_rounds=2)
        s = _ToolkitMachine(d, T, st, log=lambda *a: None, cross_engine_check=False).run()
        p = [h["to"] for h in load_state(d).history if "to" in h]
        got = getattr(T, "write_findings", None) or []
        ok("FIX_LOOP" in p and not any(c.startswith("fix:") for c in T.calls)
           and any(f.get("id") == "R-030" for f in got),
           f"R3-1 a write-up finding goes to the writer, not to a FIX_LOOP task ({s}, {T.calls}, {got})")

        # R3-3 (F6 run 3): a finding whose remedy is an owner amendment goes to OWNER_DECISION, never a SPEC rethink.
        amend = {"id": "D-031", "severity": "HIGH", "target_stage": "PLAN", "claim": "the comparator omits the funded plan",
                 "fix_requires": "Owner amendment: add the funded plan as the comparator."}
        d, T, st = new_run("amend", {"critics": {1: {"critique-domain": [amend]}},
                                     "plan": lambda pk: {"tasks": [{"id": "T1", "findings": [f["id"] for f in
                                                                                                pk["findings"]][:1] or ["x"],
                                                                    "action": "rethink", "owner_state": "SPEC",
                                                                    "brief": "redraft the comparator"}]
                                                         if pk["findings"] else {"tasks": []}}}, max_rounds=3)
        s = _ToolkitMachine(d, T, st, log=lambda *a: None, cross_engine_check=False).run()
        p = [h["to"] for h in load_state(d).history if "to" in h]
        try:
            req = json.load(open(os.path.join(d, "scope_signoff_request.json")))
        except (OSError, ValueError):
            req = {}
        ok(s == "paused:awaiting_scope_signoff" and load_state(d).state == "OWNER_DECISION"
           and "SPEC" not in p and not any(c.startswith("fix:") for c in T.calls)
           and req.get("pending") and [i.get("finding") for i in req.get("items") or []] == ["D-031"],
           f"R3-3 an owner-amendment finding pauses at OWNER_DECISION with a request, no redraft ({s}, {p[-4:]})")
        import harness_metrics as _HM
        pz = _HM.pauses(_HM.load_run(d))
        ok([(x["reason"], x["state"], x["from"]) for x in pz][-1:] == [("awaiting_scope_signoff", "OWNER_DECISION",
                                                                        "record")],
           f"RR3 a scope pause writes a history entry; harness_metrics reads it as a record ({pz[-1:]})")
        if req.get("pending"):
            import scope_signoff as _SS
            # Codex re-review of #258 P1-8: the request the machine made is what must be answered, not whatever request
            # file is there now
            good_req = open(os.path.join(d, "scope_signoff_request.json")).read()
            json.dump(dict(req, items=[dict(req["items"][0], fingerprint="stale0000000000")]),
                      open(os.path.join(d, "scope_signoff_request.json"), "w"))
            _SS.sign(d, "owner", all_decision="accept", note="stale")
            open(os.path.join(d, "scope_signoff_request.json"), "w").write(good_req)
            s_st = _ToolkitMachine(d, StubTasks(T.s), None, log=lambda *a: None, cross_engine_check=False).run()
            ok(s_st == "paused:awaiting_scope_signoff",
               f"R3-3d an answer to a request the machine did not make does not release OWNER_DECISION ({s_st})")
            _SS.sign(d, "lead", all_decision="accept", note="R3-3c")     # Codex review of #258 P1-8: not the owner's
            s_l = _ToolkitMachine(d, StubTasks(T.s), None, log=lambda *a: None, cross_engine_check=False).run()
            ok(s_l == "paused:awaiting_scope_signoff",
               f"R3-3c an answer that is not the owner's does not release OWNER_DECISION ({s_l})")
            _SS.sign(d, "owner", all_decision="decline", note="R3-3")
            s2 = _ToolkitMachine(d, StubTasks(T.s), None, log=lambda *a: None, cross_engine_check=False).run()
            p2 = [h["to"] for h in load_state(d).history if "to" in h]
            ok(s2 == "completed_scope_declined" and "SPEC" not in p2 and FL.load(d)["D-031"].status == "owner_declined"
               and "`completed_scope_declined`" in open(os.path.join(d, "report.md")).read(),
               f"R3-3b the owner's decline resumes the run, is not redrafted, and constrains the gate: the run is not "
               f"declared or accepted (Codex review of #258 P1-11) ({s2}, {p2[-5:]})")
        # W0-F: a held-out target whose automatic test changes the plan pauses the run once at OWNER_DECISION; one
        # whose test changes nothing is a declared limitation, and the run is not interrupted
        def conflict_run(name, changes, **kw):
            d_, T_, st_ = new_run(name, {}, max_rounds=2, **kw)
            os.makedirs(os.path.join(d_, "analysis"), exist_ok=True)
            import yaml as _y
            _y.safe_dump({"data": {"policy": "declare_and_test", "refused": None, "needs_owner": ["g|1"] if changes
                                   else [], "declared": [{"n": 1, "kind": "offset", "key": "g|1", "target": "g",
                                                          "units": ["1"], "why": {}, "tests": ["conflict_1_g_1_lo"],
                                                          "scales": [0.8], "verdicts": {"conflict_1_g_1_lo":
                                                                                        "CHANGES_PLAN" if changes
                                                                                        else "NO_CHANGE"},
                                                          "changes_plan": changes}]}},
                         open(os.path.join(d_, "analysis", "conflicts.yaml"), "w"))
            s_ = _ToolkitMachine(d_, T_, st_, log=lambda *a: None, cross_engine_check=False).run()
            return d_, T_, s_
        dq, Tq, sq = conflict_run("conflict_change", True)
        try:
            rq = json.load(open(os.path.join(dq, "scope_signoff_request.json")))
        except (OSError, ValueError):
            rq = {}
        ok(sq == "paused:awaiting_scope_signoff" and [i.get("finding") for i in rq.get("items") or []]
           == ["evidence_conflict_declared:g|1"] and "held-out" in rq["items"][0]["reason"]
           and any("refit without the target" in o for o in rq["items"][0].get("options") or []),
           f"T_CONFLICT_PLAN_CHANGE_PAUSES_ONCE a conflict whose test changes the plan pauses at OWNER_DECISION ({sq})")
        if rq.get("pending"):
            import scope_signoff as _SS
            _SS.sign(dq, "owner", all_decision="accept", note="W0-F")
            sq2 = _ToolkitMachine(dq, StubTasks(Tq.s), None, log=lambda *a: None, cross_engine_check=False).run()
            nq = sum(1 for h in load_state(dq).history if h.get("to") == "OWNER_DECISION")
            ok(sq2 in ACCEPTED_STATUSES and nq == 1
               and "Held-out calibration targets" in open(os.path.join(dq, "report.md")).read(),
               f"T_CONFLICT_PLAN_CHANGE_PAUSES_ONCE the owner's accept resumes the run, which is not paused again, and "
               f"the report lists the held-out target ({sq2}, {nq} pause(s))")
        # T_CONFLICT_LEAD_FLAGGED (Codex r4 of #316): under --signoff lead a conflict whose test changes the plan is
        # answered provisionally and flagged as plan-changing: the request item carries the conflict's structured
        # verdict, and the report names it above the draft and labels it "plan changes: yes"
        dl, Tl, sl = conflict_run("conflict_change_lead", True, signoff="lead")
        try:
            rl = json.load(open(os.path.join(dl, "scope_signoff_request.json")))
        except (OSError, ValueError):
            rl = {}
        it_l = next((i for i in rl.get("items") or [] if i.get("finding") == "evidence_conflict_declared:g|1"), {})
        rep_cl = open(os.path.join(dl, "report.md")).read() if os.path.exists(os.path.join(dl, "report.md")) else ""
        try:
            span_cl = json.load(open(os.path.join(dl, "report_render.json")))["writer_span"]
        except (OSError, ValueError, KeyError):
            span_cl = [0, 0]
        top_cl = rep_cl[:span_cl[0]]
        ok(not sl.startswith("paused") and (it_l.get("conflict") or {}).get("status") == "CHANGES_PLAN"
           and (it_l.get("conflict") or {}).get("verdicts") == {"conflict_1_g_1_lo": "CHANGES_PLAN"}
           and "Provisional decisions that change the plan" in top_cl and "evidence_conflict_declared:g|1" in top_cl
           and "**evidence_conflict_declared:g|1**: accepted provisionally; plan changes: yes" in rep_cl[span_cl[1]:],
           f"T_CONFLICT_LEAD_FLAGGED a lead run flags a plan-changing conflict above the draft, with its verdict in the "
           f"request ({sl}, {it_l.get('conflict')}, top has it: {'evidence_conflict_declared' in top_cl})")
        # T_CONFLICT_OLD_REQUEST (Codex r5 of #316): a conflict item saved before the verdict was carried (a run resumed
        # across this change) is plan-changing: the owner request holds only conflicts that change the plan or could
        # not be tested
        ok(_plan_changing({"finding": "evidence_conflict_declared:g|1", "kind": "evidence_conflict_declared"}) is True
           and _plan_changing({"kind": "evidence_conflict_declared", "conflict": {"changes_plan": False}}) is False,
           "T_CONFLICT_OLD_REQUEST a conflict item without its verdict counts as changing the plan")
        # T_NOT_RUN_PROVISIONAL (Codex r1 of #375, P1-3): a lead-accepted decision test that did not run reads "not
        # tested", never "plan changes: no", and is named above the draft
        import scope_signoff as _SSn
        _prov = _SSn.provisional
        _SSn.provisional = lambda rd: [{"finding": "decision_test_not_run:DT3", "plan_changing": False, "review": None}]
        try:
            os.makedirs(os.path.join(td, "not_run_prov"), exist_ok=True)
            top_n, sec_n = provisional_lines(os.path.join(td, "not_run_prov"))
        finally:
            _SSn.provisional = _prov
        ok(any("did not run" in x and "decision_test_not_run:DT3" in x for x in top_n)
           and any("decision_test_not_run:DT3**: accepted provisionally; plan changes: not tested" in x for x in sec_n),
           f"T_NOT_RUN_PROVISIONAL a provisionally accepted unrun test reads not tested ({top_n}, {sec_n[-1:]})")
        dn, Tn, sn = conflict_run("conflict_nochange", False)
        ok(sn in ACCEPTED_STATUSES and not os.path.exists(os.path.join(dn, "scope_signoff_request.json"))
           and FL.load(dn)["evidence_conflict_declared:g|1"].status == "declared"
           and "Held-out calibration targets" in open(os.path.join(dn, "report.md")).read(),
           f"T_CONFLICT_NO_CHANGE_NO_PAUSE a conflict whose test changes nothing is declared, listed, never paused ({sn})")
        # R3-6 (Codex re-review of #258): a declared finding still needs the owner's answer (P1-10), and a decline of a
        # finding its test has since resolved does not hold the gate (new P1-2)
        f6d = FL.Finding(id="D-036", kind="critique_blocker", severity="HIGH", source="critique-domain", status="declared",
                         fix_requires="Owner amendment: x")
        f6x = FL.Finding(id="D-037", kind="critique_blocker", severity="HIGH", source="critique-domain",
                         status="owner_declined", resolution_type="owner_decision")
        ok(pending_amendments({"D-036": f6d}) == ["D-036"]
           and apply_owner_declines({"action": "ACCEPT", "resolved_by_test": ["D-037"]}, {"D-037": f6x})["action"]
           == "ACCEPT" and apply_owner_declines({"action": "ACCEPT"}, {"D-037": f6x})["action"] == "RUN_FAILED",
           "R3-6 a declared amendment is asked; a declined finding its test resolved does not hold the gate")
        # R3-7 (Codex round 3 of #258): a declared amendment whose test passed is not asked (5); a declined finding
        # leaves declared_findings.yaml (1); a pause with no recorded request fails loudly, never waits (2)
        ok(pending_amendments({"D-036": f6d}, resolved=["D-036"]) == [],
           "R3-7a a declared amendment whose test passes is not sent to the owner")
        import yaml as _y7
        d7 = os.path.join(td, "r37")
        os.makedirs(d7)
        _y7.safe_dump({"rule": "3m", "declared": [{"id": "D-037"}, {"id": "D-038"}]},
                      open(os.path.join(d7, DECLARED_FILE), "w"))
        drop_declined(d7, ["D-037"])
        ok([x["id"] for x in _y7.safe_load(open(os.path.join(d7, DECLARED_FILE)))["declared"]] == ["D-038"],
           "R3-7b a finding the owner declined is no longer listed as declared")
        d, T, st = new_run("noreq", {})
        st.state = "OWNER_DECISION"
        save_state(d, st)
        _ToolkitMachine(d, T, None, log=lambda *a: None, cross_engine_check=False).step()   # the handler itself
        s7 = load_state(d).status
        ok(s7 == "run_failed" and "relaunch with reuse" in str(load_state(d).history[-1].get("why")),
           f"R3-7c a pause at OWNER_DECISION with no recorded request fails with how to go on ({s7})")
        # R3-7e (narrow Codex review of #272): a resume whose preconditions fail ends the run run_failed before any
        # handler runs; an earlier report.md is not left deliverable under that status; malformed signing evidence is
        # "unreadable", never a crash or "present"
        d, T, st = new_run("noreq_resume", {})
        st.state = "OWNER_DECISION"
        save_state(d, st)
        open(os.path.join(d, "report.md"), "w").write("# Report\n\nStatus: completed\n")
        s7e = _ToolkitMachine(d, T, None, log=lambda *a: None, cross_engine_check=False).run()
        ok(s7e == "run_failed" and not os.path.exists(os.path.join(d, "report.md"))
           and os.path.exists(os.path.join(d, "report_withheld.md")),
           f"R3-7e a run failed on resume does not leave an earlier report deliverable ({s7e})")
        d7f = os.path.join(td, "r37f")
        os.makedirs(d7f)
        ok_hex = "a" * 64
        json.dump({"spec_sha256": None, "sha256": {AB.SPEC_FILE: ok_hex}}, open(os.path.join(d7f, SIGNOFF_FILE), "w"))
        ok(_spec_signed_sha(d7f) == ok_hex, "R3-7g a null spec_sha256 beside a valid nested digest is still signed")
        for bad in ({"sha256": "invalid"}, {"spec_sha256": "invalid"}, ["x"], {"spec_sha256": None}):
            json.dump(bad, open(os.path.join(d7f, SIGNOFF_FILE), "w"))
            ok(_spec_signed_sha(d7f) == "unreadable", f"R3-7f a malformed sign-off record reads unreadable ({bad})")
        # R3-5 (Codex review of #258 P1-10): the amendment check runs on every gate route: a structural PLAN blocker
        # whose remedy is an owner amendment does not take GATE -> SPEC
        amend5 = dict(amend, id="D-035", category="STRUCTURAL")
        d, T, st = new_run("amend5", {"critics": {1: {"critique-domain": [amend5]}}, "structural": {1: "critique-domain"}})
        s = _ToolkitMachine(d, T, st, log=lambda *a: None, cross_engine_check=False).run()
        p = [h["to"] for h in load_state(d).history if "to" in h]
        ok(s == "paused:awaiting_scope_signoff" and "SPEC" not in p and p[-1] == "OWNER_DECISION",
           f"R3-5 a structural blocker that needs an owner amendment goes to the owner, not to SPEC ({s}, {p[-3:]})")

        # T_OWNER_DECISION_E2E (1F): a decision test that changes the plan goes to OWNER_DECISION with a request the
        # machine recorded; the owner's accept resumes the run, which ends accepted with the dependence at the top of
        # the report (owner decision (b), 2026-11-23; it ended scope-declared before)
        import scope_signoff as _SS
        dtf_e = {"kind": "decision_test_changes_plan", "severity": "HIGH", "stage": "ANALYZE", "key": "DT9",
                 "claim": "Decision test DT9 changes the plan: a lower effect", "scope_declarable": True,
                 "decision_bearing": True}

        class _DTMachine(Machine):
            def harness_findings(self):
                return super().harness_findings() + [dict(dtf_e)]
        mk = lambda d_, T_, st_: _DTMachine(d_, T_, st_, backend=fast_toy, log=lambda *a: None,  # noqa: E731
                                            cross_engine_check=False)
        d, T, st = new_run("dt_owner", {}, signoff="owner", max_rounds=1)
        mk(d, T, st).run()
        toolkit.sign(d, "owner", "ok")
        s_o = mk(d, StubTasks(T.s), None).run()
        st_o = load_state(d)
        req_o = json.load(open(os.path.join(d, OWNER_REQUEST))) if os.path.exists(os.path.join(d, OWNER_REQUEST)) else {}
        fps_o = [i.get("fingerprint") for i in req_o.get("items") or []]
        ok(s_o == "paused:awaiting_scope_signoff" and st_o.state == "OWNER_DECISION" and req_o.get("pending")
           and fps_o and st_o.tries.get("owner_request") == fps_o
           and [i.get("finding") for i in req_o["items"]] == ["decision_test_changes_plan:DT9"],
           f"T_OWNER_DECISION_E2E the AWAIT_OWNER route records a fingerprinted request and pauses ({s_o}, {fps_o})")
        _SS.sign(d, "owner", all_decision="accept", note="the plan stands with this caveat")
        s_o2 = mk(d, StubTasks(T.s), None).run()
        rep_o = open(os.path.join(d, "report.md")).read() if os.path.exists(os.path.join(d, "report.md")) else ""
        span_o = json.load(open(os.path.join(d, "report_render.json")))["writer_span"] if rep_o else [0, 0]
        top_o = rep_o[:span_o[0]]
        ok(s_o2 == "completed" and "The plan depends on inputs the owner accepted" in top_o and "**DT9**" in top_o
           and "accepted by the owner" in top_o and "Provisional decisions" not in rep_o
           and FL.load(d)["decision_test_changes_plan:DT9"].status == "owner_accepted",
           f"T_OWNER_DECISION_E2E2 the owner's accept resumes the run, which ends accepted with the dependence stated "
           f"above the draft ({s_o2}, {top_o[-400:]!r})")
        # T_OWNER_LEAD_PROVISIONAL (1F, D2): under --signoff lead the same finding never waits: the lead answers it
        # provisionally, the answer is recorded and listed, and, as the plan changes, flagged above the report's draft
        d, T, st = new_run("dt_lead", {}, signoff="lead", max_rounds=1)
        held, real_hr = [], _SS.harness_record

        def _hr(rd):     # T_OWNER_LEAD_LOCK (Codex r4 of #316): the lock is held while the answers are recorded
            held.append(("record", getattr(_SS, "lock_held", lambda _r: "no lock_held")(rd)))
            return real_hr(rd)
        _SS.harness_record = _hr
        try:
            s_l = mk(d, T, st).run()
        finally:
            _SS.harness_record = real_hr
        ok(held and all(h == ("record", True) for h in held),
           f"T_OWNER_LEAD_LOCK the harness records the lead's provisional answers holding the sign-off lock, so an "
           f"owner sign that waited on it sees the request closed ({held})")
        prov = _SS.provisional(d)
        rep_l = open(os.path.join(d, "report.md")).read() if os.path.exists(os.path.join(d, "report.md")) else ""
        span = json.load(open(os.path.join(d, "report_render.json")))["writer_span"] if rep_l else [0, 0]
        ok(s_l == "completed" and not any(h.get("paused") for h in load_state(d).history)
           and "accepted by the lead, provisionally" in rep_l[:span[0]]
           and [(r["finding"], r["decision"], r["plan_changing"], r["review"]) for r in prov]
           == [("decision_test_changes_plan:DT9", "accept", True, None)]
           and "Provisional decisions that change the plan" in rep_l[:span[0]]
           and "## Provisional decisions" in rep_l[span[1]:],
           f"T_OWNER_LEAD_PROVISIONAL a lead run answers the owner decision provisionally and never blocks; the "
           f"plan-changing answer heads the report ({s_l}, {prov})")
        # T_HB2_FIRST_GATE (hb2 fix 1; malaria R1/R2, measles R1/R2, tb R1/R2 used all 3 rounds): with rounds left the
        # plan-changing test is asked at the first gate, once; accepted, the run writes in round 1, with no fix loop and
        # no second critique round, and ends accepted
        d1, T1, st1 = new_run("dt_lead_r3", {}, signoff="lead", max_rounds=3)
        s_r3 = mk(d1, T1, st1).run()
        h_r3 = [h for h in load_state(d1).history if "to" in h]
        to_r3 = [h["to"] for h in h_r3]
        g_r1 = json.load(open(os.path.join(d1, "gate_r1.json")))
        crit_r3 = sorted({c.rsplit(":", 1)[-1] for c in T1.calls if c.startswith("critique:critique-domain")})
        ok(s_r3 == "completed" and to_r3.count("OWNER_DECISION") == 1 and "FIX_LOOP" not in to_r3
           and crit_r3 == ["1"] and load_state(d1).round == 1
           and any(h["from"] == "GATE" and h["to"] == "OWNER_DECISION" and "rule 2o" in str(h.get("why")) for h in h_r3)
           and g_r1.get("action") == "ACCEPT",
           f"T_HB2_FIRST_GATE an owner-resolved HIGH is asked at the first gate, once, and the run ends in round 1 "
           f"({s_r3}, {to_r3[-8:]}, critique rounds {crit_r3}, gate r1 {g_r1.get('action')})")
        d2, T2, st2 = new_run("dt_owner_r3", {}, signoff="owner", max_rounds=3)
        mk(d2, T2, st2).run()
        toolkit.sign(d2, "owner", "ok")
        s_p = mk(d2, StubTasks(T2.s), None).run()
        st_p = load_state(d2)
        ok(s_p == "paused:awaiting_scope_signoff" and st_p.round == 1
           and "rule 2o" in str([h for h in st_p.history if "to" in h][-1].get("why")),
           f"T_HB2_FIRST_GATE_OWNER under --signoff owner the run pauses for the owner at round 1, not at round 3 "
           f"({s_p}, round {st_p.round})")
        _SS.sign(d2, "owner", all_decision="decline", note="the plan must not depend on this")
        s_d = mk(d2, StubTasks(T2.s), None).run()
        ok(s_d != "completed" and s_d.startswith(("completed_scope_declined", "run_failed")),
           f"T_HB2_DECLINED a declined plan-changing test never accepts ({s_d})")
        _SS.review(d, "decision_test_changes_plan:DT9", "overturn", note="fix the effect first")
        ok([r["review"]["decision"] for r in _SS.provisional(d)] == ["overturn"],
           "T_OWNER_LEAD_REVIEW the owner later overturns (or confirms) a provisional answer; it is recorded")
        # T_DT_TOOLKIT_FP (lead-gaps α): a toolkit finding's owner item binds the verified row of
        # analysis/decision_tests.yaml, so a changed result is asked again; a stale analysis.json in the run dir
        # never stands in for it (Codex r1 of #327)
        from toolkit.analysis import contract as _CTk
        _rec = _CTk.decision_test_record
        dtf_t = dict(dtf_e, source="toolkit/analysis/contract.py")
        try:
            with tempfile.TemporaryDirectory() as dk:
                os.makedirs(os.path.join(dk, os.path.dirname(AB.ANALYSIS_JSON)), exist_ok=True)
                json.dump({"decision_tests": [{"id": "DT9", "plan_changes": True, "text": "stale"}]},
                          open(os.path.join(dk, AB.ANALYSIS_JSON), "w"))
                _CTk.decision_test_record = lambda rd, k: {"test_id": k, "verdict": "CHANGES_PLAN", "share_pct": 40.0}
                fa = _bearing_item(dk, dtf_t)["fingerprint"]
                _CTk.decision_test_record = lambda rd, k: {"test_id": k, "verdict": "CHANGES_PLAN", "share_pct": 60.0}
                fb = _bearing_item(dk, dtf_t)["fingerprint"]
                _CTk.decision_test_record = lambda rd, k: None
                fc = _bearing_item(dk, dtf_t)["fingerprint"]
                fc2 = _bearing_item(dk, dtf_t)["fingerprint"]
        finally:
            _CTk.decision_test_record = _rec
        ok(len({fa, fb, fc}) == 3 and fc2 == fc,
           f"T_DT_TOOLKIT_FP the toolkit finding binds its own decision-test record ({fa}, {fb}, {fc})")
        # T_OWNER_LEAD_AMEND (1F, D2): a non-HIGH owner-amendment finding under --signoff lead is answered
        # provisionally (owner policy 2026-11-24: critic HIGH amendments pause for the owner; tested by T_LEAD_NO_CRITIC_HIGH)
        d, T, st = new_run("amend_lead", {"critics": {1: {"critique-domain": [dict(amend, id="D-041",
                                                                                   severity="MEDIUM")]}}},
                           signoff="lead", max_rounds=2)
        s_a = _ToolkitMachine(d, T, st, log=lambda *a: None, cross_engine_check=False).run()
        led_a = FL.load(d)
        ok(not s_a.startswith("paused") and led_a["D-041"].status == "owner_accepted"
           and [r["finding"] for r in _SS.provisional(d)] == ["D-041"]
           and not _SS.provisional(d)[0]["plan_changing"],
           f"T_OWNER_LEAD_AMEND an amendment finding under --signoff lead is accepted provisionally, not waited on "
           f"({s_a}, {led_a['D-041'].status})")

        # R3-4 (F6 run 3): a paid task whose allowance is below the floor is not started: the run pauses for the owner
        # (resumable), and a resume with a higher cap continues.
        d, T, st = new_run("floor", {}, budget=20.0)
        st.spent_usd = 20.0 - 10.0 - 0.13            # cap less the write-up reserve ($10) leaves $0.13
        save_state(d, st)
        s = _ToolkitMachine(d, T, st, log=lambda *a: None, cross_engine_check=False).run()
        ok(s == "paused:awaiting_budget" and T.calls == [] and load_state(d).state == "SPEC"
           and load_state(d).status is None,
           f"R3-4 a $0.13 allowance starts no task; the run pauses awaiting budget ({s}, {T.calls}, {T.allowances})")
        T2 = StubTasks(T.s)
        mp4 = os.path.join(d, "metadata.json")
        _mp = json.load(open(mp4))
        ok(_mp.get("status") == "paused:awaiting_budget" and "allowance" in str(_mp.get("paused_why")),
           f"PS3 the budget pause is recorded in metadata.json with the task's allowance ({_mp.get('paused_why')})")
        json.dump(dict(json.load(open(mp4)), invocations=[{"at": "2020-01-01T00:00:00"}, {"at": "2020-01-01T01:00:00"}]),
                  open(mp4, "w"))
        s2 = run_v2("toy question", d, T2, signoff="none", budget_usd=60.0, log=lambda *a: None,
                    backend=fast_toy, cross_engine_check=False)
        m4, h4 = json.load(open(mp4)), load_state(d).history
        ok(m4["invocations"][-1].get("budget_usd") == 60.0 and m4["invocations"][-1].get("cap_usd") == 60.0
           and "budget_usd" not in m4["invocations"][0] and set(m4.get("models") or {}) == {"deep", "fast"}
           and m4.get("sdk_version") and m4["invocations"][-1].get("models") == m4["models"]
           and sum(1 for h in h4 if h.get("paused") == AWAITING_BUDGET) == 1
           and m4.get("status") == s2 and (s2.startswith("paused:") or "paused_state" not in m4),
           f"RR4 each invocation records its budget, the cap in force, the model ids and the SDK version; the budget "
           f"pause has one entry ({m4['invocations'][-1]}, {m4.get('sdk_version')})")
        dp = os.path.join(td, "rr5_child")
        os.environ["MA_PARENT_RUN"] = d
        try:
            init_run(dp, "toy question", derived_inputs=False)
        finally:
            os.environ.pop("MA_PARENT_RUN", None)
        pr = json.load(open(os.path.join(dp, "metadata.json"))).get("parent_run") or {}
        ok(pr.get("run_dir") == os.path.basename(d) and pr.get("run_state_sha256") == hashlib.sha256(
            open(os.path.join(d, STATE_FILE), "rb").read()).hexdigest(),
           f"RR5 a run launched with MA_PARENT_RUN records its parent and the parent's run_state hash ({pr})")
        mb5 = json.load(open(os.path.join(dp, "metadata.json"))).get("motivated_by") or {}
        dq = os.path.join(td, "rr5_parent_rq")
        os.makedirs(dq)
        _write_json(dq, SPEC_RETHINK_REQUEST, {"round": 1, "findings": [{"id": "X-404"}], "briefs": []})
        mq = _relaunch_motivation(dq)
        open(os.path.join(dq, SPEC_RETHINK_REQUEST), "w").write("[]")
        mz = _relaunch_motivation(dq)
        ok(mb5.get("parent_run") == os.path.basename(d) and mb5.get("source") == (
            "rethink_request" if os.path.isfile(os.path.join(d, SPEC_RETHINK_REQUEST)) else "unstated")
           and mq.get("source") == "rethink_request" and [f.get("id") for f in mq.get("findings") or []] == ["X-404"]
           and mz.get("source") == "unreadable",
           f"RR5b a relaunch records motivated_by: the parent's rethink request's findings, else unstated; a malformed "
           f"request is unreadable ({mb5}, {mq}, {mz})")
        cwd6 = os.getcwd()
        prs = []
        for k6, par in enumerate((os.path.join(td, "no_such_run"), ".")):
            os.chdir(d)
            os.environ["MA_PARENT_RUN"] = par
            try:
                init_run(os.path.join(td, f"rr6_child{k6}"), "toy question", derived_inputs=False)
            finally:
                os.environ.pop("MA_PARENT_RUN", None)
                os.chdir(cwd6)
            prs.append(json.load(open(os.path.join(td, f"rr6_child{k6}", "metadata.json"))).get("parent_run") or {})
        mb6 = json.load(open(os.path.join(td, "rr6_child0", "metadata.json"))).get("motivated_by") or {}
        ok(prs[0].get("run_state_sha256") is None and prs[0].get("error") and prs[1].get("run_dir") == os.path.basename(d)
           and mb6.get("source") == "unreadable",
           f"RR6 an unreadable parent records no hash (never linked); a relative parent path records its dir name ({prs})")
        st7 = load_state(d)
        if st7.state == "FINAL":
            st7.paused = "awaiting_scope_signoff"            # a stale flag a terminal run can carry
            save_state(d, st7)
            n7 = len(load_state(d).history)
            _ToolkitMachine(d, StubTasks(T.s), st7, log=lambda *a: None, cross_engine_check=False).run()
            ok(len(load_state(d).history) == n7, "RR7 a FINAL run carrying a stale paused flag writes no pause entry")
        else:
            ok(False, f"RR7 fixture: the resumed run should be FINAL ({st7.state})")
        ok(not s2.startswith("paused") and "draft_spec" in T2.calls
           and load_state(d).budget_usd == 60.0
           and any(h.get("budget_raised") for h in load_state(d).history),
           f"R3-4b resumed with a higher cap, the run continues ({s2}, cap {load_state(d).budget_usd})")

        # R3-8 (fix 4 of Codex round 3 on #258): the decline override reads the ledger synced with this gate's decision,
        # so a gate-supplied finding the owner declined, and that this gate states with a revised claim, is not held by
        # the old decline: the answer was given to the old claim, so the finding is open again
        import scope_signoff as _SS8
        d, T, st = new_run("stale_decline", {})
        st.state = "GATE"
        save_state(d, st)
        v8 = {"kind": "k_demo", "key": "x", "severity": "HIGH", "stage": "ANALYZE", "scope_declarable": True,
              "claim": "the new claim", "fix_requires": "use the new data"}
        fid8 = FL.gate_id(v8)
        old = FL.Finding(id=fid8, kind="k_demo", key="x", severity="HIGH", source="rigor-artifacts", claim="the old claim",
                         fix_requires="use the new data", status="owner_declined", resolution_type="owner_decision")
        old.events.append({"round": 1, "status": "owner_declined", "by": "owner"})
        FL.save(d, {fid8: old})
        fp8 = FL.answer_fingerprint(fid8, kind="k_demo", fix_requires="use the new data", claim="the old claim")
        json.dump({"answers": {fp8: {"finding": fid8, "decision": "decline", "by": "owner"}}},
                  open(os.path.join(d, SCOPE_SIGNOFF), "w"))
        _SS8.harness_record(d)
        _mod8 = sys.modules[Machine.__module__]
        _vg = _mod8.v2_gate
        _mod8.v2_gate = lambda *a, **k: {"action": "DECLARE_SCOPE", "rule_matched": "3", "rounds_remaining": 0,
                                         "rigor_violations": [v8], "unresolved_high": [dict(v8)]}
        try:
            _ToolkitMachine(d, T, None, log=lambda *a: None, cross_engine_check=False).step()
        finally:
            _mod8.v2_gate = _vg
        g8 = json.load(open(os.path.join(d, "gate_r1.json")))
        f8 = FL.load(d)[fid8]
        ok(g8.get("action") == "DECLARE_SCOPE" and g8.get("rule_matched") != "owner_declined" and f8.status == "open"
           and f8.claim == "the new claim",
           f"R3-8 a declined gate finding restated with a revised claim is not held by the old decline "
           f"({g8.get('rule_matched')}, {FL.load(d)[fid8].status})")

        # T29: the deliverable destination must be a plain file inside the run, not one the harness owns.
        bad_paths = {}
        for name, path in (("escape", "../escape.csv"), ("owned", "run_state.json"), ("report", "report.md"),
                           ("link", "out/link.csv")):
            d, T, st = new_run(f"deliv_{name}", {"spec_extra": {"deliverable_file": {
                "path": path, "columns": {"unit": "id", "package": "package"}}}}, max_rounds=1)
            if name == "link":
                os.makedirs(os.path.join(td, "elsewhere"), exist_ok=True)
                os.symlink(os.path.join(td, "elsewhere"), os.path.join(d, "out"))
            s = _ToolkitMachine(d, T, st, log=lambda *a: None, cross_engine_check=False).run()
            led = FL.load(d)
            bad_paths[name] = ("deliverable_path_invalid:deliverable" in led and s == "run_failed"
                               and not os.path.exists(os.path.join(td, "escape.csv"))
                               and not os.path.exists(os.path.join(td, "elsewhere", "link.csv")))
        ok(all(bad_paths.values()), f"T29 a deliverable outside the run, owned or through a symlink is refused {bad_paths}")

        # T30: a measured decision test that changes the plan cannot be declared without the owner rule.
        import validate_critique_yaml as V
        dd = os.path.join(td, "dt")
        os.makedirs(dd)
        for r in CRITICS:
            __import__("yaml").safe_dump({"reviewer": r, "round": 2, "verdict": "PASS", "structural_mismatch": {"detected": False},
                                          "carried_forward": [], "blockers": []}, open(os.path.join(dd, V.FILENAMES[r]), "w"))
        dtf = {"kind": "decision_test_changes_plan", "severity": "HIGH", "stage": "ANALYZE", "key": "DT1",
               "claim": "DT1 changes the plan", "scope_declarable": True, "decision_bearing": True}
        g = v2_gate(dd, 2, 2, False, [dict(dtf)], history=False)
        own = [b for b in list(g.get("unresolved_high") or []) + list(g.get("accept_blockers") or [])
               if isinstance(b, dict) and b.get("owner_signoff")]
        ok(OWNER_LIFECYCLE_WIRED and _owner_rule() is not None and g["action"] == "AWAIT_OWNER" and own
           and (own[0].get("owner_item") or {}).get("fingerprint"),
           f"T30 (1F) a measured decision test that changes the plan goes to the owner: AWAIT_OWNER with a "
           f"fingerprinted item ({g['action']})")
        gn = v2_gate(dd, 2, 2, False, [dict(dtf)], history=False, signoff="none")
        ok(gn["action"] == "RUN_FAILED", f"T30b under --signoff none nobody can accept it: not declarable ({gn['action']})")
        # T_OWNER_ANSWERED (1F): the harness-recorded answer to that item releases it; whose answer counts follows the
        # run's sign-off mode (a lead's only under --signoff lead, D2)
        import scope_signoff as _ss
        _ss.record(dd, g, 2)
        _ss.sign(dd, "lead", all_decision="accept", note="t")          # not provisional: counts nowhere
        _ss.harness_record(dd)
        g_ln = v2_gate(dd, 2, 2, False, [dict(dtf)], history=False, signoff="lead")
        _ss.provisional_answers(dd, json.load(open(os.path.join(dd, _ss.REQUEST)))["items"], 2)
        _ss.harness_record(dd)
        g_lo = v2_gate(dd, 2, 2, False, [dict(dtf)], history=False)
        g_ll = v2_gate(dd, 2, 2, False, [dict(dtf)], history=False, signoff="lead")
        _ss.sign(dd, "owner", all_decision="decline", note="t")
        _ss.harness_record(dd)
        g_od = v2_gate(dd, 2, 2, False, [dict(dtf)], history=False)
        # owner decision (b), 2026-11-23: an accepted plan-changing test lets the gate ACCEPT (it was DECLARE_SCOPE)
        ok(g_ln["action"] == "AWAIT_OWNER" and g_lo["action"] == "AWAIT_OWNER" and g_ll["action"] == "ACCEPT"
           and [b.get("kind") for b in g_ll.get("owner_accepted_high") or []] == ["decision_test_changes_plan"]
           and g_od["action"] == "RUN_FAILED"
           and any(isinstance(u, dict) and u.get("kind") == "scope_declaration_declined"
                   for u in g_od.get("unresolved_high") or []),
           f"T_OWNER_ANSWERED a lead's accept counts only under --signoff lead; the owner's decline holds the gate "
           f"({g_lo['action']}, {g_ll['action']}, {g_od['action']})")
        # T_INTERVAL (hb3 fix 5, lead on #377): a degenerate headline interval is an owner item: unanswered the run
        # waits; accepted (the lead, provisionally, under --signoff lead) it is declared: DECLARE_SCOPE, not RUN_FAILED
        di = os.path.join(td, "interval")
        os.makedirs(di)
        for r in CRITICS:
            __import__("yaml").safe_dump({"reviewer": r, "round": 2, "verdict": "PASS", "structural_mismatch": {"detected": False},
                                          "carried_forward": [], "blockers": []}, open(os.path.join(di, V.FILENAMES[r]), "w"))
        from toolkit.analysis import contract as _CTi
        ivf = _CTi.interval_findings({"headline": {"name": "total_x", "outcome": "x"}, "fixed_plan": {
            "x": {"ci_low": 999.9995, "median": 1000.0, "ci_high": 1000.0005, "level": 0.95}}})[0]
        gi = v2_gate(di, 2, 2, False, [dict(ivf)], history=False)
        _ss.record(di, gi, 2)
        _ss.provisional_answers(di, json.load(open(os.path.join(di, _ss.REQUEST)))["items"], 2)
        _ss.harness_record(di)
        gi_l = v2_gate(di, 2, 2, False, [dict(ivf)], history=False, signoff="lead")
        gi_n = v2_gate(di, 2, 2, False, [dict(ivf)], history=False, signoff="none")
        ok(gi["action"] == "AWAIT_OWNER" and gi_l["action"] == "DECLARE_SCOPE" and gi_n["action"] == "RUN_FAILED",
           f"T_INTERVAL a degenerate headline interval goes to the owner; declared once accepted, not a run failure "
           f"({gi['action']}, {gi_l['action']}, {gi_n['action']})")
        # T_OWNER_DECLINE_HOLDS and T_OWNER_LEAD_NONPROV (Codex r2 of #316, P1-2 and P1-3): with a policy-mapped
        # finding beside the decision test, the owner's decline on it keeps holding the gate once the decision-test
        # finding clears; and a lead answer that is not provisional never releases it, even in a lead run
        for f_ in (_ss.SIGNOFF, _ss.LEDGER, _ss.REQUEST, "trace.jsonl"):
            if os.path.exists(os.path.join(dd, f_)):
                os.remove(os.path.join(dd, f_))
        open(os.path.join(dd, "decision_rule.md"), "w").write("rule\n")          # the run allocates
        alloc = {"kind": "allocation_unstable", "severity": "HIGH", "stage": "OPTIMIZATION",
                 "key": "allocation_robustness", "claim": "UNSTABLE", "scope_declarable": True}
        g2 = v2_gate(dd, 2, 2, False, [dict(dtf), dict(alloc)], history=False)
        _ss.record(dd, g2, 2)
        its = {i["kind"]: i for i in json.load(open(os.path.join(dd, _ss.REQUEST)))["items"]}
        both = "decision_test_changes_plan" in its and "allocation_unstable" in its
        if both:
            _ss.sign(dd, "owner", {its["decision_test_changes_plan"]["fingerprint"]: "accept",
                                   its["allocation_unstable"]["fingerprint"]: "decline"}, note="t")
            _ss.harness_record(dd)
        g_both = v2_gate(dd, 2, 2, False, [dict(dtf), dict(alloc)], history=False)
        g_alone = v2_gate(dd, 2, 2, False, [dict(alloc)], history=False)
        ok(both and g2["action"] == "AWAIT_OWNER" and g_both["action"] == "RUN_FAILED"
           and g_alone["action"] == "RUN_FAILED",
           f"T_OWNER_DECLINE_HOLDS the owner's decline of a policy-mapped finding holds the gate when the decision-test "
           f"finding clears ({g2['action']}, {g_both['action']}, {g_alone['action']}, {sorted(its)})")
        if both:
            doc_ = json.load(open(os.path.join(dd, _ss.SIGNOFF)))
            doc_["answers"][its["allocation_unstable"]["fingerprint"]].update(decision="accept", by="lead")
            json.dump(doc_, open(os.path.join(dd, _ss.SIGNOFF), "w"))
            _ss.harness_record(dd)
        g_np = v2_gate(dd, 2, 2, False, [dict(dtf), dict(alloc)], history=False, signoff="lead")
        ok(both and g_np["action"] != "DECLARE_SCOPE" and _ss.provisional(dd) == [],
           f"T_OWNER_LEAD_NONPROV a lead answer that is not provisional does not release a policy-mapped item, even "
           f"under --signoff lead ({g_np['action']})")
        os.remove(os.path.join(dd, "decision_rule.md"))
        for f_ in (_ss.SIGNOFF, _ss.LEDGER, _ss.REQUEST, "trace.jsonl"):
            if os.path.exists(os.path.join(dd, f_)):
                os.remove(os.path.join(dd, f_))
        seen = {}

        class Rule:
            @staticmethod
            def gate_findings(rd, rn, viols, non_declarable=None, mode=None, **k):
                seen["marked"] = [v.get("owner_signoff") for v in viols if v.get("kind") == "decision_test_changes_plan"]
                return []
        vs = [dict(dtf)]
        apply_owner_rule(dd, 2, vs, rule=Rule)
        ok(seen.get("marked") == [True] and vs[0].get("owner_item"), "T31 with the owner rule the finding goes to it, marked for the owner")

        class Broken:
            @staticmethod
            def gate_findings(*a, **k):
                raise RuntimeError("x")
        vs = [dict(dtf)]
        apply_owner_rule(dd, 2, vs, rule=Broken)
        ok(vs[0]["scope_declarable"] is False and not vs[0].get("owner_signoff"), "T32 an owner rule that fails is fail-closed")

        class Declines:
            @staticmethod
            def gate_findings(rd, rn, viols, non_declarable=None, mode=None, **k):
                for v in viols:
                    v["owner_signoff"] = True
                return [{"kind": "scope_declaration_declined", "key": "allocation_unstable"}, dict(viols[0])]
        vs = [{"kind": "allocation_unstable", "severity": "HIGH", "claim": "c"}]
        got = apply_owner_rule(dd, 2, vs, rule=Declines)
        ok([f["kind"] for f in got] == ["scope_declaration_declined"] and not vs[0].get("owner_signoff")
           and apply_owner_rule(dd, 2, [dict(vs[0])], rule=Declines, mode="none") == [],
           "T32b (Codex r2 of #316) a recorded decline holds the gate without a decision-bearing finding; nothing new "
           "is put to the owner there")

        # T33 (Codex R2-1): the first spec draft is interrupted. On resume the started-but-uncompleted
        # entry is charged its allowance once and the draft runs again under the same key.
        d, T, st = new_run("spec_crash", {"crash": {"draft_spec": 1}, "usd": 1.0}, budget=100.0)
        try:
            _ToolkitMachine(d, T, st, log=lambda *a: None, cross_engine_check=False).run()
        except RuntimeError:
            pass
        T2 = StubTasks(dict(T.s, crash={}))
        m2 = _ToolkitMachine(d, T2, None, log=lambda *a: None, cross_engine_check=False)
        s = m2.run()
        j = load_state(d).journal
        spec_keys = sorted(k for k in j if k.startswith("SPEC#"))
        ok(s == "completed" and spec_keys == ["SPEC#0:try1"] and j["SPEC#0:try1"].get("interrupted_charged") == 1
           and j["SPEC#0:try1"]["status"] == "completed",
           f"T33 an interrupted spec draft is charged once and re-run under its own key {spec_keys}")
        d, T, st = new_run("orphan", {}, budget=100.0)
        st.journal["X#0:orphan"] = {"task": "critique", "status": "started", "allowance_usd": 2.5}
        save_state(d, st)
        m3 = _ToolkitMachine(d, StubTasks({}), None, log=lambda *a: None)
        ok(abs(m3.st.spent_usd - 2.5) < 1e-9 and m3.st.journal["X#0:orphan"]["status"] == "interrupted"
           and abs(_ToolkitMachine(d, StubTasks({}), None, log=lambda *a: None).st.spent_usd - 2.5) < 1e-9,
           f"T34 on resume every started-but-uncompleted entry is charged once ({m3.st.spent_usd})")

        # T35 (Codex R2-5): case aliases of owned files are refused (case-insensitive filesystems).
        alias = {}
        for name, path in (("state", "RUN_STATE.json"), ("report", "Report.MD"), ("spec", "Decision_Spec.yaml"),
                           ("analysis", "Analysis/x.csv")):
            dd2 = os.path.join(td, f"alias_{name}")
            os.makedirs(dd2)
            __import__("yaml").safe_dump({"deliverable_file": {"path": path, "columns": {"a": "b"}}},
                                         open(os.path.join(dd2, AB.SPEC_FILE), "w"))
            alias[name] = bool(deliverable_problems(dd2))
        os.makedirs(os.path.join(td, "alias_link"))
        __import__("yaml").safe_dump({"deliverable_file": {"path": "out.csv", "columns": {"a": "b"}}},
                                     open(os.path.join(td, "alias_link", AB.SPEC_FILE), "w"))
        os.link(os.path.join(td, "alias_link", AB.SPEC_FILE), os.path.join(td, "alias_link", "out.csv"))
        alias["hardlink"] = bool(deliverable_problems(os.path.join(td, "alias_link")))
        ok(all(alias.values()), f"T35 case aliases and a hard link to an owned file are refused {alias}")

        # T36: the default backend. Once workstream B's analysis engine is on the branch, the
        # machine runs on it by default: B's toy spec with engine: auto, B's sign-off, B's step.
        if type(AB.backend()).__name__ == "_ToolkitApi":
            import yaml
            from toolkit.spec import schema
            src = os.path.join(td, "b_src")
            os.makedirs(src)
            raw = yaml.safe_load(open(schema.toy_spec(src)))
            raw["engine"] = "auto"
            for k in ("draws", "seed"):
                (raw.get("uncertainty") or {}).pop(k, None)
            em = (raw.get("analysis") or {}).get("emulator") or {}
            pts, eseed = em.pop("points", 24), em.pop("seed", 5)
            (raw.get("analysis") or {}).pop("workers", None)
            d = os.path.join(td, "b_run")
            os.makedirs(d)
            for f in os.listdir(src):
                if f != AB.SPEC_FILE and os.path.isfile(os.path.join(src, f)):
                    shutil.copy(os.path.join(src, f), d)
            bl = {"id": "D-001", "severity": "HIGH", "target_stage": "ANALYZE", "claim": "too few draws",
                  "done_when": [{"file": "analysis_settings.yaml", "key": "uncertainty.draws", "op": ">=", "value": 12}]}
            T = StubTasks({"spec": raw, "critics": {1: {"critique-domain": [bl]}},
                           "settings": {"uncertainty": {"draws": 6, "seed": 1},
                                        "analysis": {"workers": 2, "emulator": {"points": pts, "seed": eseed}}},
                           "fixes": {"D-001": [("analysis_settings.yaml", "uncertainty.draws", 12)]}})
            st = init_run(d, "toy question", 3, "lead", "v2", None, derived_inputs=False)   # pre-DI fixture
            s = Machine(d, T, st, log=lambda *a: None, cross_engine_check=False).run()
            rows = FX.impact_rows(d)
            man = os.path.exists(os.path.join(d, "analysis", "manifest.json"))
            # lead-gaps α: the toy's automatic decision tests change the plan, so the toolkit backend now emits
            # decision_test_changes_plan for each; this --signoff lead run answers them provisionally (never pausing) and
            # is declared, not accepted, with each flagged above the draft. Everything else T36 checked is unchanged.
            # Owner decision (b), 2026-11-23 (hb2 fix 1): the lead's provisional accept lets it end accepted, with the
            # dependence stated above the draft (it ended completed_scope_declared before)
            # the expected ids are read from the artifact itself, not through the reader under test (Codex r1 of #327)
            _dt36 = (yaml.safe_load(open(os.path.join(d, "analysis", "decision_tests.yaml"))) or {}).get("data") or {}
            want = sorted(f"decision_test_changes_plan:{t.get('test_id')}" for t in _dt36.get("tests") or []
                          if t.get("verdict") == "CHANGES_PLAN")
            import scope_signoff as _SS36
            # (asked again after the fix moved their evidence: one finding may carry two answers)
            prov36 = sorted({r["finding"] for r in _SS36.provisional(d) if r.get("plan_changing")})
            rep36 = open(os.path.join(d, "report.md")).read() if os.path.exists(os.path.join(d, "report.md")) else ""
            sp36 = json.load(open(os.path.join(d, "report_render.json")))["writer_span"] if rep36 else [0, 0]
            # hb3 fix 4 (#375): a test the spec declares that did not run (the toy's DT1 has no benchmark results here)
            # is a declarable HIGH, so the run is declared, not accepted, and the ledger holds it; read from the artifact
            _decl36 = {str(t.get("id")) for t in (raw.get("analysis") or {}).get("decision_tests") or []}
            nr36 = sorted(f"decision_test_not_run:{t.get('test_id')}" for t in _dt36.get("tests") or []
                          if t.get("status") == "not_run" and str(t.get("test_id")) in _decl36)
            led36 = FL.load(d)
            exp36 = "completed_scope_declared" if nr36 else "completed"
            # Codex r1 of #375 P2-5 (seen here): no fix loop with nothing to do (no task, no owner amendment, no
            # write-up) runs: an accepted not-run test is declared at the gate, not sent to an empty fix loop
            hist36 = load_state(d).history
            empty36 = [h["fix_loop"].get("round") for h in hist36 if isinstance(h.get("fix_loop"), dict)
                       and not h["fix_loop"].get("tasks") and not h["fix_loop"].get("owner_amendments")
                       and not h["fix_loop"].get("write_up")]
            c36 = {"status": s == exp36, "manifest": bool(man), "fix_row": bool(rows) and rows[0]["files_changed"] == [
                       "analysis_settings.yaml"],
                   "dependence_top": bool(nr36) or "The plan depends on inputs the lead accepted provisionally"
                   in rep36[:sp36[0]],
                   "not_run_in_ledger": all(i in led36 for i in nr36), "no_empty_fix_loop": empty36 == [],
                   "d001_fixed": led36["D-001"].status == "fixed", "provisional": prov36 == want,
                   "never_paused": not any(h.get("paused") for h in hist36),
                   "flagged_top": all(w in rep36[:sp36[0]] for w in want)}
            bad36 = [k for k, v in c36.items() if not v]
            ok(want and not bad36,
               f"T36 the default backend (workstream B) runs the machine end to end with engine: auto; each decision "
               f"test that changes the plan is answered provisionally and flagged above the draft ({s}, {want}, {prov36}, "
               f"not run: {nr36}, failing: {bad36}, empty fix loops: {empty36}"
               + ("" if not bad36 else   # why it ended: CI showed only the status (flaky once on #156 and #160)
                  f"; path {[h.get('to') for h in hist36 if 'to' in h]}; "
                  f"final {[h.get('why') for h in hist36 if h.get('to') == 'FINAL'][-1:]}; "
                  f"D-001 {led36['D-001'].status}") + ")")
        else:
            ok(True, "T36 skipped: workstream B's analysis engine is not on this branch")

        # T37 (first real run, v2blind1): critics wrote an invalid category three times because the
        # repair re-ran them without the schema errors. The repair now re-runs only the critics whose
        # YAML failed, and gives each its errors.
        class Sloppy(StubTasks):
            def critique(self, run_dir, reviewer, round_n, feedback=None):
                res = super().critique(run_dir, reviewer, round_n)
                if reviewer == "critique-domain" and not feedback:
                    import yaml
                    p = os.path.join(run_dir, "critique_domain.yaml")
                    doc = yaml.safe_load(open(p))
                    doc["blockers"] = [{"id": "D-001", "severity": "HIGH", "category": "DOMAIN", "target_stage": "ANALYZE",
                                        "first_seen_round": round_n, "claim": "c", "evidence": "e", "fix_requires": "f",
                                        "resolved": False}]
                    doc["verdict"] = "REVISE"
                    yaml.safe_dump(doc, open(p, "w"))
                self.calls.append(f"feedback:{reviewer}:{bool(feedback)}")
                return res
        d, T, st = new_run("schema_repair", {})
        T = Sloppy(T.s)
        s = _ToolkitMachine(d, T, st, log=lambda *a: None, cross_engine_check=False).run()
        ok(s in ("completed", "completed_scope_declared") and "feedback:critique-domain:True" in T.calls
           and T.calls.count("critique:critique-redteam:1") == 1,
           f"T37 a schema repair re-runs only the failing critic, with its errors ({s}, {[c for c in T.calls if 'critique' in c][:6]})")

        # T37b: invalid YAML in critique_presentation.yaml ("invalid YAML in /.../critique_presentation.yaml: ...")
        # routes directly back to CRITIQUE with its errors, never re-runs domain/redteam, and finishes cleanly.
        class BadPresYaml(StubTasks):
            def critique(self, run_dir, reviewer, round_n, feedback=None):
                res = super().critique(run_dir, reviewer, round_n)
                if reviewer == "critique-presentation" and not feedback:
                    p = os.path.join(run_dir, "critique_presentation.yaml")
                    open(p, "w").write("reviewer: critique-presentation\nround: 1\nevidence: a (events: bad)\n")
                self.calls.append(f"feedback:{reviewer}:{bool(feedback)}")
                return res
        d_p, T_p, st_p = new_run("pres_yaml_repair", {})
        T_p = BadPresYaml(T_p.s)
        s_p = _ToolkitMachine(d_p, T_p, st_p, log=lambda *a: None, cross_engine_check=False).run()
        hist_p = load_state(d_p).history
        pres_route = any(h.get("from") == "GATE" and h.get("to") == "CRITIQUE" for h in hist_p)
        ok(s_p in ("completed", "completed_scope_declared") and pres_route
           and "feedback:critique-presentation:True" in T_p.calls
           and T_p.calls.count("critique:critique-domain:1") == 1
           and T_p.calls.count("critique:critique-redteam:1") == 1
           and os.path.exists(os.path.join(d_p, "report.md")),
           f"T37b invalid presentation YAML routes to CRITIQUE with errors and keeps report.md "
           f"({s_p}, pres_route={pres_route}, calls={[c for c in T_p.calls if 'critique' in c or 'feedback' in c]})")
        # T37c: a presentation schema error whose body mentions another critique filename (e.g. reviewer: critique_domain.yaml)
        # still routes to CRITIQUE via schema_errors_by_reviewer, never re-running critique-domain.
        class BadPresReviewer(StubTasks):
            def critique(self, run_dir, reviewer, round_n, feedback=None):
                res = super().critique(run_dir, reviewer, round_n)
                if reviewer == "critique-presentation" and not feedback:
                    import yaml
                    p = os.path.join(run_dir, "critique_presentation.yaml")
                    doc = yaml.safe_load(open(p))
                    doc["reviewer"] = "critique_domain.yaml"
                    yaml.safe_dump(doc, open(p, "w"))
                self.calls.append(f"feedback:{reviewer}:{bool(feedback)}")
                return res
        d_p2, T_p2, st_p2 = new_run("pres_reviewer_repair", {})
        T_p2 = BadPresReviewer(T_p2.s)
        s_p2 = _ToolkitMachine(d_p2, T_p2, st_p2, log=lambda *a: None, cross_engine_check=False).run()
        hist_p2 = load_state(d_p2).history
        ok(s_p2 in ("completed", "completed_scope_declared")
           and any(h.get("from") == "GATE" and h.get("to") == "CRITIQUE" for h in hist_p2)
           and "feedback:critique-presentation:True" in T_p2.calls
           and T_p2.calls.count("critique:critique-domain:1") == 1,
           f"T37c presentation error naming critique_domain.yaml routes to CRITIQUE ({s_p2})")

        # T74-T75 (two v2 eval runs, 2026-11-05, both completed_gate_error with no verdict): in round 2
        # the critics marked blockers resolved without resolved_in_round or resolved_evidence and wrote
        # carried_forward as a list of ids. The gate named one error per file, the critic fixed exactly
        # that one, the next surfaced, and the two repairs ran out. The gate now names every error in
        # a file at once, so one repair carries all of them.
        import yaml
        r2 = os.path.join(td, "resolved_shape")
        os.makedirs(r2)
        resolved_b = {"id": "D-001", "severity": "MEDIUM", "category": "HYPOTHESES", "target_stage": "WRITE",
                      "first_seen_round": 1, "claim": "c", "evidence": "e", "fix_requires": "f (done)",
                      "resolved": True, "done_when": [{"file": "manuscript.md", "contains": "draft"}]}
        yaml.safe_dump({"reviewer": "critique-domain", "round": 2, "verdict": "PASS",
                        "structural_mismatch": {"detected": False}, "carried_forward": ["D-001", "D-002"],
                        "blockers": [resolved_b, dict(resolved_b, id="D-002")]},
                       open(os.path.join(r2, "critique_domain.yaml"), "w"), sort_keys=False)
        g = v2_gate(r2, 2, 3, False, [], history=False, strict_done_when=True)
        dom = [e for e in g.get("schema_errors") or [] if e.startswith("critique_domain.yaml")]
        want = ("D-001 missing 'resolved_in_round'", "D-001 missing 'resolved_evidence'",
                "D-002 missing 'resolved_in_round'", "D-002 missing 'resolved_evidence'",
                "carried_forward entry must be a mapping")
        ok(g.get("action") is None and all(any(w in e for e in dom) for w in want),
           f"T74 the gate names every schema error in a critic's file at once, not the first ({dom})")

        # T76: a refused file's open HIGH/MEDIUM blockers still have their done_when tests checked, so
        # a prose test reaches the same repair as the file's schema errors.
        r3 = os.path.join(td, "refused_done_when")
        os.makedirs(r3)
        yaml.safe_dump({"reviewer": "critique-domain", "round": 2, "verdict": "REVISE",
                        "structural_mismatch": {"detected": False}, "carried_forward": ["D-001"],
                        "blockers": [dict(resolved_b, resolved=False, done_when="the text says so")]},
                       open(os.path.join(r3, "critique_domain.yaml"), "w"), sort_keys=False)
        g = v2_gate(r3, 2, 3, False, [], history=False, strict_done_when=True)
        dom = [e for e in g.get("schema_errors") or [] if e.startswith("critique_domain.yaml")]
        ok(g.get("action") is None and any("carried_forward entry must be a mapping" in e for e in dom)
           and any("blocker D-001 done_when" in e for e in dom),
           f"T76 a refused file's schema errors and its open blockers' done_when problems reach one repair ({dom})")
        yaml.safe_dump({"reviewer": "critique-domain", "round": 2, "verdict": "REVISE",
                        "structural_mismatch": {"detected": False}, "carried_forward": ["D-001"],
                        "blockers": [dict(resolved_b, resolved=False,
                                          done_when=[{"file": "a.md", "contains": "a{4294967296}"}])]},
                       open(os.path.join(r3, "critique_domain.yaml"), "w"), sort_keys=False)
        try:
            g = v2_gate(r3, 2, 3, False, [], history=False, strict_done_when=True)
        except Exception as e:                                # noqa: BLE001 - the test is that it does not raise
            g = {"crash": repr(e)}
        dom = [e for e in g.get("schema_errors") or [] if e.startswith("critique_domain.yaml")]
        ok(g.get("action") is None and any("carried_forward" in e for e in dom)
           and any("D-001 done_when" in e for e in dom),
           f"T76b a done_when the checker cannot evaluate is a schema error, not a crash ({g.get('crash') or dom})")

        class Literal(StubTasks):
            """A critic that, like the real ones, fixes exactly the errors it is told and nothing else."""
            def critique(self, run_dir, reviewer, round_n, feedback=None):
                p = os.path.join(run_dir, "critique_domain.yaml")
                if reviewer != "critique-domain" or round_n < 2:
                    return super().critique(run_dir, reviewer, round_n, feedback)
                if not feedback:
                    res = super().critique(run_dir, reviewer, round_n)
                    doc = yaml.safe_load(open(p))
                    for b in doc["blockers"]:
                        b.pop("resolved_in_round", None)
                        b.pop("resolved_evidence", None)
                    doc["carried_forward"] = [c["id"] for c in doc["carried_forward"]]
                    yaml.safe_dump(doc, open(p, "w"))
                    return res
                self.calls.append(f"repair:{len(feedback)}")
                doc = yaml.safe_load(open(p))
                for e in feedback:
                    for b in doc["blockers"]:
                        for key in ("resolved_in_round", "resolved_evidence"):
                            if f"blocker {b['id']} missing '{key}'" in e:
                                b[key] = round_n if key == "resolved_in_round" else "done_when passes"
                    if "carried_forward entry must be a mapping" in e:
                        doc["carried_forward"] = [{"id": i, "prior_round": round_n - 1, "still_present": False,
                                                   "notes": "fixed"} for i in doc["carried_forward"]]
                yaml.safe_dump(doc, open(p, "w"))
                return self._r(f"critique:{reviewer}:{round_n}")
        d, T, st = new_run("resolved_repair", {"critics": {1: {"critique-domain": [blocker]}},
                                               "fixes": {"D-001": [("analysis_settings.yaml", "uncertainty.draws", 32)]}})
        T = Literal(T.s)
        s = _ToolkitMachine(d, T, st, log=lambda *a: None, cross_engine_check=False).run()
        ok(s in ("completed", "completed_scope_declared") and "repair:3" in T.calls,
           f"T75 a round-2 critic that resolved a blocker in the wrong shape is repaired in one pass and the "
           f"gate decides ({s}, {[c for c in T.calls if 'repair' in c]})")

        # T38-T41: from the first blind real run (v2blind1). Fixtures: its three critique YAMLs,
        # final_gate.json and claims ledger (tests/fixtures/v2blind1; no question text, no evals content).
        import yaml
        FX_DIR = os.path.join(REPO, "tests", "fixtures", "v2blind1")
        fb = os.path.join(td, "blind1")
        os.makedirs(fb)
        for f in ("critique_domain.yaml", "critique_redteam.yaml", "critique_presentation.yaml"):
            shutil.copy(os.path.join(FX_DIR, f), fb)
        open(os.path.join(fb, "manuscript.md"), "w").write("draft\n")
        g = v2_gate(fb, 1, 2, True, [], history=False, strict_done_when=True)
        errs = " ".join(g.get("schema_errors") or [])
        ok(g.get("action") is None and "critique_domain.yaml: blocker D-001 done_when" in errs
           and "critique_presentation.yaml: blocker P-001 done_when" in errs,
           f"T38 a prose done_when is a schema error the critic is asked to repair ({errs[:160]})")
        g = v2_gate(fb, 1, 2, True, [], history=False, strict_done_when=False)
        ok(g.get("action") == "ACCEPT", f"T38b after the repairs the gate itself still accepts ({g.get('action')})")
        # T39: an open MEDIUM with a valid test goes to the fix loop while rounds remain; else it is declared.
        for f in ("critique_domain.yaml", "critique_redteam.yaml", "critique_presentation.yaml"):
            doc = yaml.safe_load(open(os.path.join(fb, f)))
            for b in doc["blockers"]:
                b["done_when"] = [{"judgement": str(b.get("done_when"))[:200]}]
            yaml.safe_dump(doc, open(os.path.join(fb, f), "w"))
        g = v2_gate(fb, 1, 2, True, [], history=False, strict_done_when=True)
        opened = open_findings(fb, g)
        ids = sorted(f["id"] for f in opened if f["tested"])
        ok("D-001" in ids and "P-001" in ids and "R-001" in ids and len(ids) >= 8,
           f"T39 the run's open MEDIUM critic findings are found, with their tests ({ids})")
        a = apply_medium_rule(dict(g), opened, rounds_remaining=1, budget_ok=True)
        b2 = apply_medium_rule(dict(g), opened, rounds_remaining=0, budget_ok=True)
        c2 = apply_medium_rule(dict(g), opened, rounds_remaining=1, budget_ok=False)
        ok(a["action"] == "PATCH_OR_RETHINK" and a["rule_matched"] == "4m" and "before ACCEPT" in a["rationale"]
           and b2["action"] == "DECLARE_SCOPE" and b2["rule_matched"] == "3m" and c2["action"] == "DECLARE_SCOPE"
           and sorted(b2["declared_open"]) == sorted(f["id"] for f in opened),
           f"T39b open findings: fixed while rounds and budget remain, else declared, never accepted ({a['action']}, {b2['action']})")
        # T40: the write-up shows values, not claim ids.
        fr = os.path.join(td, "render")
        os.makedirs(os.path.join(fr, "models"))
        shutil.copy(os.path.join(FX_DIR, "claims_ledger.yaml"), os.path.join(fr, "models", "claims_ledger.yaml"))
        yaml.safe_dump({"schema_version": 1}, open(os.path.join(fr, AB.SPEC_FILE), "w"))
        open(os.path.join(fr, "manuscript.md"), "w").write(
            "The plan averts [CLAIM:headline.point] cases at [CLAIM:cost.allocated].\n")
        render_report(fr, toolkit)
        rep = open(os.path.join(fr, "report.md")).read()
        ok("[CLAIM:" not in rep and "headline.point" not in rep.split("<!-- harness")[0] and not report_claim_findings(fr),
           f"T40 claim references are rendered inline ({rep.splitlines()[0][:120]})")
        open(os.path.join(fr, "manuscript.md"), "w").write("ITN in mix.itn districts; [CLAIM:no.such.claim].\n")   # a known id in inline code renders (CR3)
        render_report(fr, toolkit)
        kinds = [(f["kind"], f["severity"]) for f in report_claim_findings(fr)]
        ok(("report_claim_id_raw", "HIGH") in kinds and ("report_claim_unresolved", "HIGH") in kinds,
           f"T40b a raw claim id or an unresolved reference in the report is a HIGH finding ({kinds})")
        # T41: every input the question names is copied into data/ with its sha256; a spec citing a
        # file outside the run is refused.
        fp = os.path.join(td, "prov")
        os.makedirs(fp)
        staged = stage_inputs(fp, "Inputs: requirements.txt (the pins) and no/such/file.csv.")
        prov = json.load(open(os.path.join(fp, "data", "inputs_provenance.json")))
        src = open(os.path.join(REPO, "requirements.txt"), "rb").read()
        ok(staged == ["data/inputs/requirements.txt"]
           and prov["inputs"]["data/inputs/requirements.txt"]["sha256"] == hashlib.sha256(src).hexdigest()
           and open(os.path.join(fp, "data", "inputs", "requirements.txt"), "rb").read() == src,
           f"T41 the question's input files are copied into data/inputs/ with their sha256 ({staged})")
        yaml.safe_dump({"decision_units": {"csv": "data/inputs/requirements.txt"}, "calibration": {"targets": {"csv": "../outside/targets.csv"}}},
                       open(os.path.join(fp, AB.SPEC_FILE), "w"))
        probs = record_cited_inputs(fp)
        prov = json.load(open(os.path.join(fp, "data", "inputs_provenance.json")))
        ok(any("../outside/targets.csv" in x and "outside the run" in x for x in probs) and "data/inputs/requirements.txt" in prov["cited"],
           f"T41b cited inputs are recorded with their sha256; one outside the run is refused ({probs})")

        # T41c: a path at the end of a sentence or clause keeps its input (trailing punctuation is
        # stripped before the extension test, for bare, quoted and parenthesised paths).
        qp = _question_paths('Use data/a/one.csv. Also data/b/two.csv, data/c/three.json; '
                             '(data/d/four.yaml) "data/e/five.tsv." \'data/f/six.txt,\' `data/g/seven.csv;` '
                             'and "./data/h/eight.csv").')
        ok(sorted(qp) == ["data/a/one.csv", "data/b/two.csv", "data/c/three.json", "data/d/four.yaml",
                          "data/e/five.tsv", "data/f/six.txt", "data/g/seven.csv", "data/h/eight.csv"],
           f"T41c paths ending in . , ; ) or quotes are kept once each, punctuation stripped ({qp})")
        # T41d (Codex r1 of #332): a quoted name that ends in its extension is kept as written; "../" and "/" are kept
        # (stage_inputs refuses them as outside the repository); a quoted name is not re-scanned for a bare suffix;
        # an apostrophe span naming no file still is
        q2 = _question_paths('Use "[draft] t.csv" and ../requirements.txt. and /requirements.txt; also '
                             '"evals/private requirements.txt." and the program\'s file data/k/nine.csv in the '
                             'district\'s table.')
        ok(q2 == ["[draft] t.csv", "evals/private requirements.txt", "../requirements.txt", "/requirements.txt",
                  "data/k/nine.csv"],
           f"T41d brackets in a quoted name kept; ../ and / kept; no bare suffix from a quoted name; apostrophes "
           f"do not hide a path ({q2})")
        fpq = os.path.join(td, "prov_outside")
        os.makedirs(fpq)
        staged = stage_inputs(fpq, 'Inputs: ../requirements.txt. and "evals/private requirements.txt."')
        ok(staged == [], f"T41d a path outside the repo or into the evaluation tree stages nothing ({staged})")
        fpp = os.path.join(td, "prov_punct")
        os.makedirs(fpp)
        staged = stage_inputs(fpp, "The pins are in requirements.txt.")
        ok(staged == ["data/inputs/requirements.txt"], f"T41c a file named at the end of a sentence is staged ({staged})")

        # T42: through the machine. A MEDIUM with a runnable test goes to the fix loop (rule 4m) and
        # is fixed before ACCEPT; one that cannot be fixed, with no rounds left, is declared (3m) and
        # the report says so.
        med = {"id": "D-010", "severity": "MEDIUM", "target_stage": "ANALYZE", "claim": "more draws",
               "done_when": [{"file": "analysis_settings.yaml", "key": "uncertainty.draws", "op": ">=", "value": 40}]}
        d, T, st = new_run("medium_fix", {"critics": {1: {"critique-domain": [med]}},
                                          "fixes": {"D-010": [("analysis_settings.yaml", "uncertainty.draws", 40)]}})
        s = _ToolkitMachine(d, T, st, log=lambda *a: None, cross_engine_check=False).run()
        g1 = json.load(open(os.path.join(d, "gate_r1.json")))
        ok(s == "completed" and g1["action"] == "PATCH_OR_RETHINK" and g1["rule_matched"] == "4m"
           and FX.impact_rows(d) and FX.impact_rows(d)[0]["findings"] == ["D-010"],
           f"T42 an open MEDIUM with a test is fixed in the loop before ACCEPT ({s}, {g1['action']} {g1['rule_matched']})")
        d, T, st = new_run("medium_declare", {"critics": {1: {"critique-domain": [dict(med, id="D-011")]}}}, max_rounds=1)
        s = _ToolkitMachine(d, T, st, log=lambda *a: None, cross_engine_check=False).run()
        g1 = json.load(open(os.path.join(d, "gate_r1.json")))
        rep = open(os.path.join(d, "report.md")).read()
        ok(s == "completed_scope_declared" and g1["rule_matched"] == "3m" and "D-011" in rep
           and "Open findings, declared" in rep,
           f"T42b with no rounds left the open MEDIUM is declared, and the report says so ({s}, {g1['rule_matched']})")

        # T43-T51: Codex round 1 on #95 (each failed on a3a31b7).
        d43 = os.path.join(td, "t43")
        os.makedirs(d43)
        g43 = {"action": "ACCEPT", "rigor_violations": [
            {"kind": "done_when_unmet", "severity": "MEDIUM", "key": "D-001", "claim": "the test fails"},
            {"kind": "critique_presentation_missing", "severity": "MEDIUM", "claim": "no review"}]}
        ok({o["id"] for o in open_findings(d43, g43)} == {"done_when_unmet:D-001", "critique_presentation_missing"},
           "T43 a resolved blocker whose test fails, and a missing presentation review, stay open")
        # OF1-OF2 (external review 2026-11-17): an unreadable reviewer YAML is fail-closed. The gate refuses to
        # decide (a schema error the critic is asked to repair; once the repairs are used the run ends
        # completed_gate_error), so open_findings, which runs only on a gate action, never sees it; and
        # open_findings itself reports such a file as an open HIGH, never skips it or raises.
        for tag, body in (("garbled", "blockers: [unclosed\n  : -\n"), ("list", "- a\n- b\n"), ("text", "just prose\n"),
                          ("empty", ""), ("no_blockers", "reviewer: critique-domain\n"), ("false", "blockers: false\n")):
            dof = os.path.join(td, f"of_{tag}")
            os.makedirs(dof)
            shutil.copy(os.path.join(FX_DIR, "critique_redteam.yaml"), dof)
            open(os.path.join(dof, "critique_domain.yaml"), "w").write(body)
            gof = v2_gate(dof, 1, 2, False, [], history=False)
            ok(gof.get("action") is None and any("critique_domain.yaml" in str(e) for e in gof.get("schema_errors") or []),
               f"OF1 [{tag}] the gate takes no action on an unreadable reviewer YAML and names it ({gof})")
            try:
                oof = open_findings(dof, {"action": "ACCEPT", "rigor_violations": []})
            except Exception as e:  # noqa: BLE001
                oof = [f"raised {type(e).__name__}"]
            ok(any(isinstance(o, dict) and o.get("id") == "critique_unreadable:critique-domain"
                   and o.get("severity") == "HIGH" for o in oof),
               f"OF2 [{tag}] open_findings reports the unreadable reviewer YAML as an open HIGH ({oof})")
        fr = os.path.join(td, "t44")
        os.makedirs(os.path.join(fr, "models"))
        shutil.copy(os.path.join(FX_DIR, "claims_ledger.yaml"), os.path.join(fr, "models", "claims_ledger.yaml"))
        yaml.safe_dump({"schema_version": 1, "constraints": {"envelope_usd": 18000000}}, open(os.path.join(fr, AB.SPEC_FILE), "w"))
        open(os.path.join(fr, "manuscript.md"), "w").write("Intro.\n<!-- harness: copied -->\nheadline.point and [CLAIM:nope].\n")
        k44b = {f["kind"] for f in report_claim_findings(fr)}
        ok({"report_claim_id_raw", "report_claim_unresolved"} <= k44b, f"T44b no marker the writer copies hides an id ({k44b})")
        open(os.path.join(fr, "manuscript.md"), "w").write("Averts [CLAIM:headline.point] cases.\n")
        open(os.path.join(fr, "report.md"), "w").write("stale [CLAIM:headline.point]\n")
        m45 = Machine(fr, StubTasks({}), RunState(question="q", state="FIX_LOOP"), backend=toolkit, log=lambda *a: None)
        m45.rebuild(fr)
        ok("Averts 1.3M" in open(os.path.join(fr, "report.md")).read(),
           "T45 the fix loop's rebuild re-renders the report from the edited draft")
        fp = os.path.join(td, "t47", "run")
        os.makedirs(os.path.join(fp, "data"))
        os.makedirs(os.path.join(td, "t47", "x y"))
        open(os.path.join(td, "t47", "x y", "units.csv"), "w").write("a\n")
        open(os.path.join(fp, "data", "targets"), "w").write("t\n")
        yaml.safe_dump({"decision_units": {"csv": "../x y/units.csv"}, "calibration": {"targets": {"csv": "data/targets"}}},
                       open(os.path.join(fp, AB.SPEC_FILE), "w"))
        p47 = record_cited_inputs(fp)
        prov = json.load(open(os.path.join(fp, PROVENANCE)))
        ok(any("../x y/units.csv" in x for x in p47) and "data/targets" in prov["cited"],
           f"T47 a cited path with a space outside the run is refused; a path field without an extension is recorded ({p47})")
        fq = os.path.join(td, "t48")
        outside = os.path.join(td, "t48_outside")
        os.makedirs(os.path.join(fq, "data"))
        os.makedirs(outside)
        os.symlink(outside, os.path.join(fq, "data", "inputs"))
        try:
            stage_inputs(fq, "Inputs: requirements.txt")
            raised = False
        except ValueError:
            raised = True
        ok(raised and not os.listdir(outside), "T48 a symlinked data/inputs is refused; nothing is written outside the run")
        ok(answer_key_path("EVALS/q/rubric.yaml") and answer_key_path("data/_TRUTH/truth.json")
           and not answer_key_path("data/evals/q/districts.csv"),
           "T49 the answer-key exclusion is case-insensitive; the questions' public inputs are not excluded")
        fs = os.path.join(td, "t50")
        os.makedirs(fs)
        stage_inputs(fs, "requirements.txt")
        yaml.safe_dump({"decision_units": {"csv": "data/inputs/requirements.txt"}}, open(os.path.join(fs, AB.SPEC_FILE), "w"))
        ok(record_cited_inputs(fs) == [], "T50 an untouched staged input passes")
        open(os.path.join(fs, "data", "inputs", "requirements.txt"), "a").write("tampered\n")
        ok(any("changed or removed" in x for x in record_cited_inputs(fs)),
           "T50b a staged input changed after it was copied is refused")
        import pipeline as P_
        fk = os.path.join(td, "t50k")         # a joint calibration group target's key names columns, not files
        os.makedirs(fk)
        stage_inputs(fk, "requirements.txt")
        for csv, want in (("data/inputs/requirements.txt", False), ("data/missing.csv", True)):
            spec = {"decision_units": {"csv": "data/inputs/requirements.txt"}, "calibration": {"joint": {"targets": [
                {"id": "g", "kind": "group", "csv": csv, "key": {"csv": "unit", "units": "state"}}]}}}
            yaml.safe_dump(spec, open(os.path.join(fk, AB.SPEC_FILE), "w"))
            rc = record_cited_inputs(fk)
            dr = [p["claim"] for p in P_.derivation_problems(fk, spec, required=True) if p["code"] == "data_ref_not_derived"]
            ok(not any("cites unit" in x for x in rc) and not any("reads unit" in x for x in dr),
               f"T50c a joint target's key.csv (a column) is not a cited file ({rc}; {dr})")
            if want:
                ok(any("data/missing.csv" in x for x in rc) and any("data/missing.csv" in x for x in dr),
                   f"T50d the joint target's own csv is still a cited file, and a missing one is refused ({rc}; {dr})")
        # T50f (baseline hb2 §4.6): the spec cited a question file by its repository path; its staged copy
        # existed. Both refusals name the staged path, so the next try cites it.
        for cited in ("requirements.txt", "./requirements.txt"):
            spec = {"decision_units": {"csv": "data/inputs/requirements.txt"},
                    "calibration": {"targets": [{"id": "t", "csv": cited}]}}
            yaml.safe_dump(spec, open(os.path.join(fk, AB.SPEC_FILE), "w"))
            rc = [x for x in record_cited_inputs(fk) if "does not exist" in x]
            dr = [p["claim"] for p in P_.derivation_problems(fk, spec, required=True) if p["code"] == "data_ref_not_derived"]
            ok(rc and all("data/inputs/requirements.txt" in x for x in rc)
               and dr and all("data/inputs/requirements.txt" in x for x in dr),
               f"T50f a cited path whose staged copy exists: the refusals name the staged path ({rc}; {dr})")
        # T50g (Codex r1 of #355): a qualified path in another folder (a derived table) is not matched to a staged
        # input by its file name: following that hint would skip the derivation.
        spec = {"decision_units": {"csv": "data/inputs/requirements.txt"},
                "calibration": {"targets": [{"id": "t", "csv": "data/derived/requirements.txt"}]}}
        yaml.safe_dump(spec, open(os.path.join(fk, AB.SPEC_FILE), "w"))
        rc = [x for x in record_cited_inputs(fk) if "does not exist" in x]
        ok(rc and not any("staged copy" in x for x in rc),
           f"T50g a qualified path is matched to a staged input only by its full path ({rc})")
        # T50h (Codex r2 of #355): the units table must be a derivation's output, so its hint names the staged file as
        # the derivation's input, never as the path to cite.
        spec = {"decision_units": {"csv": "requirements.txt"}}
        yaml.safe_dump(spec, open(os.path.join(fk, AB.SPEC_FILE), "w"))
        rc = [x for x in record_cited_inputs(fk) if "does not exist" in x]
        ok(rc and all("derivation" in x and "cite that path" not in x for x in rc),
           f"T50h the units table's hint says to read the staged file in a derivation ({rc})")
        rc = [x for x in record_cited_inputs(fk, derived_inputs=False) if "does not exist" in x]
        ok(rc and all("cite that path" in x for x in rc),
           f"T50i (Codex r3) a run that runs no derivations is told to cite the staged units table itself ({rc})")
        for kind, key in (("group", {"csv": {"file": "data/missing.csv"}, "units": "state"}),
                          ("unit", {"csv": "data/missing.csv"})):    # Codex: only a group target's column name
            spec = {"decision_units": {"csv": "data/inputs/requirements.txt"}, "calibration": {"joint": {"targets": [
                {"id": "g", "kind": kind, "csv": "data/inputs/requirements.txt", "key": key}]}}}
            yaml.safe_dump(spec, open(os.path.join(fk, AB.SPEC_FILE), "w"))
            ok(any("data/missing.csv" in x for x in record_cited_inputs(fk)),
               f"T50e a {kind} target's key.csv that is not a group column name is still scanned for files")
        open(os.path.join(fr, "manuscript.md"), "w").write("`headline.point`\n")
        os.remove(os.path.join(fr, STATE_FILE))           # T45's machine left its state here
        m51 = Machine(fr, StubTasks({}), RunState(question="q", state="GATE", v2_rules=False), backend=toolkit,
                      log=lambda *a: None)
        ok(not any(f["kind"].startswith("report_") for f in m51.harness_findings()),
           "T51 golden replays (v2_rules off) do not run the v2 report checks")

        open(os.path.join(fr, "manuscript.md"), "w").write("The plan averts\n```\n[CLAIM:headline.point]\n```\n")
        render_report(fr, toolkit)
        ok(any(f["kind"] == "report_claim_unrendered" for f in report_claim_findings(fr)),
           "T54 a claim reference left unrendered in the report (in a fenced block) is a HIGH finding")
        fe = os.path.join(td, "t55")
        os.makedirs(os.path.join(fe, "data"))
        yaml.safe_dump({"engine": {"ref": "toolkit.engines.contract:ToyEngine",
                                   "config": {"results_path": os.path.join(td, "outside_bench")}}},
                       open(os.path.join(fe, AB.SPEC_FILE), "w"))
        os.makedirs(os.path.join(td, "outside_bench"), exist_ok=True)
        ok(any("outside_bench" in x for x in record_cited_inputs(fe)),
           "T55 an engine config path outside the run, without a file extension, is refused")

        try:
            _self_test_resume(ok, td, fast_toy, new_run)
        except Exception as e:  # noqa: BLE001 -- before the resume path exists this is the failure
            import traceback
            traceback.print_exc()
            ok(False, f"T58-T60 analysis timeout and resume: {type(e).__name__}: {e}")

        _self_test_auto1a(ok, td, fast_toy, new_run)
        _self_test_writeup_rounds(ok, td, fast_toy, new_run)
        _self_test_claim_refs(ok, td, fast_toy, new_run)
        try:
            _self_test_hb2_spec(ok, td, fast_toy, new_run)
        except Exception as e:  # noqa: BLE001 -- before the fix this is the failure
            ok(False, f"HS: {type(e).__name__}: {e}")
        try:
            _self_test_spec_p1s(ok, td, fast_toy, new_run)
        except Exception as e:  # noqa: BLE001 -- before the fix this is the failure
            ok(False, f"SP: {type(e).__name__}: {e}")
        try:
            pass  # built-engine tests run in agents/engine_build_stage.py --self-test
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            ok(False, f"BE28-BE33 built-engine stage machine: {type(e).__name__}: {e}")
        try:
            _self_test_acquire(ok, td)
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            ok(False, f"T_ACQ_* acquire stage machine: {type(e).__name__}: {e}")

        # T23: a transition outside the table is refused.
        m = _ToolkitMachine(os.path.join(td, "ref"), StubTasks({}), None, log=lambda *a: None)
        m.st.state = "SPEC"
        try:
            m._go("WRITE")
            ok(False, "T23 an illegal transition raises")
        except RuntimeError:
            ok(True, "T23 an illegal transition raises")
        p = os.path.join(td, "ref", STATE_FILE)
        json.dump(dict(json.load(open(p)), state="LEAD_PROMPT"), open(p, "w"))
        try:
            load_state(os.path.join(td, "ref"))
            ok(False, "T24 an unknown persisted state fails closed")
        except ValueError:
            ok(True, "T24 an unknown persisted state fails closed")
        try:
            _self_test_derived(ok, td)
        except Exception as e:  # noqa: BLE001 -- before the derivation step exists (step α) this is the failure
            import traceback
            traceback.print_exc()
            ok(False, f"DI machine tests: {type(e).__name__}: {e}")
        try:
            _self_test_geometry(ok, td)
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            ok(False, f"IG machine tests: {type(e).__name__}: {e}")
        try:
            _self_test_evidence(ok, td)
        except Exception as e:  # noqa: BLE001 -- before the evidence stage exists (step α) this is the failure
            import traceback
            traceback.print_exc()
            ok(False, f"EV machine tests: {type(e).__name__}: {e}")
        # T70 (run console review, finding 6): `sign --expect-sha256` refuses a spec other than the one shown.
        from toolkit.spec import schema as _S, signoff as _SO
        d70 = os.path.join(td, "t61")
        os.makedirs(d70)
        _S.toy_spec(d70)
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            try:
                rc = main(["sign", d70, "--by", "owner", "--note", "n", "--expect-sha256", "0" * 64])
            except SystemExit as e:
                rc = e.code
            st70 = _SO.status(d70)
            try:
                rc2 = main(["sign", d70, "--by", "owner", "--note", "n", "--expect-sha256",
                            hashlib.sha256(open(os.path.join(d70, AB.SPEC_FILE), "rb").read()).hexdigest()])
            except SystemExit as e:
                rc2 = e.code
        d72, o72 = os.path.join(td, "t72"), os.path.join(td, "t72out")
        os.makedirs(d72)
        os.makedirs(o72)
        _S.toy_spec(d72)
        open(os.path.join(o72, "v"), "w").write("untouched")
        os.symlink(os.path.join(o72, "v"), os.path.join(d72, SIGNOFF_SHEET))
        try:
            Machine._write_sheet(type("M", (), {"run_dir": d72, "backend": None})())
        except (ValueError, OSError):
            pass
        ok(open(os.path.join(o72, "v")).read() == "untouched",
           "T72 the sign-off sheet is published without following a link (round 3, P1-2)")
        # T73 (round 4, P1-2): run_state.json and the machine's records never follow a planted .tmp link
        d73, o73 = os.path.join(td, "t73"), os.path.join(td, "t73out")
        os.makedirs(d73)
        os.makedirs(o73)
        open(os.path.join(o73, "v"), "w").write("untouched")
        os.symlink(os.path.join(o73, "v"), os.path.join(d73, STATE_FILE + ".tmp"))
        os.symlink(os.path.join(o73, "v"), os.path.join(d73, "rec.json.tmp"))
        for fn in (lambda: save_state(d73, RunState(question="q", state="SPEC")), lambda: _write_json(d73, "rec.json", {})):
            try:
                fn()
            except (ValueError, OSError):
                pass
        ok(open(os.path.join(o73, "v")).read() == "untouched",
           "T73 save_state and the machine's JSON records do not write through a planted .tmp link")
        d71 = os.path.join(td, "t71")
        os.makedirs(d71)
        _S.toy_spec(d71)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            try:
                rc71 = main(["sign", d71, "--by", "owner", "--note", "n", "--expect-sha256",
                             hashlib.sha256(open(os.path.join(d71, AB.SPEC_FILE), "rb").read()).hexdigest(),
                             "--expect-derivations-sha256", "0" * 64])
            except SystemExit as e:
                rc71 = e.code
            st71 = _SO.status(d71)
            try:
                rc71b = main(["sign", d71, "--by", "owner", "--note", "n", "--expect-sha256",
                              hashlib.sha256(open(os.path.join(d71, AB.SPEC_FILE), "rb").read()).hexdigest(),
                              "--expect-derivations-sha256", "none"])
            except SystemExit as e:
                rc71b = e.code
        ok(rc71 not in (0, None) and st71 == "awaiting" and rc71b == 0 and _SO.status(d71) == "signed",
           f"T71 sign --expect-derivations-sha256 refuses a derivation record other than the one shown and signs "
           f"with the one shown (rc {rc71}, {st71}; rc {rc71b})")
        ok(rc not in (0, None) and st70 == "awaiting" and rc2 == 0 and _SO.status(d70) == "signed",
           f"T70 stage_machine.py sign --expect-sha256 refuses another spec's sha and signs the shown one "
           f"(rc {rc}, {st70}; rc {rc2}, {_SO.status(d70)})")
        # T80-T83 (F6 S1-7): declared findings keep their full text and remedy; not judged is not a pass;
        # a declared-scope report says so and uses no acceptance language. The fixture mimics F5's three
        # declarations whose claims were cut mid-sentence (D-003, R-001, R-002).
        import yaml
        f80 = os.path.join(td, "t80")
        os.makedirs(f80)
        longc = {i: (f"{i}: the effect used for this component is a fixed value with no cited source, and the "
                     "chosen package in each unit depends on it; " * 4 + f"END-{i}.") for i in ("D-003", "R-001", "R-002")}
        fix = {i: "Replace the fixed value with a sourced range and rerun the decision test; " * 3 + f"FIX-{i}."
               for i in longc}
        jd = [{"file": "manuscript.md", "exists": True}, {"judgement": "a sourced range is used"}]
        for fn, ids in (("critique_domain.yaml", ["D-003"]), ("critique_redteam.yaml", ["R-001", "R-002"])):
            yaml.safe_dump({"blockers": [{"id": i, "severity": "MEDIUM", "target_stage": "MODEL", "resolved": False,
                                          "first_seen_round": 1, "claim": longc[i], "fix_requires": fix[i],
                                          "done_when": jd} for i in ids]}, open(os.path.join(f80, fn), "w"))
        yaml.safe_dump({"schema_version": 1}, open(os.path.join(f80, AB.SPEC_FILE), "w"))
        open(os.path.join(f80, "manuscript.md"), "w").write("The plan is described here.\n")
        try:
            op80 = open_findings(f80, {"action": "ACCEPT", "rigor_violations": []})
            g80 = apply_medium_rule({"action": "ACCEPT"}, op80, rounds_remaining=0, budget_ok=True)
            write_declared(f80, op80, g80)
            dec80 = yaml.safe_load(open(os.path.join(f80, DECLARED_FILE)))["declared"]
            ok(g80["action"] == "DECLARE_SCOPE" and len(dec80) == 3
               and all(e["claim"] == longc[e["id"]] and e.get("required_remedy") == fix[e["id"]]
                       and 0 < len(e.get("label") or "") <= 160 for e in dec80),
               f"T80 declared_findings.yaml keeps each full claim and required remedy, with a separate short label "
               f"({[(e['id'], len(e['claim']), bool(e.get('required_remedy'))) for e in dec80]})")
            sdc = "SD-1 text: the decision depends on an unsourced effect, " * 8 + "END-SD."
            yaml.safe_dump({"declarations": [{"id": "SD-1", "blocker_id": "D-003", "claim": sdc,
                                              "description": "DESC-SD", "fix_requires": "REMEDY-SD"}]},
                           open(os.path.join(f80, "scope_declaration.yaml"), "w"))
            render_report(f80, toolkit, status="completed_scope_declared")
            rep80 = open(os.path.join(f80, "report.md")).read()
            ok(all(longc[i] in rep80 and fix[i] in rep80 for i in longc)
               and all(x in rep80 for x in (sdc, "DESC-SD", "REMEDY-SD"))
               and os.path.exists(os.path.join(f80, "report_render.json"))
               and "Run status: declared scope, not accepted" in rep80.split("\n\n")[0],
               "T81 the report repeats each declared finding and each scope declaration in full with its remedy, "
               "under a 'declared scope, not accepted' status line, and records its harness blocks")
        except Exception as e:  # noqa: BLE001 -- the α tests fail, they do not crash the suite
            ok(False, f"T80/T81 raised {type(e).__name__}: {e}")
        try:
            nj = {"action": "ACCEPT", "rule_matched": 4,
                  "done_when_status": {"D-009": {"passes": None, "result": "not_judged", "open": True,
                                                 "severity": "MEDIUM", "detail": "not judged"}}}
            r83 = require_judged(dict(nj))
            ok(r83["action"] != "ACCEPT" and r83.get("check_summary", {}).get("not_judged") == 1
               and "D-009" in json.dumps(r83),
               f"T83 a required check that is not judged cannot yield ACCEPT ({r83.get('action')})")
        except Exception as e:  # noqa: BLE001
            ok(False, f"T83 raised {type(e).__name__}: {e}")
        # T84c-T84e (Codex r1-r3 of #355): only a post-write gate's decision retires a stale report_withheld.md;
        # a run that ends before it (the budget, at WRITE) keeps it, and a directory at that name never stops FINAL.
        t84 = {}
        for case, frm, gate, wh_dir in (("budget_at_write", "WRITE", {}, False),
                                        ("gate_no_action", "GATE", {"post_write": True, "action": None}, False),
                                        ("gate_dir", "GATE", {"post_write": True, "action": "ACCEPT"}, True),
                                        ("gate_ok", "GATE", {"post_write": True, "action": "ACCEPT"}, False)):
            dc = os.path.join(td, "t84c_" + case)
            os.makedirs(dc)
            open(os.path.join(dc, "report.md"), "w").write("repaired\n")
            if wh_dir:
                os.makedirs(os.path.join(dc, "report_withheld.md"))
            else:
                open(os.path.join(dc, "report_withheld.md"), "w").write("rejected\n")
            mc = Machine(dc, StubTasks({}), RunState(question="q", state=frm, v2_rules=False, last_gate=gate),
                         backend=toolkit, log=lambda *a: None)
            try:
                mc._go("FINAL", "t", "budget_exhausted" if frm == "WRITE" else "completed")
                t84[case] = os.path.lexists(os.path.join(dc, "report_withheld.md"))
            except Exception as e:  # noqa: BLE001 -- the test is that it does not raise
                t84[case] = repr(e)
        dc = os.path.join(td, "t84d")
        os.makedirs(dc)
        open(os.path.join(dc, "report.md"), "w").write("repaired\n")
        open(os.path.join(dc, "report_withheld.md"), "w").write("rejected\n")
        mc = Machine(dc, StubTasks({}), RunState(question="q", state="GATE", v2_rules=False,
                                                 last_gate={"post_write": True, "action": "VALIDATOR_ERROR"}),
                     backend=toolkit, log=lambda *a: None)
        mc._go("FINAL", "validator error", "completed_gate_error")
        t84["validator_error"] = os.path.lexists(os.path.join(dc, "report_withheld.md"))
        dc = os.path.join(td, "t84e")
        os.makedirs(dc)
        open(os.path.join(dc, "report.md"), "w").write("repaired\n")
        open(os.path.join(dc, "report_withheld.md"), "w").write("rejected\n")
        mc = Machine(dc, StubTasks({}), RunState(question="q", state="GATE", v2_rules=False,
                                                 last_gate={"post_write": True, "action": "RUN_FAILED"}),
                     backend=toolkit, log=lambda *a: None)
        mc._go("FINAL", "declined", "completed_scope_declined")
        t84["declined"] = os.path.lexists(os.path.join(dc, "report_withheld.md"))
        ok(t84 == {"budget_at_write": True, "gate_no_action": True, "gate_dir": True, "gate_ok": False,
                   "validator_error": True, "declined": False},
           f"T84c the withheld draft is retired only after a post-write gate decided, never a directory ({t84})")
        # T92: _withhold_report goes through lib.safe_io: a planted link at report_withheld.md is replaced,
        # never written through, and a linked report.md is removed without following the link.
        d92, o92 = os.path.join(td, "t92"), os.path.join(td, "t92out")
        os.makedirs(d92)
        os.makedirs(o92)
        open(os.path.join(o92, "v"), "w").write("untouched")
        open(os.path.join(d92, "report.md"), "w").write("rejected report\n")
        os.symlink(os.path.join(o92, "v"), os.path.join(d92, "report_withheld.md"))
        try:
            _withhold_report(d92)
            moved = (not os.path.exists(os.path.join(d92, "report.md"))
                     and not os.path.islink(os.path.join(d92, "report_withheld.md"))
                     and open(os.path.join(d92, "report_withheld.md")).read() == "rejected report\n")
            os.symlink(os.path.join(o92, "v"), os.path.join(d92, "report.md"))
            _withhold_report(d92)
            link_removed = not os.path.lexists(os.path.join(d92, "report.md"))
        except Exception as e:  # noqa: BLE001
            moved, link_removed = f"raised {type(e).__name__}: {e}", False
        ok(moved is True and link_removed and open(os.path.join(o92, "v")).read() == "untouched",
           f"T92 _withhold_report never follows a link ({moved}, linked report removed: {link_removed})")
        # T86-T87 through h_GATE: an open finding whose only test is a judgement is never accepted. The fix loop
        # and rule 3m handle it first; rule 3j is the backstop when no open finding is listed for it.
        judged_only = {"id": "D-086", "severity": "MEDIUM", "target_stage": "ANALYZE", "claim": "needs a judgement",
                       "done_when": [{"judgement": "the critic reads the caveat"}]}
        g_ = globals()
        orig_open = g_["open_findings"]
        for name, patch, rule in (("t86", None, "3m"), ("t87", lambda rd, dd: [], "3j")):
            d, T, st = new_run(name, {"critics": {1: {"critique-domain": [judged_only]}}}, max_rounds=1)
            if patch:
                g_["open_findings"] = patch
            try:
                s = _ToolkitMachine(d, T, st, log=lambda *a: None, cross_engine_check=False).run()
            except Exception as e:  # noqa: BLE001
                s = f"raised {type(e).__name__}: {e}"
            finally:
                g_["open_findings"] = orig_open
            fg = json.load(open(os.path.join(d, "final_gate.json"))) if os.path.exists(os.path.join(d, "final_gate.json")) else {}
            ok(s == "completed_scope_declared" and fg.get("rule_matched") == rule
               and (fg.get("check_summary") or {}).get("required_not_judged") == ["D-086"],
               f"T{name[1:]} a judgement-only finding at exhaustion is declared by rule {rule}, with the check "
               f"summary in final_gate.json ({s}, {fg.get('rule_matched')}, {fg.get('check_summary')})")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


_TOY_BATCH_FN = '''import os
import time


def f(p):
    with open(os.environ["MA_TOY_CALLS"], "a") as fh:
        fh.write(f"{p['i']}\\n")
    time.sleep(float(os.environ.get("MA_TOY_SLEEP_S") or 0))
    if p["i"] >= int(os.environ.get("MA_TOY_SLOW_FROM") or 99):
        parent = os.getppid()
        for _ in range(600):              # until the step is killed (this process is then orphaned)
            time.sleep(0.1)
            if os.getppid() != parent:
                break
    return {"v": p["i"]}
'''
_TOY_BATCH_DRIVER = '''import json
import os
import sys
sys.path.insert(0, {scripts!r})
import compute

if os.environ.get("MA_TOY_INFRA_FROM"):
    # T61: rows from this input on come back as compute.py reports a chunk lost with its worker.
    _real_map = compute.map_fn

    def _lossy(run_dir, fn_spec, inputs, **kw):
        rows, s = _real_map(run_dir, fn_spec, inputs, **kw)
        k0 = int(os.environ["MA_TOY_INFRA_FROM"])
        return [r if inputs[r["i"]]["i"] < k0 else {{"i": r["i"], "ok": False, "kind": "infra",
                                                    "error": "chunk failed: its worker left the cluster"}}
                for r in rows], s
    compute.map_fn = _lossy


def main(rd):
    os.makedirs(os.path.join(rd, "analysis", "batch"), exist_ok=True)
    rows, _ = compute.run_map(rd, os.path.join(rd, "models", "toy_fn.py") + "::f", [{{"i": i}} for i in range(6)],
                              os.path.join(rd, "analysis", "batch", "rows.jsonl"), backend="local", max_workers=1,
                              purpose="toy batch")
    json.dump([bool(r.get("ok")) for r in rows], open(os.path.join(rd, "analysis", "toy_rows.json"), "w"))
    return 0 if all(r.get("ok") for r in rows) else 1


if __name__ == "__main__":         # the local pool's spawned processes import this file again
    sys.exit(main(sys.argv[1]))
'''


def _self_test_auto1a(ok, td: str, toolkit, new_run) -> None:
    """v2 autonomy brief, PR 1A (docs/design/v2-autonomy-brief.md §4): inline presentation review
    budget stop, the gate before a rewrite, harness ids, the RETHINK back-edges and claim references."""
    from types import SimpleNamespace as NS
    import yaml
    from v2_tasks import StubTasks

    def mk(d, T, st, backend=toolkit):
        return Machine(d, T, st, backend=backend, log=lambda *a: None, cross_engine_check=False)

    def case(name, fn):
        try:
            fn()
        except Exception as e:  # noqa: BLE001 -- before the fix exists this is the failure
            ok(False, f"{name}: {type(e).__name__}: {e}")

    def budget():
        d, T, st = new_run("a1_pres", {}, budget=20.0)
        st.state, st.write_mode, st.spent_usd = "WRITE", "accepted", 16.5        # past cap less reserve, $3.50 left
        save_state(d, st)
        mk(d, T, st).step()
        s = load_state(d)
        ok(s.state == "GATE" and s.forced_status is None
           and T.calls == ["write:accepted", "critique:critique-presentation:1"],
           f"T_PRES_BUDGET the write & presentation review run on the write-up reserve ({s.state}, {s.forced_status}, {T.calls})")
        d, T, st = new_run("a1_pres_out", {}, budget=20.0)
        st.state, st.write_mode, st.spent_usd = "WRITE", "accepted", 20.0
        st.journal["WRITE#0:accepted"] = {"task": "write", "status": "completed", "ok": True}
        open(os.path.join(d, "manuscript.md"), "w").write("# Report\n")
        save_state(d, st)
        mk(d, T, st).step()
        s = load_state(d)
        ok(s.state == "FINAL" and s.status == "budget_exhausted" and T.calls == [],
           f"T_PRES_BUDGET2 with the whole cap spent the run still ends budget_exhausted ({s.state}, {s.status})")

        class RBT(StubTasks):
            def set_allowance(self, usd, readback_usd=None):
                self.rb = readback_usd
                super().set_allowance(usd)
        d, T, st = new_run("a1_rb", {}, budget=20.0)
        st.spent_usd = 16.5
        save_state(d, st)
        T = RBT(T.s)
        m = mk(d, T, st)
        m._task("k1", "sign_spec", d, writeup=True)
        ok(T.rb == READBACK_CAP_USD and m._task("k2", "sign_spec", d) is None,
           f"T_WRITE_RB a write-up task keeps its read-back allowance; other tasks still stop at the reserve ({T.rb})")

    def post_write():
        d = os.path.join(td, "a1_pw")
        os.makedirs(d)
        S = StubTasks({"critics": {1: {"critique-presentation": [
            {"id": "P-001", "severity": "MEDIUM", "target_stage": "WRITE", "claim": "a caption is unclear",
             "done_when": [{"file": "manuscript.md", "contains": "Figure 1 shows"}]}]}}})
        S.critique(d, "critique-presentation", 1)
        for r in CRITICS:
            S.critique(d, r, 1)
        open(os.path.join(d, "manuscript.md"), "w").write("# Report\n")
        g = v2_gate(d, 1, 3, False, [], history=False)
        for r in CRITICS:
            S.critique(d, r, 2)
        gp = v2_gate(d, 2, 3, True, [], history=False)
        kinds = {v.get("kind") for v in g.get("rigor_violations") or []}
        ok(not g.get("schema_errors") and g.get("action") and "P-001" in {o["id"] for o in open_findings(d, g)}
           and not kinds & {"done_when_dropped", "critique_presentation_missing"} and gp.get("schema_errors"),
           f"T_PW_GATE round-1 gate reads round-1 presentation review; round-2 gate rejects stale round-1 "
           f"({g.get('schema_errors')}, {g.get('action')}, {sorted(kinds)}, post {bool(gp.get('schema_errors'))})")
        d = os.path.join(td, "a1_ofp")
        os.makedirs(d)
        yaml.safe_dump({"reviewer": "critique-domain", "round": 1, "blockers": [
            {"id": "D-061", "severity": "MEDIUM", "category": "METHODS", "target_stage": "WRITE", "claim": "c"},
            {"id": "D-062", "severity": "MEDIUM", "category": "METHODS", "target_stage": "ANALYZE", "claim": "c"}]},
            open(os.path.join(d, "critique_domain.yaml"), "w"))
        got = [o["id"] for o in open_findings(d, {"action": "ACCEPT", "rigor_violations": []})]
        ok(got == ["D-062"], f"T_OF_PRES a WRITE-targeted finding waits for the draft ({got})")

    def ids():
        class B:
            def __getattr__(self, n):
                return getattr(toolkit, n)

            def gate_findings(self, rd):
                return []

            def spec_violations(self, rd):
                return [{"kind": "spec_x", "key": "k1", "severity": "HIGH", "claim": "c"}]

            def registry_violations(self, rd):
                return [{"kind": "reg_y", "key": "k2", "severity": "HIGH", "claim": "c"}]
        d, T, st = new_run("a1_ids", {})
        got = mk(d, T, st, backend=B()).harness_ids(d)
        ok({"spec_x:k1", "reg_y:k2"} <= got, f"F_HARNESS_IDS spec and registry findings are harness ids ({sorted(got)})")

    def rethink():
        # T_RETHINK_SPEC_TRIES (5.1, 5.2): unsigned specs only; a signed one pauses for the owner.
        class ST(StubTasks):
            def draft_spec(self, q, rd, fb):
                self.fbs = getattr(self, "fbs", []) + [fb]
                return super().draft_spec(q, rd, fb)
        sb = {"id": "D-051", "severity": "HIGH", "target_stage": "PLAN", "category": "STRUCTURAL",
              "claim": "the decision unit is too coarse", "fix_requires": "Redraft the units."}
        d, T, st = new_run("a1_rspec", {"critics": {1: {"critique-domain": [sb]}}, "structural": {1: "critique-domain"}},
                           max_rounds=2)
        st.tries["spec"] = MAX_SPEC_TRIES - 1
        save_state(d, st)
        T = ST(T.s)
        s = mk(d, T, st).run()
        fbs = T.fbs
        ok(len(fbs) >= 2 and fbs[0] is None and any("D-051" in x and "Redraft the units." in x for x in fbs[1] or []),
           f"T_RETHINK_SPEC_TRIES a rethink of an unsigned spec redrafts it with the findings ({s}, {fbs})")
        ok(any("cannot represent the question" in x for x in (fbs + [None, None])[1] or []),
           "CX1-8 a structural rethink's reasons carry the critic's description of the mismatch")

        # T_RETHINK_ENGINE_CONTEXT (5.3): the engine pick after a rethink is told the engine left and why.
        class ET(StubTasks):
            def pick_engine(self, spec, eligible, rethink=None):
                self.rth = getattr(self, "rth", []) + [rethink]
                return super().pick_engine(spec, eligible)
        blk = {"id": "D-052", "severity": "HIGH", "target_stage": "MODEL", "claim": "the engine has no age structure"}
        plan = lambda pk: {"tasks": [{"id": "T1", "findings": [f["id"] for f in pk["findings"]], "action": "rethink",   # noqa: E731
                                      "owner_state": "ENGINE_CHOICE", "brief": "pick an engine with age structure",
                                      "files": ["engine_choice.json"]}] if pk["findings"] else []}
        d, T, st = new_run("a1_reng", {"critics": {1: {"critique-domain": [blk]}}, "plan": plan}, max_rounds=2)
        T = ET(T.s)
        s = mk(d, T, st).run()
        r = T.rth + [None, None]
        ok(r[0] is None and isinstance(r[1], dict) and str(r[1].get("engine")).endswith("ToyEngine")
           and any("age structure" in w for w in r[1].get("why") or []),
           f"T_RETHINK_ENGINE_CONTEXT the second engine pick gets the engine left and the reasons ({s}, {T.rth})")

    def claims():
        # T_CLAIM_WS (6.1): [CLAIM: id ] is the reference the renderer accepts; it is not an unresolved one.
        # T_CLAIM_ISOLATE (6.2, 6.3): one invalid claim is its own finding, and the valid claims still render.
        led = [{"id": "a.good", "claim_kind": "scalar", "value": 5}]
        bad = {"id": "b.ci", "claim_kind": "scalar", "value": 5, "ci_low": 6, "ci_high": 7}
        out = {}
        for name, cl in (("ws", led), ("iso", led + [bad])):
            d = os.path.join(td, "a1_claims_" + name)
            os.makedirs(os.path.join(d, "models"))
            yaml.safe_dump({"claims": cl}, open(os.path.join(d, "models", "claims_ledger.yaml"), "w"))
            open(os.path.join(d, "manuscript.md"), "w").write("# Report\n\nIt averts [CLAIM: a.good ] cases.\n")
            open(os.path.join(d, AB.SPEC_FILE), "w").write("{}\n")
            render_report(d, NS(headline=lambda rd: {}))
            out[name] = ({(f["kind"], f.get("key")) for f in report_claim_findings(d)},
                         open(os.path.join(d, "report.md")).read())
        ok(not {k for k, _ in out["ws"][0]} & {"report_claim_unresolved", "report_claim_unrendered", "report_claim_id_raw"},
           f"T_CLAIM_WS a reference with spaces around its id is resolved ({out['ws'][0]})")
        ok(("report_claim_invalid", "b.ci") in out["iso"][0] and "[CLAIM" not in out["iso"][1]
           and not {k for k, _ in out["iso"][0]} & {"report_claims_unreadable", "report_claim_unrendered"},
           f"T_CLAIM_ISOLATE an invalid claim is one finding; the valid claims render ({out['iso'][0]})")

    # Narrow Codex review of #297, round 1 (each failed on 650b2417).
    def r1_resume():
        # CX1-3: a presentation review that spent the last of the cap, then a crash before the transition.
        d, T, st = new_run("a1_cx3", {}, budget=20.0)
        st.state, st.write_mode, st.spent_usd = "WRITE", "accepted", 20.0
        st.journal["WRITE#0:accepted"] = {"task": "write", "status": "completed", "ok": True}
        st.journal["WRITE#0:r1:presentation"] = {"task": "critique", "status": "completed", "ok": True}
        open(os.path.join(d, "manuscript.md"), "w").write("# Report\n")
        save_state(d, st)
        mk(d, T, st).step()
        s = load_state(d)
        ok(s.state == "GATE" and s.forced_status is None and T.calls == [],
           f"CX1-3 a completed presentation review is replayed on resume with the cap spent ({s.state}, {s.status})")

    def r1_readback():
        d, T, st = new_run("a1_cx4", {}, budget=20.0)
        st.spent_usd = 16.5
        st.journal["k1"] = {"task": "sign_spec", "status": "started", "allowance_usd": 3.0, "readback_usd": 0.25,
                            "writeup": True}
        save_state(d, st)
        m = mk(d, T, st)
        ok(abs(m.st.spent_usd - 19.75) < 1e-9, f"CX1-4 an interrupted write-up task is charged its read-back too "
                                                f"({m.st.spent_usd})")
        d, T, st = new_run("a1_cx4b", {}, budget=20.0)
        st.spent_usd = 16.5
        save_state(d, st)
        m = mk(d, T, st)
        m._task("k1", "sign_spec", d, writeup=True)
        ok(m.st.journal["k1"].get("readback_usd") == READBACK_CAP_USD,
           f"CX1-4b a write-up task's journal entry records its read-back allowance ({m.st.journal['k1']})")

    def r1_open():
        # CX1-5: a rendered report without its manuscript is a draft too (the gate's predicate): nothing is deferred.
        d = os.path.join(td, "a1_cx5")
        os.makedirs(d)
        yaml.safe_dump({"reviewer": "critique-domain", "round": 1, "blockers": [
            {"id": "D-061", "severity": "MEDIUM", "category": "METHODS", "target_stage": "WRITE", "claim": "c"}]},
            open(os.path.join(d, "critique_domain.yaml"), "w"))
        open(os.path.join(d, "report.md"), "w").write("# Report\n")
        got = [o["id"] for o in open_findings(d, {"action": "ACCEPT", "rigor_violations": []})]
        ok(got == ["D-061"], f"CX1-5 with report.md and no manuscript.md a WRITE finding is not deferred ({got})")

    def r1_rethink():
        # CX1-7: a fix plan file that is not a plan does not stop a back-edge; a gate's own back-edge reads none.
        d, T, st = new_run("a1_cx7", {})
        st.state = "GATE"
        save_state(d, st)
        open(os.path.join(d, "fix_plan_r1.json"), "w").write('{"plan": ["stale"]}')
        m = mk(d, T, st)
        m._rethink("ENGINE_CHOICE", "test", planned=True)
        ok(load_state(d).state == "ENGINE_CHOICE", "CX1-7 a malformed fix plan does not stop a RETHINK back-edge")

    def r1_claims():
        d = os.path.join(td, "a1_cx2")
        os.makedirs(os.path.join(d, "models"))
        yaml.safe_dump({"claims": [{"id": "a.good", "claim_kind": "scalar", "value": 5},
                                   {"id": "b.unit", "claim_kind": "label", "value": "case"},
                                   {"id": "b.ci", "claim_kind": "scalar", "value": 5, "ci_low": 6, "ci_high": 7}]},
                       open(os.path.join(d, "models", "claims_ledger.yaml"), "w"))
        open(os.path.join(d, "manuscript.md"), "w").write("# Report\n\na.good[CLAIM:b.unit]s and [CLAIM:b.ci].\n")
        got = {(f["kind"], f.get("key")) for f in report_claim_findings(d)}
        ok(("report_claim_id_raw", "a.good") in got, f"CX1-2 a bare id beside a reference is still reported ({got})")
        ok(("report_claim_invalid", "b.ci") in got and ("report_claim_unresolved", "b.ci") not in got,
           f"CX1-9 a reference to an invalid claim is reported once, as invalid ({got})")

    # Narrow Codex review of #297, round 2 (each failed on b148196a).
    def r2_machine():
        d, T, st = new_run("a1_cx21", {})                    # CX2-1: no cap
        m = mk(d, T, st)
        m._task("k1", "sign_spec", d, writeup=True)
        ok(m.st.journal["k1"].get("readback_usd") == READBACK_CAP_USD,
           f"CX2-1 without a cap a write-up task's read-back allowance is recorded too ({m.st.journal['k1']})")
        for name, setup, want in (
                ("CX2-2 an unreadable ledger does not stop a RETHINK back-edge",
                 lambda d: _put(d, FL.LEDGER, "not json\n"), None),
                ("CX2-3 a structural mismatch any reviewer reports is among the reasons",
                 lambda d: yaml.safe_dump({"structural_mismatch": {"detected": True, "description": "no age classes"},
                                           "blockers": [{"id": "M-9", "category": "STRUCTURAL", "claim": "needs ages"}]},
                                          open(os.path.join(d, "critique_methods.yaml"), "w")),
                 "M-9: needs ages")):       # round 3 (5): the blocker's own text, whatever the ledger holds
            d, T, st = new_run("a1_" + name[:5].replace("-", ""), {})
            st.state = "GATE"
            save_state(d, st)
            setup(d)
            try:
                mk(d, T, st)._rethink("ENGINE_CHOICE", "test")
            except Exception as e:  # noqa: BLE001
                ok(False, f"{name}: {type(e).__name__}: {e}")
                continue
            s = load_state(d)
            why = (s.tries.get("rethink_engine") or {}).get("why") or []
            ok(s.state == "ENGINE_CHOICE" and (want is None or any(want in w for w in why)), f"{name} ({why})")

    def r2_findings():
        # CX2-4: with report.md alone the presentation review's findings are open findings, as its file is read.
        d = os.path.join(td, "a1_cx24")
        os.makedirs(os.path.join(d, "models"))
        yaml.safe_dump({"reviewer": "critique-presentation", "round": 1, "blockers": [
            {"id": "P-002", "severity": "MEDIUM", "category": "METHODS", "target_stage": "WRITE", "claim": "c"}]},
            open(os.path.join(d, "critique_presentation.yaml"), "w"))
        open(os.path.join(d, "report.md"), "w").write("# Report\n")
        got = [o["id"] for o in open_findings(d, {"action": "ACCEPT", "rigor_violations": []})]
        ok(got == ["P-002"], f"CX2-4 with report.md alone the presentation review's findings are read ({got})")
        # CX2-9: a reference broken over two lines is not one the renderer accepts: still reported.
        yaml.safe_dump({"claims": [{"id": "a.good", "claim_kind": "scalar", "value": 5}]},
                       open(os.path.join(d, "models", "claims_ledger.yaml"), "w"))
        open(os.path.join(d, "manuscript.md"), "w").write("# Report\n\nIt averts [CLAIM:\na.good] cases.\n")
        os.remove(os.path.join(d, "report.md"))
        got = {f["kind"] for f in report_claim_findings(d)}
        ok(got & {"report_claim_unresolved", "report_claim_id_raw"},
           f"CX2-9 a reference broken over two lines is still reported ({got})")
        # CX3-6 (round 3): an open finding whose test regressed is listed with its regression, as before the key.
        yaml.safe_dump({"reviewer": "critique-domain", "round": 1, "blockers": [
            {"id": "D-071", "severity": "MEDIUM", "category": "METHODS", "target_stage": "ANALYZE", "claim": "c"}]},
            open(os.path.join(d, "critique_domain.yaml"), "w"))
        got = [o["id"] for o in open_findings(d, {"action": "PATCH_OR_RETHINK", "rigor_violations": [
            {"kind": "done_when_regressed", "key": "D-071", "severity": "HIGH", "claim": "c"}]})]
        ok("done_when_regressed:D-071" in got and "D-071" in got, f"CX3-6 a regression is listed beside its finding ({got})")

    def r3_presentation():
        # CX3-1: a failed presentation task in WRITE goes to GATE, live and replayed alike.
        class NoReview(StubTasks):
            def critique(self, run_dir, reviewer, round_n, feedback=None):
                return self._r(f"critique:{reviewer}:{round_n}", False, note="no review written")
        states = []
        for name, journal in (("live", None), ("replay", {"task": "critique", "status": "completed", "ok": False})):
            d, T, st = new_run("a1_cx31" + name, {}, budget=20.0)
            st.state, st.write_mode, st.spent_usd = "WRITE", "accepted", 20.0 if journal else 16.5
            if journal:
                st.journal["WRITE#0:accepted"] = {"task": "write", "status": "completed", "ok": True}
                st.journal["WRITE#0:r1:presentation"] = journal
                open(os.path.join(d, "manuscript.md"), "w").write("# Report\n")
            save_state(d, st)
            mk(d, NoReview(T.s), st).step()
            states.append(load_state(d).state)
        open(os.path.join(d, "manuscript.md"), "w").write("# Report\n")
        for r in CRITICS:
            StubTasks({}).critique(d, r, 1)
        kinds = [(v.get("kind"), v.get("severity")) for v in v2_gate(d, 1, 3, True, [], history=False).get("rigor_violations") or []]
        ok(states == ["GATE", "GATE"] and ("critique_presentation_missing", "MEDIUM") in kinds,
           f"CX3-1 a failed presentation task goes to the gate, live and replayed alike; the gate reports the missing "
           f"review ({states}, {kinds})")

    for name, fn in (("T_PRES_BUDGET", budget), ("T_PW_GATE", post_write), ("F_HARNESS_IDS", ids),
                     ("T_RETHINK", rethink), ("T_CLAIM", claims), ("CX1-3", r1_resume), ("CX1-4", r1_readback),
                     ("CX1-5", r1_open), ("CX1-7", r1_rethink), ("CX1-2", r1_claims), ("CX2-1", r2_machine),
                     ("CX2-4", r2_findings), ("CX3-1", r3_presentation)):
        case(name, fn)


def _self_test_writeup_rounds(ok, td: str, toolkit, new_run) -> None:
    """Harness baseline hb1 (ranked fixes 6 and 7): the rounds a run spent with nothing to fix.

    WR1-WR3 (cause G; dengue R1 and R2, measles R1, tb R2): a write-up-only fix went FIX_LOOP -> WRITE with the round
    raised, and the post-write gate refused the domain and red-team files as a stale round ("round must be N, got
    N-1"), re-running them through the schema repair. They now re-review the draft as a route of their own.
    WR4-WR7 (cause H; malaria R2, measles R2 round 2): the medium rule sent a finding the owner (or the lead,
    provisionally) had accepted to the fix loop, which had no task for it, and two more critique rounds ran."""
    from v2_tasks import StubTasks

    def mk(d, T, st):
        return Machine(d, T, st, backend=toolkit, log=lambda *a: None, cross_engine_check=False)

    def hist(d):
        return [h for h in load_state(d).history if "to" in h]

    def case(name, fn):
        try:
            fn()
        except Exception as e:  # noqa: BLE001 -- before the fix exists this is the failure
            import traceback
            traceback.print_exc()
            ok(False, f"{name}: {type(e).__name__}: {e}")

    def writeup_rereview():
        # WR1-WR3: a HIGH write-up finding: ACCEPT pre-write, the post-write gate patches it, the writer revises
        wup = {"id": "R-030", "severity": "HIGH", "target_stage": "WRITE", "claim": "the report overstates the effect",
               "done_when": [{"file": "manuscript.md", "contains": "(?i)effect is bounded"}]}
        d, T, st = new_run("wr_writeup_rev", {"critics": {1: {"critique-redteam": [wup]}}}, max_rounds=3)
        s = mk(d, T, st).run()
        h = hist(d)
        edges = [(x["from"], x["to"]) for x in h]
        whys = [str(x.get("why")) for x in h]
        st2 = load_state(d)
        ok(("FIX_LOOP", "WRITE") in edges and ("WRITE", "CRITIQUE") in edges
           and not any(w.startswith("schema errors") for w in whys),
           f"WR1 a write-up-only fix goes WRITE -> CRITIQUE, never through a schema repair "
           f"({s}, {edges[-8:]}, {[w for w in whys if 'schema' in w]})")
        i = edges.index(("WRITE", "CRITIQUE"))
        r = h[i]["round"]
        ok(f"critique:critique-domain:{r}" in T.calls
           and f"critique:critique-redteam:{r}" in T.calls and edges[i + 1] == ("CRITIQUE", "GATE")
           and h[i + 1]["round"] == r,
           f"WR2 the re-review runs the domain and red-team critics at the revised draft's round, then the gate "
           f"({whys[i]!r}, round {r}, {T.calls[-6:]})")
        ok("schema" not in (st2.tries or {}),
           f"WR3 the re-review uses no schema repair ({st2.tries})")

    def writeup_rereview_not_accepted():
        # WR1b: a revised not-accepted draft when domain/redteam are behind round goes WRITE -> CRITIQUE -> GATE
        import shutil
        src = os.path.join(td, "wr_writeup_rev")
        d = os.path.join(td, "wr_writeup_rev_na")
        shutil.copytree(src, d)
        T, st = StubTasks({}), load_state(d)
        st.state, st.round, st.post_write, st.round_pending, st.write_mode = "WRITE", 2, False, False, "not_accepted"
        st.status, st.forced_status = None, None
        save_state(d, st)
        for r in CRITICS:
            StubTasks({}).critique(d, r, 1)
        m = mk(d, T, st)
        for _ in range(3):
            m.step()
        h = hist(d)
        e = [(x["from"], x["to"]) for x in h]
        ok(e[-3:-1] == [("WRITE", "CRITIQUE"), ("CRITIQUE", "GATE")] and e[-1][0] == "GATE"
           and "critique:critique-domain:2" in T.calls and "critique:critique-redteam:2" in T.calls
           and not os.path.exists(os.path.join(d, "critique_presentation.yaml"))
           and not str(h[-1].get("why")).startswith("schema errors"),
           f"WR1b a revised not-accepted draft goes WRITE -> CRITIQUE -> GATE ({e[-3:]}, {h[-1].get('why')!r}, {T.calls})")

    def owner_held_run():
        # WR4-WR6: malaria R2 in miniature. A MEDIUM finding whose remedy is an owner amendment, with a test that fails;
        # the lead accepts it provisionally. It is declared at once: no fix loop with no task, no second critique round.
        dw = [{"file": "analysis_settings.yaml", "key": "uncertainty.draws", "op": ">=", "value": 9999}]
        amend = {"id": "D-001", "severity": "MEDIUM", "target_stage": "PLAN", "claim": "net durability is untested",
                 "fix_requires": "Owner amendment: add a durability decision test.", "done_when": dw}
        d, T, st = new_run("wr_owner_held", {"critics": {1: {"critique-domain": [amend]}}}, signoff="lead",
                           max_rounds=3)
        s = mk(d, T, st).run()
        h = hist(d)
        to = [x["to"] for x in h]
        crit = [c for c in T.calls if c.startswith("critique:critique-domain")]
        ok(s == "completed_scope_declared" and "FIX_LOOP" not in to and crit == ["critique:critique-domain:1"],
           f"WR4 an owner-accepted finding is declared, not sent to a fix loop with no task ({s}, {to[-8:]}, {crit})")
        ok(to.count("OWNER_DECISION") == 1,
           f"WR5 the same finding (same fingerprint) is asked once ({to.count('OWNER_DECISION')} request(s))")
        # WR5b (hb2 fix 3, malaria R2 D-002): the critic carries the answered finding into round 2 reworded, beside a
        # HIGH a fix closed in round 1; the answer holds through the rewording: asked once, declared in round 2
        hi = {"id": "D-009", "severity": "HIGH", "target_stage": "ANALYZE", "claim": "too few draws",
              "done_when": [{"file": "analysis_settings.yaml", "key": "uncertainty.draws", "op": ">=", "value": 32}]}

        import yaml

        class _Reword(StubTasks):
            def critique(self, run_dir, reviewer, round_n, feedback=None):
                res = super().critique(run_dir, reviewer, round_n, feedback)
                p_ = os.path.join(run_dir, "critique_domain.yaml")
                if reviewer == "critique-domain" and os.path.exists(p_):
                    doc = yaml.safe_load(open(p_))
                    for b in doc.get("blockers") or []:
                        if b.get("id") == "D-001":
                            b["claim"] = f"net durability is untested; the lead accepted it before (round {round_n})"
                    yaml.safe_dump(doc, open(p_, "w"), sort_keys=False)
                return res
        d5, T5, st5 = new_run("wr_owner_reworded", {"critics": {1: {"critique-domain": [amend, hi]}},
                                                    "fixes": {"D-009": [("analysis_settings.yaml",
                                                                         "uncertainty.draws", 32)]}},
                              signoff="lead", max_rounds=3)
        tk5 = _Reword(T5.s)
        s5 = mk(d5, tk5, st5).run()
        to5 = [x["to"] for x in hist(d5)]
        crit5 = [c for c in tk5.calls if c.startswith("critique:critique-domain")]
        rounds5 = load_state(d5).round
        ok(to5.count("OWNER_DECISION") == 1 and s5 == "completed_scope_declared" and rounds5 == 2
           and "(round 2)" in FL.load(d5)["D-001"].claim
           and FL.load(d5)["D-001"].status == "owner_accepted",
           f"WR5b a reworded re-raise of an answered finding is not asked again ({to5.count('OWNER_DECISION')} "
           f"request(s), {s5}, round {rounds5}, {to5[-10:]}, {crit5})")
        dec = yaml.safe_load(open(os.path.join(d, DECLARED_FILE))) or {}
        g = json.load(open(os.path.join(d, "final_gate.json")))
        ok([x.get("id") for x in dec.get("declared") or []] == ["D-001"] and g.get("rule_matched") == "3m"
           and "owner" in str(g.get("rationale")),
           f"WR6 the write-up declares it, and the gate says the owner answered it ({dec.get('declared')}, "
           f"{g.get('rule_matched')}, {g.get('rationale')!r})")
        # T_LEAD_NO_CRITIC_HIGH (owner policy 2026-11-24): under --signoff lead, an open critic HIGH
        # finding whose remedy is an owner amendment is NEVER provisionally accepted by the lead — it pauses for the
        # owner at OWNER_DECISION, and releases once the owner answers.
        import scope_signoff as _SS_hi
        amend_hi = {"id": "D-002", "severity": "HIGH", "target_stage": "PLAN",
                    "claim": "the recommended plan leaves part of the fixed supply unallocated",
                    "fix_requires": "Owner amendment: require the full supply to be allocated.", "done_when": dw}
        dh, Th, sth = new_run("wr_lead_critic_high", {"critics": {1: {"critique-domain": [amend_hi]}}},
                              signoff="lead", max_rounds=2)
        sh1 = mk(dh, Th, sth).run()
        meta_h = json.load(open(os.path.join(dh, "metadata.json")))
        req_h = json.load(open(os.path.join(dh, "scope_signoff_request.json")))
        ok(sh1 == "paused:awaiting_scope_signoff" and load_state(dh).state == "OWNER_DECISION"
           and [i.get("finding") for i in req_h.get("items") or []] == ["D-002"]
           and req_h["items"][0].get("severity") == "HIGH"
           and "critic HIGH" in str(meta_h.get("paused_why") or "")
           and not _SS_hi.provisional(dh),
           f"T_LEAD_NO_CRITIC_HIGH lead mode refuses to provisionally accept an open critic HIGH finding "
           f"({sh1}, why={meta_h.get('paused_why')!r}, prov={_SS_hi.provisional(dh)})")
        # Direct call to scope_signoff.provisional_answers with a critic HIGH item also fails closed
        try:
            _SS_hi.provisional_answers(dh, req_h["items"], 1)
            ok(False, "T_LEAD_NO_CRITIC_HIGH_SS scope_signoff.provisional_answers refuses a critic HIGH item")
        except ValueError as e:
            ok("critic HIGH" in str(e), f"T_LEAD_NO_CRITIC_HIGH_SS scope_signoff refuses critic HIGH ({e})")
        _SS_hi.sign(dh, "owner", all_decision="accept", note="owner accepts D-002")
        sh2 = mk(dh, StubTasks(Th.s), None).run()
        ok(sh2 == "completed_scope_declared",
           f"T_LEAD_NO_CRITIC_HIGH_RESUME owner sign releases OWNER_DECISION in a lead run ({sh2})")
        # Codex r1 P1-1 & P1-2: stale/unknown request severity never overrides a ledger HIGH, and a prior lead MEDIUM
        # acceptance does not survive escalation to HIGH
        led_hi = {"D-002": FL.Finding(id="D-002", kind="critique_blocker", severity="HIGH", source="critique-domain",
                                      status="open")}
        ok(not _lead_may_accept({"finding": "D-002", "kind": "critique_blocker", "severity": "MEDIUM"}, led_hi)
           and not _lead_may_accept({"finding": "D-002", "kind": "critique_blocker", "severity": "unknown"}, led_hi)
           and _lead_may_accept({"finding": "D-001", "kind": "critique_blocker"}, {
               "D-001": FL.Finding(id="D-001", kind="critique_blocker", severity="MEDIUM", source="critique-domain")}),
           "T_LEAD_NO_CRITIC_HIGH_GUARD stale/unknown severity refused; legacy MEDIUM request allowed via ledger")
        esc_hi = {"id": "D-001", "severity": "HIGH", "claim": "net durability is untested",
                  "fix_requires": "Owner amendment: add a durability decision test."}
        ok(owner_accepted_now(os.path.join(td, "wr_owner_held"), [esc_hi], True) == set(),
           "T_LEAD_NO_CRITIC_HIGH_ESC prior lead MEDIUM acceptance does not hold if finding escalates to HIGH")

    def pending_post_write():
        # WR9 (Codex r2 of #344, P1-1): the owner accepted D-001 pre-write; the presentation review raises P-001, whose
        # remedy is an owner amendment. The post-write gate asks the owner before it publishes a final disposition:
        # no final_gate.json, and the report's status is pending, while P-001 waits.
        import scope_signoff as _SS
        dw = [{"file": "analysis_settings.yaml", "key": "uncertainty.draws", "op": ">=", "value": 9999}]
        amend = {"id": "D-001", "severity": "MEDIUM", "target_stage": "PLAN", "claim": "durability untested",
                 "fix_requires": "Owner amendment: add a durability test.", "done_when": dw}
        pres = {"id": "P-001", "severity": "MEDIUM", "target_stage": "WRITE", "claim": "the comparator label is unclear",
                "fix_requires": "Owner amendment: rename the comparator in the spec.",
                "done_when": [{"file": "manuscript.md", "contains": "(?i)funded plan comparator"}]}
        d, T, st = new_run("wr_pending_pw", {"critics": {1: {"critique-domain": [amend],
                                                            "critique-presentation": [pres]}}}, max_rounds=1)
        s1 = mk(d, T, st).run()
        _SS.sign(d, "owner", all_decision="accept", note="WR9")
        json.dump({"action": "DECLARE_SCOPE", "stale": True}, open(os.path.join(d, "final_gate.json"), "w"))
        s2 = mk(d, StubTasks(T.s), None).run()
        rep = open(os.path.join(d, "report.md")).read() if os.path.exists(os.path.join(d, "report.md")) else ""
        req = json.load(open(os.path.join(d, "scope_signoff_request.json")))
        ok(s1 == s2 == "paused:awaiting_scope_signoff" and [i.get("finding") for i in req.get("items") or []] == ["P-001"]
           and not os.path.exists(os.path.join(d, "final_gate.json")) and "completed_scope_declared" not in rep
           and "P-001" not in open(os.path.join(d, DECLARED_FILE)).read(),
           f"WR9 a post-write gate with a finding awaiting the owner publishes no final disposition ({s1}, {s2}, "
           f"final_gate: {os.path.exists(os.path.join(d, 'final_gate.json'))}, declared status in report: "
           f"{'completed_scope_declared' in rep})")

    def answer_binds_current_text():
        # WR10 (Codex r2 of #344, P1-2): the owner's accept holds a finding only for the claim and remedy it was given
        # for, as the gate reads them now
        d = os.path.join(td, "wr_owner_held")          # WR4's run: the lead accepted D-001 as stated
        o = {"id": "D-001", "claim": "net durability is untested",
             "fix_requires": "Owner amendment: add a durability decision test."}
        ok(owner_accepted_now(d, [o], True) == {"D-001"} and owner_accepted_now(d, [dict(o, fix_requires=None)], True)
           == set() and owner_accepted_now(d, [o], False) == set(),
           "WR10 an accept holds the finding as answered; a revised remedy, or a lead answer in an owner run, does not")

    def medium_rule_unit():
        # WR7: the rule itself. A tested finding the owner holds is declared; a tested one the fix loop can work to is
        # still patched while rounds remain.
        op = [{"id": "D-001", "tested": True, "severity": "MEDIUM", "source": "critique-domain", "claim": "c"}]
        acc = {"action": "ACCEPT", "rule_matched": "4"}
        d1 = apply_medium_rule(acc, op, 2, True, owner_held={"D-001"})
        d2 = apply_medium_rule(acc, op, 2, True)
        d3 = apply_medium_rule(acc, op + [dict(op[0], id="R-002")], 2, True, owner_held={"D-001"})
        ok(d1["action"] == "DECLARE_SCOPE" and d1["rule_matched"] == "3m" and d2["action"] == "PATCH_OR_RETHINK"
           and d3["action"] == "PATCH_OR_RETHINK" and d3["open_tested"] == ["R-002"],
           f"WR7 the medium rule never sends an owner-held finding to the fix loop ({d1['action']}, {d2['action']}, "
           f"{d3.get('open_tested')})")
        led = {"A": FL.Finding(id="A", kind="critique_blocker", severity="MEDIUM", source="critique-domain",
                               status="owner_accepted", resolution_type="owner_decision"),
               "B": FL.Finding(id="B", kind="critique_blocker", severity="MEDIUM", source="critique-domain",
                               status="open", fix_requires="Owner amendment: x"),
               "C": FL.Finding(id="C", kind="critique_blocker", severity="MEDIUM", source="critique-domain",
                               status="open", fix_requires="re-run the batch"),
               "E": FL.Finding(id="E", kind="critique_blocker", severity="MEDIUM", source="critique-domain",
                               status="owner_declined", resolution_type="owner_decision")}
        ok(owner_held(led) == {"A"}, f"WR7b owner-held: answered and accepted only; one awaiting the owner's answer is "
                                     f"not declared before it (Codex r1 of #344, P1-1) ({sorted(owner_held(led))})")
        # WR7c (Codex r1 of #344, P1-1): post-write, an unanswered amendment is not declared (no DECLARE_SCOPE is
        # published while the owner has not answered); the gate keeps it as fix-loop work and asks the owner first
        d4 = apply_medium_rule(acc, op, 2, True, owner_held=owner_held({"D-001": led["B"]}))
        ok(d4["action"] == "PATCH_OR_RETHINK", f"WR7c an unanswered amendment is not declared ({d4['action']})")

    def zero_tasks_prewrite():
        # WR8-WR9: pre-write, a fix loop with no task where every open finding the gate holds is the write-up's or
        # the owner's goes to WRITE, not to another critique round; one with an open finding a task could fix does not.
        import fix_loop as _FX
        real = _FX.run_loop
        # Codex r1 of #344: a rendered report.md counts as a draft (P1-2), and a gate that holds an unresolved HIGH (a
        # declared finding it still blocks on: P1-3) or patches by another rule (P2-4) keeps its critique round
        gate4m = {"action": "PATCH_OR_RETHINK", "rule_matched": "4m", "open_tested": ["W"], "unresolved_high": [],
                  "owner_held": ["A"], "open_tested_ids": ["W"]}
        # Codex r2 of #344: the mode follows what this gate holds, not an older owner-accepted finding (P1-4: wr_zero_wu2,
        # whose gate holds only the write-up's W), and the pre-write plan is kept apart from this round's post-write one
        # (P1-3)
        for name, extra, want, gate, report in (
                ("wr_zero_wu", None, "WRITE", gate4m, False),
                ("wr_zero_wu2", None, "WRITE", dict(gate4m, owner_held=[]), False),
                # Codex r3 of #344 (P1-1): the ledger still says A is accepted, but this gate read A as revised (not
                # held, in open_tested); and a gate record with no owner_held list (an older gate) takes no shortcut
                ("wr_zero_revised", None, "CRITIQUE", dict(gate4m, open_tested=["W", "A"], owner_held=[]), False),
                ("wr_zero_oldgate", None, "CRITIQUE", {k: v for k, v in gate4m.items() if k != "owner_held"}, False),
                ("wr_zero_fixable", "C", "CRITIQUE", gate4m, False),
                ("wr_zero_report", None, "CRITIQUE", gate4m, True),
                ("wr_zero_high", None, "CRITIQUE", dict(gate4m, unresolved_high=[{"id": "D-9", "severity": "HIGH"}]),
                 False),
                ("wr_zero_rule2", None, "CRITIQUE", dict(gate4m, rule_matched="2"), False),
                ("wr_zero_nogate", None, "CRITIQUE", None, False)):
            d, T, st = new_run(name, {}, max_rounds=3)
            if gate is not None:
                json.dump(gate, open(os.path.join(d, "gate_r1.json"), "w"))
            if report:
                open(os.path.join(d, "report.md"), "w").write("# Report\n")
            led = {"W": FL.Finding(id="W", kind="critique_blocker", severity="MEDIUM", source="critique-redteam",
                                   status="open", owner_state="WRITE", target_stage="WRITE"),
                   "A": FL.Finding(id="A", kind="critique_blocker", severity="MEDIUM", source="critique-domain",
                                   status="owner_accepted", resolution_type="owner_decision", blocking=True)}
            if extra:
                led[extra] = FL.Finding(id=extra, kind="critique_blocker", severity="MEDIUM", source="critique-domain",
                                        status="open", owner_state="ANALYZE", blocking=True)
            FL.save(d, led)
            open(os.path.join(d, "fix_plan_r1.json"), "w").write('{"round": 1, "plan": {"tasks": []}}')
            st.state, st.round, st.round_pending, st.post_write = "FIX_LOOP", 1, True, False
            save_state(d, st)
            _FX.run_loop = lambda *a, **k: {"round": 1, "tasks": 0, "rows": 0, "passed": 0, "rethink": None,
                                            "write_up": ["W"], "owner_amendments": []}
            try:
                mk(d, T, st).step()
            finally:
                _FX.run_loop = real
            s2 = load_state(d)
            last = hist(d)[-1]
            mode = "accepted" if name == "wr_zero_wu2" else "declared"
            kept = (os.path.exists(os.path.join(d, "fix_plan_r1_prewrite.json"))
                    and not os.path.exists(os.path.join(d, "fix_plan_r1.json")))
            ok(s2.state == want and (want == "CRITIQUE" or (s2.round == 1 and not s2.round_pending
                                                            and s2.write_mode == mode and kept)),
               f"WR8 pre-write, a fix loop with no task goes to {want} ({name}: {s2.state}, round {s2.round}, "
               f"pending {s2.round_pending}, mode {s2.write_mode}, {last.get('why')!r})")

    def hb2_fp_and_stale():
        # HB2-7 (Codex r3 of #357): an accept given under the earlier contract does not authorize ACCEPT (P1-1);
        # a report rendering that could not state an accepted test is read as stale, unreadable too (P1-7)
        dg = os.path.join(td, "hb2_fp_stale")
        os.makedirs(dg)
        v7 = {"kind": "decision_test_changes_plan", "key": "DT1", "claim": "c"}
        old7 = hashlib.sha256(json.dumps({"finding": FL.gate_id(v7), "claim": "c", "evidence": None}, sort_keys=True,
                                         default=str).encode()).hexdigest()[:16]
        st_missing = _render_stale(dg)
        json.dump({"dependence_stale": []}, open(os.path.join(dg, "report_render.json"), "w"))
        ok(_bearing_fp(v7, None) != old7
           and _bearing_item(dg, v7)["fingerprint"] == _bearing_fp(v7, None)
           and st_missing and _render_stale(dg) == [],
           f"HB2-7 the accept binds the disposition and a stale rendering is read ({st_missing})")

    def hb2_request_and_report():
        # HB2-5 (Codex r1 of #357, P1-4): rule 2o asks only the findings whose resolution is the owner's; a
        # policy-mapped owner item waits for the round cap
        dt_ = {"reviewer": "rigor-artifacts", "id": "RIG-001", "kind": "decision_test_changes_plan",
               "owner_signoff": True, "owner_resolution": True}
        al_ = {"reviewer": "rigor-artifacts", "id": "RIG-002", "kind": "allocation_unstable", "owner_signoff": True}
        v2o = scope_request_view({"rule_matched": "2o", "unresolved_high": [dt_, al_], "accept_blockers": [
            dict(al_, id="REQ-001")]})
        v3o = scope_request_view({"rule_matched": "3o", "unresolved_high": [dt_, al_]})
        ok([b["id"] for b in v2o["unresolved_high"]] == ["RIG-001"] and v2o["accept_blockers"] == []
           and len(v3o["unresolved_high"]) == 2,
           f"HB2-5 rule 2o requests only the owner-resolution findings ({v2o['unresolved_high']})")
        # HB2-6 (Codex r1-r3 of #357): the report's statement of an accepted dependence. Each accept binds the record
        # it was given for (its fingerprint, with the disposition); a record changed since, a toolkit result whose
        # bytes are not the bound ones or that has no bound hash is never shown as accepted
        from toolkit.analysis import contract as _CTd
        import yaml as _yd
        dd = os.path.join(td, "hb2_dependence")
        os.makedirs(os.path.join(dd, "models", "decision_tests"))
        ids13 = [f"u{i}" for i in range(1, 14)]
        _yd.safe_dump({"varied": "the unit costs of the listed cost items multiplied together", "results": [
            {"value": "case", "cost_multipliers": {"transport": 2}, "decision_changes": True, "units_changed": 13,
             "units_changed_ids": ids13, "recommendation": "A: 13 units", "point_plan_feasible": False,
             "within_solver_gap": True}]}, open(os.path.join(dd, "models", "decision_tests", "DTc.yaml"), "w"))
        for k_ in ("DTx", "DTn"):
            _yd.safe_dump({"varied": "the envelope", "results": [{"value": 9, "decision_changes": True,
                                                                   "units_changed": 1}]},
                          open(os.path.join(dd, "models", "decision_tests", f"{k_}.yaml"), "w"))
        sha_c = hashlib.sha256(open(os.path.join(dd, "models", "decision_tests", "DTc.yaml"), "rb").read()).hexdigest()
        recs = {"DTc": {"path": "models/decision_tests/DTc.yaml", "result_sha256": sha_c},
                "DTx": {"path": "models/decision_tests/DTx.yaml", "result_sha256": "0" * 64},
                "DTn": {"path": "models/decision_tests/DTn.yaml", "result_sha256": None}}
        real_rec, real_doc = _CTd.decision_test_record, _CTd._verified_doc
        _CTd.decision_test_record = lambda rd, k: recs.get(k)
        _CTd._verified_doc = lambda rd: ([], {}, None, {"reference_plan": "point_plan"})

        def row(key, by="owner", fp=None):
            v_ = {"kind": "decision_test_changes_plan", "key": key, "claim": "c",
                  "source": "toolkit/analysis/contract.py"}
            f_ = FL.Finding(id=f"decision_test_changes_plan:{key}", kind="decision_test_changes_plan", key=key,
                            severity="HIGH", source="rigor-artifacts", status="owner_accepted", claim="c")
            f_.events.append({"round": 1, "status": "owner_accepted", "by": by,
                              "record": "toolkit",
                              "fingerprint": fp or _bearing_fp(v_, _bearing_record(dd, v_)),
                              "result_sha256": (recs.get(key) or {}).get("result_sha256")})
            return f_
        try:
            FL.save(dd, {"c": row("DTc"), "x": row("DTx"), "n": row("DTn")})
            st6: list = []
            txt = "".join(dependence_lines(dd, st6))
            FL.save(dd, {"c": row("DTc", by="lead")})
            txt_l = "".join(dependence_lines(dd))
        finally:
            _CTd.decision_test_record, _CTd._verified_doc = real_rec, real_doc
        ln = {k: next((x for x in txt.splitlines() if f"**{k}**" in x), "") for k in ("DTc", "DTx", "DTn")}
        ok("counted against the point-value plan" in ln["DTc"] and "transport x2" in ln["DTc"]
           and "(" + ", ".join(ids13) + ")" in ln["DTc"] and "not settled within the solver's gap" in ln["DTc"]
           and "has changed since it was accepted" in ln["DTx"]
           and "no verified values are recorded" in ln["DTn"] and "at 9" not in ln["DTn"]
           and st6 == ["decision_test_changes_plan:DTx"]
           and "The lead accepted the plan with that dependence, provisionally" in txt_l
           and "inputs the lead accepted provisionally" in txt_l and "inputs the owner accepted" not in txt_l,
           f"HB2-6 the accepted dependence names its reference plan, the price case, every unit, an unsettled solve, "
           f"who accepted it, and never shows a changed or unbound record as accepted ({txt!r}, {txt_l!r}, {st6})")

    for name, fn in (("HB2-5-6", hb2_request_and_report), ("HB2-7", hb2_fp_and_stale), ("WR1-WR3", writeup_rereview), ("WR1b", writeup_rereview_not_accepted),
                     ("WR4-WR6", owner_held_run),
                     ("WR7", medium_rule_unit), ("WR8", zero_tasks_prewrite), ("WR9", pending_post_write),
                     ("WR10", answer_binds_current_text)):
        case(name, fn)


def _self_test_resume(ok, td: str, toolkit, new_run) -> None:
    """T58-T60 (Nigeria v2 full run, 2026-11-02: ANALYZE -> FINAL run_failed when the runner's 1-h limit killed the
    engine batch). A toy engine batch through scripts/compute.py, as a pipeline step before the toolkit's analysis."""
    import contextlib
    import io
    import yaml
    from v2_tasks import StubTasks
    quiet = lambda *a: None  # noqa: E731
    drv = os.path.join(td, "toy_batch_driver.py")
    open(drv, "w").write(_TOY_BATCH_DRIVER.format(scripts=os.path.join(REPO, "scripts")))

    class Batch:
        """The toolkit backend with a toy engine batch step first; `run` replaces the batch command."""
        def __init__(self, tmo, run=None):
            self.tmo, self.run = tmo, run

        def __getattr__(self, k):
            return getattr(toolkit, k)

        def pipeline_steps(self, spec_path, run_dir):
            steps = [dict(s) for s in toolkit.pipeline_steps(spec_path, run_dir)]
            for s in steps:
                if s["id"] == "analyze":
                    s["inputs"] = s["inputs"] + ["analysis/toy_rows.json"]
            return [{"id": "batch", "run": self.run or f"{sys.executable} {drv} {run_dir}", "inputs": [AB.SPEC_FILE],
                     "outputs": ["analysis/toy_rows.json"], "timeout_s": self.tmo}] + steps

    def toy_run(name):
        d, T, _st = new_run(name, {})
        os.makedirs(os.path.join(d, "models"), exist_ok=True)
        open(os.path.join(d, "models", "toy_fn.py"), "w").write(_TOY_BATCH_FN)
        return d, T

    # T58s (owner decision, 2026-11-17: warn, don't kill): a batch step that runs past its timeout_s (1 s here) is
    # warned about and recorded while it runs, and the run goes on to finish; nothing is killed at the soft deadline.
    import pipeline as P_
    d, T = toy_run("analyze_soft")
    os.environ.update(MA_TOY_CALLS=os.path.join(td, "toy_calls_soft.txt"), MA_TOY_SLEEP_S="1.5")
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            s0 = run_v2("toy question", d, T, signoff="none", log=quiet, backend=Batch(1), cross_engine_check=False)
    finally:
        os.environ.pop("MA_TOY_SLEEP_S", None)
    try:
        prog = json.load(open(os.path.join(d, P_.PROGRESS)))["steps"]["batch"]
        row = next(r for r in json.load(open(os.path.join(d, P_.REPORT)))["steps"] if r["id"] == "batch")
    except (OSError, ValueError, KeyError, StopIteration):
        prog, row = {}, {}
    ok(s0 in ("completed", "completed_scope_declared") and prog.get("state") == "ok"
       and prog.get("soft_deadline_s") == 1 and prog.get("backstop_s") == 24 * 3600 and row.get("over_soft_deadline")
       and "past its soft deadline" in err.getvalue(),
       f"T58s a step past its timeout_s warns, records its progress and runs on; the run finishes ({s0}, {prog}, "
       f"{row})")
    # T58: past its backstop (made 8 s here: BACKSTOP_MULTIPLE 1, no 24 h floor) the batch step is killed with 3 of
    # 6 rows done: run_failed, recorded as resumable.
    d, T = toy_run("analyze_timeout")
    calls = os.path.join(td, "toy_calls.txt")
    os.environ.update(MA_TOY_CALLS=calls, MA_TOY_SLOW_FROM="3")
    saved = (P_.BACKSTOP_MULTIPLE, P_.BACKSTOP_FLOOR_S)
    P_.BACKSTOP_MULTIPLE, P_.BACKSTOP_FLOOR_S = 1, 0.0
    try:
        s1 = run_v2("toy question", d, T, signoff="none", log=quiet, backend=Batch(8), cross_engine_check=False)
    finally:
        os.environ.pop("MA_TOY_SLOW_FROM", None)
        P_.BACKSTOP_MULTIPLE, P_.BACKSTOP_FLOOR_S = saved
    st = load_state(d)
    first = open(calls).read().split() if os.path.exists(calls) else []
    kill = next((r for r in json.load(open(os.path.join(d, P_.REPORT)))["steps"] if r["id"] == "batch"), {})
    ok(s1 == "run_failed" and st.history[-1]["from"] == "ANALYZE" and st.history[-1].get("resumable") == "timeout"
       and resumable_failure(d, st) == "timeout" and first[:3] == ["0", "1", "2"]
       and "at its backstop" in str(kill.get("error")),
       f"T58 an analysis step killed at its backstop ends run_failed, marked resumable ({s1}, {st.history[-1]}, "
       f"calls {first}, {kill.get('error')})")
    # T59: --resume (run_v2 on the same run) continues at ANALYZE with the step limit as the backend gives it now,
    # and the batch computes only the rows the killed attempt had not finished.
    s2 = run_v2("toy question", d, T, signoff="none", log=quiet, backend=Batch(120), cross_engine_check=False)
    st = load_state(d)
    again = open(calls).read().split()[len(first):]
    back = [h for h in st.history if h.get("from") == "FINAL"]
    tmo = {s["id"]: s.get("timeout_s") for s in yaml.safe_load(open(os.path.join(d, "models", "pipeline.yaml")))["steps"]}
    ok(s2 in ("completed", "completed_scope_declared") and len(back) == 1 and back[0]["to"] == "ANALYZE"
       and back[0].get("resumed_from") == "run_failed" and tmo["batch"] == 120,
       f"T59 --resume reopens the run at ANALYZE, rewrites the step limits and finishes ({s2}, {back}, {tmo})")
    ok(sorted(again) == ["3", "4", "5"],
       f"T59b the resumed batch reuses the rows the killed attempt kept and computes the rest only ({again})")
    ok(run_v2("toy question", d, T, signoff="none", log=quiet, backend=Batch(120), cross_engine_check=False) == s2
       and len([h for h in load_state(d).history if h.get("from") == "FINAL"]) == 1,
       "T59c a finished run is not reopened")
    # T60: other run_failed causes stay terminal.
    d, T = toy_run("analyze_error")
    open(os.path.join(td, "toy_fail.py"), "w").write("import sys\nsys.exit(3)\n")
    fail = Batch(30, run=f"{sys.executable} {os.path.join(td, 'toy_fail.py')}")
    s1 = run_v2("toy question", d, T, signoff="none", log=quiet, backend=fail, cross_engine_check=False)
    n = len(load_state(d).history)
    s2 = run_v2("toy question", d, T, signoff="none", log=quiet, backend=fail, cross_engine_check=False)
    st = load_state(d)
    ok(s1 == s2 == "run_failed" and len(st.history) == n and st.history[-1]["from"] == "ANALYZE"
       and "resumable" not in st.history[-1]
       and resumable_failure(d, st) is None,
       f"T60 an analysis step that fails on its own stays run_failed on resume ({s1}, {s2}, {st.history[-1]})")
    _self_test_calibration_refused(ok, td, toy_run, Batch, quiet)
    rep = {"ok": False, "errors": [], "steps": [{"id": "analyze", "status": "failed", "returncode": 1, "error": ""}]}
    causes = [analysis_failure_cause(dict(rep, steps=[dict(rep["steps"][0], returncode=rc, error=e)]))
              for rc, e in ((-9, ""), (-15, ""), (-11, ""), (1, "boom"), (None, "OSError: no such file"))]
    ok(causes == ["killed", "killed", None, None, None],
       f"T60b killed by SIGKILL/SIGTERM is resumable; a crash, an error exit or a failed start is not ({causes})")
    st_spec = RunState(question="q", state="FINAL", status="run_failed",
                       history=[{"from": "SPEC", "to": "FINAL", "why": "the spec did not validate"}])
    os.makedirs(os.path.join(d, "models"), exist_ok=True)
    json.dump(dict(rep, steps=[dict(rep["steps"][0], returncode=None, error="timed out after 9s (process group killed)")]),
              open(os.path.join(d, "models", "pipeline_report.json"), "w"))
    ok(resumable_failure(d, st_spec) is None and resumable_failure(d, RunState(
        question="q", state="FINAL", status="completed", history=[{"from": "ANALYZE", "to": "FINAL"}])) is None,
       "T60c a run_failed outside ANALYZE, or another status, is not resumable whatever the report says")

    class Unreadable(Batch):
        def pipeline_steps(self, spec_path, run_dir):
            raise ValueError("MA_COMPUTE_MAX_CORE_HOURS='lots' is not a number")
    d, T = toy_run("analyze_unreadable")
    s1 = run_v2("toy question", d, T, signoff="none", log=quiet, backend=Unreadable(30), cross_engine_check=False)
    st = load_state(d)
    ok(s1 == "run_failed" and st.history[-1]["from"] == "ANALYZE" and "MA_COMPUTE_MAX_CORE_HOURS" in st.history[-1]["why"],
       f"T60d an unreadable compute budget fails the run at ANALYZE (fail closed): {st.history[-1]}")
    # T61 (Nigeria v2 full run F3, 2026-11-02: 16 of 256 design points failed with kind infra when the autoscaler
    # retired busy workers, the analysis failed closed, and the run could not be resumed): an analysis that failed
    # only because engine rows failed with kind infra is resumable, read from the batch summary compute.py wrote.
    d, T = toy_run("analyze_infra")
    calls2 = os.path.join(td, "toy_calls_infra.txt")
    os.environ.update(MA_TOY_CALLS=calls2, MA_TOY_INFRA_FROM="3")
    try:
        s1 = run_v2("toy question", d, T, signoff="none", log=quiet, backend=Batch(120), cross_engine_check=False)
    finally:
        os.environ.pop("MA_TOY_INFRA_FROM", None)
    st = load_state(d)
    first = open(calls2).read().split() if os.path.exists(calls2) else []
    ok(s1 == "run_failed" and st.history[-1]["from"] == "ANALYZE" and st.history[-1].get("resumable") == "infra"
       and resumable_failure(d, st) == "infra" and sorted(first) == ["0", "1", "2", "3", "4", "5"],
       f"T61 an analysis that failed only on infra engine rows ends run_failed, marked resumable ({s1}, "
       f"{st.history[-1]}, calls {first})")
    s2 = run_v2("toy question", d, T, signoff="none", log=quiet, backend=Batch(120), cross_engine_check=False)
    st = load_state(d)
    again = open(calls2).read().split()[len(first):]
    back = [h for h in st.history if h.get("from") == "FINAL"]
    ok(s2 in ("completed", "completed_scope_declared") and len(back) == 1 and back[0]["to"] == "ANALYZE"
       and "infra" in back[0]["why"] and sorted(again) == ["3", "4", "5"],
       f"T61b --resume reopens the run at ANALYZE and the batch recomputes only the infra rows ({s2}, {back}, "
       f"{again})")
    # T61c: the cause is read from the batch summaries this run of the pipeline wrote: infra only.
    d = os.path.join(td, "infra_cases")
    os.makedirs(os.path.join(d, "models"), exist_ok=True)
    os.makedirs(os.path.join(d, "analysis", "batch"), exist_ok=True)
    rp = os.path.join(d, "models", "pipeline_report.json")
    sp = os.path.join(d, "analysis", "batch", "points.jsonl.summary.json")
    rep1 = {"ok": False, "errors": [], "seconds": 30.0,
            "steps": [{"id": "analyze", "status": "failed", "returncode": 1, "error": "ValueError: 512 engine results "
                       "missing"}]}

    def cause_with(kinds, age_s=0.0, summary=True):
        json.dump(rep1, open(rp, "w"))
        if os.path.exists(sp):
            os.remove(sp)
        if summary:
            json.dump({"n": 256, "n_failed": sum(kinds.values()), "failed_by_kind": kinds, "stopped": None},
                      open(sp, "w"))
            t_ = time.time() - age_s
            os.utime(sp, (t_, t_))
        return analysis_failure_cause(rep1, d)
    got = [cause_with({"infra": 16}), cause_with({"infra": 16, "model": 1}), cause_with({"stopped": 4}),
           cause_with({}), cause_with({"infra": 16}, age_s=3600), cause_with({"infra": 16}, summary=False),
           analysis_failure_cause(rep1)]
    ok(got == ["infra", None, None, None, None, None, None],
       f"T61c infra only, in a summary written during this pipeline run, is resumable; a model or stopped row, no "
       f"failed row, an older summary, no summary, or no run dir is not ({got})")
    # T62 (F6 preview, 2026-11-11): the joint calibration's bench batch lost one task to infra, in a run dir restored
    # from a checkpoint (every mtime reset); "this pipeline run's" summaries are known by the pipeline run id
    r = os.path.join(td, "restored")
    for sub in ("models", "analysis/batch", "analysis/joint_calibration/call2"):
        os.makedirs(os.path.join(r, sub), exist_ok=True)
    rep2 = dict(rep1, pipeline_run="P1")
    files = {"models/pipeline_report.json": rep2,
             "analysis/joint_calibration/bench_call3_phase1.jsonl.summary.json":
                 {"pipeline_run": "P1", "n": 42, "n_failed": 1, "failed_by_kind": {"infra": 1}},
             "analysis/joint_calibration/call2/points.jsonl.summary.json":
                 {"pipeline_run": "P1", "n": 64, "n_failed": 0, "failed_by_kind": {}},
             "analysis/batch/results.jsonl.summary.json":            # an earlier pipeline run's: never read now
                 {"pipeline_run": "P0", "n": 8, "n_failed": 1, "failed_by_kind": {"model": 1}}}

    def restored(report_age=0.0, **over):
        for rel, obj in {**files, **over}.items():
            json.dump(obj, open(os.path.join(r, rel), "w"))
            os.utime(os.path.join(r, rel), (1e9, 1e9))
        os.utime(os.path.join(r, "models", "pipeline_report.json"), (1e9 + report_age, 1e9 + report_age))
        return analysis_failure_cause(rep2, r)
    got2 = [restored(), restored(report_age=3600.0),
            restored(**{"analysis/joint_calibration/bench_call3_phase1.jsonl.summary.json":
                        {"pipeline_run": "P1", "n": 42, "n_failed": 1, "failed_by_kind": {"model": 1}}})]
    # T62b (Codex on #200): an optional batch (a sensitivity run under its own subdirectory) that lost rows to infra
    # does not make a model rejection resumable; only the primary joint-calibration batches and analysis/batch count
    ok_bench = {"pipeline_run": "P1", "n": 42, "n_failed": 0, "failed_by_kind": {}}
    sens = os.path.join(r, "analysis", "joint_calibration", "sensitivity_s1", "call1")
    os.makedirs(sens, exist_ok=True)
    json.dump({"pipeline_run": "P1", "n": 9, "n_failed": 2, "failed_by_kind": {"infra": 2}},
              open(os.path.join(sens, "points.jsonl.summary.json"), "w"))
    optional = restored(**{"analysis/joint_calibration/bench_call3_phase1.jsonl.summary.json": ok_bench})
    os.remove(os.path.join(sens, "points.jsonl.summary.json"))
    # T62c (Codex on #200): when the report has a pipeline run id, an untagged summary never counts, however new
    untagged = os.path.join(r, "analysis", "batch", "old.jsonl.summary.json")
    json.dump({"n": 4, "n_failed": 1, "failed_by_kind": {"infra": 1}}, open(untagged, "w"))
    old_only = restored(**{"analysis/joint_calibration/bench_call3_phase1.jsonl.summary.json": ok_bench})
    os.utime(untagged, None)
    old_only_fresh = analysis_failure_cause(rep2, r)
    os.remove(untagged)
    ok(optional is None and old_only is None and old_only_fresh is None,
       f"T62b-c an infra-only optional (sensitivity) batch, or an untagged summary when the report has a run id, "
       f"never makes the failure resumable: {optional} {old_only} {old_only_fresh}")
    st2 = RunState(question="q", state="FINAL", status="run_failed",
                   history=[{"from": "ANALYZE", "to": "FINAL", "why": "analysis failed"}])
    restored()
    ok(got2 == ["infra", "infra", None] and resumable_failure(r, st2) == "infra",
       f"T62 a restored run dir whose joint-calibration bench batch lost one task to infra is resumable 'infra' "
       f"(summaries matched by pipeline run id, not mtimes; an earlier run's model failure is ignored; a model "
       f"failure in this run is not resumable), so --resume reopens it at ANALYZE: {got2}")


_TOY_REFUSAL = """import json, hashlib, os, sys
rd = sys.argv[1]
os.makedirs(os.path.join(rd, "analysis"), exist_ok=True)
blocked = ("schema: analysis.blocked/1\\ndata:\\n  reason: 'calibration_ridge: the likelihood has 1 flat direction(s); 3 of "
           "4 parameter(s) sit at a bound: z:u0, z:u1, z:u2'\\n  where: calibration\\n  exception: CalibrationRefused\\n"
           "  code: calibration_ridge\\n  at_bound: [z:u0, z:u1, z:u2]\\n")
p = os.path.join(rd, "analysis", "blocked.yaml")
open(p, "w").write(blocked)
sha = hashlib.sha256(open(p, "rb").read()).hexdigest()
json.dump({"status": "blocked", "artifacts": {"analysis/blocked.yaml": {"kind": "blocked", "sha256": sha}}},
          open(os.path.join(rd, "analysis", "manifest.json"), "w"))
sys.stderr.write("toolkit.analysis.calibration.CalibrationRefused: calibration_ridge: the likelihood has 1 flat "
                 "direction(s)\\n")
sys.exit(1)
"""


def _self_test_calibration_refused(ok, td: str, toy_run, Batch, quiet) -> None:
    """CRF1-4 (eval baseline: a refused calibration ended the run at ANALYZE): it goes to the spec revision with the
    refusal and the parameters at a bound as a finding, at most MAX_CALIBRATION_REFUSALS times, then ends run_failed
    as before; conflicts.policy: stop keeps the old stop; another failure is not one."""
    drv = os.path.join(td, "toy_refusal.py")
    open(drv, "w").write(_TOY_REFUSAL)

    class Refuse(Batch):
        """The toy batch step replaced by one that refuses the calibration as the analysis does."""
        def pipeline_steps(self, spec_path, run_dir):
            steps = super().pipeline_steps(spec_path, run_dir)
            steps[0] = dict(steps[0], run=f"{sys.executable} {drv} {run_dir}")
            return steps
    refuse = Refuse(60)
    d, T = toy_run("calibration_refused")
    s1 = run_v2("toy question", d, T, signoff="none", log=quiet, backend=refuse, cross_engine_check=False)
    st = load_state(d)
    back = [h for h in st.history if h.get("from") == "ANALYZE"]
    led = FL.load(d)
    f = led.get(CALIBRATION_REFUSED)
    ok(s1 == "run_failed" and [h.get("to") for h in back] == ["SPEC"] * MAX_CALIBRATION_REFUSALS + ["FINAL"]
       and all("calibration refused (calibration_ridge" in h.get("why", "") for h in back[:-1])
       and "cap 2" in back[-1].get("why", "") and st.tries.get(CALIBRATION_REFUSED) == MAX_CALIBRATION_REFUSALS,
       f"CRF1 a refused calibration goes back to SPEC {MAX_CALIBRATION_REFUSALS} times, then ends run_failed "
       f"({s1}, {[(h.get('to'), h.get('why', '')[:60]) for h in back]})")
    ok(f is not None and f.owner_state == "SPEC" and f.severity == "HIGH" and not f.scope_declarable
       and "z:u0, z:u1, z:u2" in f.claim and "calibration.scale" in (f.fix_requires or "")
       and "calibration_ridge): calibration_ridge" not in f.claim,
       f"CRF2 the finding names the refusal, the parameters at a bound and the remedy ({f and f.claim[:160]})")
    req = rethink_request(d, st.round + 99)
    ok(any(x.get("id") == CALIBRATION_REFUSED for x in req.get("findings") or []),
       "CRF3 a signed spec's amendment request carries the refusal (the spec owns it)")
    # CRF4: conflicts.policy stop keeps today's behaviour: the first refusal ends the run
    d2, T2 = toy_run("calibration_refused_stop")
    g = globals()
    orig = g["conflicts_policy_stop"]
    g["conflicts_policy_stop"] = lambda run_dir: True
    try:
        s2 = run_v2("toy question", d2, T2, signoff="none", log=quiet, backend=refuse, cross_engine_check=False)
    finally:
        g["conflicts_policy_stop"] = orig
    back2 = [h.get("to") for h in load_state(d2).history if h.get("from") == "ANALYZE"]
    rep_other = {"ok": False, "errors": [], "steps": [{"id": "analyze", "status": "failed", "returncode": 1,
                                                       "error": "ValueError: boom"}]}
    ok(s2 == "run_failed" and back2 == ["FINAL"] and calibration_refusal(rep_other, d2) is None,
       f"CRF4 under conflicts.policy stop the first refusal ends the run; another failure is no refusal ({back2})")


def _self_test_geometry(ok, td: str) -> None:
    """IG-M1-3 (docs/design/v2-input-geometry.md §4): the input geometry check through the machine. A units table
    whose two rows carry each other's points is refused before SIGN; accepted by name, it reaches SIGN; the gate
    carries the open finding."""
    import yaml
    from v2_tasks import StubTasks
    from toolkit.spec import schema as S
    from toolkit.spec import geometry as GEO
    toolkit = AB.backend("toolkit")

    def setup(d, accept=None):
        os.makedirs(d)
        raw = yaml.safe_load(open(S.toy_spec(d)))
        os.remove(os.path.join(d, AB.SPEC_FILE))
        rows = open(os.path.join(d, "units.csv")).read().strip().split("\n")
        feats, out = [], [rows[0] + ",lon,lat"]
        for k, line in enumerate(rows[1:]):
            x = k * 0.6
            feats.append({"type": "Feature", "properties": {"fid": str(k)}, "geometry": {
                "type": "Polygon", "coordinates": [[[x, 10], [x + 0.5, 10], [x + 0.5, 10.5], [x, 10.5], [x, 10]]]}})
            j = {0: 1, 1: 0}.get(k, k)                      # units 0 and 1 carry each other's points
            out.append(f"{line},{j * 0.6 + 0.25:.3f},10.25")
        open(os.path.join(d, "units.csv"), "w").write("\n".join(out) + "\n")
        json.dump({"type": "FeatureCollection", "features": feats}, open(os.path.join(d, "b.geojson"), "w"))
        yaml.safe_dump({"boundaries": {"path": "b.geojson", "feature_id": "fid"}},
                       open(os.path.join(d, "presentation.yaml"), "w"))
        if accept:
            raw["decision_units"][GEO.ACCEPT_KEY] = accept
        st = init_run(d, "toy question", 3, "owner", "v2", None, derived_inputs=False)
        return Machine(d, StubTasks({"spec": raw}), st, backend=toolkit, log=lambda *a: None, cross_engine_check=False)
    d1 = os.path.join(td, "ig1")
    m1 = setup(d1)
    status = m1.run(max_steps=12)
    fb = load_state(d1).tries.get("spec_feedback") or []
    ok(status == "run_failed" and any(x.startswith("reciprocal_swap") and "reciprocal_swap:0+1" in x for x in fb)
       and "SIGN" not in [h.get("to") for h in load_state(d1).history],
       f"IG-M1 a units table whose rows carry each other's points is refused before SIGN ({status}; {fb[:1]})")
    # SH1 (baseline hb2 §4.6): each failed try's feedback is kept whole in its history entry (the `why` line is cut
    # short, and spec_feedback is cleared once a try passes), so a later reader can tell what each try was told.
    sh = [h for h in load_state(d1).history if h.get("from") == "SPEC" and h.get("to") == "SPEC"]
    ok(sh and all(isinstance(h.get("feedback"), list) and h["feedback"] for h in sh)
       and sh[-1]["feedback"] == fb and any(len("; ".join(h["feedback"])) > len(h["why"]) for h in sh),
       f"SH1 every SPEC retry records its try's whole feedback in history ({[len(h.get('feedback') or []) for h in sh]})")
    hf = {f["kind"]: f for f in m1.harness_findings()}
    ok(hf.get("reciprocal_swap", {}).get("severity") == "HIGH" and hf["reciprocal_swap"]["scope_declarable"] is False,
       f"IG-M3 the gate carries the open swap as a HIGH finding that cannot be declared ({sorted(hf)})")
    d2 = os.path.join(td, "ig2")
    why = "the owner checked both rows against the source"
    m2 = setup(d2, {"reciprocal_swap:0+1": why})
    m2.run(max_steps=12)
    rep = json.load(open(os.path.join(d2, GEO.ARTIFACT)))
    still = "reciprocal_swap" in {f["kind"] for f in m2.harness_findings()}
    ok(m2.st.state == "SIGN" and m2.st.paused == "awaiting_signoff"
       and [f.get("proposed") for f in rep["findings"]] == [why] and still,
       f"IG-M2a proposed by name with a reason, the spec reaches SIGN, where the owner decides; until then the gate "
       f"still carries the swap ({m2.st.state}, {m2.st.paused}, {still})")
    toolkit.sign(d2, "owner")
    m2.step()
    ok(m2.st.state == "ENGINE_CHOICE" and "reciprocal_swap" not in {f["kind"] for f in m2.harness_findings()},
       f"IG-M2b the owner's sign-off approves the proposed exception: the run goes on and the gate no longer carries "
       f"it ({m2.st.state})")
    # IG-M4 (review P1): under --signoff lead a proposed HIGH exception pauses for the owner, as a spec sign-off does
    d4 = os.path.join(td, "ig4")
    os.makedirs(d4)
    m4 = setup(os.path.join(d4, "run"), {"reciprocal_swap:0+1": why})
    m4.st.signoff = "lead"
    m4.run(max_steps=12)
    from toolkit.spec import signoff as SO
    ok(m4.st.state == "SIGN" and m4.st.paused == "awaiting_signoff" and SO.status(m4.run_dir) == "awaiting",
       f"IG-M4a --signoff lead: a HIGH geometry exception is not lead-signed; the run pauses for the owner "
       f"({m4.st.state}, {m4.st.paused}, {SO.status(m4.run_dir)})")
    toolkit.sign(m4.run_dir, "owner")
    m4.step()
    ok(m4.st.state == "ENGINE_CHOICE" and "reciprocal_swap" not in {f["kind"] for f in m4.harness_findings()},
       f"IG-M4b once the owner signs, the run goes on with the exception approved ({m4.st.state})")
    import v2_sdk_tasks as SDK
    ok({GEO.ARTIFACT, GEO.SIGNOFF_FILE} <= set(SDK.HARNESS_FILES),
       "IG-M5 the check's record and the owner's geometry sign-off are harness files no task can write")


def _self_test_derived(ok, td: str) -> None:
    """DI1, DI8 and the development flag at the gate, through the machine (docs/design/v2-derived-inputs.md §4)."""
    import shutil
    import yaml
    from v2_tasks import StubTasks
    from toolkit.spec import schema as S
    import pipeline as P
    toolkit = AB.backend("toolkit")
    flag = P.sandbox_kind() is None
    # DI1: the evidence's shape. The task writes the units table itself; the spec is refused before SIGN.
    d = os.path.join(td, "di1")
    os.makedirs(d)
    raw = yaml.safe_load(open(S.toy_spec(d)))
    os.remove(os.path.join(d, AB.SPEC_FILE))
    st = init_run(d, "toy question", 3, "none", "v2", None, derived_inputs=True, derive_unsandboxed=flag)
    status = Machine(d, StubTasks({"spec": raw}), st, backend=toolkit, log=lambda *a: None,
                     cross_engine_check=False).run(max_steps=12)
    s1 = load_state(d)
    fb = s1.tries.get("spec_feedback") or []
    ok(status == "run_failed" and any(x.startswith("units_not_derived") for x in fb)
       and "SIGN" not in [h.get("to") for h in s1.history],
       f"DI1c through the machine: a units table the task wrote is refused before SIGN ({status}; {fb[:2]})")
    # DI8: engine auto with two candidates; the second needs an attribute no derived column binds.
    mods = os.path.join(td, "di8mods")
    os.makedirs(mods)
    with open(os.path.join(mods, "di8_engines.py"), "w") as f:
        f.write("from toolkit.engines.contract import ToyEngine\n\n\nclass NeedsGroup(ToyEngine):\n"
                "    \"\"\"The toy engine with a card that gives `group` no default.\"\"\"\n")
    with open(os.path.join(mods, "attribute_binding.yaml"), "w") as f:
        yaml.safe_dump({"attribute_binding": {"x": "required", "group": "required"}}, f)
    sys.path.insert(0, mods)
    from toolkit import import_guard as _G
    _G.allow_for_test("engine", "di8_engines")             # a test engine outside toolkit/engines (allowlist)
    _G.register_test_engine("di8_engines:NeedsGroup")      # and outside the engine registry: this self-test only
    fake = os.path.join(td, "di8repo")
    os.makedirs(os.path.join(fake, "toyraw"))
    tmp = os.path.join(td, "di8src")
    os.makedirs(tmp)
    base = yaml.safe_load(open(S.toy_spec(tmp)))
    shutil.copyfile(os.path.join(tmp, "units.csv"), os.path.join(fake, "toyraw", "units_raw.csv"))
    cols = open(os.path.join(fake, "toyraw", "units_raw.csv")).readline().strip().split(",")
    staged = "data/inputs/toyraw/units_raw.csv"

    def draft(question, run_dir):
        os.makedirs(os.path.join(run_dir, "data", "derive"), exist_ok=True)
        open(os.path.join(run_dir, "data", "derive", "units.py"), "w").write(S.TOY_DERIVE_SCRIPT)
        yaml.safe_dump({"derivations": [{"id": "units", "script": "data/derive/units.py", "inputs": [staged],
                                         "outputs": ["data/derived/units.csv"],
                                         "columns": {"uses": {staged: cols}}}]},
                       open(os.path.join(run_dir, "data", "derivations.yaml"), "w"), sort_keys=False)
        r = json.loads(json.dumps(base))
        r["engine"] = {"auto": True, "candidates": ["toolkit.engines.contract:ToyEngine", "di8_engines:NeedsGroup"]}
        r["decision_units"]["csv"] = "data/derived/units.csv"
        r["decision_units"]["defaults_accepted"] = {"group": "the toy units carry no group label"}
        r["calibration"]["target_frame"] = {"band": "prev", "source": "the engine's own window metric"}
        return r
    d = os.path.join(td, "di8")
    os.makedirs(d)
    global REPO
    old_repo, REPO = REPO, fake
    try:
        st = init_run(d, "Which package per unit? Inputs: toyraw/units_raw.csv", 3, "owner", "v2", None,
                      derived_inputs=True, derive_unsandboxed=flag)
        m = Machine(d, StubTasks({"spec": draft}), st,
                    backend=toolkit, log=lambda *a: None, cross_engine_check=False)
        m.run(max_steps=4)
        sheet = open(os.path.join(d, SIGNOFF_SHEET)).read() if os.path.exists(os.path.join(d, SIGNOFF_SHEET)) else ""
        ok(m.st.state == "SIGN" and m.st.paused == "awaiting_signoff" and "NeedsGroup" in sheet
           and "engine_attribute_unbound" in sheet and "group" in sheet,
           f"DI8a engine auto: the full checks run per candidate in SPEC, before SIGN; the candidate that needs an "
           f"unbound attribute is shown ineligible with the reason ({m.st.state}, {m.st.tries.get('spec_feedback')})")
        toolkit.sign(d, "owner")
        for _ in range(6):
            if m.st.state in ("ANALYZE", "FINAL"):
                break
            m.step()
        ec = json.load(open(os.path.join(d, AB.ENGINE_CHOICE)))
        rows = {r["ref"]: r for r in ec["candidates"]}
        ng = rows.get("di8_engines:NeedsGroup") or {}
        ok(ec["engine"]["ref"] == "toolkit.engines.contract:ToyEngine" and ng.get("eligible") is False
           and any("engine_attribute_unbound" in w and "group" in w for w in ng.get("why_not") or []),
           f"DI8b engine_choice.json records that candidate as ineligible with the unbound list: {ng.get('why_not')}")
        kinds = {f["kind"]: f for f in m.harness_findings()}
        rec_kind = json.load(open(os.path.join(d, P.DERIVATIONS_RECORD)))["derivations"]["units"]["sandbox"]["kind"]
        ok(("derivation_unsandboxed" in kinds and kinds["derivation_unsandboxed"]["severity"] == "HIGH"
            and not kinds["derivation_unsandboxed"]["scope_declarable"]) if rec_kind == "none"
           else "derivation_unsandboxed" not in kinds,
           f"DI-U3 a run that used the development flag carries a HIGH non-declarable gate finding (no ACCEPT); a "
           f"sandboxed one does not (sandbox {rec_kind}; {sorted(kinds)})")
    finally:
        REPO = old_repo
        sys.path.remove(mods)


def _self_test_evidence(ok, td: str) -> None:
    """v2 autonomy 1B-ii (D1): the EVIDENCE stage before SIGN, on the toy engine whose component declares decision
    inputs (toolkit/spec/toy_engines.py ToyDossierEngine)."""
    import yaml
    from v2_tasks import StubTasks
    from toolkit.spec import evidence_dossier as ED, evidence_draft as EDR, toy_engines as TE
    TE.enable_for_test()
    toolkit = AB.backend("toolkit")
    full = ED.toy_entries()

    def setup(name, signoff, script, dossier=True):
        d = os.path.join(td, name)
        os.makedirs(d)
        raw = yaml.safe_load(open(ED.toy_dossier_spec(d, dossier=dossier)))
        for f in (AB.SPEC_FILE, ED.DOSSIER_FILE):
            if os.path.exists(os.path.join(d, f)):
                os.remove(os.path.join(d, f))
        st = init_run(d, "toy question", 3, signoff, "v2", None, derived_inputs=False)
        T = StubTasks(dict({"spec": raw}, **script))
        return Machine(d, T, st, backend=toolkit, log=lambda *a: None, cross_engine_check=False), T

    def until(m, stop=("ENGINE_CHOICE",), n=14):
        for _ in range(n):
            if m.st.state in stop or m.st.state == "FINAL" or m.st.paused:
                break
            m.step()
        return [h.get("to") for h in m.st.history if "to" in h]

    def fill(run_dir, feedback):        # the evidence task: two inputs observed or calibrated, one with no data
        doc = yaml.safe_load(open(os.path.join(run_dir, ED.DOSSIER_FILE)))["inputs"]
        k = dict(doc["parameter:k"], no_data={"reason": "no field measurement", "borrowing_rule": "the card's value"})
        return {"parameter:eff_a": full["parameter:eff_a"], "unit_attribute:x": full["unit_attribute:x"],
                "parameter:k": k}

    # T_DOSSIER_AUTO: lead sign-off, the spec names the dossier and no one wrote it: scaffold, one task, signed
    m, T = setup("ev_lead", "lead", {"evidence": fill})
    hist = until(m)
    rec = EDR.read_record(m.run_dir) or {}
    so = _signoff(m.run_dir) or {}
    ok(hist[:3] == ["EVIDENCE", "SIGN", "ENGINE_CHOICE"] and T.calls.count("draft_evidence") == 1
       and rec.get("status") == "complete" and so.get("signed_by") == "lead"
       and so.get("evidence", {}).get("dossier") == ED.DOSSIER_FILE,
       f"T_DOSSIER_AUTO under --signoff lead the run drafts the dossier before SIGN and the lead signs it ({hist}; "
       f"{T.calls}; {rec.get('status')}; {rec.get('problems')})")
    g = rec.get("gaps") or {}
    ok(g.get("no_data") == ["parameter:k", "unit_attribute:x"] and g.get("default_kept") == ["parameter:k"]
       and g.get("sources_unchecked") == ["parameter:eff_a"]
       and "provisional" in so.get("note", "") and EDR.RECORD in so.get("note", ""),
       f"T_DOSSIER_GAPS the gaps are declared in the record and the lead's note ({rec.get('gaps')}; {so.get('note')})")
    # T_SIGN_NO_REPAIR: a refused lead sign-off pauses cleanly for the owner at SIGN without auto-repair loops
    class Stale(StubTasks):
        def sign_spec(self, run_dir):
            if self.calls.count("sign_spec") == 0:
                os.remove(os.path.join(run_dir, SIGNOFF_SHEET))
            return super().sign_spec(run_dir)
    d = os.path.join(td, "ev_repair")
    os.makedirs(d)
    raw = yaml.safe_load(open(ED.toy_dossier_spec(d, dossier=False)))
    raw["evidence"] = {"dossier": ED.DOSSIER_FILE}
    os.remove(os.path.join(d, AB.SPEC_FILE))
    T = Stale({"spec": raw, "evidence": fill})
    m = Machine(d, T, init_run(d, "toy question", 3, "lead", "v2", None, derived_inputs=False), backend=toolkit,
                log=lambda *a: None, cross_engine_check=False)
    hist = until(m)
    ok(m.st.state == "SIGN" and m.st.paused == "awaiting_signoff"
       and not os.path.exists(os.path.join(d, SIGNOFF_FILE)),
       f"T_SIGN_NO_REPAIR a refused lead sign-off waits for the owner at SIGN without looping ({hist}; {T.calls})")
    # T_EVIDENCE_EXHAUSTED: a task that never supplies the evidence pauses at SIGN for the owner, unsigned
    m, T = setup("ev_none", "lead", {})
    hist = until(m)
    rec = EDR.read_record(m.run_dir) or {}
    ok(m.st.state == "SIGN" and m.st.paused == "awaiting_signoff" and T.calls.count("draft_evidence") == EDR.MAX_TRIES
       and rec.get("status") == "incomplete"
       and not os.path.exists(os.path.join(m.run_dir, SIGNOFF_FILE)),
       f"T_EVIDENCE_EXHAUSTED the check is never loosened: after {EDR.MAX_TRIES} drafts "
       f"the run waits for the owner, unsigned ({hist}; {T.calls})")
    # T_EVIDENCE_OWNER: under --signoff owner the drafted dossier is on the sheet the owner signs
    m, T = setup("ev_owner", "owner", {"evidence": fill})
    until(m)
    sheet = open(os.path.join(m.run_dir, SIGNOFF_SHEET)).read() if os.path.exists(
        os.path.join(m.run_dir, SIGNOFF_SHEET)) else ""
    ok(m.st.state == "SIGN" and m.st.paused == "awaiting_signoff" and ED.SECTION in sheet, f"T_EVIDENCE_OWNER the "
       f"owner is shown the drafted dossier on the sheet ({m.st.state}, {m.st.paused})")
    toolkit.sign(m.run_dir, "owner")
    m.step()
    ok(m.st.state == "ENGINE_CHOICE", f"T_EVIDENCE_OWNER_SIGNED once the owner signs, the run goes on ({m.st.state})")
    # T_EVIDENCE_NONE: --signoff none: the drafted dossier covers the engine at ENGINE_CHOICE (it failed there before)
    m, T = setup("ev_unsigned", "none", {"evidence": fill})
    hist = until(m, stop=("ANALYZE",))
    ok(hist[:4] == ["EVIDENCE", "SIGN", "ENGINE_CHOICE", "ANALYZE"]
       and m.st.state == "ANALYZE",
       f"T_EVIDENCE_NONE --signoff none passes ENGINE_CHOICE's coverage check with the drafted dossier ({hist})")
    # BE27: --signoff none refuses an engine.build spec at SPEC so no paid build starts unsigned
    from toolkit.engines.built import requested as _BREQ
    d_b27 = os.path.join(td, "be27_build_none")
    os.makedirs(d_b27)
    raw_b27 = yaml.safe_load(open(ED.toy_dossier_spec(d_b27, dossier=False)))
    raw_b27["engine"] = {"build": _BREQ.example_build()}
    for f in (AB.SPEC_FILE, ED.DOSSIER_FILE):
        if os.path.exists(os.path.join(d_b27, f)):
            os.remove(os.path.join(d_b27, f))
    st_b27 = init_run(d_b27, "toy build question", 3, "none", "v2", None, derived_inputs=False)
    T_b27 = StubTasks({"spec": raw_b27})
    m_b27 = Machine(d_b27, T_b27, st_b27, backend=toolkit, log=lambda *a: None, cross_engine_check=False)
    m_b27.step()
    fb27 = m_b27.st.tries.get("spec_feedback") or []
    ok(m_b27.st.state == "SPEC" and any("signed request" in str(x) for x in fb27),
       f"BE27 --signoff none refuses a build spec at SPEC ({m_b27.st.state}, {fb27})")
    # BE20b: harness_owned() refuses deliverables targeting engine_build/* or models/ext/engine_*
    ok(owned_path(d_b27, "engine_build/admission.json", harness_owned())
       and owned_path(d_b27, "models/ext/engine_x/engine.py", harness_owned())
       and not owned_path(d_b27, "allocation_out.csv", harness_owned()),
       "BE20b harness_owned() covers engine_build/* and models/ext/engine_*")
    # T_EVIDENCE_NAME: the options need inputs and the spec names no dossier: back to the draft, nothing written
    d = os.path.join(td, "ev_name")
    os.makedirs(d)
    raw = yaml.safe_load(open(ED.toy_dossier_spec(d, dossier=False)))
    os.remove(os.path.join(d, AB.SPEC_FILE))
    m = Machine(d, StubTasks({"spec": raw}), init_run(d, "toy question", 3, "lead", "v2", None, derived_inputs=False),
                backend=toolkit, log=lambda *a: None, cross_engine_check=False)
    m.step()        # hb2 (HS6): the SPEC try itself hears it, before the evidence stage
    ok(m.st.state == "SPEC" and m.st.tries.get("spec") == 1
       and any("name the evidence dossier" in x for x in m.st.tries.get("spec_feedback") or [])
       and not os.path.exists(os.path.join(d, ED.DOSSIER_FILE)),
       f"T_EVIDENCE_NAME a spec that needs a dossier and names none is redrafted with the reason "
       f"({m.st.state}; {m.st.tries.get('spec_feedback')})")
    # T_EVIDENCE_RESUME: a run signed under the earlier order (SIGN, then EVIDENCE) resumes past EVIDENCE untouched
    d = os.path.join(td, "ev_resume")
    os.makedirs(d)
    ED.toy_dossier_spec(d)
    from toolkit.spec import signoff as SO
    SO.write_sheet(d)
    toolkit.sign(d, "owner")
    before = open(os.path.join(d, ED.DOSSIER_FILE), "rb").read()
    T = StubTasks({})
    m = Machine(d, T, RunState(question="q", state="EVIDENCE", signoff="owner"), backend=toolkit, log=lambda *a: None,
                cross_engine_check=False)
    m.step()
    m.step()
    ok(m.st.state == "ENGINE_CHOICE" and T.calls == [] and open(os.path.join(d, ED.DOSSIER_FILE), "rb").read() == before
       and [h.get("to") for h in m.st.history] == ["SIGN", "ENGINE_CHOICE"],
       f"T_EVIDENCE_RESUME a spec already signed is left as signed and SIGN checks it ({m.st.state}, {T.calls})")
    # T_EVIDENCE_OWNER_MODE (Codex r1 P1-2): an owner-mode run whose spec the lead signed does not pass SIGN
    d = os.path.join(td, "ev_leadsigned")
    os.makedirs(d)
    ED.toy_dossier_spec(d)
    SO.write_sheet(d)
    toolkit.sign(d, "lead")
    m = Machine(d, StubTasks({}), RunState(question="q", state="EVIDENCE", signoff="owner"), backend=toolkit,
                log=lambda *a: None, cross_engine_check=False)
    until(m, n=4)
    ok(m.st.state == "SIGN" and m.st.paused == "awaiting_signoff",
       f"T_EVIDENCE_OWNER_MODE a lead's signature does not satisfy --signoff owner ({m.st.state}, {m.st.paused})")
    # T_STALE_NO_LEAD (Codex r1 P1-1): a signature made stale by a dossier edit is never replaced by the lead's
    d = os.path.join(td, "ev_stale")
    os.makedirs(d)
    ED.toy_dossier_spec(d)
    SO.write_sheet(d)
    toolkit.sign(d, "owner")
    rec0 = open(os.path.join(d, SIGNOFF_FILE), "rb").read()
    open(os.path.join(d, ED.DOSSIER_FILE), "a").write("# edited after signing\n")
    T = StubTasks({})
    m = Machine(d, T, RunState(question="q", state="SIGN", signoff="lead"), backend=toolkit, log=lambda *a: None,
                cross_engine_check=False)
    until(m, n=6)
    ok(m.st.paused == "awaiting_signoff" and "sign_spec" not in T.calls
       and open(os.path.join(d, SIGNOFF_FILE), "rb").read() == rec0,
       f"T_STALE_NO_LEAD the owner's stale record stays and the run waits for the owner ({m.st.state}, {T.calls})")
    # T_LOST_RECORD (Codex r1 P1-3): a run whose history records a signature is not a draft when the record is lost
    os.remove(os.path.join(d, SIGNOFF_FILE))
    st = RunState(question="q", state="EVIDENCE", signoff="lead",
                  history=[{"from": "SIGN", "to": "EVIDENCE", "why": "spec signed by the lead"}])
    T = StubTasks({})
    m = Machine(d, T, st, backend=toolkit, log=lambda *a: None, cross_engine_check=False)
    until(m, n=6)
    ok(m.st.paused == "awaiting_signoff" and T.calls == [],
       f"T_LOST_RECORD a lost sign-off record waits for the owner; nothing is redrafted or re-signed ({T.calls})")
    # T_LEGACY_SIGN (Codex r1 P2-6): an unsigned run saved at SIGN under the earlier order runs the evidence stage
    m, T = setup("ev_legacy", "owner", {"evidence": fill})
    m.st.state = "SIGN"
    m.step()
    ok(m.st.state == "EVIDENCE", f"T_LEGACY_SIGN an unsigned run at SIGN with no evidence record goes to EVIDENCE "
                                 f"({m.st.state})")
    # T_EVIDENCE_JOURNAL (Codex r1 P2-7): the drafts are counted from the journal, not a counter a crash can lose
    m, T = setup("ev_journal", "lead", {})
    m.step()                                    # SPEC -> EVIDENCE: the spec is drafted
    m.st.journal.update({f"EVIDENCE#0:try{i}": {"status": "completed", "ok": True} for i in (1, 2, 3)})
    m.step()
    ok("draft_evidence" not in T.calls and (EDR.read_record(m.run_dir) or {}).get("tries") == 3,
       f"T_EVIDENCE_JOURNAL three completed drafts in the journal: no fourth ({T.calls})")
    # T_SUPPLIED_OWNED (Codex r2 P1-5): a supplied dossier at a path the harness writes goes back to the draft
    m, T = setup("ev_owned", "lead", {})
    m.step()                                    # SPEC -> EVIDENCE
    rawo = yaml.safe_load(open(os.path.join(m.run_dir, AB.SPEC_FILE)))
    rawo["evidence"] = {"dossier": "report.md"}
    yaml.safe_dump(rawo, open(os.path.join(m.run_dir, AB.SPEC_FILE), "w"))
    m.step()
    ok(m.st.state == "SPEC" and any("report.md" in x for x in m.st.tries.get("spec_feedback") or []),
       f"T_SUPPLIED_OWNED a dossier named report.md is refused before anything is written ({m.st.state})")
    # T_SIGN_BUDGET (Codex r2 P2-3): no allowance for the lead's sign task is the budget path, not an owner pause
    m, T = setup("ev_budget", "lead", {"evidence": fill})
    until(m, stop=("SIGN",))
    m._task = lambda *a, **k: None
    m.step()
    ok(m.st.state == "FINAL" and m.st.status == "budget_exhausted",
       f"T_SIGN_BUDGET ({m.st.state}, {m.st.status}, {m.st.paused})")
    # T_DELIVERABLE_DOSSIER (Codex r1 P1-5): the deliverable may not be the dossier or the sheet
    raw = yaml.safe_load(open(os.path.join(d, AB.SPEC_FILE)))
    for name in (ED.DOSSIER_FILE, SIGNOFF_SHEET, "src[1].yaml"):     # r2 P1-1: a name that is not its own glob
        raw["evidence"] = {"dossier": "src[1].yaml"}
        raw["deliverable_file"] = {"path": name, "columns": {"unit": "id"}}
        yaml.safe_dump(raw, open(os.path.join(d, AB.SPEC_FILE), "w"))
        ok(any("owns" in p or "evidence dossier" in p for p in deliverable_problems(d)),
           f"T_DELIVERABLE_DOSSIER a deliverable named {name} is refused ({deliverable_problems(d)})")


def _self_test_claim_refs(ok, td: str, toolkit, new_run) -> None:
    """Harness baseline hb2: CR1: the harness quoted a critic's remedy holding [CLAIM:x] in the report's
    declared-findings block. CR2: the writer copied a declared remedy's placeholder [CLAIM:id]
    into Limitations. CR3: the writer named ledger ids in inline code."""
    import yaml
    from v2_tasks import StubTasks

    def case(name, fn):
        try:
            fn()
        except Exception as e:  # noqa: BLE001 -- before the fix exists this is the failure
            import traceback
            traceback.print_exc()
            ok(False, f"{name}: {type(e).__name__}: {e}")

    def bare_run(name, manuscript):
        d = os.path.join(td, name)
        os.makedirs(os.path.join(d, "models"))
        yaml.safe_dump({"claims": [{"id": "a.good", "claim_kind": "scalar", "value": 5}]},
                       open(os.path.join(d, "models", "claims_ledger.yaml"), "w"))
        yaml.safe_dump({"schema_version": 1}, open(os.path.join(d, AB.SPEC_FILE), "w"))
        open(os.path.join(d, "manuscript.md"), "w").write(manuscript)
        return d

    opened = [{"id": "R-002", "severity": "MEDIUM", "source": "critique-redteam", "tested": True,
               "claim": "The writer then cites them as [CLAIM:id].",
               "fix_requires": "Cite [CLAIM:a.good] as the regret; write `[CLAIM:a.good]` as [CLAIM:id]."}]

    def quoted():
        d = bare_run("cr1", "# Report\n\nIt averts [CLAIM:a.good] cases.\n")
        write_declared(d, opened, {"rule_matched": "3", "rationale": "rounds exhausted"})
        doc = yaml.safe_load(open(os.path.join(d, DECLARED_FILE)))
        doc["declared"][0]["required_remedy"] += " Also [CLAIM:id] and `[CLAIM:a.good]`."   # an older file, unquoted
        yaml.safe_dump(doc, open(os.path.join(d, DECLARED_FILE), "w"))
        render_report(d, toolkit)
        rep = open(os.path.join(d, "report.md")).read()
        kinds = {f["kind"] for f in report_claim_findings(d)}
        ok("[CLAIM:" not in rep and "Open findings, declared" in rep
           and not kinds & {"report_claim_unrendered", "report_claim_unresolved"},
           f"CR1 a finding the harness quotes in report.md shows no claim reference, so no write-up finding comes from "
           f"the harness's own text ({kinds}, {[x for x in rep.splitlines() if 'CLAIM' in x][:2]})")
        d2 = bare_run("cr2", "")
        write_declared(d2, opened, {"rule_matched": "3", "rationale": "rounds exhausted"})
        dec2 = yaml.safe_load(open(os.path.join(d2, DECLARED_FILE)))["declared"][0]
        ok("[CLAIM:id]" not in dec2["claim"] + dec2["required_remedy"] and "[CLAIM:a.good]" in dec2["required_remedy"],
           f"CR2 declared_findings.yaml, which the writer repeats in full, holds no reference outside the ledger; a "
           f"ledger reference stays one ({dec2})")

    def code_ids():
        d = bare_run("cr3", "# Report\n\nThe anchors `a.good` to `[CLAIM:a.good]` and ` a.good `.\n")
        render_report(d, toolkit)
        a, b = json.load(open(os.path.join(d, "report_render.json")))["writer_span"]
        body = open(os.path.join(d, "report.md")).read()[a:b]
        kinds = {f["kind"] for f in report_claim_findings(d)}
        ok(not kinds & {"report_claim_id_raw", "report_claim_unrendered"} and "a.good" not in body
           and "[CLAIM:" not in body,
           f"CR3 a known claim id or reference alone in inline code is the reference: the harness renders it "
           f"({kinds}, {body.splitlines()[-1][:120]!r})")
        for txt, want in (("It averts `[CLAIM:no.such]` cases.\n", "report_claim_unresolved"),
                          ("It averts a.good cases.\n", "report_claim_id_raw"),
                          ("```\n[CLAIM:a.good]\n```\n", "report_claim_unrendered")):
            open(os.path.join(d, "manuscript.md"), "w").write(txt)
            render_report(d, toolkit)
            got = {(f["kind"], f["severity"]) for f in report_claim_findings(d)}
            ok((want, "HIGH") in got, f"CR3b still a HIGH finding (fail closed): {txt.strip()!r} -> {got}")

    case("CR1-CR2", quoted)
    case("CR3", code_ids)

    # Codex round 1 of #353.
    def r1_units():
        k = {"a.good": {"id": "a.good", "claim_kind": "scalar", "value": 5}}
        RCL = __import__("render_claims")
        fenced = "x\n  ```\n  SELECT `a.good` FROM t\n  ```\n"
        ok(RCL.normalise_refs(fenced, k) == fenced, "CR8a an indented fenced block is code: nothing in it is normalised")
        ok(RCL.normalise_refs("Use ``a.good`` here.", k) == "Use [CLAIM:a.good] here.",
           "CR8b a known id in a double-backtick span is its reference too")
        q = RCL.quote_refs("The prefix is `[CLAIM:`. See [source](notes.md).\nNext [CLAIM:a.good] and `a.good`.", k)
        ok("[source](notes.md)" in q and "[CLAIM:" not in q and q.count("5") == 1 and "`a.good`" in q,
           f"CR8c quoting never eats a link's bracket, leaves no marker, renders a reference and leaves a recorded "
           f"identifier as it is ({q!r})")
        # Codex round 2 of #353
        for txt in ("> ```sql\n> SELECT `a.good` FROM t\n> ```\n", "  ````\n  ```\n  SELECT `a.good`\n  ````\n"):
            ok(RCL.normalise_refs(txt, k) == txt, f"CR8d a fence in a block quote, or a longer fence, is code ({txt!r})")
        k2 = dict(k, **{"uq.draws": {"id": "uq.draws", "claim_kind": "scalar", "value": 1000}})
        q15 = RCL.quote_refs("This run continues under amendment `uq.draws`.", k2)
        ok(q15 == "This run continues under amendment `uq.draws`.",
           f"CR15 an identifier the harness records is never replaced by a claim value ({q15!r})")
        q16 = RCL.quote_refs("Fix [CLAIM:[CLAIM:a.good]] now.", k) + RCL.quote_refs("[CLAIM:[CLAIM:a.good]]", k, False)
        ok("[CLAIM:" not in q16.replace("[CLAIM:a.good]", ""),
           f"CR16 a nested marker leaves no reference outside the ledger after quoting ({q16!r})")
        d = bare_run("cr9", "# Report\n\nIt averts [CLAIM:bad] cases.\n")
        yaml.safe_dump({"claims": [{"id": "bad", "claim_kind": "scalar", "value": "lots"}]},
                       open(os.path.join(d, "models", "claims_ledger.yaml"), "w"))
        render_report(d, toolkit)
        kinds = {f["kind"] for f in report_claim_findings(d)}
        ok("report_claim_invalid" in kinds
           and not (kinds & {"report_claim_unresolved", "report_claim_id_raw", "report_claim_unrendered"}),
           f"CR9 a reference to an invalid ledger entry is reported once, as invalid: no writer repair ({kinds})")
        t = StubTasks({"manuscript": "revised\n"})
        dd = bare_run("cr10", "old\n")
        t.write(dd, "accepted", findings=[{"id": "report_claim_id_raw:a.good", "claim": "c"}])
        ok("revised" in open(os.path.join(dd, "manuscript.md")).read(),
           "CR10 an ordinary WRITE visit with claim findings writes the draft; only a marked repair keeps it")

    case("CR8-CR10", r1_units)

    def units3():
        RCL = __import__("render_claims")
        k = {"a.good": {"id": "a.good", "claim_kind": "scalar", "value": 5},
             "note": {"id": "note", "claim_kind": "label", "value": "See [CLAIM:note]"}}
        lf = "- ```sql\n  SELECT `a.good` FROM t\n  ```\n\nThen `a.good`.\n"
        got = RCL.normalise_refs(lf, k)
        ok(got == lf.replace("Then `a.good`", "Then [CLAIM:a.good]"),
           f"CR21 a fence opened after a list marker is code, and the prose after it is still read ({got!r})")
        q = RCL.quote_refs("[CLAIM:note]", k)
        ok(q.upper().count("SEE") == 1 and "[CLAIM:" not in q, f"CR22 a rendered value is never substituted again ({q!r})")
    case("CR21-CR22", units3)


def _self_test_hb2_spec(ok, td: str, toolkit, new_run) -> None:
    """HS1-HS7 (baseline hb1, 2026-11-22): a SPEC try that returns no draft (the 30-turn cap) replaced the
    validation feedback, so the next try never saw the error and repeated it (tb R1, run_failed at the try cap); the
    checks stopped at the first class that failed, so each try heard one class of problem (HS4-HS6); the drafter's
    prompt did not state the two rules that cost retries in measles and dengue (HS7)."""
    from v2_tasks import StubTasks

    class Scripted(StubTasks):
        """script["tries"]: per try, "bad" (an invalid draft), "good" (a valid one), "capped" (no draft written,
        the call fails) or "capped_good" (a valid draft written, then the call fails, as at the turn cap)."""
        def draft_spec(self, q, rd, fb):
            self.fbs = getattr(self, "fbs", []) + [list(fb or [])]
            kind = (self.s["tries"] + ["capped"] * 9)[len(self.fbs) - 1]
            if kind == "capped":
                return self._r("draft_spec", False, note="max turns")
            spec = dict(self.s["spec"])
            if kind in ("bad", "bad2"):
                spec["decision_units"] = dict(spec["decision_units"], csv="missing_units_table.csv")
            if kind in ("bad2", "noobj"):
                spec.pop("objective", None)
            if kind == "outside":
                spec["decision_units"] = dict(spec["decision_units"], csv="../outside_units.csv")
            if kind == "settings_capped":       # repairs only the settings, then hits the cap
                from v2_tasks import _yaml_dump
                _yaml_dump(self.s["settings"], os.path.join(rd, "analysis_settings.yaml"), rd)
                return self._r("draft_spec", False, note="max turns")
            settings = dict(self.s["settings"], bogus_key=1) if kind == "bad_settings" else self.s["settings"]
            r = StubTasks(dict(self.s, spec=spec, settings=settings)).draft_spec(q, rd, fb)
            return self._r("draft_spec", False, note="max turns") if kind == "capped_good" else r

    def go(name, tries, cls=None):
        d, T, st = new_run(name, {})
        T = Scripted(dict(T.s, tries=tries))
        m = (cls or Machine)(d, T, st, backend=toolkit, log=lambda *a: None, cross_engine_check=False)
        for _ in range(len(tries) + 1):
            if m.st.state != "SPEC" or m.st.paused or m.st.status:
                break
            m.step()
        return m, T

    m, T = go("hs1", ["bad", "capped", "bad"])
    first = [x for x in T.fbs[1] if "missing_units_table" in x]
    ok(len(T.fbs) == 3 and first and any("missing_units_table" in x for x in T.fbs[2])
       and any("max turns" in x for x in T.fbs[2]),
       f"HS1 a try that returns no draft keeps the earlier validation feedback, and adds its own note ({T.fbs})")
    ok(sum("missing_units_table" in x for x in T.fbs[2]) == len(first),
       f"HS1b feedback carried from an earlier try is not repeated ({T.fbs[2]})")
    m, T = go("hs2", ["capped_good"])
    ok(m.st.state != "SPEC" and m.st.tries.get("spec") == 1,
       f"HS2 a valid draft written before the call failed (the turn cap) is validated and used ({m.st.state}, "
       f"{m.st.tries.get('spec_feedback')})")
    # HS4: one try hears every check class (a missing cited file and a missing section), not only the first
    m, T = go("hs4", ["bad2", "good"])
    ok(len(T.fbs) >= 2 and any("missing_units_table" in x for x in T.fbs[1])
       and any("missing section objective" in x for x in T.fbs[1]),
       f"HS4 one SPEC try reports the cited-input and the pre-sign problems together ({T.fbs[1:2]})")

    # HS5: a check class that raises is reported by name, and the other classes' problems are kept
    class GeoBoom(Machine):
        def _geometry_problems(self, rd=None):
            raise RuntimeError("geometry boom")
    m, T = go("hs5", ["bad", "good"], GeoBoom)
    ok(len(T.fbs) >= 2 and any("missing_units_table" in x for x in T.fbs[1])
       and any("geometry boom" in x for x in T.fbs[1]),
       f"HS5 a check that raises is named and does not hide the others' problems ({T.fbs[1:2]})")

    # HS6: the evidence stage's spec problems (decision inputs with no dossier named) are heard at SPEC, in the same
    # try, not one stage later (tb R2: EVIDENCE sent the spec back for 7 missing decision inputs)
    from toolkit.spec import evidence_draft as EDR
    real = EDR.assess
    try:
        EDR.assess = lambda rd, raw=None: dict(real(rd, raw), spec_problems=["the options need 7 decision input(s)"])
        m, T = go("hs6", ["good", "good"])
    finally:
        EDR.assess = real
    ok(len(T.fbs) >= 2 and any("7 decision input" in x for x in T.fbs[1]) and m.st.tries.get("spec") >= 2,
       f"HS6 the evidence stage's spec problems come back from the SPEC try ({T.fbs[1:2]}, {m.st.state})")

    # HS7: the drafter's prompt states the calibrated/fitted engine_parameters rule; a fitted parameter is never moved
    # under calibration (Codex r1 P2-7: calibration accepts kind calibrated only)
    from v2_sdk_tasks import PROMPTS
    pr = PROMPTS["draft_spec"]
    ok("engine_parameters" in pr and "calibrated" in pr and "a fitted one" in pr,
       "HS7 the draft_spec prompt says: no engine_parameters value for a calibrated or fitted parameter "
       "(a fitted one is left to the engine)")

    class Calls(Machine):
        """Records which reading checks ran."""
        def _geometry_problems(self, rd=None):
            self.ran = getattr(self, "ran", []) + ["geometry"]
            return super()._geometry_problems(rd)

        def _evidence_assess(self, rd):
            self.ran = getattr(self, "ran", []) + ["evidence"]
            return super()._evidence_assess(rd)
    # HS8 (Codex r1 P1-1): a cited file outside the run is refused, and no check reads it
    AB.write_toy_units(os.path.join(td, "outside_units.csv"), n=12)
    m, T = go("hs8", ["outside"], Calls)
    fb8 = m.st.tries.get("spec_feedback") or []
    ok(any("outside the run directory" in x for x in fb8) and not getattr(m, "ran", []),
       f"HS8 a cited file outside the run is refused and no later check reads it ({getattr(m, 'ran', [])}, {fb8})")
    # HS9 (Codex r1 P1-2): the evidence assessment (it builds the engine) runs only on a spec the pre-sign checks pass
    m, T = go("hs9", ["noobj"], Calls)
    ok("evidence" not in getattr(m, "ran", []) and any("objective" in x for x in m.st.tries.get("spec_feedback") or []),
       f"HS9 a spec the pre-sign checks refuse is not given to the evidence assessment ({getattr(m, 'ran', [])})")

    # HS10 (Codex r1 P1-3): interrupted after the task's journal entry completed, a resume reaches the same decision
    class Crash(Exception):
        pass

    class CrashOnce(Machine):
        def _task(self, *a, **k):
            r = super()._task(*a, **k)
            if not getattr(self, "crashed", False):
                self.crashed = True
                save_state(self.run_dir, self.st)
                raise Crash()
            return r
    d10, T10, st10 = new_run("hs10", {})
    T10 = Scripted(dict(T10.s, tries=["capped_good"]))
    m = CrashOnce(d10, T10, st10, backend=toolkit, log=lambda *a: None, cross_engine_check=False)
    try:
        m.step()
    except Crash:
        pass
    m2 = Machine(d10, T10, load_state(d10), backend=toolkit, log=lambda *a: None, cross_engine_check=False)
    m2.step()
    ok(m2.st.state == "EVIDENCE" and len(T10.fbs) == 1,
       f"HS10 a resume after the completed task replays it and still checks the draft it wrote ({m2.st.state}, "
       f"{m2.st.tries.get('spec_feedback')})")
    # HS11 (Codex r1 P1-4): a capped try that repaired only the settings has its draft checked again
    m, T = go("hs11", ["bad_settings", "settings_capped"])
    ok(any("bogus_key" in x for x in T.fbs[1]) and m.st.state == "EVIDENCE",
       f"HS11 a capped try that changed only the settings is checked, and goes on ({m.st.state}, "
       f"{m.st.tries.get('spec_feedback')})")
    # HS14 (Codex r2 P1-2): when containment of the cited paths cannot be established, that is a problem, never a pass
    import pipeline as _P
    real_cp, n_cp = _P.cited_paths, []

    def flaky(*a, **k):
        n_cp.append(1)
        if len(n_cp) >= 2:
            raise OSError("listing failed")
        return real_cp(*a, **k)
    try:
        _P.cited_paths = flaky
        m, T = go("hs14", ["good"])
    finally:
        _P.cited_paths = real_cp
    fb14 = m.st.tries.get("spec_feedback") or []
    ok(m.st.state == "SPEC" and any("listing failed" in x for x in fb14),
       f"HS14 a citation listing that fails is a problem, not a pass ({m.st.state}, {fb14})")
    # HS15 (Codex r2 P1-5/P1-7/P2-8): a try with no completion output is checked on whatever draft files exist, with no
    # comparison of the files before and after (a resume, a byte-identical rewrite or an odd file never skips it)
    d15, T15, st15 = new_run("hs15", {})
    T15 = Scripted(dict(T15.s, tries=["good"]))
    StubTasks(T15.s).draft_spec("q", d15, None)        # a valid draft already on disk (an earlier, older journal)
    T15 = Scripted(dict(T15.s, tries=["capped"]))
    m = Machine(d15, T15, st15, backend=toolkit, log=lambda *a: None, cross_engine_check=False)
    m.step()
    ok(m.st.state == "EVIDENCE", f"HS15 a capped try leaves a valid draft on disk: it is checked and used, whatever "
                                 f"changed during the try ({m.st.state}, {m.st.tries.get('spec_feedback')})")
    # HS16 (Codex r3 P1-1): the signed-configuration guard's problems go back to the drafter, whatever the backend
    real_sc = AB.signed_config_problems
    try:
        AB.signed_config_problems = lambda spec: ["engine.config: more components than the engine's limit"]
        m, T = go("hs16", ["good"])
    finally:
        AB.signed_config_problems = real_sc
    ok(m.st.state == "SPEC" and any("engine's limit" in x for x in m.st.tries.get("spec_feedback") or []),
       f"HS16 the configuration guard's refusal reaches the drafter ({m.st.state}, {m.st.tries.get('spec_feedback')})")
    # HS17 (Codex r3 P1-2): feedback the pre-sign checks cannot verify (a rethink's reasons) is never cleared by a
    # capped try whose old draft passes those checks; the reasons are carried to the next try
    d17, T17, st17 = new_run("hs17", {})
    StubTasks(T17.s).draft_spec("q", d17, None)          # the earlier, valid draft
    st17.tries["spec_feedback"] = ["rethink after review round 1: the comparator answers another question"]
    save_state(d17, st17)
    T17 = Scripted(dict(T17.s, tries=["capped"]))
    m = Machine(d17, T17, st17, backend=toolkit, log=lambda *a: None, cross_engine_check=False)
    m.step()
    ok(m.st.state == "SPEC" and any("comparator" in x for x in m.st.tries.get("spec_feedback") or []),
       f"HS17 a capped try after a rethink keeps the rethink's reasons and does not pass on the old draft "
       f"({m.st.state}, {m.st.tries.get('spec_feedback')})")
    # HS12 (Codex r1 P2-6): the cap on the feedback never drops this try's own problems
    many = [f"problem {i}" for i in range(45)]
    got12 = _carry_feedback(many, ["old one"])
    ok(all(x in got12 for x in many), f"HS12 every current problem reaches the next try ({len(got12)})")

    from types import SimpleNamespace as NS
    from v2_sdk_tasks import SPEC_MAX_TURNS, SdkTasks
    got = []

    def q(prompt=None, options=None):
        async def gen():
            got.append(options.max_turns)
            yield NS(total_cost_usd=0.0, session_id="hs3", is_error=False)
        return gen()
    hd = os.path.join(td, "hs3")
    os.makedirs(hd)
    t = SdkTasks(hd, query_fn=q)
    t.set_allowance(1.0)
    t._session("draft_spec", "x", "y", ())
    t._session("pick_engine", "x", "y", ())
    ok(got == [SPEC_MAX_TURNS, 30] and SPEC_MAX_TURNS > 30,
       f"HS3 the spec draft has its own turn cap, above the 30 of the other tasks ({got})")
    # HS13 (Codex r1 P1-5): an explicit cap is honoured by every task, the spec draft included
    got.clear()
    t5 = SdkTasks(hd, max_turns=5, query_fn=q)
    t5.set_allowance(1.0)
    t5._session("draft_spec", "x", "y", ())
    ok(got == [5], f"HS13 a caller's explicit turn cap holds for the spec draft ({got})")


def _self_test_spec_p1s(ok, td: str, toolkit, new_run) -> None:
    """SP1-SP4 (Codex round 4 on #348, on code merged through #342): a capped try with no draft must not stop later
    capped tries' drafts from being checked; a cited input that is not a regular file (a FIFO) is never opened by a later
    check; the spec and its settings are refused as links or hard links before any check reads them; an explicit zero
    turn cap is refused (it would remove the session's limit)."""
    from v2_tasks import StubTasks, TaskResult

    class Capped(StubTasks):
        """Per try: "none" (no draft, capped) or "good" (a valid draft written, then capped)."""
        def draft_spec(self, q, rd, fb):
            self.n_ = getattr(self, "n_", 0) + 1
            if self.s["tries"][self.n_ - 1] == "good":
                StubTasks(self.s).draft_spec(q, rd, fb)
            self.calls.append("draft_spec")
            return TaskResult(False, None, 0.0, "max turns")
    d1, T1, st1 = new_run("sp1", {})
    T1 = Capped(dict(T1.s, tries=["none", "good", "good"]))
    m = Machine(d1, T1, st1, backend=toolkit, log=lambda *a: None, cross_engine_check=False)
    for _ in range(3):
        if m.st.state != "SPEC" or m.st.status:
            break
        m.step()
    ok(m.st.state == "EVIDENCE", f"SP1 a first capped try with no draft does not stop the next capped try's valid draft "
                                 f"from being checked ({m.st.state}, {m.st.status}, {m.st.tries.get('spec_feedback')})")

    class Spy(Machine):
        def _geometry_problems(self, rd=None):
            self.ran = getattr(self, "ran", []) + ["geometry"]
            return []

        def _evidence_assess(self, rd):
            self.ran = getattr(self, "ran", []) + ["evidence"]
            return super()._evidence_assess(rd)
    # SP3: a cited FIFO inside the run is refused and no later check opens it (it would block with no writer)
    d3, T3, st3 = new_run("sp3", {})
    m3 = Spy(d3, T3, st3, backend=toolkit, log=lambda *a: None, cross_engine_check=False)
    spec3 = dict(T3.s["spec"], decision_units=dict(T3.s["spec"]["decision_units"], csv="units_fifo.csv"))
    StubTasks(dict(T3.s, spec=spec3)).draft_spec("q", d3, None)
    os.mkfifo(os.path.join(d3, "units_fifo.csv"))
    got3 = m3._spec_checks(d3)
    ok(any("units_fifo.csv" in x and "regular file" in x for x in got3) and not getattr(m3, "ran", []),
       f"SP3 a cited FIFO is refused and never opened by a later check ({getattr(m3, 'ran', [])}, {got3})")
    # SP4: the spec and analysis_settings.yaml are refused as a symlink or a hard link, before any check reads them
    marker = os.path.join(td, "sp4_fixture.yaml")
    open(marker, "w").write("fixture_marker: 1\n")
    res4 = {}
    for name, rel, how in (("settings link", "analysis_settings.yaml", os.symlink),
                           ("settings hard link", "analysis_settings.yaml", os.link),
                           ("spec hard link", AB.SPEC_FILE, os.link)):
        d4, T4, st4 = new_run("sp4_" + name.replace(" ", "_"), {})
        T4.draft_spec("q", d4, None)
        os.remove(os.path.join(d4, rel))
        how(marker, os.path.join(d4, rel))
        m4 = Spy(d4, T4, st4, backend=toolkit, log=lambda *a: None, cross_engine_check=False)
        got4 = m4._spec_checks(d4)
        res4[name] = bool(any(rel in x and "link" in x for x in got4) and not any("fixture_marker" in x for x in got4)
                          and not getattr(m4, "ran", []))
    ok(all(res4.values()), f"SP4 a linked spec or settings file is refused before any check reads it ({res4})")
    # SP5 (Codex r1 P1-1): through h_SPEC, a hard-linked spec is refused before the conflicts default rewrites it
    d5, T5, st5 = new_run("sp5", {})

    class Linking(StubTasks):
        def draft_spec(self, q, rd, fb):
            r = StubTasks(self.s).draft_spec(q, rd, fb)
            os.link(os.path.join(rd, AB.SPEC_FILE), os.path.join(td, "sp5_other_name.yaml"))
            return r
    m5 = Machine(d5, Linking(T5.s), st5, backend=toolkit, log=lambda *a: None, cross_engine_check=False)
    m5.step()
    ok(m5.st.state == "SPEC" and any("hard links" in x for x in m5.st.tries.get("spec_feedback") or []),
       f"SP5 a hard-linked spec is refused at the try's end, before the conflicts default is applied ({m5.st.state}, "
       f"{m5.st.tries.get('spec_feedback')})")

    # SP6 (Codex r1 P1-2/P1-3): a FIFO anywhere in the run, whatever path names it (a staged input a derivation reads,
    # a name with a leading space), is refused before derivations or any check runs
    class SpyAll(Spy):
        def _derive(self, rd=None):
            self.ran = getattr(self, "ran", []) + ["derive"]
            return []
    res6 = {}
    for name, rel, cite in (("staged input", "data/inputs/t.csv", None), ("spaced name", " units.csv", " units.csv")):
        d6, T6, st6 = new_run("sp6_" + name.replace(" ", "_"), {})
        spec6 = T6.s["spec"] if cite is None else dict(
            T6.s["spec"], decision_units=dict(T6.s["spec"]["decision_units"], csv=cite))
        StubTasks(dict(T6.s, spec=spec6)).draft_spec("q", d6, None)
        os.makedirs(os.path.dirname(os.path.join(d6, rel)), exist_ok=True)
        os.mkfifo(os.path.join(d6, rel))
        m6 = SpyAll(d6, T6, st6, backend=toolkit, log=lambda *a: None, cross_engine_check=False)
        m6.st.derived_inputs = True
        got6 = m6._spec_checks(d6)
        res6[name] = bool(any("regular file" in x for x in got6) and not getattr(m6, "ran", []))
    ok(all(res6.values()), f"SP6 a FIFO in the run is refused before derivations or any reading check ({res6})")
    # SP7 (Codex r1 P1-5): a run saved before this fix (feedback "draft_spec failed: ...", no checked copy) still has a
    # later capped try's valid draft checked
    d7, T7, st7 = new_run("sp7", {})
    st7.tries.update(spec=1, spec_feedback=["draft_spec failed: max turns"])
    save_state(d7, st7)
    T7 = Capped(dict(T7.s, tries=["good"]))
    m7 = Machine(d7, T7, st7, backend=toolkit, log=lambda *a: None, cross_engine_check=False)
    m7.step()
    ok(m7.st.state == "EVIDENCE", f"SP7 a capped try after an earlier no-draft note is checked ({m7.st.state}, "
                                  f"{m7.st.tries.get('spec_feedback')})")
    # SP2: an explicit zero turn cap is refused, never passed on as "no limit"
    from v2_sdk_tasks import SdkTasks
    try:
        SdkTasks(os.path.join(td, "sp2"), max_turns=0)
        refused = False
    except ValueError:
        refused = True
    ok(refused, "SP2 SdkTasks(max_turns=0) is refused: the SDK reads 0 as no turn limit")


def _self_test_acquire(ok, td: str) -> None:
    """All 14 stage-machine ACQUIRE self-tests (docs/design/acquire.md §9.1):
    T_ACQ_OFF, T_ACQ_OFF_GOLDEN, T_ACQ_SUPPLIED, T_ACQ_PROMOTE, T_ACQ_EMPTY, T_ACQ_TOPUP,
    T_ACQ_CLOSED, T_ACQ_TAMPER_PRESPEC, T_ACQ_BALANCE, T_ACQ_OLD_STATE, T_ACQ_SIGN,
    T_ACQ_QUOTE, T_ACQ_DELIVERABLE, T_ACQ_AMEND."""
    import shutil
    import yaml
    from agents import acquire as ACQ
    from agents import v2_sdk_tasks as VSDK
    from v2_tasks import StubTasks, TaskResult
    from toolkit.spec import amend as AM, evidence_dossier as ED, preconditions as PC, schema, signoff as SO

    # T_ACQ_OFF: default acquire="off" starts at SPEC and history never names ACQUIRE
    d_off = os.path.join(td, "t_acq_off")
    st_off = init_run(d_off, "Bare question?", signoff="none", derived_inputs=False)
    meta_off = json.load(open(os.path.join(d_off, "metadata.json")))
    m_off = Machine(d_off, StubTasks({}), st_off, log=lambda *a: None, cross_engine_check=False)
    m_off.step()
    ok(st_off.acquire == "off" and meta_off.get("acquire") == "off"
       and not any(h.get("from") == "ACQUIRE" or h.get("to") == "ACQUIRE" for h in m_off.st.history),
       f"T_ACQ_OFF acquire='off' starts at SPEC and history never names ACQUIRE ({m_off.st.history[:2]})")

    # T_ACQ_OFF_GOLDEN (§8): off-path provenance has no origin/acquired_inventory_sha256, draft_spec has no
    # manifest sentence, TOOLS is unchanged, and history never names ACQUIRE
    d_gold = os.path.join(td, "t_acq_gold")
    st_gold = init_run(d_gold, "Use requirements.txt for allocation.", signoff="none",
                       derived_inputs=False)
    staged_g = stage_inputs(d_gold, st_gold.question)
    prov_g = json.load(open(os.path.join(d_gold, PROVENANCE)))
    gold_prompts = {}
    orig_sess = VSDK.SdkTasks._session

    def _cap_sess(self_, kind, system, prompt, owns, model=None, **kw):
        gold_prompts[kind] = (system, prompt, owns)
        return TaskResult(True, {"ok": True}, 0.0, "fake")

    VSDK.SdkTasks._session = _cap_sess
    try:
        VSDK.SdkTasks(d_gold).draft_spec(st_gold.question, d_gold, None)
    finally:
        VSDK.SdkTasks._session = orig_sess
    ok(bool(staged_g) and "acquired_inventory_sha256" not in prov_g
       and all("origin" not in v for v in (prov_g.get("inputs") or {}).values())
       and "acquired_manifest.json" not in (gold_prompts.get("draft_spec", ("", "", ()))[1])
       and VSDK.TOOLS == ["Read", "Glob", "Grep", "Write", "Edit"]
       and st_gold.state == "SPEC",
       f"T_ACQ_OFF_GOLDEN off-path provenance, prompt, tools, and state are unchanged ({list(prov_g.keys())})")

    # T_ACQ_SUPPLIED: acquire="auto" with a question naming a repo file skips ACQUIRE at $0 and closes back-edges
    d_sup = os.path.join(td, "t_acq_sup")
    st_sup = init_run(d_sup, "Allocate using requirements.txt.", signoff="none", acquire="auto",
                      derived_inputs=False)
    T_sup = StubTasks({"usd": 1.0})
    m_sup = Machine(d_sup, T_sup, st_sup, log=lambda *a: None, cross_engine_check=False)
    nxt_sup = m_sup.step()
    ok(nxt_sup == "SPEC" and not T_sup.calls and m_sup.st.spent_usd == 0.0
       and m_sup.st.acquire == "off"
       and (m_sup.st.acquired or {}).get("status") == "skipped_question_supplies_inputs"
       and m_sup._acquire_closed(),
       f"T_ACQ_SUPPLIED question supplying files skips ACQUIRE at $0 and closes back-edges ({nxt_sup}, {m_sup.st.acquired})")

    # Fixture server for T_ACQ_PROMOTE, T_ACQ_EMPTY, T_ACQ_TOPUP, T_ACQ_CLOSED, T_ACQ_TAMPER_PRESPEC,
    # T_ACQ_BALANCE, T_ACQ_SIGN, T_ACQ_QUOTE, T_ACQ_AMEND
    routes = {
        "/api/catalogue": (200, "application/json", json.dumps([{"code": "IND1"}, {"code": "IND2"}]).encode("utf-8"), {}),
        "/api/slice/IND1?mode=csv": (200, "text/csv", b"unit,pop\nU1,1000\nU2,2000\n", {}),
        "/api/slice/IND2?mode=csv": (200, "text/csv", b"unit,inc\nU1,0.12\nU2,0.25\n", {}),
        "/api/slice/IND1?mode=xml_ok": (
            200, "application/xml",
            b"<article><body><p>Trial result showed efficacy 0.65 (95% CI 0.50-0.78) in 500 sites.</p></body></article>",
            {},
        ),
    }
    with ACQ.AcquireFixtureServer(routes) as srv:
        al_p = os.path.join(td, "acq_sm_allowlist.yaml")
        yaml.safe_dump(ACQ._test_allowlist_yaml(srv.port), open(al_p, "w"), sort_keys=False)

        def _fetch_keep(rd, code="IND1", mode="csv", wanted=None):
            F = ACQ.Fetcher(rd, al_p, resolver=lambda h, p: ["127.0.0.1"],
                            _allow_test_http=True, _test_port=srv.port, _allow_loopback_ip=True)
            F.search("svc_data", "catalogue", {})
            fid = F.fetch("svc_data", "slice", {"code": code, "mode": mode})["fetch_id"]
            return {"keep": [fid], "wanted_map": {fid: wanted or ["population"]}}

        # T_ACQ_PROMOTE: fixture fetch -> promoted files, manifest, views, SPEC prompt sees manifest
        d_pr = os.path.join(td, "t_acq_promote")
        st_pr = init_run(d_pr, "Bare policy question.", signoff="none", acquire="auto",
                         derived_inputs=False)
        T_pr = StubTasks({"acquire": lambda q, rd, pkt: _fetch_keep(rd, "IND1", "csv", ["population"])})
        m_pr = Machine(d_pr, T_pr, st_pr, log=lambda *a: None, cross_engine_check=False)
        m_pr.acquire_allowlist = al_p
        nxt_pr = m_pr.step()
        pr_prompts = {}
        VSDK.SdkTasks._session = lambda self_, k, s, p, o, model=None, **kw: (
            pr_prompts.__setitem__(k, p) or TaskResult(True, {"ok": True}, 0.0, "fake")
        )
        try:
            VSDK.SdkTasks(d_pr).draft_spec(st_pr.question, d_pr, None)
        finally:
            VSDK.SdkTasks._session = orig_sess
        views_pr = os.listdir(os.path.join(d_pr, ACQ.VIEWS_ROOT)) if os.path.isdir(os.path.join(d_pr, ACQ.VIEWS_ROOT)) else []
        ok(nxt_pr == "SPEC" and os.path.isfile(os.path.join(d_pr, ACQ.MANIFEST_REL)) and bool(views_pr)
           and "evidence/acquired_manifest.json" in pr_prompts.get("draft_spec", "")
           and "data/inputs/acquired/_views/" in pr_prompts.get("draft_spec", ""),
           f"T_ACQ_PROMOTE promotes files, writes manifest & views, and SPEC receives them ({nxt_pr}, {views_pr})")

        # T_ACQ_EMPTY: nothing promoted -> paused:awaiting_inputs and acquire/inputs_request.md written
        d_em = os.path.join(td, "t_acq_empty")
        st_em = init_run(d_em, "Bare question.", signoff="owner", acquire="auto",
                         derived_inputs=False)
        T_em = StubTasks({"acquire": {"keep": [], "wanted_map": {}}})
        m_em = Machine(d_em, T_em, st_em, log=lambda *a: None, cross_engine_check=False)
        m_em.acquire_allowlist = al_p
        res_em = m_em.run()
        ok(res_em == f"paused:{AWAITING_INPUTS}"
           and os.path.isfile(os.path.join(d_em, "acquire", "inputs_request.md"))
           and "Wanted kinds" in open(os.path.join(d_em, "acquire", "inputs_request.md")).read(),
           f"T_ACQ_EMPTY nothing promoted pauses awaiting_inputs and writes inputs_request.md ({res_em})")

        # T_ACQ_TOPUP: missing_input after 3rd SPEC try goes to ACQUIRE once, grants 4th SPEC try; 2nd gap does not go back
        d_tu = os.path.join(td, "t_acq_topup")
        st_tu = init_run(d_tu, "Bare question.", signoff="none", acquire="auto",
                         derived_inputs=False)
        acq_rounds_seen = []

        def _acq_tu(q, rd, pkt):
            acq_rounds_seen.append(pkt.get("round"))
            return _fetch_keep(rd, "IND1" if pkt.get("round") == 1 else "IND2", "csv",
                               ["population"] if pkt.get("round") == 1 else ["burden"])

        T_tu = StubTasks({"acquire": _acq_tu, "spec": {"schema_version": 1}})

        class _TopupMachine(Machine):
            def _spec_checks(self, rd):
                tr = int(self.st.tries.get("spec") or 0)
                if tr in (1, 2):
                    return ["plain spec error"]
                if tr == 3:
                    return ["missing_input: burden:incidence"]
                return ["missing_input: coverage:second_gap"]

        m_tu = _TopupMachine(d_tu, T_tu, st_tu, log=lambda *a: None, cross_engine_check=False)
        m_tu.acquire_allowlist = al_p
        res_tu = m_tu.run(max_steps=20)
        ok(res_tu == "run_failed" and acq_rounds_seen == [1, 2]
           and m_tu.st.tries.get("spec") == MAX_SPEC_TRIES + 1 and m_tu.st.tries.get("spec_bonus") == 1
           and int((m_tu.st.acquired or {}).get("rounds") or 0) == MAX_ACQUIRE_ROUNDS,
           f"T_ACQ_TOPUP missing_input after 3rd SPEC try tops up once and grants bonus try; 2nd gap does not loop "
           f"(res={res_tu}, acq_rounds={acq_rounds_seen}, tries={m_tu.st.tries})")

        # T_ACQ_CLOSED: --signoff none closes acquisition on SIGN -> ENGINE_CHOICE; later calibration refusal to SPEC
        # never reaches ACQUIRE; restored state past SIGN without closed flag is treated as closed
        d_cl = os.path.join(td, "t_acq_closed")
        st_cl = init_run(d_cl, "Bare question.", signoff="none", acquire="auto",
                         derived_inputs=False)
        T_cl = StubTasks({"acquire": lambda q, rd, pkt: _fetch_keep(rd, "IND1", "csv"), "spec": {"schema_version": 1}})

        class _ClosedMachine(Machine):
            def _spec_checks(self, rd):
                return ["missing_input: burden:after_sign"]

        m_cl = _ClosedMachine(d_cl, T_cl, st_cl, log=lambda *a: None, cross_engine_check=False)
        m_cl.acquire_allowlist = al_p
        m_cl.step()  # ACQUIRE -> SPEC (round 1 promoted)
        # Simulate reaching SIGN -> ENGINE_CHOICE under --signoff none
        _write_json(d_cl, "evidence/dossier_draft.json", {"status": "not_required", "required": [], "tries": 0})
        yaml.safe_dump({"schema_version": 1}, open(os.path.join(d_cl, AB.SPEC_FILE), "w"))
        m_cl.st.state = "SIGN"
        nxt_ec = m_cl.step()
        closed_after_sign = bool((m_cl.st.acquired or {}).get("closed"))
        # Simulate ANALYZE calibration refusal back to SPEC
        m_cl.st.state = "SPEC"
        m_cl.st.tries["spec"] = 0
        nxt_after_ref = m_cl.step()
        # Part B: restored state past SIGN missing closed flag
        d_cl2 = os.path.join(td, "t_acq_closed_restored")
        st_cl2 = init_run(d_cl2, "Bare question.", signoff="none", acquire="auto",
                          derived_inputs=False)
        st_cl2.acquired = {"rounds": 1}  # deliberately omit closed flag
        st_cl2.history.append({"at": _now(), "from": "SIGN", "to": "ENGINE_CHOICE", "why": "sign-off none"})
        st_cl2.state = "ACQUIRE"
        save_state(d_cl2, st_cl2)
        m_cl2 = Machine(d_cl2, StubTasks({}), None, log=lambda *a: None, cross_engine_check=False)
        nxt_cl2 = m_cl2.step()
        ok(nxt_ec == "ENGINE_CHOICE" and closed_after_sign and nxt_after_ref == "SPEC"
           and m_cl2._acquire_closed() and nxt_cl2 == "FINAL" and m_cl2.st.status == "run_failed",
           f"T_ACQ_CLOSED --signoff none closes acquisition at SIGN and restored state past SIGN stays closed "
           f"({nxt_ec}, closed={closed_after_sign}, after_ref={nxt_after_ref}, restored={nxt_cl2}/{m_cl2.st.status})")

        # T_ACQ_TAMPER_PRESPEC: tampering promoted file between ACQUIRE and SPEC -> artifact_tampered before draft_spec
        d_tp = os.path.join(td, "t_acq_tamper")
        st_tp = init_run(d_tp, "Bare question.", signoff="none", acquire="auto",
                         derived_inputs=False)
        T_tp = StubTasks({"acquire": lambda q, rd, pkt: _fetch_keep(rd, "IND1", "csv"), "spec": {"schema_version": 1}})
        m_tp = Machine(d_tp, T_tp, st_tp, log=lambda *a: None, cross_engine_check=False)
        m_tp.acquire_allowlist = al_p
        m_tp.step()  # ACQUIRE -> SPEC
        prov_tp = json.load(open(os.path.join(d_tp, PROVENANCE)))
        acq_p = next(p for p, e in prov_tp["inputs"].items() if isinstance(e, dict) and e.get("origin") == "acquired")
        abs_tp = os.path.join(d_tp, acq_p)
        os.chmod(abs_tp, 0o644)
        open(abs_tp, "ab").write(b"TAMPER\n")
        os.chmod(abs_tp, 0o444)
        m_tp.step()
        ok(m_tp.st.state == "FINAL" and m_tp.st.status == "artifact_tampered" and "draft_spec" not in T_tp.calls,
           f"T_ACQ_TAMPER_PRESPEC tampered acquired file stops artifact_tampered before draft_spec "
           f"({m_tp.st.state}/{m_tp.st.status}, calls={T_tp.calls})")

        # T_ACQ_BALANCE: interrupted call + retry + top-up + read-backs cannot exceed ACQUIRE_CAP_USD ($6)
        d_bal = os.path.join(td, "t_acq_balance")
        st_bal = init_run(d_bal, "Bare question.", signoff="none", acquire="auto", budget_usd=50.0,
                          derived_inputs=False)
        st_bal.journal["ACQUIRE#r1:a0"] = {
            "task": "acquire", "status": "started", "allowance_usd": 3.0, "readback_usd": 0.25, "acquire_budget": True,
        }
        save_state(d_bal, st_bal)
        T_bal = StubTasks({"acquire": lambda q, rd, pkt: _fetch_keep(rd, "IND1", "csv"), "usd": 2.50})
        m_bal = Machine(d_bal, T_bal, None, log=lambda *a: None, cross_engine_check=False)
        m_bal.acquire_allowlist = al_p
        after_int = float((m_bal.st.acquired or {}).get("spent_usd") or 0.0)
        m_bal.step()  # retry runs with allowance <= 2.75, spends 2.50 -> total 5.75
        after_retry = float((m_bal.st.acquired or {}).get("spent_usd") or 0.0)
        # Attempt top-up round 2 with only $0.25 left (< $0.50 floor) -> must pause awaiting_budget
        m_bal.st.state = "ACQUIRE"
        res_bal2 = m_bal.run()
        ok(abs(after_int - 3.25) < 1e-6 and T_bal.allowances and T_bal.allowances[0] <= 2.75 + 1e-9
           and abs(after_retry - 5.75) < 1e-6 and after_retry <= ACQUIRE_CAP_USD
           and res_bal2 == f"paused:{AWAITING_BUDGET}"
           and float((m_bal.st.acquired or {}).get("spent_usd") or 0.0) <= ACQUIRE_CAP_USD,
           f"T_ACQ_BALANCE interrupted + retry + top-up + read-backs bounded by ACQUIRE_CAP_USD "
           f"(int={after_int}, retry={after_retry}, allow={T_bal.allowances}, res2={res_bal2})")

        # T_ACQ_OLD_STATE: run_state.json without acquire/acquired loads as off and resumes cleanly
        d_old = os.path.join(td, "t_acq_old")
        os.makedirs(d_old)
        json.dump({"question": "Old Q?", "state": "SPEC", "round": 1, "max_rounds": 3, "signoff": "none"},
                  open(os.path.join(d_old, STATE_FILE), "w"))
        st_old = load_state(d_old)
        ok(st_old is not None and st_old.acquire == "off" and st_old.acquired == {} and st_old.state == "SPEC",
           f"T_ACQ_OLD_STATE old run_state.json without acquire loads as off ({st_old})")

        # T_ACQ_SIGN: sheet shows acquired-inputs tables; changing raw file, adding extra file, or changing manifest
        # URL after signing makes signature stale through sign, status, preconditions, and resume (even with NO dossier)
        d_sg = os.path.join(td, "t_acq_sign")
        os.makedirs(d_sg)
        _write_json(d_sg, "metadata.json", {"pipeline": "v2", "commit": "0" * 40, "launcher": "t", "acquire": "auto"})
        raw_sg = yaml.safe_load(open(schema.toy_spec(d_sg)))
        raw_sg["principle_departures"] = [{"principle": 2, "check": "headline_comparator", "reason": "toy"},
                                          {"principle": 3, "check": "baseline_burden", "reason": "toy"}]
        yaml.safe_dump(raw_sg, open(os.path.join(d_sg, AB.SPEC_FILE), "w"), sort_keys=False)
        AM._pre_sign(d_sg, False)
        fk_sg = _fetch_keep(d_sg, "IND1", "csv", ["population"])
        promo_sg = ACQ.promote(d_sg, fk_sg["keep"], fk_sg["wanted_map"], allowlist_path=al_p)
        SO.write_sheet(d_sg)
        sheet_txt = open(os.path.join(d_sg, SIGNOFF_SHEET), encoding="utf-8").read()
        rec_sg = SO.sign(d_sg, "owner")
        st_sg = RunState(question="q", state="ANALYZE", signoff="owner", acquire="auto",
                         acquired={"rounds": 1, "closed": True, "inventory_sha256": promo_sg["inventory_sha256"]})
        save_state(d_sg, st_sg)
        signed_clean = (
            "### Acquired inputs (fetched by the harness)" in sheet_txt
            and "#### Outgoing acquisition queries" in sheet_txt
            and "#### Fetched and not promoted" in sheet_txt
            and SO.status(d_sg) == "signed"
            and rec_sg.get("acquired_inventory_sha256") == promo_sg["inventory_sha256"]
            and PC.problems(d_sg, None, None, mode="resume") == []
        )
        tamper_results = {}
        acq_sg_rel = promo_sg["promoted"][0]
        for tname in ("raw_file", "extra_file", "manifest_url"):
            cp = os.path.join(td, f"t_acq_sign_{tname}")
            shutil.copytree(d_sg, cp)
            if tname == "raw_file":
                ap = os.path.join(cp, acq_sg_rel)
                os.chmod(ap, 0o644)
                open(ap, "ab").write(b"changed\n")
                os.chmod(ap, 0o444)
            elif tname == "extra_file":
                open(os.path.join(cp, ACQ.ACQUIRED_ROOT, "svc_data", "extra.csv"), "w").write("x\n1\n")
            elif tname == "manifest_url":
                mp = os.path.join(cp, ACQ.MANIFEST_REL)
                mdoc = json.load(open(mp))
                mdoc["files"][acq_sg_rel]["url"] = "https://tampered.example/x"
                json.dump(mdoc, open(mp, "w"))
            st_chg = SO.status(cp) == "changed"
            pc_bad = bool(PC.problems(cp, None, None, mode="resume"))
            try:
                SO.sign(cp, "owner")
                sign_ref = False
            except ValueError:
                sign_ref = True
            res_cp = Machine(cp, StubTasks({}), None, log=lambda *a: None, cross_engine_check=False).run()
            tamper_results[tname] = (st_chg and pc_bad and sign_ref and res_cp == "artifact_tampered")
        ok(signed_clean and all(tamper_results.values()),
           f"T_ACQ_SIGN sheet renders acquired tables; tampering raw/extra/manifest invalidates sign, status, "
           f"preconditions, and resume with no dossier (clean={signed_clean}, tampers={tamper_results})")

        # T_ACQ_QUOTE: acquired literature quote verification (bad quote -> HIGH observation_quote_not_found;
        # exact quote passes)
        d_qt = os.path.join(td, "t_acq_quote")
        os.makedirs(d_qt)
        fk_qt = _fetch_keep(d_qt, "IND1", "xml_ok", ["trial_effect"])
        promo_qt = ACQ.promote(d_qt, fk_qt["keep"], fk_qt["wanted_map"], allowlist_path=al_p)
        fid_qt = fk_qt["keep"][0]
        sha_qt = json.load(open(os.path.join(d_qt, ACQ.MANIFEST_REL)))["files"][promo_qt["promoted"][0]]["sha256"]
        base_obs = {"source": "Trial X", "where": "Results", "year": 2025, "sites": 1, "n": 500,
                    "sampling_frame": "adults", "value": 0.65}
        doss_bad_q = yaml.safe_dump({"inputs": {"parameter:eff": {
            "observations": [dict(base_obs, acquired={"id": fid_qt, "sha256": sha_qt, "locator": "p1",
                                                      "quote": "not present in xml"})]}}}, sort_keys=False).encode()
        doss_ok = yaml.safe_dump({"inputs": {"parameter:eff": {
            "observations": [dict(base_obs, acquired={
                "id": fid_qt, "sha256": sha_qt, "locator": "p1",
                "quote": "Trial result showed efficacy 0.65 (95% CI 0.50-0.78)"})]}}}, sort_keys=False).encode()
        f_bad = ED.quote_findings({}, d_qt, doss_bad_q)
        f_ok = ED.quote_findings({}, d_qt, doss_ok)
        ok(len(f_bad) == 1 and f_bad[0]["kind"] == "observation_quote_not_found" and f_bad[0]["severity"] == "HIGH"
           and f_bad[0]["scope_declarable"] is False and f_ok == [],
           f"T_ACQ_QUOTE quote mismatch is HIGH non-declarable; exact quote passes ({f_bad}, {f_ok})")

        # T_ACQ_DELIVERABLE: deliverable_file.path under acquire/ is refused
        d_del = os.path.join(td, "t_acq_deliv")
        os.makedirs(d_del)
        yaml.safe_dump({"deliverable_file": {"path": "acquire/out.csv", "columns": {"u": "unit"}}},
                       open(os.path.join(d_del, AB.SPEC_FILE), "w"))
        dp_probs = deliverable_problems(d_del)
        ok(bool(dp_probs), f"T_ACQ_DELIVERABLE deliverable path under acquire/ is refused ({dp_probs})")

        # T_ACQ_AMEND: amendment child carries acquired bytes, provenance, record, manifest, metadata fields,
        # and closed acquisition state (without copying acquire/quarantine/)
        st_sg.state = "FINAL"
        st_sg.status = "completed"
        save_state(d_sg, st_sg)
        os.makedirs(os.path.join(d_sg, "acquire", "quarantine"), exist_ok=True)
        open(os.path.join(d_sg, "acquire", "quarantine", "scratch.part"), "w").write("drop")
        m_sg_meta = json.load(open(os.path.join(d_sg, "metadata.json")))
        m_sg_meta.update(acquired_inventory_sha256=promo_sg["inventory_sha256"], allowlist_version=1,
                         allowlist_sha256=ACQ.load_allowlist(al_p)[1])
        _write_json(d_sg, "metadata.json", m_sg_meta)
        child_am = os.path.join(td, "t_acq_amend_child")
        patch_p = os.path.join(td, "t_acq_amend_patch.yaml")
        yaml.safe_dump({"id": "A_ACQ", "reason": "amend envelope",
                        "spec": [{"set": "constraints.envelope_usd", "value": 9500}]},
                       open(patch_p, "w", encoding="utf-8"), sort_keys=False)
        AM.amend(d_sg, patch=patch_p, out=child_am, signoff=None, log=lambda *a: None)
        c_meta = json.load(open(os.path.join(child_am, "metadata.json")))
        c_st = load_state(child_am)
        ok(os.path.isfile(os.path.join(child_am, acq_sg_rel))
           and os.path.isfile(os.path.join(child_am, ACQ.RECORD_REL))
           and os.path.isfile(os.path.join(child_am, ACQ.MANIFEST_REL))
           and os.path.isfile(os.path.join(child_am, PROVENANCE))
           and not os.path.exists(os.path.join(child_am, "acquire", "quarantine"))
           and c_meta.get("acquire") == "auto"
           and c_meta.get("acquired_inventory_sha256") == promo_sg["inventory_sha256"]
           and c_st is not None and (c_st.acquired or {}).get("closed") is True
           and ACQ.verify_inventory(child_am)["inventory_sha256"] == promo_sg["inventory_sha256"],
           f"T_ACQ_AMEND amendment child carries acquired bytes, record, manifest, metadata, and closed state "
           f"(meta_keys={sorted(c_meta)}, closed={(c_st.acquired if c_st else None)})")


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
    try:
        rec = b.sign(a.run_dir, a.by, a.note, **kw)
    except ValueError as e:
        print(f"Not signed: {e}", file=sys.stderr)
        return 1
    print(json.dumps(rec, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
