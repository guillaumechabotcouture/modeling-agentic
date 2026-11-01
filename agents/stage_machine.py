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
from __future__ import annotations

import hashlib
import json
import os
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
STATES = ("SPEC", "SIGN", "EVIDENCE", "ENGINE_CHOICE", "CONFIGURE", "ANALYZE", "CRITIQUE", "GATE",
          "FIX_LOOP", "OWNER_DECISION", "WRITE", "PRESENTATION", "FINAL")
# The state table: every transition the machine may take. A transition outside it is a bug
# (raised, never taken).
EDGES = {
    "SPEC": {"SIGN", "SPEC", "FINAL"},
    "SIGN": {"EVIDENCE", "SIGN"},
    "EVIDENCE": {"ENGINE_CHOICE"},
    "ENGINE_CHOICE": {"CONFIGURE", "FINAL"},
    "CONFIGURE": {"ANALYZE", "FINAL"},
    "ANALYZE": {"CRITIQUE", "FINAL"},
    "CRITIQUE": {"GATE", "WRITE", "FINAL"},
    "GATE": {"WRITE", "FIX_LOOP", "SPEC", "ENGINE_CHOICE", "OWNER_DECISION", "CRITIQUE", "FINAL"},
    "FIX_LOOP": {"CRITIQUE", "SPEC", "ENGINE_CHOICE", "WRITE", "FINAL"},
    "OWNER_DECISION": {"GATE", "OWNER_DECISION"},
    "WRITE": {"PRESENTATION", "GATE", "FINAL"},
    "PRESENTATION": {"GATE", "FINAL"},
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
# Findings whose resolution bears on the primary decision: Phase 49 β's owner rule decides
# whether they may be declared. Without that rule they are not declarable (fail closed).
DECISION_BEARING_KINDS = ("decision_test_changes_plan",)
MAX_SPEC_TRIES = 3
MAX_SCHEMA_REPAIRS = 2


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
    # Every paid task: key -> {task, status started|completed, allowance_usd, usd, session_id,
    # started, completed, ok, output, note}. Written before and after each call, so a resumed run
    # never repeats a completed task or loses its charge, and charges an interrupted one its allowance.
    journal: dict = field(default_factory=dict)
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


def save_state(run_dir: str, st: RunState) -> None:
    p = os.path.join(run_dir, STATE_FILE)
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st.to_dict(), f, indent=1, default=str)
    os.replace(tmp, p)


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
    import pipeline as P
    owned = tuple(P._reserved_patterns()) + PROTECTED_BY_FIX + (
        STATE_FILE, SIGNOFF_FILE, SCOPE_SIGNOFF, FL.LEDGER, FX.IMPACT, "report.md", "manuscript.md", "metadata.json",
        "final_gate.json", AB.ENGINE_CHOICE, "validator_history.jsonl", "gate_r*", "fix_plan_r*", "fix_progress_r*",
        "analysis/*", "models/pipeline*", "evidence/*", "data/*")
    if owned_path(root, norm, owned):
        return [f"deliverable path {rel!r} is a file the harness, the gate or a critic owns"]
    return []


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


def _owner_rule():
    """Phase 49 β's owner rule (scripts/scope_signoff.py with the validator's AWAIT_OWNER path),
    or None when this checkout does not have both."""
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


def apply_owner_rule(run_dir: str, round_n: int, viols: list[dict], rule="auto") -> list[dict]:
    """Decision-bearing findings (a measured decision test that changes the plan) go through
    Phase 49 β's owner rule before they can be declared. Without the rule they are not
    scope-declarable, so the run can neither declare them away nor finish around them."""
    import validate_critique_yaml as V
    rule = _owner_rule() if rule == "auto" else rule
    bearing = [v for v in viols if v.get("decision_bearing") or v.get("kind") in DECISION_BEARING_KINDS]
    if not bearing:
        return []
    if rule is not None:
        for v in bearing:
            v["owner_signoff"] = True
            v.setdefault("owner_item", {"finding": FL.gate_id(v), "reason": "a measured decision test changes the plan",
                                        "claim": v.get("claim", "")})
        try:
            return list(rule.gate_findings(run_dir, round_n, viols, non_declarable=V.is_non_declarable) or [])
        except Exception as e:  # noqa: BLE001 -- fail closed below
            reason = f"the owner rule failed ({type(e).__name__}: {e})"
    else:
        reason = "the owner rule (Phase 49 β, scripts/scope_signoff.py) is not available"
    for v in bearing:
        v["scope_declarable"] = False
        v.pop("owner_signoff", None)
        v["claim"] = f"{v.get('claim', '')} Not declarable: {reason}."
    return []


def v2_gate(run_dir: str, round_n: int, max_rounds: int, post_write: bool, extra: list[dict],
            history: bool = True, critics=CRITICS, spec_violations=None, registry_violations=None,
            strict_done_when: bool = False, append_history: bool | None = None) -> dict:
    """Today's rule functions, in today's order, on the v2 findings: the critic YAMLs, the
    harness's checks (`extra`), and optionally spec-compliance and registry findings."""
    import done_when
    import validate_critique_yaml as V
    critiques, schema_errors = {}, []
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
        try:
            critiques[reviewer] = V.validate_critique(path, reviewer, round_n)
        except V.SchemaError as e:
            schema_errors.append(str(e))
    if strict_done_when:
        # v2blind1: critics wrote done_when as prose, done_when.py counted every test as missing,
        # and the open MEDIUMs were accepted. A HIGH or MEDIUM blocker's test is part of the schema
        # here: one that does not parse is repaired like any schema error (the T37 path).
        import done_when as DW
        for reviewer, doc in critiques.items():
            for b in doc.get("blockers") or []:
                if b.get("resolved") or str(b.get("severity")) not in ("HIGH", "MEDIUM"):
                    continue
                probs = DW.problems(b["done_when"]) if b.get("done_when") is not None else ["missing"]
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
    viols += apply_owner_rule(run_dir, round_n, viols)
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
    import validate_critique_yaml as V
    draft = os.path.exists(os.path.join(run_dir, "manuscript.md"))
    closed = set(map(str, decision.get("resolved_by_test") or [])) | set(map(str, decision.get("outside_brief") or []))
    out = []
    for reviewer, name in V.FILENAMES.items():
        p = os.path.join(run_dir, name)
        if not os.path.exists(p) or (reviewer == "critique-presentation" and not draft):
            continue
        try:
            import yaml
            doc = yaml.safe_load(open(p)) or {}
        except Exception:  # noqa: BLE001 -- the gate already failed closed on an unreadable file
            continue
        for b in doc.get("blockers") or []:
            if (not isinstance(b, dict) or b.get("resolved") or str(b.get("severity")) not in ("HIGH", "MEDIUM")
                    or str(b.get("id")) in closed):
                continue
            if b.get("target_stage") == "WRITE" and not draft:
                continue
            dw = b.get("done_when")
            out.append({"id": str(b.get("id")), "source": reviewer, "severity": b.get("severity"),
                        "tested": dw is not None and not DW.problems(dw), "claim": str(b.get("claim") or "")[:300]})
    critic_open = {o["id"] for o in out}
    seen = set()
    for v in decision.get("rigor_violations") or []:
        if not (isinstance(v, dict) and str(v.get("severity")) in ("HIGH", "MEDIUM")):
            continue
        fid = FL.gate_id(v)
        # Every gate finding counts (a resolved blocker whose test fails is done_when_unmet; a missing
        # presentation review is critique_presentation_missing); only one that restates an open
        # critic blocker (same id as its key) is a duplicate.
        if str(v.get("key") or "") in critic_open or fid in seen:
            continue
        seen.add(fid)
        out.append({"id": fid, "source": "harness", "severity": v.get("severity"),
                    "tested": not str(v.get("kind") or "").startswith("critique_done_when_missing"),
                    "claim": str(v.get("claim") or "")[:300]})
    return out


def apply_medium_rule(decision: dict, opened: list[dict], rounds_remaining: int, budget_ok: bool) -> dict:
    """v2: the gate never accepts an open HIGH or MEDIUM finding. Today's rules ACCEPT when no HIGH
    is open (rule 4); in v2 an open finding with a test goes to the fix loop while rounds and
    budget remain (rule 4m) and is declared, with the write-up saying so, when they do not (3m).
    The decision's action and rationale say what the machine then does."""
    if decision.get("action") != "ACCEPT" or not opened:
        return decision
    d = dict(decision)
    tested = [f["id"] for f in opened if f["tested"]]
    if tested and rounds_remaining > 0 and budget_ok:
        d.update(action="PATCH_OR_RETHINK", rule_matched="4m", open_tested=tested,
                 rationale=(f"No HIGH finding is open, but {len(tested)} finding(s) with a test are "
                            f"({', '.join(tested)[:300]}) and {rounds_remaining} round(s) remain: they go to the "
                            "fix loop before ACCEPT."))
    else:
        why = "no rounds remain" if rounds_remaining <= 0 else ("the budget is used" if not budget_ok
                                                                 else "none has a test the fix loop can work to")
        d.update(action="DECLARE_SCOPE", rule_matched="3m", declared_open=[f["id"] for f in opened],
                 rationale=(f"{len(opened)} HIGH/MEDIUM finding(s) are still open and {why}: they are declared "
                            "in the write-up (declared_findings.yaml), not accepted."))
    return d


DECLARED_FILE = "declared_findings.yaml"


def write_declared(run_dir: str, opened: list[dict], decision: dict) -> None:
    import yaml
    with open(os.path.join(run_dir, DECLARED_FILE), "w") as f:
        yaml.safe_dump({"rule": decision.get("rule_matched"), "rationale": decision.get("rationale"),
                        "declared": [{"id": o["id"], "severity": o["severity"], "source": o["source"],
                                      "claim": o["claim"]} for o in opened]}, f, sort_keys=False)


# ---- provenance of inputs
_INPUT_EXT = (".csv", ".tsv", ".json", ".yaml", ".yml", ".txt", ".xlsx", ".parquet")
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


def _question_paths(question: str) -> list[str]:
    """File names in the question: bare tokens, and quoted or backticked ones (which may hold spaces)."""
    import re
    q = question or ""
    toks = re.findall(r"[`\"']([^`\"'\n]+)[`\"']", q) + re.findall(r"[A-Za-z0-9_./-]+", q)
    return [t.strip().strip("./") for t in toks if t.strip().lower().endswith(_INPUT_EXT)]


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


def _copy_nofollow(src: str, dest: str) -> None:
    data = open(src, "rb").read()
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o644)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


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
        _copy_nofollow(src, dest)
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
    try:
        spec = yaml.safe_load(open(os.path.join(run_dir, AB.SPEC_FILE))) or {}
    except Exception as e:  # noqa: BLE001
        return [f"{AB.SPEC_FILE} unreadable: {type(e).__name__}"]
    found = []

    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                if k not in ("deliverable", "deliverable_file"):      # outputs the run writes, not inputs
                    walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
        elif isinstance(x, str) and x.strip().lower().endswith(_INPUT_EXT):
            found.append(x.strip())

    def walk_keys(x):          # a path field by name, whatever its spelling or extension
        if isinstance(x, dict):
            for k, v in x.items():
                if k in ("deliverable", "deliverable_file"):
                    continue
                if k in ("csv", "file", "path", "table") and isinstance(v, str) and v.strip():
                    found.append(v.strip())
                walk_keys(v)
        elif isinstance(x, list):
            for v in x:
                walk_keys(v)
    def walk_paths(x, key=""):  # any value that names an existing file or directory (engine config too)
        if isinstance(x, dict):
            for k, v in x.items():
                if k not in ("deliverable", "deliverable_file"):
                    walk_paths(v, str(k))
        elif isinstance(x, list):
            for v in x:
                walk_paths(v, key)
        elif isinstance(x, str) and x.strip() and ("/" in x or os.sep in x or key.endswith(("path", "file", "dir"))):
            v = x.strip()
            if ":" in v and not v.startswith("/") and "/" not in v.split(":", 1)[0]:
                return                 # a module reference such as pkg.mod:Class
            if os.path.exists(os.path.join(run_dir, os.path.expanduser(v))):
                found.append(v)
    walk(spec)
    walk_keys(spec)
    walk_paths(spec)
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
        claims = RCL.load_claims(lp)
    except Exception as e:  # noqa: BLE001 -- fail closed
        return [{"kind": "report_claims_unreadable", "severity": "HIGH", "stage": "WRITE", "target_stage": "WRITE",
                 "key": "claims", "scope_declarable": False, "claim": f"claims ledger unreadable: {e}"}]
    # The writer's own text, all of it: what the harness appends is not in manuscript.md, so no
    # marker the writer could copy can hide anything.
    text = open(rp).read()
    out = []
    for cid in sorted(set(re.findall(r"\[CLAIM:([^\]]*)\]", text)) - set(claims)):
        out.append({"kind": "report_claim_unresolved", "severity": "HIGH", "stage": "WRITE", "target_stage": "WRITE",
                    "key": cid, "scope_declarable": False,
                    "claim": f"The report has [CLAIM:{cid}], which is not in the claims ledger."})
    for cid in sorted(c for c in claims if "." in c or "_" in c):     # a plain word is not an id
        if re.search(r"(?<![A-Za-z0-9_.:\-])" + re.escape(cid) + r"(?![A-Za-z0-9_\-]|\.[A-Za-z0-9_])", text):
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


def sign(run_dir: str, by: str, note: str = "") -> dict:
    rec = {"by": by, "note": note, "at": _now(), "spec_sha256": signed_hash(run_dir)}
    if rec["spec_sha256"] is None:
        raise ValueError(f"no {AB.SPEC_FILE} to sign")
    with open(os.path.join(run_dir, SIGNOFF_FILE), "w") as f:
        json.dump(rec, f, indent=1)
    return rec


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
        self.st = st or state
        if self.st is None:
            raise ValueError("no run_state.json and no initial state")
        self._settle_interrupted()
        save_state(self.run_dir, self.st)

    def _settle_interrupted(self) -> None:
        """Every journal entry left `started` by a previous process was interrupted: charge its
        allowance once (the task cap when it had none) and mark it, before anything else runs."""
        for j in self.st.journal.values():
            if isinstance(j, dict) and j.get("status") == "started":
                a = j.get("allowance_usd")
                self.st.spent_usd += float(a if a is not None else self.st.task_cap_usd)
                j["interrupted_charged"] = j.get("interrupted_charged", 0) + 1
                j["status"] = "interrupted"

    # -------------------------------------------------- plumbing
    def _go(self, to: str, why: str = "", status: str | None = None) -> str:
        frm = self.st.state
        if to not in EDGES[frm]:
            raise RuntimeError(f"transition {frm} -> {to} is not in the state table")
        self.st.history.append({"at": _now(), "from": frm, "to": to, "why": why, "round": self.st.round,
                                "spent_usd": round(self.st.spent_usd, 4)})
        self.st.state = to
        if to == "FINAL":
            self.st.status = self.st.forced_status or status or "completed_gate_error"
            self._finish()
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

    def _task(self, key: str, name: str, *args, writeup: bool = False):
        """One paid task through the journal. Returns its TaskResult, or None when the run has no
        allowance left for it (the caller takes the budget path). A completed task is not run
        again (its result is replayed, already charged); a task found started but not completed
        was interrupted: it is charged its allowance once, then run again."""
        from v2_tasks import TaskResult
        j = self.st.journal.get(key)
        if j and j.get("status") == "completed":
            return TaskResult(bool(j.get("ok")), j.get("output"), 0.0, j.get("note", ""), j.get("session_id"))
        if j and j.get("status") == "started":
            a = j.get("allowance_usd")
            self.st.spent_usd += float(a if a is not None else self.st.task_cap_usd)
            j["interrupted_charged"] = j.get("interrupted_charged", 0) + 1
            j["status"] = "interrupted"
            save_state(self.run_dir, self.st)
        allowance = self._allowance(writeup)
        if allowance is not None and allowance <= 0:
            return None
        j = dict(j or {}, task=name, status="started", started=_now(), allowance_usd=allowance)
        self.st.journal[key] = j
        save_state(self.run_dir, self.st)
        setter = getattr(self.tasks, "set_allowance", None)
        if setter:
            a = allowance if allowance is not None else self.st.task_cap_usd
            b = self._budget()
            # A crash read-back is a paid call too: a small cap, from what is left after this allowance.
            rb = READBACK_CAP_USD if b is None else max(0.0, min(READBACK_CAP_USD, round(b["left"] - a, 4)))
            setter(a, readback_usd=rb)
        res = getattr(self.tasks, name)(*args)
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

    def _budget(self) -> dict | None:
        cap = self.st.budget_usd
        if not cap:
            return None
        from agents.run_budget import writeup_reserve
        left = cap - writeup_reserve(cap) - self.st.spent_usd
        return {"spent": round(self.st.spent_usd, 4), "cap": cap, "left": round(left, 4)}

    def _out_of_budget(self) -> bool:
        b = self._budget()
        return b is not None and b["left"] <= 0

    def _has_draft(self) -> bool:
        return os.path.exists(os.path.join(self.run_dir, "manuscript.md"))

    def _spec(self) -> dict:
        import yaml
        return yaml.safe_load(open(os.path.join(self.run_dir, AB.SPEC_FILE))) or {}

    def harness_findings(self) -> list[dict]:
        out = self.backend.gate_findings(self.run_dir) + spec_signoff_findings(self.run_dir, self.st.signoff, self.backend)
        out += [{"kind": "deliverable_path_invalid", "severity": "HIGH", "stage": "PLAN", "target_stage": "PLAN",
                 "key": "deliverable", "scope_declarable": False, "claim": p} for p in deliverable_problems(self.run_dir)]
        if self.st.v2_rules:
            out += report_claim_findings(self.run_dir)
        return out

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

    def _signed_by(self):
        """Who signed the current spec, or None when it is unsigned or changed since."""
        if getattr(self.backend, "sign", None):
            if self.backend.signoff_status(self.run_dir) != "signed":
                return None
            try:
                from toolkit.spec import signoff
                rec = signoff._spec_record(self.run_dir) or {}
            except Exception:  # noqa: BLE001
                rec = {}
            return rec.get("signed_by") or rec.get("by") or "owner"
        rec = _signoff(self.run_dir)
        return rec.get("by") if rec and rec.get("spec_sha256") == signed_hash(self.run_dir) else None

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
        return {FL.gate_id(v) for v in self.harness_findings()}

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
        self.st.paused = None
        if s in LLM_STATES and self._out_of_budget():
            return self._budget_out(s)
        return getattr(self, "h_" + s)()

    def run(self, max_steps: int = 200) -> str:
        for _ in range(max_steps):
            before = (self.st.state, len(self.st.history))
            self.step()
            if self.st.state == "FINAL" or self.st.paused:
                break
            if (self.st.state, len(self.st.history)) == before:
                raise RuntimeError(f"state {self.st.state} made no progress")
        save_state(self.run_dir, self.st)
        return self.st.status or (f"paused:{self.st.paused}" if self.st.paused else self.st.state)

    # -------------------------------------------------- handlers
    def h_SPEC(self):
        n = self.st.tries.get("spec", 0)
        if n >= MAX_SPEC_TRIES:
            return self._go("FINAL", "the spec did not validate", "run_failed")
        stage_inputs(self.run_dir, self.st.question)
        fb = self.st.tries.get("spec_feedback")
        # The key is fixed before the call and the try is counted only once the call returns, so an
        # interrupted draft resumes under the same key (charged once by _settle_interrupted).
        res = self._task(f"SPEC#{self._visit()}:try{n + 1}", "draft_spec", self.st.question, self.run_dir, fb)
        self.st.tries["spec"] = n + 1
        if res is None:
            return self._budget_out("SPEC")
        errs = [f"draft_spec failed: {res.note}"] if not res.ok else []
        if res.ok:
            try:
                errs = record_cited_inputs(self.run_dir)       # before anything reads a cited file
                errs = errs or self.backend.pre_sign_problems(self.run_dir)
            except Exception as e:  # noqa: BLE001
                errs = [f"spec unreadable: {e}"]
        if errs:
            self.st.tries["spec_feedback"] = errs
            return self._go("SPEC", f"spec invalid: {'; '.join(errs)[:200]}")
        self.st.tries.pop("spec_feedback", None)
        return self._go("SIGN", "spec drafted and valid")

    def h_SIGN(self):
        mode = self.st.signoff
        if mode == "none":
            return self._go("EVIDENCE", "sign-off not required (--signoff none)")
        by = self._signed_by()
        if by and (mode == "lead" or by == "owner"):
            return self._go("EVIDENCE", f"spec signed by {by}")
        if mode == "lead":
            res = self._task(f"SIGN#{self._visit()}:sign_spec", "sign_spec", self.run_dir)
            note = (_signoff(self.run_dir) or {}).get("note", "")
            if res is not None and res.ok:
                if os.path.exists(os.path.join(self.run_dir, SIGNOFF_FILE)) and getattr(self.backend, "sign", None):
                    os.remove(os.path.join(self.run_dir, SIGNOFF_FILE))   # the lead's note; B writes its own record
                try:
                    self._sign("lead", note)              # the harness records the hash
                    return self._go("EVIDENCE", "spec signed by the lead")
                except ValueError as e:
                    self.log(f"[machine] the spec could not be signed: {e}")
        self.st.paused = "awaiting_signoff"
        self.st.status = None
        save_state(self.run_dir, self.st)
        self.log(f"[machine] waiting for the owner: python3 agents/stage_machine.py sign {self.run_dir} --by owner")
        return "SIGN"

    def h_EVIDENCE(self):
        # Stub (workstream V5): every parameter is an assumption until the evidence service checks it.
        os.makedirs(os.path.join(self.run_dir, "evidence"), exist_ok=True)
        with open(os.path.join(self.run_dir, "evidence", "registry.yaml"), "w") as f:
            f.write("# evidence service stub (v2 C): no parameter has been fetched or matched yet\n"
                    "status: stub\nentries: []\n")
        return self._go("ENGINE_CHOICE", "evidence stub")

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
        res = self._task(f"ENGINE_CHOICE#{self._visit()}:pick_engine", "pick_engine", spec, rows)
        if res is None:
            return self._budget_out("ENGINE_CHOICE")
        pick = (res.output or {}).get("engine") if res.ok else None
        if pick not in {r["ref"] for r in eligible}:
            pick, why = eligible[0]["ref"], f"harness: the pick {pick!r} is not eligible; first eligible engine"
        else:
            why = (res.output or {}).get("why", "")
        cfg = next((r.get("config") for r in eligible if r["ref"] == pick), None) or {}
        rec.update(AB.engine_choice_record(pick, why, rows, config=cfg))
        if self.cross and len(eligible) > 1:
            try:
                rec["cross_check"] = self.backend.cross_check(
                    spec, [pick] + [r["ref"] for r in eligible if r["ref"] != pick][:1],
                    os.path.join(self.run_dir, "analysis", "cross_check"))
            except Exception as e:  # noqa: BLE001 -- recorded; the check is optional
                rec["cross_check"] = {"error": f"{type(e).__name__}: {e}"}
        _write_json(self.run_dir, AB.ENGINE_CHOICE, rec)     # the spec stays as signed
        self.st.engine = pick
        return self._go("CONFIGURE", f"engine {pick}")

    def h_CONFIGURE(self):
        import pipeline as P
        import yaml
        errs = self.backend.validate(os.path.join(self.run_dir, AB.SPEC_FILE))
        if errs:
            return self._go("FINAL", f"spec invalid for the engine: {'; '.join(errs)[:200]}", "run_failed")
        os.makedirs(os.path.join(self.run_dir, "models"), exist_ok=True)
        with open(os.path.join(self.run_dir, "models", "pipeline.yaml"), "w") as f:
            yaml.safe_dump({"version": 1, "steps": self.backend.pipeline_steps(
                os.path.join(self.run_dir, AB.SPEC_FILE), self.run_dir)}, f, sort_keys=False)
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
            return self._go("FINAL", f"analysis failed: {_rep_error(rep)}", "run_failed")
        return self._go("CRITIQUE", "analysis built")

    def h_CRITIQUE(self):
        if self.st.round_pending:
            self.st.round += 1
            self.st.round_pending = False
        if self.st.round > self.st.max_rounds:
            return self._go("FINAL", "round cap passed (gate did not stop the run)", "completed_gate_error")
        v = self._visit()
        fb = self.st.tries.get("schema_feedback") or {}
        critics = list(CRITICS) if (not fb or "*" in fb) else [r for r in fb if r in CRITICS or r == "critique-presentation"]
        for reviewer in critics:
            args = (self.run_dir, reviewer, self.st.round) + ((fb.get(reviewer) or fb.get("*"),) if fb else ())
            res = self._task(f"CRITIQUE#{v}:r{self.st.round}:{reviewer}", "critique", *args)
            if res is None:
                return self._budget_out("CRITIQUE")
        self.st.tries.pop("schema_feedback", None)
        return self._go("GATE", f"critique round {self.st.round}")

    def h_GATE(self):
        pw = self.st.post_write
        if self.st.gate_mode == "legacy":
            d = legacy_gate(self.run_dir, self.st.round, self.st.max_rounds, pw)
        else:
            sv = getattr(self.backend, "spec_violations", None)
            rv = getattr(self.backend, "registry_violations", None)
            d = v2_gate(self.run_dir, self.st.round, self.st.max_rounds, pw, self.harness_findings(),
                        spec_violations=sv(self.run_dir) if sv else None,
                        registry_violations=rv(self.run_dir) if rv else None,
                        strict_done_when=self.st.v2_rules and self.st.tries.get("schema", 0) < MAX_SCHEMA_REPAIRS,
                        append_history=False)
            opened = []
            if d.get("action") and self.st.v2_rules:
                opened = open_findings(self.run_dir, d)
                d = apply_medium_rule(d, opened, int(d.get("rounds_remaining") or 0), not self._out_of_budget())
            if d.get("action"):
                import validate_critique_yaml as V
                V._append_validator_history(self.run_dir, self.st.round, d, pw)
            if self.st.v2_rules and d.get("action") == "DECLARE_SCOPE":
                # Every DECLARE_SCOPE (rule 3, 3a or 3m) lists what is still open in the write-up.
                write_declared(self.run_dir, opened, d)
                if pw and self._has_draft():
                    render_report(self.run_dir, self.backend)
        tag = f"gate_r{self.st.round}{'_post' if pw else ''}.json"
        _write_json(self.run_dir, tag, d or {})
        if pw:
            _write_json(self.run_dir, "final_gate.json", d or {})
        self.st.last_gate = {k: (d or {}).get(k) for k in ("action", "rule_matched", "rounds_remaining", "rationale")}
        self.st.last_gate.update(round=self.st.round, post_write=pw)
        if (d or {}).get("action"):
            FL.sync(self.run_dir, d, self.st.round, post_write=pw)
            if d.get("rule_matched") == "4m":           # these are why the loop runs: plan them
                led = FL.load(self.run_dir)
                for fid in d.get("open_tested") or []:
                    if fid in led:
                        led[fid].blocking = True
                FL.save(self.run_dir, led)
        nxt, status, mode = route_gate(d, pw)
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
                           budget=self._budget, log=self.log, guard=self.fix_guard, protect=PROTECTED_BY_FIX)
        self.st.history.append({"at": _now(), "fix_loop": summ})
        if summ.get("rethink"):
            return self._go(summ["rethink"], f"the planner chose a rethink ({summ['rethink']})")
        return self._go("CRITIQUE", f"fix loop: {summ['rows']} task(s), {summ['passed']} passed their checks")

    def h_OWNER_DECISION(self):
        try:
            answered = bool((json.load(open(os.path.join(self.run_dir, SCOPE_SIGNOFF))) or {}).get("answers"))
        except (OSError, ValueError):
            answered = False
        if answered:
            return self._go("GATE", "the owner answered the scope declarations")
        self.st.paused = "awaiting_scope_signoff"
        save_state(self.run_dir, self.st)
        return "OWNER_DECISION"

    def h_WRITE(self):
        mode = self.st.write_mode or "not_accepted"
        res = self._task(f"WRITE#{self._visit()}:{mode}", "write", self.run_dir, mode, writeup=True)
        if res is None:
            return self._go("FINAL", "no budget left for the write-up", "completed_no_report")
        if not res.ok or not self._has_draft():
            return self._go("FINAL", "the writer produced no draft", "completed_no_report")
        render_report(self.run_dir, self.backend)
        self.st.post_write = True
        if mode == "not_accepted":
            return self._go("GATE", "not-accepted report written; final gate")
        return self._go("PRESENTATION", f"draft written ({mode})")

    def h_PRESENTATION(self):
        res = self._task(f"PRESENTATION#{self._visit()}:r{self.st.round}", "critique", self.run_dir,
                         "critique-presentation", self.st.round)
        if res is None:
            return self._budget_out("PRESENTATION")
        return self._go("GATE", "presentation review of the draft")

    def _last_decision(self) -> dict | None:
        for name in (f"gate_r{self.st.round - (1 if self.st.round_pending else 0)}_post.json",
                     f"gate_r{self.st.round - (1 if self.st.round_pending else 0)}.json"):
            try:
                return json.load(open(os.path.join(self.run_dir, name)))
            except (OSError, ValueError):
                continue
        return None

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
    p = os.path.join(run_dir, rel)
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1, default=str)
    os.replace(tmp, p)


def _rep_error(rep: dict) -> str:
    errs = list(rep.get("errors") or []) + [f"{s.get('id')}: {s.get('error')}" for s in rep.get("steps") or []
                                            if s.get("status") != "ok"]
    return "; ".join(str(e) for e in errs)[:300]


def _with_abs_paths(spec: dict, run_dir: str) -> dict:
    return AB._abs_spec(spec, run_dir)


def _pre_engine_check(spec: dict) -> list[str]:
    """Before an engine is chosen: the sections every engine needs are present."""
    from spike.analysis.spec import REQUIRED
    return [f"missing section {k}" for k in REQUIRED if k != "engine" and k not in spec]


def render_report(run_dir: str, backend=AB) -> str:
    """report.md = the writer's draft plus the blocks the harness renders from artifacts: the
    headline numbers, the declared limits (verbatim), and the deliverable file the spec names."""
    import yaml
    draft = open(os.path.join(run_dir, "manuscript.md")).read()
    lp = os.path.join(run_dir, "models", "claims_ledger.yaml")
    if os.path.exists(lp):
        # The writer writes [CLAIM:id]; the harness substitutes the value (no number is typed).
        import render_claims as RCL
        try:
            draft, _unresolved = RCL.render_text(draft, RCL.load_claims(lp))
        except Exception:  # noqa: BLE001 -- left unrendered; report_claim_findings flags it
            pass
    h = backend.headline(run_dir)
    lines = ["", "<!-- harness: rendered from analysis/analysis.json -->", "## Numbers (rendered by the harness)", "",
             "| quantity | value |", "|---|---|"]
    lines += [f"| {k} | {v:,.4g} |" if isinstance(v, (int, float)) else f"| {k} | {v} |" for k, v in h.items()]
    try:
        decl = (yaml.safe_load(open(os.path.join(run_dir, "scope_declaration.yaml"))) or {}).get("declarations") or []
    except OSError:
        decl = []
    if decl:
        lines += ["", "## Declared limits (rendered verbatim from scope_declaration.yaml)", ""]
        for e in decl:
            lines.append(f"- **{e.get('id')}** ({e.get('blocker_id')}): {e.get('why_unresolved', '')} "
                         f"Impact: {e.get('impact_on_conclusions', '')}")
    try:
        dec2 = (yaml.safe_load(open(os.path.join(run_dir, DECLARED_FILE))) or {})
    except OSError:
        dec2 = {}
    if dec2.get("declared"):
        lines += ["", "## Open findings, declared (not resolved)", "", str(dec2.get("rationale") or ""), ""]
        lines += [f"- **{e['id']}** ({e['severity']}, {e['source']}): {e['claim']}" for e in dec2["declared"]]
    with open(os.path.join(run_dir, "report.md"), "w") as f:
        f.write(draft.rstrip() + "\n" + "\n".join(lines) + "\n")
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
    with open(os.path.join(run_dir, df["path"]), "w", newline="") as f:
        w = csv.writer(f)
        cols = df["columns"]
        w.writerow(list(cols))
        for r in rows:
            w.writerow([names.get(r[c], r[c]) if c == "package" else r[c] for c in cols.values()])


def init_run(run_dir: str, question: str, max_rounds: int = 3, signoff: str = "owner", gate_mode: str = "v2",
             budget_usd: float | None = None) -> RunState:
    os.makedirs(run_dir, exist_ok=True)
    st = load_state(run_dir)
    if st is not None:
        return st
    st = RunState(question=question, max_rounds=max_rounds, signoff=signoff, gate_mode=gate_mode, budget_usd=budget_usd)
    _write_json(run_dir, "metadata.json", {"question": question, "started": _now(), "pipeline": "v2",
                                           "max_rounds": max_rounds, "signoff": signoff})
    save_state(run_dir, st)
    return st


def run_v2(question: str, run_dir: str, tasks, max_rounds: int = 3, signoff: str = "owner",
           budget_usd: float | None = None, log=print) -> str:
    """Entry point for main.py --v2 (and the dry run). Resumes when run_state.json exists."""
    st = init_run(run_dir, question, max_rounds, signoff, "v2", budget_usd)
    m = Machine(run_dir, tasks, st, log=log)
    return m.run()


# ------------------------------------------------------------------ self-test
def _self_test() -> int:
    import tempfile
    import shutil
    from v2_tasks import StubTasks
    fails = []
    # T10-T35 use the spike adapter's toy spec, so they pin that backend: since workstream B
    # merged, the default backend is B's, whose spec schema the spike fixtures do not follow.
    spike = AB.backend("spike")

    def _SpikeMachine(*a, **k):
        k.setdefault("backend", spike)
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

    with tempfile.TemporaryDirectory() as td:
        def new_run(name, script, signoff="none", max_rounds=3, budget=None):
            d = os.path.join(td, name)
            os.makedirs(d)
            AB.write_toy_units(os.path.join(d, "units.csv"), n=12)
            spec = AB.toy_spec("units.csv")
            spec.pop("engine")
            spec["deliverable_file"] = {"path": "allocation_optimized.csv",
                                        "columns": {"unit": "id", "package": "package", "dalys_averted": "dalys_averted"}}
            spec.update(script.pop("spec_extra", {}))
            # 16 emulator points: the resume test runs the whole machine many times
            script = dict({"spec": spec, "settings": AB.toy_settings(draws=24, points=16)}, **script)
            st = init_run(d, "toy question", max_rounds, signoff, "v2", budget)
            return d, StubTasks(script), st

        # T10: the happy path with one critic finding fixed in the loop.
        blocker = {"id": "D-001", "severity": "HIGH", "target_stage": "ANALYZE", "claim": "too few draws",
                   "done_when": [{"file": "analysis_settings.yaml", "key": "uncertainty.draws", "op": ">=", "value": 32}]}
        d, T, st = new_run("happy", {"critics": {1: {"critique-domain": [blocker]}},
                                     "fixes": {"D-001": [("analysis_settings.yaml", "uncertainty.draws", 32)],
                                               "uncertainty_draws_low:draws": [("analysis_settings.yaml", "uncertainty.draws", 32)]}})
        status = _SpikeMachine(d, T, st, log=lambda *a: None, cross_engine_check=False).run()
        hist = [h["to"] for h in load_state(d).history if "to" in h]
        ok(status == "completed", f"T10 toy run completes: {status} via {hist}")
        ok(hist[:6] == ["SIGN", "EVIDENCE", "ENGINE_CHOICE", "CONFIGURE", "ANALYZE", "CRITIQUE"] and "FIX_LOOP" in hist
           and hist[-3:] == ["PRESENTATION", "GATE", "FINAL"], f"T11 the path SPEC..FIX_LOOP..WRITE..FINAL {hist}")
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
        ref_status = _SpikeMachine(d0, T0, st0, log=lambda *a: None, cross_engine_check=False).run()
        ref_path = [h["to"] for h in load_state(d0).history if "to" in h]
        bad = []
        for k in range(1, len(ref_path)):
            dk, Tk, stk = new_run(f"resume{k}", {"critics": {1: {"critique-domain": [blocker]}},
                                                "fixes": {"D-001": [("analysis_settings.yaml", "uncertainty.draws", 32)]}})
            m = _SpikeMachine(dk, Tk, stk, log=lambda *a: None, cross_engine_check=False)
            for _ in range(k):
                m.step()
            del m
            m2 = _SpikeMachine(dk, StubTasks(Tk.s), None, log=lambda *a: None, cross_engine_check=False)   # from run_state.json
            s2 = m2.run()
            p2 = [h["to"] for h in load_state(dk).history if "to" in h]
            if s2 != ref_status or p2 != ref_path:
                bad.append((k, s2, p2))
            shutil.rmtree(dk)
        ok(not bad, f"T16 a run stopped after any of {len(ref_path) - 1} transitions resumes to the same path and status {bad[:1]}")

        # T17: owner sign-off pauses, persists, resumes.
        d, T, st = new_run("owner", {}, signoff="owner")
        m = _SpikeMachine(d, T, st, log=lambda *a: None, cross_engine_check=False)
        s = m.run()
        ok(s == "paused:awaiting_signoff" and load_state(d).state == "SIGN", f"T17 owner mode pauses at SIGN ({s})")
        sign(d, "owner", "ok")
        ok(_SpikeMachine(d, StubTasks(T.s), None, log=lambda *a: None, cross_engine_check=False).run() == "completed",
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
                signed_before = signed_before or _signoff(run_dir)["spec_sha256"]
                return super().fix(task, run_dir, evidence)
        s = _SpikeMachine(d, Spy(T.s), st, log=lambda *a: None, cross_engine_check=False).run()
        rows = FX.impact_rows(d)
        led = FL.load(d)
        ok(rows and all(r.get("rejected") and not r.get("steps_rebuilt") for r in rows)
           and signed_hash(d) == signed_before and "constraints.envelope_usd" not in AB._leaves(
               __import__("yaml").safe_load(open(os.path.join(d, AB.SETTINGS_FILE))) or {})
           and "spec_changed_after_signoff:spec" not in led and s == "completed_scope_declared",
           f"T19 edits to the signed spec are rejected and undone before any rebuild ({s}, {[r.get('rejected') for r in rows][:1]})")

        # T20: a structural mismatch is a back-edge to ENGINE_CHOICE, then the run continues.
        d, T, st = new_run("rethink", {"structural": {1: "critique-domain"}})
        s = _SpikeMachine(d, T, st, log=lambda *a: None, cross_engine_check=False).run()
        p = [h["to"] for h in load_state(d).history if "to" in h]
        i = p.index("GATE")
        ok(p[i + 1] == "ENGINE_CHOICE" and s == "completed" and load_state(d).round == 2,
           f"T20 RETHINK_STRUCTURAL goes back to ENGINE_CHOICE and the next critique is round 2 ({s})")

        # T21: an open blocker at the last round: DECLARE_SCOPE when declarable.
        nofix = {"id": "D-009", "severity": "HIGH", "target_stage": "ANALYZE", "claim": "cannot fix"}
        d, T, st = new_run("declare", {"critics": {1: {"critique-domain": [nofix]}}}, max_rounds=2)
        s = _SpikeMachine(d, T, st, log=lambda *a: None, cross_engine_check=False).run()
        ok(s == "completed_scope_declared", f"T21 an unfixed critic blocker at exhaustion ends scope-declared ({s})")
        rep21 = open(os.path.join(d, "report.md")).read()
        ok(os.path.exists(os.path.join(d, DECLARED_FILE)) and "D-009" in rep21 and "Open findings, declared" in rep21,
           "T46 (Codex #95-5) an ordinary DECLARE_SCOPE (rule 3) also lists what is open in the report")

        # T22: the budget, not the gate, ends the run: the harness writes the report.
        d, T, st = new_run("budget", {"critics": {1: {"critique-domain": [nofix]}}, "usd": 30.0}, budget=100.0)
        s = _SpikeMachine(d, T, st, log=lambda *a: None, cross_engine_check=False).run()
        p = [h["to"] for h in load_state(d).history if "to" in h]
        ok(s == "budget_exhausted" and "WRITE" in p and os.path.exists(os.path.join(d, "report.md")),
           f"T22 with the budget used the run ends budget_exhausted with a report ({s}, {p[-4:]})")

        # T25: an interrupted paid task. The redteam critique crashes once in round 1: on resume the
        # domain critique (completed) is not run again, and the interrupted one is charged its allowance.
        d, T, st = new_run("crash", {"crash": {"critique:critique-redteam:1": 1}, "usd": 1.0}, budget=100.0)
        m = _SpikeMachine(d, T, st, log=lambda *a: None, cross_engine_check=False)
        try:
            m.run()
            ok(False, "T25 the stub crash propagates")
        except RuntimeError:
            pass
        spent_at_crash = load_state(d).spent_usd
        T2 = StubTasks(dict(T.s, crash={}))
        s = _SpikeMachine(d, T2, None, log=lambda *a: None, cross_engine_check=False).run()
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
        s = _SpikeMachine(d, T, st, log=lambda *a: None, cross_engine_check=False).run()
        ok(T.allowances == [9.0, 9.0, 2.0, 3.0] and "critique:critique-redteam:1" not in T.calls
           and s == "budget_exhausted",
           f"T27 allowances follow the budget and the pair stops when it is used ({T.allowances}, {s})")
        jj = load_state(d).journal
        ok(all("allowance_usd" in e for k, e in jj.items() if not k.endswith("spec_sha256")),
           "T28 every journal entry records the allowance it was given")

        # T29: the deliverable destination must be a plain file inside the run, not one the harness owns.
        bad_paths = {}
        for name, path in (("escape", "../escape.csv"), ("owned", "run_state.json"), ("report", "report.md"),
                           ("link", "out/link.csv")):
            d, T, st = new_run(f"deliv_{name}", {"spec_extra": {"deliverable_file": {
                "path": path, "columns": {"unit": "id", "package": "package"}}}}, max_rounds=1)
            if name == "link":
                os.makedirs(os.path.join(td, "elsewhere"), exist_ok=True)
                os.symlink(os.path.join(td, "elsewhere"), os.path.join(d, "out"))
            s = _SpikeMachine(d, T, st, log=lambda *a: None, cross_engine_check=False).run()
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
        ok(_owner_rule() is None and g["action"] == "RUN_FAILED",
           f"T30 without the owner rule the finding is not declarable: RUN_FAILED, not DECLARE_SCOPE ({g['action']})")
        seen = {}

        class Rule:
            @staticmethod
            def gate_findings(rd, rn, viols, non_declarable=None):
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

        # T33 (Codex R2-1): the first spec draft is interrupted. On resume the started-but-uncompleted
        # entry is charged its allowance once and the draft runs again under the same key.
        d, T, st = new_run("spec_crash", {"crash": {"draft_spec": 1}, "usd": 1.0}, budget=100.0)
        try:
            _SpikeMachine(d, T, st, log=lambda *a: None, cross_engine_check=False).run()
        except RuntimeError:
            pass
        T2 = StubTasks(dict(T.s, crash={}))
        m2 = _SpikeMachine(d, T2, None, log=lambda *a: None, cross_engine_check=False)
        s = m2.run()
        j = load_state(d).journal
        spec_keys = sorted(k for k in j if k.startswith("SPEC#"))
        ok(s == "completed" and spec_keys == ["SPEC#0:try1"] and j["SPEC#0:try1"].get("interrupted_charged") == 1
           and j["SPEC#0:try1"]["status"] == "completed",
           f"T33 an interrupted spec draft is charged once and re-run under its own key {spec_keys}")
        d, T, st = new_run("orphan", {}, budget=100.0)
        st.journal["X#0:orphan"] = {"task": "critique", "status": "started", "allowance_usd": 2.5}
        save_state(d, st)
        m3 = _SpikeMachine(d, StubTasks({}), None, log=lambda *a: None)
        ok(abs(m3.st.spent_usd - 2.5) < 1e-9 and m3.st.journal["X#0:orphan"]["status"] == "interrupted"
           and abs(_SpikeMachine(d, StubTasks({}), None, log=lambda *a: None).st.spent_usd - 2.5) < 1e-9,
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
            st = init_run(d, "toy question", 3, "lead", "v2", None)
            s = Machine(d, T, st, log=lambda *a: None, cross_engine_check=False).run()
            rows = FX.impact_rows(d)
            man = os.path.exists(os.path.join(d, "analysis", "manifest.json"))
            ok(s == "completed" and man and rows and rows[0]["files_changed"] == ["analysis_settings.yaml"]
               and FL.load(d)["D-001"].status == "fixed",
               f"T36 the default backend (workstream B) runs the machine end to end with engine: auto ({s})")
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
        s = _SpikeMachine(d, T, st, log=lambda *a: None, cross_engine_check=False).run()
        ok(s in ("completed", "completed_scope_declared") and "feedback:critique-domain:True" in T.calls
           and T.calls.count("critique:critique-redteam:1") == 1,
           f"T37 a schema repair re-runs only the failing critic, with its errors ({s}, {[c for c in T.calls if 'critique' in c][:6]})")

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
        render_report(fr, AB)
        rep = open(os.path.join(fr, "report.md")).read()
        ok("[CLAIM:" not in rep and "headline.point" not in rep.split("<!-- harness")[0] and not report_claim_findings(fr),
           f"T40 claim references are rendered inline ({rep.splitlines()[0][:120]})")
        open(os.path.join(fr, "manuscript.md"), "w").write("ITN in `mix.itn` districts; [CLAIM:no.such.claim].\n")
        render_report(fr, AB)
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

        # T42: through the machine. A MEDIUM with a runnable test goes to the fix loop (rule 4m) and
        # is fixed before ACCEPT; one that cannot be fixed, with no rounds left, is declared (3m) and
        # the report says so.
        med = {"id": "D-010", "severity": "MEDIUM", "target_stage": "ANALYZE", "claim": "more draws",
               "done_when": [{"file": "analysis_settings.yaml", "key": "uncertainty.draws", "op": ">=", "value": 40}]}
        d, T, st = new_run("medium_fix", {"critics": {1: {"critique-domain": [med]}},
                                          "fixes": {"D-010": [("analysis_settings.yaml", "uncertainty.draws", 40)]}})
        s = _SpikeMachine(d, T, st, log=lambda *a: None, cross_engine_check=False).run()
        g1 = json.load(open(os.path.join(d, "gate_r1.json")))
        ok(s == "completed" and g1["action"] == "PATCH_OR_RETHINK" and g1["rule_matched"] == "4m"
           and FX.impact_rows(d) and FX.impact_rows(d)[0]["findings"] == ["D-010"],
           f"T42 an open MEDIUM with a test is fixed in the loop before ACCEPT ({s}, {g1['action']} {g1['rule_matched']})")
        d, T, st = new_run("medium_declare", {"critics": {1: {"critique-domain": [dict(med, id="D-011")]}}}, max_rounds=1)
        s = _SpikeMachine(d, T, st, log=lambda *a: None, cross_engine_check=False).run()
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
        fr = os.path.join(td, "t44")
        os.makedirs(os.path.join(fr, "models"))
        shutil.copy(os.path.join(FX_DIR, "claims_ledger.yaml"), os.path.join(fr, "models", "claims_ledger.yaml"))
        yaml.safe_dump({"schema_version": 1, "constraints": {"envelope_usd": 18000000}}, open(os.path.join(fr, AB.SPEC_FILE), "w"))
        open(os.path.join(fr, "manuscript.md"), "w").write(
            "1. Within the 18000000 budget for 2026-2028, D09 gets nothing; the plan averts 999,999 cases.\n")
        k44 = [f for f in report_claim_findings(fr) if f["kind"] == "report_number_typed"]
        ok(k44 and "999,999" in k44[0]["claim"] and "18000000" not in k44[0]["claim"] and "2026" not in k44[0]["claim"],
           f"T44 a typed result number is HIGH; the owner's given numbers, years, ids and list numbers are not ({k44[:1]})")
        open(os.path.join(fr, "manuscript.md"), "w").write("Intro.\n<!-- harness: copied -->\n`headline.point` and [CLAIM:nope].\n")
        k44b = {f["kind"] for f in report_claim_findings(fr)}
        ok({"report_claim_id_raw", "report_claim_unresolved"} <= k44b, f"T44b no marker the writer copies hides an id ({k44b})")
        open(os.path.join(fr, "manuscript.md"), "w").write("Averts [CLAIM:headline.point] cases.\n")
        open(os.path.join(fr, "report.md"), "w").write("stale [CLAIM:headline.point]\n")
        m45 = Machine(fr, StubTasks({}), RunState(question="q", state="FIX_LOOP"), backend=spike, log=lambda *a: None)
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
        open(os.path.join(fr, "manuscript.md"), "w").write("`headline.point`\n")
        os.remove(os.path.join(fr, STATE_FILE))           # T45's machine left its state here
        m51 = Machine(fr, StubTasks({}), RunState(question="q", state="GATE", v2_rules=False), backend=spike,
                      log=lambda *a: None)
        ok(not any(f["kind"].startswith("report_") for f in m51.harness_findings()),
           "T51 golden replays (v2_rules off) do not run the v2 report checks")

        # T52-T55: Codex round 2 on #95 (each failed on 7aa9642).
        for txt in ("Benefit is -987654 cases.", "Benefit is 987K cases.", "Benefit is 987e6 cases."):
            open(os.path.join(fr, "manuscript.md"), "w").write(txt + "\n")
            if not any(f["kind"] == "report_number_typed" for f in report_claim_findings(fr)):
                ok(False, f"T52 signed, magnitude-suffixed and scientific numbers are typed numbers: {txt}")
                break
        else:
            ok(True, "T52 signed, magnitude-suffixed and scientific numbers are typed numbers")
        json.dump({"question": "Choose packages for 10 districts.", "started": "2026-10-31T16:42:57Z"},
                  open(os.path.join(fr, "metadata.json"), "w"))
        open(os.path.join(fr, "manuscript.md"), "w").write("Across 10 districts it reduces cases by 57%.\n")
        k53 = [f for f in report_claim_findings(fr) if f["kind"] == "report_number_typed"]
        ok(k53 and "57%" in k53[0]["claim"] and "10" not in k53[0]["claim"].split("(")[1].split(")")[0].split(", "),
           f"T53 only the question's own numbers are given; a timestamp's digits are not ({k53[:1]})")
        open(os.path.join(fr, "manuscript.md"), "w").write("The plan averts `[CLAIM:headline.point]` cases.\n")
        render_report(fr, spike)
        ok(any(f["kind"] == "report_claim_unrendered" for f in report_claim_findings(fr)),
           "T54 a claim reference left unrendered in the report (inside code) is a HIGH finding")
        # T56: the typed-number rule is a text heuristic: MEDIUM and declarable (process rule 4), and the
        # interval level and any value in the spec, the ledger or the question are not typed numbers.
        open(os.path.join(fr, "manuscript.md"), "w").write(
            "A 95% interval; about 1.3M cases for $17.9M across 10 districts; 3 years.\n")
        k56 = [f for f in report_claim_findings(fr) if f["kind"] == "report_number_typed"]
        ok(k56 and k56[0]["severity"] == "LOW" and "(3)" in k56[0]["claim"],
           f"T56 typed numbers are LOW (advisory); 95%, 1.3M, $17.9M and 10 are exempt ({k56[:1]})")
        # T57: a run whose only open finding is a typed number reaches ACCEPT; the finding is reported
        # in findings.jsonl (advisory), not planned and not declared.
        d57, T57, st57 = new_run("typed_only", {"manuscript": "The plan averts 999 cases. It averts [CLAIM:headline.point] cases.\n"})
        os.makedirs(os.path.join(d57, "models"), exist_ok=True)
        shutil.copy(os.path.join(FX_DIR, "claims_ledger.yaml"), os.path.join(d57, "models", "claims_ledger.yaml"))
        s57 = _SpikeMachine(d57, T57, st57, log=lambda *a: None, cross_engine_check=False).run()
        led57 = FL.load(d57)
        typed57 = [f for f in led57.values() if f.kind == "report_number_typed"]
        ok(s57 == "completed" and typed57 and typed57[0].severity == "LOW" and not typed57[0].blocking
           and not os.path.exists(os.path.join(d57, DECLARED_FILE))
           and json.load(open(os.path.join(d57, "final_gate.json")))["action"] == "ACCEPT",
           f"T57 a run whose only open finding is a typed number reaches ACCEPT; the finding is advisory ({s57})")
        fe = os.path.join(td, "t55")
        os.makedirs(os.path.join(fe, "data"))
        yaml.safe_dump({"engine": {"ref": "toolkit.engines.contract:ToyEngine",
                                   "config": {"results_path": os.path.join(td, "outside_bench")}}},
                       open(os.path.join(fe, AB.SPEC_FILE), "w"))
        os.makedirs(os.path.join(td, "outside_bench"), exist_ok=True)
        ok(any("outside_bench" in x for x in record_cited_inputs(fe)),
           "T55 an engine config path outside the run, without a file extension, is refused")

        # T23: a transition outside the table is refused.
        m = _SpikeMachine(os.path.join(td, "ref"), StubTasks({}), None, log=lambda *a: None)
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
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main(argv=None) -> int:
    import argparse
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--self-test"]:
        return _self_test()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s1 = sub.add_parser("status")
    s1.add_argument("run_dir")
    s2 = sub.add_parser("sign")
    s2.add_argument("run_dir")
    s2.add_argument("--by", choices=["owner"], required=True)
    s2.add_argument("--note", default="")
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
    rec = b.sign(a.run_dir, a.by, a.note) if getattr(b, "sign", None) else sign(a.run_dir, a.by, a.note)
    print(json.dumps(rec, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
