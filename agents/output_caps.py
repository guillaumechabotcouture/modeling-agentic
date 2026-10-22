"""History-growth levers for stage agents (architecture v2 §3.3, experiment E2).

E1 measurement (experiments/E2_history_growth/measure.py): a 60-turn modeler
session grows from ~32K to ~260K context tokens. Visible content accounts for
~90K of that (tool results ~52K, Write inputs ~29K, Bash commands ~9K); the
rest is retained extended-thinking blocks (transcripts store only their
signatures, ~280K chars per session). Every later turn re-reads all of it.

Levers, all opt-in per replay variant:
  tool_output_cap   cap what Bash output and file Reads add to context
                    (PostToolUse ``updatedToolOutput`` for Bash; PreToolUse
                    ``updatedInput`` adding a line ``limit`` for Read). The
                    full Bash output is spilled to a file whose path is given
                    to the agent, so nothing is lost.
  compaction        CLAUDE_CODE_AUTO_COMPACT_WINDOW for the session (see
                    compaction_env); the CLI then compacts when the context
                    nears that window instead of the model's 2M limit.
  fresh_context_every   implemented in scripts/replay.py (segmented sessions
                    with a deterministic handoff built by build_handoff()).

    python3 agents/output_caps.py --self-test
"""
from __future__ import annotations

import os
import sys

SPILL_DIRNAME = ".tool_outputs"


# --------------------------------------------------------------------------- Bash output

def cap_text(text: str, cap: int) -> tuple[str, int]:
    """Keep the head (40%) and tail (60%) of text within cap chars.
    Returns (capped_text, omitted_chars)."""
    text = text or ""
    if len(text) <= cap:
        return text, 0
    head = int(cap * 0.4)
    tail = cap - head
    omitted = len(text) - head - tail
    return text[:head] + f"\n… [{omitted} chars omitted] …\n" + text[-tail:], omitted


def cap_bash_response(response, cap: int, spill_path: str | None):
    """Return a replacement Bash tool response (same shape) if the output
    exceeds cap, else None. Bash responses look like
    {"stdout": str, "stderr": str, "interrupted": bool, ...}."""
    if not isinstance(response, dict):
        return None
    out, err = response.get("stdout") or "", response.get("stderr") or ""
    if len(out) + len(err) <= cap:
        return None
    if spill_path:
        os.makedirs(os.path.dirname(spill_path), exist_ok=True)
        with open(spill_path, "w") as f:
            f.write(out)
            if err:
                f.write("\n--- stderr ---\n" + err)
    err_cap = min(len(err), cap // 4)
    new_out, omitted_out = cap_text(out, cap - err_cap)
    new_err, omitted_err = cap_text(err, err_cap) if err else ("", 0)
    note = (f"\n[output capped: {omitted_out + omitted_err} chars omitted"
            + (f"; full output saved to {spill_path} — read slices of it only if needed]" if spill_path else "]"))
    new = dict(response)
    new["stdout"] = new_out + note
    new["stderr"] = new_err
    return new


# --------------------------------------------------------------------------- Read input

def cap_read_input(tool_input: dict, max_lines: int) -> dict | None:
    """Return an updated Read input with a line limit if the call reads a
    whole large text file, else None. Respects an explicit offset/limit."""
    if not isinstance(tool_input, dict) or tool_input.get("limit") or tool_input.get("offset"):
        return None
    path = tool_input.get("file_path") or ""
    if not path or not os.path.isfile(path) or path.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".pdf", ".ipynb")):
        return None
    try:
        with open(path, "rb") as f:
            n = sum(1 for _ in f)
    except OSError:
        return None
    if n <= max_lines:
        return None
    return {**tool_input, "limit": max_lines}


# --------------------------------------------------------------------------- compaction

def compaction_env(cfg) -> dict:
    """Variant `compaction: {window_tokens: N}` -> env for the CLI session.
    CLAUDE_CODE_AUTO_COMPACT_WINDOW is read by the bundled CLI (2.1.30x) and
    takes precedence over the autoCompactWindow setting."""
    if not cfg:
        return {}
    n = int(cfg.get("window_tokens") if isinstance(cfg, dict) else cfg)
    return {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": str(n)}


# --------------------------------------------------------------------------- fresh-context handoff

WORKLOG_REL = "models/WORKLOG.md"
WORKLOG_INSTRUCTION = (
    "\n\n## Working log (required)\n"
    f"Maintain `{{run_dir}}/{WORKLOG_REL}` as you go: after each milestone append a short entry "
    "with what you did, decisions and why, files written, and what remains. This session may be "
    "ended and continued by a fresh agent that only sees this log, the task, and the file list.")


def build_handoff(task: str, worklog: str, files: list[tuple[str, int]], segment: int,
                  turns_used: int, turns_total: int, worklog_cap: int = 12000) -> str:
    """Deterministic handoff prompt for segment `segment` (2, 3, …).
    `files`: (relpath, bytes) written by the stage so far, sorted by relpath."""
    log = (worklog or "").strip() or "(the previous agent left no working log)"
    log, _ = cap_text(log, worklog_cap)
    listing = "\n".join(f"- {p} ({b} bytes)" for p, b in sorted(files)) or "- (none)"
    return (
        f"CONTINUATION, segment {segment}. A previous agent worked on this task for "
        f"{turns_used} of {turns_total} turns and was ended to keep context small. You are a fresh "
        "agent: read only what you need from the files below, do not redo finished work, and "
        "continue from the working log. Keep appending to the working log.\n\n"
        f"## Original task\n{task}\n\n## Working log so far\n{log}\n\n"
        f"## Files written so far (relative to the run directory)\n{listing}\n")


def written_files(run_dir: str, since: float, exclude=(SPILL_DIRNAME,)) -> list[tuple[str, int]]:
    out = []
    for root, dirs, files in os.walk(run_dir):
        dirs[:] = [d for d in dirs if d not in exclude and d != "__pycache__"]
        for fn in files:
            p = os.path.join(root, fn)
            try:
                st = os.stat(p)
            except OSError:
                continue
            if st.st_mtime >= since:
                out.append((os.path.relpath(p, run_dir), st.st_size))
    return sorted(out)


# --------------------------------------------------------------------------- hook wrapping

def wrap_hooks(hooks: dict, cfg: dict, run_dir: str) -> dict:
    """Wrap an existing hooks dict (from agents.create_hooks) with output caps.
    cfg: {"bash_chars": int, "read_lines": int}. Enforcement runs first: a
    denied call is returned unchanged and never rewritten."""
    from claude_agent_sdk import HookMatcher
    bash_chars = int(cfg.get("bash_chars", 0) or 0)
    read_lines = int(cfg.get("read_lines", 0) or 0)
    spill_root = os.path.join(run_dir, SPILL_DIRNAME)

    def _chain(event):
        return [h for m in hooks.get(event, []) for h in m.hooks]

    pre_orig, post_orig = _chain("PreToolUse"), _chain("PostToolUse")

    async def pre(input_data, tool_use_id, context):
        res = {}
        for h in pre_orig:
            r = await h(input_data, tool_use_id, context) or {}
            hso = r.get("hookSpecificOutput") or {}
            if hso.get("permissionDecision") == "deny":
                return r
            res = r or res
        if read_lines and input_data.get("tool_name") == "Read":
            new = cap_read_input(input_data.get("tool_input") or {}, read_lines)
            if new:
                return {"hookSpecificOutput": {
                    "hookEventName": "PreToolUse", "permissionDecision": "allow", "updatedInput": new,
                    "additionalContext": (f"[read capped to the first {read_lines} lines; "
                                          "use offset/limit to read further sections if needed]")}}
        return res

    async def post(input_data, tool_use_id, context):
        res = {}
        for h in post_orig:
            res = (await h(input_data, tool_use_id, context)) or res
        if bash_chars and input_data.get("tool_name") == "Bash":
            spill = os.path.join(spill_root, f"{tool_use_id or input_data.get('tool_use_id', 'x')}.txt")
            new = cap_bash_response(input_data.get("tool_response"), bash_chars, spill)
            if new is not None:
                return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "updatedToolOutput": new}}
        return res

    out = dict(hooks)
    out["PreToolUse"] = [HookMatcher(matcher=None, hooks=[pre])]
    out["PostToolUse"] = [HookMatcher(matcher=None, hooks=[post])]
    return out


# --------------------------------------------------------------------------- self-test

def _self_test() -> int:
    import asyncio
    import tempfile
    fails = []

    def ok(c, m):
        if not c:
            fails.append(m)

    t, om = cap_text("a" * 1000, 100)
    ok(om == 900 and t.startswith("a" * 40) and t.endswith("a" * 60), "C1 cap_text head/tail")
    ok(cap_text("short", 100) == ("short", 0), "C2 cap_text passthrough")
    with tempfile.TemporaryDirectory() as td:
        spill = os.path.join(td, ".tool_outputs", "t1.txt")
        r = cap_bash_response({"stdout": "x" * 50000, "stderr": "", "interrupted": False}, 2000, spill)
        ok(r and len(r["stdout"]) < 2300 and "full output saved to" in r["stdout"] and r["interrupted"] is False,
           "B1 bash capped, shape kept")
        ok(os.path.getsize(spill) == 50000, "B2 full output spilled")
        ok(cap_bash_response({"stdout": "ok", "stderr": ""}, 2000, spill) is None, "B3 small output untouched")
        ok(cap_bash_response("not a dict", 10, None) is None, "B4 unknown shape untouched")
        big = os.path.join(td, "big.md")
        open(big, "w").write("line\n" * 1000)
        ok(cap_read_input({"file_path": big}, 300) == {"file_path": big, "limit": 300}, "R1 read limit added")
        ok(cap_read_input({"file_path": big, "offset": 10, "limit": 50}, 300) is None, "R2 explicit slice kept")
        small = os.path.join(td, "small.md")
        open(small, "w").write("a\nb\n")
        ok(cap_read_input({"file_path": small}, 300) is None, "R3 small file untouched")
        ok(compaction_env({"window_tokens": 150000}) == {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "150000"}, "K1 compaction env")
        ok(compaction_env(None) == {}, "K2 compaction off")
        h1 = build_handoff("T", "did A", [("models/b.py", 2), ("models/a.py", 1)], 2, 20, 60)
        h2 = build_handoff("T", "did A", [("models/a.py", 1), ("models/b.py", 2)], 2, 20, 60)
        ok(h1 == h2 and h1.index("models/a.py") < h1.index("models/b.py") and "segment 2" in h1
           and "20 of 60" in h1, "H1 handoff deterministic and ordered")
        ok("no working log" in build_handoff("T", "", [], 3, 40, 60), "H2 empty worklog")
        os.makedirs(os.path.join(td, "run", "models")); os.makedirs(os.path.join(td, "run", ".tool_outputs"))
        open(os.path.join(td, "run", "models", "m.py"), "w").write("x")
        open(os.path.join(td, "run", ".tool_outputs", "s.txt"), "w").write("y")
        ok(written_files(os.path.join(td, "run"), 0) == [("models/m.py", 1)], "W1 written files excludes spill dir")

        # hook wrapping: enforcement deny passes through; caps apply otherwise
        try:
            from claude_agent_sdk import HookMatcher
        except ImportError:
            HookMatcher = None
        if HookMatcher:
            async def deny_pre(i, t, c):
                if i.get("tool_input", {}).get("file_path", "").endswith("secret"):
                    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                   "permissionDecisionReason": "no"}}
                return {}
            async def noop_post(i, t, c):
                return {}
            hooks = {"PreToolUse": [HookMatcher(matcher=None, hooks=[deny_pre])],
                     "PostToolUse": [HookMatcher(matcher=None, hooks=[noop_post])]}
            w = wrap_hooks(hooks, {"bash_chars": 1000, "read_lines": 300}, os.path.join(td, "run"))
            pre, post = w["PreToolUse"][0].hooks[0], w["PostToolUse"][0].hooks[0]
            r = asyncio.run(pre({"tool_name": "Read", "tool_input": {"file_path": big + "secret"}}, "u1", None))
            ok(r["hookSpecificOutput"]["permissionDecision"] == "deny", "HW1 deny wins over cap")
            r = asyncio.run(pre({"tool_name": "Read", "tool_input": {"file_path": big}}, "u2", None))
            ok(r["hookSpecificOutput"].get("updatedInput", {}).get("limit") == 300, "HW2 read capped")
            r = asyncio.run(post({"tool_name": "Bash", "tool_input": {}, "tool_use_id": "u3",
                                  "tool_response": {"stdout": "z" * 9000, "stderr": "", "interrupted": False}}, "u3", None))
            ok("updatedToolOutput" in r["hookSpecificOutput"]
               and os.path.exists(os.path.join(td, "run", ".tool_outputs", "u3.txt")), "HW3 bash capped + spilled")
            r = asyncio.run(post({"tool_name": "Bash", "tool_response": {"stdout": "small", "stderr": ""}}, "u4", None))
            ok(r == {}, "HW4 small bash untouched")
    if fails:
        print("SELF-TEST FAILURES:", *fails, sep="\n  - ")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    print(__doc__)
