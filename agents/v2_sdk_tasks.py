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
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)
from agents import enforcement  # noqa: E402
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

Task: draft decision_spec.yaml for the question, and analysis_settings.yaml with the
computational settings only ({settings}; not repeated in the spec). Follow {skill} (the schema is the
docstring of {schema}; a full example is {example}). Write `engine: auto`: the harness chooses the
engine after sign-off. The question's input files are relative to the repository root {repo}; copy or
derive what the spec reads (the units table, the calibration targets) into data/ in the run
directory, only from those files, with a short derivation note in data/README.md. The spec's paths
are relative to the run directory. Output: decision_spec.yaml, analysis_settings.yaml and
{out} = {{"ok": true, "notes": "..."}}.""",
    "pick_engine": COMMON + """

Task: choose the engine. {out_in} lists the registered engines, which are eligible, and why the
others are not. Pick one eligible engine and say why in two sentences (mechanisms the question
needs, validated benchmarks). Output: {out} = {{"engine": "<ref>", "why": "..."}}.""",
    "critique": COMMON + """

Task: {reviewer} review, round {round}. Review the computed facts: the analysis artifacts in
analysis/ (manifest.json and the *.yaml artifacts it lists), results.md, uncertainty_report.yaml,
the deliverable file the spec names, engine_choice.json, decision_spec.yaml, impact_ledger.jsonl
and the draft (manuscript.md) if it exists. Judge what code cannot: plausibility against the domain, whether the
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
You may edit analysis_settings.yaml, which holds only computational settings
({settings}), and files under models/ext/.
decision_spec.yaml is signed by the owner and you cannot change it; a fix that needs it changed is
a finding for the owner. The harness undoes any other edit, then rebuilds and measures the effect.
Output: {out} = {{"ok": true, "changed": "<one line>"}}.""",
    "write": COMMON + """

Task: write manuscript.md ({mode}). Read results.md and the analysis artifacts in analysis/
(manifest.json lists them), engine_choice.json, decision_spec.yaml and scope_declaration.yaml if
present. Write the narrative, the methods and the
interpretation. Refer to numbers by name (the harness renders the numbers table below your text);
do not type them. Mode `not_accepted`: the first section is "Status: not accepted by the final
gate", listing the open blocking findings from findings.jsonl in plain words. Mode `declared`: a
Limitations section that repeats each declaration. Output: manuscript.md and {out} = {{"ok": true}}.""",
}
OWNS = {"draft_spec": ("decision_spec.yaml", "analysis_settings.yaml", "data/"), "pick_engine": (), "plan_fixes": (),
        "critique": (), "fix": ("analysis_settings.yaml", "models/ext/"), "write": ("manuscript.md",)}


def _answer_key(target: str, run_dir: str) -> bool:
    """A path into the repository's evals/ tree or any _truth directory. data/evals/<q>/ (the
    questions' public inputs) is readable."""
    if "_truth" in target.replace("\\", "/").split("/"):
        return True
    p = os.path.realpath(os.path.join(run_dir, os.path.expanduser(target)))
    rel = os.path.relpath(p, enforcement.REPO_ROOT).replace(os.sep, "/")
    return rel == "evals" or rel.startswith("evals/") or target.replace("\\", "/").lstrip("./").startswith("evals/")


def _allowed(path: str, run_dir: str, owns: tuple) -> str | None:
    p = os.path.realpath(os.path.join(run_dir, path))
    root = os.path.realpath(run_dir)
    if not p.startswith(root + os.sep):
        return "outside the run directory"
    rel = os.path.relpath(p, root).replace(os.sep, "/")
    low = rel.lower()
    if low in {h.lower() for h in HARNESS_FILES} or low.startswith(tuple(h.lower() for h in HARNESS_PREFIXES)):
        return f"{rel} is written by the harness"
    if os.path.exists(p):
        try:
            if os.path.isfile(p) and os.stat(p).st_nlink > 1:
                return f"{rel} is a hard link"
            for h in HARNESS_FILES:
                hp = os.path.join(root, h)
                if os.path.exists(hp) and os.path.samefile(hp, p):
                    return f"{rel} is the harness file {h}"
        except OSError:
            return f"{rel} cannot be checked"
    if low == OUT.lower() or any(low.startswith(o.lower()) for o in owns if o.endswith("/")) \
            or low in {o.lower() for o in owns}:
        return None
    return f"this task may write only {', '.join(owns + (OUT,))}"


def _settings_text() -> str:
    from agents import analysis_backend as AB
    return ", ".join(getattr(AB.backend(), "settings_paths", AB.SETTINGS_PATHS))


class SdkTasks:
    """Each call's dollar cap is the allowance the machine sets just before it (set_allowance: the
    run's remaining budget less the write-up reserve, at most the task cap). Spend is charged
    through agents/run_budget.py like the lead's sessions: a reported cost exactly (also one
    reported before an exception); after a crash the conversation's cost is read back
    (crashed_session_cost); when it cannot be, the whole allowance. Never $0 for a call that ran.
    Every charge is also written to budget_ledger.json."""

    def __init__(self, run_dir: str, allow_cloud: bool = False, task_cap_usd: float = 3.0, max_turns: int = 30,
                 query_fn=None):
        self.run_dir = os.path.abspath(run_dir)
        self.cloud = allow_cloud
        self.cap = task_cap_usd
        self.allowance = task_cap_usd
        self.readback = 0.0          # no read-back unless the machine grants a budget for it
        self.turns = max_turns
        self.query_fn = query_fn
        self.n = 0

    def set_allowance(self, usd: float, readback_usd: float | None = None) -> None:
        """The call's dollar cap, and the cap of one crash read-back (0: none; the whole allowance
        is then charged when a crashed session's cost is unknown)."""
        self.allowance = float(usd)
        self.readback = max(0.0, float(readback_usd or 0.0))

    def _charge(self, result, allowance: float, sid: str | None, source: str) -> float:
        """Exactly the reported cost when there is one; otherwise the whole allowance. Never the
        duration estimate charge_session uses for a session with no id (Codex R2-2)."""
        from agents import run_budget as RB
        ledger = RB.load_budget_ledger(self.run_dir)
        self.n += 1
        RB.charge_session(ledger, self.n, result, allowance, session_id=sid, elapsed_s=None, source=source)
        RB.write_budget_ledger(self.run_dir, ledger, None)
        return float(ledger["sessions"][-1].get("total_cost_usd") or 0.0)

    def _session(self, kind: str, system: str, prompt: str, owns: tuple, model: str | None = None) -> TaskResult:
        import time
        from claude_agent_sdk import ClaudeAgentOptions, HookMatcher, query
        from agents import run_budget as RB
        allowance = self.allowance
        if allowance is None or allowance <= 0:
            return TaskResult(False, None, 0.0, "no allowance: the call was not made")
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
                tgt = str(ti.get("file_path") or ti.get("path") or ti.get("pattern") or "")
                if _answer_key(tgt, self.run_dir):
                    return enforcement.deny_output("evaluation answer keys are not readable")
            return {}

        opts = ClaudeAgentOptions(system_prompt=system, model=model or enforcement.MODEL_DEEP, tools=TOOLS,
                                  allowed_tools=TOOLS, permission_mode="bypassPermissions", cwd=self.run_dir,
                                  max_turns=self.turns, max_budget_usd=allowance, setting_sources=[],
                                  env=enforcement.filtered_env(dict(os.environ), self.cloud),
                                  hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[pre])]})

        seen = {"result": None, "sid": None}
        qf = self.query_fn or query

        async def go():
            async for m in qf(prompt=prompt, options=opts):
                sid = getattr(m, "session_id", None) or ((getattr(m, "data", None) or {}).get("session_id")
                                                         if isinstance(getattr(m, "data", None), dict) else None)
                seen["sid"] = sid or seen["sid"]
                if isinstance(getattr(m, "total_cost_usd", None), (int, float)):
                    seen["result"] = m          # kept even if the stream raises afterwards

        t0 = time.time()
        err = None
        try:
            asyncio.run(go())
        except BaseException as e:  # noqa: BLE001 -- charged below, then reported (or re-raised)
            err = e
        source = "v2 task"
        result = seen["result"]
        charge_cap = allowance
        if result is None and seen["sid"] and self.readback > 0:
            # One read-back, capped at its own small budget (never the SDK's unbounded default).
            import dataclasses
            probe = dataclasses.replace(opts, resume=seen["sid"], max_turns=1, tools=[], allowed_tools=[],
                                        hooks=None, max_budget_usd=self.readback)
            charge_cap = allowance + self.readback

            async def readback():
                async for m in qf(prompt=RB.RECONCILE_PROMPT, options=probe):
                    c = getattr(m, "total_cost_usd", None)
                    if isinstance(c, (int, float)):
                        return m if (c > 0 and not getattr(m, "is_error", False)) else None
                return None
            try:
                result = asyncio.run(asyncio.wait_for(readback(), 120))
                source = "read back after a crash" if result is not None else "read-back gave no cost"
            except Exception:  # noqa: BLE001 -- unknown cost: the allowance plus the read-back cap
                result, source = None, "read-back failed"
        elif result is None:
            source = "unknown cost: the whole allowance"
        usd = self._charge(result, charge_cap, seen["sid"], source)
        if isinstance(err, (KeyboardInterrupt, SystemExit)):
            raise err
        if err is not None:
            return TaskResult(False, None, usd, f"{type(err).__name__}: {err}"[:300], seen["sid"])
        try:
            output = json.load(open(out))
        except (OSError, ValueError):
            return TaskResult(False, None, usd, f"no valid {OUT}", seen["sid"])
        finally:
            if os.path.exists(out):
                os.remove(out)
        return TaskResult(True, output, usd, str(output.get("notes") or output.get("changed") or "")[:300], seen["sid"])

    def _input(self, obj) -> str:
        name = "_task_input.json"
        with open(os.path.join(self.run_dir, name), "w") as f:
            json.dump(obj, f, indent=1, default=str)
        return name

    def draft_spec(self, question, run_dir, feedback):
        R = enforcement.REPO_ROOT
        p = f"Question:\n{question}\n" + (f"\nThe last draft failed validation:\n- " + "\n- ".join(feedback) if feedback else "")
        return self._session("draft_spec", PROMPTS["draft_spec"].format(
            settings=_settings_text(), skill=os.path.join(R, ".claude", "skills", "decision-spec", "SKILL.md"),
            schema=os.path.join(R, "toolkit", "spec", "schema.py"), example=os.path.join(R, "spike", "decision_spec.v2.yaml"),
            repo=R, out=OUT), p, OWNS["draft_spec"])

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
                                                           settings=_settings_text(), out=OUT), "Make the fix.", owns)

    def write(self, run_dir, mode):
        return self._session("write", PROMPTS["write"].format(mode=mode, out=OUT), "Write the draft.", OWNS["write"])


def _self_test() -> int:
    """Charging without the API: fake query streams."""
    import tempfile
    from types import SimpleNamespace as NS
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)

    def stream(*msgs, raise_after=None):
        async def q(prompt=None, options=None):
            for m in msgs:
                yield m
            if raise_after:
                raise raise_after
        return q
    with tempfile.TemporaryDirectory() as d:
        init = NS(subtype="init", data={"session_id": "s1"})
        res = NS(total_cost_usd=0.42, session_id="s1", is_error=False)
        t = SdkTasks(d, query_fn=stream(init, res))
        t.set_allowance(2.0)
        r = t._session("pick_engine", "x", "y", ())
        ok(not r.ok and abs(r.usd - 0.42) < 1e-9 and r.session_id == "s1",
           f"S1 a session without its output file still charges its reported cost ({r.usd})")
        init2, res2 = NS(subtype="init", data={"session_id": "s2"}), NS(total_cost_usd=0.42, session_id="s2", is_error=False)
        t = SdkTasks(d, query_fn=stream(init2, res2, raise_after=RuntimeError("stream broke")))
        t.set_allowance(2.0)
        r = t._session("pick_engine", "x", "y", ())
        ok(not r.ok and abs(r.usd - 0.42) < 1e-9, f"S2 a cost reported before an exception is charged ({r.usd})")

        async def failing_readback(prompt=None, options=None):
            if options is not None and getattr(options, "resume", None):
                raise RuntimeError("no read-back")
            yield init
            raise RuntimeError("crash before any cost")
        import agents.run_budget as RB
        orig = RB.crashed_session_cost

        async def no_cost(options, sid, **k):
            return None, "result"
        RB.crashed_session_cost = no_cost
        try:
            t = SdkTasks(d, query_fn=failing_readback)
            t.set_allowance(1.25)          # no read-back budget: none is made
            r = t._session("pick_engine", "x", "y", ())
        finally:
            RB.crashed_session_cost = orig
        ok(not r.ok and abs(r.usd - 1.25) < 1e-9, f"S3 a crash with no readable cost is charged the whole allowance ({r.usd})")
        t = SdkTasks(d, query_fn=stream(init, res))
        t.set_allowance(0.0)
        r = t._session("pick_engine", "x", "y", ())
        ok(not r.ok and r.usd == 0.0 and "not made" in r.note, "S4 no allowance: the call is not made")
        led = json.load(open(os.path.join(d, "budget_ledger.json")))
        ok(len(led["sessions"]) == 3 and abs(led["spent_usd"] - (0.42 + 0.42 + 1.25)) < 1e-9,
           f"S5 every charge is in budget_ledger.json ({led['spent_usd']})")
        R = enforcement.REPO_ROOT
        ok(not _answer_key(os.path.join(R, "data", "evals", "q", "districts.csv"), d)
           and _answer_key(os.path.join(R, "evals", "q", "rubric.yaml"), d) and _answer_key("x/_truth/truth.json", d)
           and _answer_key("evals/q/generate.py", d),
           "S9 the questions' public inputs (data/evals/) are readable; evals/ and _truth are not")
        ok("{settings}" not in PROMPTS["fix"].format(tid="T", brief="b", findings=[], evidence="", settings="a.b", out=OUT)
           and "engine: auto" in PROMPTS["draft_spec"], "S10 the prompts follow the analysis backend's schema")

        async def crash_no_sid(prompt=None, options=None):
            raise RuntimeError("crash before the conversation started")
            yield  # noqa: unreachable -- makes this an async generator
        t = SdkTasks(d, query_fn=crash_no_sid)
        t.set_allowance(1.75)
        r = t._session("pick_engine", "x", "y", ())
        ok(abs(r.usd - 1.75) < 1e-9, f"S6 (Codex R2-2) no cost and no session id: the whole allowance, not a duration estimate ({r.usd})")

        probes = []

        async def crash_then_probe(prompt=None, options=None):
            if getattr(options, "resume", None):
                probes.append(options.max_budget_usd)
                yield NS(total_cost_usd=0.9, session_id="s9", is_error=False)
                return
            yield NS(subtype="init", data={"session_id": "s9"})
            raise RuntimeError("crash after start")
        t = SdkTasks(d, query_fn=crash_then_probe)
        t.set_allowance(2.0, readback_usd=0.2)
        r = t._session("pick_engine", "x", "y", ())
        ok(probes == [0.2] and abs(r.usd - 0.9) < 1e-9,
           f"S7 (Codex R2-3) the read-back is one call capped at its own small budget ({probes}, {r.usd})")
        probes.clear()
        t = SdkTasks(d, query_fn=crash_then_probe)
        t.set_allowance(2.0, readback_usd=0.0)
        r = t._session("pick_engine", "x", "y", ())
        ok(probes == [] and abs(r.usd - 2.0) < 1e-9,
           f"S8 (Codex R2-3) with no budget for a read-back none is made; the whole allowance is charged ({probes}, {r.usd})")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_self_test() if sys.argv[1:2] == ["--self-test"] else 2)
