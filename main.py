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
from agents import (
    build_agents,
    build_lead_prompt,
    create_hooks,
    cleanup_orphaned_claude_processes,
    LEAD_SYSTEM_PROMPT,
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
                    "completed_no_report", "run_failed", "budget_exhausted"}


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


def charge_session(ledger: dict, session: int, result, allowance: float | None) -> dict:
    """Record one session's spend. A session that ended without a cost
    (disconnect, crash) is charged its whole remaining allowance: the cap
    errs toward stopping, never toward overspending (Codex review, PR #15)."""
    cost = getattr(result, "total_cost_usd", None) if result is not None else None
    if isinstance(cost, (int, float)):
        # The SDK reports CUMULATIVE cost for a resumed conversation (same
        # session_id; verified 2026-10-23): charge only the increase.
        sid = getattr(result, "session_id", None)
        prev = max((e.get("reported_usd") or 0 for e in ledger.get("sessions", [])
                    if sid and e.get("session_id") == sid), default=0)
        entry = {"session": session, "session_id": sid, "reported_usd": round(cost, 4),
                 "total_cost_usd": round(max(cost - prev, 0), 4), "source": "result"}
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
    if os.path.exists(report_path) or not os.path.exists(snapshot_path):
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
              methods_critic: bool = False, max_budget_usd: float | None = None) -> str:
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

    harness_before, harness_prior_diff = write_harness_baseline(run_path)
    # Phase 23 α: hash protected run artifacts; every change must be explained
    # by an allowed in-flight tool call (see agents/integrity.py).
    integrity = ArtifactIntegrity(run_path)
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

    try:
        for session_num in range(1, max_sessions + 1):
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
                                 integrity=integrity)

            # Run-wide dollar cap across sessions (and earlier invocations),
            # from the harness-owned ledger.
            left = remaining_budget(max_budget_usd, budget["sessions"])
            if left is not None and left <= 0:
                print(f"Budget cap ${max_budget_usd:.0f} reached; stopping.", flush=True)
                session_outcome = "budget_exhausted"
                break
            options = ClaudeAgentOptions(
                system_prompt=lead_system,
                agents=agents,
                model=enforcement.MODEL_DEEP,
                max_buffer_size=enforcement.MAX_BUFFER_SIZE,
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
            try:
                cleanup_orphaned_claude_processes()

                async for message in query(prompt=lead_prompt, options=options):
                    log_message(message, trace_file)
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
                charge_session(budget, session_num, result, left)
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
                where = pipeline_unfinished(run_path)
                if where and session_num < max_sessions:
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

            if session_num >= max_sessions:
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
    finally:
        status = finalize_run(
            run_path, run_dir_rel, run_start, max_rounds, session_outcome,
            session_num, session_id, session_records, harness_before,
            harness_prior_diff, run_gate=not interrupted,
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
    report_restored = restore_report_if_missing(run_path)
    if report_restored and integrity is not None:
        integrity.harness_wrote("report.md")
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
    if run_gate and session_outcome in ("session_completed", "budget_exhausted"):
        print("\nRunning final STAGE 7 gate...", flush=True)
        gate = run_final_gate(run_path, max_rounds)

    harness_diff = enforcement.diff_harness(harness_before, enforcement.hash_harness())
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
    print(f"{'='*60}", flush=True)
    return status


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
        ok(harness_scripts_run(f"(cd {REPO_ROOT} && timeout 600 python3 scripts/identifiability.py runs/x)")[0]
           == ["identifiability.py"]
           and harness_scripts_run("timeout --signal=KILL 30m nice -n 5 python3 scripts/compare_models.py runs/x")[0]
           == ["compare_models.py"],
           "IC1e producers behind timeout/nice wrappers are recognized (E7 attempt 4)")
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
    ok({k: v for k, v in ov.items() if k not in ("PATH", "VIRTUAL_ENV")}
       == {"AZ_BATCH_ACCOUNT": "", "OPENAI_API_KEY": ""}, f"V1 env filter: {ov}")
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
    ok(enforcement.MAX_BUFFER_SIZE > 1024 * 1024, "BUF1 buffer not raised")

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


def preflight_command() -> str:
    mods = ", ".join(PREFLIGHT_IMPORTS)
    return (f"python3 -c \"import sys, {mods}; "
            f"print('PREFLIGHT_OK', sys.executable)\"")


def evaluate_preflight(text: str, model_usage: dict | None,
                       is_error: bool) -> list[str]:
    """Return problems found (empty list = pass). Pure, so it is self-tested."""
    problems = []
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
        max_buffer_size=enforcement.MAX_BUFFER_SIZE,
        agents={"preflight-probe": probe},
        tools=["Bash", "Agent"], allowed_tools=["Bash", "Agent"],
        permission_mode="bypassPermissions", setting_sources=[],
        cwd=REPO_ROOT, env=enforcement.filtered_env(dict(os.environ), allow_cloud),
        max_turns=8)
    prompt = (f"1. Run this with Bash and copy its output verbatim:\n{cmd}\n"
              f"2. Spawn the `preflight-probe` agent and give it exactly the same "
              f"command. 3. Reply with both outputs verbatim, one per line.")
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
    problems = evaluate_preflight("\n".join(texts), usage,
                                  bool(result is None or result.is_error))
    return {"ok": not problems, "problems": problems,
            "model_usage": usage,
            "cost_usd": getattr(result, "total_cost_usd", None) if result else None,
            "sdk_version": _sdk_version()}


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
                   max_budget_usd=args.max_budget_usd)
    else:
        if not args.question:
            print("Error: question is required (or use --resume)")
            return 1
        coro = run(args.question, args.max_rounds, args.max_sessions,
                   allow_cloud=args.cloud, cross_model=args.cross_model,
                   methods_critic=args.methods_critic, max_budget_usd=args.max_budget_usd)
    try:
        status = asyncio.run(coro)
    except KeyboardInterrupt:
        # metadata was already written by run()'s finally block
        return 130
    return exit_code_for(status)


if __name__ == "__main__":
    sys.exit(main())
