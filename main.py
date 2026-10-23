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
                    "completed_no_report", "run_failed"}


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
                     report_restored: bool, harness_diff: dict | None) -> str:
    """Single source of truth for metadata.json["status"].

    Phase 11 Commit η (F1) introduced status discrimination; Phase 21 α
    stops equating "the stream ended" with "completed". Order matters:
    tampering beats everything, then session-level failures, then the
    final gate's verdict on what was delivered.
    """
    if harness_diff and enforcement.harness_changed(harness_diff):
        return "harness_tampered"
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
              methods_critic: bool = False) -> str:
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
                                 max_turns={n: d.maxTurns for n, d in agents.items()})

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
                try:
                    trace_file.close()
                except OSError:
                    pass

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
            original_meta=prior_meta)
    return status


def finalize_run(run_path, run_dir_rel, run_start, max_rounds, session_outcome,
                 session_num, session_id, session_records, harness_before,
                 harness_prior_diff, run_gate: bool = True,
                 original_meta: dict | None = None) -> str:
    """Restore report if needed, run the final gate, check the harness
    hash, and write metadata.json. Runs in a `finally`, so it executes on
    Ctrl-C and on unexpected exceptions too."""
    original_meta = original_meta or {}
    report_restored = restore_report_if_missing(run_path)
    has_report = os.path.exists(os.path.join(run_path, "report.md"))

    gate = None
    if run_gate and session_outcome == "session_completed":
        print("\nRunning final STAGE 7 gate...", flush=True)
        gate = run_final_gate(run_path, max_rounds)

    harness_diff = enforcement.diff_harness(harness_before, enforcement.hash_harness())
    status = determine_status(session_outcome, gate, has_report,
                              report_restored, harness_diff)

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
    # The planner may add decision_year to metadata.json; it may not
    # change the question spec-compliance was checked against.
    for key in ("question", "started", "run_dir"):
        if key in original_meta and meta.get(key) != original_meta[key]:
            meta.setdefault("restored_fields", {})[key] = meta.get(key)
            meta[key] = original_meta[key]
    meta["completed"] = datetime.now().isoformat()
    meta["status"] = status
    meta["session_outcome"] = session_outcome
    meta["elapsed_s"] = elapsed
    meta["sessions"] = session_num
    meta["has_report"] = has_report
    meta["report_restored"] = report_restored
    meta["final_gate"] = gate
    if session_records:
        meta.setdefault("session_results", []).extend(session_records)
    costs = [r.get("total_cost_usd") for r in meta.get("session_results", [])
             if isinstance(r.get("total_cost_usd"), (int, float))]
    meta["total_cost_usd"] = round(sum(costs), 4) if costs else None
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
                   cross_model=args.cross_model, methods_critic=args.methods_critic)
    else:
        if not args.question:
            print("Error: question is required (or use --resume)")
            return 1
        coro = run(args.question, args.max_rounds, args.max_sessions,
                   allow_cloud=args.cloud, cross_model=args.cross_model,
                   methods_critic=args.methods_critic)
    try:
        status = asyncio.run(coro)
    except KeyboardInterrupt:
        # metadata was already written by run()'s finally block
        return 130
    return exit_code_for(status)


if __name__ == "__main__":
    sys.exit(main())
