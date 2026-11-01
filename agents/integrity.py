"""Phase 23 α — integrity record for protected run artifacts.

PreToolUse enforcement (agents/enforcement.py) blocks the obvious ways an
agent can rewrite a file the gate grades. Its Bash check is a heuristic, and
six rounds of review (PR #14) showed it cannot be made complete: variable
expansion, `bash -c`, a `cd` that never runs, escaped filenames, process
substitution. So instead of parsing commands, this module watches the files.

Every allowed tool call registers what it may change, before it runs:
  - Write/Edit           → its target file;
  - a harness script     → script outputs (`python3 scripts/<x>.py …`);
  - any subagent call    → critique files (critique_*: owned by the critics;
                           only the lead is barred from them).
After every tool call, and once at the end, the harness re-hashes the
protected files. A change no in-flight call can explain, and no live harness
process (a `nohup`-ed script) can explain, is a violation. Violations are
written to `artifact_integrity.json` — by the harness, never by a tool, and
itself protected — and the final gate turns each into a non-declarable HIGH.

This detects after the fact rather than blocking, but it does not depend on
how the change was made. A violation is resolved when a later sanctioned
write (its producer script, or the owning critic) replaces the file: the
gate cares whether the CURRENT content is producer-written. Resolved
violations stay in the record as advisories; tampering with the record
itself is never resolvable.

Known limits (PR #15 reviews):
  - Permission is decided before a call runs. In
    `false && python3 scripts/x.py; <rewrite x's output>` the producer never
    runs, yet the call holds x's output permission. The structural fix is
    harness-controlled producers: the harness runs gate scripts itself (as
    in-process tools) and denies agents running them via Bash.
  - A lead change made while a subagent's call is in flight is explained for
    agent-owned critique files.
  - Phase 36 process watch: while a producer runs on this run (and until it
    is seen finished once), changes to its outputs are explained whoever
    makes them, as for background grants; a Python started with a
    sitecustomize on PYTHONPATH runs other code inside a producer process,
    as an env assignment before a typed producer command already could.

S1 (docs/design/stage-machine.md §3.2): the outputs `models/pipeline.yaml`
declares are protected too, and only the harness's pipeline run
(scripts/pipeline.py `harness_build`) explains writes to them: it holds a
grant for the outputs of the steps it runs (`harness_pipeline`) and marks
what they wrote as harness-written. An agent's own Write/Edit of a declared
output is not an explanation. What the harness built stays protected for the
life of this record (removing a step does not unprotect the files it built);
files that already exist when first declared are baselined, as at start (the
runner reruns a step that never ran, so the baseline is not what the gate
reads). Two exceptions: a file no current step declares as an output but a
current step declares as an input is released (the runner keeps it as that
step's input and no step claims to have built it); and a file only a glob the
declaration has since narrowed or retired matched, which the harness never
built, is an ordinary run file again: its open violations are closed as
advisories when the record reads the declaration change (evalbase1 measles:
eda.py's figure sidecars, written by the modeler's own run of eda.py while the
figures step still declared `figures/*.provenance.json`). Declared an output
again, either is baselined and protected once more, and its step reruns.

In-flight calls end at the first of PostToolUse, the SDK's terminal task
message for the call (a foreground Bash call the CLI moves to the background
gets no PostToolUse), the agent's SubagentStop, or the end of the session;
see `on_task_ended`, `on_agent_stop`, `on_session_end`. Each runs a check
before the call is dropped. A full `scripts/pipeline.py run <run_dir>` from an
agent is refused at PreToolUse (`pipeline_builds`, agents/hooks.py).
"""
from __future__ import annotations

import fnmatch
import glob
import hashlib
import json
import os
import re
import threading
import time
from contextlib import contextmanager
from datetime import datetime

from agents import enforcement

REPORT_NAME = "artifact_integrity.json"
# Written by main.py itself (not through a tool), so never a tool's change.
# metadata.json is NOT here: the planner may edit it, so it is watched and
# the harness records its own writes (harness_wrote).
_HARNESS_WRITTEN = {"harness_manifest.json", "trace.jsonl",
                    "final_gate.json", "final_gate_stderr.txt", REPORT_NAME,
                    "model_card.yaml", "model_card.md"}
# The gate's records main.py writes (run_final_gate), and the model card it
# writes at finalize (Phase 44 α): not scanned at start (a resumed run's old
# copy is the harness's), but watched once the harness has written them and
# said so (harness_wrote): only main.py writes them.
_WATCH_ONCE_WRITTEN = {"final_gate.json", "final_gate_stderr.txt", "model_card.yaml", "model_card.md"}
# Which protected outputs each harness script may write (measured: the
# validator also rewrites four gate reports). A script authorizes only
# these — never critique files, and never another script's output.
SCRIPT_PRODUCES: dict[str, tuple[str, ...]] = {
    "compare_models.py": ("model_comparison_formal.yaml", "models/model_comparison_formal.yaml"),
    "identifiability.py": ("identifiability.yaml",),
    "propagate_uncertainty.py": ("uncertainty_report.yaml",),
    "cloud_batch.py": ("uncertainty_report.yaml",),
    "coherence_audit.py": ("coherence_audit.yaml",),
    "writer_qa.py": ("writer_qa_report.yaml",),
    "benchmark_match.py": ("benchmark_match.yaml",),
    "effort_floors.py": ("effort_floors_report.yaml",),
    "sanity_checks.py": ("sanity_report.yaml",),
    "validate_critique_yaml.py": ("validator_history.jsonl", "coherence_audit.yaml",
                                  "writer_qa_report.yaml", "effort_floors_report.yaml",
                                  "benchmark_match.yaml",
                                  # Phases 32-34 (Codex review): the gate's open-problem
                                  # list, acceptance lock and finding-test registry.
                                  "open_problems.json", "open_problems.md", "open_problems.json.tmp",
                                  "open_problems.md.tmp", "acceptance_lock.json",
                                  "done_when_registry.json", "done_when_registry.json.tmp",
                                  # Phase 49 β: the owner's sign-off request.
                                  "scope_signoff_request.json", "scope_signoff_request.json.tmp"),
    "decision_brief.py": ("decision_brief.signoff.json", "decision_brief.yaml", "success_criteria.yaml",
                          "decision_brief.signed/decision_brief.yaml", "decision_brief.signed/success_criteria.yaml"),
    "cross_model_critic.py": ("critique_crossmodel.yaml", "critique_crossmodel.yaml.tmp",
                              "critique_crossmodel.md"),
    "eligibility_probe.py": ("models/eligibility_probe.yaml",),
    "build_claims_ledger.py": ("models/claims_ledger.yaml", "claims_ledger.yaml"),
    "render_claims.py": ("report.md", "models/claims_ledger.yaml", "claims_ledger.yaml"),
    "model_card.py": ("model_card.yaml", "model_card.md"),
    "ask.py": ("whatif_log.jsonl",),
}
_SCRIPTS_DIR = os.path.realpath(os.path.join(enforcement.REPO_ROOT, "scripts"))
_WRAPPERS = {"nohup", "env", "time", "nice", "command", "exec", "sudo", "stdbuf"}
_DURATION = re.compile(r"([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?[smhd]?", re.ASCII)


class _Signals:
    """Signal names timeout accepts (with or without SIG, any case) and 1-64."""
    import signal as _sig
    NAMES = {n[3:] for n in dir(_sig) if n.startswith("SIG") and not n.startswith("SIG_")}

    @classmethod
    def fullmatch(cls, t: str) -> bool:
        t = str(t)
        if not t.isascii():          # GNU parses signal operands as ASCII only
            return False
        if re.fullmatch(r"[0-9]+", t):
            # only numbers with a signal name here (glibc reserves 32-33 unnamed;
            # older coreutils reject numbers without a name)
            return int(t) in {int(x) for x in cls._sig.Signals}
        u = t.upper()
        return (u[3:] if u.startswith("SIG") else u) in cls.NAMES


_SIGNAL = _Signals


def _skip_timeout(toks: list[str]) -> list[str] | None:
    """`timeout [OPTION] DURATION COMMAND...` -> COMMAND..., or None.

    Conservative (Codex review of PR #27): only options it fully understands
    (--preserve-status, --foreground, -v/--verbose, -k/--kill-after DUR,
    -s/--signal SIG, `--`) with valid arguments; anything else, or no valid
    duration, returns None, so nothing is credited as run. An uncredited
    harness run only leaves an integrity event; a wrongly credited one would
    authorize writes. Attempt-7 smoke: a critic ran the gate as
    `timeout 600 python3 scripts/validate_critique_yaml.py ...`."""
    j = 1
    while j < len(toks):
        t = toks[j]
        if t == "--":
            j += 1
            break
        if t in ("--preserve-status", "--foreground", "-v", "--verbose"):
            j += 1
        elif t in ("-k", "--kill-after") and j + 1 < len(toks) and _DURATION.fullmatch(toks[j + 1]):
            j += 2
        elif t in ("-s", "--signal") and j + 1 < len(toks) and _SIGNAL.fullmatch(toks[j + 1]):
            j += 2
        elif t.startswith("--kill-after=") and _DURATION.fullmatch(t.split("=", 1)[1]):
            j += 1
        elif t.startswith("--signal=") and _SIGNAL.fullmatch(t.split("=", 1)[1]):
            j += 1
        elif t.startswith("-"):
            return None          # an option we do not model (--help, clusters, abbreviations)
        else:
            break
    if j >= len(toks) or not _DURATION.fullmatch(toks[j]):
        return None
    return toks[j + 1:]
_CRITIQUE_PATTERNS = ("critique_*.yaml", "critique_*.md")
# A tool whose PostToolUse never arrives (an error path) must not keep
# sanctioning changes: intents expire (Bash's own timeout is 10 min).
INTENT_TTL_S = 15 * 60


def _sha(path: str) -> str | None:
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def _is_critique(rel: str) -> bool:
    """Agent-owned critique files. Script-owned ones (the cross-model
    critique) follow script-output rules instead (Copilot review, PR #15)."""
    return (any(fnmatch.fnmatch(rel, p) for p in _CRITIQUE_PATTERNS)
            and rel not in enforcement.SCRIPT_OUTPUTS)


_PIPELINE = []


def _pipeline():
    """scripts/pipeline.py, loaded by path once (None when it cannot load)."""
    if not _PIPELINE:
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "_ma_pipeline", os.path.join(enforcement.REPO_ROOT, "scripts", "pipeline.py"))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            _PIPELINE.append(mod)
        except Exception:  # noqa: BLE001
            _PIPELINE.append(None)
    return _PIPELINE[0]


def _protected_patterns() -> list[str]:
    pats = [p for p in enforcement.SCRIPT_OUTPUTS if p not in _HARNESS_WRITTEN]
    return pats + list(_CRITIQUE_PATTERNS)


_SHELLS = {"sh", "bash", "zsh", "dash"}


def harness_scripts_run(command: str, cwd: str | None = None) -> tuple[list[str], bool]:
    """Harness scripts a Bash command actually EXECUTES (a Python interpreter
    whose script argument resolves into the repo's scripts/ directory), and
    whether it backgrounds them. Mentions in comments, strings or other
    arguments do not count (Codex review of PR #15)."""
    runs, background = _executed_scripts(command, cwd)
    return [name for name, _args in runs if name in SCRIPT_PRODUCES], background


# `scripts/pipeline.py` options (argparse accepts unique prefixes of long options).
_PIPELINE_FLAGS = {"--dry-run": 0, "--scratch": 0, "--keep": 0, "--budget-s": 1, "--json": 0,
                   "--self-test": 0, "--help": 0, "-h": 0}


def pipeline_builds(command: str, cwd: str | None = None) -> bool:
    """Whether a Bash command executes a full `scripts/pipeline.py run <run_dir>` build
    (no --scratch, no --dry-run). That form writes the declared outputs into the real
    run directory outside the harness's own run (`harness_build`), so every file it
    writes is an integrity violation (evalbase1 flu_vaccine: 56 events from one
    `nohup python3 scripts/pipeline.py run . &`). Same execution parser as
    `harness_scripts_run`: a mention of the command is not a run of it."""
    runs, _bg = _executed_scripts(command, cwd, bind=True)
    return any(name == "pipeline.py" and _is_full_build(args) for name, args in runs)


def _strip_redirections(args: list[str]) -> list[str]:
    """The shell removes redirections (`>f`, `2>&1`, a bare `>` and its operand) before
    the program sees its arguments."""
    out, j = [], 0
    while j < len(args):
        redir = re.fullmatch(r"\d*(>>?|<|&>>?|>&|<&)(\S*)", args[j])
        if redir:
            j += 1 if redir.group(2) else 2
            continue
        out.append(args[j])
        j += 1
    return out


def _is_full_build(args: list[str]) -> bool:
    args = _strip_redirections(args)
    pos, opts, j = [], set(), 0
    while j < len(args):
        t = args[j]
        if t == "--":
            pos.extend(args[j + 1:])
            break                        # argparse: everything after `--` is positional
        if t.startswith("-") and t != "-":
            name = t.split("=", 1)[0]
            match = [o for o in _PIPELINE_FLAGS if o == name] or \
                [o for o in _PIPELINE_FLAGS if o.startswith("--") and name.startswith("--") and o.startswith(name)]
            if len(match) != 1:
                return False             # argparse exits on an unknown or ambiguous option: nothing runs
            opts.add(match[0])
            j += 1 + (_PIPELINE_FLAGS[match[0]] if "=" not in t else 0)
            continue
        pos.append(t)
        j += 1
    if opts & {"--self-test", "--help", "-h", "--dry-run", "--scratch"}:
        return False
    # `run <run_dir> <step>` without --scratch is refused by the CLI itself.
    return len(pos) == 2 and pos[0] == "run"


def _bind_positional(cmd_str: str, operands: list[str]) -> str:
    """`sh -c STRING $0 $1 …`: substitute $@, $*, $N and ${N} in STRING with the
    shell-quoted operands it would see ($0 is the first operand)."""
    import shlex
    if not operands or "$" not in cmd_str:
        return cmd_str
    args = operands[1:]
    every = " ".join(shlex.quote(a) for a in args)

    def one(m):
        n = int(m.group(1) or m.group(2))
        v = operands[0] if n == 0 else (args[n - 1] if n - 1 < len(args) else "")
        return shlex.quote(v)
    s = re.sub(r'"\$[@*]"|\$[@*]|"\$\{[@*]\}"|\$\{[@*]\}', lambda m: every, cmd_str)
    return re.sub(r'"?\$(?:(\d)|\{(\d+)\})"?', one, s)


def _executed_scripts(command: str, cwd: str | None = None, _depth: int = 0, bind: bool = False
                      ) -> tuple[list[tuple[str, list[str]]], bool]:
    """(name, arguments) of each repo script under scripts/ a Bash command executes
    with a Python interpreter, and whether it backgrounds them. `bind` substitutes a
    `sh -c` string's positional parameters: for refusals only (it can only add runs),
    never for crediting a producer (Codex r2: `sh -c '$1 x.py' sh python3` runs nothing)."""
    import shlex
    found, background = [], False
    for seg, base in enforcement.iter_segments_with_cwd(command, cwd):
        if (enforcement._tokens(seg) or [""])[0] in ("cd", "pushd", "popd"):
            continue
        try:
            toks = shlex.split(seg)
        except ValueError:
            continue
        while toks and (re.match(r"^\w+=", toks[0]) or os.path.basename(toks[0]) in _WRAPPERS
                        or os.path.basename(toks[0]) == "timeout"
                        or re.fullmatch(r"-\w+|\d+[smh]?", toks[0])):
            if os.path.basename(toks[0]) == "nohup":
                background = True
            if os.path.basename(toks[0]) == "timeout":
                toks = _skip_timeout(toks) or []
                break            # timeout runs its COMMAND as is: no further wrappers or assignments
            toks = toks[1:]
        if toks and os.path.basename(toks[0]) in _SHELLS and _depth < 3:
            # `nohup sh -c "python3 scripts/x.py …; echo DONE" &` (seen in E7):
            # the producer runs inside the shell string — but only when -c
            # (or a cluster like -lc) comes before any script operand or `--`;
            # in `bash /dev/null -c '…'` the string never runs (Codex, PR #17).
            cmd_str, noexec, j = None, False, 1
            while j < len(toks):
                t = toks[j]
                if t in ("-o", "+o", "-O", "+O"):
                    noexec = noexec or (j + 1 < len(toks) and toks[j + 1] == "noexec")
                    j += 2                   # option with an operand (`-o pipefail`)
                    continue
                if t == "--" or not t.startswith(("-", "+")):
                    break
                if t.startswith("-") and not t.startswith("--") and "n" in t[1:]:
                    noexec = True            # `-n`: syntax check only, nothing runs
                # only a cluster of real single-letter shell options; `-Zc` makes
                # bash exit without running the string (Codex, PR #17).
                if re.fullmatch(r"-[eluxivfaBHP]*c[eluxivfaBHP]*", t):
                    cmd_str = toks[j + 1] if j + 1 < len(toks) else None
                    break
                j += 1
            if noexec:
                cmd_str = None
            if cmd_str is not None:
                # `bash -c '… "$@"' $0 $1 …`: the string runs with these as its positional
                # parameters, so they are part of what runs (Codex r1).
                if bind:
                    cmd_str = _bind_positional(cmd_str, toks[j + 2:])
                inner, bg = _executed_scripts(cmd_str, base, _depth + 1, bind)
                found += inner
                background = background or bg
            continue
        if len(toks) < 2 or not re.fullmatch(r"python[\d.]*", os.path.basename(toks[0])):
            continue
        # The script is the first operand after the interpreter's own options; -W/-X take
        # an operand (`python3 -W ignore x.py`), and -c/-m/- run no script file (Codex r1).
        k, i = None, 1
        while i < len(toks):
            t = toks[i]
            if t == "-" or re.fullmatch(r"-[bBdEiIOPqsSuvVxR]*[cm].*", t, re.S):
                break
            if re.fullmatch(r"-[bBdEiIOPqsSuvVxR]*[WX]", t):
                i += 2                   # -W / -X (alone or ending a cluster: -uW) take the next token
                continue
            if not t.startswith("-"):
                k = i
                break
            i += 1
        if k is None:
            continue
        script = os.path.expanduser(toks[k])
        if base is None and not os.path.isabs(script):
            continue                     # relative script after `cd $X`: can't resolve
        real = os.path.realpath(os.path.join(base or "", script))
        if os.path.dirname(real) == _SCRIPTS_DIR:
            found.append((os.path.basename(real), toks[k + 1:]))
    if re.search(r"(?<![&>|])&(?![&>])\s*(?:$|[;\n)])", command):
        background = True
    return found, background


def _live_harness_scripts(run_path: str) -> set[str]:
    """Harness scripts running right now ON THIS RUN (e.g. launched with
    nohup by an earlier call): script argv under the repo's scripts/ and the
    run directory on the command line. Unrelated processes don't count."""
    try:
        import psutil
    except ImportError:
        return set()
    out: set[str] = set()
    run_abs = os.path.realpath(run_path)
    try:
        for p in psutil.process_iter(["cmdline", "cwd"]):
            argv = p.info.get("cmdline") or []
            pcwd = p.info.get("cwd") or enforcement.REPO_ROOT

            def _is_this_run(a: str) -> bool:
                # Normalize first: `runs/x/../y` is run y, not run x (Codex, PR #15).
                if not a or a.startswith("-"):
                    return False
                real = os.path.realpath(os.path.join(pcwd, a))
                return real == run_abs or real.startswith(run_abs + os.sep)
            if not argv or not any(_is_this_run(a) for a in argv):
                continue
            # The interpreter must be RUNNING the producer: argv[0] is a
            # Python interpreter and its script argument (the first one, no
            # -c/-m) is the producer — not any argument (Copilot, PR #15).
            if not re.fullmatch(r"python[\d.]*", os.path.basename(argv[0]), re.IGNORECASE):
                continue
            rest = argv[1:]
            while rest and rest[0].startswith("-") and rest[0] not in ("-c", "-m"):
                rest = rest[1:]
            if not rest or rest[0] in ("-c", "-m"):
                continue
            real = os.path.realpath(os.path.join(pcwd, rest[0]))
            if os.path.dirname(real) == _SCRIPTS_DIR and os.path.basename(real) in SCRIPT_PRODUCES \
                    and any(_is_this_run(a) for a in rest[1:]):
                out.add(os.path.basename(real))
    except Exception:  # noqa: BLE001 — a failed scan must not crash the hook
        return out
    return out


class ArtifactIntegrity:
    def __init__(self, run_path: str, process_check=None):
        self.run_path = run_path
        self.hashes: dict[str, str | None] = {}
        self.inflight: dict[str, dict] = {}
        self.events: list[dict] = []
        self.unmatched_intents = 0
        self._lock = threading.Lock()
        self._process_check = process_check or (lambda: _live_harness_scripts(run_path))
        self.grants: list[dict] = []     # background harness scripts launched
        self._check_seq = 0              # bumped at the start and end of every check (process watch)
        self.report_ok = True
        self.pipeline_patterns: list[str] = []   # S1: declared pipeline outputs (sticky)
        self._pipeline_sig = None
        self._pipeline_now: tuple[list[str], list[str]] = ([], [])   # current (outputs, inputs)
        self._released: set[str] = set()
        self._baseline: set[str] = set()
        self._harness_files: set[str] = set()   # _WATCH_ONCE_WRITTEN files the harness wrote
        self._pipeline_built: set[str] = set()  # files the harness pipeline run wrote (harness_wrote)
        self._built_now: set[str] = set()       # ... plus models/pipeline_state.json's records, per scan
        self._unowned: set[str] = set()         # held only by a retired glob, never harness-built
        self._retired_now: list[str] = []       # globs the declaration retired in this scan
        self.declaration_changes: list[dict] = []
        self._task_tool: dict[str, str] = {}    # SDK task id -> tool_use_id (TaskStartedMessage)
        # A resumed run keeps the violations already recorded. Only a MISSING
        # record is a fresh start: an unreadable, unstamped or malformed one
        # is itself evidence and becomes a violation (never silently reset).
        rp = os.path.join(run_path, REPORT_NAME)
        if os.path.exists(rp):
            try:
                with open(rp) as f:
                    prior = json.load(f)
                ev = prior.get("violations") if isinstance(prior, dict) else None
                if prior.get("producer") != "agents/integrity.py" or not isinstance(ev, list) \
                        or not all(isinstance(e, dict) for e in ev):
                    raise ValueError("unstamped or malformed")
                self.events = list(ev)
                dc = prior.get("declaration_changes")
                self.declaration_changes = [c for c in dc if isinstance(c, dict)] if isinstance(dc, list) else []
                # A resumed run keeps the globs declared before and what the harness built
                # (Codex r2): both only ever add protection. The record is harness-written;
                # any other change to it is an unresolvable violation.
                for key, dst in (("pipeline_declared", self.pipeline_patterns), ("pipeline_built", None)):
                    vals = prior.get(key)
                    vals = [v for v in vals if isinstance(v, str)] if isinstance(vals, list) else []
                    if dst is None:
                        self._pipeline_built.update(vals)
                    else:
                        dst.extend(v for v in vals if v not in dst)
            except (OSError, ValueError, AttributeError) as e:
                self.events = [{"file": REPORT_NAME, "change": "invalid_at_start", "open": True,
                                "resolvable": False, "detected": "start", "detail": str(e)[:200],
                                "ts": datetime.now().isoformat(timespec="seconds"),
                                "in_flight": []}]
        self.hashes = self._scan()
        self._mtimes = dict(self._mtimes_now)
        self._write_report()

    # -- process watch (Phase 36) -----------------------------------------
    # E7 attempt 7: the modeler ran the producer scripts from its own shell
    # script (`bash models/run_all_r2.sh`, itself started by a waiting driver
    # script), so no command line named them and every output they wrote
    # became an unexplained, non-declarable HIGH. A producer is now credited
    # by what is RUNNING, not by what was typed: a background sampler records
    # each producer process it sees alive on this run (the same test as
    # `_live_harness_scripts`: a Python interpreter running the repo's
    # read-only script with this run's directory in its arguments), and that
    # explains its outputs until it has been seen finished once. A producer
    # that starts and ends between two samples is not seen (its output is then
    # reported as before; rerunning the producer directly resolves it).
    def start_watch(self, interval_s: float = 0.25) -> None:
        if getattr(self, "_watch", None) is not None:
            return
        self._stop_watch = threading.Event()

        def loop():
            while not self._stop_watch.wait(interval_s):
                self.observe()
        self._watch = threading.Thread(target=loop, name="integrity-watch", daemon=True)
        self._watch.start()

    def stop_watch(self) -> None:
        if getattr(self, "_watch", None) is not None:
            self._stop_watch.set()
            self._watch.join(timeout=5)
            self._watch = None

    def observe(self) -> None:
        """One sample: a grant for each producer seen running on this run. A
        sample taken before a check that ran meanwhile is discarded: that
        check may already have seen the producer finish (Codex review)."""
        seq = self._check_seq
        try:
            live = set(self._process_check() or ())
        except Exception:  # noqa: BLE001 — a failed sample must not stop the watch
            return
        with self._lock:
            if self._check_seq != seq:
                return
            for sc in live:
                if sc not in SCRIPT_PRODUCES:
                    continue
                g = next((g for g in self.grants if g.get("observed") and sc in g["scripts"]), None)
                if g is not None:
                    g["seen_dead"] = False
                else:
                    self.grants.append({"scripts": {sc}, "seen_dead": False, "armed": True, "observed": True,
                                        "t": time.monotonic(), "paths": set(SCRIPT_PRODUCES[sc])})

    # -- scanning ---------------------------------------------------------
    def _refresh_pipeline_patterns(self) -> list[str]:
        """Add the outputs models/pipeline.yaml declares now; returns the new ones."""
        sp = os.path.join(self.run_path, "models", "pipeline.yaml")
        try:
            st = os.stat(sp)
            sig = (st.st_mtime_ns, st.st_size)
        except OSError:
            return []
        if sig == self._pipeline_sig:
            return []
        mod = _pipeline()
        if mod is None:
            return []
        try:
            pats = mod.declared_outputs(self.run_path)
            ins = mod.declared_inputs(self.run_path)
        except Exception:  # noqa: BLE001 — keep what was declared before (sticky)
            return []
        self._pipeline_sig = sig
        prev = self._pipeline_now[0]
        self._pipeline_now = (list(pats), list(ins))
        # The declaration narrowed or retired a glob it held before: recorded here, by the
        # harness, from the declaration it read (never by elapsed time). `_check` closes
        # open violations on files the glob alone held and the harness never built.
        retired = [p for p in prev if p not in pats]
        if retired:
            self._retired_now = retired
            self.declaration_changes.append({"ts": datetime.now().isoformat(timespec="seconds"),
                                             "retired": retired,
                                             "added": [p for p in pats if p not in prev]})
        new = [p for p in pats if p not in self.pipeline_patterns]
        self.pipeline_patterns.extend(new)
        return new

    def _built(self) -> set[str]:
        """Files the harness's pipeline run wrote: recorded by harness_wrote during its run,
        and the outputs its own record (models/pipeline_state.json, a protected runner
        file) lists, so a resumed run keeps them. Accumulated for the life of this
        record: rewriting the state file later cannot take a file back out (Codex r1).
        Only ever makes ownership stickier."""
        mod = _pipeline()
        if mod is not None:
            try:
                for rec in (mod._load_state(self.run_path).get("steps") or {}).values():
                    if isinstance(rec, dict) and isinstance(rec.get("outputs"), dict):
                        self._pipeline_built.update(str(k) for k in rec["outputs"])
            except Exception:  # noqa: BLE001 — a bad record adds nothing (the scan still flags it)
                pass
        return set(self._pipeline_built)

    def _pm(self):
        return getattr(_pipeline(), "path_match", fnmatch.fnmatchcase)

    def _claims(self, rel: str) -> bool:
        """Whether the declared pipeline owns `rel`: a glob the current declaration holds
        matches it, or the harness built it under a glob declared earlier (sticky:
        dropping a step does not unprotect what the harness built). A file only a
        retired glob matches, which the harness never built, is an ordinary run file."""
        pm = self._pm()
        if any(pm(rel, p) for p in self._pipeline_now[0]):
            return True
        return rel in self._built_now and any(pm(rel, p) for p in self.pipeline_patterns)

    def _pipeline_owned(self, rel: str) -> bool:
        mod = _pipeline()
        own = tuple(mod.OWN_FILES) if mod is not None else ()
        if rel in own:
            return True
        pm = self._pm()
        return any(pm(rel, p) for p in self.pipeline_patterns) and self._claims(rel) \
            and not self._is_released(rel)

    def _is_released(self, rel: str) -> bool:
        """A retired pipeline output a current step reads as an input (review #3)."""
        mod = _pipeline()
        pm = mod.path_match if mod is not None else fnmatch.fnmatchcase
        outs, ins = self._pipeline_now
        return any(pm(rel, p) for p in ins) and not any(pm(rel, p) for p in outs)

    def _scan(self) -> dict[str, str | None]:
        out = {}
        self._mtimes_now = {}
        self._retired_now = []
        new_pats = self._refresh_pipeline_patterns()
        self._built_now = self._built()
        self._baseline = set()
        for pat in new_pats:
            for p in glob.glob(os.path.join(self.run_path, pat), recursive=True):
                rel = os.path.relpath(p, self.run_path).replace(os.sep, "/")
                if os.path.isfile(p) and rel not in self.hashes:
                    self._baseline.add(rel)
        was_released, self._released = self._released, set()
        was_unowned, self._unowned = self._unowned, set()
        protected = _protected_patterns()
        for pat in protected + self.pipeline_patterns:
            for p in glob.glob(os.path.join(self.run_path, pat), recursive=True):
                if os.path.isfile(p):
                    rel = os.path.relpath(p, self.run_path).replace(os.sep, "/")
                    if rel in out:
                        continue          # a protected pattern (scanned first) holds it
                    if pat not in protected and self._is_released(rel):
                        self._released.add(rel)
                        continue
                    if pat not in protected and not self._claims(rel):
                        self._unowned.add(rel)
                        continue
                    out[rel] = _sha(p)
                    self._mtimes_now[rel] = os.stat(p).st_mtime_ns
                    if (rel in was_released or rel in was_unowned) and rel not in self.hashes:
                        self._baseline.add(rel)    # declared an output again: baselined
        for rel in self._harness_files:
            p = os.path.join(self.run_path, rel)
            if os.path.isfile(p) and rel not in out:
                out[rel] = _sha(p)
                self._mtimes_now[rel] = os.stat(p).st_mtime_ns
        rp = os.path.join(self.run_path, REPORT_NAME)
        if os.path.exists(rp):
            out[REPORT_NAME] = _sha(rp)
        return out

    def _watched(self, rel: str) -> bool:
        """Whether `_scan` hashes `rel` (the same patterns, released files excluded)."""
        if rel == REPORT_NAME or rel in self._harness_files:
            return True
        if any(fnmatch.fnmatchcase(rel, p) for p in _protected_patterns()):
            return True
        pm = self._pm()
        return any(pm(rel, p) for p in self.pipeline_patterns) and self._claims(rel) \
            and not self._is_released(rel)

    # -- tool lifecycle ---------------------------------------------------
    # An in-flight entry is opened by PreToolUse (on_pre) and ends at the first of:
    #   - its PostToolUse (on_post);
    #   - the SDK's terminal task message for it (on_task_ended): a foreground Bash call
    #     the CLI moves to the background itself gets no PostToolUse (evalbase1 flu
    #     planner #16, measles data-agent), only TaskNotificationMessage /
    #     TaskUpdatedMessage when it finishes;
    #   - its agent's SubagentStop (on_agent_stop);
    #   - the end of the session (on_session_end) or of the run (final_check).
    # Each end runs a check first, with the entry still in flight, so what the call
    # wrote before it ended is judged with its intent; then the entry is dropped.
    def on_pre(self, tool_use_id: str, tool_name: str, tool_input: dict,
               agent_type: str | None, cwd: str | None = None, agent_id: str | None = None) -> None:
        """Register what an ALLOWED call may change (call after enforcement)."""
        intent = {"agent": agent_type, "tool": tool_name, "paths": set(),
                  "t": time.monotonic(), "agent_id": agent_id}
        # An agent's own carve-outs (writer → report.md, planner →
        # metadata.json) cover any of its tools, Bash included.
        for rel, owners in enforcement._OUTPUT_CARVE_OUTS.items():
            if agent_type in owners:
                intent["paths"].add(rel)
        if tool_name in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
            path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
            if path:
                rel = enforcement._run_rel(path, self.run_path, cwd)
                if rel and not self._pipeline_owned(rel):
                    # S1: a declared pipeline output is written by the harness run only.
                    intent["paths"].add(rel)
        elif tool_name == "Bash":
            scripts, background = harness_scripts_run(tool_input.get("command") or "", cwd)
            for sc in scripts:
                intent["paths"].update(SCRIPT_PRODUCES[sc])
            if scripts and background:
                with self._lock:
                    self.grants.append({"scripts": set(scripts), "seen_dead": False, "armed": False,
                                        "launch_id": tool_use_id, "t": time.monotonic(),
                                        "paths": {o for sc in scripts for o in SCRIPT_PRODUCES[sc]}})
        with self._lock:
            self.inflight[tool_use_id or f"anon-{len(self.inflight)}"] = intent

    def on_post(self, tool_use_id: str | None) -> list[dict]:
        with self._lock:
            self._posting = tool_use_id
            new = self._check(where="post_tool")
            self._posting = None
            self.inflight.pop(tool_use_id, None)
        return new

    def on_task_started(self, task_id: str | None, tool_use_id: str | None) -> None:
        """TaskStartedMessage: remember which tool call a task id belongs to (a
        TaskUpdatedMessage names only the task)."""
        if task_id and tool_use_id:
            with self._lock:
                self._task_tool[task_id] = tool_use_id

    def on_task_ended(self, task_id: str | None = None, tool_use_id: str | None = None) -> list[dict]:
        """A terminal task message: the call ends as on its PostToolUse."""
        with self._lock:
            mapped = self._task_tool.pop(task_id, None) if task_id else None
        tid = tool_use_id or mapped
        if not tid or tid not in self.inflight:
            return []
        return self.on_post(tid)

    def on_agent_stop(self, agent_id: str | None) -> list[dict]:
        """SubagentStop: every call of that agent still in flight ends (its PostToolUse
        will not come). Counted as unmatched intents, as at finalize."""
        with self._lock:
            ids = [k for k, i in self.inflight.items() if agent_id and i.get("agent_id") == agent_id]
            return self._end(ids, where="subagent_stop")

    def on_session_end(self) -> list[dict]:
        """The lead's session ended: no call of it is in flight any more."""
        with self._lock:
            self._task_tool.clear()
            return self._end(list(self.inflight), where="session_end")

    def _end(self, ids: list[str], where: str) -> list[dict]:
        if not ids:
            return []
        # The ending calls get their post-hook scan: an unarmed background grant they
        # launched is armed only if its producer is alive now, else dropped (Codex r1).
        self._ending = set(ids)
        try:
            new = self._check(where=where)
        finally:
            self._ending = set()
        for k in ids:
            self.inflight.pop(k, None)
        self.unmatched_intents += len(ids)
        self._write_report()
        return new

    def harness_wrote(self, rel: str) -> None:
        """The harness itself (not a tool) wrote `rel`, e.g. restoring a
        deleted report.md at finalize: accept the new content.

        Only for a file the scan watches. A hash recorded for a file `_scan`
        never hashes made the next check see it as deleted (S1 smoke,
        2026-10-29: `final_gate.json` and `final_gate_stderr.txt`, registered
        by main.py's `_harness_wrote_gate_outputs` before the final write-up,
        were reported "deleted" on the writer's first tool call, and the run
        ended `artifact_tampered`). The gate's own records
        (_WATCH_ONCE_WRITTEN) are watched from the harness's first write on,
        so an agent changing or deleting them afterwards is still flagged."""
        with self._lock:
            if rel in _WATCH_ONCE_WRITTEN:
                self._harness_files.add(rel)
            if any(g.get("pipeline") for g in self.grants):
                # The harness pipeline run wrote it (scripts/pipeline.py _finish): built,
                # so it stays protected if its glob is later narrowed or retired.
                self._pipeline_built.add(rel)
                self._built_now.add(rel)
            if not self._watched(rel):
                self.hashes.pop(rel, None)
                return
            self.hashes[rel] = _sha(os.path.join(self.run_path, rel))

    def check_now(self) -> list[dict]:
        """S1: judge pending changes now (before the harness pipeline run trusts its state)."""
        with self._lock:
            return self._check(where="pipeline_start")

    def is_open(self, rel: str) -> bool:
        return any(e.get("file") == rel and e.get("open", True) for e in self.events)

    @contextmanager
    def harness_pipeline(self, patterns):
        """S1: the harness's pipeline run is writing `patterns` (the declared outputs of
        the steps it runs, and its own records). Changes to them are explained while it
        runs; on exit the files are checked once more, so a hand edit it replaced is
        resolved, then the grant ends."""
        g = {"scripts": set(), "paths": set(), "patterns": tuple(patterns), "pipeline": True,
             "armed": True, "seen_dead": False, "t": time.monotonic()}
        with self._lock:
            self.grants.append(g)
        try:
            yield
        finally:
            with self._lock:
                try:
                    self._check(where="pipeline_run")
                finally:
                    self.grants = [x for x in self.grants if x is not g]

    @contextmanager
    def harness_runs_code(self, where: str, allowed=()):
        """Phase 44 α (Codex): the harness runs the run's own model code after the
        final check (the model card at finalize). Any watched file that changes
        meanwhile, other than `allowed` (the harness's own outputs, recorded with
        harness_wrote), is an unresolvable violation."""
        with self._lock:
            before = self._scan()
        try:
            yield
        finally:
            with self._lock:
                after = self._scan()
                new = [{"file": rel, "change": ("deleted" if rel not in after else
                                                "created" if rel not in before else "modified"),
                        "detected": where, "ts": datetime.now().isoformat(timespec="seconds"),
                        "in_flight": [], "open": True, "resolvable": False}
                       for rel in sorted(set(before) | set(after))
                       if before.get(rel) != after.get(rel) and rel not in allowed]
                if new:
                    self.events.extend(new)
                    self._write_report()
                if REPORT_NAME in self.hashes:
                    self.hashes[REPORT_NAME] = _sha(os.path.join(self.run_path, REPORT_NAME))

    def final_check(self) -> list[dict]:
        with self._lock:
            # Intents whose PostToolUse never arrived (tool errors, or a
            # tool_use_id mismatch between Pre and Post): reported, so a
            # systematic mismatch is visible rather than silently permissive.
            self.unmatched_intents += len(self.inflight)
            new = self._check(where="final")   # still-valid intents explain writes
            self.inflight.clear()
            self._write_report()
            return new

    # -- core -------------------------------------------------------------
    def _explained(self, rel: str, live: set[str]) -> bool:
        now = time.monotonic()
        intents = [i for i in self.inflight.values() if now - i["t"] < INTENT_TTL_S]
        if any(rel in i["paths"] for i in intents):
            return True          # its own Write/Edit, carve-out, or harness script
        if _is_critique(rel) and any(str(i["agent"] or "").startswith("critique-") for i in intents):
            return True          # critics own their files
        pm = getattr(_pipeline(), "path_match", fnmatch.fnmatchcase)
        if any(rel in g["paths"] or any(pm(rel, p) for p in g.get("patterns", ()))
               for g in self.grants):
            return True          # a background harness script, or the harness pipeline run
        return any(rel in SCRIPT_PRODUCES.get(sc, ()) for sc in live)

    def _refresh_grants(self) -> set[str]:
        """Live harness scripts on this run; a background grant lasts until
        its process has been seen finished once (so its last write, made
        between hooks, is still explained)."""
        live = set(self._process_check() or ())
        keep = []
        for g in self.grants:
            if g.get("pipeline"):
                keep.append(g)   # held by harness_pipeline until its run ends
                continue
            if not g["armed"] and g.get("launch_id") != getattr(self, "_posting", None) \
                    and g.get("launch_id") not in getattr(self, "_ending", ()):
                # Only the launching call's own post-hook arms it; if that
                # post-hook never comes, the grant expires like an intent.
                if time.monotonic() - g.get("t", 0) < INTENT_TTL_S:
                    keep.append(g)
                continue
            if not g["armed"]:
                # The launching call's own scan. Alive → the grant covers its
                # later writes. Not alive → it never ran (`true || nohup … &`)
                # or already finished, and its writes so far were covered by
                # the launching call's intent: drop the grant (Codex, PR #15).
                g["armed"] = True
                if g["scripts"] & live:
                    keep.append(g)
            elif g["scripts"] & live:
                g["seen_dead"] = False
                keep.append(g)
            elif not g["seen_dead"]:
                g["seen_dead"] = True
                keep.append(g)
        self.grants = keep
        return live

    def _check(self, where: str) -> list[dict]:
        self._check_seq += 1
        cur = self._scan()
        live = self._refresh_grants()
        new_events = []
        resolved = False
        for rel in sorted(set(cur) | set(self.hashes)):
            if rel == REPORT_NAME:
                continue
            if rel in self._baseline or rel in self._released or rel in self._unowned:
                continue         # S1: existed before it was declared a pipeline output, or released
            if rel not in cur and not self._watched(rel):
                continue         # gone, and no longer protected (a retired glob's never-built file)
            in_flight =sorted({f"{i['agent'] or 'lead'}:{i['tool']}" for i in self.inflight.values()})
            if cur.get(rel) == self.hashes.get(rel):
                # Same bytes, but rewritten (mtime moved) by a sanctioned
                # producer: a deterministic regeneration still resolves an
                # open violation (Codex review, PR #16). A skipped producer
                # (`false && …`) doesn't touch the file, so it can't.
                touched = self._mtimes_now.get(rel) != getattr(self, "_mtimes", {}).get(rel)
                if touched and rel in cur and any(e.get("file") == rel and e.get("open", True)
                                                  for e in self.events) and self._explained(rel, live):
                    for ev in self.events:
                        if ev.get("file") == rel and ev.get("open", True):
                            ev["open"] = False
                            ev["resolved"] = {"ts": datetime.now().isoformat(timespec="seconds"),
                                              "by": in_flight, "identical_bytes": True}
                            resolved = True
                continue
            if not self._explained(rel, live):
                ev = {"file": rel, "change": ("deleted" if rel not in cur else
                                              "created" if rel not in self.hashes else "modified"),
                      "detected": where, "ts": datetime.now().isoformat(timespec="seconds"),
                      "in_flight": in_flight, "open": True}
                new_events.append(ev)
            elif rel in cur:
                # A sanctioned write (its producer script, or the owning
                # critic) replaced the file: what the gate grades is now
                # producer-written again, so earlier violations on it are
                # resolved — kept visible, no longer blocking.
                for ev in self.events:
                    if ev.get("file") == rel and ev.get("open", True):
                        ev["open"] = False
                        ev["resolved"] = {"ts": datetime.now().isoformat(timespec="seconds"),
                                          "by": in_flight}
                        resolved = True
        if self._retired_now:
            self._release_retired()
            resolved = True       # the record is rewritten with the declaration change
        # The report itself: only this object writes it (never resolvable).
        if cur.get(REPORT_NAME) != self.hashes.get(REPORT_NAME):
            new_events.append({"file": REPORT_NAME, "change": "modified", "detected": where,
                               "ts": datetime.now().isoformat(timespec="seconds"),
                               "in_flight": [], "open": True, "resolvable": False})
        self.hashes = cur
        self._mtimes = dict(self._mtimes_now)
        if new_events or resolved:
            self.events.extend(new_events)
            self._write_report()
        self._check_seq += 1      # again at the end: a sample overlapping any part of a check is stale
        return new_events

    def _release_retired(self) -> None:
        """The declaration just narrowed or retired `self._retired_now`. An open violation
        on a file such a glob held, that the current declaration no longer claims and the
        harness never built, is closed as an advisory: nothing grades that file as a
        pipeline output any more (declared again, it is baselined and its step reruns, so
        the gate reports the step as not built until the harness builds it). It is never
        marked harness-written. A file the harness built stays owned (`_claims`), and so do
        files other protected patterns hold; neither is released here."""
        pm = self._pm()
        protected = _protected_patterns()
        change = self.declaration_changes[-1] if self.declaration_changes else {}
        for ev in self.events:
            f = ev.get("file")
            if not ev.get("open", True) or ev.get("resolvable") is False or not isinstance(f, str):
                continue
            if f == REPORT_NAME or any(fnmatch.fnmatchcase(f, q) for q in protected):
                continue
            if not any(pm(f, p) for p in self._retired_now) or self._claims(f):
                continue
            ev["open"] = False
            ev["resolved"] = {"ts": datetime.now().isoformat(timespec="seconds"), "by": "declaration_retired",
                              "retired": list(self._retired_now), "declaration_ts": change.get("ts"),
                              "advisory": "no current pipeline step declares this file and the harness "
                                          "never built it: it is an ordinary run file, not a pipeline output"}

    @property
    def open_events(self) -> list[dict]:
        return [e for e in self.events if e.get("open", True)]

    def _write_report(self) -> None:
        p = os.path.join(self.run_path, REPORT_NAME)
        try:
            if os.path.exists(p) and not os.access(p, os.W_OK):
                os.chmod(p, 0o644)       # a chmod 444 must not freeze a clean report
            with open(p, "w") as f:
                json.dump({"producer": "agents/integrity.py",
                           "protected_patterns": _protected_patterns() + self.pipeline_patterns,
                           "unmatched_intents": self.unmatched_intents,
                           "open_violations": len(self.open_events),
                           "declaration_changes": self.declaration_changes,
                           "pipeline_declared": list(self.pipeline_patterns),
                           "pipeline_built": sorted(self._pipeline_built),
                           "violations": self.events}, f, indent=2)
        except OSError:
            self.report_ok = False       # finalize blocks on in-memory events anyway
            return
        self.report_ok = True
        self.hashes[REPORT_NAME] = _sha(p)


# --------------------------------------------------------------------------- self-test

_FIXTURE = os.path.join(enforcement.REPO_ROOT, "tests", "fixtures", "integrity_measles_evalbase1.json")


def _self_test() -> int:
    """IG1-IG10: the in-flight lifecycle, the release of files a retired pipeline glob
    held that the harness never built, and the pipeline-build refusal parser. IG1 replays
    the evalbase1 measles record (tests/fixtures/integrity_measles_evalbase1.json)."""
    import tempfile
    fails: list[str] = []

    def ok(c, m):
        if not c:
            fails.append(m)

    def w(d, rel, text):
        p = os.path.join(d, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)

    def spec(d, outputs, extra=""):
        # JSON is YAML; `extra` changes the size so the record sees a new declaration.
        w(d, "models/figures.py", "print('figures')\n")
        w(d, "models/pipeline.yaml", json.dumps({"steps": [
            {"id": "figures", "run": "python3 models/figures.py", "purpose": "figures" + extra,
             "inputs": ["models/figures.py"], "outputs": list(outputs)}]}) + "\n")

    def harness_build(ig, d, rels):
        """What scripts/pipeline.py harness_build does for the files its steps wrote."""
        with ig.harness_pipeline(list(rels)):
            for rel in rels:
                w(d, rel, f"built {rel}\n")
            for rel in rels:
                ig.harness_wrote(rel)

    def labels(ig):
        return sorted({f"{i['agent'] or 'lead'}:{i['tool']}" for i in ig.inflight.values()})

    # IG1: the evalbase1 measles record, replayed.
    with open(_FIXTURE) as f:
        fx = json.load(f)
    with tempfile.TemporaryDirectory() as d:
        ig = ArtifactIntegrity(d, process_check=lambda: set())
        agents_by_id = {}
        for e in fx["events"]:
            op = e["op"]
            if op == "subagent_start":
                agents_by_id[e["agent_id"]] = e["agent"]
            elif op == "pre":
                ig.on_pre(e["id"], e["tool"], {"command": e.get("command", "")}, e["agent"],
                          cwd=d, agent_id=e["agent_id"])
            elif op == "post":
                new = sorted(x["file"] for x in ig.on_post(e["id"]))
                ok(new == sorted(e["expect_new"]), f"IG1 post {e['id']}: new events {new}")
                if e["id"] == "toolu_mod_eda":
                    lab = next((x["in_flight"] for x in ig.events if x["file"] == e["expect_new"][0]), [])
                    ok("data-agent:Bash" not in lab,
                       f"IG1a a stopped agent's call is no longer in flight: {lab}")
            elif op == "subagent_stop":
                ig.on_agent_stop(e["agent_id"])
                ok(not any(i.get("agent_id") == e["agent_id"] for i in ig.inflight.values()),
                   f"IG1b SubagentStop ends {agents_by_id.get(e['agent_id'])}'s calls: {labels(ig)}")
            elif op == "spec":
                spec(d, fx[e["figures_outputs"]], extra=e["figures_outputs"])
            elif op == "harness_build":
                ig.check_now()
                harness_build(ig, d, e["writes"])
            elif op == "write":
                w(d, e["file"], f"agent wrote {e['file']}\n")
            elif op == "final":
                ig.final_check()
        opened = sorted(x["file"] for x in ig.open_events)
        ok(opened == fx["expected"]["open_after_final"], f"IG1c measles replay ends with no open violation: {opened}")
        closed = sorted(x["file"] for x in ig.events if not x.get("open", True)
                        and (x.get("resolved") or {}).get("by") == "declaration_retired")
        ok(closed == sorted(fx["expected"]["advisory_closed"]),
           f"IG1d the eda sidecars are closed as advisories by the declaration change: {closed}")
        with open(os.path.join(d, REPORT_NAME)) as f:
            rep = json.load(f)
        ok(rep.get("open_violations") == 0 and any("figures/*.provenance.json" in c.get("retired", [])
                                                   for c in rep.get("declaration_changes", [])),
           f"IG1e the record states the declaration change it acted on: {rep.get('declaration_changes')}")
        ok(not ig._watched("figures/eda_01_admin_vs_sero.png.provenance.json"), "IG1f released file unwatched")

    # IG2: a real tamper is still caught. A file the harness BUILT stays owned after its glob retires.
    with tempfile.TemporaryDirectory() as d:
        spec(d, ["figures/old_*.png", "figures/h1_*.png"], extra="a")
        ig = ArtifactIntegrity(d, process_check=lambda: set())
        harness_build(ig, d, ["figures/old_a.png", "figures/h1_a.png"])
        ig.on_pre("t1", "Bash", {"command": "python3 -c 'edit'"}, "modeler", cwd=d, agent_id="m1")
        w(d, "figures/old_a.png", "hand edit\n")
        ok([x["file"] for x in ig.on_post("t1")] == ["figures/old_a.png"], "IG2a hand edit of a built output flagged")
        ig.on_pre("t2", "Bash", {"command": "python3 -c 'narrow'"}, "modeler", cwd=d, agent_id="m1")
        spec(d, ["figures/h1_*.png"], extra="bb")
        ig.on_post("t2")
        ok(ig.is_open("figures/old_a.png"), "IG2b retiring the glob does not close a violation on a built file")
        ig.on_pre("t3", "Bash", {"command": "python3 -c 'edit'"}, "modeler", cwd=d, agent_id="m1")
        w(d, "figures/old_a.png", "hand edit 2\n")
        ok([x["file"] for x in ig.on_post("t3")] == ["figures/old_a.png"],
           "IG2c a built file stays protected after its glob retires (sticky)")
        ig.on_agent_stop("m1")
        w(d, "figures/h1_a.png", "changed by nobody\n")
        ok([x["file"] for x in ig.final_check()] == ["figures/h1_a.png"],
           "IG2d a change with nothing in flight is a violation")

    # IG3: release happens only on a declaration change, never by time.
    with tempfile.TemporaryDirectory() as d:
        spec(d, ["figures/*.provenance.json"], extra="a")
        ig = ArtifactIntegrity(d, process_check=lambda: set())
        ig.on_pre("t1", "Bash", {"command": "python3 eda.py"}, "modeler", cwd=d, agent_id="m1")
        w(d, "figures/eda.png.provenance.json", "{}\n")
        ok([x["file"] for x in ig.on_post("t1")] == ["figures/eda.png.provenance.json"],
           "IG3a an agent write under a current pipeline glob is flagged")
        for i in ig.inflight.values():
            i["t"] -= INTENT_TTL_S * 10
        for ev in ig.events:
            ev["ts"] = "2000-01-01T00:00:00"
        ig.check_now()
        ok(ig.is_open("figures/eda.png.provenance.json"), "IG3b no timer releases it")
        spec(d, ["figures/*.provenance.json"], extra="ab")      # same glob, other text: not a retirement
        ig.check_now()
        ok(ig.is_open("figures/eda.png.provenance.json"),
           "IG3c a declaration change that keeps the glob releases nothing")
        ig.final_check()
        ok(ig.is_open("figures/eda.png.provenance.json"), "IG3d still open at the end")

    # IG4: an advisory-closed file is never treated as harness-produced: declared again, the
    # gate reports the step as not built (a finding), and the record does not count it as built.
    with tempfile.TemporaryDirectory() as d:
        spec(d, ["figures/*.provenance.json"], extra="a")
        ig = ArtifactIntegrity(d, process_check=lambda: set())
        ig.on_pre("t1", "Bash", {"command": "python3 eda.py"}, "modeler", cwd=d, agent_id="m1")
        w(d, "figures/eda.png.provenance.json", "{}\n")
        ig.on_post("t1")
        spec(d, ["figures/h1_*.png"], extra="bb")
        ig.check_now()
        ok(not ig.is_open("figures/eda.png.provenance.json"), "IG4a narrowed: closed as an advisory")
        spec(d, ["figures/*.provenance.json"], extra="ccc")
        new = ig.check_now()
        ok(new == [] and "figures/eda.png.provenance.json" not in ig._built(),
           f"IG4b declared again it is baselined, not harness-built: {new}")
        pl = _pipeline()
        found = pl.gate_findings(d) if pl is not None else []
        ok(any(x.get("kind") == "pipeline_outputs_stale" for x in found),
           f"IG4c the gate reading it as a pipeline output gets a finding: {found}")
        ig.on_pre("t2", "Bash", {"command": "python3 eda.py"}, "modeler", cwd=d, agent_id="m1")
        w(d, "figures/eda.png.provenance.json", "{\"again\": 1}\n")
        ok([x["file"] for x in ig.on_post("t2")] == ["figures/eda.png.provenance.json"],
           "IG4d protected again once declared: an agent write is flagged")

    # IG5: the auto-backgrounded call (flu planner #16). Its PostToolUse never comes; the SDK's
    # task messages carry its tool_use_id (TaskStartedMessage / TaskNotificationMessage) or only
    # the task id (TaskUpdatedMessage). Writes it made before finishing are judged with its intent.
    with tempfile.TemporaryDirectory() as d:
        w(d, "uncertainty_report.yaml", "n: 1\n")
        ig = ArtifactIntegrity(d, process_check=lambda: set())
        ig.on_pre("toolu_bg", "Bash", {"command": f"python3 scripts/propagate_uncertainty.py {d}"}, "modeler",
                  cwd=enforcement.REPO_ROOT, agent_id="m1")
        ig.on_task_started("bk1", "toolu_bg")
        w(d, "uncertainty_report.yaml", "n: 2\n")
        ok(ig.on_task_ended(task_id="bk1") == [] and "toolu_bg" not in ig.inflight,
           f"IG5a a terminal task update ends the backgrounded call, after judging its writes: {labels(ig)}")
        ig.on_pre("toolu_bg2", "Bash", {"command": "sleep 300"}, "planner", cwd=d, agent_id="p1")
        ig.on_task_ended(tool_use_id="toolu_bg2")
        ok("toolu_bg2" not in ig.inflight, "IG5b a task notification with the tool_use_id ends it")
        ok(ig.on_task_ended(task_id="unknown") == [], "IG5c an unknown task id is ignored")

    # IG6: a stale carve-out no longer explains anything (the 15-minute hole). The planner's
    # backgrounded call held its metadata.json carve-out after the planner stopped.
    with tempfile.TemporaryDirectory() as d:
        w(d, "metadata.json", "{}\n")
        ig = ArtifactIntegrity(d, process_check=lambda: set())
        ig.on_pre("toolu_p16", "Bash", {"command": "python3 /tmp/fs_check.py"}, "planner", cwd=d, agent_id="p1")
        ig.on_agent_stop("p1")
        ig.on_pre("l1", "Bash", {"command": "printf x > metadata.json"}, None, cwd=d, agent_id="lead")
        w(d, "metadata.json", "{\"forged\": 1}\n")
        ok([x["file"] for x in ig.on_post("l1")] == ["metadata.json"],
           "IG6 after the planner stops, its carve-out does not explain a lead's write")

    # IG7: session end ends every call still in flight, counted as unmatched.
    with tempfile.TemporaryDirectory() as d:
        ig = ArtifactIntegrity(d, process_check=lambda: set())
        ig.on_pre("a", "Bash", {"command": "true"}, "modeler", cwd=d, agent_id="m1")
        ig.on_pre("b", "Agent", {}, None, cwd=d, agent_id="lead")
        ig.on_session_end()
        ok(ig.inflight == {} and ig.unmatched_intents == 2, f"IG7 session end: {ig.inflight} {ig.unmatched_intents}")

    # IG8: SubagentStop ends only that agent's calls (two agents of one type in parallel).
    with tempfile.TemporaryDirectory() as d:
        ig = ArtifactIntegrity(d, process_check=lambda: set())
        ig.on_pre("x", "Bash", {"command": "true"}, "critique-domain", cwd=d, agent_id="c1")
        ig.on_pre("y", "Bash", {"command": "true"}, "critique-domain", cwd=d, agent_id="c2")
        ig.on_agent_stop("c1")
        ok(list(ig.inflight) == ["y"], f"IG8 only c1's call ends: {list(ig.inflight)}")

    # IG11 (Codex r1 P1): a background producer launched by a call that never got its
    # PostToolUse does not keep a grant after its agent stops (the process is gone).
    with tempfile.TemporaryDirectory() as d:
        w(d, "model_comparison_formal.yaml", "a: 1\n")
        ig = ArtifactIntegrity(d, process_check=lambda: set())
        ig.on_pre("bg", "Bash", {"command": f"nohup python3 scripts/compare_models.py {d} &"}, "modeler",
                  cwd=enforcement.REPO_ROOT, agent_id="m1")
        ig.on_agent_stop("m1")
        ig.on_pre("l1", "Bash", {"command": "true"}, None, cwd=d, agent_id="lead")
        w(d, "model_comparison_formal.yaml", "a: forged\n")
        ok([x["file"] for x in ig.on_post("l1")] == ["model_comparison_formal.yaml"] and not ig.grants,
           f"IG11 the stopped agent's unarmed grant ends with it: {ig.grants}")

    # IG12 (Codex r1 P1): editing the runner's record cannot take back what the harness built.
    with tempfile.TemporaryDirectory() as d:
        spec(d, ["figures/old_*.png", "figures/h1_*.png"], extra="a")
        w(d, "figures/old_a.png", "built\n")
        w(d, "models/pipeline_state.json", json.dumps({"steps": {"figures": {
            "ok": True, "outputs": {"figures/old_a.png": "x"}}}}))
        ig = ArtifactIntegrity(d, process_check=lambda: set())       # a resumed run: built per the record
        ig.on_pre("t1", "Bash", {"command": "python3 -c 'x'"}, "modeler", cwd=d, agent_id="m1")
        w(d, "models/pipeline_state.json", json.dumps({"steps": {}}))
        spec(d, ["figures/h1_*.png"], extra="bb")
        w(d, "figures/old_a.png", "forged\n")
        ok("figures/old_a.png" in [x["file"] for x in ig.on_post("t1")],
           f"IG12 a built file stays owned when the state record is rewritten: {ig.open_events}")

    # IG13 (Codex r1 P2): one call that retires the glob and deletes the never-built file.
    with tempfile.TemporaryDirectory() as d:
        spec(d, ["figures/*.provenance.json"], extra="a")
        ig = ArtifactIntegrity(d, process_check=lambda: set())
        ig.on_pre("t1", "Bash", {"command": "python3 eda.py"}, "modeler", cwd=d, agent_id="m1")
        w(d, "figures/eda.png.provenance.json", "{}\n")
        ig.on_post("t1")
        ig.on_pre("t2", "Bash", {"command": "python3 -c 'x'"}, "modeler", cwd=d, agent_id="m1")
        os.remove(os.path.join(d, "figures/eda.png.provenance.json"))
        spec(d, ["figures/h1_*.png"], extra="bb")
        new = ig.on_post("t2")
        ok(new == [] and ig.open_events == [], f"IG13 nothing stays open: {new} {ig.open_events}")

    # IG14 (Codex r2 P1): positional binding never credits a producer (it only widens refusals).
    ok(harness_scripts_run(f"bash -c '$1 scripts/compare_models.py runs/r' sh python3", enforcement.REPO_ROOT)[0] == [],
       "IG14 `sh -c '$1 …' sh python3` does not credit compare_models.py")

    # IG15 (Codex r2 P1): a resumed record keeps the globs declared before and what the harness built.
    with tempfile.TemporaryDirectory() as d:
        spec(d, ["figures/old_*.png", "figures/h1_*.png"], extra="a")
        ig = ArtifactIntegrity(d, process_check=lambda: set())
        harness_build(ig, d, ["figures/old_a.png"])
        spec(d, ["figures/h1_*.png"], extra="bb")
        ig.final_check()
        ig2 = ArtifactIntegrity(d, process_check=lambda: set())         # resume
        ig2.on_pre("t1", "Bash", {"command": "python3 -c 'x'"}, "modeler", cwd=d, agent_id="m1")
        w(d, "figures/old_a.png", "forged after resume\n")
        ok([x["file"] for x in ig2.on_post("t1")] == ["figures/old_a.png"],
           f"IG15 a built file under a retired glob stays protected across a resume: {ig2.pipeline_patterns}")

    # IG9-IG10: which Bash commands are a full pipeline build in the real run directory.
    run = "runs/r1"
    for cmd in (f"nohup python3 scripts/pipeline.py run {run} > models/pipeline_run.log 2>&1 &",
                "python3 scripts/pipeline.py run . --budget-s 600",
                f"cd x; timeout 900 python3 {enforcement.REPO_ROOT}/scripts/pipeline.py run {run}",
                f"bash -c 'python3 scripts/pipeline.py run {run}'",
                f"python3 scripts/pipeline.py run -- {run}",                      # Codex r1
                f"python3 -W ignore scripts/pipeline.py run {run}",
                f"python3 -X utf8 -u scripts/pipeline.py run {run}",
                f"bash -c 'python3 scripts/pipeline.py run \"$@\"' sh {run}",
                f"python3 scripts/pipeline.py run -- {run} > models/log 2>&1",      # Codex r2
                f"python3 -uW ignore scripts/pipeline.py run {run}",
                f"python3 -uXutf8 scripts/pipeline.py run {run}",
                f"python3 -BX utf8 scripts/pipeline.py run {run}"):
        ok(pipeline_builds(cmd, enforcement.REPO_ROOT), f"IG9 a full build is recognised: {cmd}")
    for cmd in (f"python3 scripts/pipeline.py run {run} sensitivity --scratch --keep",
                f"python3 scripts/pipeline.py run {run} b --scr",
                f"python3 scripts/pipeline.py run {run} --dry-run",
                f"python3 scripts/pipeline.py run {run} --dry",
                f"python3 scripts/pipeline.py validate {run}",
                "python3 scripts/pipeline.py --self-test",
                f"grep -n 'pipeline.py run {run}' models/notes.md",
                f"python3 models/pipeline.py run {run}",
                f"bash -c 'python3 scripts/pipeline.py run \"$@\"' sh {run} --dry-run",   # Codex r1
                f"python3 scripts/pipeline.py run -- {run} b"):
        ok(not pipeline_builds(cmd, enforcement.REPO_ROOT), f"IG10 not a full build: {cmd}")

    if fails:
        for m in fails:
            print("FAIL:", m)
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
