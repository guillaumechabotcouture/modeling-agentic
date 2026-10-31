"""Hook infrastructure for the lead session: the PreToolUse / PostToolUse /
SubagentStart / SubagentStop hooks, the report snapshot, and the hold rules
that keep the lead from spawning the writer, critics or planners too early.

Phase 48 δ moved this code out of agents/__init__.py unchanged; the package
imports every name back (`from agents import create_hooks, writer_hold_reason`
still works). Tested by `python3 main.py --self-test`.
"""

import json
import os
import re
import shutil
import time
from datetime import datetime

from claude_agent_sdk import HookMatcher

from agents import enforcement, _registry
# scripts/ is on sys.path: agents/__init__.py put it there before importing this module.
import open_problems  # noqa: E402  (Phase 32: open-problem list, round plan, fix checks)
import decision_brief  # noqa: E402  (Phase 33: the signed decision brief)
import scope_signoff  # noqa: E402  (Phase 49 β: the owner signs decision-bearing declarations)
import derived_rebuild  # noqa: E402  (Phase 39 β: the harness regenerates stale figures)

REPO_ROOT = enforcement.REPO_ROOT

REBUILD_AFTER = ("modeler", "analyst")   # stages whose outputs the figures show

MAX_FIGURE_PIXELS = 10_000_000  # 10MP -- anything larger is likely a bug


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
# Hooks
# ---------------------------------------------------------------------------

def _safe_write(trace_file, record: dict) -> None:
    try:
        trace_file.write(json.dumps(record) + "\n")
        trace_file.flush()
    except (ValueError, OSError):
        pass


def snapshot_report(run_path: str, state: dict) -> bool:
    """Copy report.md to report.snapshot.md when it changed since the
    last snapshot. Phase 21 α: called after every tool use, so the
    snapshot exists before any later disappearance (Phase 12 δ only
    snapshotted at run end, after the failure it guarded against).
    Returns True when a snapshot was written."""
    report = os.path.join(run_path, "report.md")
    try:
        st = os.stat(report)
    except OSError:
        return False
    key = (st.st_mtime_ns, st.st_size)
    if state.get("report_key") == key or st.st_size == 0:
        return False
    try:
        shutil.copyfile(report, os.path.join(run_path, "report.snapshot.md"))
    except OSError:
        return False
    state["report_key"] = key
    return True


WRITE_ALLOWED_AFTER = ("ACCEPT", "DECLARE_SCOPE", "RUN_FAILED")


def _gate_rounds(run_path: str) -> list[dict]:
    rows = []
    try:
        for line in open(os.path.join(run_path, "validator_history.jsonl"), errors="replace"):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict):
                rows.append(r)
    except OSError:
        pass
    return rows


def budget_note(status: dict | None, n_rounds: int) -> str | None:
    """Phase 31: the lead's view of the dollar cap (E7 attempt 6 spent its
    last round on a writing loop without knowing the budget was nearly gone)."""
    if not status or not status.get("cap"):
        return None
    spent, cap, reserve = status["spent"], status["cap"], status["reserve"]
    left = cap - reserve - spent
    line = (f"[harness] Budget: about ${spent:.0f} of ${cap:.0f} spent (estimate); ${reserve:.0f} is held for "
            f"the final write-up, leaving about ${max(left, 0):.0f} for modeling")
    if n_rounds:
        per = spent / n_rounds
        line += (f", about {max(left, 0) / per:.1f} more review round(s) at the ${per:.0f} per round "
                 f"average so far ({n_rounds} gate round(s))")
    return line + ". Plan the remaining work to fit; if the budget runs out the harness writes the report."


def writer_hold_reason(run_path: str, status: dict | None, max_rounds: int | None) -> str | None:
    """Phase 31: the first write-up waits for the gate. Returns why a writer
    spawn is refused now, or None. Allowed after ACCEPT / DECLARE_SCOPE /
    RUN_FAILED, in the final round, when less than one more round of budget
    is left, or once a draft exists (revisions)."""
    if any(os.path.exists(os.path.join(run_path, f)) for f in ("manuscript.md", "report.md")):
        return None
    rows = _gate_rounds(run_path)
    last = rows[-1] if rows else {}
    if str(last.get("action") or "") in WRITE_ALLOWED_AFTER:
        return None
    rnd = max((int(r.get("round") or 0) for r in rows), default=0)
    if max_rounds and rnd >= max_rounds:
        return None
    if status and status.get("cap") and rows:
        left = status["cap"] - status["reserve"] - status["spent"]
        if left < status["spent"] / max(1, len({r.get("round") for r in rows})):
            return None
    return ("The write-up waits for the gate: the latest gate action is "
            f"{last.get('action') or 'none yet'}, and the budget covers another review round. "
            "Fix the blocking findings, re-run the critiques and the gate; spawn the writer after ACCEPT, "
            "DECLARE_SCOPE or RUN_FAILED. Findings aimed at the write-up do not block or escalate before it exists.")


PLAN_AGENTS = ("planner", "data-agent", "modeler", "analyst")
CRITIC_PROMPT_HEADS = ("This is critique round", "This is a fix check", "This is a repair of")
_PLAN_ACTIONS = ("PATCH_OR_RETHINK", "RETHINK_STRUCTURAL")


def _budget_tight(status: dict | None, rows: list[dict]) -> bool:
    """Less than one more review round of budget left (at the average so far)."""
    if not status or not status.get("cap") or not rows:
        return False
    left = status["cap"] - status["reserve"] - status["spent"]
    return left < status["spent"] / max(1, len({r.get("round") for r in rows}))


def _plan_required(run_path: str) -> dict | None:
    """Phase 32: the open-problem list the next round's work must plan for,
    or None (no gate yet, gate not asking for work, or a stale list)."""
    rows = [r for r in _gate_rounds(run_path) if not r.get("post_write")]
    if not rows or str(rows[-1].get("action") or "") not in _PLAN_ACTIONS:
        return None
    doc = open_problems.load_open(run_path)
    if doc is None or int(doc.get("round") or -1) != int(rows[-1].get("round") or -2):
        return None
    return doc


def _lead_spawns_since(run_path: str, since: float, with_time: bool = False) -> list[tuple]:
    """(subagent_type, ids named in its prompt) for lead spawns after `since`
    (an mtime), read back from trace.jsonl so a resumed session sees them."""
    out = []
    try:
        for line in open(os.path.join(run_path, "trace.jsonl"), errors="replace"):
            if '"mentions"' not in line:
                continue
            try:
                r = json.loads(line)
                t = datetime.fromisoformat(r["ts"]).timestamp()
                if t >= since and not r.get("denied"):
                    out.append((r.get("subagent_type"), set(r.get("mentions") or ()))
                               + ((t,) if with_time else ()))
            except (ValueError, KeyError, TypeError):
                continue
    except OSError:
        pass
    return out


def _list_missing(run_path: str) -> str | None:
    """Codex review: the gate asked for a plan but its open-problem list is
    missing or older than that gate run: refuse rather than wave work through."""
    rows = [r for r in _gate_rounds(run_path) if not r.get("post_write")]
    if not rows or str(rows[-1].get("action") or "") not in _PLAN_ACTIONS:
        return None
    if _plan_required(run_path) is not None:
        return None
    n = rows[-1].get("round")
    return (f"The round {n} gate asked for more work, but {open_problems.OPEN_JSON} for round {n} is missing or "
            f"stale. Re-run the gate for round {n} (scripts/validate_critique_yaml.py with --current-round {n}) "
            "so it writes the open-problem list, then plan it.")


def plan_hold_reason(run_path: str) -> str | None:
    """Phase 32 α: work for the next round waits for round_plan.yaml."""
    doc = _plan_required(run_path)
    if doc is None:
        return _list_missing(run_path)
    errors = open_problems.check_plan(run_path)
    if not errors:
        return None
    return (f"Before the next round's work, write {open_problems.PLAN_FILE} (round: {doc['round']}) giving every "
            f"open problem in open_problems.md an action. Not yet: " + "; ".join(errors)[:1500])


def critique_hold_reason(run_path: str, status: dict | None) -> str | None:
    """Phase 32 α/β: the round's full critique waits until every planned
    fix went to its stage's agent, the critics checked their fixes, and what
    was not fixed went back once. Skipped when the budget cannot cover it."""
    doc = _plan_required(run_path)
    if _budget_tight(status, _gate_rounds(run_path)):
        return None
    if doc is None:
        return _list_missing(run_path)
    errors = open_problems.check_plan(run_path)
    if errors:
        return plan_hold_reason(run_path)
    plan_path = os.path.join(run_path, open_problems.PLAN_FILE)
    missing = open_problems.unsent(open_problems.planned_work(run_path),
                                   _lead_spawns_since(run_path, open_problems.round_start(run_path)))
    if missing:
        return ("The round's critique waits until every planned fix has been handed to the agent that does it. "
                "Not yet named in a spawn prompt: " + ", ".join(missing)[:1200])
    absent, redo = open_problems.check_fix_checks(run_path)
    if absent:
        return ("Before the round's full critique, ask each critic to check its own planned fixes: spawn it with "
                f"a prompt beginning `This is a fix check after round {doc['round']}.` listing its ids. Missing: "
                + "; ".join(absent)[:1200])
    if redo:
        since = open_problems.redo_since(run_path)
        start = min(since.values(), default=open_problems.fix_check_mtime(run_path) or 0)
        later = [(a, got, t) for a, got, t in _lead_spawns_since(run_path, start, with_time=True)]
        left = [f"{i} -> {agent}" for agent, ids in redo.items() for i in ids
                if not any(a == agent and i in got and t >= since.get(i, start) for a, got, t in later)]
        if left:
            return ("The fix checks found items not fixed or new problems the fixes caused. Send each back to its "
                    "agent once (name the id and quote the critic's evidence), then run the full critique: "
                    + ", ".join(left)[:1200])
    return None


_SPAWN_START_GRACE_S = 120


def _trace_since(run_path: str, since: float):
    try:
        for line in open(os.path.join(run_path, "trace.jsonl"), errors="replace"):
            try:
                r = json.loads(line)
                if datetime.fromisoformat(r["ts"]).timestamp() >= since:
                    yield r
            except (ValueError, KeyError, TypeError):
                continue
    except OSError:
        return


def work_running(run_path: str, since: float, kinds=PLAN_AGENTS, now: float | None = None) -> list[str]:
    """Phase 32 γ: work agents launched since `since` (this session) that have
    not finished. The CLI launches agents in the background ("Async agent
    launched"), so the lead can ask a critic to review work still being done.
    An agent cut off at its turn limit logs a task_notification and no
    subagent_stop (E7 attempt 6: 5 of 57), so either counts as finished. A
    spawn not yet started counts for a short grace period, since the critic
    may be spawned in the same message."""
    now = time.time() if now is None else now
    started, done, spawned, starts = {}, set(), {}, {}
    for r in _trace_since(run_path, since):
        t = r.get("type")
        if t == "subagent_start" and r.get("agent_type") in kinds:
            started[r.get("agent_id")] = (r["agent_type"], r["ts"][11:19])
            starts[r["agent_type"]] = starts.get(r["agent_type"], 0) + 1
        elif t == "subagent_stop":
            done.add(r.get("agent_id"))
        elif t in ("task_notification", "task_updated"):   # task_updated: terminal statuses only (main.py)
            done.add(r.get("task_id"))
        elif r.get("subagent_type") in kinds and r.get("agent") == "lead" and not r.get("denied"):
            spawned.setdefault(r["subagent_type"], []).append(datetime.fromisoformat(r["ts"]).timestamp())
    out = [f"{k} (since {ts})" for a, (k, ts) in started.items() if a not in done]
    for k, times in spawned.items():
        out += [f"{k} (launching)" for t in sorted(times)[starts.get(k, 0):] if t >= now - _SPAWN_START_GRACE_S]
    return out


def review_wait_reason(run_path: str, sub: str | None, since: float) -> str | None:
    """Phase 32 γ: critics and the writer review finished work only. The
    pre-mortem reviews the plan, so it waits for the planner alone."""
    sub = str(sub or "")
    if not (sub.startswith("critique-") or sub == "writer"):
        return None
    busy = work_running(run_path, since, ("planner",) if sub == "critique-premortem" else PLAN_AGENTS)
    if not busy:
        return None
    return (f"{sub} reviews finished work, and these agents are still working: {', '.join(busy)[:600]}. Wait "
            f"for their results (each comes back to you when it finishes), then spawn {sub}.")


def signoff_hold_reason(run_path: str, mode: str | None, run_dir_rel: str = "{run_dir}") -> str | None:
    """Phase 33 α: nothing but planning happens before the decision brief is
    signed (owner mode: the owner signs and the harness resumes the lead;
    lead mode, for unattended test runs: the lead signs with its reason)."""
    if mode not in ("owner", "lead"):
        return None
    st = decision_brief.status(run_path)
    if st == "signed":
        return None
    if st == "changed":
        return (f"{decision_brief.BRIEF} or {decision_brief.CRITERIA} changed after sign-off. They are frozen: run "
                f"`python3 scripts/decision_brief.py restore {run_dir_rel}` to put the signed versions back, and "
                f"record framing changes in {decision_brief.AMENDMENTS}.")
    if st in ("absent", "invalid"):
        errs = "; ".join(decision_brief.load_brief(run_path)[1])[:900]
        return (f"Before any other work the planner writes {decision_brief.BRIEF} (what the answer must deliver; see "
                f"STAGE 1). Re-spawn the planner with these problems: {errs}")
    if mode == "owner":
        return ("The decision brief is ready and waits for the owner's sign-off. Do not start other work. End your "
                "turn now with the line AWAITING_SIGNOFF; the harness resumes you with the owner's answers.")
    return ("Review the decision brief against the question, then sign it: python3 scripts/decision_brief.py sign "
            f"{run_dir_rel} --by lead --accept-proposed --note \"<why it is an adequate target>\" (or answer the "
            "open questions with --answers <file>).")


def scope_hold_reason(run_path: str, mode: str | None, run_dir_rel: str = "{run_dir}") -> str | None:
    """Phase 49 β: the writer waits while declarations of findings that bear on
    the primary decision wait for sign-off (owner mode: the owner; lead mode: the lead)."""
    if mode not in ("owner", "lead") or scope_signoff.status(run_path) != "awaiting":
        return None
    if mode == "owner":
        return ("The gate's decision is AWAIT_OWNER: declarations of findings that bear on the primary decision "
                "wait for the owner (scope_signoff_request.json). Do not spawn the writer. End your turn now with the "
                "line AWAITING_SIGNOFF; the harness resumes you with the owner's answers.")
    return (f"The gate's decision is AWAIT_OWNER. Review `python3 scripts/scope_signoff.py show {run_dir_rel}` (each "
            f"finding, its declaration and decision test), write {run_dir_rel}/{scope_signoff.LEAD_ANSWERS} (one line "
            "per finding: `\"<finding>\": {decision: accept|decline, note: <why>}`), then end your turn with the line "
            "AWAITING_SIGNOFF; the harness signs your answers and resumes you.")


def create_hooks(run_path: str, trace_file, start_time: datetime,
                 registered_agents=None, max_turns: dict[str, int] | None = None,
                 integrity=None, budget_status=None, max_rounds: int | None = None,
                 hold_writer: bool = True, signoff: str | None = None):
    """Create hook config for the lead query() session.

    Phase 21 α: PreToolUse also enforces file ownership and the agent
    registry via agents.enforcement.check_tool_use (deny, not warn);
    PostToolUse snapshots report.md whenever it changes.
    `registered_agents` defaults to the full registry.
    """

    tool_count = 0
    per_agent_tool_counts: dict[str, int] = {}
    turn_warned: dict[str, set] = {}
    agent_types: dict[str, str] = {}
    snapshot_state: dict = {}
    session_t0 = time.time()   # Phase 32 γ: background agents end with their session
    if registered_agents is None:
        registered_agents = set(_registry().keys())
    registered_agents = set(registered_agents)
    if max_turns is None:
        max_turns = {n: d.maxTurns for n, d in _registry().items()}

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
            return (f"Agent[{input_data.get('subagent_type', '?')}]: "
                    f"{input_data.get('description', '')}")
        return name

    async def pre_tool_hook(input_data, tool_use_id, context):
        nonlocal tool_count
        tool_count += 1
        tool_name = input_data.get("tool_name", "unknown")
        tool_input = input_data.get("tool_input", {}) or {}
        agent_id = input_data.get("agent_id", "lead")
        # agent_type is absent on the main thread (the lead).
        agent_type = input_data.get("agent_type") if "agent_id" in input_data else None
        if agent_type is None and "agent_id" in input_data:
            # Inside a subagent whose type the CLI didn't report: use the
            # SubagentStart record; never fall back to the lead's
            # (stricter, ownership-based) rules for a specialist.
            agent_type = agent_types.get(agent_id, "unknown-subagent")
        per_agent_tool_counts[agent_id] = per_agent_tool_counts.get(agent_id, 0) + 1
        summary = _format_tool(tool_name, tool_input)
        elapsed = (datetime.now() - start_time).total_seconds()
        label = agent_type or agent_id
        deny_reason = enforcement.check_tool_use(
            tool_name, tool_input, run_path, agent_type, registered_agents,
            cwd=input_data.get("cwd"))
        spawn = agent_id == "lead" and tool_name in ("Agent", "Task")
        sub = tool_input.get("subagent_type") if spawn else None
        if not deny_reason and spawn and sub != "planner":
            deny_reason = signoff_hold_reason(run_path, signoff, run_path)
        if (not deny_reason and signoff == "owner" and tool_name == "Bash"
                and re.search(r"decision_brief\.py\b.*\bsign\b", str(tool_input.get("command") or ""), re.S)):
            deny_reason = "The owner signs the decision brief in this run; agents cannot sign it."
        if (not deny_reason and signoff in ("owner", "lead") and tool_name == "Bash"
                and re.search(r"scope_signoff\.py\b.*\bsign\b", str(tool_input.get("command") or ""), re.S)):
            deny_reason = "Agents cannot sign scope declarations (the harness records the answers)."   # Phase 49 β
        if not deny_reason and spawn and sub == "writer":
            deny_reason = scope_hold_reason(run_path, signoff, run_path)   # Phase 49 β
        if not deny_reason and spawn:
            deny_reason = review_wait_reason(run_path, sub, session_t0)
        if not deny_reason and hold_writer and sub == "writer":
            deny_reason = writer_hold_reason(run_path, _status(), max_rounds)
        if not deny_reason and hold_writer and sub in PLAN_AGENTS:
            deny_reason = plan_hold_reason(run_path)
        head = str(tool_input.get("prompt") or "").lstrip()
        if not deny_reason and hold_writer and sub in open_problems.FIX_CHECK_CRITICS \
                and not head.startswith(CRITIC_PROMPT_HEADS):
            # Codex review: the round's holds keyed on one phrase, so a reworded
            # full critique skipped them. Every critic spawn now says what it is.
            deny_reason = (f"Start a {sub} prompt with one of: `This is critique round N.` (the round's "
                           "review), `This is a fix check after round N.` (check planned fixes), or "
                           "`This is a repair of <file>.` (fix only that file's format, no new review).")
        if not deny_reason and hold_writer and sub == "critique-presentation" \
                and head.startswith("This is critique round") and not open_problems._written(run_path):
            # Phase 39 α: presentation is reviewed once, on the first draft; before it,
            # its findings could not be acted on (they wait for the draft).
            deny_reason = ("critique-presentation reviews the write-up, so its round review waits for the first "
                           "draft (manuscript.md). Spawn critique-domain and critique-redteam for this round; run "
                           "critique-presentation after STAGE 8 on the draft.")
        if not deny_reason and hold_writer and sub in open_problems.FIX_CHECK_CRITICS \
                and head.startswith("This is critique round"):
            deny_reason = critique_hold_reason(run_path, _status())
        if not deny_reason and hold_writer and sub in PLAN_AGENTS and _plan_required(run_path) is not None:
            try:   # Phase 32 γ: the round's actions are fixed once work is handed out
                if open_problems.freeze(run_path) and integrity is not None:
                    integrity.harness_wrote(open_problems.FROZEN_FILE)
            except OSError:
                pass
        status = "DENIED" if deny_reason else ""
        print(f"[{label:>12} {elapsed:6.0f}s | #{tool_count}] {status} {summary}".replace("]  ", "] "),
              flush=True)
        record = {
            "ts": datetime.now().isoformat(),
            "elapsed_s": elapsed,
            "agent": agent_id,
            "agent_type": agent_type or ("lead" if agent_id == "lead" else None),
            "type": "tool_use",
            "tool": tool_name,
            "summary": summary,
        }
        if spawn:
            record["subagent_type"] = sub
            if str(sub or "").startswith("critique-"):
                head = str(tool_input.get("prompt") or "").lstrip()
                record["critique_kind"] = ("full" if head.startswith("This is critique round")
                                           else "fix_check" if head.startswith("This is a fix check")
                                           else "repair" if head.startswith("This is a repair of") else "other")
            record["mentions"] = open_problems.mentions(str(tool_input.get("prompt") or ""), run_path)
        if deny_reason:
            record["denied"] = deny_reason
        if tool_name == "Bash":
            # Phase 42 ε: which harness scripts the call runs, with its full command
            # (the summary is cut at 80 characters), so scripts/convergence_report.py
            # can count uncertainty runs and gate runs per round.
            try:
                from agents.integrity import harness_scripts_run
                cmd = str(tool_input.get("command") or "")
                ran = harness_scripts_run(cmd, input_data.get("cwd"))[0]
                if ran:
                    record["scripts_run"] = ran
                    record["command"] = cmd[:1000]
            except Exception:  # noqa: BLE001 — bookkeeping must not break enforcement
                pass
        _safe_write(trace_file, record)  # a closed trace must not disable enforcement
        if deny_reason:
            return enforcement.deny_output(deny_reason)
        if integrity is not None:
            # Phase 23 α: record what this allowed call may change.
            integrity.on_pre(tool_use_id, tool_name, tool_input, agent_type,
                             cwd=input_data.get("cwd"))
        return {}

    async def post_tool_hook(input_data, tool_use_id, context):
        """Check written PNG files for oversized figures; snapshot report.md."""
        tool_name = input_data.get("tool_name", "")
        tool_input = input_data.get("tool_input", {}) or {}
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
        snapshot_report(run_path, snapshot_state)
        if integrity is not None:
            for ev in integrity.on_post(tool_use_id):
                print(f"[INTEGRITY] unexplained {ev['change']} of {ev['file']} "
                      f"(in flight: {ev['in_flight'] or 'nothing'})", flush=True)
                _safe_write(trace_file, {"ts": ev["ts"], "type": "integrity_violation", **ev})
        if agent_id == "lead" and tool_name in ("Agent", "Task"):
            note = budget_note(_status(), len({r.get("round") for r in _gate_rounds(run_path)}))
            if note:
                return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": note}}
        warning = turn_warning(agent_id)
        if warning:
            return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": warning}}
        return {}

    def _status():
        try:
            return budget_status() if budget_status else None
        except Exception:  # noqa: BLE001 — a status error must not break a tool call
            return None

    def turn_warning(agent_id: str) -> str | None:
        """Phase 31: tell a subagent before its turn limit cuts it off, so it
        saves its outputs (E7 attempt 6: 6 of 47 spawns hit the limit; the red
        team's first session ended with no output). Tool calls are counted, a
        slight over-count of turns, so the warning comes early, never late."""
        if agent_id == "lead":
            return None
        cap = max_turns.get(agent_types.get(agent_id, ""))
        used = per_agent_tool_counts.get(agent_id, 0)
        if not cap or cap < 10:
            return None
        left = cap - used
        done = turn_warned.setdefault(agent_id, set())
        level = ("early" if used >= min(int(cap * 0.85), cap - 4) and "early" not in done
                 else "final" if left <= 3 and "final" not in done else None)
        if level is None:
            return None
        turn_warned[agent_id].add(level)
        _safe_write(trace_file, {"ts": datetime.now().isoformat(), "type": "turn_warning", "agent_id": agent_id,
                                 "agent_type": agent_types.get(agent_id), "used": used, "cap": cap})
        if level == "final":
            return (f"[harness] About {max(left, 0)} turns left of your {cap}. Your session will be cut off at the "
                    "limit. Write your required output files NOW, then stop; summarise what is unfinished in them.")
        return (f"[harness] You have used about {used} of your {cap} turns. Start writing your required output "
                "files now; a session cut off at the limit loses whatever is not saved to disk.")

    async def subagent_start_hook(input_data, tool_use_id, context):
        agent_id = input_data.get("agent_id", "?")
        agent_type = input_data.get("agent_type", "?")
        agent_types[agent_id] = agent_type
        elapsed = (datetime.now() - start_time).total_seconds()
        print(f"\n--- SUBAGENT START: {agent_type} (id={agent_id}) [{elapsed:.0f}s] ---", flush=True)
        _safe_write(trace_file, {
            "ts": datetime.now().isoformat(),
            "elapsed_s": elapsed,
            "type": "subagent_start",
            "agent_id": agent_id,
            "agent_type": agent_type,
        })
        return {}

    async def subagent_stop_hook(input_data, tool_use_id, context):
        agent_id = input_data.get("agent_id", "?")
        agent_type = input_data.get("agent_type", "?")
        elapsed = (datetime.now() - start_time).total_seconds()
        turns_used = per_agent_tool_counts.get(agent_id, 0)
        cap = max_turns.get(agent_type)
        if cap is None:
            approx_stop_reason = "unknown"
        elif turns_used >= cap - 2:
            approx_stop_reason = "ran_to_cap"
        else:
            approx_stop_reason = "completed_under_cap"
        print(
            f"--- SUBAGENT STOP: {agent_type} (id={agent_id}) [{elapsed:.0f}s] "
            f"turns={turns_used} {approx_stop_reason} ---\n",
            flush=True,
        )
        _safe_write(trace_file, {
            "ts": datetime.now().isoformat(),
            "elapsed_s": elapsed,
            "type": "subagent_stop",
            "agent_id": agent_id,
            "agent_type": agent_type,
            "turns_used": turns_used,
            "approx_stop_reason": approx_stop_reason,
        })
        # Phase 39 β: figures drawn from this stage's outputs are regenerated by
        # the harness before anyone reads them (the analyst, the critics, the gate).
        if agent_type in REBUILD_AFTER:
            used = 0.0
            if integrity is not None and os.path.exists(os.path.join(run_path, derived_rebuild.pipeline.SPEC)):
                # S1: the run's declared pipeline is rebuilt by the harness first (its
                # outputs are harness-owned); figure sidecars cover what it does not declare.
                try:
                    import asyncio
                    t_p = time.time()
                    prep = await asyncio.to_thread(derived_rebuild.pipeline.harness_build, run_path, integrity,
                                                   derived_rebuild.HOOK_BUDGET_S * 0.7)
                    used = time.time() - t_p
                    pline = derived_rebuild.pipeline.summary_line(prep)
                    print(f"[PIPELINE] {pline}", flush=True)
                    _safe_write(trace_file, {"ts": datetime.now().isoformat(), "type": "pipeline_build",
                                             "after": agent_type, "summary": pline, "ok": prep.get("ok"),
                                             "steps": [{"id": r["id"], "status": r.get("status")}
                                                       for r in prep.get("steps", [])],
                                             "errors": prep.get("errors")})
                except Exception as e:  # noqa: BLE001 — a build failure must not end the session
                    print(f"[PIPELINE] failed: {type(e).__name__}: {e}", flush=True)
            try:
                import asyncio
                rep = await asyncio.to_thread(derived_rebuild.rebuild, run_path,
                                              budget_s=max(0.0, derived_rebuild.HOOK_BUDGET_S - used),
                                              integrity=integrity)
                line = derived_rebuild.summary_line(rep)
                print(f"[REBUILD] {line}", flush=True)
                _safe_write(trace_file, {"ts": datetime.now().isoformat(), "type": "derived_rebuild",
                                         "after": agent_type, "summary": line,
                                         "rebuilt": len(rep["rebuilt"]), "still_stale": rep["still_stale"],
                                         "provenance_rerecorded": rep.get("provenance_rerecorded", []),
                                         "failed": [r["script"] for r in rep["runs"] if r["returncode"] != 0]})
            except Exception as e:  # noqa: BLE001 — a rebuild failure must not end the session
                print(f"[REBUILD] failed: {type(e).__name__}: {e}", flush=True)
        return {}

    return {
        "PreToolUse": [HookMatcher(matcher=None, hooks=[pre_tool_hook])],
        "PostToolUse": [HookMatcher(matcher=None, hooks=[post_tool_hook])],
        "SubagentStart": [HookMatcher(matcher=None, hooks=[subagent_start_hook])],
        "SubagentStop": [HookMatcher(matcher=None, hooks=[subagent_stop_hook])],
    }
