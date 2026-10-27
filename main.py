"""Modeling agent orchestrator -- single lead session with parallel subagents."""

import asyncio
import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

from claude_agent_sdk import (
    query,
    ClaudeAgentOptions,
    AssistantMessage,
    ResultMessage,
    UserMessage,
    SystemMessage,
    TaskStartedMessage,
    TaskProgressMessage,
    TaskNotificationMessage,
)
try:   # Phase 32 γ (Codex review): a background task can end with only an update
    from claude_agent_sdk import TaskUpdatedMessage
except ImportError:  # older SDKs
    TaskUpdatedMessage = ()
from agents import (
    build_agents,
    build_lead_prompt,
    create_hooks,
    cleanup_orphaned_claude_processes,
    LEAD_SYSTEM_PROMPT,
    smoke_settings,
)
from agents import enforcement
from agents.integrity import ArtifactIntegrity

REPO_ROOT = enforcement.REPO_ROOT
VALIDATOR = os.path.join(REPO_ROOT, "scripts", "validate_critique_yaml.py")

# Phase 21 α: the lead's tool set is passed as `tools=` (the base set the
# CLI exposes). `allowed_tools` alone is only a permission allowlist,
# which bypassPermissions makes moot, so before Phase 21 the lead had
# every default tool (including Edit) and used it on gate inputs.
LEAD_TOOLS = ["Agent", "Bash", "Read", "Write", "Glob", "Grep", "Skill"]

# Terminal statuses that count as success (exit 0). Everything else is
# non-zero so cron / CI wrappers can't mistake a non-accepted run for a
# good one. See determine_status() for the full list.
SUCCESS_STATUSES = {"completed", "completed_with_report_restored"}
# Pipeline finished and wrote a report, but the final gate did not ACCEPT.
PARTIAL_STATUSES = {"completed_scope_declared", "completed_gate_not_accepted",
                    "completed_gate_error", "completed_gate_not_run",
                    "completed_no_report", "run_failed", "budget_exhausted",
                    "awaiting_signoff"}   # Phase 33: resumable with --resume once signed


BUDGET_LEDGER = "budget_ledger.json"


def load_budget_ledger(run_path: str) -> dict:
    """The harness-owned spend record (agents may not write it; the
    integrity record watches it). Not metadata.json: the planner may edit
    that file (Codex review of PR #15)."""
    try:
        with open(os.path.join(run_path, BUDGET_LEDGER)) as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("sessions"), list):
            return d
    except (OSError, ValueError):
        pass
    return {"sessions": []}


# Phase 28: a session that dies before starting its conversation (no session
# id: no API call yet) is charged an upper bound from its duration, ~2x the
# highest hourly spend observed (E7 runs averaged ~$25-30/h).
MAX_USD_PER_HOUR = 60.0
RECONCILE_PROMPT = ("The previous session was interrupted by a crash. Reply with the single word OK "
                    "and do nothing else; the harness is reading this conversation's cost.")


async def reconcile_conversation_cost(options, session_id: str, timeout_s: float = 300, query_fn=None):
    """After a crash, the conversation's true cumulative cost: resume it for
    one tool-less turn; the SDK reports cumulative cost for a resumed
    conversation (verified 2026-10-23, crash smokes 2026-10-25). Returns the
    ResultMessage, or None if it cannot be read (API down, timeout)."""
    import dataclasses
    probe = dataclasses.replace(options, resume=session_id, max_turns=1, tools=[], allowed_tools=[],
                                agents={}, hooks=None, max_budget_usd=None)
    qf = query_fn or query

    async def go():
        async for m in qf(prompt=RECONCILE_PROMPT, options=probe):
            if isinstance(m, ResultMessage):
                # The read-back turn itself costs something: an error result or a
                # zero cost means the conversation was not actually resumed.
                ok_ = (not m.is_error and isinstance(m.total_cost_usd, (int, float))
                       and m.total_cost_usd > 0 and (m.session_id in (None, session_id)))
                return m if ok_ else None
        return None
    try:
        return await asyncio.wait_for(go(), timeout_s)
    except Exception:  # noqa: BLE001 — unknown cost: the caller charges the allowance
        return None


_HARNESS_LOCK: dict = {}   # Phase 34 ε: modes of the harness files locked for this run (finalize_run restores them)
TRANSCRIPT_MARGIN = 1.05   # Phase 34 δ: E7 attempt 6, transcript estimate +0.25% vs reported


async def crashed_session_cost(options, session_id: str, attempts: int = 3, delay_s: float = 20.0,
                               query_fn=None, transcript_root: str | None = None):
    """Phase 34 δ: a crashed session's cumulative cost. Read it back by
    resuming the conversation (retried: the 2026-10-27 smoke's single try
    returned nothing, a second scenario's worked), else estimate it from the
    CLI transcripts (+5%). Returns (result-like or None, source)."""
    from types import SimpleNamespace
    for i in range(attempts):
        r = await reconcile_conversation_cost(options, session_id, query_fn=query_fn)
        if r is not None:
            return r, "reconciled: cumulative cost read back after a crash"
        if i + 1 < attempts:
            await asyncio.sleep(delay_s)
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts", "lib"))
    import transcript_cost
    est = transcript_cost.conversation_cost(session_id, transcript_root)
    if est is None:
        return None, "result"
    return (SimpleNamespace(total_cost_usd=round(est * TRANSCRIPT_MARGIN, 4), session_id=session_id),
            f"estimated from the CLI transcripts (+{TRANSCRIPT_MARGIN - 1:.0%}); read-back failed")


def charge_session(ledger: dict, session: int, result, allowance: float | None,
                   session_id: str | None = None, elapsed_s: float | None = None,
                   source: str = "result") -> dict:
    """Record one session's spend.
    - A cost report (the session's own, or after a crash the cumulative cost
      read back by resuming the conversation): charged exactly.
    - Crashed before its conversation started (no session id, so no API
      call): an upper bound from its duration.
    - Otherwise (cost unreadable): the whole remaining allowance, so the cap
      can never be exceeded (Codex reviews, PR #15 and #23).
    E7 attempt 5: a ~$62 crashed session was charged the whole $450 and the
    run stopped; reading the true cost back lets the run continue."""
    cost = getattr(result, "total_cost_usd", None) if result is not None else None
    if isinstance(cost, (int, float)):
        # The SDK reports CUMULATIVE cost for a resumed conversation (same
        # session_id; verified 2026-10-23): charge only the increase.
        sid = getattr(result, "session_id", None)
        # Everything already charged to this conversation, including a crashed
        # session's read-back: each report adds only its increase.
        prev = sum(e.get("total_cost_usd") or 0 for e in ledger.get("sessions", [])
                   if sid and e.get("session_id") == sid)
        entry = {"session": session, "session_id": sid, "reported_usd": round(cost, 4),
                 "total_cost_usd": round(max(cost - prev, 0), 4), "source": source}
    elif session_id is None and isinstance(elapsed_s, (int, float)):
        bound = max(elapsed_s, 0) / 3600 * MAX_USD_PER_HOUR
        charged = bound if allowance is None else min(bound, allowance)
        entry = {"session": session, "session_id": session_id, "total_cost_usd": round(charged, 4),
                 "source": f"upper bound: {elapsed_s:.0f}s at ${MAX_USD_PER_HOUR:.0f}/h "
                           "(crashed before its conversation started)"}
    else:
        entry = {"session": session, "total_cost_usd": round(allowance or 0, 4),
                 "source": "unreconciled: charged the full remaining allowance"}
    ledger.setdefault("sessions", []).append(entry)
    return ledger


def conversation_cost(records: list[dict]) -> float | None:
    """True spend from per-session results: the reported cost is cumulative
    within a conversation (session_id), so take each conversation's maximum
    and sum across conversations. Summing sessions over-counted resumed runs."""
    by_sid: dict = {}
    for i, r in enumerate(records):
        c = r.get("total_cost_usd")
        if isinstance(c, (int, float)):
            k = r.get("session_id") or f"_nosid{i}"
            by_sid[k] = max(by_sid.get(k, 0), c)
    return round(sum(by_sid.values()), 4) if by_sid else None


def write_budget_ledger(run_path: str, ledger: dict, cap: float | None, integrity=None) -> None:
    ledger = {**ledger, "cap_usd": cap,
              "spent_usd": round(sum(e.get("total_cost_usd") or 0 for e in ledger["sessions"]), 4)}
    with open(os.path.join(run_path, BUDGET_LEDGER), "w") as f:
        json.dump(ledger, f, indent=2)
    if integrity is not None:
        integrity.harness_wrote(BUDGET_LEDGER)


def writeup_reserve(cap: float | None) -> float:
    """Phase 29: dollars held back from the modeling sessions so the final
    write-up can always run (8% of the cap, at least $10, at most half)."""
    if cap is None:
        return 0.0
    return round(min(max(0.08 * cap, 10.0), 0.5 * cap), 2)


def remaining_budget(cap: float | None, records: list[dict]) -> float | None:
    """Dollars left under a run-wide cap, summed over every session so far
    (including earlier invocations of a resumed run). None = no cap."""
    if cap is None:
        return None
    spent = sum(r.get("total_cost_usd") or 0 for r in records
                if isinstance(r.get("total_cost_usd"), (int, float)))
    return round(cap - spent, 4)


def slugify(text: str, max_len: int = 40) -> str:
    slug = text.lower().strip()
    slug = re.sub(r'[^\w\s-]', '', slug)
    slug = re.sub(r'[\s_]+', '-', slug)
    return slug[:max_len].rstrip('-')


def create_run_dir(question: str) -> str:
    runs_root = os.path.join(REPO_ROOT, "runs")
    os.makedirs(runs_root, exist_ok=True)
    # Phase 11 Commit η (F5): seconds-granularity timestamp + collision
    # detection. Previously: minute granularity + os.makedirs(...,
    # exist_ok=True) silently reused the dir, then metadata.json was
    # rewritten with mode "w", clobbering the prior run's record.
    # Now: if `{ts}_{slug}` exists, append `-2`, `-3`, ... until a
    # free name is found, and write metadata.json with mode "x"
    # (exclusive create) so any latent race surfaces as
    # FileExistsError rather than silent overwrite.
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    slug = slugify(question)
    base_name = f"{timestamp}_{slug}"
    run_name = base_name
    suffix = 2
    while os.path.exists(os.path.join(runs_root, run_name)):
        run_name = f"{base_name}-{suffix}"
        suffix += 1
    run_path = os.path.join(runs_root, run_name)
    os.makedirs(run_path)  # no exist_ok — we know it's free
    os.makedirs(os.path.join(run_path, "data"), exist_ok=True)
    os.makedirs(os.path.join(run_path, "figures"), exist_ok=True)

    metadata = {
        "question": question,
        "started": datetime.now().isoformat(),
        "run_dir": run_name,
    }
    with open(os.path.join(run_path, "metadata.json"), "x") as f:
        json.dump(metadata, f, indent=2)

    with open(os.path.join(run_path, "progress.md"), "w") as f:
        f.write(f"# Progress\n\n## Question: {question}\n\n## Completed Stages:\n\n## Notes:\n")

    return run_name


def build_resume_context(run_path: str) -> str:
    """Build resume context from pipeline_state.yaml or file detection."""
    # Prefer structured state file
    state_path = os.path.join(run_path, "pipeline_state.yaml")
    if os.path.exists(state_path):
        try:
            import yaml
            with open(state_path) as f:
                state = yaml.safe_load(f)
            stage = state.get("current_stage", "PLAN")
            round_num = state.get("current_round", 1)
            completed = list(state.get("completed", {}).keys())
            return (
                f"RESUME: Pipeline is at stage {stage}, round {round_num}. "
                f"Completed stages: {', '.join(completed)}. "
                f"Read {run_path}/progress.md and pipeline_state.yaml for full context. "
                f"Continue from stage {stage}."
            )
        except Exception:
            pass  # Fall through to file detection

    # Fallback: detect existing files
    existing = []
    for fname in [
        "plan.md", "threads.yaml", "data_quality.md", "data_provenance.md",
        "eda.py", "figure_rationale.md", "modeling_strategy.md",
        "model_comparison.md", "model_run.py", "results.md",
        "critique_methods.md", "critique_domain.md", "critique_presentation.md",
        "report.md",
    ]:
        if os.path.exists(os.path.join(run_path, fname)):
            existing.append(fname)
    if not existing:
        return ""
    return (
        f"RESUME CONTEXT: The following files already exist from a prior run: "
        f"{', '.join(existing)}. Read them and skip completed stages. "
        f"Continue from where they leave off."
    )


TERMINAL_TASK_STATUSES = ("completed", "failed", "killed", "stopped", "cancelled", "canceled", "error")


def log_message(message, trace_file):
    """Log a message from the lead session to console and trace."""
    if isinstance(message, TaskStartedMessage):
        print(f"  [task started] {message.description}", flush=True)
        trace_file.write(json.dumps({
            "ts": datetime.now().isoformat(),
            "type": "task_started",
            "task_id": message.task_id,
            "description": message.description,
        }) + "\n")
    elif TaskUpdatedMessage and isinstance(message, TaskUpdatedMessage):
        if str(message.status or "").lower() in TERMINAL_TASK_STATUSES:
            trace_file.write(json.dumps({"ts": datetime.now().isoformat(), "type": "task_updated",
                                         "task_id": message.task_id, "status": message.status}) + "\n")
    elif isinstance(message, TaskProgressMessage):
        tool = message.last_tool_name or ""
        if tool:
            print(f"  [progress] {message.description} | {tool}", flush=True)
    elif isinstance(message, TaskNotificationMessage):
        status_marker = "+" if message.status == "completed" else "!"
        print(f"  [{status_marker} {message.status}] {message.summary[:120]}", flush=True)
        usage = None
        if message.usage:
            u = message.usage
            if isinstance(u, dict):
                usage = u
            else:
                usage = {
                    "total_tokens": u.total_tokens,
                    "tool_uses": u.tool_uses,
                    "duration_ms": u.duration_ms,
                }
        trace_file.write(json.dumps({
            "ts": datetime.now().isoformat(),
            "type": "task_notification",
            "task_id": message.task_id,
            "status": message.status,
            "summary": message.summary,
            "usage": usage,
        }) + "\n")
    elif isinstance(message, AssistantMessage):
        for block in message.content:
            if hasattr(block, "text") and block.text:
                # Print lead's own reasoning (truncated for readability)
                text = block.text.strip()
                if text:
                    for line in text.split("\n")[:5]:
                        print(f"[lead] {line[:150]}", flush=True)
                    if text.count("\n") > 5:
                        print(f"[lead] ... ({text.count(chr(10))} lines total)", flush=True)
    elif isinstance(message, ResultMessage):
        print(f"\n[result] {message.subtype} | {message.duration_ms/1000:.0f}s"
              f" | error={message.is_error}", flush=True)
        trace_file.write(json.dumps({
            "ts": datetime.now().isoformat(),
            "type": "result",
            **result_summary(message),
        }) + "\n")
    trace_file.flush()


def result_summary(message) -> dict:
    """JSON-safe record of a ResultMessage (cost, usage, models, errors)."""
    return {
        "subtype": getattr(message, "subtype", None),
        "is_error": bool(getattr(message, "is_error", False)),
        "num_turns": getattr(message, "num_turns", None),
        "duration_ms": getattr(message, "duration_ms", None),
        "total_cost_usd": getattr(message, "total_cost_usd", None),
        "usage": getattr(message, "usage", None),
        "model_usage": getattr(message, "model_usage", None),
        "stop_reason": getattr(message, "stop_reason", None),
        "errors": getattr(message, "errors", None),
        "session_id": getattr(message, "session_id", None),
    }


def result_is_success(message) -> bool:
    """Phase 21 α: a ResultMessage ends the stream for success AND for
    error_max_turns / error_during_execution. Only subtype "success"
    without is_error counts; everything else goes down the retry path."""
    return (message is not None
            and not getattr(message, "is_error", False)
            and getattr(message, "subtype", None) == "success")


def is_policy_block(error_str: str) -> bool:
    # Phase 21 α: the bare substring "violate" matched ordinary errors
    # (e.g. "constraint violated") and ended runs without retry.
    low = error_str.lower()
    return "usage policy" in low or "acceptable use policy" in low


_TERMINAL_STAGES = {"DONE", "COMPLETE", "COMPLETED", "FINISHED", "RUN_FAILED",
                    "FAILED", "DECLARE_SCOPE", "ACCEPTED"}


def pipeline_unfinished(run_path: str) -> str | None:
    """Phase 21 θ: return a description of where the pipeline stopped if it
    is clearly unfinished (no report.md and no terminal stage), else None.

    A clean ResultMessage only means the lead ended its turn, not that the
    pipeline finished; 2026-10-20_213310 ended "success" in round 1 while
    waiting on background jobs.
    """
    if os.path.exists(os.path.join(run_path, "report.md")):
        return None
    stage, rnd = None, None
    try:
        import yaml
        with open(os.path.join(run_path, "pipeline_state.yaml")) as f:
            ps = yaml.safe_load(f) or {}
        stage = str(ps.get("current_stage") or "").strip().upper() or None
        rnd = ps.get("current_round")
    except (OSError, ValueError, AttributeError, ImportError):
        pass
    except Exception:  # yaml.YAMLError without importing yaml at module scope
        pass
    if stage in _TERMINAL_STAGES:
        return None
    return f"stage {stage or 'unknown'}, round {rnd if rnd is not None else 'unknown'}"


CONTINUE_PROMPT = (
    "CONTINUE. Your previous turn ended while the pipeline was unfinished "
    "({where}); the session closed and nothing ran in the meantime. Do not "
    "summarize or wait for the user: there is no user in this loop. Check the "
    "state of any jobs you started, then continue the pipeline from where you "
    "are. Background execution (run_in_background) is disabled: run long jobs "
    "with nohup inside a foreground command and poll with bounded loops. Do "
    "not end your turn until the pipeline reaches a terminal state (report "
    "written and final gate run, or RUN_FAILED).")


SIGNOFF_PROMPT = (
    "CONTINUE. The owner has signed the decision brief.\n{text}\n"
    "Continue the pipeline from STAGE 2 (DATA): tell the planner to carry the owner's answers into plan.md "
    "if they change it, and pass them to every specialist. Do not end your turn until the pipeline reaches a "
    "terminal state.")


async def wait_for_signoff(run_path: str, run_dir_rel: str, hours: float, poll_s: float = 30.0,
                           integrity=None) -> bool:
    """Phase 33 α: the run waits for the owner to sign the decision brief.
    True once signed; False when the wait times out (status awaiting_signoff,
    resumable with --resume after signing)."""
    import decision_brief
    md = os.path.join(run_path, "decision_brief.md")
    try:
        open(md, "w").write(decision_brief.render_md(run_path))
    except OSError:
        pass
    print(f"\n{'='*60}\nWAITING FOR THE OWNER TO SIGN THE DECISION BRIEF\n"
          f"  Read:  {run_dir_rel}/decision_brief.md\n"
          f"  Sign:  python3 scripts/decision_brief.py sign {run_dir_rel} --answers <answers.yaml> "
          f"[--note \"...\"]   (or --accept-proposed)\n"
          f"  Waiting up to {hours:g} h.\n{'='*60}", flush=True)
    with open(os.path.join(run_path, "trace.jsonl"), "a") as tf:
        tf.write(json.dumps({"ts": datetime.now().isoformat(), "type": "awaiting_signoff"}) + "\n")
    deadline = time.time() + hours * 3600
    while time.time() < deadline:
        if decision_brief.status(run_path) == "signed":
            if integrity is not None:   # the owner signed outside any tool call
                integrity.harness_wrote(decision_brief.SIGNOFF)
                for f in decision_brief.FROZEN_AT_SIGNOFF:   # and the signed copies it kept
                    integrity.harness_wrote(f"{decision_brief.SIGNED_DIR}/{f}")
            with open(os.path.join(run_path, "trace.jsonl"), "a") as tf:
                tf.write(json.dumps({"ts": datetime.now().isoformat(), "type": "signed_off"}) + "\n")
            print("Decision brief signed; resuming the lead.", flush=True)
            return True
        await asyncio.sleep(poll_s)
    return decision_brief.status(run_path) == "signed"


def detect_current_round(run_path: str) -> int | None:
    """Highest `round:` across critique_*.yaml (the gate's own input),
    falling back to pipeline_state.yaml."""
    import yaml
    rounds = []
    for path in glob.glob(os.path.join(run_path, "critique_*.yaml")):
        try:
            with open(path) as f:
                doc = yaml.safe_load(f)
            r = doc.get("round") if isinstance(doc, dict) else None
            if isinstance(r, int):
                rounds.append(r)
        except Exception:
            continue
    if rounds:
        return max(rounds)
    try:
        with open(os.path.join(run_path, "pipeline_state.yaml")) as f:
            state = yaml.safe_load(f) or {}
        r = state.get("current_round")
        return r if isinstance(r, int) else None
    except Exception:
        return None


def run_final_gate(run_path: str, max_rounds: int,
                   validator: str = VALIDATOR, timeout_s: int = 900) -> dict | None:
    """Phase 21 α: re-run the STAGE 7 validator after the session ends.

    The lead was supposed to re-run it after WRITE, but nothing enforced
    that, so post-WRITE HIGHs (coherence, sufficiency) never reached a
    gate. Returns None when there are no critique YAMLs to gate on;
    otherwise a dict with action / exit_code / counts. Writes
    final_gate.json and final_gate_stderr.txt into the run dir.
    """
    current_round = detect_current_round(run_path)
    if current_round is None:
        return None
    cmd = [sys.executable, validator, run_path,
           "--max-rounds", str(max_rounds), "--current-round", str(current_round),
           "--spec-compliance", "--parameter-registry", "--rigor-artifacts",
           "--json"]
    # Phase 21 β: --post-write makes coherence / sufficiency / writer-QA
    # HIGHs blocking. Only meaningful once a report exists; without one
    # determine_status() reports completed_no_report anyway.
    if os.path.exists(os.path.join(run_path, "report.md")):
        cmd.append("--post-write")
    gate = {"current_round": current_round, "max_rounds": max_rounds,
            "ran_at": datetime.now().isoformat(), "action": None}
    try:
        proc = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True,
                              text=True, timeout=timeout_s)
    except (OSError, subprocess.TimeoutExpired) as e:
        gate.update(exit_code=None, error=f"{type(e).__name__}: {e}")
        return gate
    gate["exit_code"] = proc.returncode
    try:
        with open(os.path.join(run_path, "final_gate_stderr.txt"), "w") as f:
            f.write(proc.stderr)
    except OSError:
        pass
    decision = None
    out = proc.stdout.strip()
    if out:
        try:
            decision = json.loads(out[out.index("{"):])
        except (ValueError, json.JSONDecodeError):
            decision = None
    if isinstance(decision, dict):
        gate["action"] = decision.get("action")
        if "schema_errors" in decision:
            gate["schema_errors"] = decision["schema_errors"]
        unresolved = decision.get("unresolved_high")
        if isinstance(unresolved, list):
            gate["n_unresolved_high"] = len(unresolved)
        try:
            with open(os.path.join(run_path, "final_gate.json"), "w") as f:
                json.dump(decision, f, indent=2)
        except OSError:
            pass
    else:
        gate["error"] = (proc.stderr.strip().splitlines() or ["no JSON on stdout"])[-1][:500]
    return gate


def determine_status(session_outcome: str, gate: dict | None, has_report: bool,
                     report_restored: bool, harness_diff: dict | None,
                     integrity_violations: int = 0) -> str:
    """Single source of truth for metadata.json["status"].

    Phase 11 Commit η (F1) introduced status discrimination; Phase 21 α
    stops equating "the stream ended" with "completed". Order matters:
    tampering beats everything, then session-level failures, then the
    final gate's verdict on what was delivered.
    """
    if harness_diff and enforcement.harness_changed(harness_diff):
        return "harness_tampered"
    if integrity_violations:
        # Phase 23 α: decided from the harness's in-memory record, so a
        # report file an agent made unwritable cannot hide it.
        return "artifact_tampered"
    if session_outcome != "session_completed":
        return session_outcome
    action = (gate or {}).get("action")
    if action == "RUN_FAILED":
        return "run_failed"
    if not has_report:
        return "completed_no_report"
    if gate is None:
        return "completed_gate_not_run"
    if action == "ACCEPT":
        return "completed_with_report_restored" if report_restored else "completed"
    if action == "DECLARE_SCOPE":
        return "completed_scope_declared"
    if action is None or action == "VALIDATOR_ERROR":
        return "completed_gate_error"
    return "completed_gate_not_accepted"


def exit_code_for(status: str) -> int:
    if status in SUCCESS_STATUSES:
        return 0
    if status == "interrupted":
        return 130
    if status in PARTIAL_STATUSES:
        return 2
    return 1


def restore_report_if_missing(run_path: str) -> bool:
    """Phase 12 δ safety net, Phase 21 α rewiring: the PostToolUse hook
    snapshots report.md every time it changes, so if it is missing at
    the end but a snapshot exists, restore it. (The Phase 12 version
    only snapshotted at run end, after the disappearance it guarded
    against, and keyed on writer_qa CLEAN, which writer_qa also
    reported for a missing report.)"""
    report_path = os.path.join(run_path, "report.md")
    snapshot_path = os.path.join(run_path, "report.snapshot.md")
    if os.path.exists(report_path):
        return False
    if os.path.exists(os.path.join(run_path, "manuscript.md")):
        # Phase 25: the writer's draft exists but the render step never ran.
        # Build report.md from it (render_claims falls back to the draft
        # itself when references cannot be resolved).
        try:
            r = subprocess.run([sys.executable, os.path.join(REPO_ROOT, "scripts", "render_claims.py"), run_path],
                               capture_output=True, text=True, timeout=600)
        except (subprocess.TimeoutExpired, OSError) as e:
            r = subprocess.CompletedProcess([], 1, "", f"{type(e).__name__}: {e}")
        if os.path.exists(report_path):
            print("[WARNING] report.md was missing at run end; rendered from manuscript.md.",
                  file=sys.stderr, flush=True)
            return True
        # Rendering failed (e.g. no claims ledger): the draft itself is the
        # report; writer QA and the gate then judge it as written.
        try:
            shutil.copyfile(os.path.join(run_path, "manuscript.md"), report_path)
            print(f"[WARNING] report.md was missing at run end; rendering failed "
                  f"({(r.stdout + r.stderr).strip()[-300:]}); copied manuscript.md.",
                  file=sys.stderr, flush=True)
            return True
        except OSError as e:
            print(f"[ERROR] report.md missing; copying manuscript.md failed: {e}",
                  file=sys.stderr, flush=True)
    if not os.path.exists(snapshot_path):
        return False
    try:
        shutil.copyfile(snapshot_path, report_path)
    except OSError as e:
        print(f"[ERROR] report.md missing AND restore failed: {e}",
              file=sys.stderr, flush=True)
        return False
    print(f"[WARNING] report.md was missing at run end; restored from "
          f"{snapshot_path}. Investigate trace.jsonl for the deletion path.",
          file=sys.stderr, flush=True)
    return True


VERDICT_BANNER_MARK = "<!-- harness: final gate verdict -->"


def add_verdict_banner(run_path: str, gate: dict | None) -> bool:
    """If the final gate did not ACCEPT, make sure report.md opens with the
    verdict (a report written before the final rejection would otherwise
    read as accepted). Harness-written and labelled as such; idempotent."""
    rp = os.path.join(run_path, "report.md")
    action = (gate or {}).get("action")
    if not os.path.exists(rp) or action == "ACCEPT":
        return False
    body = open(rp).read()
    if VERDICT_BANNER_MARK in body:
        return False
    n = gate.get("unresolved_high")
    n = len(n) if isinstance(n, list) else gate.get("n_unresolved_high")
    banner = (f"{VERDICT_BANNER_MARK}\n> **Status: not accepted by the final gate** "
              f"(verdict: {action or gate.get('error') or 'not run'}"
              + (f"; {n} unresolved blocking finding(s)" if n is not None else "")
              + "; see `final_gate.json`). Added by the harness after the report was written: "
              "read the findings below as what was built, not as a recommendation.\n\n")
    open(rp, "w").write(banner + body)
    return True


def write_harness_baseline(run_path: str) -> tuple[dict, dict | None]:
    """Hash the harness into <run>/harness_manifest.json. On resume, also
    return the diff against the previous invocation's baseline (operator
    edits between invocations are legitimate but recorded)."""
    current = enforcement.hash_harness()
    path = os.path.join(run_path, "harness_manifest.json")
    prior_diff = None
    if os.path.exists(path):
        try:
            with open(path) as f:
                prior = json.load(f).get("files", {})
            prior_diff = enforcement.diff_harness(prior, current)
        except (OSError, ValueError, AttributeError):
            prior_diff = None
    with open(path, "w") as f:
        json.dump({"hashed_at": datetime.now().isoformat(), "files": current}, f, indent=2)
    return current, prior_diff


async def run(question: str, max_rounds: int, max_sessions: int,
              run_dir: str | None = None, run_path: str | None = None,
              allow_cloud: bool = False, cross_model: bool = False,
              methods_critic: bool = False, max_budget_usd: float | None = None,
              signoff: str = "owner", signoff_hours: float = 24.0) -> str:
    """Run the modeling pipeline as a single lead agent session.

    Returns the terminal status written to metadata.json.
    """

    # Create or reuse run directory
    if run_dir is None:
        run_dir = create_run_dir(question)
        run_path = os.path.join(REPO_ROOT, "runs", run_dir)
    run_dir_rel = f"runs/{run_dir}"

    print(f"Run: {run_dir_rel}/", flush=True)
    print(f"Question: {question}", flush=True)
    print(f"Max rounds: {max_rounds} | Max sessions: {max_sessions}"
          f" | Cloud: {'on' if allow_cloud else 'off'}"
          f" | Cross-model: {'on' if cross_model else 'off'}"
          f" | Methods critic: {'on' if methods_critic else 'off'}", flush=True)

    run_start = datetime.now()
    # Phase 29: live status, resource samples and storage checkpoints.
    from agents import observability
    live_state = observability.LiveState()
    heartbeat = observability.Heartbeat(run_path, live_state, run_start,
                                        uploader=observability.storage_uploader(run_path))
    heartbeat.start()
    trace_path = os.path.join(run_path, "trace.jsonl")
    metadata_path = os.path.join(run_path, "metadata.json")
    with open(metadata_path) as f:
        prior_meta = json.load(f)
    # A resumed run keeps its critic set: the validator's requirements follow it.
    methods_critic = methods_critic or bool(prior_meta.get("methods_critic"))

    # Build the agent registry and lead prompt
    agents = build_agents(question=question, run_dir=run_dir_rel)
    resume_context = build_resume_context(run_path)
    lead_system = LEAD_SYSTEM_PROMPT.replace("{run_dir}", run_dir_rel).replace(
        "{max_rounds}", str(max_rounds)
    )
    lead_prompt = build_lead_prompt(question, run_dir_rel, max_rounds, resume_context,
                                    cross_model=cross_model, methods_critic=methods_critic)
    prior_meta["cross_model"] = cross_model
    prior_meta["methods_critic"] = methods_critic  # validator requires its YAML iff True
    prior_meta["models"] = {"deep": enforcement.MODEL_DEEP, "fast": enforcement.MODEL_FAST}
    with open(metadata_path, "w") as f:
        json.dump(prior_meta, f, indent=2)
    env_overrides = enforcement.filtered_env(dict(os.environ), allow_cloud)
    env_overrides["MA_SIGNOFF_MODE"] = signoff   # Phase 33: decision_brief.py sign checks it
    # The harness's own gate runs (run_final_gate) read the mode too: with
    # --signoff none an unsigned brief is not a finding (Codex review).
    os.environ["MA_SIGNOFF_MODE"] = signoff
    import decision_brief
    if resume_context and decision_brief.is_signed(run_path):
        lead_prompt += "\n" + decision_brief.signoff_text(run_path) + "\n"

    harness_before, harness_prior_diff = write_harness_baseline(run_path)
    import atexit
    _HARNESS_LOCK.update(enforcement.lock_harness())   # read-only on disk for the run; restored at exit
    atexit.register(enforcement.unlock_harness, _HARNESS_LOCK)
    # Phase 23 α: hash protected run artifacts; every change must be explained
    # by an allowed in-flight tool call (see agents/integrity.py).
    integrity = ArtifactIntegrity(run_path)
    integrity.start_watch()     # Phase 36: producers credited by what runs, not by what was typed
    budget = load_budget_ledger(run_path)
    if not budget["sessions"] and prior_meta.get("session_results"):
        # Run started before the ledger existed: carry its recorded spend over
        # rather than granting a fresh allowance (Codex review, PR #15).
        # One entry per conversation, carrying its cumulative cost as
        # reported_usd so a resumed session is charged only the increase.
        per_sid: dict = {}
        for i, r in enumerate(prior_meta["session_results"]):
            c = r.get("total_cost_usd")
            if isinstance(c, (int, float)):
                k = r.get("session_id") or f"_nosid{i}"
                per_sid[k] = max(per_sid.get(k, 0), c)
        for k, c in per_sid.items():
            budget["sessions"].append({"session": 0, "session_id": None if k.startswith("_nosid") else k,
                                       "reported_usd": round(c, 4), "total_cost_usd": round(c, 4),
                                       "source": "migrated from metadata.json session_results"})

    # Phase 21 α: on --resume, continue the saved conversation instead of
    # starting a fresh one (session_id used to be captured only from a
    # ResultMessage, which never arrives for a crashed session).
    session_id = prior_meta.get("session_id")

    # Phase 11 Commit η (F1): discriminate terminal states. Default to
    # "unknown_error" — the loop only exits via explicit branches; if it
    # ever falls through, this default flags the bug instead of silently
    # claiming success.
    session_outcome = "unknown_error"
    session_num = 0
    session_records: list[dict] = []
    interrupted = False
    paused = False          # Phase 33: waiting for the owner's sign-off timed out
    signoff_waits = 0       # a sign-off wait does not use up a session

    # Phase 31: the lead sees the dollar cap. Spend = the ledger at process
    # start (earlier invocations) + this process's live estimate.
    _spent_before = (max_budget_usd - remaining_budget(max_budget_usd, budget["sessions"])
                     if max_budget_usd else 0.0)

    def _budget_status():
        if not max_budget_usd:
            return None
        return {"spent": _spent_before + float(live_state.est_usd or 0), "cap": max_budget_usd,
                "reserve": writeup_reserve(max_budget_usd)}

    try:
        for session_num in range(1, max_sessions + 2):
            if session_num - signoff_waits > max_sessions:
                session_outcome = "max_sessions_reached"
                break
            live_state.session = session_num
            print(f"\n{'#'*60}", flush=True)
            print(f"SESSION {session_num}/{max_sessions}", flush=True)
            print(f"{'#'*60}", flush=True)

            trace_file = open(trace_path, "a")
            trace_file.write(json.dumps({
                "ts": datetime.now().isoformat(),
                "type": "session_start",
                "session": session_num,
                "resume_session_id": session_id,
            }) + "\n")
            # Create hooks per session so the hook closure captures the current
            # (freshly opened) trace_file. Previously hooks captured a file opened
            # once before the loop; after a session crash that fd was closed and
            # subsequent sessions' tool_use / subagent_start/stop events silently
            # failed to write.
            hooks = create_hooks(run_path, trace_file, run_start,
                                 registered_agents=set(agents.keys()),
                                 max_turns={n: d.maxTurns for n, d in agents.items()},
                                 integrity=integrity, max_rounds=max_rounds,
                                 budget_status=_budget_status,
                                 signoff=signoff if signoff in ("owner", "lead") else None)

            # Run-wide dollar cap across sessions (and earlier invocations),
            # from the harness-owned ledger.
            left = remaining_budget(max_budget_usd, budget["sessions"])
            if left is not None:
                left = round(left - writeup_reserve(max_budget_usd), 4)   # kept for the final write-up
            if left is not None and left <= 0:
                print(f"Budget cap ${max_budget_usd:.0f} reached (less the write-up reserve); "
                      f"stopping modeling.", flush=True)
                session_outcome = "budget_exhausted"
                break
            options = ClaudeAgentOptions(
                system_prompt=lead_system,
                agents=agents,
                model=(smoke_settings() or {}).get("model", enforcement.MODEL_DEEP),
                max_buffer_size=enforcement.MAX_BUFFER_SIZE, settings=enforcement.SESSION_SETTINGS,
                tools=LEAD_TOOLS,
                allowed_tools=LEAD_TOOLS,
                permission_mode="bypassPermissions",
                setting_sources=["project"],
                hooks=hooks,
                cwd=REPO_ROOT,
                env=env_overrides,
            )
            if left is not None:
                options.max_budget_usd = left
            if session_id:
                options.resume = session_id

            result = None
            error_str = None
            session_started = datetime.now().astimezone()
            try:
                cleanup_orphaned_claude_processes()

                async for message in query(prompt=lead_prompt, options=options):
                    log_message(message, trace_file)
                    live_state.on_message(message)
                    if (isinstance(message, SystemMessage)
                            and message.subtype == "init"):
                        session_id = (message.data or {}).get("session_id") or session_id
                    elif isinstance(message, ResultMessage):
                        result = message
                        session_id = getattr(message, "session_id", None) or session_id
            except (KeyboardInterrupt, asyncio.CancelledError):
                # Phase 21 α: under asyncio.run, Ctrl-C arrives here as
                # CancelledError (a BaseException), so the old
                # `except KeyboardInterrupt` never fired and final
                # metadata was never written.
                print("\nInterrupted.", flush=True)
                interrupted = True
                session_outcome = "interrupted"
                break
            except Exception as e:
                error_str = str(e)
            finally:
                if result is not None:
                    session_records.append({"session": session_num, **result_summary(result)})
                charged_from, source = result, "result"
                if result is None and session_id and not interrupted:
                    charged_from, source = await crashed_session_cost(options, session_id)
                    print(f"[budget] session {session_num} ended without a cost: "
                          f"{'$%.4f cumulative, %s' % (charged_from.total_cost_usd, source) if charged_from is not None else 'no read-back and no transcript (charging the allowance)'}",
                          flush=True)
                charge_session(budget, session_num, charged_from, left, session_id=session_id,
                               elapsed_s=(datetime.now().astimezone() - session_started).total_seconds(),
                               source=source)
                write_budget_ledger(run_path, budget, max_budget_usd, integrity)
                try:
                    trace_file.close()
                except OSError:
                    pass

            if result is not None and "budget" in str(getattr(result, "subtype", "")):
                print(f"Budget cap ${max_budget_usd} reached during session {session_num}.", flush=True)
                session_outcome = "budget_exhausted"
                break
            if error_str is None and result_is_success(result):
                if signoff == "owner" and decision_brief.status(run_path) == "awaiting":
                    # Phase 33 α: the lead stopped for the owner's sign-off.
                    if not await wait_for_signoff(run_path, run_dir_rel, signoff_hours, integrity=integrity):
                        print("Sign-off wait timed out; resume with --resume after signing.", flush=True)
                        session_outcome, paused = "awaiting_signoff", True
                        break
                    signoff_waits += 1
                    lead_prompt = SIGNOFF_PROMPT.format(text=decision_brief.signoff_text(run_path))
                    continue
                where = pipeline_unfinished(run_path)
                if where and session_num - signoff_waits < max_sessions:
                    # Phase 21 θ: the lead stopped early; continue the same
                    # conversation (options.resume = session_id) instead of
                    # finalizing a half-finished run.
                    print(f"\nLead ended its turn with the pipeline unfinished "
                          f"({where}); continuing (session {session_num + 1}).",
                          flush=True)
                    with open(trace_path, "a") as tf:
                        tf.write(json.dumps({"ts": datetime.now().isoformat(),
                                             "type": "session_continue",
                                             "session": session_num,
                                             "where": where}) + "\n")
                    lead_prompt = CONTINUE_PROMPT.format(where=where)
                    continue
                session_outcome = ("session_completed" if not where
                                   else "max_sessions_reached")
                break  # lead session finished; the final gate decides the status

            if error_str is None:
                if result is None:
                    error_str = "stream ended without a ResultMessage"
                else:
                    error_str = (f"result {result.subtype} (is_error={result.is_error})"
                                 f"{': ' + '; '.join(result.errors) if result.errors else ''}")
            print(f"\nSession {session_num} error: {error_str}", flush=True)
            with open(trace_path, "a") as tf:
                tf.write(json.dumps({
                    "ts": datetime.now().isoformat(),
                    "type": "session_error",
                    "session": session_num,
                    "error": error_str,
                }) + "\n")

            # Don't retry policy blocks
            if is_policy_block(error_str):
                print("Policy block detected. Try rephrasing the question.", flush=True)
                session_outcome = "policy_blocked"
                break

            if session_num - signoff_waits >= max_sessions:
                print("Max sessions reached.", flush=True)
                session_outcome = "max_sessions_reached"
                break

            # Exponential backoff
            backoff = min(30 * (2 ** (session_num - 1)), 300)
            if "529" in error_str or "overload" in error_str.lower():
                backoff = min(60 * (2 ** (session_num - 1)), 600)
                print(f"API overloaded. Waiting {backoff}s before retry...", flush=True)
            else:
                print(f"Waiting {backoff}s before retry...", flush=True)

            cleanup_orphaned_claude_processes()
            try:
                await asyncio.sleep(backoff)  # Phase 21 α: was a blocking time.sleep
            except asyncio.CancelledError:
                print("\nInterrupted during backoff.", flush=True)
                interrupted = True
                session_outcome = "interrupted"
                break

            # On retry, rebuild resume context from whatever files exist
            resume_context = build_resume_context(run_path)
            lead_prompt = build_lead_prompt(question, run_dir_rel, max_rounds, resume_context,
                                            cross_model=cross_model,
                                            methods_critic=methods_critic)
            print(f"Restarting pipeline (session {session_num + 1})...", flush=True)
        # Phase 29: every run ends with a report, so failures can be studied.
        if not interrupted and not paused and not os.path.exists(os.path.join(run_path, "report.md")) \
                and not os.path.exists(os.path.join(run_path, "manuscript.md")):
            live_state.session = session_num + 1
            await final_writeup(run_path, run_dir_rel, question, max_rounds, budget,
                                max_budget_usd, session_num + 1, session_outcome, integrity,
                                live_state=live_state)
    finally:
        heartbeat.stop(timeout_s=180)   # last status + checkpoint; never blocks finalize longer
        status = finalize_run(
            run_path, run_dir_rel, run_start, max_rounds, session_outcome,
            session_num, session_id, session_records, harness_before,
            harness_prior_diff, run_gate=not interrupted and not paused,
            original_meta=prior_meta, integrity=integrity)
    return status


# methods_critic / cross_model decide which critiques the validator requires
# (Codex review of PR #15: flipping methods_critic dropped a requirement).
_PROTECTED_META = ("question", "started", "run_dir", "methods_critic", "cross_model")


def _restore_protected_meta(metadata_path: str, original_meta: dict) -> bool:
    """The planner may add decision_year to metadata.json; it may not change
    the question spec-compliance is checked against. Put protected fields
    back (recording what was there) and rewrite the file."""
    try:
        with open(metadata_path) as f:
            meta = json.load(f)
        if not isinstance(meta, dict):
            raise ValueError("metadata.json is not an object")
    except (OSError, ValueError):
        # Missing or corrupted: rebuild it from the originals rather than
        # let finalization write a metadata.json without the question.
        meta = {"restored_fields": {"_metadata_unreadable": True}}
    if not isinstance(meta.get("restored_fields"), dict):
        meta["restored_fields"] = {}      # null / wrong type must not crash finalization
    changed = not os.path.exists(metadata_path) or "_metadata_unreadable" in meta["restored_fields"]
    for key in _PROTECTED_META:
        if key in original_meta and meta.get(key) != original_meta[key]:
            meta["restored_fields"][key] = meta.get(key)
            meta[key] = original_meta[key]
            changed = True
    if changed:
        with open(metadata_path, "w") as f:
            json.dump(meta, f, indent=2)
    return changed


def finalize_run(run_path, run_dir_rel, run_start, max_rounds, session_outcome,
                 session_num, session_id, session_records, harness_before,
                 harness_prior_diff, run_gate: bool = True,
                 original_meta: dict | None = None, integrity=None) -> str:
    """Restore report if needed, run the final gate, check the harness
    hash, and write metadata.json. Runs in a `finally`, so it executes on
    Ctrl-C and on unexpected exceptions too."""
    original_meta = original_meta or {}
    # Last integrity scan FIRST: pending tool changes are judged against the
    # previous baseline before the harness writes anything (Copilot, PR #15).
    if integrity is not None:
        integrity.final_check()
        integrity.stop_watch()
    report_restored = restore_report_if_missing(run_path)
    if report_restored and integrity is not None:
        for rel in ("report.md", "report.unrendered.md", "models/claims_ledger.yaml", "claims_ledger.yaml"):
            integrity.harness_wrote(rel)
    has_report = os.path.exists(os.path.join(run_path, "report.md"))
    # Restore the protected fields BEFORE the gate: the validator reads the
    # question from metadata.json (Codex review of PR #14: a planner edit to
    # the question would otherwise be gated, then silently undone).
    if _restore_protected_meta(os.path.join(run_path, "metadata.json"), original_meta) \
            and integrity is not None:
        integrity.harness_wrote("metadata.json")
    if integrity is not None and integrity.open_events:
        print(f"[INTEGRITY] {len(integrity.open_events)} unresolved change(s) to protected "
              f"run artifacts; see {os.path.join(run_dir_rel, 'artifact_integrity.json')}",
              flush=True)

    gate = None
    # A budget stop is still gated: the report shows what the money bought.
    if run_gate:          # Phase 29: also after max_sessions / crashes: the verdict is evidence
        print("\nRunning final STAGE 7 gate...", flush=True)
        gate = run_final_gate(run_path, max_rounds)
        if gate is None:
            # No critique round was reached: record that explicitly (Codex, Phase 29).
            gate = {"action": None, "error": "no critique round was reached; nothing to gate",
                    "ran_at": datetime.now().isoformat()}
            json.dump(gate, open(os.path.join(run_path, "final_gate.json"), "w"), indent=2)
        add_verdict_banner(run_path, gate)

    harness_diff = enforcement.diff_harness(harness_before, enforcement.hash_harness())
    enforcement.unlock_harness(_HARNESS_LOCK)
    status = determine_status(session_outcome, gate, has_report,
                              report_restored, harness_diff,
                              integrity_violations=len(integrity.open_events) if integrity else 0)

    elapsed = (datetime.now() - run_start).total_seconds()
    metadata_path = os.path.join(run_path, "metadata.json")
    try:
        with open(metadata_path) as f:
            meta = json.load(f)
    except (OSError, ValueError):
        meta = {}
    # Phase 11 Commit η (F1): `completed` is the timestamp the run loop
    # finished (kept for backward compat with existing readers).
    # `status` is the authoritative discriminator; see determine_status()
    # for the values. Automation should consult `status` (or the process
    # exit code), not the presence of `completed`.
    meta["completed"] = datetime.now().isoformat()
    meta["status"] = status
    meta["session_outcome"] = session_outcome
    meta["elapsed_s"] = elapsed
    meta["sessions"] = session_num
    meta["has_report"] = has_report
    meta["report_restored"] = report_restored
    meta["final_gate"] = gate
    if integrity is not None:
        meta["artifact_integrity_violations"] = len(integrity.open_events)
        meta["artifact_integrity_resolved"] = len(integrity.events) - len(integrity.open_events)
    if session_records:
        meta.setdefault("session_results", []).extend(session_records)
    meta["total_cost_usd"] = conversation_cost(meta.get("session_results", []))
    if enforcement.harness_changed(harness_diff):
        meta["harness_diff"] = harness_diff
    if harness_prior_diff and enforcement.harness_changed(harness_prior_diff):
        meta["harness_changed_since_last_invocation"] = harness_prior_diff
    if session_id:
        meta["session_id"] = session_id
    with open(metadata_path, "w") as f:
        json.dump(meta, f, indent=2)

    print(f"\n{'='*60}", flush=True)
    print(f"RUN {status.upper()}", flush=True)
    print(f"  Duration: {elapsed:.0f}s ({elapsed/60:.1f} min)", flush=True)
    print(f"  Sessions: {session_num}", flush=True)
    print(f"  Report: {'written' if has_report else 'not written'}", flush=True)
    if gate is not None:
        print(f"  Final gate: {gate.get('action') or gate.get('error')}", flush=True)
    if meta.get("total_cost_usd") is not None:
        print(f"  Cost: ${meta['total_cost_usd']:.2f}", flush=True)
    if status == "harness_tampered":
        print(f"  HARNESS CHANGED DURING RUN: {harness_diff}", flush=True)
    if status == "artifact_tampered":
        print(f"  PROTECTED RUN ARTIFACTS CHANGED OUTSIDE THEIR WRITERS: "
              f"{[e['file'] for e in integrity.open_events]}", flush=True)
    print(f"  Results: {run_dir_rel}/", flush=True)
    # Phase 29: the run's post-mortem (autopsy.md / autopsy.json), before storage
    # so it travels with the results; plus one row in the cross-run register.
    try:
        sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))
        import run_autopsy
        _a = run_autopsy.write(run_path)
        print(f"  Autopsy: primary layer {_a.get('primary_layer') or 'none (accepted)'}", flush=True)
        _row = run_autopsy.register_row(_a)
        # Lessons ledger: known error classes that came back, plus this run's
        # expert checklist (lessons_check.md).
        import lessons
        _row["lessons"] = lessons.register_fields(run_path)
        print(f"  Lessons: {_row['lessons']}", flush=True)
        import results_store
        if results_store.configured_account():
            _rp = os.path.join(run_path, "register_row.json")
            json.dump(_row, open(_rp, "w"), indent=1, default=str)
            results_store.upload_retry(_rp, f"outputs/register/{_a['run']}.json", attempts=2)
    except Exception as e:  # noqa: BLE001 — never block finalize
        print(f"[WARNING] autopsy/register failed: {type(e).__name__}: {e}", flush=True)
    stored = store_run_results(run_path)
    if stored:
        print(f"  Stored: {stored}", flush=True)
    print(f"{'='*60}", flush=True)
    return status


def store_run_results(run_path: str, service=None) -> str | None:
    """Phase 26: when a storage account is configured ($MA_STORAGE_ACCOUNT or
    $COILED_STORAGE_ACCOUNT), upload the finished run directory to
    outputs/<prefix>/<run name>.tar.gz so the result never depends on someone
    collecting it from the VM. Answer keys and credential files are left out
    and API keys redacted. The outcome is recorded in metadata.json
    (`stored` / `store_error`); a failure is loud but never fatal."""
    sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))
    import results_store
    account = results_store.configured_account()
    if not account and service is None:
        return None
    name = os.path.basename(os.path.normpath(run_path))
    blob, err = None, None
    try:
        secrets = [os.environ.get(k, "") for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")]
        data, skipped = results_store.archive_dir(run_path, name, secrets)
        tmp = os.path.join(os.path.dirname(os.path.normpath(run_path)), f".{name}.store.tar.gz")
        with open(tmp, "wb") as f:
            f.write(data)
        blob = f"outputs/{os.environ.get('MA_STORAGE_PREFIX', 'runs')}/{name}.tar.gz"
        results_store.upload_retry(tmp, blob, account, service=service)
        os.remove(tmp)
    except Exception as e:  # noqa: BLE001 — the local copy remains; say so loudly
        blob, err = None, f"{type(e).__name__}: {e}"[:500]
        print(f"[ERROR] RUN RESULTS NOT STORED (only the VM copy exists): {err}", file=sys.stderr, flush=True)
    try:
        mp = os.path.join(run_path, "metadata.json")
        meta = json.load(open(mp)) if os.path.exists(mp) else {}
        meta.update({"stored": blob, "store_error": err})
        json.dump(meta, open(mp, "w"), indent=2)
    except (OSError, ValueError):
        pass
    return blob


# ---------------------------------------------------------------------------
# Self-test (no API calls)
# ---------------------------------------------------------------------------

def _run_self_test() -> int:
    import tempfile
    import agents as agents_pkg
    from agents import modeler

    failures: list[str] = []

    def ok(cond, msg):
        if not cond:
            failures.append(msg)

    run_path = os.path.join(REPO_ROOT, "runs", "selftest_run")
    lead = None  # agent_type for the lead
    reg = set(agents_pkg.build_agents().keys())

    def deny(tool, inp, agent_type=lead):
        return enforcement.check_tool_use(tool, inp, run_path, agent_type, reg)

    # E1: harness writes denied for every agent
    for at in (None, "modeler", "writer"):
        ok(deny("Write", {"file_path": os.path.join(REPO_ROOT, "scripts/x.py")}, at),
           f"E1: Write scripts/ allowed for {at}")
        ok(deny("Edit", {"file_path": "agents/modeler.py"}, at),
           f"E1: Edit agents/ allowed for {at}")
        ok(deny("Write", {"file_path": os.path.join(REPO_ROOT, "docs/lessons/observations.jsonl")}, at),
           f"E1: Write to the lessons ledger allowed for {at}")
        ok(deny("Edit", {"file_path": os.path.join(REPO_ROOT, ".claude/orchestration/effort_floors.yaml")}, at),
           f"E1: Edit .claude/ allowed for {at}")
        ok(deny("Write", {"file_path": os.path.join(REPO_ROOT, "main.py")}, at),
           f"E1: Write main.py allowed for {at}")
    # E2: lead may not write critique YAMLs or models/**; specialists may
    ok(deny("Write", {"file_path": f"{run_path}/critique_methods.yaml"}),
       "E2: lead Write critique yaml allowed")
    ok(deny("Edit", {"file_path": f"{run_path}/models/sensitivity_analysis.yaml"}),
       "E2: lead Edit models/ allowed")
    ok(not deny("Write", {"file_path": f"{run_path}/critique_methods.yaml"}, "critique-methods"),
       "E2: critique agent denied its own yaml")
    ok(not deny("Edit", {"file_path": f"{run_path}/models/outcome_fn.py"}, "modeler"),
       "E2: modeler denied models/")
    # E3: lead may write its own files
    for f in ("progress.md", "pipeline_state.yaml", "scope_declaration.yaml"):
        ok(not deny("Write", {"file_path": f"{run_path}/{f}"}), f"E3: lead denied {f}")
    # E4: script outputs are write-protected for everyone; report.md only for writer
    for at in (None, "modeler", "analyst"):
        ok(deny("Edit", {"file_path": f"{run_path}/identifiability.yaml"}, at),
           f"E4: {at} may edit identifiability.yaml")
        ok(deny("Write", {"file_path": f"{run_path}/uncertainty_report.yaml"}, at),
           f"E4: {at} may write uncertainty_report.yaml")
        ok(deny("Write", {"file_path": f"{run_path}/metadata.json"}, at),
           f"E4: {at} may write metadata.json")
    ok(deny("Write", {"file_path": f"{run_path}/report.md"}), "E4: lead may write report.md")
    ok(not deny("Edit", {"file_path": f"{run_path}/metadata.json"}, "planner"),
       "E4: planner denied metadata.json (decision_year)")
    ok(not deny("Write", {"file_path": f"{run_path}/report.md"}, "writer"),
       "E4: writer denied report.md")
    # E5: unregistered agents denied, registered allowed
    ok(deny("Agent", {"subagent_type": "general-purpose", "prompt": "x"}),
       "E5: general-purpose spawn allowed")
    ok(deny("Agent", {"prompt": "x"}), "E5: spawn without subagent_type allowed")
    ok(not deny("Agent", {"subagent_type": "modeler", "prompt": "x"}),
       "E5: registered modeler spawn denied")
    # E6: Bash heuristics
    bash_denied = [
        "sed -i 's/DETECTED/ACKNOWLEDGED/' runs/selftest_run/identifiability.yaml",
        "echo hi > scripts/validate_critique_yaml.py",
        "cat x | tee -a agents/__init__.py",
        "cp /tmp/x.py scripts/identifiability.py",
        "rm -f .claude/orchestration/effort_floors.yaml",
        "git checkout -- scripts/",
        "python3 -c \"open('scripts/x.py','w').write('')\"",
        "cat .env",
        "set -a && source .env && set +a",
        "echo verdict >> runs/selftest_run/critique_domain.yaml",
    ]
    for cmd in bash_denied:
        ok(deny("Bash", {"command": cmd}), f"E6: Bash allowed: {cmd}")
    bash_allowed = [
        "python3 scripts/validate_critique_yaml.py runs/selftest_run --json",
        "python3 scripts/render_claims.py runs/selftest_run 2>&1 | tail -5",
        "cat scripts/identifiability.py | head",
        "grep -n verdict runs/selftest_run/identifiability.yaml",
        "echo note >> runs/selftest_run/progress.md",
        "ls .claude/skills > /dev/null",
    ]
    for cmd in bash_allowed:
        ok(not deny("Bash", {"command": cmd}), f"E6: Bash denied: {cmd}")
    # modeler may write models/ via bash; lead may not
    ok(not deny("Bash", {"command": "echo x > runs/selftest_run/models/a.yaml"}, "modeler"),
       "E6: modeler bash models/ denied")
    ok(deny("Bash", {"command": "echo x > runs/selftest_run/models/a.yaml"}),
       "E6: lead bash models/ allowed")
    # E7: credential reads blocked
    ok(deny("Read", {"file_path": os.path.join(REPO_ROOT, ".env")}), "E7: Read .env allowed")
    ok(not deny("Read", {"file_path": os.path.join(REPO_ROOT, "scripts/x.py")}),
       "E7: Read scripts denied")

    # IN1-IN7 (Phase 23 α): integrity record of protected run artifacts.
    from agents.integrity import ArtifactIntegrity, REPORT_NAME
    with tempfile.TemporaryDirectory() as td:
        def w(rel, text):
            os.makedirs(os.path.dirname(os.path.join(td, rel)), exist_ok=True)
            open(os.path.join(td, rel), "w").write(text)
        w("critique_domain.yaml", "verdict: REVISE\n")
        w("uncertainty_report.yaml", "n_draws: 200\n")
        ig = ArtifactIntegrity(td, process_check=lambda: set())
        # IN1: the lead's plain Bash rewrites a critique → violation
        ig.on_pre("b1", "Bash", {"command": "printf x > \"$P\""}, None)
        w("critique_domain.yaml", "verdict: PASS\n")
        ok([e["file"] for e in ig.on_post("b1")] == ["critique_domain.yaml"],
           f"IN1 lead rewrite of a critique flagged: {ig.events}")
        # IN2: the critic's own Write (or its Bash) → explained
        ig.on_pre("w1", "Write", {"file_path": f"{td}/critique_domain.yaml"}, "critique-domain")
        w("critique_domain.yaml", "verdict: REVISE\nblockers: []\n")
        ok(ig.on_post("w1") == [], "IN2 critic's own write explained")
        # IN3: a harness script run explains a script output
        ig.on_pre("b2", "Bash", {"command": f"python3 scripts/propagate_uncertainty.py {td}"}, "modeler")
        w("uncertainty_report.yaml", "n_draws: 400\n")
        ok(ig.on_post("b2") == [], "IN3 harness script explains its output")
        # IN4: a non-script Bash editing a script output → violation, unless a
        # background harness process is alive
        ig.on_pre("b3", "Bash", {"command": "p=uncertainty_report.yaml; sed -i '' s/400/40/ $p"}, "modeler")
        w("uncertainty_report.yaml", "n_draws: 40\n")
        ok([e["file"] for e in ig.on_post("b3")] == ["uncertainty_report.yaml"],
           "IN4 variable-expansion edit of a script output flagged")
        ig2 = ArtifactIntegrity(td, process_check=lambda: {"propagate_uncertainty.py"})
        w("uncertainty_report.yaml", "n_draws: 800\n")
        ok(ig2.final_check() == [], "IN4b a live background harness script explains it")
        # IN5: editing the integrity report itself is a violation
        w(REPORT_NAME, '{"producer": "agents/integrity.py", "violations": []}')
        ok(any(e["file"] == REPORT_NAME for e in ig2.final_check()), "IN5 report tamper flagged")
        # IN8 (Phase 36): a producer run from the agent's own shell script is
        # credited by the process watch; tampering after it ends is not.
        with tempfile.TemporaryDirectory() as td8:
            open(os.path.join(td8, "identifiability.yaml"), "w").write("a: 1\n")
            alive = set()
            ig8 = ArtifactIntegrity(td8, process_check=lambda: set(alive))
            ig8.on_pre("s1", "Bash", {"command": "bash models/run_all.sh"}, "modeler")
            alive.add("identifiability.py"); ig8.observe()
            open(os.path.join(td8, "identifiability.yaml"), "w").write("a: 2\n")
            alive.clear()
            ok(ig8.on_post("s1") == [], f"IN8 a producer started by the agent's script is credited: {ig8.events}")
            ig8.on_pre("s2", "Bash", {"command": "true"}, "modeler")
            ok(ig8.on_post("s2") == [], "IN8b the grant ends once the producer is seen finished")
            ig8.on_pre("s3", "Bash", {"command": "p=identifiability.yaml; echo 'a: 3' > $p"}, "modeler")
            open(os.path.join(td8, "identifiability.yaml"), "w").write("a: 3\n")
            ok([e["file"] for e in ig8.on_post("s3")] == ["identifiability.yaml"],
               "IN8c a change after the producer has finished is still flagged")
            ig9 = ArtifactIntegrity(td8, process_check=lambda: set(alive))
            ig9.on_pre("s4", "Bash", {"command": "bash models/run_all.sh"}, "modeler")
            open(os.path.join(td8, "identifiability.yaml"), "w").write("a: 4\n")
            ok([e["file"] for e in ig9.on_post("s4")] == ["identifiability.yaml"],
               "IN8d without a producer seen running, the change is flagged (control)")
            alive.add("identifiability.py")
            import time as _t8
            ig9.start_watch(0.05); _t8.sleep(0.3); ig9.stop_watch()
            ok(any(g.get("observed") for g in ig9.grants), "IN8e the background watch samples producers")
            ig10 = ArtifactIntegrity(td8, process_check=lambda: set(alive))
            alive.add("identifiability.py")
            _orig = ig10._process_check
            def _slow():
                got = _orig(); ig10._check(where="post_tool"); return got   # a check runs mid-sample
            ig10._process_check = _slow
            ig10.observe()
            ok(not any(g.get("observed") for g in ig10.grants),
               "IN8f a sample taken before a check that ran meanwhile is discarded")
            ig11 = ArtifactIntegrity(td8, process_check=lambda: set(alive))
            _seq_mid = ig11._check_seq
            ig11._check(where="post_tool")       # a check that began before the sample and ends after it
            ig11._check_seq = _seq_mid + 1       # as if sampled mid-check
            _orig11 = ig11._process_check
            def _mid():
                got = _orig11(); ig11._check_seq += 1; return got   # the check ends while sampling
            ig11._process_check = _mid
            ig11.observe()
            ok(not any(g.get("observed") for g in ig11.grants),
               "IN8g a sample overlapping the end of a check is discarded")
        # IN6: a resumed run keeps earlier violations
        ok(len(ArtifactIntegrity(td, process_check=lambda: set()).events) >= 1,
           "IN6 violations survive a restart")
        # IN7: hooks feed the record (allowed calls only)
        trace = open(os.path.join(td, "trace.jsonl"), "w")
        ig3 = ArtifactIntegrity(td, process_check=lambda: set())
        n0 = len(ig3.events)
        hk = agents_pkg.create_hooks(td, trace, datetime.now(), registered_agents=reg, integrity=ig3)
        pre_i, post_i = hk["PreToolUse"][0].hooks[0], hk["PostToolUse"][0].hooks[0]
        asyncio.run(pre_i({"tool_name": "Bash", "tool_input": {"command": "true"}}, "h1", None))
        w("critique_domain.yaml", "verdict: ACCEPT\n")
        asyncio.run(post_i({"tool_name": "Bash", "tool_input": {"command": "true"}}, "h1", None))
        ok(len(ig3.events) == n0 + 1 and ig3.events[-1]["file"] == "critique_domain.yaml",
           f"IN7 hooks record a lead change to a critique: {ig3.events[n0:]}")
        # TW1-TW3 (Phase 31): a subagent is warned before its turn limit, once per level
        hk2 = agents_pkg.create_hooks(td, trace, datetime.now(), registered_agents=reg, max_turns={"modeler": 20})
        pre2, post2, start2 = (hk2["PreToolUse"][0].hooks[0], hk2["PostToolUse"][0].hooks[0],
                               hk2["SubagentStart"][0].hooks[0])
        asyncio.run(start2({"agent_id": "m1", "agent_type": "modeler"}, None, None))
        got = []
        for k in range(20):
            call = {"tool_name": "Read", "tool_input": {"file_path": "x"}, "agent_id": "m1", "agent_type": "modeler"}
            asyncio.run(pre2(call, f"t{k}", None))
            r = asyncio.run(post2(call, f"t{k}", None))
            got.append((k + 1, (r.get("hookSpecificOutput") or {}).get("additionalContext")))
        warned = [(n, m) for n, m in got if m]
        ok([n for n, _ in warned] == [16, 17] and "Start writing" in warned[0][1] and "NOW" in warned[1][1],
           f"TW1 early warning (85% of the cap, at least 4 turns before it) and a final one 3 turns before, "
           f"once each: {[n for n, _ in warned]}")
        r = asyncio.run(post2({"tool_name": "Read", "tool_input": {}}, "tl", None))
        ok(r == {}, f"TW2 the lead is never warned: {r}")
        ok(sum(1 for l in open(os.path.join(td, "trace.jsonl")) if '"turn_warning"' in l) == 2,
           "TW3 each warning is recorded in the trace")
        # WH1-WH6 (Phase 31): the first write-up waits for the gate; the lead sees its budget
        from agents import writer_hold_reason, budget_note
        st = {"spent": 200.0, "cap": 450.0, "reserve": 36.0}
        hist = os.path.join(td, "validator_history.jsonl")

        def gates(*acts):
            open(hist, "w").write("".join(json.dumps({"round": k + 1, "action": a}) + "\n" for k, a in enumerate(acts)))
        gates("PATCH_OR_RETHINK", "PATCH_OR_RETHINK")
        ok(writer_hold_reason(td, st, 8) is not None, "WH1 writer held after PATCH with budget for another round")
        ok(writer_hold_reason(td, st, 2) is None, "WH2 writer allowed in the final round")
        ok(writer_hold_reason(td, {"spent": 380.0, "cap": 450.0, "reserve": 36.0}, 8) is None,
           "WH3 writer allowed when less than one round of budget is left")
        gates("PATCH_OR_RETHINK", "DECLARE_SCOPE")
        ok(writer_hold_reason(td, st, 8) is None, "WH4 writer allowed after DECLARE_SCOPE")
        gates("PATCH_OR_RETHINK")
        open(os.path.join(td, "manuscript.md"), "w").write("draft")
        ok(writer_hold_reason(td, st, 8) is None, "WH5 revisions of an existing draft are allowed")
        os.remove(os.path.join(td, "manuscript.md"))
        note = budget_note(st, 2)
        ok(note and "$200 of $450" in note and "$214 for modeling" in note and "2.1 more review round" in note,
           f"WH6 budget note: spent, reserve, rounds left at the average pace: {note}")
        hk3 = agents_pkg.create_hooks(td, trace, datetime.now(), registered_agents=reg, max_rounds=8,
                                      budget_status=lambda: st)
        pre3, post3 = hk3["PreToolUse"][0].hooks[0], hk3["PostToolUse"][0].hooks[0]
        spawn = {"tool_name": "Agent", "tool_input": {"subagent_type": "writer", "description": "write", "prompt": "x"}}
        r = asyncio.run(pre3(spawn, "w1", None))
        ok("write-up waits for the gate" in json.dumps(r), f"WH7 the hook refuses an early writer spawn: {r}")
        r = asyncio.run(post3({"tool_name": "Agent", "tool_input": {"subagent_type": "modeler"}}, "w2", None))
        ok("Budget: about $200" in json.dumps(r), f"WH8 the lead gets the budget note when a subagent returns: {r}")
        hk4 = agents_pkg.create_hooks(td, trace, datetime.now(), registered_agents=reg, max_rounds=8,
                                      budget_status=lambda: st, hold_writer=False)
        r = asyncio.run(hk4["PreToolUse"][0].hooks[0](spawn, "w3", None))
        ok("write-up waits" not in json.dumps(r), f"WH9 the harness's own write-up is never held: {r}")
        # RP1-RP10 (Phase 32): work waits for the round plan; the critique waits for the
        # planned fixes, the critics' fix checks and one return trip for what was not fixed
        import time
        from agents import critique_hold_reason
        from open_problems import check_plan as open_problems_check
        gates("PATCH_OR_RETHINK", "PATCH_OR_RETHINK")
        json.dump({"round": 2, "items": [
            {"id": "R-005", "source": "critique-redteam", "severity": "MEDIUM", "target_stage": "ANALYZE",
             "blocks_in": 1, "fix_check": True, "scope_declarable": True},
            {"id": "RIG-001", "source": "rigor-artifacts", "severity": "HIGH", "target_stage": "MODEL",
             "blocks_in": 0, "fix_check": False, "scope_declarable": False}]},
            open(os.path.join(td, "open_problems.json"), "w"))

        start3, stop3 = hk3["SubagentStart"][0].hooks[0], hk3["SubagentStop"][0].hooks[0]

        def spawn_as(sub, prompt, finish=True):
            r = asyncio.run(pre3({"tool_name": "Agent", "tool_input": {
                "subagent_type": sub, "description": "d", "prompt": prompt}}, "s", None))
            if r == {} and finish:   # the spawned agent runs and returns
                aid = f"a{time.time_ns()}"
                asyncio.run(start3({"agent_id": aid, "agent_type": sub}, None, None))
                asyncio.run(stop3({"agent_id": aid, "agent_type": sub}, None, None))
            return r
        r = spawn_as("modeler", "Fix RIG-001.")
        ok("round_plan.yaml" in json.dumps(r) and "does not exist" in json.dumps(r),
           f"RP1 modeler refused before the round plan exists: {r}")
        time.sleep(0.02)
        open(os.path.join(td, "round_plan.yaml"), "w").write(
            "round: 2\nitems:\n  R-005: {action: fix, stage: ANALYZE}\n  RIG-001: {action: fix, stage: MODEL}\n")
        time.sleep(0.02)
        ok(spawn_as("modeler", "Fix RIG-001 (zero-width interval).") == {}, "RP2 modeler allowed once the plan is complete")
        r = spawn_as("critique-redteam", "This is critique round 3. ...")
        ok("R-005 -> analyst" in json.dumps(r), f"RP3 critique refused while a planned fix was never handed over: {r}")
        ok(spawn_as("analyst", "Fix R-005: qualify the cases figure.") == {}, "RP4 analyst spawn allowed")
        r = spawn_as("critique-redteam", "This is critique round 3. ...")
        ok("fix check" in json.dumps(r) and "fix_check_r2_redteam.yaml" in json.dumps(r),
           f"RP5 critique refused until the critic checks its fixes: {r}")
        ok(critique_hold_reason(td, {"spent": 380.0, "cap": 450.0, "reserve": 36.0}) is None,
           "RP6 no fix check required when the budget cannot cover another round")
        ok(spawn_as("critique-redteam", "This is a fix check after round 2. Check R-005.") == {},
           "RP7 a fix-check spawn is allowed")
        time.sleep(0.02)
        open(os.path.join(td, "fix_check_r2_redteam.yaml"), "w").write(
            "round: 2\nreviewer: critique-redteam\nchecks:\n  - {id: R-005, verdict: partly, evidence: still no qualifier}\n")
        time.sleep(0.02)
        r = spawn_as("critique-redteam", "This is critique round 3. ...")
        ok("R-005 -> analyst" in json.dumps(r) and "back to its agent once" in json.dumps(r),
           f"RP8 an item not fixed goes back once before the full critique: {r}")
        spawn_as("analyst", "R-005 is only partly fixed: the critic says still no qualifier.")
        ok(spawn_as("critique-redteam", "This is critique round 3. ...") == {},
           "RP9 full critique allowed after the one return trip")
        # RP11-RP16 (Phase 32 γ): the plan is fixed once its work is handed out; reviews wait
        # for work agents still running in the background
        def lead_tool(name, inp):
            return json.dumps(asyncio.run(pre3({"tool_name": name, "tool_input": inp}, "t", None)))
        plan_p, t_rp = os.path.join(td, "round_plan.yaml"), time.time()
        ok(os.path.exists(os.path.join(td, "round_plan.frozen.json")), "RP11a the actions were recorded at hand-out")
        _plan_txt = open(plan_p).read()
        open(plan_p, "w").write("round: 2\nitems:\n  R-005: {action: declare, reason: 'cannot be fixed with the data at hand'}\n"
                                "  RIG-001: {action: fix, stage: MODEL}\n")
        r = json.dumps(spawn_as("critique-redteam", "This is critique round 3. ..."))
        ok("R-005: was planned as fix (ANALYZE)" in r and "stays that way" in r,
           f"RP11 a rewrite that changes a handed-out action (indirect writes included) holds the round: {r}")
        json.dump({"round": 2, "items": [
            {"id": "R-005", "source": "critique-redteam", "severity": "MEDIUM", "target_stage": "ANALYZE",
             "blocks_in": 1, "fix_check": True, "scope_declarable": True},
            {"id": "RIG-001", "source": "rigor-artifacts", "severity": "HIGH", "target_stage": "MODEL",
             "blocks_in": 0, "fix_check": False, "scope_declarable": False},
            {"id": "R-044", "source": "critique-redteam", "severity": "HIGH", "target_stage": "MODEL",
             "blocks_in": 0, "fix_check": False, "scope_declarable": True}]},
            open(os.path.join(td, "open_problems.json"), "w"))
        open(plan_p, "w").write(_plan_txt + "  R-044: {action: fix, stage: MODEL}\n")
        ok(open_problems_check(td) == [],
           f"RP12 a same-round gate rerun's new item can be added to a handed-out plan: {open_problems_check(td)}")
        r = json.dumps(spawn_as("critique-domain", "Please review everything again for round 3."))
        ok("Start a critique-domain prompt with one of" in r, f"RP12b a critic spawn must say what it is: {r}")
        ok(spawn_as("critique-domain", "This is a repair of critique_domain.yaml: fix the schema error.") == {},
           "RP12c a repair spawn is allowed and is not a review")
        spawn_as("modeler", "Fix R-044.")
        ok(spawn_as("modeler", "Re-check RIG-001.", finish=False) == {}, "RP13a modeler spawn allowed")
        r = json.dumps(spawn_as("critique-redteam", "This is critique round 3. ..."))
        ok("still working: modeler (launching)" in r, f"RP13 a critic waits for a modeler not yet started: {r}")
        asyncio.run(start3({"agent_id": "a-run", "agent_type": "modeler"}, None, None))
        r = json.dumps(spawn_as("writer", "Write the report."))
        ok("still working: modeler (since" in r, f"RP14 the writer waits for a running modeler: {r}")
        ok(spawn_as("critique-premortem", "Pre-mortem the plan.") == {}, "RP15 the pre-mortem waits for the planner only")
        trace.write(json.dumps({"ts": datetime.now().isoformat(), "type": "task_notification",
                                "task_id": "a-run", "status": "completed"}) + "\n")
        trace.flush()
        ok(spawn_as("critique-redteam", "This is critique round 3. ...") == {},
           "RP16 an agent cut off at its turn limit (task_notification, no stop) counts as finished")
        from agents import work_running
        ok(work_running(td, t_rp, now=time.time() + 3600) == [],
           "RP16b a spawn that never started stops counting after the grace period")
        from agents import plan_hold_reason
        os.remove(os.path.join(td, "open_problems.json"))
        ok("missing or stale" in (plan_hold_reason(td) or ""),
           "RP17 the gate asked for work but its open-problem list is missing: work waits (fail closed)")
        json.dump({"round": 2, "items": []}, open(os.path.join(td, "open_problems.json"), "w"))
        from agents.enforcement import protected_reason
        ok(protected_reason(os.path.join(td, "round_plan.yaml"), td, "modeler") is not None
           and protected_reason(os.path.join(td, "round_plan.yaml"), td, None) is None
           and protected_reason(os.path.join(td, "fix_check_r2_redteam.yaml"), td, None) is not None
           and protected_reason(os.path.join(td, "open_problems.json"), td, "critique-redteam") is not None
           and protected_reason(os.path.join(td, "fix_check_r2_redteam.yaml"), td, "modeler") is not None
           and protected_reason(os.path.join(td, "fix_check_r2_redteam.yaml"), td, "critique-domain") is not None
           and protected_reason(os.path.join(td, "fix_check_r2_redteam.yaml"), td, "critique-redteam") is None
           and protected_reason(os.path.join(td, "critique_domain.yaml"), td, "analyst") is not None
           and protected_reason(os.path.join(td, "round_plan.frozen.json"), td, None) is not None,
           "RP10 only the lead writes the plan, only critics write fix checks, only the gate writes the list")
        for f in ("open_problems.json", "round_plan.yaml", "fix_check_r2_redteam.yaml", "round_plan.frozen.json"):
            os.remove(os.path.join(td, f))
        gates()   # no gate history left for the sign-off tests below
        # SO1-SO8 (Phase 33): nothing but planning before the decision brief is signed
        import yaml as _y
        import decision_brief as _db
        from agents import signoff_hold_reason
        ok("planner writes decision_brief.yaml" in (signoff_hold_reason(td, "owner") or ""),
           "SO1 no brief: re-spawn the planner")
        _y.safe_dump({"hard_blockers": [{"id": "HB-001"}]}, open(os.path.join(td, "success_criteria.yaml"), "w"))
        _y.safe_dump({"question": "q", "decision": "d", "decision_maker": "m", "objective": "o",
                      "answers": [{"id": "A1", "statement": "s", "resolution": "district"}],
                      "acceptance": ["HB-001"]}, open(os.path.join(td, "decision_brief.yaml"), "w"))
        ok("AWAITING_SIGNOFF" in (signoff_hold_reason(td, "owner") or "")
           and "--by lead" in (signoff_hold_reason(td, "lead") or "") and signoff_hold_reason(td, None) is None,
           "SO2 a ready brief: owner mode waits, lead mode signs, no mode does nothing")
        hk5 = agents_pkg.create_hooks(td, trace, datetime.now(), registered_agents=reg, signoff="owner")
        pre5 = hk5["PreToolUse"][0].hooks[0]

        def call5(tool, inp):
            return json.dumps(asyncio.run(pre5({"tool_name": tool, "tool_input": inp}, "so", None)))
        ok("AWAITING_SIGNOFF" in call5("Agent", {"subagent_type": "data-agent", "prompt": "x"})
           and "deny" not in call5("Agent", {"subagent_type": "planner", "prompt": "fix the brief"}),
           "SO3 data work refused before sign-off; the planner may still revise the brief")
        ok("agents cannot sign" in call5("Bash", {"command": f"python3 scripts/decision_brief.py sign {td} --by owner"}),
           "SO4 in owner mode no agent can sign")
        hk6 = agents_pkg.create_hooks(td, trace, datetime.now(), registered_agents=reg, signoff="lead")
        r6 = json.dumps(asyncio.run(hk6["PreToolUse"][0].hooks[0](
            {"tool_name": "Bash", "tool_input": {"command": f"python3 scripts/decision_brief.py sign {td} --by lead"}},
            "so6", None)))
        ok("agents cannot sign" not in r6, "SO5 in lead mode the lead signs")
        ok(asyncio.run(wait_for_signoff(td, "runs/x", hours=0.00001, poll_s=0.01)) is False,
           "SO6 an unsigned wait times out")
        _db.sign(td, "owner")
        ok(asyncio.run(wait_for_signoff(td, "runs/x", hours=1, poll_s=0.01)) is True
           and signoff_hold_reason(td, "owner") is None
           and "deny" not in call5("Agent", {"subagent_type": "data-agent", "prompt": "x"}),
           "SO7 signed: the wait ends and work proceeds")
        open(os.path.join(td, "success_criteria.yaml"), "a").write("# moved\n")
        ok("frozen" in (signoff_hold_reason(td, "owner") or "") and "decision_brief.py restore" in
           (signoff_hold_reason(td, "owner") or ""), "SO8 criteria edited after sign-off: work stops until restored")
        from agents.enforcement import protected_reason as _pr, is_harness_path as _ihp, REPO_ROOT as _RR
        ok(_pr(os.path.join(td, "decision_brief.yaml"), td, "planner") is not None
           and _pr(os.path.join(td, "success_criteria.yaml"), td, None) is not None
           and not _ihp(_RR) and _ihp(os.path.join(_RR, "docs")),
           "SO10 once signed the brief and criteria are frozen for every agent; docs/ (parent of docs/lessons) is harness")
        ok(exit_code_for("awaiting_signoff") == 2, "SO9 a paused run exits 2 (resumable)")
        for f in ("success_criteria.yaml", "decision_brief.yaml", "decision_brief.signoff.json", "decision_brief.md"):
            if os.path.exists(os.path.join(td, f)):
                os.remove(os.path.join(td, f))
        os.remove(hist)
        trace.close()
    # IC1-IC7 (Codex review of PR #15).
    from agents.integrity import harness_scripts_run
    with tempfile.TemporaryDirectory() as td:
        def w(rel, text):
            os.makedirs(os.path.dirname(os.path.join(td, rel)), exist_ok=True)
            open(os.path.join(td, rel), "w").write(text)
        w("uncertainty_report.yaml", "n: 1\n"); w("critique_domain.yaml", "v: 1\n")
        w("metadata.json", '{"question": "Q", "methods_critic": true}')
        ig = ArtifactIntegrity(td, process_check=lambda: set())
        # IC1: a comment or string mentioning a script authorizes nothing
        _v = f"{REPO_ROOT}/scripts/validate_critique_yaml.py runs/x"
        _tf = [harness_scripts_run(f"timeout {o} python3 {_v}")[0] for o in
               ("600", "1.5m", "-k 5 600", "--signal TERM 600", "-s TERM 10s", "--kill-after=5 --preserve-status 600")]
        _neg = [harness_scripts_run(f"timeout {o} python3 {_v}")[0] for o in
                ("-k 5", "", "invalid", "--help 600", "-- -v 600", "600 X=1", "-k nonsense 600", "--signal 9x 600",
                 "--signal NOTASIGNAL 600", "\u0661\u0660", "-k \u0661 600", "-s 99 600", "-s \u017ftop 600", "-s 0 600",
                 f"-s {__import__('signal').NSIG} 600")]
        ok(all(x == ["validate_critique_yaml.py"] for x in _tf)
           and harness_scripts_run(f"timeout -- 600 python3 {_v}")[0] == ["validate_critique_yaml.py"]
           and harness_scripts_run(f"timeout 1e2 python3 {_v}")[0] == ["validate_critique_yaml.py"]
           and harness_scripts_run(f"timeout -s KILL 600 python3 {_v}")[0] == ["validate_critique_yaml.py"]
           and harness_scripts_run(f"timeout --signal=sigterm 600 python3 {_v}")[0] == ["validate_critique_yaml.py"]
           and all(x == [] for x in _neg),
           f"IG-timeout: known options and a valid duration are skipped; anything else credits nothing: {_tf} {_neg}")
        ok(harness_scripts_run('p=x; printf f > "$p"; # scripts/compare_models.py')[0] == []
           and harness_scripts_run('echo "python3 scripts/compare_models.py"')[0] == []
           and harness_scripts_run("cd /tmp && python3 /other/repo/scripts/compare_models.py x")[0] == []
           and harness_scripts_run("nohup .venv/bin/python3 scripts/propagate_uncertainty.py runs/x &")
           == (["propagate_uncertainty.py"], True),
           "IC1 only an executed harness script counts")
        ok(harness_scripts_run('R=runs/x && nohup sh -c "python3 scripts/propagate_uncertainty.py $R --n-draws 225; '
                               'echo UQDONE exit=\\$?" > $R/uq_run.log 2>&1 &')
           == (["propagate_uncertainty.py"], True)
           and harness_scripts_run("bash -c 'echo scripts/compare_models.py'")[0] == []
           and harness_scripts_run("bash /dev/null -c 'python3 scripts/propagate_uncertainty.py runs/x'")[0] == []
           and harness_scripts_run("bash -lc 'python3 scripts/compare_models.py runs/x'")[0] == ["compare_models.py"]
           and harness_scripts_run("bash -Zc 'python3 scripts/propagate_uncertainty.py runs/x'")[0] == []
           and harness_scripts_run("bash -n -c 'python3 scripts/propagate_uncertainty.py runs/x'")[0] == []
           and harness_scripts_run("bash -nc 'python3 scripts/propagate_uncertainty.py runs/x'")[0] == []
           and harness_scripts_run("nohup bash -o pipefail -c 'python3 scripts/propagate_uncertainty.py runs/x' &")
           == (["propagate_uncertainty.py"], True),
           "IC1d a producer inside sh -c / bash -c is recognized (E7); a mention is not")
        ig.on_pre("c1", "Bash", {"command": 'p=u.yaml; printf f > "$p"; # scripts/propagate_uncertainty.py'}, "modeler")
        w("uncertainty_report.yaml", "n: forged\n")
        ok(len(ig.on_post("c1")) == 1, "IC1b comment mention doesn't authorize a forged output")
        # IC1c: a script authorizes only its own outputs, never critiques
        ig.on_pre("c2", "Bash", {"command": "python3 scripts/compare_models.py runs/x"}, None)
        w("critique_domain.yaml", "v: forged\n")
        ok([e["file"] for e in ig.on_post("c2")] == ["critique_domain.yaml"],
           "IC1c a harness script doesn't authorize critique changes")
        # IC2: metadata.json is watched; the planner's edit is fine, the lead's is not
        ig.on_pre("c3", "Write", {"file_path": f"{td}/metadata.json"}, "planner")
        w("metadata.json", '{"question": "Q", "methods_critic": true, "decision_year": 2027}')
        ok(ig.on_post("c3") == [], "IC2 planner metadata edit explained")
        ig.on_pre("c4", "Bash", {"command": 'p=metadata.json; printf x > "$p"'}, None)
        w("metadata.json", '{"question": "Q", "methods_critic": false}')
        ok([e["file"] for e in ig.on_post("c4")] == ["metadata.json"], "IC2b lead metadata edit flagged")
        mp = os.path.join(td, "metadata.json")
        _restore_protected_meta(mp, {"question": "Q", "methods_critic": True})
        ok(json.load(open(mp))["methods_critic"] is True, "IC2c methods_critic restored before the gate")
        # IC3: a read-only report doesn't freeze the record; status blocks regardless
        os.chmod(os.path.join(td, REPORT_NAME), 0o444)
        w("uncertainty_report.yaml", "n: forged again\n")
        ig.final_check()
        rep = json.load(open(os.path.join(td, REPORT_NAME)))
        ok(len(rep["violations"]) == len(ig.events) >= 3,
           f"IC3 report rewritten despite chmod 444 ({len(rep['violations'])} vs {len(ig.events)})")
        ok(determine_status("session_completed", {"action": "ACCEPT"}, True, False, None,
                            integrity_violations=1) == "artifact_tampered"
           and exit_code_for("artifact_tampered") == 1, "IC3b violations block via status, exit 1")
        # IC5: the writer's allowed Bash write to report.md is explained
        ig5 = ArtifactIntegrity(td, process_check=lambda: set())
        ig5.on_pre("c5", "Bash", {"command": f"printf draft > {td}/report.md"}, "writer")
        w("report.md", "draft\n")
        ok(ig5.on_post("c5") == [], "IC5 writer carve-out covers its Bash too")
        # IC6: a background script that finishes between hooks is still explained once
        alive = {"v": {"propagate_uncertainty.py"}}
        ig5._process_check = lambda: alive["v"]
        ig5.on_pre("c6", "Bash", {"command": f"nohup python3 scripts/propagate_uncertainty.py {td} > log 2>&1 &"}, "modeler")
        ig5.on_post("c6")            # launch scan: seen alive → grant kept
        alive["v"] = set()           # it then finishes before the next hook
        w("uncertainty_report.yaml", "n: final\n")
        ok(ig5.on_post("poll") == [], "IC6 background grant explains the output after the process ended")
        w("uncertainty_report.yaml", "n: forged later\n")
        ig5.on_post("poll2")
        ok(any(e["file"] == "uncertainty_report.yaml" for e in ig5.events),
           "IC6b the grant expires once the process has been seen finished")
        # IC6c (Codex): a background launch that never ran grants nothing
        ig6 = ArtifactIntegrity(td, process_check=lambda: set())
        ig6.on_pre("c6c", "Bash", {"command": f"true || nohup python3 scripts/propagate_uncertainty.py {td} &"}, "modeler")
        ig6.on_post("c6c")
        ig6.on_pre("c6d", "Bash", {"command": 'p=uncertainty_report.yaml; printf x > "$p"'}, "modeler")
        w("uncertainty_report.yaml", "n: forged via dead grant\n")
        ok([e["file"] for e in ig6.on_post("c6d")] == ["uncertainty_report.yaml"],
           "IC6c a never-started background launch leaves no grant")
        # IC6d (Codex): `cd <repo> && python3 scripts/x.py` from another cwd is recognized;
        # a sibling run's process does not count
        ok(harness_scripts_run(f"cd {REPO_ROOT} && python3 scripts/coherence_audit.py {td}", cwd=td)[0]
           == ["coherence_audit.py"]
           and harness_scripts_run("cd $X && python3 scripts/coherence_audit.py r", cwd=td)[0] == [],
           "IC6d script resolution follows cd; unknown cd authorizes nothing")
        # IC6e (Codex r3): pushd/popd tracked; a concurrent tool's scan can't drop a grant
        ok(harness_scripts_run(f"pushd /tmp; popd; python3 scripts/coherence_audit.py {td}",
                               cwd=REPO_ROOT)[0] == ["coherence_audit.py"],
           "IC6e popd restores the directory for script resolution")
        ok(enforcement.check_tool_use("Bash", {"command": f"pushd /tmp; popd; echo x > {os.path.relpath(run_path, REPO_ROOT)}/critique_domain.yaml"},
                                      run_path, None, set()) is not None,
           "IC6f popd restores the directory for the write check")
        alive2 = {"v": set()}
        ig6e = ArtifactIntegrity(td, process_check=lambda: alive2["v"])
        ig6e.on_pre("L", "Bash", {"command": f"nohup python3 scripts/propagate_uncertainty.py {td} &"}, "modeler")
        ig6e.on_pre("R", "Read", {"file_path": f"{td}/x"}, "modeler")
        ig6e.on_post("R")              # concurrent Read finishes before the producer starts
        alive2["v"] = {"propagate_uncertainty.py"}
        ig6e.on_post("L")              # launch's own post: alive → grant kept
        alive2["v"] = set()
        w("uncertainty_report.yaml", "n: final after exit\n")
        ok(ig6e.on_post("poll") == [], "IC6g only the launching call's post-hook judges its grant")
        # IC6h (Codex r4): popd -n keeps the cwd; an unarmed grant expires
        ok(harness_scripts_run(f"cd /tmp; pushd {REPO_ROOT}; popd -n; python3 scripts/coherence_audit.py {td}",
                               cwd=REPO_ROOT)[0] == ["coherence_audit.py"],
           "IC6h popd -n does not change directory")
        import agents.integrity as _ig
        ig6h = ArtifactIntegrity(td, process_check=lambda: set())
        ig6h.on_pre("L2", "Bash", {"command": f"nohup python3 scripts/propagate_uncertainty.py {td} &"}, "modeler")
        for g in ig6h.grants:
            g["t"] -= _ig.INTENT_TTL_S + 1
        for i in ig6h.inflight.values():
            i["t"] -= _ig.INTENT_TTL_S + 1
        w("uncertainty_report.yaml", "n: forged after a lost post-hook\n")
        ok(any(e["file"] == "uncertainty_report.yaml" for e in ig6h.final_check()),
           "IC6i an unarmed grant whose post-hook never came expires")
        # IC7: a tool whose PostToolUse never came still explains its output at finalize
        ig7 = ArtifactIntegrity(td, process_check=lambda: set())
        ig7.on_pre("c7", "Bash", {"command": f"python3 scripts/coherence_audit.py {td}; false"}, "lead-ish")
        w("coherence_audit.yaml", "ok: true\n")
        ok(ig7.final_check() == [], "IC7 final scan runs before in-flight intents are cleared")
        # IC8 (Copilot): the cross-model critique is script-owned, not critic-owned
        w("critique_crossmodel.yaml", "v: 1\n")
        ig8 = ArtifactIntegrity(td, process_check=lambda: set())
        ig8.on_pre("c8", "Bash", {"command": 'p=critique_crossmodel.yaml; printf x > "$p"'}, "critique-redteam")
        w("critique_crossmodel.yaml", "v: forged\n")
        ok([e["file"] for e in ig8.on_post("c8")] == ["critique_crossmodel.yaml"],
           "IC8 a critic can't rewrite the script-owned cross-model critique")
        # IC9 (Copilot): a malformed record on resume is evidence, not a fresh start
        open(os.path.join(td, REPORT_NAME), "w").write("{truncated")
        ig9 = ArtifactIntegrity(td, process_check=lambda: set())
        ok(ig9.events and ig9.events[0]["change"] == "invalid_at_start",
           "IC9 unreadable integrity record on resume becomes a violation")
    # IR1-IR3 (E7 resume): a violation resolves when the producer rewrites the file.
    with tempfile.TemporaryDirectory() as td:
        def w(rel, text):
            os.makedirs(os.path.dirname(os.path.join(td, rel)), exist_ok=True)
            open(os.path.join(td, rel), "w").write(text)
        igr = ArtifactIntegrity(td, process_check=lambda: set())
        igr.on_pre("m1", "Bash", {"command": "python3 models/run_calibration.py"}, "modeler")
        w("identifiability.yaml", "verdict: CLEAN  # hand-written by an agent script\n")
        igr.on_post("m1")
        ok(len(igr.open_events) == 1, "IR1 agent-script write of a gate output opens a violation")
        igr.on_pre("m2", "Bash", {"command": f"python3 scripts/identifiability.py {td}"}, "modeler")
        w("identifiability.yaml", "producer: scripts/identifiability.py\nverdict: CLEAN\n")
        igr.on_post("m2")
        ok(igr.open_events == [] and igr.events[0].get("resolved")
           and determine_status("session_completed", {"action": "ACCEPT"}, True, False, None,
                                integrity_violations=len(igr.open_events)) == "completed",
           f"IR2 producer rewrite resolves it; the run is not tampered: {igr.events}")
        w(REPORT_NAME, '{"producer": "agents/integrity.py", "violations": []}')
        igr.final_check()
        igr.on_pre("m3", "Bash", {"command": f"python3 scripts/identifiability.py {td}"}, "modeler")
        igr.on_post("m3")
        ok(any(e["file"] == REPORT_NAME and e.get("open") for e in igr.events),
           "IR3 tampering with the integrity record itself is never resolved")
        # IR4/IR5 (Codex, PR #16): an identical-bytes producer rewrite resolves;
        # a skipped producer (file untouched) does not.
        w("models/eligibility_probe.yaml", "verdict: NOT_APPLICABLE\n")      # agent copy
        igr.on_pre("e1", "Bash", {"command": "cp x models/eligibility_probe.yaml"}, "modeler")
        igr.on_post("e1")
        ok(any(e["file"] == "models/eligibility_probe.yaml" and e.get("open") for e in igr.events),
           "IR4a copied gate output opens a violation")
        igr.on_pre("e2", "Bash", {"command": f"false && python3 scripts/eligibility_probe.py {td}"}, "modeler")
        igr.on_post("e2")
        ok(any(e["file"] == "models/eligibility_probe.yaml" and e.get("open") for e in igr.events),
           "IR4b a skipped producer (file untouched) does not resolve it")
        import time as _t; _t.sleep(0.01)
        igr.on_pre("e3", "Bash", {"command": f"python3 scripts/eligibility_probe.py {td}"}, "modeler")
        w("models/eligibility_probe.yaml", "verdict: NOT_APPLICABLE\n")      # identical bytes, rewritten
        igr.on_post("e3")
        ok(not any(e["file"] == "models/eligibility_probe.yaml" and e.get("open") for e in igr.events),
           "IR5 an identical-bytes producer rewrite resolves it")
    ok(enforcement.check_tool_use("Write", {"file_path": f"{run_path}/artifact_integrity.json"},
                                  run_path, "analyst", set()) is not None,
       "IN8 no agent may write artifact_integrity.json")

    # H1: hook returns the SDK deny shape and allows benign calls
    with tempfile.TemporaryDirectory() as td:
        trace = open(os.path.join(td, "trace.jsonl"), "w")
        hooks = agents_pkg.create_hooks(td, trace, datetime.now(), registered_agents=reg)
        pre = hooks["PreToolUse"][0].hooks[0]
        post = hooks["PostToolUse"][0].hooks[0]
        out = asyncio.run(pre({"tool_name": "Edit",
                               "tool_input": {"file_path": f"{td}/critique_methods.yaml"}},
                              "t1", None))
        hso = out.get("hookSpecificOutput", {})
        ok(hso.get("permissionDecision") == "deny" and hso.get("hookEventName") == "PreToolUse",
           f"H1: lead edit of critique yaml not denied via hook: {out}")
        out = asyncio.run(pre({"tool_name": "Edit", "agent_id": "a1", "agent_type": "critique-methods",
                               "tool_input": {"file_path": f"{td}/critique_methods.yaml"}},
                              "t2", None))
        ok(out == {}, f"H1: critique agent's own edit denied: {out}")
        out = asyncio.run(pre({"tool_name": "Edit", "agent_id": "a9",
                               "tool_input": {"file_path": f"{td}/models/x.py"}},
                              "t2b", None))
        ok(out == {}, f"H1: untyped subagent treated as lead: {out}")
        # H2: post hook snapshots report.md on change
        with open(os.path.join(td, "report.md"), "w") as f:
            f.write("v1")
        asyncio.run(post({"tool_name": "Write", "tool_input": {}}, "t3", None))
        snap = os.path.join(td, "report.snapshot.md")
        ok(os.path.exists(snap) and open(snap).read() == "v1", "H2: snapshot not written")
        os.remove(os.path.join(td, "report.md"))
        ok(restore_report_if_missing(td) and open(os.path.join(td, "report.md")).read() == "v1",
           "H2: restore from snapshot failed")
        trace.close()
        ok("denied" in open(os.path.join(td, "trace.jsonl")).read(), "H1: denial not traced")

    # S1: status determination
    acc = {"action": "ACCEPT"}
    ok(determine_status("session_completed", acc, True, False, None) == "completed", "S1 accept")
    ok(determine_status("session_completed", acc, True, True, None) == "completed_with_report_restored",
       "S1 restored")
    ok(determine_status("session_completed", {"action": "DECLARE_SCOPE"}, True, False, None)
       == "completed_scope_declared", "S1 declare")
    ok(determine_status("session_completed", {"action": "PATCH_OR_RETHINK"}, True, False, None)
       == "completed_gate_not_accepted", "S1 not accepted")
    ok(determine_status("session_completed", {"action": None, "error": "x"}, True, False, None)
       == "completed_gate_error", "S1 gate error")
    ok(determine_status("session_completed", acc, False, False, None) == "completed_no_report",
       "S1 no report")
    ok(determine_status("session_completed", {"action": "RUN_FAILED"}, False, False, None)
       == "run_failed", "S1 run failed")
    ok(determine_status("session_completed", None, True, False, None) == "completed_gate_not_run",
       "S1 gate not run")
    ok(determine_status("interrupted", acc, True, False, None) == "interrupted", "S1 interrupted")
    ok(determine_status("session_completed", acc, True, False,
                        {"modified": ["scripts/x.py"], "added": [], "removed": []})
       == "harness_tampered", "S1 tampered")
    ok(exit_code_for("completed") == 0 and exit_code_for("completed_scope_declared") == 2
       and exit_code_for("max_sessions_reached") == 1 and exit_code_for("interrupted") == 130,
       "S1 exit codes")

    # S2: ResultMessage classification
    class _R:
        def __init__(self, subtype, is_error):
            self.subtype, self.is_error = subtype, is_error
    ok(result_is_success(_R("success", False)), "S2 success")
    ok(not result_is_success(_R("error_max_turns", True)), "S2 max_turns treated as success")
    ok(not result_is_success(_R("success", True)), "S2 is_error ignored")
    ok(not result_is_success(None), "S2 None")
    ok(not is_policy_block("constraint violated in optimizer"), "S2 'violate' misclassified")
    ok(is_policy_block("API Error: request violates our Usage Policy"), "S2 policy missed")

    # P1: prompts rendered, no leftover sentinels / placeholders
    for sentinel in ("__AGENT_ROSTER__", "__UQ_DRAW_FLOORS__"):
        ok(sentinel not in LEAD_SYSTEM_PROMPT, f"P1 {sentinel} left in lead prompt")
    for sentinel in ("__RIGOR_TIMELINE_TABLE__", "__EFFORT_FLOORS_TABLE__",
                     "__CALIBRATION_SCHEMA__", "__EFFORT_ENFORCE_ROUND__"):
        ok(sentinel not in modeler.SYSTEM_PROMPT, f"P1 {sentinel} left in modeler prompt")
    for name in reg:
        ok(f"**{name}**" in LEAD_SYSTEM_PROMPT, f"P1 roster missing {name}")
    ok("model-tester" not in reg and "Agent" not in modeler.TOOLS, "P1 dead model-tester path")
    built = agents_pkg.build_agents(question="SIR model of measles", run_dir="runs/X")
    for name, d in built.items():
        ok("{run_dir}" not in d.prompt, f"P1 {{run_dir}} left in {name} prompt")
        ok(not any(s in (d.skills or []) for s in ("malaria-modeling", "vectors")),
           f"P1 malaria skills attached to {name} for a measles question")
    built = agents_pkg.build_agents(question="malaria transmission model using Starsim")
    ok("malaria-modeling" in built["modeler"].skills
       and "starsim-dev-intro" in built["modeler"].skills, "P1 gated skills dropped for matching question")
    ok("uq_min_draws_local" in LEAD_SYSTEM_PROMPT and "--n-draws 200" not in LEAD_SYSTEM_PROMPT,
       "P1 UQ floors not rendered from manifest")
    ok("calibration_result.yaml" in modeler.SYSTEM_PROMPT
       and "n_restarts" in modeler.SYSTEM_PROMPT
       and "eligibility_manifest.yaml" in modeler.SYSTEM_PROMPT, "P1 modeler contract missing")
    ok("source .env" not in LEAD_SYSTEM_PROMPT, "P1 lead still told to source .env")
    p_fresh = build_lead_prompt("q", "runs/x", 5, "")
    p_resume = build_lead_prompt("q", "runs/x", 5, "RESUME: Continue from stage MODEL.")
    ok("Start with STAGE 1" in p_fresh and "Start with STAGE 1" not in p_resume,
       "P1 resume prompt still says Start with STAGE 1")

    # V1: environment filtering
    env = {"ANTHROPIC_API_KEY": "a", "AZ_BATCH_ACCOUNT": "b", "OPENAI_API_KEY": "c", "PATH": "/bin"}
    ov = enforcement.filtered_env(env, allow_cloud=False)
    ok({k: v for k, v in ov.items() if k not in ("PATH", "VIRTUAL_ENV") and "CACHE_TTL" not in k}
       == {"AZ_BATCH_ACCOUNT": "", "OPENAI_API_KEY": ""}, f"V1 env filter: {ov}")
    ok(ov.get("CLAUDE_CODE_PROMPT_CACHE_TTL") == "1h" and ov.get("CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL") == "1h"
       and enforcement.filtered_env({**env, "MA_PROMPT_CACHE_TTL": "5m"}, False)["CLAUDE_CODE_PROMPT_CACHE_TTL"] == "5m",
       f"V1b (Phase 31) 1-hour prompt cache for the lead and subagents, 5m on request: {ov}")
    ok("AZ_BATCH_ACCOUNT" not in enforcement.filtered_env(env, allow_cloud=True),
       "V1 cloud creds kept with --cloud")
    # V2 (Phase 21 α): agents' PATH starts with this interpreter's bin/,
    # so bare `python3` is the harness venv even when it wasn't activated.
    ok(ov["PATH"].split(os.pathsep)[0] == os.path.dirname(sys.executable)
       and ov["PATH"].endswith("/bin"), f"V2 PATH not venv-first: {ov['PATH']}")
    venv_ov = enforcement.interpreter_env({"PATH": "/usr/bin"},
                                          os.path.join(REPO_ROOT, ".venv", "bin", "python3"))
    ok(venv_ov.get("VIRTUAL_ENV") == os.path.join(REPO_ROOT, ".venv"),
       f"V2 VIRTUAL_ENV not set for a venv interpreter: {venv_ov}")
    # M1: every agent and the lead are pinned to explicit model IDs.
    pinned = {enforcement.MODEL_DEEP, enforcement.MODEL_FAST}
    reg = build_agents(question="q", run_dir="runs/x")
    ok(all(d.model in pinned for d in reg.values()),
       f"M1 unpinned agent models: {{n: d.model for n, d in reg.items()}}")

    # T1: harness hashing detects a modification
    with tempfile.TemporaryDirectory() as td:
        os.makedirs(os.path.join(td, "scripts"))
        p = os.path.join(td, "scripts", "a.py")
        with open(p, "w") as f:
            f.write("x = 1\n")
        # HL1-HL2: harness files are read-only during a run, and chmod on them is denied
        os.makedirs(os.path.join(td, "scripts"), exist_ok=True)
        _hp = os.path.join(td, "scripts", "x.py")
        open(_hp, "w").write("x = 1\n")
        _modes = enforcement.lock_harness(td)
        try:
            open(_hp, "w").write("x = 2\n")
            _wrote = True
        except PermissionError:
            _wrote = os.geteuid() == 0   # root ignores file modes; the hash still catches it
        try:
            open(os.path.join(td, "scripts", "y.tmp"), "w").write("y")
            os.replace(os.path.join(td, "scripts", "y.tmp"), _hp)
            _replaced = os.geteuid() != 0
        except PermissionError:
            _replaced = False
        enforcement.unlock_harness(_modes)
        ok(not _wrote and not _replaced and _hp in _modes and os.access(_hp, os.W_OK)
           and os.access(os.path.join(td, "scripts"), os.W_OK),
           "HL1 a locked harness file cannot be written or replaced; unlock restores it")
        ok(enforcement.bash_reason("chmod u+w scripts/effect_size_registry.py", td, "modeler") is not None
           and enforcement.bash_reason("python3 -c \"import os; os.chmod('agents/x.py', 0o644)\"", td, "modeler") is not None
           and enforcement.bash_reason("chmod +x runs/r1/models/run.sh", td, "modeler") is None,
           "HL2 changing harness permissions is denied; run files are not affected")
        _hl3 = enforcement.bash_reason("cat > models/x.sh <<EOF\npython3 scripts/compute.py map .\nEOF\nchmod +x models/x.sh",
                                       td, "modeler") or ""
        ok("separate command" in _hl3 and enforcement.bash_reason("chmod +x models/x.sh", td, "modeler") is None,
           "HL3 a refused chmod says how to chmod a run file; a separate chmod of a run file is allowed")
        os.remove(_hp)
        before = enforcement.hash_harness(td)
        ok(not enforcement.harness_changed(enforcement.diff_harness(before, enforcement.hash_harness(td))),
           "T1 clean tree flagged")
        with open(p, "w") as f:
            f.write("x = 2\n")
        d = enforcement.diff_harness(before, enforcement.hash_harness(td))
        ok(d["modified"] == ["scripts/a.py"], f"T1 modification missed: {d}")

    # G1: final gate parses validator JSON, handles missing critiques and errors
    with tempfile.TemporaryDirectory() as td:
        ok(run_final_gate(td, 5) is None, "G1 gate ran with no critiques")
        with open(os.path.join(td, "critique_methods.yaml"), "w") as f:
            f.write("round: 3\n")
        ok(detect_current_round(td) == 3, "G1 round detection")
        stub = os.path.join(td, "stub_validator.py")
        with open(stub, "w") as f:
            f.write("import json,sys\nprint('stderr text', file=sys.stderr)\n"
                    "print(json.dumps({'action': 'DECLARE_SCOPE', 'unresolved_high': [1, 2]}))\n"
                    "sys.exit(1)\n")
        g = run_final_gate(td, 5, validator=stub)
        ok(g and g["action"] == "DECLARE_SCOPE" and g["n_unresolved_high"] == 2
           and g["current_round"] == 3, f"G1 gate parse: {g}")
        ok(os.path.exists(os.path.join(td, "final_gate.json")), "G1 final_gate.json not written")
        with open(stub, "w") as f:
            f.write("import sys\nprint('Traceback boom', file=sys.stderr)\nsys.exit(1)\n")
        g = run_final_gate(td, 5, validator=stub)
        ok(g and g["action"] is None and "boom" in g.get("error", ""), f"G1 crash handling: {g}")

    # XM (Phase 21 ζ): cross-model step only when enabled; its outputs are
    # write-protected; the lead may run the script.
    ok("cross_model_critic.py runs/x --round" in build_lead_prompt("q", "runs/x", 5, "", cross_model=True)
       and "cross_model_critic" not in build_lead_prompt("q", "runs/x", 5, ""),
       "XM1 cross-model lead prompt gating")
    # MC1 (E5): critique-methods is spawned only when the run opts in.
    ok("METHODS CRITIC (enabled" in build_lead_prompt("q", "runs/x", 5, "", methods_critic=True)
       and "METHODS CRITIC" not in build_lead_prompt("q", "runs/x", 5, "")
       and "Spawn ALL THREE critique agents" in LEAD_SYSTEM_PROMPT,
       "MC1 methods critic lead prompt gating")
    # CR1-CR4 (Codex review of PR #14): `cd` is tracked; Grep/Glob can't read .env.
    rp_rel = os.path.relpath(run_path, REPO_ROOT)
    rp_cd = f"cd {rp_rel}"
    ok(enforcement.check_tool_use("Bash", {"command": f"cd {rp_rel} && echo forged > critique_domain.yaml"},
                                  run_path, None, set()) is not None,
       "CR1 cd into the run dir then write a protected file is denied")
    ok(enforcement.check_tool_use("Bash", {"command": "cd $RUN && echo x > critique_domain.yaml"},
                                  run_path, None, set()) is not None,
       "CR2 cd to an unknown dir: relative protected write is denied conservatively")
    ok(enforcement.check_tool_use("Bash", {"command": f"cd {rp_rel} && echo ok > notes.txt"},
                                  run_path, None, set()) is None,
       "CR3 cd then an ordinary write stays allowed")
    ok(enforcement.check_tool_use("Grep", {"path": os.path.join(REPO_ROOT, ".env"), "pattern": ".*"},
                                  run_path, "analyst", set()) is not None
       and enforcement.check_tool_use("Grep", {"path": REPO_ROOT, "glob": ".env*", "pattern": "KEY"},
                                      run_path, "analyst", set()) is not None
       and enforcement.check_tool_use("Grep", {"path": run_path, "glob": "*.py", "pattern": "x"},
                                      run_path, "analyst", set()) is None
       and enforcement.check_tool_use("Glob", {"pattern": "**/*"}, run_path, "analyst", set()) is None,
       "CR4 Grep on .env denied; ordinary Grep and Glob allowed")
    # CR6-CR8 (Codex re-review): cd-segment redirects, subshell cd, ../ after unknown cd.
    ok(enforcement.check_tool_use("Bash", {"command": f"cd /tmp > {rp_rel}/critique_domain.yaml"},
                                  run_path, None, set()) is not None,
       "CR6 redirect on a cd segment is checked before the cd")
    ok(enforcement.check_tool_use("Bash", {"command": f"(cd /tmp); echo forged > {rp_rel}/critique_domain.yaml"},
                                  run_path, None, set()) is not None
       and enforcement.check_tool_use("Bash", {"command": "(cd /tmp && echo ok > x.txt); echo hi"},
                                      run_path, None, set()) is None,
       "CR7 a subshell cd does not leak into later segments")
    ok(enforcement.check_tool_use("Bash", {"command": 'cd "$RUN" && echo forged > ../critique_domain.yaml'},
                                  run_path, None, set()) is not None,
       "CR8 ../ target after an unknown cd is checked by file name")
    with tempfile.TemporaryDirectory() as td:
        mp = os.path.join(td, "metadata.json")
        with open(mp, "w") as f:
            f.write("{corrupt")
        _restore_protected_meta(mp, {"question": "Q", "run_dir": "r"})
        m = json.load(open(mp))
        ok(m.get("question") == "Q" and m.get("run_dir") == "r",
           "CR9 corrupted metadata.json is rebuilt with the protected fields")
        with open(mp, "w") as f:
            json.dump({"question": "Q", "run_dir": "r", "restored_fields": None}, f)
        _restore_protected_meta(mp, {"question": "Q", "run_dir": "r"})
        ok(json.load(open(mp))["question"] == "Q", "CR10 restored_fields: null does not crash")
    ok(enforcement.check_tool_use("Bash", {"command": f"(cd {rp_rel} && echo ')' && echo forged > critique_domain.yaml; true)"},
                                  run_path, None, set()) is not None,
       "CR11 a quoted ) does not close the subshell")
    ok(enforcement.check_tool_use("Bash", {"command": f"cd {rp_rel} && (cd /tmp && echo \\\"); echo forged > critique_domain.yaml"},
                                  run_path, None, set()) is not None,
       "CR12 an escaped quote does not hide the subshell close")
    for cmd in (f'({rp_cd} && echo "a;\\" )" && echo forged > critique_domain.yaml; true)',
                f'({rp_cd} && echo "a;)b" && echo forged > critique_domain.yaml; true)'):
        ok(enforcement.check_tool_use("Bash", {"command": cmd}, run_path, None, set()) is not None,
           f"CR13 quoted separators/parens don't desync subshell tracking: {cmd}")
    ok(enforcement.check_tool_use("Bash", {"command": "cd models && python3 fit.py > fit.log 2>&1 && echo done"},
                                  run_path, "modeler", set()) is None,
       "CR14 ordinary redirects (2>&1) still allowed")
    # CR5 (Codex review of PR #14): the gate must see the original question.
    with tempfile.TemporaryDirectory() as td:
        mp = os.path.join(td, "metadata.json")
        with open(mp, "w") as f:
            json.dump({"question": "weakened", "decision_year": 2027}, f)
        _restore_protected_meta(mp, {"question": "Use Starsim within a $10M budget."})
        m = json.load(open(mp))
        ok(m["question"] == "Use Starsim within a $10M budget." and m["decision_year"] == 2027
           and m["restored_fields"]["question"] == "weakened",
           "CR5 protected metadata restored (before the gate), planner fields kept")
    # BG1 (Phase 23 β): run-wide budget cap.
    ok(remaining_budget(None, [{"total_cost_usd": 5}]) is None
       and remaining_budget(300, [{"total_cost_usd": 120.5}, {"total_cost_usd": None},
                                  {"total_cost_usd": 79.5}]) == 100
       and remaining_budget(300, [{"total_cost_usd": 310}]) == -10
       and determine_status("budget_exhausted", {"action": "PATCH_OR_RETHINK"}, True, False, None)
       == "budget_exhausted" and exit_code_for("budget_exhausted") == 2,
       "BG1 remaining budget sums sessions; budget stop is a partial status (exit 2)")
    # BG2 (Codex, PR #15): an unreconciled session is charged its whole
    # allowance; the ledger is harness-owned and survives a resume.
    with tempfile.TemporaryDirectory() as td:
        R = lambda c, sid="s1": type("R", (), {"total_cost_usd": c, "session_id": sid})()
        led0 = {"sessions": []}
        charge_session(led0, 1, R(4.0), 10.0)
        charge_session(led0, 2, R(6.5), 6.0)       # resumed: cumulative 6.5 → charge 2.5
        ok(remaining_budget(10.0, led0["sessions"]) == 3.5,
           f"BG2a resumed session charged the increase only: {led0}")
        ok(conversation_cost([{"total_cost_usd": 4.0, "session_id": "a"},
                              {"total_cost_usd": 6.5, "session_id": "a"},
                              {"total_cost_usd": 1.0, "session_id": "b"}]) == 7.5,
           "BG2b run cost = max per conversation, summed across conversations")
        led = load_budget_ledger(td)
        charge_session(led, 1, R(4.0), 5.0)
        charge_session(led, 2, None, remaining_budget(5.0, led["sessions"]))
        # CR15-CR18 (Phase 28): crashed sessions.
        class _R2:
            def __init__(self, c, sid):
                self.total_cost_usd, self.session_id = c, sid
        _lc = {"sessions": []}
        charge_session(_lc, 1, _R2(62.0, "s1"), 450.0, session_id="s1", source="reconciled")
        ok(_lc["sessions"][0]["total_cost_usd"] == 62.0 and remaining_budget(450.0, _lc["sessions"]) > 380,
           f"CR15 a crashed session is charged its read-back cost, not the cap: {_lc}")
        charge_session(_lc, 2, _R2(100.0, "s1"), 388.0)
        ok(abs(sum(e["total_cost_usd"] for e in _lc["sessions"]) - 100.0) < 1e-6,
           f"CR16 the next cumulative report adds only the increase: {_lc}")
        _lc2 = {"sessions": []}
        charge_session(_lc2, 1, None, 8.0, session_id=None, elapsed_s=6.0)
        ok(_lc2["sessions"][0]["total_cost_usd"] < 0.2 and remaining_budget(8.0, _lc2["sessions"]) > 7.5,
           f"CR17 crashed before its conversation started: a duration bound: {_lc2}")
        _lc5 = {"sessions": []}
        charge_session(_lc5, 1, None, 100.0, session_id=None, elapsed_s=3 * 3600)
        ok(_lc5["sessions"][0]["total_cost_usd"] == 100.0, "CR17 the bound never exceeds the allowance")
        _lc6 = {"sessions": []}
        charge_session(_lc6, 1, None, 450.0, session_id="s1", elapsed_s=60.0)
        ok(remaining_budget(450.0, _lc6["sessions"]) <= 0,
           "CR18 a started conversation whose cost cannot be read is charged the whole allowance (cap holds)")

        async def _fq_ok(prompt, options):
            assert options.resume == "s1" and options.max_turns == 1 and options.tools == []
            yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                                num_turns=1, session_id="s1", total_cost_usd=62.5)

        async def _fq_fail(prompt, options):
            raise RuntimeError("api down")
            yield  # pragma: no cover
        _opts = ClaudeAgentOptions(model="m", tools=["Bash"], allowed_tools=["Bash"], max_turns=50,
                                   max_buffer_size=enforcement.MAX_BUFFER_SIZE)
        _r = asyncio.run(reconcile_conversation_cost(_opts, "s1", query_fn=_fq_ok))
        ok(_r is not None and _r.total_cost_usd == 62.5, f"CR18 read-back resumes the conversation tool-less: {_r}")
        ok(asyncio.run(reconcile_conversation_cost(_opts, "s1", query_fn=_fq_fail)) is None,
           "CR18 an unreadable cost returns None (then the allowance is charged)")

        async def _fq_zero(prompt, options):
            yield ResultMessage(subtype="error_during_execution", duration_ms=1, duration_api_ms=0,
                                is_error=True, num_turns=0, session_id="s1", total_cost_usd=0.0)
        ok(asyncio.run(reconcile_conversation_cost(_opts, "s1", query_fn=_fq_zero)) is None,
           "CR18 an error or zero-cost read-back is not a cost reading (crash smoke 5 read back $0.00)")
        # CR19 (Phase 34 δ): the read-back is retried, then the transcripts give the cost.
        _tr_root = os.path.join(td, "projects")
        os.makedirs(os.path.join(_tr_root, "-tmp-x"), exist_ok=True)
        with open(os.path.join(_tr_root, "-tmp-x", "s1.jsonl"), "w") as _f:
            _f.write(json.dumps({"type": "assistant", "message": {"id": "m1", "model": "claude-opus-5-5",
                                                                   "usage": {"input_tokens": 500_000}}}) + "\n")
        _calls = []

        async def _fq_count(prompt, options):
            _calls.append(1)
            async for m in _fq_fail(prompt, options):
                yield m
        _c, _src = asyncio.run(crashed_session_cost(_opts, "s1", attempts=3, delay_s=0, query_fn=_fq_count,
                                                    transcript_root=_tr_root))
        ok(len(_calls) == 3 and _c is not None and abs(_c.total_cost_usd - 2.1) < 1e-9
           and _src.startswith("estimated from the CLI transcripts"),
           f"CR19 three read-back tries, then the transcript estimate (+5%): {len(_calls)} {_c} {_src}")
        _c2, _ = asyncio.run(crashed_session_cost(_opts, "s9", attempts=1, delay_s=0, query_fn=_fq_fail,
                                                  transcript_root=_tr_root))
        _c3, _src3 = asyncio.run(crashed_session_cost(_opts, "s1", attempts=3, delay_s=0, query_fn=_fq_ok,
                                                      transcript_root=_tr_root))
        ok(_c2 is None and _c3.total_cost_usd == 62.5 and _src3.startswith("reconciled"),
           "CR19b no transcript -> None (allowance charged); a working read-back is used first")
        write_budget_ledger(td, led, 5.0)
        again = load_budget_ledger(td)
        ok(remaining_budget(5.0, again["sessions"]) == 0 and again["spent_usd"] == 5.0
           and again["sessions"][1]["source"].startswith("unreconciled"),
           f"BG2 disconnect charged in full; ledger reloads: {again}")
        mig = {"sessions": [{"session": 0, "session_id": "s1", "reported_usd": 4.0,
                             "total_cost_usd": 4.0, "source": "migrated"}]}
        charge_session(mig, 1, R(6.0), 6.0)
        ok(remaining_budget(10.0, mig["sessions"]) == 4.0,
           f"BG2c migrated spend + resumed cumulative result is not double-counted: {mig}")
        ok(enforcement.check_tool_use("Write", {"file_path": f"{td}/budget_ledger.json"},
                                      td, "planner", set()) is not None,
           "BG3 agents may not write the budget ledger")
    # CS1 (Phase 22 γ): the ledger is built by a script; agents write the spec.
    ok(enforcement.check_tool_use("Write", {"file_path": f"{run_path}/models/claims_ledger.yaml"},
                                  run_path, "analyst", set()) is not None
       and enforcement.check_tool_use("Write", {"file_path": f"{run_path}/models/claims_spec.yaml"},
                                      run_path, "analyst", set()) is None
       and enforcement.check_tool_use("Bash", {"command": f"python3 scripts/build_claims_ledger.py {run_path}"},
                                      run_path, "analyst", set()) is None,
       "CS1 claims ledger script-produced; spec writable; builder runnable")
    ok(enforcement.check_tool_use("Write", {"file_path": f"{run_path}/critique_crossmodel.yaml"},
                                  run_path, "critique-redteam", set(reg)) is not None,
       "XM2 critique_crossmodel.yaml writable by an agent")
    ok(enforcement.check_tool_use("Bash", {"command": "python3 scripts/cross_model_critic.py runs/x --round 2"},
                                  run_path, None, set(reg)) is None,
       "XM3 lead cannot run cross_model_critic.py")

    # BG1 (Phase 21 θ): background execution denied for every agent.
    for agent_t in (None, "modeler", "analyst"):
        ok(enforcement.check_tool_use("Bash", {"command": "python3 x.py", "run_in_background": True},
                                      run_path, agent_t, set(reg)) == enforcement.BACKGROUND_DENY_REASON,
           f"BG1 background Bash allowed for {agent_t}")
    ok(enforcement.check_tool_use("Bash", {"command": "ls"}, run_path, None, set(reg)) is None,
       "BG1 foreground Bash wrongly denied")
    # CT1 (Phase 21 θ): unfinished-pipeline detection drives continuation.
    with tempfile.TemporaryDirectory() as td:
        ok(pipeline_unfinished(td) is not None, "CT1 empty run dir not unfinished")
        with open(os.path.join(td, "pipeline_state.yaml"), "w") as f:
            f.write("current_stage: MODEL\ncurrent_round: 1\n")
        ok("MODEL" in (pipeline_unfinished(td) or ""), "CT1 MODEL stage not unfinished")
        with open(os.path.join(td, "pipeline_state.yaml"), "w") as f:
            f.write("current_stage: RUN_FAILED\ncurrent_round: 8\n")
        ok(pipeline_unfinished(td) is None, "CT1 RUN_FAILED treated as unfinished")
        with open(os.path.join(td, "pipeline_state.yaml"), "w") as f:
            f.write("current_stage: CRITIQUE\n")
        open(os.path.join(td, "report.md"), "w").write("# r\n")
        ok(pipeline_unfinished(td) is None, "CT1 report.md present treated as unfinished")
    ok("{where}" in CONTINUE_PROMPT and "run_in_background" in CONTINUE_PROMPT, "CT1 continue prompt")

    # BUF1 (Phase 21 ι): every ClaudeAgentOptions in main.py raises the
    # SDK stream buffer above the 1 MiB default.
    import ast as _ast
    _calls = [n for n in _ast.walk(_ast.parse(open(os.path.abspath(__file__)).read()))
              if isinstance(n, _ast.Call) and getattr(n.func, "id", "") == "ClaudeAgentOptions"]
    ok(_calls and all(any(k.arg == "max_buffer_size" for k in c.keywords) for c in _calls),
       "BUF1 a ClaudeAgentOptions(...) call lacks max_buffer_size")
    ok(all(any(k.arg == "settings" for k in c.keywords) for c in _calls if any(k.arg == "env" for k in c.keywords)),
       "BUF1b (Phase 31) a pipeline ClaudeAgentOptions(...) call lacks settings=SESSION_SETTINGS (idle compaction off)")
    ok(enforcement.MAX_BUFFER_SIZE > 1024 * 1024, "BUF1 buffer not raised")

    # WD1 (Phase 25): the bundled CLI refuses Write by a subagent to
    # REPORT*/SUMMARY*/FINDINGS*/ANALYSIS*.md. E7 lost two full runs to it
    # (the writer was told to write report.md). No agent prompt may instruct
    # a Write to such a name.
    _blocked = re.compile(r"(?i)\b(?:write|writes|save|emit|edit|add|insert|append|update|put|create)\b"
                          r"[^\n]*?\b((?:report|summary|findings|analysis)[\w.-]*\.md)\b")
    from agents import build_agents as _ba
    _bad = []
    for _name, _d in _ba(question="malaria smoke", run_dir="runs/x").items():
        for _line in (_d.prompt or "").splitlines():
            _m = _blocked.search(_line)
            if _m and not re.search(r"(?i)\bnever\b|\bnot\b", _line):
                _bad.append(f"{_name}: {_line.strip()[:90]}")
    ok(not _bad, f"WD1 a subagent is told to Write a file the CLI blocks: {_bad[:3]}")
    ok(evaluate_preflight(good_pf := f"PREFLIGHT_OK {sys.executable}\nPREFLIGHT_OK {sys.executable}",
                          {enforcement.MODEL_DEEP: {}}, False, subagent_wrote=False),
       "WD2 preflight must fail when the subagent cannot Write the writer's draft")
    ok(any("pinned" in p for p in evaluate_preflight(good_pf, {enforcement.MODEL_DEEP: {}}, False,
                                                    sdk_version="0.2.174", sdk_pinned="0.2.169")),
       "WD4 preflight must fail when the installed SDK differs from the pinned one")
    ok(pinned_sdk_version() is not None, "WD4 requirements.txt pins claude-agent-sdk")
    # ST1 (Phase 26): a finished run uploads itself (never _truth/), when configured.
    class _FB:
        def __init__(self, st, k): self.st, self.k = st, k
        def upload_blob(self, data, overwrite=False): self.st[self.k] = bytes(data)
        def download_blob(self):
            v = self.st[self.k]
            return type("D", (), {"readall": lambda _s: v})()

    class _FS:
        def __init__(self): self.st = {}
        def get_blob_client(self, c, n): return _FB(self.st, (c, n))
    _sr = tempfile.mkdtemp()
    _run = os.path.join(_sr, "2026-10-24_000000_x")
    os.makedirs(os.path.join(_run, "_truth"))
    open(os.path.join(_run, "report.md"), "w").write("r")
    open(os.path.join(_run, "_truth", "answers.yaml"), "w").write("secret")
    open(os.path.join(_run, ".env.local"), "w").write("TOKEN=x")
    open(os.path.join(_run, "trace.jsonl"), "w").write("key sk-ant-TESTSECRET12345 leaked\n")
    _saved_key = os.environ.get("ANTHROPIC_API_KEY")
    os.environ["ANTHROPIC_API_KEY"] = "sk-ant-TESTSECRET12345"
    _fs = _FS()
    _blob = store_run_results(_run, service=_fs)
    import io as _io2, tarfile as _tf2
    _names = _tf2.open(fileobj=_io2.BytesIO(_fs.st.get(("outputs", "runs/2026-10-24_000000_x.tar.gz"), b"")),
                       mode="r:gz").getnames() if _blob else []
    if _saved_key is None:
        os.environ.pop("ANTHROPIC_API_KEY", None)
    else:
        os.environ["ANTHROPIC_API_KEY"] = _saved_key
    _tr = _tf2.open(fileobj=_io2.BytesIO(_fs.st.get(("outputs", "runs/2026-10-24_000000_x.tar.gz"), b"")), mode="r:gz") \
        if _blob else None
    _trace = _tr.extractfile("2026-10-24_000000_x/trace.jsonl").read() if _tr else b""
    ok(_blob and any(n.endswith("report.md") for n in _names) and not any("_truth" in n or ".env" in n for n in _names)
       and b"TESTSECRET" not in _trace,
       f"ST1 run stored without _truth/ or .env*, API keys redacted: {_names}")
    ok(json.load(open(os.path.join(_run, "metadata.json"))).get("stored") == _blob,
       "ST1 metadata.json records where the run was stored")
    ok(store_run_results(_run) is None or os.environ.get("MA_STORAGE_ACCOUNT"),
       "ST1 nothing is stored when no account is configured")
    shutil.rmtree(_sr, ignore_errors=True)

    # WU (Phase 29): every run ends with a report.
    ok(writeup_reserve(450.0) == 36.0 and writeup_reserve(8.0) == 4.0 and writeup_reserve(None) == 0.0,
       "WU1 write-up reserve: 8% (min $10, at most half the cap)")
    _wd = tempfile.mkdtemp()
    os.makedirs(os.path.join(_wd, "models"))
    json.dump({"question": "q"}, open(os.path.join(_wd, "metadata.json"), "w"))
    _seen = {}

    async def _fq_w(prompt, options):
        _seen["prompt"], _seen["budget"], _seen["agents"] = prompt, options.max_budget_usd, sorted(options.agents)
        open(os.path.join(_wd, WRITER_DRAFT), "w").write("# Status: not accepted\n")
        yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                            num_turns=2, session_id="w1", total_cost_usd=3.5)
    _bud = {"sessions": [{"session": 1, "total_cost_usd": 420.0}]}
    _info = asyncio.run(final_writeup(_wd, _wd, "q", 8, _bud, 450.0, 2, "max_sessions_reached", query_fn=_fq_w))
    ok(_info.get("ran") and _info.get("manuscript") and _seen.get("agents") == ["writer"]
       and abs((_seen.get("budget") or 0) - 30.0) < 1e-6 and "Status: not accepted" in _seen.get("prompt", ""),
       f"WU2 the write-up spawns only the writer, uses what is left (incl. the reserve), states the verdict: {_info} {_seen.get('budget')}")
    ok(_bud["sessions"][-1]["source"] == "final write-up" and _bud["sessions"][-1]["total_cost_usd"] == 3.5,
       f"WU3 the write-up is charged to the ledger: {_bud['sessions'][-1]}")
    ok(json.load(open(os.path.join(_wd, "metadata.json"))).get("final_writeup", {}).get("ran") is True,
       "WU4 the write-up outcome is recorded in metadata.json")
    _bud2 = {"sessions": [{"session": 1, "total_cost_usd": 450.0}]}
    ok(asyncio.run(final_writeup(_wd, _wd, "q", 8, _bud2, 450.0, 2, "budget_exhausted", query_fn=_fq_w))
       .get("skipped"), "WU5 no budget left: the write-up is skipped and says so")
    shutil.rmtree(_wd, ignore_errors=True)

    # WU6-WU9 (Codex, Phase 29 review)
    _bd = tempfile.mkdtemp()
    open(os.path.join(_bd, "report.md"), "w").write("# Report\nWe recommend X.\n")
    ok(add_verdict_banner(_bd, {"action": "RUN_FAILED", "unresolved_high": [1, 2]})
       and open(os.path.join(_bd, "report.md")).read().startswith(VERDICT_BANNER_MARK)
       and not add_verdict_banner(_bd, {"action": "RUN_FAILED"}),
       "WU6 a report written before a rejection gets the verdict banner once")
    ok(not add_verdict_banner(_bd, {"action": "ACCEPT"}), "WU6 an accepted report is untouched")
    shutil.rmtree(_bd, ignore_errors=True)
    _cd = tempfile.mkdtemp()
    json.dump({"question": "q"}, open(os.path.join(_cd, "metadata.json"), "w"))

    async def _fq_crash(prompt, options):
        if options.resume:          # the read-back
            yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                                num_turns=1, session_id="w9", total_cost_usd=20.0)
            return
        yield SystemMessage(subtype="init", data={"session_id": "w9"})
        raise RuntimeError("Command failed with exit code -9")
    _bud3 = {"sessions": [{"session": 1, "total_cost_usd": 414.0}]}
    _i = asyncio.run(final_writeup(_cd, _cd, "q", 8, _bud3, 450.0, 2, "max_sessions_reached",
                                   query_fn=_fq_crash))
    ok(_bud3["sessions"][-1]["total_cost_usd"] == 20.0 and "crashed" in _bud3["sessions"][-1]["source"]
       and _i.get("writer_error"), f"WU7 a crashed write-up is charged (read back), not skipped: {_bud3['sessions'][-1]}")

    class _Ig:
        def __init__(self): self.w = set()
        def harness_wrote(self, rel): self.w.add(rel)
    _ig = _Ig()
    _harness_wrote_gate_outputs(_ig)
    ok({"validator_history.jsonl", "metadata.json", "final_gate.json"} <= _ig.w,
       f"WU8 the write-up's gate and metadata writes are registered as harness writes: {_ig.w}")
    shutil.rmtree(_cd, ignore_errors=True)

    # OB (Phase 29): live status, resources, checkpoints.
    from agents import observability as _ob
    for _f in _ob.self_test():
        ok(False, _f)

    # WD5 (Codex, PR #21): a render that times out still ends with report.md.
    _rd5 = tempfile.mkdtemp()
    open(os.path.join(_rd5, WRITER_DRAFT), "w").write("# Draft\n")
    _real_run = subprocess.run

    def _timeout_run(*a, **k):
        raise subprocess.TimeoutExpired(a[0] if a else "render", 600)
    subprocess.run = _timeout_run
    try:
        ok(restore_report_if_missing(_rd5) and os.path.exists(os.path.join(_rd5, "report.md")),
           "WD5 a render timeout must still leave report.md (copied draft)")
    except subprocess.TimeoutExpired:
        ok(False, "WD5 a render timeout escaped finalize")
    finally:
        subprocess.run = _real_run
        shutil.rmtree(_rd5, ignore_errors=True)

    # WD6 (Codex, PR #21): the write probe passes only on a SUCCESSFUL Write
    # issued inside the subagent, never on a refused one plus a lead Write.
    from claude_agent_sdk import ToolUseBlock as _TU, ToolResultBlock as _TR

    def _fake_query(child_error, lead_writes):
        async def q(prompt, options):
            path = re.search(r"file path: (\S+?)\. ", prompt).group(1)
            yield AssistantMessage(content=[_TU("w1", "Write", {"file_path": path, "content": "PREFLIGHT_WRITE"})],
                                   model="m", parent_tool_use_id="a1")
            if not child_error:
                open(path, "w").write("PREFLIGHT_WRITE")
            yield UserMessage(content=[_TR("w1", "refused" if child_error else "ok", is_error=child_error)],
                              parent_tool_use_id="a1")
            if lead_writes:
                yield AssistantMessage(content=[_TU("w2", "Write", {"file_path": path, "content": "PREFLIGHT_WRITE"})],
                                       model="m")
                open(path, "w").write("PREFLIGHT_WRITE")
                yield UserMessage(content=[_TR("w2", "ok", is_error=False)])
            yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                                num_turns=1, session_id="s", total_cost_usd=0.0)
        return q
    _real_query = globals()["query"]
    try:
        globals()["query"] = _fake_query(child_error=True, lead_writes=True)
        _w, _log, _ = asyncio.run(_probe_subagent_write(False))
        ok(not _w, "WD6 a refused subagent Write followed by the lead's Write must fail the probe")
        globals()["query"] = _fake_query(child_error=False, lead_writes=False)
        _w, _log, _ = asyncio.run(_probe_subagent_write(False))
        ok(_w, f"WD6 a successful subagent Write must pass the probe: {_log[-200:]}")
    finally:
        globals()["query"] = _real_query

    _rd = tempfile.mkdtemp()
    open(os.path.join(_rd, WRITER_DRAFT), "w").write("# Draft\n\nNo references.\n")
    ok(restore_report_if_missing(_rd) and os.path.exists(os.path.join(_rd, "report.md")),
       "WD3 finalize builds report.md from manuscript.md when the render step never ran")
    shutil.rmtree(_rd, ignore_errors=True)

    # PF1 (Phase 21 α): preflight evaluation.
    exe = sys.executable
    good = f"PREFLIGHT_OK {exe}\nPREFLIGHT_OK {exe}"
    ok(evaluate_preflight(good, {enforcement.MODEL_DEEP: {}}, False) == [],
       "PF1 good preflight flagged")
    ok(any("4.x" in p or "unexpected models" in p for p in
           evaluate_preflight(good, {"claude-opus-4-6": {}}, False)),
       "PF1 old model not flagged")
    ok(evaluate_preflight(f"PREFLIGHT_OK {exe}", {enforcement.MODEL_DEEP: {}}, False),
       "PF1 missing subagent probe not flagged")
    ok(evaluate_preflight("PREFLIGHT_OK /usr/bin/python3\nPREFLIGHT_OK /usr/bin/python3",
                          {enforcement.MODEL_DEEP: {}}, False),
       "PF1 system python not flagged")
    ok(evaluate_preflight(good, {}, False), "PF1 empty model_usage not flagged")
    ok(evaluate_preflight(good + "\nPREFLIGHT_OK line.", {enforcement.MODEL_DEEP: {}}, False) == [],
       "PF1 prose 'PREFLIGHT_OK line' wrongly counted as an interpreter")
    ok(evaluate_preflight(good, {enforcement.MODEL_DEEP: {}, "claude-haiku-5-5": {}}, False) == [],
       "PF1 CLI-internal haiku wrongly flagged")

    if failures:
        print("SELF-TEST FAILURES:", file=sys.stderr)
        for f_ in failures:
            print(f"  - {f_}", file=sys.stderr)
        return 1
    print("OK: all self-test cases passed.")
    return 0


# Phase 21 α: preflight. Every self-test mocks the SDK, so nothing
# exercised the real CLI / model / PATH until a multi-hour run was already
# spending money. 2026-10-20_150152 ran six hours on 4.x-era models (the
# bundled CLI's alias table) with a system-Python PATH that made the
# modeler give up on the requested framework. This ~1-minute check runs a
# real lead + a real registered-style subagent and verifies both.
PREFLIGHT_IMPORTS = ("laser.core", "laser.generic", "starsim", "yaml",
                     "numpy", "scipy")


WRITER_DRAFT = "manuscript.md"   # agents/writer.py writes this; render_claims.py builds report.md


def preflight_command() -> str:
    mods = ", ".join(PREFLIGHT_IMPORTS)
    return (f"python3 -c \"import sys, {mods}; "
            f"print('PREFLIGHT_OK', sys.executable)\"")


def pinned_sdk_version() -> str | None:
    """claude-agent-sdk version pinned in requirements.txt."""
    try:
        for line in open(os.path.join(REPO_ROOT, "requirements.txt")):
            m = re.match(r"\s*claude-agent-sdk==([\w.]+)", line)
            if m:
                return m.group(1)
    except OSError:
        pass
    return None


def evaluate_preflight(text: str, model_usage: dict | None,
                       is_error: bool, subagent_wrote: bool | None = None,
                       sdk_version: str | None = None, sdk_pinned: str | None = None) -> list[str]:
    """Return problems found (empty list = pass). Pure, so it is self-tested.
    subagent_wrote: whether the probe subagent's Write of the writer's draft
    file name landed on disk (Phase 25; None = not checked)."""
    problems = []
    if sdk_pinned and sdk_version and sdk_version != sdk_pinned:
        # The bundled CLI changes behaviour between versions (Phase 25: a
        # subagent Write rule cost two full E7 runs); test what is pinned.
        problems.append(f"claude-agent-sdk {sdk_version} installed, {sdk_pinned} pinned in requirements.txt")
    if subagent_wrote is False:
        problems.append(f"a subagent could not Write {WRITER_DRAFT} (the writer's draft); "
                        f"the WRITE stage would fail")
    if is_error:
        problems.append("preflight session ended with an error result")
    # Absolute paths only: the model's prose can say "PREFLIGHT_OK line"
    # (seen on Coiled 2026-10-21), which must not count as an interpreter.
    hits = re.findall(r"PREFLIGHT_OK\s+(/\S+)", text or "")
    want = os.path.realpath(sys.executable)
    if len(hits) < 2:
        problems.append(
            f"expected the import probe to succeed in both the lead and the "
            f"subagent (saw {len(hits)} PREFLIGHT_OK line(s)); a framework "
            f"import failed or python3 is not the harness interpreter")
    bad = sorted({h for h in hits if os.path.realpath(h) != want})
    if bad:
        problems.append(f"agents' python3 is {bad}, not the harness interpreter {want}")
    used = set((model_usage or {}).keys())
    if not used:
        problems.append("ResultMessage.model_usage is empty; cannot verify models")
    else:
        # The CLI itself uses a small Haiku model for internal chores
        # (Bash command summaries, titles); no pipeline agent runs on it.
        unexpected = sorted(m for m in used
                            if not m.startswith((enforcement.MODEL_DEEP,
                                                 enforcement.MODEL_FAST,
                                                 "claude-haiku-")))
        if unexpected:
            problems.append(f"unexpected models used: {unexpected} "
                            f"(pinned: {enforcement.MODEL_DEEP}, {enforcement.MODEL_FAST})")
        if not any(m.startswith(enforcement.MODEL_DEEP) for m in used):
            problems.append(f"{enforcement.MODEL_DEEP} was not used")
    return problems


async def run_preflight(allow_cloud: bool = False) -> dict:
    from claude_agent_sdk import AgentDefinition
    cmd = preflight_command()
    probe = AgentDefinition(
        description="Preflight probe: runs one shell command and reports its output.",
        prompt="Run exactly the shell command you are given with Bash and "
               "reply with its full output verbatim. Do nothing else.",
        tools=["Bash"], model=enforcement.MODEL_DEEP, maxTurns=4)
    options = ClaudeAgentOptions(
        system_prompt="You are a harness preflight check. Follow instructions literally.",
        model=enforcement.MODEL_DEEP,
        max_buffer_size=enforcement.MAX_BUFFER_SIZE, settings=enforcement.SESSION_SETTINGS,
        agents={"preflight-probe": probe},
        tools=["Bash", "Agent"], allowed_tools=["Bash", "Agent"],
        permission_mode="bypassPermissions", setting_sources=[],
        cwd=REPO_ROOT, env=enforcement.filtered_env(dict(os.environ), allow_cloud),
        max_turns=8)
    prompt = (f"1. Run this with Bash and copy its output verbatim:\n{cmd}\n"
              f"2. Spawn the `preflight-probe` agent and give it exactly the same "
              f"command. "
              f"3. Reply with both outputs verbatim, one per line.")
    texts, result = [], None
    async for m in query(prompt=prompt, options=options):
        if isinstance(m, AssistantMessage):
            for b in m.content:
                if hasattr(b, "text"):
                    texts.append(b.text)
        elif isinstance(m, UserMessage) and isinstance(m.content, list):
            # The probe's real Bash output (tool results), not a paraphrase.
            for b in m.content:
                c = getattr(b, "content", None)
                if isinstance(c, str):
                    texts.append(c)
                elif isinstance(c, list):
                    texts.extend(x.get("text", "") for x in c if isinstance(x, dict))
        elif isinstance(m, ResultMessage):
            result = m
            if getattr(m, "result", None):
                texts.append(m.result)
    usage = getattr(result, "model_usage", None) if result else None
    wrote, write_log, write_cost = await _probe_subagent_write(allow_cloud)
    problems = evaluate_preflight("\n".join(texts), usage,
                                  bool(result is None or result.is_error), subagent_wrote=wrote,
                                  sdk_version=_sdk_version(), sdk_pinned=pinned_sdk_version())
    cost = getattr(result, "total_cost_usd", None) if result else None
    return {"ok": not problems, "problems": problems,
            "model_usage": usage,
            "cost_usd": (cost or 0) + (write_cost or 0),
            "sdk_version": _sdk_version(),
            "write_probe": None if wrote else write_log[-1500:]}


async def _probe_subagent_write(allow_cloud: bool, attempts: int = 2) -> tuple[bool, str, float]:
    """A subagent must be able to Write the writer's draft file name with the
    Write tool, as the writer does (Phase 25: the bundled CLI blocks some
    names for subagents). One job per probe, retried once, so a model that
    skips the step is not mistaken for a block; the tool results are kept
    for the log."""
    import tempfile
    from claude_agent_sdk import AgentDefinition
    log, cost = [], 0.0
    for attempt in range(attempts):
        wdir = tempfile.mkdtemp(prefix="ma_preflight_")
        wpath = os.path.join(wdir, WRITER_DRAFT)
        probe = AgentDefinition(
            description="Preflight write probe: saves one file with the Write tool.",
            prompt="Use the Write tool to save the exact text PREFLIGHT_WRITE to the file "
                   "path you are given. Then reply with the tool's result verbatim.",
            tools=["Write"], model=enforcement.MODEL_DEEP, maxTurns=3)
        options = ClaudeAgentOptions(
            system_prompt="You are a harness preflight check. Follow instructions literally.",
            model=enforcement.MODEL_DEEP, max_buffer_size=enforcement.MAX_BUFFER_SIZE, settings=enforcement.SESSION_SETTINGS,
            # The lead's real tool list: a subagent only gets tools its parent has.
            agents={"write-probe": probe}, tools=LEAD_TOOLS, allowed_tools=LEAD_TOOLS,
            permission_mode="bypassPermissions", setting_sources=[], cwd=REPO_ROOT,
            env=enforcement.filtered_env(dict(os.environ), allow_cloud), max_turns=4)
        by_subagent, sub_ids, lead_wrote, write_ok = False, set(), False, False
        async for m in query(prompt=f"Spawn the `write-probe` agent and give it this file path: {wpath}. "
                                    f"Do not write anything yourself. Reply with what it reported.",
                             options=options):
            if isinstance(m, AssistantMessage):
                for b in m.content:
                    if getattr(b, "name", None) != "Write":
                        continue
                    if getattr(m, "parent_tool_use_id", None):
                        # A Write issued inside the subagent...
                        if os.path.realpath(str((getattr(b, "input", None) or {}).get("file_path", ""))) \
                                == os.path.realpath(wpath):
                            by_subagent = True
                            sub_ids.add(getattr(b, "id", None))
                    else:
                        lead_wrote = True        # ...not the lead's own
            if isinstance(m, UserMessage) and isinstance(m.content, list):
                for b in m.content:
                    # ...and its result must not be an error (a refused Write).
                    if getattr(b, "tool_use_id", None) in sub_ids and not getattr(b, "is_error", False):
                        write_ok = True
                for b in m.content:
                    c = getattr(b, "content", None)
                    log.append(c if isinstance(c, str) else json.dumps(c, default=str)[:500])
            elif isinstance(m, ResultMessage):
                cost += m.total_cost_usd or 0
                log.append(f"[attempt {attempt + 1}] result: {(m.result or '')[:300]}")
        wrote = (by_subagent and write_ok and not lead_wrote
                 and os.path.exists(wpath) and "PREFLIGHT_WRITE" in open(wpath).read())
        if lead_wrote:
            log.append(f"[attempt {attempt + 1}] the lead used Write itself; not a subagent test")
        if not by_subagent and os.path.exists(wpath):
            log.append(f"[attempt {attempt + 1}] the file was written, but not by the subagent")
        shutil.rmtree(wdir, ignore_errors=True)
        if wrote:
            return True, "\n".join(log), cost
    return False, "\n".join(log), cost


WRITEUP_PROMPT = """The modeling sessions have ended (outcome: {outcome}) without a report.
Spawn the `writer` agent exactly once for run directory {run_dir}. Tell it:

- This is the run's final write-up. The final gate's verdict is in
  {run_dir}/final_gate.json (action: {action}; unresolved blocking findings:
  {n_high}). If the action is not ACCEPT, the report's first section is
  "Status: not accepted by the final gate": the verdict and each unresolved
  blocking finding in plain words. The model is then presented as what was
  built and where it fell short, never as a recommendation; any allocation
  is labelled illustrative.
- Write the draft to {run_dir}/manuscript.md from the artifacts that exist
  (plan, data, models, results, critiques). Missing pieces are stated as
  missing, not invented.

Do nothing else; the harness renders report.md afterwards."""


def _harness_wrote_gate_outputs(integrity) -> None:
    """The validator (run by the harness) rewrites its own outputs: record them
    as harness writes so they are not flagged as tampering (Codex, Phase 29)."""
    if integrity is None:
        return
    from agents.integrity import SCRIPT_PRODUCES
    for rel in set(SCRIPT_PRODUCES.get("validate_critique_yaml.py", ())) | {
            "final_gate.json", "final_gate_stderr.txt", "metadata.json", BUDGET_LEDGER}:
        integrity.harness_wrote(rel)


async def final_writeup(run_path, run_dir_rel, question, max_rounds, budget, cap, session_num,
                        outcome, integrity=None, query_fn=None, live_state=None) -> dict:
    """Phase 29: when the modeling sessions end without a report (final-round
    RUN_FAILED, budget, sessions, crash), run the final gate and one writer
    session paid from the write-up reserve, so every run leaves a report that
    states its verdict. Never raises into finalize."""
    info = {"ran": False}
    try:
        gate = run_final_gate(run_path, max_rounds) or {}
        _harness_wrote_gate_outputs(integrity)
        n_high = len(gate.get("unresolved_high") or []) if isinstance(gate.get("unresolved_high"), list) \
            else gate.get("n_unresolved_high")
        left = remaining_budget(cap, budget["sessions"])
        if left is not None and left <= 0:
            info.update(skipped="no budget left for the write-up")
            return info
        agents = {"writer": build_agents(question=question, run_dir=run_dir_rel)["writer"]}
        trace = open(os.path.join(run_path, "trace.jsonl"), "a")
        hooks = create_hooks(run_path, trace, datetime.now(), registered_agents={"writer"},
                             max_turns={"writer": agents["writer"].maxTurns}, integrity=integrity,
                             hold_writer=False)   # the harness's own write-up is never held
        options = ClaudeAgentOptions(
            system_prompt="You are the harness's final write-up step. Follow the instructions literally.",
            agents=agents, model=(smoke_settings() or {}).get("model", enforcement.MODEL_DEEP),
            max_buffer_size=enforcement.MAX_BUFFER_SIZE, settings=enforcement.SESSION_SETTINGS, tools=LEAD_TOOLS, allowed_tools=LEAD_TOOLS,
            permission_mode="bypassPermissions", setting_sources=["project"], hooks=hooks,
            cwd=REPO_ROOT, env=enforcement.filtered_env(dict(os.environ), False), max_turns=8)
        if left is not None:
            options.max_budget_usd = left
        prompt = WRITEUP_PROMPT.format(outcome=outcome, run_dir=run_dir_rel,
                                       action=gate.get("action") or gate.get("error") or "not run",
                                       n_high=n_high if n_high is not None else "unknown")
        print(f"\nFINAL WRITE-UP (gate: {gate.get('action')}; budget left ${left})", flush=True)
        result, sid, err = None, None, None
        try:
            async for m in (query_fn or query)(prompt=prompt, options=options):
                log_message(m, trace)
                if live_state is not None:
                    live_state.on_message(m)         # stage snapshot of the writer, live status
                if isinstance(m, SystemMessage) and m.subtype == "init":
                    sid = (m.data or {}).get("session_id") or sid
                elif isinstance(m, ResultMessage):
                    result, sid = m, m.session_id or sid
        except Exception as e:  # noqa: BLE001 — still charged below, then recorded
            err = f"{type(e).__name__}: {e}"[:300]
        source = "final write-up"
        if result is None and sid:
            result = await reconcile_conversation_cost(options, sid, query_fn=query_fn)
            source = "final write-up (crashed; cost read back)"
        # unreadable cost: the whole remaining allowance (cap holds)
        charge_session(budget, session_num, result, left, session_id=sid, elapsed_s=None, source=source)
        write_budget_ledger(run_path, budget, cap, integrity)
        if err:
            info["writer_error"] = err
        info.update(ran=True, gate_action=gate.get("action"),
                    manuscript=os.path.exists(os.path.join(run_path, "manuscript.md")))
    except Exception as e:  # noqa: BLE001 — finalize still runs; the gap is recorded
        info.update(error=f"{type(e).__name__}: {e}"[:300])
        print(f"[ERROR] final write-up failed: {info['error']}", flush=True)
    try:
        mp = os.path.join(run_path, "metadata.json")
        meta = json.load(open(mp))
        meta["final_writeup"] = info
        json.dump(meta, open(mp, "w"), indent=2)
        _harness_wrote_gate_outputs(integrity)
    except (OSError, ValueError):
        pass
    return info


def _sdk_version() -> str | None:
    try:
        from importlib.metadata import version
        return version("claude-agent-sdk")
    except Exception:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build a mathematical model for a research question"
    )
    parser.add_argument("question", nargs="?", help="The research question to model")
    parser.add_argument("--max-rounds", type=int, default=5,
                        help="Max critique-revision rounds (default: 5)")
    parser.add_argument("--max-sessions", type=int, default=10,
                        help="Max sessions for context recovery (default: 10)")
    parser.add_argument("--resume", type=str, default=None,
                        help="Resume an existing run directory (e.g., runs/2026-04-07_1523_...)")
    parser.add_argument("--cloud", action="store_true",
                        help="Pass cloud credentials (AZ_*/AZURE_*) to agents so UQ "
                             "can run on Azure Batch. Off by default (Phase 21 α).")
    parser.add_argument("--cross-model", action="store_true",
                        help="Add the gpt-6.1-sol cross-model critic (Codex CLI) to "
                             "every STAGE 6. Sends run artifacts to OpenAI.")
    parser.add_argument("--max-budget-usd", type=float, default=None,
                        help="Hard dollar cap for the whole run (all sessions, including "
                             "earlier invocations of a resumed run). Stops with status "
                             "budget_exhausted; the final gate still runs.")
    parser.add_argument("--methods-critic", action="store_true",
                        help="Add critique-methods to every STAGE 6. Off by default: in E5 "
                             "it caught no planted defect the other critics missed.")
    parser.add_argument("--signoff", choices=["owner", "lead", "none"], default="owner",
                        help="Who signs the decision brief before compute (Phase 33): the owner "
                             "(default; the run waits), the lead (unattended test runs), or none")
    parser.add_argument("--signoff-hours", type=float, default=24.0,
                        help="How long an owner sign-off wait lasts before the run pauses (resumable)")
    parser.add_argument("--self-test", action="store_true",
                        help="Run inline self-tests (no API calls) and exit")
    parser.add_argument("--preflight-only", action="store_true",
                        help="Run the ~1-minute live preflight (real CLI, models, "
                             "PATH, framework imports) and exit")
    parser.add_argument("--skip-preflight", action="store_true",
                        help="Skip the live preflight before a run (not recommended)")
    args = parser.parse_args()

    if args.self_test:
        return _run_self_test()
    if args.preflight_only or not args.skip_preflight:
        pf = asyncio.run(run_preflight(allow_cloud=args.cloud))
        print(f"Preflight: {'OK' if pf['ok'] else 'FAILED'} | models="
              f"{sorted((pf.get('model_usage') or {}).keys())} | sdk={pf['sdk_version']}"
              f" | cost=${pf.get('cost_usd') or 0:.3f}", flush=True)
        for p in pf["problems"]:
            print(f"  PREFLIGHT PROBLEM: {p}", flush=True)
        if pf.get("write_probe"):
            print("  write probe log (last lines):\n    " + pf["write_probe"].replace("\n", "\n    "), flush=True)
        if args.cross_model:
            cm = subprocess.run(["codex", "login", "status"], capture_output=True, text=True,
                                env={**os.environ, **enforcement.filtered_env(dict(os.environ), args.cloud)})
            cm_ok = cm.returncode == 0 and "logged in" in (cm.stdout + cm.stderr).lower()
            print(f"  cross-model: codex {'OK' if cm_ok else 'NOT AVAILABLE'}"
                  f" ({(cm.stdout + cm.stderr).strip()[:80]})", flush=True)
            pf["ok"] = pf["ok"] and cm_ok
        if args.preflight_only or not pf["ok"]:
            return 0 if pf["ok"] else 3
    if args.max_sessions < 1 or args.max_rounds < 1:
        parser.error("--max-sessions and --max-rounds must be >= 1")

    if args.resume:
        run_path = args.resume if os.path.isabs(args.resume) else os.path.join(os.getcwd(), args.resume)
        if not os.path.isdir(run_path):
            print(f"Error: {run_path} not found")
            return 1

        meta_path = os.path.join(run_path, "metadata.json")
        question = args.question
        if not question and os.path.exists(meta_path):
            with open(meta_path) as f:
                meta = json.load(f)
            question = meta.get("question", "")

        if not question:
            print("Error: no question found in metadata and none provided")
            return 1

        run_dir = os.path.basename(run_path)
        coro = run(question, args.max_rounds, args.max_sessions,
                   run_dir=run_dir, run_path=run_path, allow_cloud=args.cloud,
                   cross_model=args.cross_model, methods_critic=args.methods_critic,
                   max_budget_usd=args.max_budget_usd,
                   signoff=args.signoff, signoff_hours=args.signoff_hours)
    else:
        if not args.question:
            print("Error: question is required (or use --resume)")
            return 1
        coro = run(args.question, args.max_rounds, args.max_sessions,
                   allow_cloud=args.cloud, cross_model=args.cross_model,
                   methods_critic=args.methods_critic, max_budget_usd=args.max_budget_usd,
                   signoff=args.signoff, signoff_hours=args.signoff_hours)
    try:
        status = asyncio.run(coro)
    except KeyboardInterrupt:
        # metadata was already written by run()'s finally block
        return 130
    return exit_code_for(status)


if __name__ == "__main__":
    sys.exit(main())
