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
ACTIONS = ("fix", "rethink", "decision_test")
RETHINK_STATES = ("SPEC", "ENGINE_CHOICE")
# Files the harness writes; never counted as an agent's change.
_HARNESS_OWNED = ("findings.jsonl", IMPACT, "run_state.json", "trace.jsonl", "validator_history.jsonl",
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
  impact_history           one row per earlier fix task: findings, files changed, headline before and
                           after, whether its check passed, findings it opened
  budget                   spent, cap and what is left in USD

Return only JSON:

  {"tasks": [{"id": "T1", "findings": ["R-005"], "action": "fix" | "rethink" | "decision_test",
              "owner_state": "<state>", "brief": "<what to change and why, citing the evidence>",
              "files": ["<files the change touches>"]}],
   "deferred": [{"finding": "<id>", "reason": "<why not in this loop>"}]}

Rules:
- One finding per task. Group findings in one task only when the same change fixes them and
  they share files; then list those files.
- Every blocking finding is in a task. A non-blocking finding may be deferred with a reason.
- Order: what unblocks most first; a finding that other findings depend on before them.
- Rethink, not patch, when the finding is structural, when the impact history shows the same
  finding patched twice without its check passing, or when several blocking findings point at one
  mechanism. A rethink names owner_state SPEC (the decision spec is wrong) or ENGINE_CHOICE
  (the engine or its configuration cannot represent what the question needs).
- decision_test instead of fix when the finding cannot be fixed with the data and engines
  available: the brief says which quantity to vary and what would change the decision. Never
  for a finding with scope_declarable false.
- An owner_decision finding goes to the owner; do not plan a task for it.
- Use the impact history: do not repeat a change that moved nothing; prefer changes whose
  measured effect on the headline is understood.
- Stay inside the budget: a loop that cannot finish is worse than a shorter one.
"""


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
                 budget: dict | None, history_rows: int = 30) -> dict:
    att = attempts(run_dir)
    fs = []
    for f in FL.open_findings(ledger):
        fs.append({"id": f.id, "kind": f.kind, "severity": f.severity, "blocking": f.blocking,
                   "owner_state": f.owner_state, "resolution_type": f.resolution_type,
                   "scope_declarable": f.scope_declarable, "claim": f.claim[:400], "done_when": f.done_when,
                   "first_seen_round": f.first_seen_round, "attempts": att.get(f.id, 0)})
    g = gate or {}
    return {"round": round_n, "max_rounds": max_rounds,
            "gate": {"action": g.get("action"), "rule": g.get("rule_matched"), "rationale": g.get("rationale"),
                     "escalation_required": g.get("escalation_required"),
                     "stalled": g.get("stalled_findings") or []},
            "findings": fs, "impact_history": impact_rows(run_dir)[-history_rows:], "budget": budget or {}}


def validate_plan(plan, packet: dict) -> list[str]:
    """Problems with a planner's output (empty: the plan can run)."""
    if not isinstance(plan, dict) or not isinstance(plan.get("tasks"), list):
        return ["the plan must be a JSON object with a `tasks` list"]
    by_id = {f["id"]: f for f in packet["findings"]}
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
            if fid not in by_id:
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
    deferred = {d.get("finding") for d in plan.get("deferred") or [] if isinstance(d, dict) and str(d.get("reason") or "").strip()}
    for f in packet["findings"]:
        if f["blocking"] and f["resolution_type"] != "owner_decision" and f["id"] not in used:
            errs.append(f"blocking finding {f['id']} is in no task" + (" (blocking findings cannot be deferred)"
                                                                      if f["id"] in deferred else ""))
    return errs


def default_plan(packet: dict) -> dict:
    """The harness's own plan when the planner fails twice: one task per blocking finding, in
    the packet's order (blocking first, then severity), each a fix by its owner state."""
    tasks = []
    for f in packet["findings"]:
        if f["blocking"] and f["resolution_type"] != "owner_decision":
            tasks.append({"id": f"T{len(tasks) + 1}", "findings": [f["id"]], "action": "fix",
                          "owner_state": f["owner_state"], "brief": f"Fix {f['id']}: {f['claim'][:300]}", "files": []})
    return {"tasks": tasks, "deferred": [], "by": "harness default"}


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


def check_task(run_dir: str, task: dict, ledger: dict, gate_ids: set[str]) -> dict:
    """The task's own checks: a finding with a runnable done_when test passes when the test
    passes; a harness-check finding passes when the harness no longer reports it; anything
    else is left to the next critique (None)."""
    import done_when as DW
    res = {}
    for fid in task["findings"]:
        f = ledger.get(fid)
        if f is None:
            res[fid] = {"passed": False, "why": "not in the ledger"}
        elif f.done_when and DW.mechanical(f.done_when):
            ok_, why = DW.evaluate(f.done_when, run_dir)
            res[fid] = {"passed": bool(ok_), "why": why}
        elif not f.source.startswith("critique"):
            res[fid] = {"passed": fid not in gate_ids, "why": "absent from the harness checks" if fid not in gate_ids
                        else "still reported by the harness checks"}
        else:
            res[fid] = {"passed": None, "why": "judgement: the next critique decides"}
    return res


def append_impact(run_dir: str, row: dict) -> None:
    with open(os.path.join(run_dir, IMPACT), "a", encoding="utf-8") as f:
        f.write(json.dumps(row, sort_keys=True, default=str) + "\n")


def _read_or_none(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def _restore(run_dir: str, saved: dict) -> None:
    for rel, text in saved.items():
        p = os.path.join(run_dir, rel)
        if os.path.islink(p):
            os.remove(p)
        if text is None:
            if os.path.exists(p):
                os.remove(p)
        else:
            with open(p, "w", encoding="utf-8") as f:
                f.write(text)


def run_loop(run_dir: str, round_n: int, max_rounds: int, gate: dict | None, tasks, rebuild, harness_ids,
             headline, budget=None, log=print, guard=None, protect=()) -> dict:
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
    packet = build_packet(run_dir, ledger, round_n, max_rounds, gate, budget() if budget else None)
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
    with open(os.path.join(run_dir, f"fix_plan_r{round_n}.json"), "w") as f:
        json.dump({"round": round_n, "plan": plan, "rejected": errs}, f, indent=1)
    done = {r.get("task") for r in impact_rows(run_dir) if r.get("round") == round_n}
    prog_p = os.path.join(run_dir, f"fix_progress_r{round_n}.json")
    try:
        progress = json.load(open(prog_p))
    except (OSError, ValueError):
        progress = {}
    summary = {"round": round_n, "tasks": len(plan["tasks"]), "rows": 0, "passed": 0, "rethink": None,
               "skipped_budget": 0, "rejected": 0, "resumed_done": 0, "plan_by": plan.get("by", "planner")}

    def out_of_budget():
        b = budget() if budget else None
        return bool(b and b.get("left") is not None and b["left"] <= 0)

    for t in plan["tasks"]:
        if t["id"] in done:
            summary["resumed_done"] += 1
            if t["action"] == "rethink":
                summary["rethink"] = summary["rethink"] or t["owner_state"]
            continue
        if t["action"] == "rethink":
            summary["rethink"] = summary["rethink"] or t["owner_state"]
            append_impact(run_dir, {"round": round_n, "task": t["id"], "findings": t["findings"], "action": "rethink",
                                    "owner_state": t["owner_state"], "passed": None, "note": "routed to " + t["owner_state"]})
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
            with open(prog_p, "w") as f:
                json.dump(progress, f, default=str)
        evidence, rebuilt, attempts_, rejected, rep, r = None, [], 0, [], {}, None
        check, passed, now_ids = {}, False, set(pend["before_ids"])
        while True:
            attempts_ += 1
            if attempts_ > 1 and out_of_budget():
                break                                   # the retry is a paid call too
            r = tasks.fix(t, run_dir, evidence)
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
               "files_changed": changed_files(pend["snap"], snapshot(run_dir)), "steps_rebuilt": sorted(set(rebuilt)),
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

    packet = {"findings": [
        {"id": "R-001", "blocking": True, "resolution_type": "fix", "scope_declarable": True, "owner_state": "ANALYZE", "claim": "a"},
        {"id": "k:x", "blocking": True, "resolution_type": "fix", "scope_declarable": False, "owner_state": "CONFIGURE", "claim": "b"},
        {"id": "M", "blocking": False, "resolution_type": "fix", "scope_declarable": True, "owner_state": "WRITE", "claim": "c"},
        {"id": "O", "blocking": True, "resolution_type": "owner_decision", "scope_declarable": True, "owner_state": "ANALYZE", "claim": "d"}]}
    good = {"tasks": [{"id": "T1", "findings": ["R-001"], "action": "fix", "owner_state": "ANALYZE", "brief": "x"},
                      {"id": "T2", "findings": ["k:x"], "action": "fix", "owner_state": "CONFIGURE", "brief": "y"}],
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
                    {"id": "T1", "findings": ["R-001"], "action": "fix", "owner_state": "ANALYZE", "brief": "raise draws"},
                    {"id": "T2", "findings": ["k:x"], "action": "fix", "owner_state": "CONFIGURE", "brief": "fix x"}]})

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
                    {"id": "T1", "findings": ["k:x"], "action": "fix", "owner_state": "CONFIGURE", "brief": "b"}]})

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
    ok("STAGE" not in FIX_PLANNER_PROMPT and "validate_critique_yaml" not in FIX_PLANNER_PROMPT
       and "spawn" not in FIX_PLANNER_PROMPT.lower(),
       "F12 the planner prompt has no stage protocol, validator invocation or spawn instructions")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    print(__doc__)
