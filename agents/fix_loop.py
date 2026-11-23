#!/usr/bin/env python3
"""v2 C (S3): the fix loop. One finding per task, one rebuild, one measured impact.

    python3 agents/fix_loop.py --self-test

Design: docs/design/v2c-orchestration.md §5 and stage-machine.md §3.4. The LLM fix
planner gets a packet (the open findings from findings.jsonl, the budget, the impact
history, the gate's verdict) and returns ordered tasks. The harness validates the plan,
then for each task: snapshot the run's files, let the agent task edit, diff the files
(what changed is measured, not reported), rebuild through the S1 pipeline runner, re-run
the finding's checks, and append a row to impact_ledger.jsonl. A task whose check fails
goes back to its agent once with the evidence.

FIX_PLANNER_PROMPT is the whole prompt the planner sees: what is judgement (rethink or
patch, grouping, order, decision test instead of fix). Orchestration is the machine's.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
for _p in (REPO, os.path.join(REPO, "scripts"), HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import findings_ledger as FL  # noqa: E402

IMPACT = "impact_ledger.jsonl"
# What a fix (or decision_test) task may edit: a file, or a folder when the entry ends in "/". The fix task's write
# scope (agents/v2_sdk_tasks.py OWNS["fix"]) is this tuple, so the planner, plan validation and the tools agree.
# Harness baseline hb1: every planned fix named files outside it (analysis outputs, data/README.md, harness code), and
# every one changed nothing.
FIX_EDITABLE = ("analysis_settings.yaml", "models/ext/")
ACTIONS = ("fix", "rethink", "decision_test")
HARNESS_GAP = FL.HARNESS_GAP          # the ledger event a declared harness gap leaves on its finding
RETHINK_STATES = ("SPEC", "ENGINE_CHOICE")
# F6 run 3: a write-up finding (report or manuscript text) is the WRITE stage's. A FIX_LOOP task may edit only
# analysis_settings.yaml and models/ext/, so a task for one can never pass its check; the writer gets it instead.
WRITE_STATES = ("WRITE", "PRESENTATION")
# F6 run 3: a finding whose required remedy is an owner amendment of the signed spec is the owner's, not a SPEC
# rethink's. The critics are told to write "owner amendment" in fix_requires when the fix needs the signed spec changed
# (agents/v2_sdk_tasks.py, critique prompt).
_OWNER_AMENDMENT = FL.OWNER_AMENDMENT_RE     # the documented prefix only (Codex #258 P2-4)


def is_write_up(f) -> bool:
    """A finding (ledger Finding or packet row) only the writer can fix."""
    st = f.get("owner_state") if isinstance(f, dict) else getattr(f, "owner_state", None)
    return st in WRITE_STATES


def needs_owner_amendment(f) -> bool:
    """A finding whose required remedy is an owner amendment and which the owner has not answered yet."""
    g = f.get if isinstance(f, dict) else (lambda k, _f=f: getattr(_f, k, None))
    return g("resolution_type") != "owner_decision" and bool(_OWNER_AMENDMENT.search(str(g("fix_requires") or "")))
# Files the harness writes; never counted as an agent's change.
_HARNESS_OWNED = ("findings.jsonl", IMPACT, "budget_ledger.json", "_task_input.json", "_task_output.json", "run_state.json", "trace.jsonl", "validator_history.jsonl",
                  "done_when_registry.json", "open_problems.md", "open_problems.json", "final_gate.json",
                  "gate_r", "fix_plan_r", "fix_progress_r", "models/pipeline_state.json", "models/pipeline_report.json", "analysis/")

FIX_PLANNER_PROMPT = """\
You are the fix planner of a modeling run. The harness runs the pipeline; you decide what
to fix next and how. You receive one JSON packet:

  round, max_rounds        the gate round just run and the cap
  gate                     the gate's action, rule and rationale (computed by code; not yours to change),
                           escalation_required, stalled findings
  findings                 the open findings: id, kind, severity, blocking, owner_state (the stage
                           that can change it), resolution_type (fix | decision_test | owner_decision),
                           scope_declarable, claim, done_when, first_seen_round, attempts so far
  write_up                 findings in the report or manuscript text: the WRITE stage fixes them; never plan them
  owner_amendments         findings whose remedy is an owner amendment of the signed spec: the owner decides
                           them; never plan them, and never rethink SPEC for them
  impact_history           one row per earlier fix task: findings, files changed, headline before and
                           after, whether its check passed, findings it opened
  budget                   spent, cap and what is left in USD
  edit_scope               what a fix or decision_test task may edit: `files` (a file, or a folder when it ends
                           in "/") and `settings` (the only leaves analysis_settings.yaml may hold)

Return only JSON:

  {"tasks": [{"id": "T1", "findings": ["R-005"], "action": "fix" | "rethink" | "decision_test",
              "owner_state": "<state>", "brief": "<what to change and why, citing the evidence>",
              "files": ["<files the change touches>"]}],
   "deferred": [{"finding": "<id>", "reason": "<why not in this loop>"}],
   "harness_gaps": [{"finding": "<id>", "reason": "<why no file in edit_scope can fix it>",
                     "needs": ["<the harness file or capability the fix needs>"]}]}

Rules:
- A fix or decision_test task lists in `files` every file it changes, and each is inside edit_scope.
  Nothing else is: not the signed decision_spec.yaml, the data, the outputs the harness writes (analysis/,
  results.md, the claims files, the allocation and uncertainty tables) or the harness code (toolkit/,
  scripts/, agents/). The harness rebuilds every output from the spec, the settings and the engine.
- A finding whose fix needs a change outside edit_scope is a harness gap, not a task: list it in
  `harness_gaps` with the reason and what it needs. It counts as covered, and the harness records it
  for the owner. Do not plan a task that cannot change anything.
- One finding per task. Group findings in one task only when the same change fixes them and
  they share files; then list those files.
- Every blocking finding is in a task or in harness_gaps. A non-blocking finding may be deferred with a reason.
- Order: what unblocks most first; a finding that other findings depend on before them.
- Rethink, not patch, when the finding is structural, when the impact history shows the same
  finding patched twice without its check passing, or when several blocking findings point at one
  mechanism. A rethink names owner_state SPEC (the decision spec is wrong) or ENGINE_CHOICE
  (the engine or its configuration cannot represent what the question needs).
- A SPEC rethink also carries "patch": the amendment it asks for, in machine-readable form:
  {"id": "<short id>", "reason": "<why, in words>", "spec": [{"set": "<path>", "value": <new value>},
  {"add": "<path>", "value": <value>}, {"remove": "<path>"}]}. A path is a dotted key of
  decision_spec.yaml; a list element by index (".0") or by "[key=value]". "set" replaces a value that
  exists, "add" appends to a list or adds a key a mapping lacks, "remove" deletes one. Change only what
  the brief names. The amendment goes to a new run, never this one, and the owner reviews it.
- decision_test instead of fix when the finding cannot be fixed with the data and engines
  available: the brief says which quantity to vary and what would change the decision. Never
  for a finding with scope_declarable false.
- An owner_decision finding goes to the owner; do not plan a task for it.
- Use the impact history: do not repeat a change that moved nothing; prefer changes whose
  measured effect on the headline is understood.
- Stay inside the budget: a loop that cannot finish is worse than a shorter one.
"""
# The owner's decision design principles (2026-11-12): the planner judges and plans by them too (one file, the
# source of truth for every v2 task's prompt; agents/v2_sdk_tasks.py PRINCIPLES_FILE).
FIX_PLANNER_PROMPT += "\n" + open(os.path.join(REPO, ".claude", "orchestration",
                                                 "decision_design_principles.md")).read().strip() + "\n"


# ------------------------------------------------------------------ packet and plan
def attempts(run_dir: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in impact_rows(run_dir):
        for f in row.get("findings") or []:
            out[f] = out.get(f, 0) + 1
    return out


def impact_rows(run_dir: str) -> list[dict]:
    p = os.path.join(run_dir, IMPACT)
    rows = []
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def build_packet(run_dir: str, ledger: dict, round_n: int, max_rounds: int, gate: dict | None,
                 budget: dict | None, history_rows: int = 30, settings=None) -> dict:
    att = attempts(run_dir)
    fs, write_up, owner = [], [], []
    current_gaps = FL.harness_gaps(ledger)
    for f in FL.open_findings(ledger):
        row = {"id": f.id, "kind": f.kind, "severity": f.severity, "blocking": f.blocking,
               "owner_state": f.owner_state, "resolution_type": f.resolution_type,
               "scope_declarable": f.scope_declarable, "label": FL.short_label(f.claim), "claim": f.claim,
               "fix_requires": f.fix_requires, "done_when": f.done_when,
               "first_seen_round": f.first_seen_round, "attempts": att.get(f.id, 0)}
        gaps = [e for e in f.events or [] if isinstance(e, dict) and e.get("status") == HARNESS_GAP]
        if gaps:                                   # declared in an earlier round: the planner sees why
            row["harness_gap"] = gaps[-1].get("note") or "declared"
        if f.id in current_gaps:                   # and it still reads as declared (hb2 fix 4)
            row["harness_gap_current"] = True
        if needs_owner_amendment(f):           # the owner's decision first, whatever the stage (Codex #258 P2-4)
            owner.append(row)
        elif is_write_up(f):
            write_up.append(row)
        else:
            fs.append(row)
    g = gate or {}
    by_id = lambda rows: sorted(rows, key=lambda r: r["id"])   # noqa: E731
    return {"round": round_n, "max_rounds": max_rounds,
            # not plannable here: the WRITE stage's findings, and those the owner must amend (OWNER_DECISION)
            "write_up": by_id(write_up), "owner_amendments": by_id(owner),
            "gate": {"action": g.get("action"), "rule": g.get("rule_matched"), "rationale": g.get("rationale"),
                     "escalation_required": g.get("escalation_required"),
                     "stalled": g.get("stalled_findings") or []},
            "findings": fs, "impact_history": impact_rows(run_dir)[-history_rows:], "budget": budget or {},
            "edit_scope": edit_scope(settings)}


def edit_scope(settings=None) -> dict:
    """What a fix task may edit, as the planner's packet gives it: FIX_EDITABLE and the computational leaves of
    analysis_settings.yaml. `settings` is the running machine's backend's list (its settings_paths); without one,
    the default backend's (the list the fix worker's prompt is given)."""
    if settings is None:
        from agents import analysis_backend as AB
        settings = getattr(AB.backend(), "settings_paths", AB.SETTINGS_PATHS)
    return {"files": list(FIX_EDITABLE), "settings": sorted(settings)}


def task_scope(task: dict) -> tuple:
    """What the fix worker for `task` may write: the in-scope files its plan names (normalised; a folder keeps its
    trailing "/", so the write hook reads it as a folder), or the whole FIX_EDITABLE when it names none (only the
    harness default plan's form validates that way)."""
    import posixpath
    named = [posixpath.normpath(p) + ("/" if p.endswith("/") else "") for p in task.get("files") or []
             if in_edit_scope(p)]
    named = [p + "/" if p + "/" in FIX_EDITABLE else p for p in named]
    return tuple(dict.fromkeys(named)) or FIX_EDITABLE


def in_edit_scope(path) -> bool:
    """True when `path` (relative to the run) is a file a fix task may edit. An absolute path, a `..` part or a
    non-string is outside; `./` prefixes are ignored. Taken as written, as the fix task's write hook takes it: a
    backslash or surrounding space is outside too (Codex round 2 P3)."""
    if not isinstance(path, str) or not path.strip() or path != path.strip() or "\\" in path:
        return False
    p = path
    if p.startswith("/") or ".." in p.split("/"):
        return False
    import posixpath
    n = posixpath.normpath(p)
    return any(n == e.rstrip("/") or n.startswith(e) if e.endswith("/") else n == e for e in FIX_EDITABLE)


def validate_plan(plan, packet: dict) -> list[str]:
    """Problems with a planner's output (empty: the plan can run)."""
    if not isinstance(plan, dict) or not isinstance(plan.get("tasks"), list):
        return ["the plan must be a JSON object with a `tasks` list"]
    by_id = {f["id"]: f for f in packet["findings"]}
    elsewhere = {f["id"]: "is a write-up finding: the WRITE stage takes it, not a FIX_LOOP task"
                 for f in packet.get("write_up") or []}
    elsewhere.update({f["id"]: "needs an owner amendment of the signed spec: the owner decides it (OWNER_DECISION), "
                               "not a task or a rethink" for f in packet.get("owner_amendments") or []})
    errs, used, tids = [], set(), set()
    for t in plan["tasks"]:
        if not isinstance(t, dict):
            errs.append("a task is not an object")
            continue
        tid = t.get("id")
        if not isinstance(tid, str) or tid in tids:
            errs.append(f"task id {tid!r} missing or repeated")
        tids.add(tid)
        fids = t.get("findings")
        if not isinstance(fids, list) or not fids:
            errs.append(f"{tid}: `findings` must list at least one finding")
            continue
        if t.get("action") not in ACTIONS:
            errs.append(f"{tid}: action {t.get('action')!r} not in {ACTIONS}")
        for fid in fids:
            if fid in elsewhere:
                errs.append(f"{tid}: {fid} {elsewhere[fid]}")
            elif fid not in by_id:
                errs.append(f"{tid}: unknown finding {fid}")
            elif fid in used:
                errs.append(f"{tid}: finding {fid} is in two tasks")
            elif by_id[fid]["resolution_type"] == "owner_decision":
                errs.append(f"{tid}: {fid} is the owner's decision, not a task")
            elif t.get("action") == "decision_test" and not by_id[fid]["scope_declarable"]:
                errs.append(f"{tid}: {fid} is not scope-declarable; it needs a fix, not a decision test")
            used.add(fid)
        if len(fids) > 1 and not (isinstance(t.get("files"), list) and t["files"]):
            errs.append(f"{tid}: groups {len(fids)} findings, so it must name the files they share")
        if t.get("action") == "rethink" and t.get("owner_state") not in RETHINK_STATES:
            errs.append(f"{tid}: a rethink names owner_state SPEC or ENGINE_CHOICE")
        if not str(t.get("brief") or "").strip():
            errs.append(f"{tid}: empty brief")
        if t.get("action") != "rethink":
            # hb1 (ranked fix 8): what the task changes must be something a fix task can edit (FIX_EDITABLE)
            files = t.get("files")
            if not isinstance(files, list) or not files:
                errs.append(f"{tid}: a {t.get('action')} task must name the files it changes (edit_scope)")
            else:
                out = [str(p) for p in files if not in_edit_scope(p)]
                if out:
                    errs.append(f"{tid}: {', '.join(out)[:200]} outside what a fix task may edit "
                                f"({', '.join(FIX_EDITABLE)}); a finding that needs another change is a harness gap: "
                                "list it in `harness_gaps` with the reason and what it needs")
    gaps = plan.get("harness_gaps")
    if gaps is not None and not isinstance(gaps, list):
        errs.append("`harness_gaps` must be a list")
        gaps = []
    declared = set()
    for g in gaps or []:
        fid = g.get("finding") if isinstance(g, dict) else None
        if not isinstance(fid, str):
            errs.append("a harness gap must name its `finding`")
            continue
        if fid in elsewhere:
            errs.append(f"harness gap {fid} {elsewhere[fid]}")
        elif fid not in by_id:
            errs.append(f"harness gap: unknown finding {fid}")
        elif by_id[fid]["resolution_type"] == "owner_decision":
            errs.append(f"harness gap {fid} is the owner's decision")
        elif fid in used:
            errs.append(f"{fid} is both in a task and a harness gap")
        elif not str(g.get("reason") or "").strip():
            errs.append(f"harness gap {fid} needs its reason")
        elif not (isinstance(g.get("needs"), list) and g["needs"]
                  and all(isinstance(x, str) and x.strip() for x in g["needs"])):
            errs.append(f"harness gap {fid}: `needs` must list the harness files or capability its fix needs")
        elif fid in declared:
            errs.append(f"harness gap {fid} is listed twice")
        else:
            declared.add(fid)
    deferred = {d.get("finding") for d in plan.get("deferred") or [] if isinstance(d, dict) and str(d.get("reason") or "").strip()}
    for f in packet["findings"]:
        if f["blocking"] and f["resolution_type"] != "owner_decision" and f["id"] not in used | declared \
                and not is_write_up(f):
            errs.append(f"blocking finding {f['id']} is in no task" + (" (blocking findings cannot be deferred)"
                                                                      if f["id"] in deferred else ""))
    return errs


def spec_patch(plan) -> dict | None:
    """lead-gaps β: the machine-readable amendment of the rethink the loop routes (its first rethink task) when that
    rethink is the spec's (toolkit/spec/amend.py's patch format; amend validates it), else None."""
    tasks = plan.get("tasks") if isinstance(plan, dict) else None
    t = next((t for t in tasks or [] if isinstance(t, dict) and t.get("action") == "rethink"), None)
    p = t.get("patch") if t is not None and t.get("owner_state") == "SPEC" else None
    return p if isinstance(p, dict) else None


def default_plan(packet: dict) -> dict:
    """The harness's own plan when the planner fails twice: one task per blocking finding, in
    the packet's order (blocking first, then severity), each a fix by its owner state."""
    tasks, gaps = [], []
    for f in packet["findings"]:
        if f.get("harness_gap_current"):           # no task for a declared harness gap (Codex r1 of #357, P2-11)
            gaps.append({"finding": f["id"], "reason": f"declared earlier: {f.get('harness_gap')}"[:300], "needs": []})
            continue
        if f["blocking"] and f["resolution_type"] != "owner_decision" and not is_write_up(f):
            tasks.append({"id": f"T{len(tasks) + 1}", "findings": [f["id"]], "action": "fix",
                          "owner_state": f["owner_state"], "brief": f"Fix {f['id']}: {f.get('label') or FL.short_label(f['claim'])}",
                          "files": list(FIX_EDITABLE)})        # all it may edit: the planner did not narrow it
    return {"tasks": tasks, "deferred": [], "by": "harness default", **({"harness_gaps": gaps} if gaps else {})}


# ------------------------------------------------------------------ measurement
def _owned(rel: str) -> bool:
    return any(rel == h or rel.startswith(h) for h in _HARNESS_OWNED) or rel.endswith(".tmp")


def snapshot(run_dir: str) -> dict[str, str]:
    out = {}
    for root, dirs, files in os.walk(run_dir):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
        for n in files:
            p = os.path.join(root, n)
            rel = os.path.relpath(p, run_dir).replace(os.sep, "/")
            if _owned(rel):
                continue
            try:
                with open(p, "rb") as f:
                    out[rel] = hashlib.sha256(f.read()).hexdigest()[:16]
            except OSError:
                continue
    return out


def changed_files(before: dict, after: dict) -> list[str]:
    return sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))


# Gate findings made from the critiques and the done_when registry (scripts/done_when.py gate_findings; the
# gate's missing-presentation-review finding): only a gate re-evaluates them.
GATE_ONLY = ("done_when_", "critique_")


def check_task(run_dir: str, task: dict, ledger: dict, gate_ids: set[str]) -> dict:
    """The task's own checks: a finding with a runnable done_when test passes when the test
    passes; a harness-check finding passes when the harness no longer reports it; anything
    else is left to the next critique or gate (None). `gate_ids` holds what the harness can
    re-check between gates (stage_machine.Machine.harness_ids); a GATE_ONLY finding is never
    among them, so its absence there is not a pass."""
    import done_when as DW
    res = {}
    for fid in task["findings"]:
        f = ledger.get(fid)
        if f is None:
            res[fid] = {"passed": False, "why": "not in the ledger"}
        elif f.done_when and DW.mechanical(f.done_when):
            ok_, why = DW.evaluate(f.done_when, run_dir)
            res[fid] = {"passed": bool(ok_), "why": why}
        elif f.source.startswith("critique"):
            res[fid] = {"passed": None, "why": "judgement: the next critique decides"}
        elif str(f.kind).startswith(GATE_ONLY):
            res[fid] = {"passed": None, "why": "only the next gate can re-check it"}
        else:
            res[fid] = {"passed": fid not in gate_ids, "why": "absent from the harness checks" if fid not in gate_ids
                        else "still reported by the harness checks"}
    return res


def _sio():
    """scripts/lib/safe_io.py: run-dir writes with no link followed (follow-up to review round 5)."""
    import sys
    scripts = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    from lib import safe_io
    return safe_io


def append_impact(run_dir: str, row: dict) -> None:
    with _sio().open_append_nofollow(run_dir, IMPACT) as f:
        f.write(json.dumps(row, sort_keys=True, default=str) + "\n")


def _read_or_none(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def _restore(run_dir: str, saved: dict) -> None:
    sio = _sio()
    for rel, text in saved.items():
        if text is None:
            sio.remove_nofollow(run_dir, rel)          # the entry itself (a link included), never its target
        else:
            sio.write_nofollow(run_dir, rel, text.encode("utf-8"), atomic=True)   # replaces a planted link


def run_loop(run_dir: str, round_n: int, max_rounds: int, gate: dict | None, tasks, rebuild, harness_ids,
             headline, budget=None, log=print, guard=None, protect=(), settings=None) -> dict:
    """One fix loop. `tasks` is the machine's Tasks (plan_fixes, fix); `rebuild(run_dir)` is the S1
    runner, returning its report; `harness_ids(run_dir)` the ledger ids the harness checks report
    now; `headline(run_dir)` the numbers to record; `budget()` -> {spent, cap, left} or None.

    `guard(run_dir)` -> problems with what the agent changed (a signed file edited, a setting
    outside the allowed ones); with problems the files in `protect` are put back as they were
    before the task, nothing is rebuilt, and the row records the rejection.

    Resumable: a task with an impact row for this round is not run again, and a task that was
    started keeps the snapshot taken before it (fix_progress_r<N>.json), so its row still
    measures the change."""
    ledger = FL.load(run_dir)
    packet = build_packet(run_dir, ledger, round_n, max_rounds, gate, budget() if budget else None, settings=settings)
    plan, errs, tries = None, [], 0
    while tries < 2:
        tries += 1
        res = tasks.plan_fixes(packet if not errs else dict(packet, plan_problems=errs))
        plan = res.output if res.ok else None
        errs = validate_plan(plan, packet) if res.ok else [f"planner task failed: {res.note}"]
        if not errs:
            break
    if errs:
        log(f"[fix loop] planner output rejected twice ({'; '.join(errs)[:300]}); harness default plan")
        plan = default_plan(packet)
    write_up = [f["id"] for f in packet["write_up"]]
    owner = [f["id"] for f in packet["owner_amendments"]]
    gaps = [g for g in plan.get("harness_gaps") or [] if isinstance(g, dict)]
    gap_ids = sorted({str(g.get("finding")) for g in gaps})
    _sio().write_nofollow(run_dir, f"fix_plan_r{round_n}.json",
                          json.dumps({"round": round_n, "plan": plan, "rejected": errs, "write_up": write_up,
                                      "owner_amendments": owner, "harness_gaps": gap_ids}, indent=1).encode(),
                          atomic=True)
    if gaps:
        # hb1 (ranked fix 8): a finding only a harness change can fix is declared with its reason on the finding
        # itself (it stays open: the gate, the report and the owner see it), never as a fix row.
        led = FL.load(run_dir)
        for g in gaps:
            f = led.get(str(g.get("finding")))
            if f is not None:
                # the gap binds the finding as it reads now (its kind and remedy class: hb2 fix 4)
                FL._event(f, round_n, HARNESS_GAP, "fix_planner",
                          f"{str(g.get('reason')).strip()} Needs: {', '.join(map(str, g.get('needs') or []))}"[:600],
                          key=FL.harness_gap_key(f))
        FL.save(run_dir, led)
        log(f"[fix loop] harness gap(s), not fix tasks: {', '.join(gap_ids)}")
    done = {r.get("task") for r in impact_rows(run_dir) if r.get("round") == round_n}
    prog_p = os.path.join(run_dir, f"fix_progress_r{round_n}.json")
    try:
        progress = json.load(open(prog_p))
    except (OSError, ValueError):
        progress = {}
    summary = {"round": round_n, "tasks": len(plan["tasks"]), "rows": 0, "passed": 0, "rethink": None,
               "skipped_budget": 0, "skipped_rethink": 0, "rejected": 0, "resumed_done": 0, "plan_by": plan.get("by", "planner"),
               "write_up": write_up, "owner_amendments": owner, "harness_gaps": gap_ids,
               # the blocking ones keep the machine off its write-up-only route (stage_machine h_FIX_LOOP)
               "harness_gaps_blocking": sorted(f["id"] for f in packet["findings"]
                                               if f["id"] in gap_ids and f.get("blocking"))}

    def out_of_budget():
        b = budget() if budget else None
        return bool(b and b.get("left") is not None and b["left"] <= 0)

    for n, t in enumerate(plan["tasks"]):
        if t["action"] == "rethink":
            # The run goes back to that state, which redoes what any later task would fix or rebuild: the loop
            # stops here (the tasks after it are counted, not run).
            summary["rethink"], summary["skipped_rethink"] = t["owner_state"], len(plan["tasks"]) - n - 1
            if t["id"] in done:
                summary["resumed_done"] += 1
            else:
                append_impact(run_dir, {"round": round_n, "task": t["id"], "findings": t["findings"],
                                        "action": "rethink", "owner_state": t["owner_state"], "passed": None,
                                        "note": "routed to " + t["owner_state"]})
            break
        if t["id"] in done:
            summary["resumed_done"] += 1
            continue
        if out_of_budget():
            summary["skipped_budget"] += 1
            append_impact(run_dir, {"round": round_n, "task": t["id"], "findings": t["findings"],
                                    "action": t["action"], "passed": None, "note": "skipped: loop budget used"})
            continue
        b = budget() if budget else None
        pend = progress.get(t["id"])
        if pend is None:
            pend = {"t0": time.time(), "usd0": b["spent"] if b else None, "before_h": headline(run_dir),
                    "before_ids": sorted(harness_ids(run_dir)), "snap": snapshot(run_dir),
                    "saved": {rel: _read_or_none(os.path.join(run_dir, rel)) for rel in protect}}
            progress[t["id"]] = pend
            _sio().publish(prog_p, json.dumps(progress, default=str), run_dir)
        evidence, rebuilt, attempts_, rejected, rep, r = None, [], 0, [], {}, None
        agent_changed: set = set()
        check, passed, now_ids = {}, False, set(pend["before_ids"])
        while True:
            attempts_ += 1
            if attempts_ > 1 and out_of_budget():
                break                                   # the retry is a paid call too
            r = tasks.fix(t, run_dir, evidence)
            agent_changed |= set(changed_files(pend["snap"], snapshot(run_dir)))   # before any rebuild
            rejected = list(guard(run_dir)) if guard else []
            if rejected:
                _restore(run_dir, pend["saved"])
                log(f"[fix loop] {t['id']} rejected: {'; '.join(rejected)[:200]}")
                check = {fid: {"passed": False, "why": "edit rejected"} for fid in t["findings"]}
                passed = False
                break
            rep = rebuild(run_dir) or {}
            rebuilt += [s.get("id") for s in rep.get("steps") or [] if s.get("status") == "ok"]
            ledger = FL.load(run_dir)
            now_ids = harness_ids(run_dir)
            check = check_task(run_dir, t, ledger, now_ids)
            passed = all(c["passed"] is not False for c in check.values()) and rep.get("ok", True)
            if passed or attempts_ >= 2:
                break
            evidence = {"check": check, "rebuild_errors": [s.get("error") for s in rep.get("steps") or [] if s.get("error")]
                        + list(rep.get("errors") or [])}
            log(f"[fix loop] {t['id']} check failed; back to its agent once")
        b2 = budget() if budget else None
        row = {"round": round_n, "task": t["id"], "findings": t["findings"], "action": t["action"],
               "owner_state": t.get("owner_state"), "brief": t.get("brief", "")[:300],
               # what the agent changed (measured before any rebuild), and what the rebuild then rewrote
               "files_changed": sorted(agent_changed), "steps_rebuilt": sorted(set(rebuilt)),
               "outputs_changed": sorted(set(changed_files(pend["snap"], snapshot(run_dir))) - agent_changed),
               "rebuild_ok": rep.get("ok", True), "attempts": attempts_,
               "seconds": round(time.time() - float(pend["t0"]), 1),
               "usd": (round(b2["spent"] - pend["usd0"], 4) if b2 and pend["usd0"] is not None else None),
               "headline_before": pend["before_h"], "headline_after": headline(run_dir),
               "check": check, "passed": passed if any(c["passed"] is not None for c in check.values()) else None,
               "new_findings": sorted(set(now_ids) - set(pend["before_ids"])),
               "agent_note": ((r.note if r else "") or "")[:300]}
        if rejected:
            row["rejected"] = rejected
            summary["rejected"] += 1
        append_impact(run_dir, row)
        summary["rows"] += 1
        summary["passed"] += bool(row["passed"])
    return summary


# ------------------------------------------------------------------ self-test
def _self_test() -> int:
    import tempfile
    from types import SimpleNamespace as NS
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)

    S = ["analysis_settings.yaml"]                     # a file a fix task may edit (FIX_EDITABLE)
    packet = {"findings": [
        {"id": "R-001", "blocking": True, "resolution_type": "fix", "scope_declarable": True, "owner_state": "ANALYZE", "claim": "a"},
        {"id": "k:x", "blocking": True, "resolution_type": "fix", "scope_declarable": False, "owner_state": "CONFIGURE", "claim": "b"},
        {"id": "M", "blocking": False, "resolution_type": "fix", "scope_declarable": True, "owner_state": "WRITE", "claim": "c"},
        {"id": "O", "blocking": True, "resolution_type": "owner_decision", "scope_declarable": True, "owner_state": "ANALYZE", "claim": "d"}]}
    good = {"tasks": [{"id": "T1", "findings": ["R-001"], "action": "fix", "owner_state": "ANALYZE", "brief": "x", "files": S},
                      {"id": "T2", "findings": ["k:x"], "action": "fix", "owner_state": "CONFIGURE", "brief": "y", "files": S}],
            "deferred": [{"finding": "M", "reason": "after the write-up"}]}
    ok(validate_plan(good, packet) == [], f"F1 a plan covering every blocking finding validates {validate_plan(good, packet)}")
    e = validate_plan({"tasks": [{"id": "T1", "findings": ["R-001", "k:x"], "action": "fix", "brief": "x"}]}, packet)
    ok(any("share" in x for x in e), "F2 a grouped task must name the files its findings share")
    e = validate_plan({"tasks": [{"id": "T1", "findings": ["R-001"], "action": "fix", "brief": "x"}],
                       "deferred": [{"finding": "k:x", "reason": "later"}]}, packet)
    ok(any("cannot be deferred" in x for x in e), "F3 a blocking finding cannot be deferred")
    e = validate_plan({"tasks": [{"id": "T1", "findings": ["R-001"], "action": "fix", "brief": "x"},
                                 {"id": "T2", "findings": ["k:x"], "action": "decision_test", "brief": "x"},
                                 {"id": "T3", "findings": ["O", "ZZ"], "action": "rethink", "owner_state": "MODEL",
                                  "brief": "x", "files": ["a"]}]}, packet)
    ok(any("not scope-declarable" in x for x in e) and any("owner's decision" in x for x in e)
       and any("unknown finding ZZ" in x for x in e) and any("SPEC or ENGINE_CHOICE" in x for x in e),
       f"F4 non-declarable decision test, owner item, unknown id and a bad rethink target are rejected {e}")
    ok([t["findings"] for t in default_plan(packet)["tasks"]] == [["R-001"], ["k:x"]],
       "F5 the default plan is one task per blocking finding, owner items left to the owner")
    # F_PATCH (lead-gaps β): the planner writes a SPEC rethink's amendment in amend.py's format; the loop reads the
    # patch of the rethink it routes (the first), and only a spec rethink's
    pa = {"id": "A1", "reason": "r", "spec": [{"set": "objective.horizon", "value": 5}]}
    rt = lambda st, p=None, i="T1": dict({"id": i, "findings": ["R-001"], "action": "rethink",  # noqa: E731
                                          "owner_state": st, "brief": "b"}, **({"patch": p} if p is not None else {}))
    ok(all(w in FIX_PLANNER_PROMPT for w in ('"patch"', '"set"', '"add"', '"remove"', "[key=value]"))
       and validate_plan({"tasks": [rt("SPEC", pa), {"id": "T2", "findings": ["k:x"], "action": "fix",
                                                     "owner_state": "CONFIGURE", "brief": "y", "files": S}]}, packet) == [],
       "F_PATCH1 the prompt asks a SPEC rethink for a patch (set, add, remove by dotted path); a plan with one validates")
    ok(spec_patch({"tasks": [rt("SPEC", pa)]}) == pa and spec_patch({"tasks": [rt("ENGINE_CHOICE", pa)]}) is None
       and spec_patch({"tasks": [rt("SPEC")]}) is None and spec_patch({"tasks": [rt("SPEC", "set x")]}) is None
       and spec_patch({"tasks": [rt("ENGINE_CHOICE"), rt("SPEC", pa, "T2")]}) is None and spec_patch(None) is None,
       "F_PATCH2 the routed rethink's patch is read when it is a spec rethink's mapping, else None")
    with tempfile.TemporaryDirectory() as d:
        open(os.path.join(d, "spec.txt"), "w").write("draws 6\n")
        os.makedirs(os.path.join(d, "analysis"))
        led = {"k:x": FL.Finding(id="k:x", kind="k", key="x", severity="HIGH", source="harness", blocking=True),
               "R-001": FL.Finding(id="R-001", kind="critique_blocker", severity="HIGH", source="critique-redteam",
                                   blocking=True, done_when=[{"file": "spec.txt", "contains": "draws 32"}])}
        FL.save(d, led)
        state = {"fixed": False, "calls": 0}

        class T:
            def plan_fixes(self, pk):
                return NS(ok=True, note="", output={"tasks": [
                    {"id": "T1", "findings": ["R-001"], "action": "fix", "owner_state": "ANALYZE", "brief": "raise draws", "files": S},
                    {"id": "T2", "findings": ["k:x"], "action": "fix", "owner_state": "CONFIGURE", "brief": "fix x", "files": S}]})

            def fix(self, t, run_dir, evidence):
                state["calls"] += 1
                if t["id"] == "T1":
                    open(os.path.join(run_dir, "spec.txt"), "w").write("draws 32\n")
                    open(os.path.join(run_dir, "analysis", "out.json"), "w").write("{}")   # harness-owned: not counted
                elif evidence is not None:
                    state["fixed"] = True
                return NS(ok=True, note="done", output=None)

        heads = iter([{"h": 1.0}, {"h": 1.1}, {"h": 1.1}, {"h": 1.1}])
        s = run_loop(d, 1, 3, {"action": "PATCH_OR_RETHINK"}, T(), rebuild=lambda rd: {"ok": True, "steps": [{"id": "analyze", "status": "ok"}]},
                     harness_ids=lambda rd: set() if state["fixed"] else {"k:x"}, headline=lambda rd: next(heads),
                     log=lambda *a: None)
        rows = impact_rows(d)
        ok(s["rows"] == 2 and s["passed"] == 2 and len(rows) == 2, f"F6 two tasks, two impact rows {s}")
        ok(rows[0]["files_changed"] == ["spec.txt"] and rows[0]["steps_rebuilt"] == ["analyze"]
           and rows[0]["headline_before"] == {"h": 1.0} and rows[0]["headline_after"] == {"h": 1.1},
           f"F7 the row measures files changed (harness outputs excluded), steps rebuilt and the headline {rows[0]}")
        ok(rows[1]["attempts"] == 2 and rows[1]["passed"] and state["calls"] == 3,
           "F8 a failed check goes back to the agent once with the evidence, then passes")
        ok(os.path.exists(os.path.join(d, "fix_plan_r1.json")), "F9 the plan the loop ran is stored")

        class Bad(T):
            def plan_fixes(self, pk):
                return NS(ok=True, note="", output={"tasks": []})
        s2 = run_loop(d, 2, 3, None, Bad(), rebuild=lambda rd: {"ok": True}, harness_ids=lambda rd: {"k:x"},
                      headline=lambda rd: {}, log=lambda *a: None)
        ok(s2["plan_by"] == "harness default" and s2["tasks"] == 2, "F10 a planner that leaves blocking findings out twice gets the default plan")
        s3 = run_loop(d, 3, 3, None, T(), rebuild=lambda rd: {"ok": True}, harness_ids=lambda rd: set(),
                      headline=lambda rd: {}, budget=lambda: {"spent": 10, "cap": 10, "left": 0}, log=lambda *a: None)
        ok(s3["skipped_budget"] == 2 and s3["rows"] == 0, "F11 with the loop budget used, tasks are recorded as skipped, not run")
    with tempfile.TemporaryDirectory() as d:
        open(os.path.join(d, "decision_spec.yaml"), "w").write("budget: 10\n")
        FL.save(d, {"k:x": FL.Finding(id="k:x", kind="k", key="x", severity="HIGH", source="harness", blocking=True)})
        calls = {"fix": 0, "rebuild": 0}

        class Edit:
            def plan_fixes(self, pk):
                return NS(ok=True, note="", output={"tasks": [
                    {"id": "T1", "findings": ["k:x"], "action": "fix", "owner_state": "CONFIGURE", "brief": "b", "files": S}]})

            def fix(self, t, run_dir, evidence):
                calls["fix"] += 1
                open(os.path.join(run_dir, "decision_spec.yaml"), "w").write("budget: 99\n")
                return NS(ok=True, note="", output=None)

        def reb(rd):
            calls["rebuild"] += 1
            return {"ok": True}
        guard = lambda rd: (["decision_spec.yaml changed after sign-off"]   # noqa: E731
                            if "99" in open(os.path.join(rd, "decision_spec.yaml")).read() else [])
        s5 = run_loop(d, 1, 3, None, Edit(), rebuild=reb, harness_ids=lambda rd: {"k:x"}, headline=lambda rd: {},
                      log=lambda *a: None, guard=guard, protect=("decision_spec.yaml",))
        row = impact_rows(d)[-1]
        ok(s5["rejected"] == 1 and calls["rebuild"] == 0 and row.get("rejected") and row["passed"] is False
           and open(os.path.join(d, "decision_spec.yaml")).read() == "budget: 10\n",
           f"F13 an edit the guard rejects is undone, nothing is rebuilt, and the row says why {s5}")
        n = calls["fix"]
        s6 = run_loop(d, 1, 3, None, Edit(), rebuild=reb, harness_ids=lambda rd: {"k:x"}, headline=lambda rd: {},
                      log=lambda *a: None, guard=guard, protect=("decision_spec.yaml",))
        ok(calls["fix"] == n and s6["resumed_done"] == 1 and len(impact_rows(d)) == 1,
           "F14 on resume a task with its impact row is not run again")

        class Settings(Edit):
            def fix(self, t, run_dir, evidence):
                open(os.path.join(run_dir, "analysis_settings.yaml"), "w").write("uncertainty: {draws: 32}\n")
                return NS(ok=True, note="", output=None)

        def reb2(rd):
            open(os.path.join(rd, "results.md"), "w").write("rebuilt\n")    # an output the step rewrites
            return {"ok": True, "steps": [{"id": "analyze", "status": "ok"}]}
        run_loop(d, 2, 3, None, Settings(), rebuild=reb2, harness_ids=lambda rd: set(), headline=lambda rd: {},
                 log=lambda *a: None, guard=lambda rd: [], protect=())
        row = impact_rows(d)[-1]
        ok(row["files_changed"] == ["analysis_settings.yaml"] and row["outputs_changed"] == ["results.md"],
           f"F15 the row separates what the agent changed from what the rebuild rewrote {row['files_changed']} {row['outputs_changed']}")
    ok("STAGE" not in FIX_PLANNER_PROMPT and "validate_critique_yaml" not in FIX_PLANNER_PROMPT
       and "spawn" not in FIX_PLANNER_PROMPT.lower(),
       "F12 the planner prompt has no stage protocol, validator invocation or spawn instructions")
    # F16 (F6 S1-7): the planner gets each finding's full claim and required remedy, with a short label.
    longc, fixr = "the effect is a fixed unsourced value; " * 20, "use a sourced range and rerun the test; " * 10
    with tempfile.TemporaryDirectory() as d:
        led16 = {"D-003": FL.Finding(id="D-003", kind="critique_blocker", severity="HIGH", source="critique-domain",
                                     claim=longc, blocking=True)}
        try:
            led16["D-003"].fix_requires = fixr
        except Exception:  # noqa: BLE001
            pass
        f16 = build_packet(d, led16, 1, 3, None, None)["findings"][0]
        ok(f16["claim"] == longc and f16.get("fix_requires") == fixr and 0 < len(f16.get("label") or "") <= 120
           and "fix_requires" in FL.Finding.__dataclass_fields__
           and len(default_plan({"findings": [f16]})["tasks"][0]["brief"]) <= 200,
           "F16 the planner packet carries the full claim and remedy; labels and briefs stay short")
    # F17 (F6 run 3): a write-up finding (owner state WRITE or PRESENTATION) is the WRITE stage's: it is not in the
    # planner's packet, a task naming it is refused, and a blocking one needs no task. A FIX_LOOP task can edit only
    # analysis_settings.yaml and models/ext/, so such a task could never pass its check.
    with tempfile.TemporaryDirectory() as d:
        led17 = {"R-010": FL.Finding(id="R-010", kind="critique_blocker", severity="HIGH", source="critique-redteam",
                                     owner_state="WRITE", target_stage="WRITE", blocking=True, claim="report wording"),
                 "P-002": FL.Finding(id="P-002", kind="critique_blocker", severity="MEDIUM", source="critique-presentation",
                                     owner_state="PRESENTATION", target_stage="PRESENTATION", blocking=True, claim="a figure"),
                 "R-011": FL.Finding(id="R-011", kind="critique_blocker", severity="HIGH", source="critique-redteam",
                                     owner_state="ANALYZE", blocking=True, claim="too few draws")}
        pk17 = build_packet(d, led17, 1, 3, None, None)
        ids17 = [f["id"] for f in pk17["findings"]]
        ok(ids17 == ["R-011"] and [f["id"] for f in pk17.get("write_up") or []] == ["P-002", "R-010"],
           f"F17 write-up findings leave the planner's packet for the WRITE stage ({ids17}, {pk17.get('write_up')})")
        e17 = validate_plan({"tasks": [{"id": "T1", "findings": ["R-011"], "action": "fix", "brief": "x"},
                                       {"id": "T2", "findings": ["R-010"], "action": "fix", "owner_state": "ANALYZE",
                                        "brief": "y"}]}, pk17)
        ok(any("R-010" in x and "WRITE stage" in x for x in e17)
           and validate_plan({"tasks": [{"id": "T1", "findings": ["R-011"], "action": "fix", "brief": "x", "files": S}]}, pk17) == []
           and [t["findings"] for t in default_plan(pk17)["tasks"]] == [["R-011"]],
           f"F17b a task naming a write-up finding is refused; a blocking one needs no task ({e17})")
        FL.save(d, led17)
        seen17 = []

        class W:
            def plan_fixes(self, pk):
                return NS(ok=True, note="", output={"tasks": [
                    {"id": "T1", "findings": ["R-011"], "action": "fix", "owner_state": "ANALYZE", "brief": "draws", "files": S}]})

            def fix(self, t, run_dir, evidence):
                seen17.append(t["findings"])
                return NS(ok=True, note="", output=None)
        s17 = run_loop(d, 1, 3, None, W(), rebuild=lambda rd: {"ok": True}, harness_ids=lambda rd: set(),
                       headline=lambda rd: {}, log=lambda *a: None)
        plan17 = json.load(open(os.path.join(d, "fix_plan_r1.json")))
        ok(seen17 == [["R-011"]] and s17.get("write_up") == ["P-002", "R-010"]
           and plan17.get("write_up") == ["P-002", "R-010"],
           f"F17c the loop runs no task for a write-up finding and records it for the WRITE stage ({s17}, {seen17})")
    # F18 (F6 run 3): a finding whose required remedy is an owner amendment of the signed spec is the owner's: it is
    # not planned (no SPEC rethink redrafts what the owner signed), and the loop's summary names it for OWNER_DECISION.
    with tempfile.TemporaryDirectory() as d:
        led18 = {"D-004": FL.Finding(id="D-004", kind="critique_blocker", severity="HIGH", source="critique-domain",
                                     owner_state="SPEC", blocking=True, claim="the comparator omits the funded plan",
                                     fix_requires="Owner amendment: add the funded plan as the comparator."),
                 "D-005": FL.Finding(id="D-005", kind="critique_blocker", severity="HIGH", source="critique-domain",
                                     owner_state="WRITE", blocking=True, claim="c",
                                     fix_requires="Owner amendment: the report's comparator follows the spec's."),
                 "D-008": FL.Finding(id="D-008", kind="critique_blocker", severity="HIGH", source="critique-domain",
                                     owner_state="SPEC", blocking=False, claim="c",
                                     fix_requires="No owner amendment is needed: restate the year."),
                 "D-006": FL.Finding(id="D-006", kind="critique_blocker", severity="HIGH", source="critique-domain",
                                     owner_state="SPEC", blocking=True, claim="c", fix_requires="Name the decision year."),
                 "D-007": FL.Finding(id="D-007", kind="critique_blocker", severity="HIGH", source="critique-domain",
                                     owner_state="SPEC", blocking=True, claim="c", resolution_type="owner_decision",
                                     fix_requires="owner amendment")}
        pk18 = build_packet(d, led18, 1, 3, None, None)
        ok([f["id"] for f in pk18["findings"]] == ["D-006", "D-007", "D-008"]
           and [f["id"] for f in pk18.get("owner_amendments") or []] == ["D-004", "D-005"],
           f"F18 findings whose remedy starts 'Owner amendment' leave the packet for the owner, a write-up one too; a "
           f"remedy that only mentions one does not ({pk18.get('owner_amendments')})")
        e18 = validate_plan({"tasks": [{"id": "T1", "findings": ["D-006"], "action": "rethink", "owner_state": "SPEC",
                                        "brief": "x"},
                                       {"id": "T2", "findings": ["D-004"], "action": "rethink", "owner_state": "SPEC",
                                        "brief": "x"}]}, pk18)
        ok(any("D-004" in x and "owner amendment" in x for x in e18),
           f"F18b a rethink of a finding that needs an owner amendment is refused ({e18})")
        FL.save(d, led18)

        class R:
            def plan_fixes(self, pk):
                return NS(ok=True, note="", output={"tasks": [
                    {"id": "T1", "findings": ["D-006"], "action": "rethink", "owner_state": "SPEC", "brief": "year"}]})

            def fix(self, t, run_dir, evidence):
                raise AssertionError("no fix call")
        s18 = run_loop(d, 1, 3, None, R(), rebuild=lambda rd: {"ok": True}, harness_ids=lambda rd: set(),
                       headline=lambda rd: {}, log=lambda *a: None)
        ok(s18.get("owner_amendments") == ["D-004", "D-005"] and s18["rethink"] == "SPEC",
           f"F18c the loop's summary names the findings the owner must amend ({s18})")
    # F_CHECK_NOOP (v2 autonomy 1A, 4a).
    with tempfile.TemporaryDirectory() as d:
        ledn = {"critique_presentation_missing": FL.Finding(id="critique_presentation_missing", severity="MEDIUM",
                                                            kind="critique_presentation_missing", source="rigor-artifacts"),
                "done_when_unmet:D-1": FL.Finding(id="done_when_unmet:D-1", kind="done_when_unmet", key="D-1",
                                                  severity="MEDIUM", source="rigor-artifacts",
                                                  done_when=[{"file": "never.txt", "exists": True}]),
                "k:x": FL.Finding(id="k:x", kind="k", key="x", severity="HIGH", source="rigor-artifacts")}
        cn = check_task(d, {"findings": list(ledn)}, ledn, set())
        ok(cn["critique_presentation_missing"]["passed"] is None and cn["done_when_unmet:D-1"]["passed"] is False
           and cn["k:x"]["passed"] is True,
           f"F_CHECK_NOOP after a no-op edit, a gate-only finding is left to the gate and a failing test fails ({cn})")
        # F_RETHINK_BREAK (5.4): nothing is fixed or rebuilt after a rethink task.
        FL.save(d, {"k:x": ledn["k:x"], "k:y": FL.Finding(id="k:y", kind="k", key="y", severity="HIGH", source="harness")})
        ran = []

        class RB:
            def plan_fixes(self, pk):
                return NS(ok=True, note="", output={"tasks": [
                    {"id": "T1", "findings": ["k:x"], "action": "rethink", "owner_state": "ENGINE_CHOICE", "brief": "b"},
                    {"id": "T2", "findings": ["k:y"], "action": "fix", "owner_state": "ANALYZE", "brief": "b", "files": S}]})

            def fix(self, t, run_dir, evidence):
                ran.append(t["id"])
                return NS(ok=True, note="", output=None)
        srb = run_loop(d, 1, 3, None, RB(), rebuild=lambda rd: ran.append("rebuild") or {"ok": True},
                       harness_ids=lambda rd: set(), headline=lambda rd: {}, log=lambda *a: None)
        ok(srb["rethink"] == "ENGINE_CHOICE" and ran == [] and srb.get("skipped_rethink") == 1,
           f"F_RETHINK_BREAK the loop stops at the first rethink task ({ran}, {srb})")
    # F19-F21 (harness baseline hb1, ranked fix 8): every fix task in hb1 planned a change outside what a fix task may
    # edit (the files of the dengue, measles and tb R2 plans below), and each changed nothing. The planner is told the
    # edit scope, plan validation refuses a file outside it, and a finding that needs harness code is a declared
    # harness gap with its reason, not a fix row.
    with tempfile.TemporaryDirectory() as d:
        led19 = {"R-001": FL.Finding(id="R-001", kind="critique_blocker", severity="HIGH", source="critique-redteam",
                                     owner_state="ANALYZE", blocking=True, claim="the span the data allow is unreported"),
                 "R-004": FL.Finding(id="R-004", kind="critique_blocker", severity="MEDIUM", source="critique-redteam",
                                     owner_state="CONFIGURE", blocking=False, claim="a source path is unchecked")}
        pk19 = build_packet(d, led19, 1, 3, None, None)
        ok(pk19.get("edit_scope", {}).get("files") == list(FIX_EDITABLE)
           and "uncertainty.draws" in (pk19.get("edit_scope") or {}).get("settings", []),
           f"F19 the planner's packet names what a fix task may edit ({pk19.get('edit_scope')})")
        hb1 = {"dengue R2 T1": ["analysis/estimates.yaml", "analysis/claims.yaml", "models/claims_spec.yaml",
                                "models/claims_ledger.yaml", "results.md"],
               "measles R2 T1": ["allocation_optimized.csv", "analysis/uncertainty.yaml", "uncertainty_report.yaml",
                                 "analysis/voi.yaml", "claims.yaml", "results.md"],
               "tb R2 T1": ["data/README.md"], "harness code": ["toolkit/analysis/run.py"],
               "escape": ["models/ext/../../toolkit/analysis/run.py"], "absolute": ["/tmp/x/analysis_settings.yaml"],
               # Codex round 2 P3: the write hook takes the path as written, so the validator does too
               "backslash": ["models\\ext\\a.py"], "padded": [" analysis_settings.yaml"]}
        bad = {k: validate_plan({"tasks": [{"id": "T1", "findings": ["R-001"], "action": "fix", "owner_state": "ANALYZE",
                                            "brief": "x", "files": v}]}, pk19) for k, v in hb1.items()}
        ok(all(any("outside what a fix task may edit" in x and "harness_gaps" in x for x in e) for e in bad.values()),
           f"F19b each hb1 plan's files are refused, pointing at harness_gaps ({bad})")
        e19c = validate_plan({"tasks": [{"id": "T1", "findings": ["R-001"], "action": "fix", "owner_state": "ANALYZE",
                                         "brief": "x"}]}, pk19)
        ok(any("must name the files" in x for x in e19c),
           f"F19c a fix task that names no file is refused: the scope check needs them ({e19c})")
        good19 = {"tasks": [{"id": "T1", "findings": ["R-001"], "action": "fix", "owner_state": "ANALYZE", "brief": "x",
                             "files": ["analysis_settings.yaml", "./models/ext/span.py"]}]}
        ok(validate_plan(good19, pk19) == [] and validate_plan(default_plan(pk19), pk19) == [],
           f"F19d files inside the scope validate, and the harness default plan does too "
           f"({validate_plan(good19, pk19)}, {validate_plan(default_plan(pk19), pk19)})")
        gap = {"tasks": [], "harness_gaps": [{"finding": "R-001", "reason": "the claims are fixed in harness code",
                                              "needs": ["toolkit/analysis/estimates.py"]}]}
        ok(validate_plan(gap, pk19) == [], f"F20 a blocking finding declared a harness gap is covered ({validate_plan(gap, pk19)})")
        e20b = validate_plan({"tasks": [{"id": "T1", "findings": ["R-001"], "action": "fix", "brief": "x",
                                         "files": ["analysis_settings.yaml"]}],
                              "harness_gaps": [{"finding": "R-001", "reason": "r", "needs": ["x"]},
                                               {"finding": "R-004", "reason": " ", "needs": ["x"]},
                                               {"finding": "R-009", "reason": "r", "needs": ["x"]},
                                               {"finding": "R-004", "reason": "r", "needs": []}]}, pk19)
        ok(any("R-001" in x and "both" in x for x in e20b) and any("R-004" in x and "reason" in x for x in e20b)
           and any("unknown finding R-009" in x for x in e20b) and any("R-004" in x and "needs" in x for x in e20b),
           f"F20b a harness gap needs a known finding not in a task, a reason and what it needs ({e20b})")
        FL.save(d, led19)

        class G:
            def plan_fixes(self, pk):
                return NS(ok=True, note="", output=dict(gap, harness_gaps=gap["harness_gaps"] + [{"finding": "R-004", "reason": "r", "needs": ["x"]}]))

            def fix(self, t, run_dir, evidence):
                raise AssertionError("no fix call for a harness gap")
        s20 = run_loop(d, 1, 3, None, G(), rebuild=lambda rd: {"ok": True}, harness_ids=lambda rd: set(),
                       headline=lambda rd: {}, log=lambda *a: None)
        plan20 = json.load(open(os.path.join(d, "fix_plan_r1.json")))
        ev20 = [e for e in FL.load(d)["R-001"].events if e.get("status") == "harness_gap"]
        ok(s20.get("harness_gaps") == ["R-001", "R-004"] and s20.get("harness_gaps_blocking") == ["R-001"]
           and s20["rows"] == 0 and impact_rows(d) == []
           and plan20.get("harness_gaps") == ["R-001", "R-004"] and ev20 and "harness code" in ev20[-1].get("note", "")
           and FL.load(d)["R-001"].status == "open" and "R-001" in FL.harness_gaps(FL.load(d))
           # Codex r1 of #357 P2-11: the harness's default plan plans no task for a declared gap
           and not any("R-001" in t["findings"] for t in default_plan(build_packet(d, FL.load(d), 2, 3, None,
                                                                                   None))["tasks"])
           and build_packet(d, FL.load(d), 2, 3, None, None)["findings"][0].get("harness_gap"),
           f"F20c a harness gap is recorded with its reason (plan, summary, ledger event, next packet), not as a fix "
           f"row ({s20}, {ev20})")
    # F21 (hb1): the harness's own writes during a task (budget_ledger.json, the task's input and output files) are
    # not the task's change. In hb1 every no-change fix task showed budget_ledger.json in files_changed.
    with tempfile.TemporaryDirectory() as d:
        FL.save(d, {"R-001": FL.Finding(id="R-001", kind="critique_blocker", severity="HIGH", source="critique-redteam",
                                        owner_state="ANALYZE", blocking=True, claim="c",
                                        done_when=[{"file": "never.txt", "exists": True}])})

        class N:
            def plan_fixes(self, pk):
                return NS(ok=True, note="", output={"tasks": [{"id": "T1", "findings": ["R-001"], "action": "fix",
                                                               "owner_state": "ANALYZE", "brief": "x", "files": S}]})

            def fix(self, t, run_dir, evidence):
                for n in ("budget_ledger.json", "_task_input.json", "_task_output.json"):
                    open(os.path.join(run_dir, n), "w").write(json.dumps({"e": evidence is not None}))
                return NS(ok=True, note="Nothing changed: needs harness code", output=None)
        run_loop(d, 1, 3, None, N(), rebuild=lambda rd: {"ok": True}, harness_ids=lambda rd: set(),
                 headline=lambda rd: {}, log=lambda *a: None)
        row21 = impact_rows(d)[-1]
        ok(row21["files_changed"] == [], f"F21 harness writes during a task are not its change ({row21['files_changed']})")
    # F22 (Codex r3 P2-7, P2-8): the worker's write list keeps a folder a folder; the packet's settings are the running
    # machine's backend's when it passes them.
    ok(task_scope({"files": ["analysis_settings.yaml"]}) == ("analysis_settings.yaml",)
       and task_scope({"files": ["./models/ext/a.py", "toolkit/x.py"]}) == ("models/ext/a.py",)
       and task_scope({"files": ["models/ext/calibration/"]}) == ("models/ext/calibration/",)
       and task_scope({"files": ["models/ext"]}) == ("models/ext/",)
       and task_scope({"files": []}) == FIX_EDITABLE,
       f"F22 the fix worker may write only the in-scope files its task names; a folder keeps its '/' "
       f"({task_scope({'files': ['models/ext/calibration/']})})")
    with tempfile.TemporaryDirectory() as d:
        pk22 = build_packet(d, {}, 1, 3, None, None, settings=("emulator.points", "uncertainty.draws"))
        ok(pk22["edit_scope"]["settings"] == ["emulator.points", "uncertainty.draws"],
           f"F22b the packet lists the settings of the backend the machine runs ({pk22['edit_scope']})")
    ok(all(s in FIX_PLANNER_PROMPT for s in ("edit_scope", "harness_gaps")),
       "F21c the planner prompt explains the edit scope and harness gaps")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    print(__doc__)
