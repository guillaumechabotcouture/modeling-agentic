"""Agent registry, lead prompt, and hook infrastructure for the modeling pipeline."""

import json
import os
from datetime import datetime

from claude_agent_sdk import AgentDefinition, HookMatcher

from agents import (
    planner, data, modeler, analyst,
    critique_methods, critique_domain, critique_presentation,
    writer,
)

MAX_FIGURE_PIXELS = 10_000_000  # 10MP -- anything larger is likely a bug

# Mirror of maxTurns configured in build_agents(). Used by subagent_stop_hook
# to annotate stop events with an approximate stop reason (ran_to_cap vs
# completed_under_cap) based on observed tool-use count.
AGENT_MAX_TURNS = {
    "planner": 80,
    "data-agent": 60,
    "modeler": 100,
    "model-tester": 60,
    "analyst": 40,
    "critique-methods": 35,
    "critique-domain": 40,
    "critique-presentation": 30,
    "writer": 60,
}


# ---------------------------------------------------------------------------
# Utilities (kept from prior version)
# ---------------------------------------------------------------------------

def cleanup_orphaned_claude_processes():
    """Kill orphaned claude CLI processes from crashed agent sessions.
    These accumulate at 400-600MB each and cause OOM SIGKILL crashes."""
    import subprocess
    try:
        result = subprocess.run(
            ["pgrep", "-f", "claude_agent_sdk/_bundled/claude"],
            capture_output=True, text=True,
        )
        pids = [p for p in result.stdout.strip().split("\n") if p]
        if len(pids) > 2:  # Keep max 2 (current session), kill the rest
            for pid in pids[2:]:
                subprocess.run(["kill", "-9", pid], capture_output=True)
            print(f"[cleanup] Killed {len(pids) - 2} orphaned claude processes", flush=True)
    except Exception:
        pass


def _check_figure_size(path: str, stage_name: str) -> None:
    """Warn and resize oversized PNG files."""
    if not os.path.exists(path) or not path.endswith(".png"):
        return
    try:
        from PIL import Image
        Image.MAX_IMAGE_PIXELS = 2_000_000_000
        img = Image.open(path)
        pixels = img.size[0] * img.size[1]
        if pixels > MAX_FIGURE_PIXELS:
            ratio = (MAX_FIGURE_PIXELS / pixels) ** 0.5
            new_size = (int(img.size[0] * ratio), int(img.size[1] * ratio))
            print(
                f"[{stage_name}] WARNING: {os.path.basename(path)} is "
                f"{img.size[0]}x{img.size[1]} ({pixels/1e6:.0f}MP). "
                f"Resizing to {new_size[0]}x{new_size[1]}.",
                flush=True,
            )
            img = img.resize(new_size, Image.LANCZOS)
            img.save(path)
        img.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Agent registry
# ---------------------------------------------------------------------------

def build_agents() -> dict[str, AgentDefinition]:
    """Return all subagent definitions for the lead session."""
    return {
        "planner": AgentDefinition(
            description=planner.DESCRIPTION,
            prompt=planner.SYSTEM_PROMPT,
            tools=planner.TOOLS,
            model="opus",
            maxTurns=80,
            skills=["semantic-scholar-lookup", "asta-literature-search",
                    "pdf-text-extraction", "investigation-threads",
                    "modeling-strategy", "malaria-modeling",
                    "basic_epi_modeling", "vectors", "vaccination"],
        ),
        "data-agent": AgentDefinition(
            description=data.DESCRIPTION,
            prompt=data.SYSTEM_PROMPT,
            tools=data.TOOLS,
            model="sonnet",
            maxTurns=60,
        ),
        "modeler": AgentDefinition(
            description=modeler.DESCRIPTION,
            prompt=modeler.SYSTEM_PROMPT,
            tools=modeler.TOOLS,
            model="opus",
            maxTurns=100,
            skills=["modeling-strategy", "laser-spatial-disease-modeling",
                    "epi-model-parametrization", "malaria-modeling",
                    "model-validation",
                    # starsim_ai: disease modeling fundamentals
                    "vectors", "sir-models", "sir-elaborations",
                    "vaccination", "parameter-estimation",
                    # starsim_ai: starsim framework
                    "starsim-dev-intro", "starsim-dev-diseases",
                    "starsim-dev-interventions", "starsim-dev-calibration",
                    "starsim-dev-networks", "starsim-dev-demographics"],
        ),
        "model-tester": AgentDefinition(
            description=modeler.MODEL_TESTER_DESCRIPTION,
            prompt=modeler.MODEL_TESTER_PROMPT,
            tools=modeler.MODEL_TESTER_TOOLS,
            skills=modeler.MODEL_TESTER_SKILLS + ["malaria-modeling",
                    "starsim-dev-intro", "starsim-dev-diseases",
                    "starsim-dev-interventions", "starsim-dev-calibration"],
            model="sonnet",
            maxTurns=60,
        ),
        "analyst": AgentDefinition(
            description=analyst.DESCRIPTION,
            prompt=analyst.SYSTEM_PROMPT,
            tools=analyst.TOOLS,
            model="opus",
            maxTurns=40,
            skills=["investigation-threads", "malaria-modeling",
                    "basic_epi_modeling", "surveillance"],
        ),
        "critique-methods": AgentDefinition(
            description=critique_methods.DESCRIPTION,
            prompt=critique_methods.SYSTEM_PROMPT,
            tools=critique_methods.TOOLS,
            model="opus",
            maxTurns=35,  # increased: now does parameter provenance checks with WebSearch
            skills=["malaria-modeling", "model-validation",
                    "parameter-estimation"],
        ),
        "critique-domain": AgentDefinition(
            description=critique_domain.DESCRIPTION,
            prompt=critique_domain.SYSTEM_PROMPT,
            tools=critique_domain.TOOLS,
            model="opus",
            maxTurns=40,  # increased: now does citation verification with WebSearch+WebFetch
            skills=["investigation-threads", "model-fitness", "malaria-modeling",
                    "vectors", "vaccination"],
        ),
        "critique-presentation": AgentDefinition(
            description=critique_presentation.DESCRIPTION,
            prompt=critique_presentation.SYSTEM_PROMPT,
            tools=critique_presentation.TOOLS,
            model="sonnet",
            maxTurns=30,
        ),
        "writer": AgentDefinition(
            description=writer.DESCRIPTION,
            prompt=writer.SYSTEM_PROMPT,
            tools=writer.TOOLS,
            model="opus",  # needs to synthesize the most complex output
            maxTurns=60,
            skills=["investigation-threads"],
        ),
    }


# ---------------------------------------------------------------------------
# Lead agent prompt
# ---------------------------------------------------------------------------

LEAD_SYSTEM_PROMPT = """\
You are a research modeling team lead. You orchestrate a team of specialist
agents to take a research question and produce a publication-quality
mathematical model, analysis, and report.

You have access to these named agents via the Agent tool:
- **planner**: Deep literature review, modeling plan, benchmarks, hypotheses
- **data-agent**: Download datasets, validate quality, run EDA
- **modeler**: Build and run models, produce figures (has model-tester sub-agent)
- **analyst**: Interpret results, test hypotheses, causal reasoning
- **critique-methods**: Statistical validation review
- **critique-domain**: Scientific reasoning review
- **critique-presentation**: Figure and writing quality review
- **writer**: Assemble final publication-quality report

You also have Read, Write, Glob, and Grep tools to inspect files yourself.

## PIPELINE PROTOCOL

Follow these stages IN ORDER. Do not skip stages.

### STAGE 1: PLAN
Spawn the "planner" agent. Tell it the research question and run directory.
Wait for completion. Then read {run_dir}/plan.md and verify it has:
- Literature review with 10+ papers
- Published benchmarks table
- Candidate models (baseline, standard, advanced)
- Testable hypotheses
- threads.yaml

If critical sections are missing, re-spawn planner with specific feedback.

### STAGE 2: DATA
Spawn the "data-agent" agent. Tell it the run directory (which has plan.md).
Wait for completion. Then read {run_dir}/data_quality.md and verify datasets
were downloaded, validated, and EDA was run.

### STAGE 3: PRE-MODEL STRATEGY CHECK
Read plan.md and data_quality.md yourself. Assess:
- Is the proposed modeling approach feasible given the available data?
- Are there critical data gaps that would prevent calibration or validation?
- Is the proposed complexity appropriate for the stated purpose?

If the approach needs adjustment, either:
- Re-spawn data-agent for missing datasets, OR
- Re-spawn planner to revise the modeling strategy
Otherwise, proceed to modeling.

### STAGE 4: MODEL
Spawn the "modeler" agent. Tell it the run directory and research question.
If this is a revision round, include the specific critique feedback.
Wait for completion. Read {run_dir}/model_comparison.md to confirm models
ran and metrics were reported.

### STAGE 5: ANALYZE
Spawn the "analyst" agent. Tell it the run directory.
Wait for completion. Read {run_dir}/results.md to confirm hypothesis
verdicts exist with causal labels.

### STAGE 6: CRITIQUE (PARALLEL)
Spawn ALL THREE critique agents simultaneously in a SINGLE response:
- critique-methods
- critique-domain
- critique-presentation

Do NOT use background: true. Spawn them in the foreground so you
receive their results directly before moving to Stage 7.

Tell each one the research question, run directory, AND the current
critique round number (starts at 1, increments per revision cycle).
Each critique agent writes TWO files:
- `critique_{name}.md` — human-readable prose (as before)
- `critique_{name}.yaml` — machine-readable blocker manifest (see the
  `critique-blockers-schema` skill for the required schema)

Wait for all three to complete.

### STAGE 7: MECHANICAL DECISION GATE

The STAGE 7 decision is NOT a free judgment call. You compute it
mechanically from the three critique YAML files. You do not get to
override the rules with prose reasoning.

#### Step 1: Run the validator

After the three critique YAMLs exist, invoke the validator via Bash:

```bash
python3 scripts/validate_critique_yaml.py {run_dir} \\
  --max-rounds {max_rounds} --current-round <N> --json
```

The validator will:
- Schema-check all three YAML files. Exit code 3 = schema error;
  instruct the offending critique agent to re-write the YAML before
  you proceed. Do NOT hand-edit critique YAMLs.
- Compute `unresolved_high`, `structural_mismatch`, `rounds_remaining`.
- Emit an `action` field: RETHINK_STRUCTURAL | RUN_FAILED |
  PATCH_OR_RETHINK | DECLARE_SCOPE | ACCEPT.

Exit 0 means ACCEPT. Exit 1 means any non-ACCEPT action. Exit 3 means
schema error — stop and fix before proceeding.

#### Step 2: Write the decision record

Before taking any action, append a `## Stage 7 decision (round N)`
section to `{run_dir}/progress.md` containing the validator's JSON
output verbatim, plus one sentence explaining your planned next step
(e.g., "RETHINK: modeler to re-spawn with instructions to add an
age-structured compartment and keep the Level 1 model as baseline").

This is your audit trail. Future resumes read it.

#### Step 3: Execute the action

The validator has already decided for you. Your job is to carry it out:

- **RETHINK_STRUCTURAL**: the model's architecture does not match the
  question. Do NOT patch. Do NOT DECLARE_SCOPE. Re-spawn the modeler
  with the structural-mismatch `description` from the critique YAML as
  the primary instruction. Reference modeling strategy Level
  progression where appropriate.

- **RUN_FAILED**: structural mismatch AND no rounds remaining. Write
  `RUN_FAILED` at the top of progress.md with the structural mismatch
  description. Do NOT spawn the writer. End the pipeline. The
  delivered model does not answer the question asked; shipping a
  report would misrepresent what was built.

- **PATCH_OR_RETHINK**: HIGH blockers remain and rounds are left. You
  choose PATCH vs RETHINK using the heuristics below. ACCEPT is
  forbidden. DECLARE_SCOPE is forbidden.

  * **PATCH** when blockers are `target_stage: MODEL | ANALYZE` and
    category is HARD_BLOCKER, METHODS, CAUSAL, HYPOTHESES, CITATIONS,
    or PRESENTATION, AND none of the PATCH→RETHINK escalation triggers
    below apply. Re-spawn the target_stage agent with the verbatim
    `claim` + `fix_requires` text from each blocker.

  * **RETHINK** when ANY apply:
    - Any blocker has `category: STRUCTURAL`.
    - The same blocker `id` has `first_seen_round < current_round`
      AND was not resolved by an intervening patch (check
      `carried_forward[].still_present: true` in the latest critique).
      Don't patch the same thing twice.
    - Two or more HIGH blockers across critiques point at the same
      model mechanism (e.g., all three mention "no age structure").

  * **REDIRECT** (subcase of PATCH) when all unresolved HIGH blockers
    have `target_stage: PLAN | DATA`. Re-spawn planner or data-agent
    with the verbatim blocker text, not the modeler.

- **DECLARE_SCOPE**: HIGH blockers remain and rounds exhausted.
  Before spawning the writer, you MUST write
  `{run_dir}/scope_declaration.yaml` acknowledging each unresolved
  HIGH blocker by id with a `why_unresolved` paragraph and an
  `impact_on_conclusions` paragraph. Then include in the writer spawn
  the instruction: "Embed the scope_declaration.yaml acknowledgments
  verbatim in report.md §Limitations. Do not soften the language. Do
  not omit blockers."

- **ACCEPT**: no unresolved HIGH blockers, no structural mismatch.
  Proceed to STAGE 8.

#### Forbidden moves

You cannot:
- Skip the validator. Every round 6 decision must follow a Bash call
  to `validate_critique_yaml.py`.
- ACCEPT while `unresolved_high` is non-empty.
- DECLARE_SCOPE while `structural_mismatch` is true.
- DECLARE_SCOPE while rounds remain.
- Overrule the validator's action with prose. If you believe a HIGH
  blocker is mis-severity, instruct the critique agent to re-write
  the YAML and re-run the validator. Do not paper over it.

When re-spawning an agent after PATCH/RETHINK/REDIRECT, pass the
SPECIFIC blocker text — not just "fix the issues." Quote the
`claim` and `fix_requires` fields verbatim, grouped by blocker id.

After deciding, if not ACCEPTing, go back to the targeted stage
(STAGE 2, 3, or 4) and continue from there. Increment the round
counter before the next STAGE 6.

### STAGE 8: WRITE
Spawn the "writer" agent. Tell it the run directory.
Wait for completion. Verify {run_dir}/report.md exists.

## GENERAL RULES

- Every finding must have a causal label: CAUSAL, ASSOCIATIONAL, or PROXY.
- All artifacts go in {run_dir}/.
- Update {run_dir}/progress.md after each stage with what was completed.
- After each stage, update {run_dir}/pipeline_state.yaml so the pipeline
  can resume cleanly if interrupted. Use this YAML format:
  ```yaml
  current_stage: MODEL  # the NEXT stage to run
  current_round: 1
  completed:
    PLAN: {at: "timestamp"}
    DATA: {at: "timestamp"}
  ```
- If a subagent fails (you see it reported as failed), read its output for
  diagnostics. Retry ONCE with adjusted instructions. If it fails again,
  log the failure in progress.md and proceed with DECLARE_SCOPE.
- Never retry a failed agent more than once per round.
"""


def build_lead_prompt(question: str, run_dir: str, max_rounds: int,
                      resume_context: str = "") -> str:
    """Build the user-facing prompt for the lead agent."""
    prompt = (
        f"Research question: {question}\n\n"
        f"Run directory: {run_dir}\n"
        f"Max critique-revision rounds: {max_rounds}\n"
    )
    if resume_context:
        prompt += f"\n{resume_context}\n"
    prompt += (
        "\nBegin the pipeline. Start with STAGE 1 (PLAN)."
    )
    return prompt


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------

def create_hooks(run_path: str, trace_file, start_time: datetime):
    """Create hook config for the lead query() session."""

    tool_count = 0
    per_agent_tool_counts: dict[str, int] = {}

    def _format_tool(name: str, input_data: dict) -> str:
        if name == "WebSearch":
            return f'WebSearch: "{input_data.get("query", "")}"'
        elif name == "WebFetch":
            return f"WebFetch: {input_data.get('url', '')[:80]}"
        elif name == "Bash":
            return f"Bash: {input_data.get('command', '')[:80]}"
        elif name == "Write":
            return f"Write: {input_data.get('file_path', '')}"
        elif name == "Edit":
            return f"Edit: {input_data.get('file_path', '')}"
        elif name == "Read":
            return f"Read: {input_data.get('file_path', '')}"
        elif name == "Glob":
            return f"Glob: {input_data.get('pattern', '')}"
        elif name == "Grep":
            return f'Grep: "{input_data.get("pattern", "")}"'
        elif name in ("Agent", "Task"):
            return f"Agent: {input_data.get('description', input_data.get('subagent_type', ''))}"
        return name

    async def pre_tool_hook(input_data, tool_use_id, context):
        nonlocal tool_count
        tool_count += 1
        tool_name = input_data.get("tool_name", "unknown")
        tool_input = input_data.get("tool_input", {})
        agent_id = input_data.get("agent_id", "lead")
        per_agent_tool_counts[agent_id] = per_agent_tool_counts.get(agent_id, 0) + 1
        summary = _format_tool(tool_name, tool_input)
        elapsed = (datetime.now() - start_time).total_seconds()
        label = agent_id if agent_id != "lead" else "lead"
        print(f"[{label:>12} {elapsed:6.0f}s | #{tool_count}] {summary}", flush=True)
        trace_file.write(json.dumps({
            "ts": datetime.now().isoformat(),
            "elapsed_s": elapsed,
            "agent": agent_id,
            "type": "tool_use",
            "tool": tool_name,
            "summary": summary,
        }) + "\n")
        trace_file.flush()
        return {}

    async def post_tool_hook(input_data, tool_use_id, context):
        """Check written PNG files for oversized figures."""
        tool_name = input_data.get("tool_name", "")
        tool_input = input_data.get("tool_input", {})
        agent_id = input_data.get("agent_id", "lead")
        if tool_name in ("Bash", "Write"):
            file_path = tool_input.get("file_path", "")
            if file_path.endswith(".png"):
                _check_figure_size(file_path, agent_id)
        if tool_name == "Bash":
            fig_dir = os.path.join(run_path, "figures")
            if os.path.isdir(fig_dir):
                for fname in os.listdir(fig_dir):
                    if fname.endswith(".png"):
                        _check_figure_size(os.path.join(fig_dir, fname), agent_id)
        return {}

    async def subagent_start_hook(input_data, tool_use_id, context):
        agent_id = input_data.get("agent_id", "?")
        agent_type = input_data.get("agent_type", "?")
        elapsed = (datetime.now() - start_time).total_seconds()
        print(f"\n--- SUBAGENT START: {agent_type} (id={agent_id}) [{elapsed:.0f}s] ---", flush=True)
        trace_file.write(json.dumps({
            "ts": datetime.now().isoformat(),
            "elapsed_s": elapsed,
            "type": "subagent_start",
            "agent_id": agent_id,
            "agent_type": agent_type,
        }) + "\n")
        trace_file.flush()
        return {}

    async def subagent_stop_hook(input_data, tool_use_id, context):
        agent_id = input_data.get("agent_id", "?")
        agent_type = input_data.get("agent_type", "?")
        elapsed = (datetime.now() - start_time).total_seconds()
        turns_used = per_agent_tool_counts.get(agent_id, 0)
        max_turns = AGENT_MAX_TURNS.get(agent_type, None)
        if max_turns is None:
            approx_stop_reason = "unknown"
        elif turns_used >= max_turns - 2:
            approx_stop_reason = "ran_to_cap"
        else:
            approx_stop_reason = "completed_under_cap"
        print(
            f"--- SUBAGENT STOP: {agent_type} (id={agent_id}) [{elapsed:.0f}s] "
            f"turns={turns_used} {approx_stop_reason} ---\n",
            flush=True,
        )
        trace_file.write(json.dumps({
            "ts": datetime.now().isoformat(),
            "elapsed_s": elapsed,
            "type": "subagent_stop",
            "agent_id": agent_id,
            "agent_type": agent_type,
            "turns_used": turns_used,
            "approx_stop_reason": approx_stop_reason,
        }) + "\n")
        trace_file.flush()
        return {}

    return {
        "PreToolUse": [HookMatcher(matcher=None, hooks=[pre_tool_hook])],
        "PostToolUse": [HookMatcher(matcher=None, hooks=[post_tool_hook])],
        "SubagentStart": [HookMatcher(matcher=None, hooks=[subagent_start_hook])],
        "SubagentStop": [HookMatcher(matcher=None, hooks=[subagent_stop_hook])],
    }
