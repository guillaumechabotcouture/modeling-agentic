"""v2 C: the stage machine's LLM tasks as bounded Agent SDK sessions (main.py --v2).

Each task is one short `query()` session: a task-specific system prompt, file tools only
(Read, Glob, Grep, Write, Edit; no Bash, no subagents), the run directory as cwd, a turn
cap, a dollar cap, and a PreToolUse hook that refuses writes outside the files the task
owns. The output is a file the harness reads and validates; the spend is the session's
reported cost. The machine (agents/stage_machine.py) decides everything else.
"""
from __future__ import annotations

import asyncio
import json
import os

from agents import enforcement
from agents.v2_tasks import CRITIQUE_FILE, PREFIX, TaskResult
from agents.fix_loop import FIX_PLANNER_PROMPT

TOOLS = ["Read", "Glob", "Grep", "Write", "Edit"]
OUT = "_task_output.json"
# Files only the harness writes in a v2 run.
HARNESS_FILES = ("run_state.json", "findings.jsonl", "impact_ledger.jsonl", "metadata.json", "final_gate.json",
                 "decision_spec.signoff.json", "engine_choice.json", "validator_history.jsonl", "done_when_registry.json",
                 "report.md", "allocation_optimized.csv")
HARNESS_PREFIXES = ("analysis/", "models/pipeline", "gate_r", "fix_plan_r", "evidence/")

COMMON = """You are one bounded task inside a modeling run driven by code (a state machine). The harness
computes every number from the decision spec and the engine; you never type a number the report
uses and never edit harness outputs (analysis/, run_state.json, findings.jsonl, gate files).
Work only in the run directory, which is your working directory. Finish by writing the output
file named below, then stop."""

PROMPTS = {
    "draft_spec": COMMON + """

Task: draft decision_spec.yaml for the question. Read the data files the question names and the
example spec at {example}. Keep its section names (decision_units, calibration, horizon, emulator,
options, objective, outcomes_reported, constraints, comparator, uncertainty, deliverable,
deliverable_file, engine_needs). Do not set `engine`: the harness chooses it. Map the question's
units to a units table (CSV) under data/ with one row per decision unit: write that table only from
the question's data files, with a short derivation note in data/README.md. Output: decision_spec.yaml
and {out} = {{"ok": true, "notes": "..."}}.""",
    "pick_engine": COMMON + """

Task: choose the engine. {out_in} lists the registered engines, which are eligible, and why the
others are not. Pick one eligible engine and say why in two sentences (mechanisms the question
needs, validated benchmarks). Output: {out} = {{"engine": "<ref>", "why": "..."}}.""",
    "critique": COMMON + """

Task: {reviewer} review, round {round}. Review the computed facts: analysis/analysis.json,
analysis/allocation.csv, engine_choice.json, decision_spec.yaml, impact_ledger.jsonl and the draft
(manuscript.md) if it exists. Judge what code cannot: plausibility against the domain, whether the
comparator and the objective match the question, hidden assumptions, overclaims. Write
{file} in the critique-blockers schema: reviewer, round, verdict (PASS | REVISE),
structural_mismatch {{detected: bool}}, carried_forward (round 1: []), blockers (ids {prefix}-NNN;
id, severity HIGH|MEDIUM|LOW, category, target_stage PLAN|DATA|MODEL|ANALYZE|WRITE,
first_seen_round, claim, evidence, fix_requires, resolved, and a done_when test where one fits).
Then write {out} = {{"ok": true}}.""",
    "plan_fixes": FIX_PLANNER_PROMPT + "\nThe packet is in {out_in}. Write your JSON to {out}.",
    "fix": COMMON + """

Task: fix task {tid}: {brief}
Findings: {findings}. Evidence from the last check (if any): {evidence}
You may edit decision_spec.yaml sections that are technical (emulator, calibration ranges,
uncertainty, decision_tests, engine_needs) and files under data/ and models/ext/. The decision
sections (decision_units, options, objective, constraints, comparator, horizon, deliverable) are
signed by the owner: do not change them. The harness rebuilds and measures the effect.
Output: {out} = {{"ok": true, "changed": "<one line>"}}.""",
    "write": COMMON + """

Task: write manuscript.md ({mode}). Read analysis/analysis.json, engine_choice.json,
decision_spec.yaml and scope_declaration.yaml if present. Write the narrative, the methods and the
interpretation. Refer to numbers by name (the harness renders the numbers table below your text);
do not type them. Mode `not_accepted`: the first section is "Status: not accepted by the final
gate", listing the open blocking findings from findings.jsonl in plain words. Mode `declared`: a
Limitations section that repeats each declaration. Output: manuscript.md and {out} = {{"ok": true}}.""",
}
OWNS = {"draft_spec": ("decision_spec.yaml", "data/"), "pick_engine": (), "plan_fixes": (),
        "critique": (), "fix": ("decision_spec.yaml", "data/", "models/ext/"), "write": ("manuscript.md",)}


def _allowed(path: str, run_dir: str, owns: tuple) -> str | None:
    p = os.path.realpath(os.path.join(run_dir, path))
    root = os.path.realpath(run_dir)
    if not p.startswith(root + os.sep):
        return "outside the run directory"
    rel = os.path.relpath(p, root).replace(os.sep, "/")
    if rel == OUT or any(rel == o or rel.startswith(o) for o in owns if o.endswith("/")) or rel in owns:
        return None
    if rel in HARNESS_FILES or rel.startswith(HARNESS_PREFIXES):
        return f"{rel} is written by the harness"
    return f"this task may write only {', '.join(owns + (OUT,))}"


class SdkTasks:
    def __init__(self, run_dir: str, allow_cloud: bool = False, task_cap_usd: float = 3.0, max_turns: int = 30):
        self.run_dir = os.path.abspath(run_dir)
        self.cloud = allow_cloud
        self.cap = task_cap_usd
        self.turns = max_turns

    def _session(self, kind: str, system: str, prompt: str, owns: tuple, model: str | None = None) -> TaskResult:
        from claude_agent_sdk import ClaudeAgentOptions, HookMatcher, ResultMessage, query
        out = os.path.join(self.run_dir, OUT)
        if os.path.exists(out):
            os.remove(out)

        async def pre(input_data, tool_use_id, context):
            name = input_data.get("tool_name")
            ti = input_data.get("tool_input") or {}
            if name in ("Write", "Edit"):
                why = _allowed(ti.get("file_path", ""), self.run_dir, owns)
                if why:
                    return enforcement.deny_output(f"{kind}: {why}")
            if name in ("Read", "Grep", "Glob"):
                tgt = str(ti.get("file_path") or ti.get("path") or "")
                if "evals/" in tgt or "_truth" in tgt:
                    return enforcement.deny_output("evaluation answer keys are not readable")
            return {}

        opts = ClaudeAgentOptions(system_prompt=system, model=model or enforcement.MODEL_DEEP, tools=TOOLS,
                                  allowed_tools=TOOLS, permission_mode="bypassPermissions", cwd=self.run_dir,
                                  max_turns=self.turns, max_budget_usd=self.cap, setting_sources=[],
                                  env=enforcement.filtered_env(dict(os.environ), self.cloud),
                                  hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[pre])]})

        async def go():
            res = None
            async for m in query(prompt=prompt, options=opts):
                if isinstance(m, ResultMessage):
                    res = m
            return res
        try:
            r = asyncio.run(go())
        except Exception as e:  # noqa: BLE001 -- a failed task is a result, not a crash of the machine
            return TaskResult(False, None, 0.0, f"{type(e).__name__}: {e}"[:300])
        usd = float(getattr(r, "total_cost_usd", 0.0) or 0.0)
        try:
            output = json.load(open(out))
        except (OSError, ValueError):
            return TaskResult(False, None, usd, f"no valid {OUT}")
        finally:
            if os.path.exists(out):
                os.remove(out)
        return TaskResult(True, output, usd, str(output.get("notes") or output.get("changed") or "")[:300])

    def _input(self, obj) -> str:
        name = "_task_input.json"
        with open(os.path.join(self.run_dir, name), "w") as f:
            json.dump(obj, f, indent=1, default=str)
        return name

    def draft_spec(self, question, run_dir, feedback):
        ex = os.path.join(enforcement.REPO_ROOT, "spike", "decision_spec.yaml")
        p = f"Question:\n{question}\n" + (f"\nThe last draft failed validation:\n- " + "\n- ".join(feedback) if feedback else "")
        return self._session("draft_spec", PROMPTS["draft_spec"].format(example=ex, out=OUT), p, OWNS["draft_spec"])

    def sign_spec(self, run_dir):
        with open(os.path.join(run_dir, "decision_spec.signoff.json"), "w") as f:
            json.dump({"by": "lead", "note": "lead mode: the harness accepts the drafted spec"}, f)
        return TaskResult(True, None, 0.0, "lead mode")

    def pick_engine(self, spec, eligible):
        inp = self._input(eligible)
        return self._session("pick_engine", PROMPTS["pick_engine"].format(out_in=inp, out=OUT),
                             "Choose the engine.", OWNS["pick_engine"], enforcement.MODEL_FAST)

    def critique(self, run_dir, reviewer, round_n):
        f = CRITIQUE_FILE[reviewer]
        model = enforcement.MODEL_FAST if reviewer == "critique-presentation" else enforcement.MODEL_DEEP
        return self._session("critique", PROMPTS["critique"].format(reviewer=reviewer, round=round_n, file=f,
                                                                     prefix=PREFIX[reviewer], out=OUT),
                             f"Review round {round_n}.", (f,), model)

    def plan_fixes(self, packet):
        inp = self._input(packet)
        return self._session("plan_fixes", PROMPTS["plan_fixes"].format(out_in=inp, out=OUT),
                             "Plan the fixes.", OWNS["plan_fixes"])

    def fix(self, task, run_dir, evidence):
        owns = OWNS["fix"] if task.get("owner_state") not in ("WRITE", "PRESENTATION") else ("manuscript.md",)
        return self._session("fix", PROMPTS["fix"].format(tid=task["id"], brief=task.get("brief", ""),
                                                           findings=task["findings"],
                                                           evidence=json.dumps(evidence)[:2000] if evidence else "none",
                                                           out=OUT), "Make the fix.", owns)

    def write(self, run_dir, mode):
        return self._session("write", PROMPTS["write"].format(mode=mode, out=OUT), "Write the draft.", OWNS["write"])
