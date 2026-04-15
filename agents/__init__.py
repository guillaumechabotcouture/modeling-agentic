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
            maxTurns=25,
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
Spawn ALL THREE critique agents simultaneously in a SINGLE response,
each with background: true:
- critique-methods
- critique-domain
- critique-presentation

Tell each one the research question and run directory, and which file
to write (critique_methods.md, critique_domain.md, critique_presentation.md).

Wait for all three to complete.

### STAGE 7: STRATEGIC DECISION (YOU decide)
Read all three critique files yourself. Then, BEFORE triaging individual
items, do a fit-for-purpose assessment:

#### FIT-FOR-PURPOSE GATE (do this first — see model-fitness skill)

Use the model-fitness skill's evaluation checklist. Re-read the original
research question, then assess:

1. WHO IS THE AUDIENCE and what do they require? (The skill has specific
   requirements for Global Fund, WHO, journals, and internal use.)
2. WHAT DECISIONS will be made? List the intervention comparisons.
3. For EACH comparison: does the model capture the MECHANISM that
   differentiates the options? (The skill calls this the "mechanism test.")
4. Does the model answer the SAME question that was asked, or a SIMPLER
   one? (The skill calls this the "simpler question test.")
5. Would the audience ENGAGE with the results or REJECT the structure?
   (The skill calls this the "audience rejection test.")

If #3, #4, or #5 reveals a structural gap: this is RETHINK and the
model needs Level escalation — regardless of what individual critique
items say. Do not let individually-patchable items distract from a
structural mismatch.

Write your fit-for-purpose assessment in {run_dir}/progress.md before
proceeding to the decision framework below.

#### Decision framework

**PATCH** — Code bugs, wrong parameter values, missing outputs,
presentation fixes. The model STRUCTURE is correct but the
IMPLEMENTATION has errors. Re-spawn the modeler with specific fixes.

**RETHINK** — The model structure cannot answer the research question.
Signs that RETHINK is needed (choose RETHINK, not PATCH, if ANY apply):
- Critique says the model lacks a feature needed for the stated PURPOSE
  (e.g., "can't evaluate age-targeted interventions without age structure",
  "can't compute DALYs without a mortality module", "can't compare to
  policy X without sub-regional resolution")
- The model can't reproduce a key calibration target due to missing
  compartments or mechanisms (not just wrong parameter values)
- Same metric hasn't improved after 1 PATCH round
- Multiple HIGH-severity critiques point at the same structural gap
- The model answers a SIMPLER question than what was asked

When you RETHINK: tell the modeler what structural change is needed
(add compartments, add age groups, change spatial resolution) and why.
Reference the modeling strategy progression: Level 1 → Level 2.
The modeler should keep Level 1 as a baseline and build Level 2 on top.

**REDIRECT** — Problem is upstream of the model: data gaps prevent
calibration, hypotheses are untestable, wrong question framing.
Re-spawn data-agent or planner as appropriate.

**ACCEPT** — Model is fit for its stated purpose, key hypotheses have
verdicts, hard blockers resolved, metrics reasonable.
Proceed to STAGE 8.

**DECLARE_SCOPE** — Model answers SOME but not all parts of the question.
Some hypotheses untestable. A structural limitation won't resolve with
more patches. The model IS fit for its primary purpose even if imperfect.
Write a scope declaration and proceed to STAGE 8.

CRITICAL RULES:
- If ANY critique flags a HIGH-severity unresolved issue, you MUST fix
  it before accepting. Do NOT accept with known contradictions.
- If the same hard blocker persists for 2+ rounds, RETHINK or DECLARE_SCOPE.
  Do NOT keep patching.
- RETHINK early is cheap. If round 1 critique reveals structural issues,
  RETHINK immediately — don't waste a round patching a broken structure.
- Track round count. After {max_rounds} rounds, DECLARE_SCOPE and proceed.
- When re-spawning an agent, pass the SPECIFIC critique feedback — not
  just "fix the issues." Quote the critique items verbatim.

After deciding, if not accepting, go back to the targeted stage (STAGE 2,
3, or 4) and continue from there.

### STAGE 8: WRITE
Spawn the "writer" agent. Tell it the run directory.
Wait for completion. Verify {run_dir}/report.md exists.

## GENERAL RULES

- Every finding must have a causal label: CAUSAL, ASSOCIATIONAL, or PROXY.
- All artifacts go in {run_dir}/.
- Update {run_dir}/progress.md after each stage with what was completed.
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
        print(f"--- SUBAGENT STOP: {agent_type} (id={agent_id}) [{elapsed:.0f}s] ---\n", flush=True)
        trace_file.write(json.dumps({
            "ts": datetime.now().isoformat(),
            "elapsed_s": elapsed,
            "type": "subagent_stop",
            "agent_id": agent_id,
            "agent_type": agent_type,
        }) + "\n")
        trace_file.flush()
        return {}

    return {
        "PreToolUse": [HookMatcher(matcher=None, hooks=[pre_tool_hook])],
        "PostToolUse": [HookMatcher(matcher=None, hooks=[post_tool_hook])],
        "SubagentStart": [HookMatcher(matcher=None, hooks=[subagent_start_hook])],
        "SubagentStop": [HookMatcher(matcher=None, hooks=[subagent_stop_hook])],
    }
