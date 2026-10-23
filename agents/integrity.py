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
_HARNESS_WRITTEN = {"metadata.json", "harness_manifest.json", "trace.jsonl",
                    "final_gate.json", "final_gate_stderr.txt", REPORT_NAME}
_CRITIQUE_PATTERNS = ("critique_*.yaml", "critique_*.md")
_SCRIPT_RE = re.compile(r"(?:^|[\s/'\"=;&|(])scripts/[\w./-]+\.py\b")
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
    return any(fnmatch.fnmatch(rel, p) for p in _CRITIQUE_PATTERNS)


def _protected_patterns() -> list[str]:
    pats = [p for p in enforcement.SCRIPT_OUTPUTS if p not in _HARNESS_WRITTEN]
    return pats + list(_CRITIQUE_PATTERNS)


def _live_harness_script() -> bool:
    """Is a harness script running right now (e.g. launched with nohup by an
    earlier tool call)? Then a script-output change is explained."""
    try:
        import psutil
    except ImportError:
        return False
    try:
        for p in psutil.process_iter(["cmdline"]):
            cmd = " ".join(p.info.get("cmdline") or [])
            if _SCRIPT_RE.search(cmd) or os.path.join(enforcement.REPO_ROOT, "scripts") in cmd:
                return True
    except Exception:  # noqa: BLE001 — a failed scan must not crash the hook
        return False
    return False


class ArtifactIntegrity:
    def __init__(self, run_path: str, process_check=_live_harness_script):
        self.run_path = run_path
        self.hashes: dict[str, str | None] = {}
        self.inflight: dict[str, dict] = {}
        self.events: list[dict] = []
        self.unmatched_intents = 0
        self._lock = threading.Lock()
        self._process_check = process_check
        # A resumed run keeps the violations already recorded.
        try:
            with open(os.path.join(run_path, REPORT_NAME)) as f:
                prior = json.load(f).get("violations") or []
            self.events = [e for e in prior if isinstance(e, dict)]
        except (OSError, ValueError, AttributeError):
            pass
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
                  "script": False, "t": time.monotonic()}
        if tool_name in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
            path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
            if path:
                rel = enforcement._run_rel(path, self.run_path, cwd)
                if rel:
                    intent["paths"].add(rel)
        elif tool_name == "Bash":
            intent["script"] = bool(_SCRIPT_RE.search(" " + (tool_input.get("command") or "")))
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
            self.inflight.clear()
            new = self._check(where="final")
            self._write_report()
            return new

    # -- core -------------------------------------------------------------
    def _explained(self, rel: str) -> bool:
        now = time.monotonic()
        intents = [i for i in self.inflight.values() if now - i["t"] < INTENT_TTL_S]
        if any(rel in i["paths"] for i in intents):
            return True                      # its own allowed Write/Edit
        if _is_critique(rel):
            # Critics own their files; only the lead is barred from them.
            return any(i["agent"] is not None for i in intents) or \
                any(i["script"] for i in intents)
        if any(i["script"] for i in intents):
            return True                      # a harness script in flight
        return bool(self._process_check())   # a background harness script

    def _check(self, where: str) -> list[dict]:
        cur = self._scan()
        new_events = []
        for rel in sorted(set(cur) | set(self.hashes)):
            if rel == REPORT_NAME:
                continue
            if cur.get(rel) == self.hashes.get(rel):
                continue
            if not self._explained(rel):
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
            with open(p, "w") as f:
                json.dump({"producer": "agents/integrity.py",
                           "protected_patterns": _protected_patterns(),
                           "unmatched_intents": self.unmatched_intents,
                           "violations": self.events}, f, indent=2)
        except OSError:
            return
        self.hashes[REPORT_NAME] = _sha(p)
