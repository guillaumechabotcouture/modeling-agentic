"""Modeling agent orchestrator -- multi-stage pipeline with critique loop."""

import asyncio
import argparse
import json
import os
import re
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

from agents import run_agent, run_critique_with_fallback, parse_critique_target, STAGES, stage_index
from agents import planner, data, modeler, analyst
from agents import critique_methods, critique_domain, critique_presentation
from agents import writer


def detect_resume_stage(run_path: str) -> str:
    """Check what files exist and critique verdicts to determine where to resume."""
    from agents import parse_critique_target

    def has(f):
        return os.path.exists(os.path.join(run_path, f))

    if not has("plan.md"):
        return "plan"
    if not has("data_quality.md"):
        return "data"
    if not has("model_comparison.md") and not has("model.py"):
        return "model"
    if not has("results.md"):
        return "analyze"
    if not any(has(f) for f in ["critique_methods.md", "critique_domain.md", "critique_presentation.md"]):
        return "critique"

    # Critique files exist -- check if they said REVISE
    critique_target = parse_critique_target(run_path)
    if critique_target != "accept":
        print(f"Critiques say REVISE → {critique_target}", flush=True)
        return critique_target

    if not has("report.md"):
        return "write"
    return "complete"


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

    # Initialize progress file
    with open(os.path.join(run_path, "progress.md"), "w") as f:
        f.write(f"# Progress\n\n## Current Stage: plan\n")
        f.write(f"## Question: {question}\n\n")
        f.write(f"## Completed Stages:\n\n## Notes:\n")

    return run_name


async def run_stage(
    agent_module,
    question: str,
    run_dir: str,
    run_path: str,
    trace_file,
    start_time: datetime,
    stage_name: str,
    **kwargs,
) -> None:
    """Run a single pipeline stage."""
    run_dir_rel = f"runs/{run_dir}"
    sys_prompt = agent_module.SYSTEM_PROMPT.replace("{run_dir}", run_dir_rel)
    prompt = agent_module.make_prompt(question, run_dir_rel, **kwargs)
    agents = getattr(agent_module, "AGENTS", None)

    await run_agent(
        system_prompt=sys_prompt,
        prompt=prompt,
        tools=agent_module.TOOLS,
        run_path=run_path,
        stage_name=stage_name,
        trace_file=trace_file,
        agents=agents,
        start_time=start_time,
    )


async def run_pipeline(
    question: str,
    run_dir: str,
    run_path: str,
    max_rounds: int,
    trace_file,
    start_time: datetime,
) -> None:
    """Run the full modeling pipeline with critique loop."""

    # Detect where to resume from existing files
    stage = detect_resume_stage(run_path)
    if stage == "complete":
        print("All stages already complete. Nothing to do.", flush=True)
        return
    if stage != "plan":
        print(f"Resuming from: {stage} (prior stages detected on disk)", flush=True)

    for round_num in range(1, max_rounds + 1):
        print(f"\n{'='*60}", flush=True)
        print(f"ROUND {round_num}/{max_rounds} — starting from: {stage}", flush=True)
        print(f"{'='*60}", flush=True)

        trace_file.write(json.dumps({
            "ts": datetime.now().isoformat(),
            "type": "round_start",
            "round": round_num,
            "stage": stage,
        }) + "\n")

        # Run from current stage forward
        # Small delay between stages to let CLI processes clean up
        import time as _time

        if stage_index(stage) <= stage_index("plan"):
            await run_stage(
                planner, question, run_dir, run_path,
                trace_file, start_time, "plan",
            )
            _time.sleep(2)

        if stage_index(stage) <= stage_index("data"):
            await run_stage(
                data, question, run_dir, run_path,
                trace_file, start_time, "data",
            )
            _time.sleep(2)

        if stage_index(stage) <= stage_index("model"):
            await run_stage(
                modeler, question, run_dir, run_path,
                trace_file, start_time, "model",
                round_num=round_num,
            )
            _time.sleep(2)

        if stage_index(stage) <= stage_index("analyze"):
            await run_stage(
                analyst, question, run_dir, run_path,
                trace_file, start_time, "analyze",
            )
            _time.sleep(2)

        # Critique: 3 reviewers in parallel
        print(f"\n{'='*60}", flush=True)
        print(f"CRITIQUE (3 reviewers in parallel)", flush=True)
        print(f"{'='*60}", flush=True)

        # Run critiques with cascading fallback (Claude → GPT)
        run_dir_rel = f"runs/{run_dir}"

        async def run_critique(module, stage_name, output_file):
            sys_prompt = module.SYSTEM_PROMPT.replace("{run_dir}", run_dir_rel)
            prompt = module.make_prompt(question, run_dir_rel)
            await run_critique_with_fallback(
                system_prompt=sys_prompt,
                prompt=prompt,
                tools=module.TOOLS,
                run_path=run_path,
                stage_name=stage_name,
                trace_file=trace_file,
                output_filename=output_file,
                start_time=start_time,
            )

        await asyncio.gather(
            run_critique(critique_methods, "crit-methods", "critique_methods.md"),
            run_critique(critique_domain, "crit-domain", "critique_domain.md"),
            run_critique(critique_presentation, "crit-present", "critique_presentation.md"),
        )

        # Parse critique verdicts
        target = parse_critique_target(run_path)

        trace_file.write(json.dumps({
            "ts": datetime.now().isoformat(),
            "type": "critique_result",
            "round": round_num,
            "target": target,
        }) + "\n")

        if target == "accept":
            print(f"\nAll reviewers ACCEPT. Proceeding to report.", flush=True)
            break
        else:
            print(f"\nREVISE → back to {target} stage", flush=True)
            stage = target

    # Final: write report
    await run_stage(
        writer, question, run_dir, run_path,
        trace_file, start_time, "write",
    )


async def run(question: str, max_rounds: int, max_sessions: int) -> None:
    """Multi-session wrapper around the pipeline."""
    run_dir = create_run_dir(question)
    run_path = os.path.join(os.getcwd(), "runs", run_dir)

    print(f"Run: runs/{run_dir}/", flush=True)
    print(f"Question: {question}", flush=True)
    print(f"Max rounds: {max_rounds} | Max sessions: {max_sessions}", flush=True)

    run_start = datetime.now()
    trace_path = os.path.join(run_path, "trace.jsonl")

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

        try:
            await run_pipeline(
                question, run_dir, run_path, max_rounds,
                trace_file, run_start,
            )
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

            # Don't retry policy blocks -- they'll fail every time
            if "Usage Policy" in error_str or "violate" in error_str:
                print("Policy block detected. Try rephrasing the question.", flush=True)
                break

            if session_num >= max_sessions:
                print("Max sessions reached.", flush=True)
                break
            print("Restarting pipeline...", flush=True)

    # Save metadata
    elapsed = (datetime.now() - run_start).total_seconds()
    metadata_path = os.path.join(run_path, "metadata.json")
    with open(metadata_path) as f:
        meta = json.load(f)
    meta["completed"] = datetime.now().isoformat()
    meta["elapsed_s"] = elapsed
    meta["sessions"] = session_num
    meta["has_report"] = os.path.exists(os.path.join(run_path, "report.md"))
    with open(metadata_path, "w") as f:
        json.dump(meta, f, indent=2)

    has_report = meta["has_report"]
    print(f"\n{'='*60}", flush=True)
    print(f"RUN COMPLETE", flush=True)
    print(f"  Duration: {elapsed:.0f}s ({elapsed/60:.1f} min)", flush=True)
    print(f"  Sessions: {session_num}", flush=True)
    print(f"  Report: {'written' if has_report else 'not written'}", flush=True)
    print(f"  Results: runs/{run_dir}/", flush=True)
    print(f"{'='*60}", flush=True)


def main():
    parser = argparse.ArgumentParser(
        description="Build a mathematical model for a research question"
    )
    parser.add_argument("question", help="The research question to model")
    parser.add_argument("--max-rounds", type=int, default=5,
                        help="Max critique-revision rounds (default: 5)")
    parser.add_argument("--max-sessions", type=int, default=10,
                        help="Max sessions for context recovery (default: 10)")
    args = parser.parse_args()
    asyncio.run(run(args.question, args.max_rounds, args.max_sessions))


if __name__ == "__main__":
    main()
