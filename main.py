"""Modeling agent orchestrator -- single lead session with parallel subagents."""

import asyncio
import argparse
import json
import os
import re
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

from claude_agent_sdk import (
    query,
    ClaudeAgentOptions,
    AssistantMessage,
    ResultMessage,
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


def slugify(text: str, max_len: int = 40) -> str:
    slug = text.lower().strip()
    slug = re.sub(r'[^\w\s-]', '', slug)
    slug = re.sub(r'[\s_]+', '-', slug)
    return slug[:max_len].rstrip('-')


def create_run_dir(question: str) -> str:
    runs_root = os.path.join(os.getcwd(), "runs")
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
        print(f"\n[result] {message.subtype} | {message.duration_ms/1000:.0f}s", flush=True)
    trace_file.flush()


async def run(question: str, max_rounds: int, max_sessions: int,
              run_dir: str | None = None, run_path: str | None = None) -> None:
    """Run the modeling pipeline as a single lead agent session."""

    # Create or reuse run directory
    if run_dir is None:
        run_dir = create_run_dir(question)
        run_path = os.path.join(os.getcwd(), "runs", run_dir)
    run_dir_rel = f"runs/{run_dir}"

    print(f"Run: {run_dir_rel}/", flush=True)
    print(f"Question: {question}", flush=True)
    print(f"Max rounds: {max_rounds} | Max sessions: {max_sessions}", flush=True)

    run_start = datetime.now()
    trace_path = os.path.join(run_path, "trace.jsonl")

    # Build the agent registry and lead prompt
    agents = build_agents()
    resume_context = build_resume_context(run_path)
    lead_system = LEAD_SYSTEM_PROMPT.replace("{run_dir}", run_dir_rel).replace(
        "{max_rounds}", str(max_rounds)
    )
    lead_prompt = build_lead_prompt(question, run_dir_rel, max_rounds, resume_context)

    session_id = None

    # Phase 11 Commit η (F1): discriminate terminal states so external
    # orchestrators (cron / watchdog / scheduler) checking metadata can
    # tell completion apart from interruption / policy-block / max-
    # sessions exhaustion. Default to "unknown_error" — the for-loop
    # only exits via one of the explicit `break` branches below; if it
    # ever falls through without hitting a branch, this default at
    # least flags the bug instead of silently claiming success.
    terminal_status = "unknown_error"

    for session_num in range(1, max_sessions + 1):
        print(f"\n{'#'*60}", flush=True)
        print(f"SESSION {session_num}/{max_sessions}", flush=True)
        print(f"{'#'*60}", flush=True)

        trace_file = open(trace_path, "a")
        trace_file.write(json.dumps({
            "ts": datetime.now().isoformat(),
            "type": "session_start",
            "session": session_num,
        }) + "\n")
        # Create hooks per session so the hook closure captures the current
        # (freshly opened) trace_file. Previously hooks captured a file opened
        # once before the loop; after a session crash that fd was closed and
        # subsequent sessions' tool_use / subagent_start/stop events silently
        # failed to write.
        hooks = create_hooks(run_path, trace_file, run_start)

        # On resume sessions, use the saved session_id if available
        options = ClaudeAgentOptions(
            system_prompt=lead_system,
            agents=agents,
            allowed_tools=["Agent", "Bash", "Read", "Write", "Glob", "Grep"],
            permission_mode="bypassPermissions",
            setting_sources=["project"],
            hooks=hooks,
        )
        if session_id and session_num > 1:
            options.resume = session_id

        try:
            cleanup_orphaned_claude_processes()

            async for message in query(prompt=lead_prompt, options=options):
                log_message(message, trace_file)
                if isinstance(message, ResultMessage):
                    session_id = getattr(message, "session_id", None)

            trace_file.close()
            terminal_status = "completed"
            break  # Pipeline completed successfully

        except KeyboardInterrupt:
            print("\nInterrupted.", flush=True)
            trace_file.close()
            terminal_status = "interrupted"
            break

        except Exception as e:
            error_str = str(e)
            print(f"\nSession {session_num} error: {error_str}", flush=True)
            trace_file.write(json.dumps({
                "ts": datetime.now().isoformat(),
                "type": "session_error",
                "error": error_str,
            }) + "\n")
            trace_file.close()

            # Don't retry policy blocks
            if "Usage Policy" in error_str or "violate" in error_str:
                print("Policy block detected. Try rephrasing the question.", flush=True)
                terminal_status = "policy_blocked"
                break

            if session_num >= max_sessions:
                print("Max sessions reached.", flush=True)
                terminal_status = "max_sessions_reached"
                break

            # Exponential backoff
            backoff = min(30 * (2 ** (session_num - 1)), 300)
            if "529" in error_str or "overload" in error_str.lower():
                backoff = min(60 * (2 ** (session_num - 1)), 600)
                print(f"API overloaded. Waiting {backoff}s before retry...", flush=True)
            else:
                print(f"Waiting {backoff}s before retry...", flush=True)

            import time as _backoff_time
            cleanup_orphaned_claude_processes()
            _backoff_time.sleep(backoff)

            # On retry, rebuild resume context from whatever files exist
            resume_context = build_resume_context(run_path)
            lead_prompt = build_lead_prompt(question, run_dir_rel, max_rounds, resume_context)
            print(f"Restarting pipeline (session {session_num + 1})...", flush=True)

    # Save final metadata
    elapsed = (datetime.now() - run_start).total_seconds()
    metadata_path = os.path.join(run_path, "metadata.json")
    with open(metadata_path) as f:
        meta = json.load(f)
    # Phase 11 Commit η (F1): `completed` is the timestamp the run loop
    # finished (kept for backward compat with existing readers).
    # `status` is the new authoritative discriminator: "completed" |
    # "interrupted" | "policy_blocked" | "max_sessions_reached" |
    # "unknown_error". Automation should consult `status`, not the
    # presence of `completed`.
    meta["completed"] = datetime.now().isoformat()
    meta["status"] = terminal_status
    meta["elapsed_s"] = elapsed
    meta["sessions"] = session_num
    meta["has_report"] = os.path.exists(os.path.join(run_path, "report.md"))
    if session_id:
        meta["session_id"] = session_id
    with open(metadata_path, "w") as f:
        json.dump(meta, f, indent=2)

    has_report = meta["has_report"]
    print(f"\n{'='*60}", flush=True)
    print(f"RUN {terminal_status.upper()}", flush=True)
    print(f"  Duration: {elapsed:.0f}s ({elapsed/60:.1f} min)", flush=True)
    print(f"  Sessions: {session_num}", flush=True)
    print(f"  Report: {'written' if has_report else 'not written'}", flush=True)
    print(f"  Results: {run_dir_rel}/", flush=True)
    print(f"{'='*60}", flush=True)


def main():
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
    args = parser.parse_args()

    if args.resume:
        run_path = args.resume if os.path.isabs(args.resume) else os.path.join(os.getcwd(), args.resume)
        if not os.path.isdir(run_path):
            print(f"Error: {run_path} not found")
            return

        meta_path = os.path.join(run_path, "metadata.json")
        question = args.question
        if not question and os.path.exists(meta_path):
            with open(meta_path) as f:
                meta = json.load(f)
            question = meta.get("question", "")

        if not question:
            print("Error: no question found in metadata and none provided")
            return

        run_dir = os.path.basename(run_path)
        asyncio.run(run(question, args.max_rounds, args.max_sessions,
                        run_dir=run_dir, run_path=run_path))
    else:
        if not args.question:
            print("Error: question is required (or use --resume)")
            return
        asyncio.run(run(args.question, args.max_rounds, args.max_sessions))


if __name__ == "__main__":
    main()
