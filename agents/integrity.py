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
output is not an explanation. Declarations are sticky for the life of this
record (removing a step does not unprotect its files); files that already
exist when first declared are baselined, as at start (the runner reruns a
step that never ran, so the baseline is not what the gate reads).
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
                    "final_gate.json", "final_gate_stderr.txt", REPORT_NAME}
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
                                  "done_when_registry.json", "done_when_registry.json.tmp"),
    "decision_brief.py": ("decision_brief.signoff.json", "decision_brief.yaml", "success_criteria.yaml",
                          "decision_brief.signed/decision_brief.yaml", "decision_brief.signed/success_criteria.yaml"),
    "cross_model_critic.py": ("critique_crossmodel.yaml", "critique_crossmodel.yaml.tmp",
                              "critique_crossmodel.md"),
    "eligibility_probe.py": ("models/eligibility_probe.yaml",),
    "build_claims_ledger.py": ("models/claims_ledger.yaml", "claims_ledger.yaml"),
    "render_claims.py": ("report.md", "models/claims_ledger.yaml", "claims_ledger.yaml"),
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


def harness_scripts_run(command: str, cwd: str | None = None, _depth: int = 0
                        ) -> tuple[list[str], bool]:
    """Harness scripts a Bash command actually EXECUTES (a Python interpreter
    whose script argument resolves into the repo's scripts/ directory), and
    whether it backgrounds them. Mentions in comments, strings or other
    arguments do not count (Codex review of PR #15)."""
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
                inner, bg = harness_scripts_run(cmd_str, base, _depth + 1)
                found += inner
                background = background or bg
            continue
        if len(toks) < 2 or not re.fullmatch(r"python[\d.]*", os.path.basename(toks[0])):
            continue
        script = next((t for t in toks[1:] if not t.startswith("-")), None)
        if not script:
            continue
        script = os.path.expanduser(script)
        if base is None and not os.path.isabs(script):
            continue                     # relative script after `cd $X`: can't resolve
        real = os.path.realpath(os.path.join(base or "", script))
        if os.path.dirname(real) == _SCRIPTS_DIR and os.path.basename(real) in SCRIPT_PRODUCES:
            found.append(os.path.basename(real))
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
        self._baseline: set[str] = set()
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
        except Exception:  # noqa: BLE001 — keep what was declared before (sticky)
            return []
        self._pipeline_sig = sig
        new = [p for p in pats if p not in self.pipeline_patterns]
        self.pipeline_patterns.extend(new)
        return new

    def _pipeline_owned(self, rel: str) -> bool:
        mod = _pipeline()
        own = tuple(mod.OWN_FILES) if mod is not None else ()
        return rel in own or any(mod.path_match(rel, p) if mod is not None else fnmatch.fnmatchcase(rel, p)
                                 for p in self.pipeline_patterns)

    def _scan(self) -> dict[str, str | None]:
        out = {}
        self._mtimes_now = {}
        new_pats = self._refresh_pipeline_patterns()
        self._baseline = set()
        for pat in new_pats:
            for p in glob.glob(os.path.join(self.run_path, pat), recursive=True):
                rel = os.path.relpath(p, self.run_path).replace(os.sep, "/")
                if os.path.isfile(p) and rel not in self.hashes:
                    self._baseline.add(rel)
        for pat in _protected_patterns() + self.pipeline_patterns:
            for p in glob.glob(os.path.join(self.run_path, pat), recursive=True):
                if os.path.isfile(p):
                    rel = os.path.relpath(p, self.run_path).replace(os.sep, "/")
                    out[rel] = _sha(p)
                    self._mtimes_now[rel] = os.stat(p).st_mtime_ns
        rp = os.path.join(self.run_path, REPORT_NAME)
        if os.path.exists(rp):
            out[REPORT_NAME] = _sha(rp)
        return out

    # -- tool lifecycle ---------------------------------------------------
    def on_pre(self, tool_use_id: str, tool_name: str, tool_input: dict,
               agent_type: str | None, cwd: str | None = None) -> None:
        """Register what an ALLOWED call may change (call after enforcement)."""
        intent = {"agent": agent_type, "tool": tool_name, "paths": set(),
                  "t": time.monotonic()}
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

    def harness_wrote(self, rel: str) -> None:
        """The harness itself (not a tool) wrote `rel`, e.g. restoring a
        deleted report.md at finalize: accept the new content."""
        with self._lock:
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
            if not g["armed"] and g.get("launch_id") != getattr(self, "_posting", None):
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
            if rel in self._baseline:
                continue         # S1: existed before it was declared a pipeline output
            in_flight = sorted({f"{i['agent'] or 'lead'}:{i['tool']}" for i in self.inflight.values()})
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
                           "violations": self.events}, f, indent=2)
        except OSError:
            self.report_ok = False       # finalize blocks on in-memory events anyway
            return
        self.report_ok = True
        self.hashes[REPORT_NAME] = _sha(p)
