"""Phase 21 α: structural enforcement for the lead session.

The STAGE 7 gate is only meaningful if the files it reads were produced
by the agents and scripts that own them. Before Phase 21 that rested on
prompt prose: the lead (and any built-in ``general-purpose`` agent it
spawned) could and did rewrite critique YAMLs, rigor-artifact verdicts,
and even gate code mid-run. This module moves those rules into code:

- ``check_tool_use`` is called from the PreToolUse hook and returns a
  deny reason (or None) for Write/Edit/Bash/Read/Agent calls that touch
  protected paths or spawn unregistered agents.
- ``hash_harness`` / ``diff_harness`` give tamper evidence: main.py
  hashes the harness at run start and re-hashes at run end, so a write
  that slips past the Bash heuristics still surfaces as
  ``harness_tampered``.
- ``filtered_env`` strips cloud credentials from the agent subprocess
  environment unless cloud execution was requested.

Everything here is disease-agnostic: protection is defined by file
ownership (harness code, critique outputs, modeler outputs, script
outputs), never by domain content.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import shlex

REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))

# Harness code and configuration. No agent may write these.
HARNESS_DIRS = ("scripts", "agents", ".claude")
HARNESS_FILES = ("main.py", "CLAUDE.md", "CHANGELOG.md", "requirements.txt",
                 "requirements-cloud.txt")
# Phase 31: the lessons ledger records expert findings about the agents' own
# work, so agents may not edit it. Read-only but not hashed: observations are
# appended while runs are live.
PROTECTED_REPO_DIRS = ("docs/lessons",)

# Subtrees hashed for tamper evidence. `.claude/` as a whole also holds
# lock files and local settings that change legitimately, so only the
# orchestration manifests and skills are hashed.
HASHED_DIRS = ("scripts", "agents", ".claude/orchestration", ".claude/skills")
HASHED_FILES = HARNESS_FILES
_HASH_SKIP_DIRS = {"__pycache__", ".pytest_cache"}
_HASH_SKIP_SUFFIXES = (".pyc", ".pyo")

# Files inside a run directory that only a script may produce. Writing
# them with Write/Edit is how a verdict gets rewritten
# (e.g. DETECTED -> ACKNOWLEDGED) without the evidence changing. Paths
# are relative to the run directory; globs allowed.
SCRIPT_OUTPUTS = (
    "model_comparison_formal.yaml",
    "models/model_comparison_formal.yaml",
    "identifiability.yaml",
    "uncertainty_report.yaml",
    "coherence_audit.yaml",
    "writer_qa_report.yaml",
    "benchmark_match.yaml",
    "effort_floors_report.yaml",
    "sanity_report.yaml",
    "validator_history.jsonl",       # Phase 21 β round-over-round memory
    "critique_crossmodel.yaml",      # Phase 21 ζ: scripts/cross_model_critic.py
    "critique_crossmodel.yaml.tmp",
    "critique_crossmodel.md",
    "models/eligibility_probe.yaml",  # scripts/eligibility_probe.py
    "models/claims_ledger.yaml",      # Phase 22 γ: scripts/build_claims_ledger.py
    "claims_ledger.yaml",
    "report.md",          # produced by scripts/render_claims.py
    # Written by main.py itself (plain Python, not a tool call). The
    # validator reads the research question from metadata.json, so an
    # agent rewriting it could neuter spec-compliance.
    "metadata.json",
    "harness_manifest.json",
    "trace.jsonl",
    "final_gate.json",
    "final_gate_stderr.txt",
    "artifact_integrity.json",   # Phase 23 α: written by agents/integrity.py only
    "budget_ledger.json",        # Phase 23 β: run-wide spend, written by main.py only
    "open_problems.json",        # Phase 32 α: written by the gate
    "open_problems.md",
    "open_problems.json.tmp",
    "open_problems.md.tmp",
    "decision_brief.signoff.json",   # Phase 33 α: scripts/decision_brief.py sign
    "decision_brief.signed/*",       # the signed copies, for restoring a changed file
    "acceptance_lock.json",          # Phase 33 α: written by the gate
    "done_when_registry.json",       # Phase 34: frozen finding tests, written by the gate
    "done_when_registry.json.tmp",
    "round_plan.frozen.json",        # Phase 32 γ: the plan as handed out, written by the hook
    "round_plan.frozen.json.tmp",
)

# Phase 33 α (Gemini review): frozen once decision_brief.signoff.json exists.
SIGNED_FILES = ("decision_brief.yaml", "success_criteria.yaml")

# Files only one writer may change (None = the lead). Phase 32 α: the round
# plan; Phase 33 α: the decision brief (planner) and its amendments (lead).
OWNED_BY = {"round_plan.yaml": {None}, "brief_amendments.yaml": {None},
            "decision_brief.yaml": {"planner"}}

# Files the lead may not write directly: it must re-spawn the owning
# agent. critique_* belong to the critique agents; models/** to the
# modeler/analyst/planner.
LEAD_PROTECTED = (
    "critique_*.yaml",
    "critique_*.md",
    "fix_check_*.yaml",   # Phase 32 β: written by the critics
    "models/*",
    "models/**",
)

# Narrow carve-outs from SCRIPT_OUTPUTS: the writer drafts manuscript.md (the CLI
# blocks subagent Writes to report.md; render_claims.py builds it) — the
# report.md carve-out is kept for the harness-era writer revision path;
# before render_claims runs; the planner records `decision_year` in
# metadata.json (main.py restores the original question at run end, so
# the carve-out cannot rewrite what spec-compliance checks against).
_OUTPUT_CARVE_OUTS = {"report.md": {"writer"}, "metadata.json": {"planner"}}

_WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
_AGENT_TOOLS = {"Agent", "Task"}

# Environment variable prefixes holding cloud / third-party credentials.
# They are only passed to agent subprocesses when cloud execution is on.
CLOUD_ENV_PREFIXES = ("AZ_", "AZURE_", "OPENAI_", "AWS_", "GOOGLE_")


# ---------------------------------------------------------------------------
# Path classification
# ---------------------------------------------------------------------------

def _abspath(path: str, cwd: str | None = None) -> str:
    path = os.path.expanduser(path.strip().strip("'\""))
    if not os.path.isabs(path):
        path = os.path.join(cwd or REPO_ROOT, path)
    return os.path.normpath(path)


def is_harness_path(path: str, cwd: str | None = None) -> bool:
    """True when `path` is harness code/config under the repo root."""
    p = _abspath(path, cwd)
    rel = os.path.relpath(p, REPO_ROOT)
    if rel.startswith(".."):
        return False
    if rel in HARNESS_FILES:
        return True
    # Gemini review: a parent of a protected directory reaches it too. (The
    # repo root itself is not listed: a relative `.` after an unknown `cd` is
    # checked against the root, and `cp x .` in a run directory is routine;
    # whole-repo git reverts are caught by the harness hash in main.py.)
    if rel != "." and any(d.startswith(rel + os.sep) for d in PROTECTED_REPO_DIRS):
        return True
    if any(rel == d or rel.startswith(d + os.sep) for d in PROTECTED_REPO_DIRS):
        return True
    first = rel.split(os.sep, 1)[0]
    return first in HARNESS_DIRS


def is_env_file(path: str, cwd: str | None = None) -> bool:
    """True for the repo's .env / .env.* credential files."""
    base = os.path.basename(_abspath(path, cwd))
    return base == ".env" or base.startswith(".env.")


def _run_rel(path: str, run_path: str, cwd: str | None = None) -> str | None:
    p = _abspath(path, cwd)
    rel = os.path.relpath(p, os.path.normpath(run_path))
    if rel.startswith(".."):
        return None
    return rel.replace(os.sep, "/")


def _matches(rel: str, patterns) -> bool:
    return any(fnmatch.fnmatch(rel, pat) for pat in patterns)


def protected_reason(path: str, run_path: str, agent_type: str | None,
                     cwd: str | None = None) -> str | None:
    """Return why `agent_type` (None = lead) may not write `path`."""
    if is_env_file(path, cwd):
        return "credential files (.env) are not accessible to agents"
    if is_harness_path(path, cwd):
        return ("harness code/config (scripts/, agents/, .claude/, main.py, "
                "CLAUDE.md, docs/lessons/) is read-only during a run")
    rel = _run_rel(path, run_path, cwd)
    if rel is None:
        return None
    if _matches(rel, SCRIPT_OUTPUTS):
        if agent_type in _OUTPUT_CARVE_OUTS.get(rel, ()):
            return None
        return (f"{rel} is produced by a pipeline script; re-run the script "
                f"instead of writing it by hand")
    # Codex review (Phases 31-34): critique and fix-check files belong to the
    # critic that wrote them; another specialist must not write a verdict.
    if agent_type is not None and _matches(rel, ("critique_*.yaml", "critique_*.md")) \
            and not str(agent_type).startswith("critique-"):
        return f"{rel} is written only by the critics"
    m = re.fullmatch(r"fix_check_r\d+_([a-z]+)\.yaml", rel)
    if m and agent_type is not None and agent_type != f"critique-{m.group(1)}":
        return f"{rel} is written only by critique-{m.group(1)}"
    if rel in SIGNED_FILES and os.path.exists(os.path.join(run_path, "decision_brief.signoff.json")):
        # Gemini review: once signed these are frozen for everyone; framing
        # changes go in brief_amendments.yaml (the lead), not the files.
        return (f"{rel} was signed by the owner and is frozen; record framing changes in "
                "brief_amendments.yaml")
    if rel in OWNED_BY and agent_type not in OWNED_BY[rel]:
        who = ", ".join("the lead" if w is None else w for w in OWNED_BY[rel])
        return f"{rel} is written only by {who}"
    if agent_type is None and _matches(rel, LEAD_PROTECTED):
        return (f"{rel} is owned by a specialist agent; the lead must "
                f"re-spawn the owning agent rather than edit it")
    return None


# ---------------------------------------------------------------------------
# Bash heuristics
# ---------------------------------------------------------------------------

_REDIRECT = re.compile(r"(?:\d|&)?>>?\s*([^\s;&|<>]+)")
_PY_WRITE = re.compile(
    r"""open\(\s*(?:f?['"])([^'"]+)['"]\s*,\s*['"][wax+]"""
    r"""|Path\(\s*(?:f?['"])([^'"]+)['"]\s*\)\s*\.\s*write_(?:text|bytes)"""
)
_ENV_REF = re.compile(r"""(?:^|[\s/'"=<(])\.env(?:\.[\w-]+)?(?=$|[\s'";|&)<>])""")
# Commands whose path arguments are all written / destroyed.
_DESTRUCTIVE_ALL_ARGS = {"rm", "rmdir", "truncate", "shred", "chmod", "chown",
                         "touch", "unlink"}
# Commands whose last path argument is the destination.
_DEST_LAST_ARG = {"cp", "mv", "install", "ln", "rsync"}
# In-place editors: any path argument is modified.
_INPLACE = {"sed", "perl", "gsed"}
_GIT_WRITE = {"checkout", "restore", "apply", "am", "stash", "reset",
              "clean", "rm", "mv"}


def _tokens(segment: str) -> list[str]:
    try:
        return shlex.split(segment, posix=True)
    except ValueError:
        return segment.split()


def _segment_targets(segment: str) -> list[str]:
    """Paths one shell segment writes to (no cwd handling)."""
    targets: list[str] = [m.group(1) for m in _REDIRECT.finditer(segment)
                          if m.group(1) not in ("/dev/null", "&1", "&2")]
    for m in _PY_WRITE.finditer(segment):
        targets.append(m.group(1) or m.group(2))
    toks = _tokens(segment.strip().lstrip("({").strip())
    # Skip leading env assignments / sudo / command wrappers.
    while toks and (re.match(r"^\w+=", toks[0]) or toks[0] in ("sudo", "command", "env")):
        toks = toks[1:]
    if not toks:
        return targets
    cmd = os.path.basename(toks[0])
    args = [t for t in toks[1:] if not t.startswith("-")]
    if cmd == "tee":
        targets.extend(args)
    elif cmd in _DESTRUCTIVE_ALL_ARGS:
        targets.extend(args)
    elif cmd in _DEST_LAST_ARG and args:
        targets.append(args[-1])
        if cmd == "mv":
            targets.extend(args[:-1])  # moving a file away also removes it
    elif cmd in _INPLACE and any(t.startswith("-i") or t == "--in-place"
                                 or (t.startswith("-") and "i" in t[1:] and cmd == "perl")
                                 for t in toks[1:]):
        targets.extend(args[1:] if args else [])
    elif cmd == "git" and len(toks) > 1 and toks[1] in _GIT_WRITE:
        path_args = [t for t in toks[2:] if not t.startswith("-") and t != "--"]
        targets.extend(path_args or ["."])
    return targets


def _cd_target(segment: str, cur: str | None) -> tuple[bool, str | None]:
    """(is_cd, new_cwd). new_cwd is None when the destination can't be
    known statically (`cd $X`, `cd -`, `cd` with a glob)."""
    toks = _tokens(segment.strip().lstrip("({").strip())
    if not toks or toks[0] not in ("cd", "pushd"):
        return False, cur
    args = [t for t in toks[1:] if not t.startswith("-") or t == "-"]
    dest = args[0] if args else "~"
    dest = os.path.expanduser(dest)
    if dest == "-" or any(c in dest for c in "$`*?["):
        return True, None
    if os.path.isabs(dest):
        return True, os.path.normpath(dest)
    if cur is None:
        return True, None  # relative to an unknown directory is unknown
    return True, os.path.normpath(os.path.join(cur, dest))


def _shell_tokens(command: str) -> list[str]:
    """Split a command into simple-command segments and `(` / `)` markers.

    One quote- and escape-aware scan: separators (`&&`, `||`, `;`, `|`,
    `&`, newline) and parentheses inside quotes or after a backslash are
    text, not syntax. Splitting and quote handling must agree, or a quoted
    `;` or `)` desynchronises subshell tracking (Codex reviews of PR #14).
    `&` adjacent to `>`/`<` is part of a redirection (`2>&1`, `&>`).
    """
    toks: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i, n = 0, len(command)

    def flush():
        if "".join(buf).strip():
            toks.append("".join(buf))
        buf.clear()
    while i < n:
        c = command[i]
        if c == "\\" and quote != "'":
            buf.append(command[i:i + 2]); i += 2
            continue
        if quote:
            buf.append(c)
            if c == quote:
                quote = None
            i += 1
            continue
        if c in ("'", '"'):
            quote = c; buf.append(c); i += 1
            continue
        if c == "#" and (not buf or buf[-1].isspace()):
            # A comment runs to end of line; its quotes and words are not
            # syntax and must not authorize anything (Codex review, PR #15).
            j = command.find("\n", i)
            i = n if j < 0 else j
            continue
        if command.startswith(("&&", "||"), i):
            flush(); i += 2
            continue
        if c == "&" and ((i > 0 and command[i - 1] in "<>") or (i + 1 < n and command[i + 1] in "<>")):
            buf.append(c); i += 1
            continue
        if c in ";|&\n":
            flush(); i += 1
            continue
        if c in "()":
            flush(); toks.append(c); i += 1
            continue
        buf.append(c); i += 1
    flush()
    return toks


def iter_segments_with_cwd(command: str, cwd: str | None = None
                           ) -> list[tuple[str, str | None]]:
    """(segment, cwd before it) for every simple command, tracking `( … )`
    subshells, `cd`, and the `pushd`/`popd` stack (Codex review, PR #15).
    cwd None = unknown (after `cd $X`, or `popd` on an empty stack)."""
    out: list[tuple[str, str | None]] = []
    cur: str | None = cwd or REPO_ROOT
    sub: list[tuple[str | None, list]] = []     # (cwd, dirstack) at each `(`
    dirs: list[str | None] = []
    for tok in _shell_tokens(command):
        if tok == "(":
            sub.append((cur, list(dirs)))
            continue
        if tok == ")":
            if sub:
                cur, dirs = sub.pop()
            continue
        body = tok.strip()
        out.append((body, cur))
        toks = _tokens(body) or [""]
        head, no_cd = toks[0], "-n" in toks[1:]
        if head == "popd":
            # `popd -n` drops a stack entry without changing directory.
            top = dirs.pop() if dirs else None
            if not no_cd:
                cur = top
            continue
        before = cur
        is_cd, new = _cd_target(body, cur)
        if is_cd:
            if head == "pushd" and no_cd:
                dirs.append(new)         # `pushd -n` adds to the stack only
                continue
            if head == "pushd":
                dirs.append(before)
            cur = new
    return out


def bash_write_targets_cwd(command: str, cwd: str | None = None
                           ) -> list[tuple[str, str | None]]:
    """(path, cwd it is relative to) for every path a command writes.

    Tracks `cd` / `pushd` across `&&`, `;`, `|` and newlines, so
    `cd runs/x && echo forged > critique_domain.yaml` resolves inside the
    run directory. After a `cd` whose destination is unknown, the cwd is
    None and callers must treat relative targets conservatively.
    """
    out: list[tuple[str, str | None]] = []
    # Redirections on a cd segment apply in the cwd before it (`cd /tmp > x`).
    for body, cur in iter_segments_with_cwd(command, cwd):
        out.extend((t, cur) for t in _segment_targets(body))
    return out


def bash_write_targets(command: str) -> list[str]:
    """Best-effort list of paths a shell command writes to.

    Heuristic, not a shell parser. It covers the forms observed in past
    runs (redirection, tee, sed -i, cp/mv/rm, git checkout/restore, and
    python one-liners with open(..., 'w')). Anything it misses in harness
    code is caught after the fact by the harness hash in main.py.

    Known limitation (Codex review of PR #14): these forms still evade the
    heuristic — variable
    expansion (`p=…; echo > "$p"`), nested shells (`bash -c "cd …; …"`), a
    `cd` that never runs (`false && cd x`) or runs in a pipeline,
    backslash-escaped filenames, quotes inside `#` comments, and process
substitution (`tee >(…) <protected>`). They are caught after the
    fact by agents/integrity.py (Phase 23 α), which hashes protected run
    artifacts and flags any change no allowed in-flight call explains.
    """
    return [t for t, _ in bash_write_targets_cwd(command)]


def bash_reason(command: str, run_path: str, agent_type: str | None,
                cwd: str | None = None) -> str | None:
    """Deny reason for a Bash command, or None."""
    if _CHMOD.search(command) and _HARNESS_WORDS.search(command):
        return ("harness code/config (scripts/, agents/, .claude/, main.py, CLAUDE.md) is read-only during a "
                "run; changing its permissions is not allowed")
    if _ENV_REF.search(command):
        return ("credential files (.env) are not accessible to agents; cloud "
                "credentials are provided in the environment when --cloud is set")
    for target, tcwd in bash_write_targets_cwd(command, cwd):
        if tcwd is not None or os.path.isabs(os.path.expanduser(target.strip("'\""))):
            candidates = [(target, tcwd)]
        else:
            # Unknown cwd after `cd $X`: a relative target (even `../x`) could
            # land anywhere in the run, so check its file name in every run
            # directory a protected file lives in, and at the repo root.
            name = os.path.basename(os.path.normpath(target)) or target
            candidates = [(name, d) for d in (run_path, os.path.join(run_path, "models"))]
            candidates += [(target, REPO_ROOT), (target, run_path)]
        for path, base in candidates:
            p = base if path in (".", "./") else path
            reason = protected_reason(p, run_path, agent_type, base)
            if reason:
                return f"command writes to {target}: {reason}"
    return None


# ---------------------------------------------------------------------------
# Hook entry point
# ---------------------------------------------------------------------------

BACKGROUND_DENY_REASON = (
    "run_in_background is not supported in this pipeline: when you end your "
    "turn to wait for a background job, the session closes and the pipeline "
    "stops. Run the command in the foreground instead. For jobs longer than "
    "the Bash timeout, start them with `nohup ... > <log> 2>&1 &` inside the "
    "command and poll in the foreground with bounded loops, e.g. "
    "`for i in $(seq 1 18); do grep -q DONE <log> && break; sleep 30; done; tail -5 <log>`, "
    "repeating until the job finishes. Never end your turn while work is pending.")


def check_tool_use(tool_name: str, tool_input: dict, run_path: str,
                   agent_type: str | None, registered_agents,
                   cwd: str | None = None) -> str | None:
    """Return a deny reason for this tool call, or None to allow it.

    `agent_type` is None for the lead (main thread) and the subagent's
    registry name otherwise.
    """
    tool_input = tool_input or {}
    # Phase 21 θ: no background execution. With SDK 0.2.x a one-shot
    # query() session closes once the agent ends its turn to "wait for the
    # background job", so the pipeline silently stopped mid-round
    # (2026-10-20_213310: lead backgrounded UQ + identifiability in round 1,
    # the stream ended, the run finished with no critique and no report).
    if tool_input.get("run_in_background") and tool_name in ("Bash", *_AGENT_TOOLS):
        return (BACKGROUND_DENY_REASON)
    if tool_name in _WRITE_TOOLS:
        path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        if path:
            return protected_reason(path, run_path, agent_type, cwd)
        return None
    if tool_name in ("Read", "Grep"):
        # Grep reads file contents too: a .env search path, or a glob that
        # names .env, exposes credentials as surely as Read. (Glob returns
        # names only, so it is not restricted.)
        for key in ("file_path", "path", "glob"):
            val = str(tool_input.get(key) or "")
            if val and (is_env_file(val, cwd) or ".env" in val):
                return "credential files (.env) are not accessible to agents"
        return None
    if tool_name == "Bash":
        return bash_reason(tool_input.get("command") or "", run_path,
                           agent_type, cwd)
    if tool_name in _AGENT_TOOLS:
        sub = tool_input.get("subagent_type")
        if not sub or sub not in registered_agents:
            return (f"subagent_type {sub!r} is not in the pipeline registry "
                    f"({', '.join(sorted(registered_agents))}). Built-in "
                    f"agents such as general-purpose are not allowed: spawn "
                    f"the specialist that owns the work.")
        return None
    return None


def deny_output(reason: str) -> dict:
    """PreToolUse hook response that blocks the call (SDK schema)."""
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": f"Phase 21 enforcement: {reason}",
        }
    }


# ---------------------------------------------------------------------------
# Tamper evidence
# ---------------------------------------------------------------------------

def _harness_paths(repo_root: str = REPO_ROOT):
    for d in HASHED_DIRS:
        for dirpath, dirnames, filenames in os.walk(os.path.join(repo_root, d)):
            dirnames[:] = sorted(x for x in dirnames if x not in _HASH_SKIP_DIRS)
            for fname in sorted(filenames):
                if not fname.endswith(_HASH_SKIP_SUFFIXES) and fname != ".DS_Store":
                    yield os.path.join(dirpath, fname)
    for fname in HASHED_FILES:
        p = os.path.join(repo_root, fname)
        if os.path.isfile(p):
            yield p


def lock_harness(repo_root: str = REPO_ROOT) -> dict[str, int]:
    """Make every hashed harness file read-only for the run; returns the
    original modes for unlock_harness. The 2026-10-27 smoke's modeler edited
    scripts/effect_size_registry.py through a Python heredoc with a variable
    path, which the shell heuristics cannot see; the harness hash then voided
    the run. A read-only file fails the write at once, and the agent sees why."""
    modes = {}
    for p in _harness_paths(repo_root):
        try:
            m = os.stat(p).st_mode
            os.chmod(p, m & ~0o222)
            modes[p] = m
        except OSError:
            continue
    return modes


def unlock_harness(modes: dict[str, int]) -> None:
    for p, m in (modes or {}).items():
        try:
            os.chmod(p, m)
        except OSError:
            continue


_CHMOD = re.compile(r"\b(chmod|chattr|chown|chflags)\b|os\.chmod|\.chmod\(")
_HARNESS_WORDS = re.compile(r"(?:^|[\s/'\"=])(?:scripts|agents|\.claude)(?:/|\b)|\bmain\.py\b|\bCLAUDE\.md\b|\brequirements\.txt\b")


def hash_harness(repo_root: str = REPO_ROOT) -> dict[str, str]:
    """sha256 of every hashed harness file, keyed by repo-relative path."""
    out: dict[str, str] = {}

    def _add(path: str) -> None:
        h = hashlib.sha256()
        try:
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 16), b""):
                    h.update(chunk)
        except OSError:
            return
        out[os.path.relpath(path, repo_root).replace(os.sep, "/")] = h.hexdigest()

    for d in HASHED_DIRS:
        root = os.path.join(repo_root, d)
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(x for x in dirnames if x not in _HASH_SKIP_DIRS)
            for fname in sorted(filenames):
                if fname.endswith(_HASH_SKIP_SUFFIXES) or fname == ".DS_Store":
                    continue
                _add(os.path.join(dirpath, fname))
    for fname in HASHED_FILES:
        p = os.path.join(repo_root, fname)
        if os.path.isfile(p):
            _add(p)
    return out


def diff_harness(before: dict[str, str], after: dict[str, str]) -> dict[str, list[str]]:
    """{'modified': [...], 'added': [...], 'removed': [...]} (empty lists when clean)."""
    return {
        "modified": sorted(k for k in before.keys() & after.keys() if before[k] != after[k]),
        "added": sorted(after.keys() - before.keys()),
        "removed": sorted(before.keys() - after.keys()),
    }


def harness_changed(diff: dict[str, list[str]]) -> bool:
    return any(diff.get(k) for k in ("modified", "added", "removed"))


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

# Phase 21 α: pinned model IDs. The bare "opus"/"sonnet" aliases are
# resolved by whatever CLI the SDK bundles; SDK 0.1.56's CLI (2.1.92)
# resolved them to the 4.x family, so runs silently used older models
# than intended and metadata could not say which. Bump these together
# with claude-agent-sdk in requirements.txt.
MODEL_DEEP = "claude-opus-5-5"     # synthesis, modeling, adversarial critique
MODEL_FAST = "claude-sonnet-5-5"   # data wrangling, presentation critique

# Phase 21 ι: SDK stream buffer. The default (1 MiB) killed A/B run B1's
# session 1 on 2026-10-21 ("JSON message exceeded maximum buffer size of
# 1048576 bytes") when one tool result exceeded it. Identical in both A/B arms.
MAX_BUFFER_SIZE = 32 * 1024 * 1024

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

# Phase 31: the prompt cache lives 1 hour instead of 5 minutes, for the lead
# and for subagents. E7 attempt 6: 44% of the measured spend was the cache
# being re-written after 5-60 min waits (the lead waiting on subagents, the
# modeler on simulations); replayed on that run, 1 h writes would have cut
# cache spend from $300 to about $186. MA_PROMPT_CACHE_TTL=5m restores the
# default for comparison.
def cache_env(environ: dict[str, str]) -> dict[str, str]:
    ttl = environ.get("MA_PROMPT_CACHE_TTL", "1h")
    if ttl not in ("1h", "5m"):
        ttl = "1h"
    return {"CLAUDE_CODE_PROMPT_CACHE_TTL": ttl, "CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL": ttl}


# Session settings for every pipeline session: with a 1 h cache the CLI may
# compact a long idle conversation, which could drop the lead's instructions;
# off until compaction is tested on its own.
SESSION_SETTINGS = '{"idleCompaction": false}'


def filtered_env(environ: dict[str, str], allow_cloud: bool) -> dict[str, str]:
    """Overrides for ClaudeAgentOptions.env.

    The SDK merges os.environ with options.env, so variables cannot be
    removed, only overridden. Cloud credentials are blanked when cloud
    execution is off; downstream code treats empty as unset. Phase 31:
    also sets the prompt-cache TTL (cache_env).
    """
    overrides = interpreter_env(environ)
    overrides.update(cache_env(environ))
    if not allow_cloud:
        overrides.update({k: "" for k in environ if k.startswith(CLOUD_ENV_PREFIXES)})
    return overrides


def interpreter_env(environ: dict[str, str],
                    executable: str | None = None) -> dict[str, str]:
    """Phase 21 α: put this interpreter's bin/ first on the agents' PATH.

    Agents call bare `python3` / `pip`. When main.py was launched as
    `.venv/bin/python3 main.py` without activating the venv, those
    resolved to the system interpreter: `pip install` failed with
    externally-managed-environment and the modeler concluded the
    requested framework (LASER) was "not installed" and silently swapped
    it for scipy (2026-10-20_150152). Derive the venv from sys.executable
    so the agents always see the interpreter that runs the harness.
    """
    import sys
    exe = executable or sys.executable
    bin_dir = os.path.dirname(exe)
    prefix = os.path.dirname(bin_dir)
    out = {"PATH": bin_dir + os.pathsep + environ.get("PATH", "")}
    if os.path.isfile(os.path.join(prefix, "pyvenv.cfg")):
        out["VIRTUAL_ENV"] = prefix
    return out
