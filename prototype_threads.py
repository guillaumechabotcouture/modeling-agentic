"""
Prototype: Git-native multi-thread investigation with worktrees.

Tests the core concept: two investigation threads running in parallel
git worktrees, coordinated through threads.yaml on main, with a
strategist that merges completed threads.

Usage:
    python prototype_threads.py "research question"
"""

import asyncio
import json
import os
import subprocess
import yaml
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from claude_agent_sdk import query, ClaudeAgentOptions, AgentDefinition, AssistantMessage, ResultMessage


# ─── Git Operations ─────────────────────────────────────────────

def git(*args, cwd=None):
    """Run a git command and return stdout."""
    result = subprocess.run(
        ["git"] + list(args),
        capture_output=True, text=True,
        cwd=cwd or os.getcwd()
    )
    if result.returncode != 0 and "Already up to date" not in result.stderr:
        print(f"  git {' '.join(args)}: {result.stderr.strip()}", flush=True)
    return result.stdout.strip()


def create_worktree(thread_id: str) -> str:
    """Create a git worktree for a thread. Returns worktree path."""
    branch = f"thread/{thread_id}"
    worktree_path = f"/tmp/modeling-worktree-{thread_id}"

    # Clean up if exists from previous run
    if os.path.exists(worktree_path):
        git("worktree", "remove", "--force", worktree_path)
    try:
        git("branch", "-D", branch)
    except Exception:
        pass

    git("worktree", "add", worktree_path, "-b", branch)
    print(f"  Created worktree: {worktree_path} [{branch}]", flush=True)
    return worktree_path


def commit_in_worktree(worktree_path: str, message: str):
    """Stage and commit all changes in a worktree."""
    git("add", "-A", cwd=worktree_path)
    git("commit", "-m", message, "--allow-empty", cwd=worktree_path)


def merge_thread(thread_id: str):
    """Merge a thread branch back to main."""
    branch = f"thread/{thread_id}"
    result = git("merge", branch, "--no-edit")
    print(f"  Merged {branch} → main: {result}", flush=True)


def cleanup_worktree(thread_id: str):
    """Remove worktree and branch."""
    worktree_path = f"/tmp/modeling-worktree-{thread_id}"
    branch = f"thread/{thread_id}"
    if os.path.exists(worktree_path):
        git("worktree", "remove", "--force", worktree_path)
    git("branch", "-D", branch)


# ─── Thread Manifest ────────────────────────────────────────────

def create_threads_manifest(question: str, run_dir: str) -> dict:
    """Create initial threads.yaml with two investigation threads."""
    threads = {
        "run_dir": run_dir,
        "question": question,
        "created": datetime.now().isoformat(),
        "threads": [
            {
                "id": "T1_literature",
                "question": f"What does the literature say about: {question[:80]}?",
                "type": "research",
                "status": "planned",
                "branch": "thread/T1_literature",
                "worktree": None,
                "data_required": [],
                "outputs": [],
                "verdict": None,
            },
            {
                "id": "T2_data_model",
                "question": f"Can we build a simple model for: {question[:80]}?",
                "type": "modeling",
                "status": "planned",
                "depends_on": ["T1_literature"],
                "branch": "thread/T2_data_model",
                "worktree": None,
                "data_required": [],
                "outputs": [],
                "verdict": None,
            },
        ]
    }
    return threads


def save_threads(threads: dict, path: str):
    """Save threads manifest to yaml."""
    with open(path, "w") as f:
        yaml.dump(threads, f, default_flow_style=False, sort_keys=False)


def load_threads(path: str) -> dict:
    """Load threads manifest from yaml."""
    with open(path) as f:
        return yaml.safe_load(f)


def update_thread_status(threads: dict, thread_id: str, **updates):
    """Update a thread's fields."""
    for t in threads["threads"]:
        if t["id"] == thread_id:
            t.update(updates)
            return
    raise ValueError(f"Thread {thread_id} not found")


# ─── Agent Runner ───────────────────────────────────────────────

async def run_thread_agent(
    thread: dict,
    worktree_path: str,
    system_prompt: str,
    prompt: str,
    tools: list[str],
) -> None:
    """Run an agent in a worktree for a specific thread."""

    thread_id = thread["id"]
    print(f"\n{'='*50}", flush=True)
    print(f"THREAD: {thread_id}", flush=True)
    print(f"Worktree: {worktree_path}", flush=True)
    print(f"{'='*50}", flush=True)

    tool_count = 0

    async for message in query(
        prompt=prompt,
        options=ClaudeAgentOptions(
            system_prompt=system_prompt,
            allowed_tools=tools,
            permission_mode="bypassPermissions",
            cwd=worktree_path,
        ),
    ):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if hasattr(block, "text") and block.text:
                    # Print first 200 chars of each text block
                    preview = block.text[:200].replace('\n', ' ')
                    print(f"  [{thread_id}] {preview}...", flush=True)
                elif hasattr(block, "name"):
                    tool_count += 1
                    tool_input = getattr(block, "input", {}) or {}
                    if block.name == "WebSearch":
                        detail = tool_input.get("query", "")[:60]
                    elif block.name == "Write":
                        detail = tool_input.get("file_path", "")
                    elif block.name == "Bash":
                        detail = tool_input.get("command", "")[:60]
                    else:
                        detail = block.name
                    print(f"  [{thread_id} #{tool_count}] {block.name}: {detail}", flush=True)
        elif isinstance(message, ResultMessage):
            print(f"  [{thread_id}] Done: {message.subtype}", flush=True)

    # Auto-commit everything in the worktree
    commit_in_worktree(worktree_path, f"{thread_id}: agent work complete")
    print(f"  [{thread_id}] Committed to branch {thread['branch']}", flush=True)


# ─── Main Orchestrator ──────────────────────────────────────────

async def main():
    import sys
    if len(sys.argv) < 2:
        print("Usage: python prototype_threads.py 'research question'")
        return

    question = sys.argv[1]
    run_dir = f"prototype_runs/{datetime.now().strftime('%Y%m%d_%H%M')}"
    os.makedirs(run_dir, exist_ok=True)

    print(f"Question: {question}", flush=True)
    print(f"Run dir: {run_dir}", flush=True)

    # Step 1: Create threads manifest on main
    print("\n--- STEP 1: Create threads manifest ---", flush=True)
    threads = create_threads_manifest(question, run_dir)
    threads_path = os.path.join(run_dir, "threads.yaml")
    save_threads(threads, threads_path)
    git("add", threads_path)
    git("commit", "-m", f"Create threads manifest for: {question[:50]}")
    print(f"  Created {threads_path} with {len(threads['threads'])} threads", flush=True)

    # Step 2: Run Thread T1 (literature) in a worktree
    print("\n--- STEP 2: Thread T1 (literature research) ---", flush=True)
    wt1 = create_worktree("T1_literature")
    update_thread_status(threads, "T1_literature",
                         status="in_progress", worktree=wt1)
    save_threads(threads, threads_path)

    try:
        await run_thread_agent(
            thread=threads["threads"][0],
            worktree_path=wt1,
            system_prompt=(
                "You are a literature researcher. Search for 3-5 key papers "
                "on the research question. For each paper, extract: model type, "
                "key quantitative results, data used. Write your findings to "
                "research_notes.md in the current directory. Be concise."
            ),
            prompt=f"Research question: {question}\n\nWrite findings to research_notes.md.",
            tools=["WebSearch", "WebFetch", "Write", "Read"],
        )
        update_thread_status(threads, "T1_literature", status="complete")
    except Exception as e:
        print(f"  T1 error: {e}", flush=True)
        update_thread_status(threads, "T1_literature", status="failed",
                             error=str(e))

    # Merge T1 back to main
    print("\n--- STEP 3: Merge T1 → main ---", flush=True)
    merge_thread("T1_literature")
    save_threads(threads, threads_path)
    git("add", "-A")
    git("commit", "-m", "Merge T1_literature: research complete")

    # Step 4: Run Thread T2 (data + model) in its own worktree
    # T2 depends on T1, so it runs after T1 merges
    print("\n--- STEP 4: Thread T2 (data + model) ---", flush=True)
    wt2 = create_worktree("T2_data_model")

    # Copy research notes from main to T2's worktree
    # (they're already on main after merge)

    update_thread_status(threads, "T2_data_model",
                         status="in_progress", worktree=wt2)
    save_threads(threads, threads_path)

    try:
        await run_thread_agent(
            thread=threads["threads"][1],
            worktree_path=wt2,
            system_prompt=(
                "You are a modeler. Read research_notes.md for literature context. "
                "Build the SIMPLEST model that can answer the research question. "
                "Write model code to model.py (< 200 lines). Run it. "
                "Write key findings to results.md. Generate one figure."
            ),
            prompt=(
                f"Research question: {question}\n\n"
                f"Read research_notes.md for literature context.\n"
                f"Build a simple model, run it, write results.md."
            ),
            tools=["WebSearch", "Bash", "Write", "Read", "Edit", "Glob"],
        )
        update_thread_status(threads, "T2_data_model", status="complete")
    except Exception as e:
        print(f"  T2 error: {e}", flush=True)
        update_thread_status(threads, "T2_data_model", status="failed",
                             error=str(e))

    # Merge T2 back to main
    print("\n--- STEP 5: Merge T2 → main ---", flush=True)
    merge_thread("T2_data_model")
    save_threads(threads, threads_path)
    git("add", "-A")
    git("commit", "-m", "Merge T2_data_model: model complete")

    # Step 6: Strategist reviews on main
    print("\n--- STEP 6: Strategist review ---", flush=True)
    threads = load_threads(threads_path)
    complete = sum(1 for t in threads["threads"] if t["status"] == "complete")
    total = len(threads["threads"])
    print(f"  Threads: {complete}/{total} complete", flush=True)

    for t in threads["threads"]:
        print(f"  {t['id']}: {t['status']}", flush=True)

    # Cleanup worktrees
    print("\n--- STEP 7: Cleanup ---", flush=True)
    cleanup_worktree("T1_literature")
    cleanup_worktree("T2_data_model")

    print(f"\n{'='*50}", flush=True)
    print(f"PROTOTYPE COMPLETE", flush=True)
    print(f"  Threads: {complete}/{total}", flush=True)
    print(f"  Run dir: {run_dir}", flush=True)
    print(f"  git log:", flush=True)
    print(git("log", "--oneline", "-10"), flush=True)
    print(f"{'='*50}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
