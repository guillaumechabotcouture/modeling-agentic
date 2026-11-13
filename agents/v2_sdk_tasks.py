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
for _p in (_REPO, os.path.join(_REPO, "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
from agents import enforcement  # noqa: E402
from agents.v2_tasks import CRITIQUE_FILE, PREFIX, TaskResult
from agents.fix_loop import FIX_PLANNER_PROMPT

TOOLS = ["Read", "Glob", "Grep", "Write", "Edit"]
OUT = "_task_output.json"
# Files only the harness writes in a v2 run.
HARNESS_FILES = ("run_state.json", "findings.jsonl", "impact_ledger.jsonl", "metadata.json", "final_gate.json",
                 "decision_spec.signoff.json", "engine_choice.json", "validator_history.jsonl", "done_when_registry.json",
                 "report.md", "allocation_optimized.csv", "data/inputs_provenance.json", "declared_findings.yaml",
                 "data/derivations_record.json", "decision_spec.signoff_sheet.md",
                 "data/input_checks.json", "data/geometry_signoff.json")
# data/derived/ (v2-derived-inputs): the tables the spec reads are written only by the harness's derivation runner.
HARNESS_PREFIXES = ("analysis/", "models/pipeline", "gate_r", "fix_plan_r", "evidence/", "data/inputs/", "data/derived/")

# The owner's decision design principles (2026-11-12): one file, the source of truth, in every task's system prompt.
PRINCIPLES_FILE = os.path.join(_REPO, ".claude", "orchestration", "decision_design_principles.md")
PRINCIPLES = open(PRINCIPLES_FILE).read().strip()

COMMON = """You are one bounded task inside a modeling run driven by code (a state machine). The harness
computes every number from the decision spec and the engine; you never type a number the report
uses and never edit harness outputs (analysis/, run_state.json, findings.jsonl, gate files).
Work only in the run directory, which is your working directory. Finish by writing the output
file named below, then stop.

""" + PRINCIPLES.replace("{", "{{").replace("}", "}}")

PROMPTS = {
    "draft_spec": COMMON + """

Task: draft decision_spec.yaml for the question, and analysis_settings.yaml with the
computational settings only ({settings}; not repeated in the spec). Follow {skill} (the schema is the
docstring of {schema}; a full example is {example}). Write `engine: auto`: the harness chooses the
engine after sign-off. The harness has copied the question's input files into data/inputs/ (same
relative paths; their sha256 in data/inputs_provenance.json): read them. You write code, never
tables. For each file the spec reads that is not a staged input (the units table, calibration
targets, cost tables), write one script under data/derive/ and declare it in data/derivations.yaml
(id, script, inputs, outputs, columns). The harness runs it as `python <script> --inputs <paths>
--outputs <paths>` in a sandbox (no network, inputs read-only, only the standard library, numpy,
pandas and pyyaml), twice, and writes its outputs to data/derived/, where you cannot write; if it
fails, the next try shows you its error. Every column of every staged table a derivation reads is in
columns.uses or in columns.ignores with a reason, and a staged table no derivation reads is in
ignored_inputs with a reason: the owner sees what you chose not to use. Bind every engine unit
attribute in decision_units.engine_attributes, or accept the engine card's default by name in
decision_units.defaults_accepted with a reason (each engine's attribute_binding.yaml lists the
defaults). Declare the calibration target's frame (the population band it measures) and, when it
differs from the engine output it is compared with, the mapping. A prior goes on an engine
parameter, never on a unit attribute bound to data. Note what each derivation does in
data/README.md. Every file the spec cites is a staged input or a derived output; paths are relative
to the run directory. Output: decision_spec.yaml, analysis_settings.yaml, data/derivations.yaml,
the scripts, and {out} = {{"ok": true, "notes": "..."}}.""",
    "pick_engine": COMMON + """

Task: choose the engine. {out_in} lists the registered engines, which are eligible, and why the
others are not. Pick one eligible engine and say why in two sentences (mechanisms the question
needs, validated benchmarks). Output: {out} = {{"engine": "<ref>", "why": "..."}}.""",
    "critique": COMMON + """

Task: {reviewer} review, round {round}. Review the computed facts: the analysis artifacts in
analysis/ (manifest.json and the *.yaml artifacts it lists), results.md, uncertainty_report.yaml,
the deliverable file the spec names, engine_choice.json, decision_spec.yaml, impact_ledger.jsonl
and the draft (manuscript.md) if it exists. The presentation review also reads findings.jsonl:
its LOW report_number_typed rows (advisory) name numbers the writer typed instead of [CLAIM:id];
raise one only if it misleads the reader. Judge what code cannot: plausibility against the domain, whether the
comparator and the objective match the question, hidden assumptions, overclaims. Write
{file} in the critique-blockers schema: reviewer, round, verdict (PASS | REVISE),
structural_mismatch {{detected: bool}}, carried_forward (round 1: []), blockers (ids {prefix}-NNN;
each with id, severity ({severities}), category ({categories}), target_stage ({stages}),
first_seen_round, claim, evidence, fix_requires, resolved (true/false), and done_when (required for
HIGH and MEDIUM). Use only those values: the gate refuses any other.
A blocker with resolved: true also needs resolved_in_round: {round} and resolved_evidence: "<where
the fix is, file and section>"; keep its done_when (the harness re-runs the test and reopens the
finding if it fails). From round 2, carried_forward has one mapping per blocker of the previous
round, never a bare id: {{id: <id>, prior_round: {prev_round}, still_present: <true if the blocker
is still open>, notes: "<one line>"}}; still_present: true needs that blocker with resolved: false,
still_present: false needs it with resolved: true.

done_when is a structured test the harness runs, never prose: a list of checks, all must pass.
  - {{file: <path in the run>, exists: true}}
  - {{file: <path>, contains: "<regex>"}}   or   {{file: <path>, lacks: "<regex>"}}
  - {{file: <x.yaml or .json>, key: a.b.0, op: "==|!=|<|<=|>|>=|between|in|matches|width_gt", value: <v>}}
  - {{file: <x.csv>, column: <name>, op: <op>, value: <v>, rows: all|any|none}}  (or agg: sum|mean|min|max|count)
  - {{judgement: "<what would show it is fixed>"}}   only when no runnable check fits; at most one
Example:
  done_when:
    - {{file: uncertainty_report.yaml, key: sampler_varied_parameters, op: ">=", value: 16}}
    - {{file: manuscript.md, contains: "decision rule"}}{feedback}
Then write {out} = {{"ok": true}}.""",
    # FIX_PLANNER_PROMPT is constant text with a literal JSON example: escape its braces so .format
    # substitutes only the two paths (PF1-PF3).
    "plan_fixes": FIX_PLANNER_PROMPT.replace("{", "{{").replace("}", "}}")
    + "\nThe packet is in {out_in}. Write your JSON to {out}.",
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
interpretation. Write every number as a reference [CLAIM:<id>] to models/claims_ledger.yaml
(the ids are listed there): the harness substitutes the value. Never type a number, and never write
a bare id such as `mix.itn`: the gate refuses a report that shows one. Mode `not_accepted`: the first section is "Status: not accepted by the final
gate", listing the open blocking findings from findings.jsonl in plain words. Mode `declared`: a
Limitations section that repeats each declaration in full with its required remedy. In both modes the
report was not accepted: never call it accepted, validated or conclusive (the terms are listed in
.claude/orchestration/acceptance_language.yaml; the gate flags them). Output: manuscript.md and {out} = {{"ok": true}}.""",
}
SEED_FILE = "world_model_seed.yaml"       # the harness validates it and copies it to analysis/world_model_seed.yaml
WORLD_MODEL_PY = os.path.join(_REPO, "toolkit", "analysis", "world_model.py")


def seed_prompt_text() -> str:
    """The world model's own documentation of its seed format and tests, read from toolkit/analysis/world_model.py's
    docstring (from "Seed (YAML)" up to the paragraph on analysis/point.yaml), so the prompt never restates the
    format. Missing markers fail closed."""
    import ast
    doc = ast.get_docstring(ast.parse(open(WORLD_MODEL_PY).read())) or ""
    a, b = doc.find("Seed (YAML)"), doc.find("analysis/point.yaml is used only")
    if a < 0 or b <= a:
        raise RuntimeError(f"{WORLD_MODEL_PY}: its docstring no longer documents the seed format where expected")
    common = COMMON.replace("{{", "{").replace("}}", "}")          # COMMON is a .format template: as delivered
    return (common + """

Task: write the world model's version 0 for this question, before any simulation: the modeller's working
understanding of how the decision works, meant to be mostly right, not precise. {out_in} holds the question, the
decision spec's options (each package and its components, the comparator, the primary outcome), the engine's card
(mechanisms, components, notes) and the sweep the harness runs after you finish: its design features (the only
names your tests may use), their ranges, the number of design points, which features the design samples
independently of the others, and whether a shadow price and an optimised plan will exist. No results exist yet,
and the harness refuses any read of analysis/ or of other runs: nothing you write can depend on a simulation
output. The spec's values, the engine's effect values and priors and the input data values are withheld on
purpose: the world model is built from literature and reasoning, then confronted with the simulation, never with its
own inputs. In the run directory only {out_in} and your own seed file are readable; the repository's code and
documentation (toolkit/, docs/, .claude/) are readable, its data/ and scripts/ are not.

Write """ + SEED_FILE + """ in the world model's format, documented below by toolkit/analysis/world_model.py itself:
- beliefs: the mechanisms that drive this decision, each with its direction, a rough magnitude band on a stated
  scale (a band, never a point value), the conditions or branching points where the answer changes, roughly
  located, its basis (literature: the citation and its table or page; data: a dataset of the run listed in
  {out_in}, its file and column, never a file the harness writes such as {out_in}) and your confidence. Mark decision_relevant: true on the beliefs the choice turns on. Cite only
  sources you know, with authors, journal and year; when unsure of a value or a location say so and set confidence
  low; never invent a citation or a table.
- open_questions: what is unknown and would change the choice if known.
- hypotheses: 5 to 8, each tied to one belief and with a machine-checkable refutation test of one of the kinds
  below, with explicit thresholds, naming only the packages and features listed in {out_in}. Each states what you
  expect the sweep to show and what would refute it. Choose each threshold from the belief, never to make the
  test easy to pass: a band wide enough to hold any plausible result tests nothing. The harness registers the
  hypotheses before the sweep runs (you do not write registered_at).

""" + doc[a:b].strip() + """

Then write {out} = {"ok": true, "notes": "<one line: what drives the choice, in your view>"}.""")


# as a .format template: every brace literal except the two fields the task method fills
PROMPTS["seed_world_model"] = seed_prompt_text().replace("{", "{{").replace("}", "}}") \
    .replace("{{out_in}}", "{out_in}").replace("{{out}}", "{out}")
OWNS = {"draft_spec": ("decision_spec.yaml", "analysis_settings.yaml", "data/derive/", "data/derivations.yaml",
                       "data/README.md"), "pick_engine": (), "plan_fixes": (),
        "critique": (), "fix": ("analysis_settings.yaml", "models/ext/"), "write": ("manuscript.md",),
        "seed_world_model": (SEED_FILE,)}


SEED_RUN_READABLE = ("_task_input.json", SEED_FILE)
SEED_REPO_READABLE = ("toolkit", "docs", ".claude")


def _seed_read_denied(tool: str, ti: dict, run_dir: str) -> str | None:
    """Why the world-model seed task may not run this Read, Grep or Glob: the seed exists before any result, so
    nothing under the run's analysis/ (the batch, the world model, the hypotheses) and nothing in another run is
    readable, and a search that would reach analysis/ is refused. The repository's code and docs are readable (after
    the evaluation answer-key rules)."""
    import glob as _glob
    why = _search_denied(tool, ti, run_dir)
    if why:
        return why
    root = os.path.realpath(run_dir)
    target = str(ti.get("file_path") or ti.get("path") or "")
    base = os.path.realpath(os.path.join(run_dir, os.path.expanduser(target))) if target else root
    if tool == "Glob" and ti.get("pattern"):
        pat = str(ti["pattern"]).replace("\\", "/")
        lit = []
        for part in pat.split("/"):
            if _glob.has_magic(part):
                break
            lit.append(part)
        if lit:
            base = os.path.realpath(os.path.join(base, "/".join(lit)) if not pat.startswith("/") else
                                    "/" + "/".join(lit).lstrip("/"))
        if ".." in pat.split("/"):
            return "the seed task may not search outside its run directory"
    analysis = os.path.join(root, "analysis")
    if _within(base, analysis):
        return "the world-model seed is written before any result: analysis/ is not readable"
    repo = os.path.realpath(_REPO)
    if not _within(base, root) and _within(base, repo) and not _within(root, base):
        # the repository's code and documentation only: no run, no template, no input data, no driver files
        rel = os.path.relpath(base, repo).replace(os.sep, "/")
        if rel.split("/")[0] not in SEED_REPO_READABLE:
            return (f"the seed task may read the repository's {', '.join(SEED_REPO_READABLE)} only: inputs, "
                    "templates and runs are withheld")
        return None
    if not _within(base, root):
        return "the seed task may read only its run directory's input and the repository's code"
    rel = os.path.relpath(base, root).replace(os.sep, "/")
    if rel not in SEED_RUN_READABLE:
        return (f"in the run directory the seed task reads only {', '.join(SEED_RUN_READABLE)}: the spec's values and "
                "the input data are withheld (the world model is confronted with the simulation, never its inputs)")
    return None


def _answer_key(target: str, run_dir: str) -> bool:
    """A path into the repository's evals/ tree or any _truth directory. data/evals/<q>/ (the
    questions' public inputs) is readable."""
    low = target.replace("\\", "/").lower()
    if "_truth" in low.split("/"):
        return True
    p = os.path.realpath(os.path.join(run_dir, os.path.expanduser(target)))
    if "_truth" in p.replace(os.sep, "/").lower().split("/"):
        return True                                    # a link resolving into an answer-key tree
    rel = os.path.relpath(p, enforcement.REPO_ROOT).replace(os.sep, "/").lower()   # case-insensitive filesystems
    return rel == "evals" or rel.startswith("evals/") or low.lstrip("./").startswith("evals/")


def _reaches_answer_keys(base: str, limit: int = 20000) -> bool:
    """Does a recursive search from `base` reach an answer key: a _truth directory or file, an evals/
    tree, or a symlink resolving into one? Fail closed past `limit` entries."""
    n = 0
    for root, dirs, files in os.walk(base, followlinks=False):
        for name in dirs + files:
            n += 1
            if n > limit:
                return True
            full = os.path.join(root, name)
            if name.lower() == "_truth" or _answer_key(full, base):
                return True
            if os.path.islink(full) and _answer_key(os.path.realpath(full), base):
                return True
    return False


def _within(p: str, root: str) -> bool:
    p, root = os.path.normcase(p).lower(), os.path.normcase(root).lower()   # case-insensitive filesystems
    return p == root or p.startswith(root.rstrip(os.sep) + os.sep)


def _search_denied(tool: str, ti: dict, run_dir: str) -> str | None:
    """Why a Read, Grep or Glob may not run. A path or pattern naming the evaluation tree is refused;
    so is any search whose root contains the repository's evals/ tree (a Grep at the repository root
    reaches evals/**/_truth whatever its pattern), unless the search is confined to the run directory
    or data/."""
    import glob as _glob
    target = str(ti.get("file_path") or ti.get("path") or "")
    pattern = str(ti.get("pattern") or "") if tool == "Glob" else ""
    filters = [str(ti.get(k) or "") for k in ("glob", "type")] + ([pattern] if pattern else [])
    if (target and _answer_key(target, run_dir)) or any(f and ("_truth" in f.lower() or _answer_key(f, run_dir))
                                                          for f in filters):
        return "evaluation answer keys are not readable"
    if tool == "Read":
        return None
    base = os.path.realpath(os.path.join(run_dir, os.path.expanduser(target))) if target else os.path.realpath(run_dir)
    if tool == "Glob" and pattern:
        # A pattern can climb or be absolute: its literal prefix is part of the search root.
        lit = []
        for part in pattern.replace("\\", "/").split("/"):
            if _glob.has_magic(part):
                break
            lit.append(part)
        prefix = "/".join(lit)
        if prefix:
            base = os.path.realpath(os.path.join(base, prefix) if not pattern.startswith("/") else "/" + prefix.lstrip("/"))
    repo = os.path.realpath(enforcement.REPO_ROOT)
    evals = os.path.join(repo, "evals")
    confined = _within(base, os.path.realpath(run_dir)) or _within(base, os.path.join(repo, "data"))
    if not confined and _within(evals, base):
        return (f"{tool} from {base} would search the evaluation tree (evals/); confine it to the run directory "
                "or data/")
    if os.path.isdir(base) and _reaches_answer_keys(base):
        return f"{tool} from {base} can reach an answer-key file (a _truth tree or a link to one)"
    return None


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


def _sio():
    """scripts/lib/safe_io.py: run-dir writes with no link followed (follow-up to review round 5)."""
    scripts = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    from lib import safe_io
    return safe_io


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
        _sio().remove_path(out, self.run_dir)

        async def pre(input_data, tool_use_id, context):
            name = input_data.get("tool_name")
            ti = input_data.get("tool_input") or {}
            if name not in TOOLS:
                # Fail closed (main #97): no shell, so no full pipeline build and no background call.
                return enforcement.deny_output(f"{kind}: {name} is not one of this task's tools ({', '.join(TOOLS)})")
            if name in ("Write", "Edit"):
                why = _allowed(ti.get("file_path", ""), self.run_dir, owns)
                if why:
                    return enforcement.deny_output(f"{kind}: {why}")
            if name in ("Read", "Grep", "Glob"):
                why = (_seed_read_denied if kind == "seed_world_model" else _search_denied)(name, ti, self.run_dir)
                if why:
                    return enforcement.deny_output(why)
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
            _sio().remove_path(out, self.run_dir)
        return TaskResult(True, output, usd, str(output.get("notes") or output.get("changed") or "")[:300], seen["sid"])

    def _input(self, obj) -> str:
        name = "_task_input.json"
        _sio().write_nofollow(self.run_dir, name, json.dumps(obj, indent=1, default=str).encode(), atomic=True)
        return name

    def draft_spec(self, question, run_dir, feedback):
        R = enforcement.REPO_ROOT
        p = f"Question:\n{question}\n" + (f"\nThe last draft failed validation:\n- " + "\n- ".join(feedback) if feedback else "")
        return self._session("draft_spec", PROMPTS["draft_spec"].format(
            settings=_settings_text(), skill=os.path.join(R, ".claude", "skills", "decision-spec", "SKILL.md"),
            schema=os.path.join(R, "toolkit", "spec", "schema.py"), example=os.path.join(R, "spike", "decision_spec.v2.yaml"),
            repo=R, out=OUT), p, OWNS["draft_spec"])

    def sign_spec(self, run_dir):
        _sio().write_nofollow(run_dir, "decision_spec.signoff.json", json.dumps(
            {"by": "lead", "note": "lead mode: the harness accepts the drafted spec"}).encode(), atomic=True)
        return TaskResult(True, None, 0.0, "lead mode")

    def pick_engine(self, spec, eligible):
        inp = self._input(eligible)
        return self._session("pick_engine", PROMPTS["pick_engine"].format(out_in=inp, out=OUT),
                             "Choose the engine.", OWNS["pick_engine"], enforcement.MODEL_FAST)

    def critique(self, run_dir, reviewer, round_n, feedback=None):
        import validate_critique_yaml as V
        f = CRITIQUE_FILE[reviewer]
        model = enforcement.MODEL_FAST if reviewer == "critique-presentation" else enforcement.MODEL_DEEP
        fb = ("\n\nThis is a repair: the gate refused your file. These are all of its errors; fix every one, "
              "check the whole file against the schema above, and keep the review:\n- "
              + "\n- ".join(feedback)) if feedback else ""
        return self._session("critique", PROMPTS["critique"].format(
            reviewer=reviewer, round=round_n, prev_round=max(round_n - 1, 0), file=f, prefix=PREFIX[reviewer],
            out=OUT, feedback=fb,
            severities=" | ".join(sorted(V.VALID_SEVERITY)), categories=" | ".join(sorted(V.VALID_CATEGORY)),
            stages=" | ".join(sorted(V.VALID_TARGET_STAGE))),
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

    def write(self, run_dir, mode, feedback=None):
        fb = ("\n\nThis is a repair: the report was not accepted, and the harness will not deliver it while it "
              "uses acceptance language. Rewrite these passages (say what was tested and what is declared), "
              "keep everything else:\n- " + "\n- ".join(feedback)) if feedback else ""
        return self._session("write", PROMPTS["write"].format(mode=mode, out=OUT) + fb, "Write the draft.",
                             OWNS["write"])

    def seed_world_model(self, context, feedback=None):
        """The world model's version 0 (docs/design/v2-progressive-steps.md §4): beliefs and 5-8 pre-registrable
        hypotheses written before any result exists, to world_model_seed.yaml. `context`: the question, the options,
        the engine card and the sweep's design features (no results). `feedback`: the harness's refusal, for the one
        repair."""
        inp = self._input(context)
        fb = ("\n\nThis is a repair: the harness refused your " + SEED_FILE + ". These are all of its errors; fix "
              "every one and keep the rest of your world model:\n- " + "\n- ".join(feedback)) if feedback else ""
        return self._session("seed_world_model", PROMPTS["seed_world_model"].format(out_in=inp, out=OUT) + fb,
                             "Write the world-model seed.", OWNS["seed_world_model"])


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
    # Owner, 2026-11-12: the decision design principles are the default of every task (one file, the source of truth).
    try:
        ptxt = open(PRINCIPLES_FILE).read().strip()
        # As delivered: the templates hold the file with its braces doubled for .format().
        every = {k: (ptxt in v.replace("{{", "{").replace("}}", "}")) for k, v in PROMPTS.items()}
        banned = [w for w in ("malaria", "nigeria", "lga", "pbo", "smc", "itn", "net ", "anopheles")
                  if w in ptxt.lower()]
        ok(len(ptxt) > 500 and all(every.values()),
           f"DP1 every v2 task prompt carries the decision design principles from {PRINCIPLES_FILE}: {every}")
        ok(not banned, f"DP2 the principles name no disease, place or intervention (disease-agnostic): {banned}")
    except Exception as e:  # noqa: BLE001 -- before the file and the wiring exist this is the failure
        ok(False, f"DP1/DP2: {type(e).__name__}: {e}")
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
        ok(_answer_key(os.path.join(R, "EVALS", "q", "rubric.yaml"), d) and _answer_key("data/_TRUTH/t.json", d)
           and _allowed("data/inputs/x.csv", d, ("data/",)) and _allowed("data/inputs_provenance.json", d, ("data/",))
           and _allowed("data/units.csv", d, ("data/",)) is None,
           "S12 (Codex #95) answer keys refused in any case; the staged inputs and their manifest are the harness's")
        # S13: a search whose root contains the repository's evals/ tree is refused (a Grep at the repo
        # root reaches evals/**/_truth whatever its pattern); one confined to the run or data/ is not.
        rd = os.path.join(R, "runs", "x")
        cases = {
            "grep repo root": (("Grep", {"path": R, "pattern": "optimum"}), True),
            "glob repo root": (("Glob", {"path": R, "pattern": "**/truth.json"}), True),
            "glob climbing pattern": (("Glob", {"pattern": "../../**/truth.json"}), True),
            "grep above the repo": (("Grep", {"path": os.path.dirname(R), "pattern": "x"}), True),
            "grep run dir": (("Grep", {"path": rd, "pattern": "optimum"}), False),
            "grep default (run dir)": (("Grep", {"pattern": "optimum"}), False),
            "glob data/": (("Glob", {"path": os.path.join(R, "data"), "pattern": "**/*.csv"}), False),
            "read a data file": (("Read", {"file_path": os.path.join(R, "data", "evals", "q", "a.csv")}), False),
        }
        got = {k: bool(_search_denied(t, ti, rd)) == want for k, ((t, ti), want) in cases.items()}
        ok(all(got.values()), f"S13 searches that reach evals/ are refused, confined ones are not {got}")
        # S14-S15: Codex round 2. A _truth tree or a link into one under an allowed root, a Grep glob
        # naming _truth, and a symlinked file resolving into _truth are refused.
        rr = os.path.join(d, "rr")
        os.makedirs(os.path.join(rr, "data", "private", "_truth"))
        open(os.path.join(rr, "data", "private", "_truth", "secret.json"), "w").write("{}")
        os.makedirs(os.path.join(rr, "run"))
        os.symlink(os.path.join(rr, "data", "private", "_truth", "secret.json"), os.path.join(rr, "run", "alias.json"))
        ok(_search_denied("Grep", {"path": os.path.join(rr, "data"), "pattern": ".", "glob": "**/_truth/**"}, rr)
           and _search_denied("Grep", {"path": os.path.join(rr, "data"), "pattern": "."}, rr)
           and _search_denied("Grep", {"path": os.path.join(rr, "run"), "pattern": "."}, os.path.join(rr, "run")),
           "S14 a search under an allowed root that can reach a _truth tree (directly, by glob, or by a link) is refused")
        ok(_search_denied("Read", {"file_path": "alias.json"}, os.path.join(rr, "run")),
           "S15 a Read of a link resolving into _truth is refused")
        import validate_critique_yaml as V
        cp = PROMPTS["critique"].format(reviewer="critique-domain", round=1, prev_round=0, file="f", prefix="D", out=OUT,
                                        feedback="\n- X", severities="H", categories=" | ".join(sorted(V.VALID_CATEGORY)),
                                        stages="S")
        ok(all(c in cp for c in V.VALID_CATEGORY) and "- X" in cp,
           "S11 the critique prompt lists the gate's categories and carries a repair's errors")
        # S17 (two v2 eval runs, 2026-11-05): critics resolved blockers without resolved_in_round or
        # resolved_evidence and wrote carried_forward as bare ids. The prompt states both requirements
        # with the round filled in, and the repair says its list is every error in the file.
        cp2 = PROMPTS["critique"].format(reviewer="critique-domain", round=2, prev_round=1, file="f", prefix="D",
                                         out=OUT, feedback="", severities="H", categories="C", stages="S")
        ok(all(k in cp2 for k in ("resolved_in_round: 2", "resolved_evidence", "prior_round: 1", "still_present",
                                  "never a bare id")),
           "S17 the critique prompt states what a resolved blocker and a carried_forward entry need")
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
        # S16 (main #97 ported): a task has file tools only. Should the CLI offer any other tool (Bash,
        # a subagent, a notebook edit), the hook refuses it, so no task can run a full
        # `scripts/pipeline.py run <run_dir>` or leave a background call no hook sees end.
        verdicts = {}

        async def probe_hook(prompt=None, options=None):
            pre = options.hooks["PreToolUse"][0].hooks[0]
            for name, ti in (("Bash", {"command": f"python3 scripts/pipeline.py run {d}"}),
                             ("Agent", {"subagent_type": "modeler", "prompt": "x"}),
                             ("NotebookEdit", {"notebook_path": os.path.join(d, "n.ipynb")}),
                             ("Read", {"file_path": os.path.join(d, "budget_ledger.json")})):
                out = await pre({"tool_name": name, "tool_input": ti}, "t1", None)
                verdicts[name] = (out.get("hookSpecificOutput") or {}).get("permissionDecision") == "deny"
            yield NS(total_cost_usd=0.01, session_id="s16", is_error=False)
        t = SdkTasks(d, query_fn=probe_hook)
        t.set_allowance(1.0)
        t._session("pick_engine", "x", "y", ())
        ok(verdicts == {"Bash": True, "Agent": True, "NotebookEdit": True, "Read": False},
           f"S16 tools outside the task's file tools are refused at PreToolUse; a Read is not ({verdicts})")
        # DI1 (docs/design/v2-derived-inputs.md §3.1): the planner writes code, never tables. A Write to the derived
        # tables (or anywhere else in data/) is refused; the derivation scripts, their declaration and the note are
        # the task's own.
        own = OWNS["draft_spec"]
        verdicts = {}

        async def probe_draft(prompt=None, options=None):
            pre = options.hooks["PreToolUse"][0].hooks[0]
            for rel in ("data/derived/units.csv", "data/units.csv", "data/derivations_record.json",
                        "data/derive/units.py", "data/derivations.yaml", "data/README.md"):
                out = await pre({"tool_name": "Write", "tool_input": {"file_path": os.path.join(d, rel)}}, "t2", None)
                verdicts[rel] = (out.get("hookSpecificOutput") or {}).get("permissionDecision") == "deny"
            yield NS(total_cost_usd=0.01, session_id="s17", is_error=False)
        t = SdkTasks(d, query_fn=probe_draft)
        t.set_allowance(1.0)
        t._session("draft_spec", "x", "y", own)
        ok(verdicts == {"data/derived/units.csv": True, "data/units.csv": True, "data/derivations_record.json": True,
                        "data/derive/units.py": False, "data/derivations.yaml": False, "data/README.md": False},
           f"DI1 a Write to data/derived/ (or a table anywhere in data/) is refused at PreToolUse; the derivation "
           f"code and its declaration are the draft task's ({verdicts})")
        ok("derivations.yaml" in PROMPTS["draft_spec"] and "data/derive/" in PROMPTS["draft_spec"],
           "DI1b the draft_spec prompt says the derived tables come from declared derivations the harness runs")
        # PF1-PF4 (2026-11-02 v2 run: FIX_LOOP crashed with KeyError '"tasks"'): every task method is driven
        # through a fake _session with its real arguments, so the real prompt assembly runs in CI, not
        # the fix loop's fake task layer. A template field that is not a plain name is a stray brace.
        import re as _re
        import string as _string
        stray = {}
        for k, tmpl in PROMPTS.items():
            try:
                fields = {f for _, f, _, _ in _string.Formatter().parse(tmpl) if f is not None}
            except ValueError as e:
                fields = {f"<unparseable: {e}>"}
            bad = sorted(f for f in fields if not _re.fullmatch(r"[a-z_]+", f))
            if bad:
                stray[k] = bad
        ok(not stray, f"PF1 no prompt template has a stray brace (a field that is not a plain name) {stray}")
        seen_prompts, errors = {}, {}
        t = SdkTasks(d, query_fn=stream(init, res))

        def fake_session(kind, system, prompt, owns, model=None):
            seen_prompts[kind] = system
            return TaskResult(True, {"ok": True}, 0.0, "fake")
        t._session = fake_session
        calls = {
            "draft_spec": lambda: t.draft_spec("Q?", d, ["e1"]),
            "pick_engine": lambda: t.pick_engine({}, {"eligible": ["e"]}),
            "critique": lambda: t.critique(d, "critique-domain", 1, ["fb"]),
            "plan_fixes": lambda: t.plan_fixes({"round": 1, "findings": [{"id": "R-001"}]}),
            "fix": lambda: t.fix({"id": "T1", "brief": "b {x}", "findings": ["R-001"], "owner_state": "MODEL"},
                                 d, {"k": "{v}"}),
            "write": lambda: t.write(d, "accepted"),
        }
        # first, so plan_fixes' packet is the last _task_input.json written (PF4 reads it)
        calls = dict({"seed_world_model": lambda: t.seed_world_model({"question": "Q?", "packages": {"none": []}},
                                                                     ["fb {x}"])}, **calls)
        for k, call in calls.items():
            try:
                call()
            except Exception as e:  # noqa: BLE001 -- the failure is the finding
                errors[k] = f"{type(e).__name__}: {e}"
        ok(not errors and set(seen_prompts) == set(PROMPTS),
           f"PF2 every PROMPTS entry is assembled by its task method with real arguments "
           f"(errors {errors}; not driven {sorted(set(PROMPTS) - set(seen_prompts))})")
        ok(seen_prompts.get("plan_fixes") == FIX_PLANNER_PROMPT
           + "\nThe packet is in _task_input.json. Write your JSON to _task_output.json.",
           "PF3 the fix planner's prompt is FIX_PLANNER_PROMPT byte for byte, then the two paths")
        pk = json.load(open(os.path.join(d, "_task_input.json")))
        ok('{"tasks": [{"id": "T1"' in (seen_prompts.get("plan_fixes") or "") and pk["findings"] == [{"id": "R-001"}],
           "PF4 plan_fixes keeps the plan's JSON example and writes the packet the prompt names")
    # World-model seed task (F6 world-model trials): the world model's own seed documentation in the prompt, no results
    # readable, one owned output outside the harness's analysis/ tree.
    try:
        import ast
        wm_doc = ast.get_docstring(ast.parse(open(os.path.join(_REPO, "toolkit", "analysis", "world_model.py")).read()))
        sp = seed_prompt_text()
        ok("Seed (YAML)" in sp and "effect_band {package, band, min_share, where?}" in sp
           and wm_doc[wm_doc.index("Tests (each declares"):wm_doc.index("Statuses:")] in sp and PRINCIPLES in sp,
           "SW1 the seed prompt carries the world model's own seed and test documentation (read from world_model.py) "
           "and the decision design principles")
        ok("results" in sp.lower() and "5 to 8" in sp and SEED_FILE in sp,
           "SW1b the seed prompt says no results are readable, asks for 5 to 8 hypotheses and names its output file")
        with tempfile.TemporaryDirectory() as d:
            rd = os.path.join(d, "run")
            os.makedirs(os.path.join(rd, "analysis", "batch"))
            got = {
                "results": _seed_read_denied("Read", {"file_path": "analysis/batch/results.jsonl"}, rd),
                "analysis glob": _seed_read_denied("Glob", {"pattern": "analysis/**"}, rd),
                "grep root": _seed_read_denied("Grep", {"pattern": "rho"}, rd) is None,
                "spec": _seed_read_denied("Read", {"file_path": "decision_spec.yaml"}, rd),
                "input": _seed_read_denied("Read", {"file_path": "_task_input.json"}, rd) is None,
                "sibling run": _seed_read_denied("Read", {"file_path": os.path.join(d, "other", "analysis",
                                                                                     "hypotheses.yaml")}, rd),
                "climb": _seed_read_denied("Glob", {"pattern": "../**/world_model.yaml"}, rd),
                "repo world model": _seed_read_denied("Read", {"file_path": os.path.join(
                    _REPO, "toolkit", "analysis", "world_model.py")}, rd) is None,
            }
            want = {"results": True, "analysis glob": True, "grep root": False, "spec": True, "input": True, "sibling run": True,
                    "climb": True, "repo world model": True}
            ok({k: bool(v) for k, v in got.items()} == want,
               f"SW2 the seed task cannot read results (analysis/, other runs, a climb out of the run) or the spec, and "
               f"can read its input and the repository's code: {got}")
            # a recursive search at the run root would reach analysis/: refused unless it is confined elsewhere
            ok(_allowed(SEED_FILE, rd, OWNS["seed_world_model"]) is None
               and _allowed("analysis/world_model_seed.yaml", rd, OWNS["seed_world_model"]) is not None,
               "SW3 the seed task writes only its own seed file; analysis/ stays the harness's")
    except Exception as e:  # noqa: BLE001 -- before the task exists this is the failure
        ok(False, f"SW1-SW3 seed task: {type(e).__name__}: {e}")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_self_test() if sys.argv[1:2] == ["--self-test"] else 2)
