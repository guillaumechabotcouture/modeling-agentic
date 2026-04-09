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
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    slug = slugify(question)
    run_name = f"{timestamp}_{slug}"
    run_path = os.path.join(runs_root, run_name)
    os.makedirs(run_path, exist_ok=True)
    os.makedirs(os.path.join(run_path, "data"), exist_ok=True)
    os.makedirs(os.path.join(run_path, "figures"), exist_ok=True)

    metadata = {
        "question": question,
        "started": datetime.now().isoformat(),
        "run_dir": run_name,
    }
    with open(os.path.join(run_path, "metadata.json"), "w") as f:
        json.dump(metadata, f, indent=2)

    with open(os.path.join(run_path, "progress.md"), "w") as f:
        f.write(f"# Progress\n\n## Question: {question}\n\n## Completed Stages:\n\n## Notes:\n")

    return run_name


def build_resume_context(run_path: str) -> str:
    """Check what files exist for resume context."""
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
    hooks = create_hooks(run_path, open(trace_path, "a"), run_start)

    session_id = None

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

        # On resume sessions, use the saved session_id if available
        options = ClaudeAgentOptions(
            system_prompt=lead_system,
            agents=agents,
            allowed_tools=["Agent", "Read", "Write", "Glob", "Grep"],
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
            break  # Pipeline completed successfully

        except KeyboardInterrupt:
            print("\nInterrupted.", flush=True)
            trace_file.close()
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
                break

            if session_num >= max_sessions:
                print("Max sessions reached.", flush=True)
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
    meta["completed"] = datetime.now().isoformat()
    meta["elapsed_s"] = elapsed
    meta["sessions"] = session_num
    meta["has_report"] = os.path.exists(os.path.join(run_path, "report.md"))
    if session_id:
        meta["session_id"] = session_id
    with open(metadata_path, "w") as f:
        json.dump(meta, f, indent=2)

    has_report = meta["has_report"]
    print(f"\n{'='*60}", flush=True)
    print(f"RUN COMPLETE", flush=True)
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
