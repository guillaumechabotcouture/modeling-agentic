#!/usr/bin/env python3
"""v2 C (S4): the code-owned run state machine.

    python3 agents/stage_machine.py status <run_dir>
    python3 agents/stage_machine.py sign <run_dir> --by owner [--note "..."]      # the owner signs the spec

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
    ENGINE_REVIEW_CAP_USD,
    EngineBuildMixin,
    MAX_CRITIQUE_ROUNDS,
    MAX_ENGINE_BUILD_TRIES,
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


def sign(run_dir: str, by: str, note: str = "", expect_sha256: str | None = None,
         expect_derivations_sha256: str | None = None) -> dict:
    """Sign the run's decision spec via the analysis backend (`toolkit/spec/signoff.py`)."""
    return AB.backend("toolkit").sign(
        run_dir, by, note, expect_sha256=expect_sha256,
        expect_derivations_sha256=expect_derivations_sha256)


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
        if (j.get("build_budget") or j.get("task") in ("build_engine", "critique_engine")) and isinstance(
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
        """The spec and the evidence stage's assessment of it (toolkit/spec/evidence_dossier.py assess), with a dossier
        path that the harness, the gate or a task writes added to its spec problems. SPEC and EVIDENCE share it."""
        from toolkit.spec import evidence_dossier as EDR
        raw = EDR._spec(rd)
        a = EDR.assess(rd, raw)
        rel = a.get("rel")
        if rel and not a.get("draftable") and owned_path(rd, os.path.normpath(rel).replace(os.sep, "/"), tuple(
                p for p in harness_owned() if p not in ("data/*", EDR.DOSSIER_FILE))):
            a["spec_problems"] = a["spec_problems"] + [   # Codex r2 P1-5: a file the harness or a task writes
                f"evidence.dossier {rel!r} is a file the harness, the gate or a task writes: name "
                f"{EDR.DOSSIER_FILE}"]
        return raw, a

    def _derive(self, rd: str | None = None) -> list[str]:
        """The draft's declared derivations, run by the harness (scripts/pipeline.py derive). Their problems go back
        to the planner with the script's stderr tail: the planner never needs a shell to learn whether its code
        works."""
        import pipeline as P
        rep = P.derive(rd or self.run_dir, unsandboxed_ok=self.st.derive_unsandboxed)
        return [f"{p['code']}: {p['claim']}" for p in rep["problems"]]

    def h_SIGN(self):
        mode = self.st.signoff
        from toolkit.spec import evidence_dossier as EDR
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
        from toolkit.spec import evidence_dossier as EDR
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
        Otherwise the harness scaffolds each missing entry with what execution uses (toolkit/spec/evidence_dossier.py)
        and the bounded draft_evidence task fills the evidence, at most EDR.MAX_TRIES times in the run, each time with
        the signer's check's problems; then the record (evidence/dossier_draft.json).
        A spec signed under the earlier order (SIGN before EVIDENCE) is left as signed."""
        from toolkit.spec import evidence_dossier as EDR
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
                notes.append(f"the spec names {a['rel']}: only {EDR.DOSSIER_FILE} is drafted in the run")
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
            except Exception as e:  # noqa: BLE001 -- SIGN checks the dossier directly
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
            req_ref = (
                bool(prev_eb["require_reference"])
                if "require_reference" in prev_eb
                else bool(build_decl.get("require_reference"))
            )
            self.st.engine_build = {
                "id": bid,
                "kind": str(build_decl.get("kind") or "starsim_module"),
                "status": "requested",
                "require_reference": req_ref,
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


def main(argv=None) -> int:
    import argparse
    argv = sys.argv[1:] if argv is None else argv
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
                    help="the sha256 of data/derivations_record.json when shown ('none': there was none)")
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
                            ("expect_derivations_sha256", a.expect_derivations_sha256)) if v}
    try:
        rec = b.sign(a.run_dir, a.by, a.note, **kw)
    except ValueError as e:
        print(f"Not signed: {e}", file=sys.stderr)
        return 1
    print(json.dumps(rec, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
