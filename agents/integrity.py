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
how the change was made.
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
                                  "benchmark_match.yaml"),
    "cross_model_critic.py": ("critique_crossmodel.yaml", "critique_crossmodel.yaml.tmp",
                              "critique_crossmodel.md"),
    "eligibility_probe.py": ("models/eligibility_probe.yaml",),
    "build_claims_ledger.py": ("models/claims_ledger.yaml", "claims_ledger.yaml"),
    "render_claims.py": ("report.md", "models/claims_ledger.yaml", "claims_ledger.yaml"),
}
_SCRIPTS_DIR = os.path.realpath(os.path.join(enforcement.REPO_ROOT, "scripts"))
_WRAPPERS = {"nohup", "env", "time", "nice", "command", "exec", "sudo", "stdbuf"}
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


def _protected_patterns() -> list[str]:
    pats = [p for p in enforcement.SCRIPT_OUTPUTS if p not in _HARNESS_WRITTEN]
    return pats + list(_CRITIQUE_PATTERNS)


def harness_scripts_run(command: str, cwd: str | None = None) -> tuple[list[str], bool]:
    """Harness scripts a Bash command actually EXECUTES (a Python interpreter
    whose script argument resolves into the repo's scripts/ directory), and
    whether it backgrounds them. Mentions in comments, strings or other
    arguments do not count (Codex review of PR #15)."""
    import shlex
    found, background = [], False
    base = cwd or enforcement.REPO_ROOT
    for seg in enforcement._shell_tokens(command):
        if seg in ("(", ")"):
            continue
        try:
            toks = shlex.split(seg)
        except ValueError:
            continue
        while toks and (re.match(r"^\w+=", toks[0]) or os.path.basename(toks[0]) in _WRAPPERS
                        or re.fullmatch(r"-\w+|\d+[smh]?", toks[0])):
            if os.path.basename(toks[0]) == "nohup":
                background = True
            toks = toks[1:]
        if len(toks) < 2 or not re.fullmatch(r"python[\d.]*", os.path.basename(toks[0])):
            continue
        script = next((t for t in toks[1:] if not t.startswith("-")), None)
        if not script:
            continue
        real = os.path.realpath(os.path.join(base, os.path.expanduser(script)))
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
    run_rel = os.path.relpath(run_abs, enforcement.REPO_ROOT)
    try:
        for p in psutil.process_iter(["cmdline", "cwd"]):
            argv = p.info.get("cmdline") or []
            if not argv or not any(run_abs in a or a.rstrip("/") == run_rel or run_rel + "/" in a
                                   for a in argv):
                continue
            for a in argv[1:]:
                real = os.path.realpath(os.path.join(p.info.get("cwd") or enforcement.REPO_ROOT, a))
                if os.path.dirname(real) == _SCRIPTS_DIR and os.path.basename(real) in SCRIPT_PRODUCES:
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
        self.report_ok = True
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
                self.events = [{"file": REPORT_NAME, "change": "invalid_at_start",
                                "detected": "start", "detail": str(e)[:200],
                                "ts": datetime.now().isoformat(timespec="seconds"),
                                "in_flight": []}]
        self.hashes = self._scan()
        self._write_report()

    # -- scanning ---------------------------------------------------------
    def _scan(self) -> dict[str, str | None]:
        out = {}
        for pat in _protected_patterns():
            for p in glob.glob(os.path.join(self.run_path, pat)):
                if os.path.isfile(p):
                    rel = os.path.relpath(p, self.run_path).replace(os.sep, "/")
                    out[rel] = _sha(p)
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
                if rel:
                    intent["paths"].add(rel)
        elif tool_name == "Bash":
            scripts, background = harness_scripts_run(tool_input.get("command") or "", cwd)
            for sc in scripts:
                intent["paths"].update(SCRIPT_PRODUCES[sc])
            if scripts and background:
                with self._lock:
                    self.grants.append({"scripts": set(scripts), "seen_dead": False, "armed": False,
                                        "paths": {o for sc in scripts for o in SCRIPT_PRODUCES[sc]}})
        with self._lock:
            self.inflight[tool_use_id or f"anon-{len(self.inflight)}"] = intent

    def on_post(self, tool_use_id: str | None) -> list[dict]:
        with self._lock:
            new = self._check(where="post_tool")
            self.inflight.pop(tool_use_id, None)
        return new

    def harness_wrote(self, rel: str) -> None:
        """The harness itself (not a tool) wrote `rel`, e.g. restoring a
        deleted report.md at finalize: accept the new content."""
        with self._lock:
            self.hashes[rel] = _sha(os.path.join(self.run_path, rel))

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
        if _is_critique(rel) and any(i["agent"] is not None for i in intents):
            return True          # critics own their files; only the lead is barred
        if any(rel in g["paths"] for g in self.grants):
            return True          # a background harness script launched for this run
        return any(rel in SCRIPT_PRODUCES.get(sc, ()) for sc in live)

    def _refresh_grants(self) -> set[str]:
        """Live harness scripts on this run; a background grant lasts until
        its process has been seen finished once (so its last write, made
        between hooks, is still explained)."""
        live = set(self._process_check() or ())
        keep = []
        for g in self.grants:
            if not g["armed"]:
                g["armed"] = True        # the launching call's own scan: too early to judge
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
        cur = self._scan()
        live = self._refresh_grants()
        new_events = []
        for rel in sorted(set(cur) | set(self.hashes)):
            if rel == REPORT_NAME:
                continue
            if cur.get(rel) == self.hashes.get(rel):
                continue
            if not self._explained(rel, live):
                ev = {"file": rel, "change": ("deleted" if rel not in cur else
                                              "created" if rel not in self.hashes else "modified"),
                      "detected": where, "ts": datetime.now().isoformat(timespec="seconds"),
                      "in_flight": sorted({f"{i['agent'] or 'lead'}:{i['tool']}"
                                           for i in self.inflight.values()})}
                new_events.append(ev)
        # The report itself: only this object writes it.
        if cur.get(REPORT_NAME) != self.hashes.get(REPORT_NAME):
            new_events.append({"file": REPORT_NAME, "change": "modified", "detected": where,
                               "ts": datetime.now().isoformat(timespec="seconds"),
                               "in_flight": []})
        self.hashes = cur
        if new_events:
            self.events.extend(new_events)
            self._write_report()
        return new_events

    def _write_report(self) -> None:
        p = os.path.join(self.run_path, REPORT_NAME)
        try:
            if os.path.exists(p) and not os.access(p, os.W_OK):
                os.chmod(p, 0o644)       # a chmod 444 must not freeze a clean report
            with open(p, "w") as f:
                json.dump({"producer": "agents/integrity.py",
                           "protected_patterns": _protected_patterns(),
                           "unmatched_intents": self.unmatched_intents,
                           "violations": self.events}, f, indent=2)
        except OSError:
            self.report_ok = False       # finalize blocks on in-memory events anyway
            return
        self.report_ok = True
        self.hashes[REPORT_NAME] = _sha(p)
