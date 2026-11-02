#!/usr/bin/env python3
"""The run console: a local page for the owner of a v2 run (docs/design/v2-run-console.md).

    python3 scripts/run_console.py --run-dir runs/<run>                    # one local run
    python3 scripts/run_console.py --roots runs ~/keep                     # every run under these roots
    python3 scripts/run_console.py --mirror <dir> --sync-cmd "<command>" --cloud-config cloud/configs/<x>.py
    python3 scripts/run_console.py --self-test

Serves one page (scripts/run_console.html) on 127.0.0.1 only. It reads run artifacts and
rebuilds the run's story as event cards; the owner chats with Claude about the run, records
feedback for the learning loop (<run_dir>/owner/feedback.jsonl) and answers the questions the
run waits on. The only writes that change a run go through the existing sign-off commands
(agents/stage_machine.py sign, scripts/scope_signoff.py sign, cloud/run_cluster.py <config>
sign). It never edits the spec, code or harness files, and never creates, scales or stops a
cluster. The one outbound call is the Claude API for chat (ANTHROPIC_API_KEY from the
environment; never logged).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import glob
import hashlib
import http.server
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse

import yaml

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (REPO, os.path.join(REPO, "scripts"), os.path.join(REPO, "agents")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

HTML = os.path.join(REPO, "scripts", "run_console.html")
OWNER_DIR = "owner"
FEEDBACK, CHAT = "owner/feedback.jsonl", "owner/chat.jsonl"
KINDS = ("question", "correction", "guidance", "approval")
LAYERS = ("engine", "analysis_harness", "critique", "writer", "spec", "data", "process")
STEERING = ("correction", "guidance", "approval")
FAIL_STATUSES = ("run_failed", "completed_gate_error", "completed_gate_not_accepted", "completed_scope_declined")
# Chat (claude-api skill): Opus 5.5, adaptive thinking, effort set explicitly, streamed, refusal fallback.
MODEL = "claude-opus-5-5"
MAX_TOKENS = 8000
EFFORT = "medium"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# USD per million tokens at Opus 5.5 rates: input, output, cache write (5-min, 1.25x), cache read.
PRICE = {"in": 4.0, "out": 20.0, "cache_write": 5.0, "cache_read": 0.20}
HISTORY_TURNS = 12
# Character budgets for the run context (each part says when it was cut).
BUDGET = {"results": 12000, "gate": 3000, "blockers": 6000, "tests": 3000, "timeline": 3000, "owner": 3000}
CLOUD_MIN_S = 60.0             # each cloud refresh attaches to a cluster: never more often than this

SYSTEM = """You are the run console's assistant. The owner of a modelling run (an epidemiological
modelling expert) asks you about one run of a code-driven modelling pipeline: its state, figures,
results, critique blockers, gate decisions, decision tests and the questions it is waiting on.

Rules:
- Use only the run context below and the conversation. If the context does not hold the answer, say
  so and name the file that would.
- Never invent or estimate a number. Every number and every factual claim about the run carries a
  citation in the form [[<file>#<anchor>]], where <file> is a path relative to the run directory and
  <anchor> is a blocker id, a heading, a key or a test id, for example [[critique_domain.yaml#D-001]],
  [[results.md#Recommendation]], [[gate_r1.json#action]], [[figures/<name>.png]].
- When the owner gives guidance or a correction, it has already been recorded as feedback. Say in
  two or three sentences what you would change and in which layer (engine, analysis_harness, critique,
  writer, spec, data, process). You cannot change the run, the spec or the code; the owner's sign-off
  answers are the only path that changes a run.
- Be concise and plain. No preamble."""


# ------------------------------------------------------------------ small readers (fail closed)
def _iso(t: float) -> str:
    return _dt.datetime.fromtimestamp(t, _dt.timezone.utc).isoformat(timespec="seconds")


def _now() -> str:
    return _iso(time.time())


def _rel(run_dir: str, p: str) -> str:
    return os.path.relpath(p, run_dir).replace(os.sep, "/")


def _read(path: str):
    """(parsed, None) or (None, error). JSON or YAML by extension; text otherwise."""
    raise NotImplementedError("run console β")


def read_jsonl(path: str) -> tuple[list, list]:
    """(rows, errors): a torn or non-object line is an error, not skipped silently."""
    raise NotImplementedError("run console β")


def _err_card(run_dir: str, path: str, stage: str, err: str) -> dict:
    raise NotImplementedError("run console β")


def _mtime(path: str) -> str:
    raise NotImplementedError("run console β")


def safe_path(run_dir: str, rel: str) -> str:
    """An existing file inside the run dir (links resolved), never a credential file."""
    raise NotImplementedError("run console β")


def states() -> tuple:
    from agents import stage_machine
    return tuple(stage_machine.STATES)


# ------------------------------------------------------------------ state, stepper, cards
def load_run_state(run_dir: str):
    """(state dict, None) through the harness's reader, or (None, error)."""
    raise NotImplementedError("run console β")


def stepper(st: dict) -> dict:
    raise NotImplementedError("run console β")


def timeline_cards(st: dict) -> list:
    raise NotImplementedError("run console β")


def critique_cards(run_dir: str) -> list:
    raise NotImplementedError("run console β")


def gate_cards(run_dir: str) -> list:
    raise NotImplementedError("run console β")


def figure_cards(run_dir: str) -> list:
    raise NotImplementedError("run console β")


def decision_test_cards(run_dir: str) -> list:
    raise NotImplementedError("run console β")


def compute_status(run_dir: str, cap_core_hours: float | None = None) -> dict:
    """Core-hours used vs cap and each call in flight (ledger reservations joined with their progress)."""
    raise NotImplementedError("run console β")


def _spec_status(run_dir: str):
    raise NotImplementedError("run console β")


def inbox(run_dir: str) -> dict:
    """The questions the run waits on: the spec sign-off and pending scope declarations."""
    raise NotImplementedError("run console β")


def _inbox_cards(run_dir: str, box: dict, st: dict) -> list:
    raise NotImplementedError("run console β")


def build_cards(run_dir: str, st: dict | None, box: dict, cap=None) -> list:
    raise NotImplementedError("run console β")


def headline(run_dir: str) -> str:
    raise NotImplementedError("run console β")


def engine_of(run_dir: str, st: dict | None) -> str | None:
    raise NotImplementedError("run console β")


def read_run(run_dir: str, cap=None) -> dict:
    raise NotImplementedError("run console β")


# ------------------------------------------------------------------ feedback and the learning view
def append_feedback(run_dir: str, rec: dict) -> dict:
    raise NotImplementedError("run console β")


def _append(run_dir: str, rel: str, row: dict) -> None:
    raise NotImplementedError("run console β")


def learning(run_dirs: list) -> dict:
    raise NotImplementedError("run console β")


def export_digest(run_dirs: list, out_dir: str) -> str:
    raise NotImplementedError("run console β")


# ------------------------------------------------------------------ the owner's answers (existing commands)
def _env() -> dict:
    # The owner signs from their own shell: MA_SIGNOFF_MODE marks an agent's environment (main.py).
    raise NotImplementedError("run console β")


def _run(cmd: list, runner=None) -> dict:
    raise NotImplementedError("run console β")


def _remote_sign(cfg: str, answers: dict, note: str, runner=None) -> dict:
    raise NotImplementedError("run console β")


def sign_spec(run_dir: str, note: str, confirm: bool, mirror: bool = False, cloud_config: str | None = None,
              status_fn=None, runner=None) -> dict:
    """The owner signs decision_spec.yaml through agents/stage_machine.py sign (remote: run_cluster sign)."""
    raise NotImplementedError("run console β")


def answer_scope(run_dir: str, answers: dict, note: str, confirm: bool, mirror: bool = False,
                 cloud_config: str | None = None, runner=None) -> dict:
    """Answers to the pending scope declarations, through scripts/scope_signoff.py sign (remote: run_cluster
    sign). `answers`: finding -> accept|decline, or {decision, note}; every pending item needs one."""
    raise NotImplementedError("run console β")


# ------------------------------------------------------------------ chat
def _cut(name: str, text: str, n: int, cut: list) -> str:
    raise NotImplementedError("run console β")


def chat_context(run_dir: str) -> dict:
    """The system prompt's two blocks: the fixed rules, then this run's context (each part cut to its
    budget, and the cut stated)."""
    raise NotImplementedError("run console β")


def price(usage) -> float:
    raise NotImplementedError("run console β")


def worst_case_usd(chars: int) -> float:
    """Before a call: the prompt as if all of it were a cache write (about 3 characters a token), plus a
    full max_tokens of output."""
    raise NotImplementedError("run console β")


_SENT = re.compile(r"(?<=[.!?])\s+|\n+")


def uncited(text: str) -> list:
    """Sentences that state a number and carry no [[file#anchor]] citation."""
    raise NotImplementedError("run console β")


def default_client():
    raise NotImplementedError("run console β")


class ChatSession:
    """One console session's chat: its spend and cap across runs."""

    def __init__(self, cap_usd: float = 2.0, client_factory=default_client, log=print):
        self.cap_usd, self.spent, self.client_factory, self.log = float(cap_usd), 0.0, client_factory, log
        self._lock = threading.Lock()

    def send(self, run_dir: str, text: str, anchor: dict | None = None, kind: str = "question",
             layer: str | None = None, proposed_change: str | None = None, stage: str | None = None,
             verdict: str | None = None) -> dict:
        raise NotImplementedError("run console β")


# ------------------------------------------------------------------ projects, build on a run
def discover(roots: list, mirror_roots: list = (), depth: int = 4) -> list:
    raise NotImplementedError("run console β")


def run_summary(run_dir: str, root: str | None = None, mirror: bool = False) -> dict:
    raise NotImplementedError("run console β")


def batch_reuse(run_dir: str) -> list:
    """For each stored engine batch (an --out with its .summary.json): would scripts/compute.py reuse its rows
    with today's code? The same test _reusable makes: the summary's fn and code_fp against code_fingerprint."""
    raise NotImplementedError("run console β")


BUILD_ON_FILES = ("analysis_settings.yaml", "engine_choice.json", "data/derivations.yaml",
                  "data/derivations_record.json", "data/README.md")
BUILD_ON_DIRS = ("data/derive", "data/derived")


def build_on(run_dir: str, drafts_root: str) -> dict:
    """A draft for a new run that reuses this run's assets. Launches nothing; the page shows the commands."""
    raise NotImplementedError("run console β")


# ------------------------------------------------------------------ compute on the cloud (read-only)
class CloudPanel:
    """run_cluster.py <config> status | health | report-cost, as subprocesses, at most once per CLOUD_MIN_S."""

    def __init__(self, config: str | None, runner=None, clock=time.time, coiled_ok=None):
        self.config, self.runner, self.clock = config, runner, clock
        if coiled_ok is None:
            import importlib.util
            coiled_ok = importlib.util.find_spec("coiled") is not None
        self.coiled_ok, self.last, self.at = coiled_ok, None, 0.0
        self._lock = threading.Lock()

    def snapshot(self, live: bool) -> dict:
        raise NotImplementedError("run console β")


# ------------------------------------------------------------------ the server
class Console:
    def __init__(self, roots: list, run_dirs: list = (), mirror: str | None = None, sync_cmd: str | None = None,
                 sync_s: float = 120.0, cloud_config: str | None = None, chat_cap_usd: float = 2.0,
                 core_hours_cap: float | None = None, client_factory=default_client, log=print):
        self.roots, self.extra, self.mirror = [os.path.realpath(os.path.expanduser(r)) for r in roots], \
            [os.path.realpath(d) for d in run_dirs], mirror and os.path.realpath(mirror)
        self.sync_cmd, self.sync_s, self.cloud_config, self.cap = sync_cmd, sync_s, cloud_config, core_hours_cap
        self.chat = ChatSession(chat_cap_usd, client_factory, log)
        self.cloud = CloudPanel(cloud_config)
        self.token, self.log, self.sync_state = secrets.token_urlsafe(24), log, None
        first = self.roots[0] if self.roots else os.path.join(REPO, "runs")
        self.drafts_root, self.digest_dir = os.path.join(first, "_drafts"), os.path.join(first, "_owner")

    def runs(self) -> list:
        raise NotImplementedError("run console β")

    def run_dir(self, rid: str) -> tuple[str, bool]:
        raise NotImplementedError("run console β")

    def sync_loop(self) -> None:
        raise NotImplementedError("run console β")

    def handle(self, method: str, path: str, query: dict, body: dict):
        """(status, content_type, payload bytes or JSON-able)."""
        raise NotImplementedError("run console β")


def make_server(console: Console, host: str = "127.0.0.1", port: int = 8765):
    raise NotImplementedError("run console β")


# ------------------------------------------------------------------ self-test
def _toy_run(d: str, state: str = "OWNER_DECISION", paused="awaiting_scope_signoff", status=None,
             path=("SPEC", "SIGN", "EVIDENCE", "ENGINE_CHOICE", "CONFIGURE", "ANALYZE", "CRITIQUE", "GATE", "FIX_LOOP",
                   "CRITIQUE", "GATE", "OWNER_DECISION")) -> str:
    """A synthetic run dir: toy numbers, no data from any real source."""
    os.makedirs(os.path.join(d, "figures"), exist_ok=True)
    os.makedirs(os.path.join(d, "analysis"), exist_ok=True)
    os.makedirs(os.path.join(d, "models", "decision_tests"), exist_ok=True)
    hist, prev = [], path[0]
    for i, s in enumerate(path[1:], 1):
        hist.append({"at": f"2026-01-01T00:{i:02d}:00+00:00", "from": prev, "to": s, "why": f"step {i}",
                     "round": 1 if i < 9 else 2, "spent_usd": 0.1 * i})
        prev = s
    hist.insert(8, {"at": "2026-01-01T00:08:30+00:00", "fix_loop": {"fixed": ["T-001"]}})
    json.dump({"question": "Toy question: choose one option per unit within a budget.", "state": state,
               "round": 2, "max_rounds": 3, "spent_usd": 1.2, "budget_usd": 5.0, "status": status, "paused": paused,
               "history": hist, "engine": "toy.engine:Toy"}, open(os.path.join(d, "run_state.json"), "w"))
    json.dump({"question": "Toy question", "started": "2026-01-01T00:00:00+00:00"},
              open(os.path.join(d, "metadata.json"), "w"))
    yaml.safe_dump({"reviewer": "critique-domain", "round": 1, "blockers": [
        {"id": "T-001", "severity": "HIGH", "category": "METHODS", "target_stage": "ANALYZE",
         "claim": "The interval omits one source.", "fix_requires": "Add it.", "resolved": False},
        {"id": "T-002", "severity": "LOW", "category": "PRESENTATION", "target_stage": "WRITE",
         "claim": "A label is unclear.", "resolved": True}]}, open(os.path.join(d, "critique_domain.yaml"), "w"))
    json.dump({"action": "AWAIT_OWNER", "rule_matched": "3m", "rationale": "a decision-bearing finding",
               "unresolved_high": [{"claim": "DT1 changes the plan"}], "done_when_status": {"T-001": "fail"}},
              open(os.path.join(d, "gate_r2.json"), "w"))
    open(os.path.join(d, "figures", "plan.png"), "wb").write(b"\x89PNG\r\n\x1a\n")
    json.dump({"producer": "toolkit/analysis/figures.py", "written_at_utc": "2026-01-01T00:05:30+00:00",
               "source_hashes": {"analysis/point.yaml": "ab"}},
              open(os.path.join(d, "figures", "plan.png.provenance.json"), "w"))
    yaml.safe_dump({"data": {"figures": [{"path": "figures/plan.png", "shows": "plan option counts"}]}},
                   open(os.path.join(d, "analysis", "figures.yaml"), "w"))
    yaml.safe_dump({"data": {"tests": [{"test_id": "DT1", "path": "models/decision_tests/DT1.yaml",
                                        "verdict": "CHANGES_PLAN", "statement": "re-solved"}]}},
                   open(os.path.join(d, "analysis", "decision_tests.yaml"), "w"))
    yaml.safe_dump({"test_id": "DT1", "question": "Does the plan change with a lower budget?", "decision_changes": True,
                    "criterion": "the option changes in more than 10% of units"},
                   open(os.path.join(d, "models", "decision_tests", "DT1.yaml"), "w"))
    open(os.path.join(d, "results.md"), "w").write("# Results\n\n## Recommendation\n\nThe plan averts 120 toy "
                                                   "events (95% UI 100-140).\n\n## Other\n\ntext\n")
    now = time.time()
    json.dump({"core_seconds": 7200.0, "calls": [{"fn": "models/f.py::f"}], "reservations": {"h:1:2": {
        "fn": "models/f.py::f", "n": 100, "purpose": "T-001", "started": now - 100,
        "progress": ".compute_progress/a.json"}}},
        open(os.path.join(d, "models", "compute_ledger.json"), "w"))
    os.makedirs(os.path.join(d, ".compute_progress"), exist_ok=True)
    json.dump({"t": now, "core_s": 50.0, "done_tasks": 25}, open(os.path.join(d, ".compute_progress", "a.json"), "w"))
    json.dump({"pending": True, "round": 2, "items": [
        {"finding": "decision_test:DT1", "fingerprint": "fp0000000000aaaa", "reason": "a measured decision test "
         "changes the plan", "claim": "DT1 changes the plan", "declarations": [], "declared": False}]},
        open(os.path.join(d, "scope_signoff_request.json"), "w"))
    return d


def _tree(d: str) -> dict:
    out = {}
    for dp, _, fns in os.walk(d):
        for fn in fns:
            p = os.path.join(dp, fn)
            out[_rel(d, p)] = hashlib.sha256(open(p, "rb").read()).hexdigest()
    return out


def _self_test() -> int:
    from types import SimpleNamespace as NS
    import urllib.request
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)

    with tempfile.TemporaryDirectory() as td:
        rd = _toy_run(os.path.join(td, "runs", "proj", "run1"))
        # RC1 state and stepper
        st, err = load_run_state(rd)
        sp = stepper(st)
        by = {s["state"]: s["status"] for s in sp["stages"]}
        ok(err is None and by["OWNER_DECISION"] == "waiting" and by["GATE"] == "done" and by["FIX_LOOP"] == "done"
           and by["WRITE"] == "todo" and sp["fix_rounds"] == 1 and sp["round"] == 2,
           f"RC1a stepper: done, waiting, todo and one fix-loop round ({by}, {sp['fix_rounds']})")
        ok([s["state"] for s in sp["stages"]] == list(states()) and states()[0] == "SPEC" and states()[-1] == "FINAL",
           "RC1b the stage list is stage_machine.STATES")
        rd2 = _toy_run(os.path.join(td, "runs", "proj", "run2"), "WRITE", None,
                       path=("SPEC", "SIGN", "EVIDENCE", "ENGINE_CHOICE", "CONFIGURE", "ANALYZE", "CRITIQUE", "GATE",
                             "WRITE"))
        by2 = {s["state"]: s["status"] for s in stepper(load_run_state(rd2)[0])["stages"]}
        ok(by2["FIX_LOOP"] == "skipped" and by2["OWNER_DECISION"] == "skipped" and by2["WRITE"] == "current",
           f"RC1c states before the current one never entered are skipped ({by2})")
        rd3 = _toy_run(os.path.join(td, "runs", "other", "run3"), "FINAL", None, "run_failed",
                       path=("SPEC", "SIGN", "EVIDENCE", "ENGINE_CHOICE", "CONFIGURE", "ANALYZE", "FINAL"))
        by3 = {s["state"]: s["status"] for s in stepper(load_run_state(rd3)[0])["stages"]}
        ok(by3["ANALYZE"] == "failed" and by3["CRITIQUE"] == "skipped", f"RC1d a failed run marks the state it "
           f"failed in ({by3})")
        bad = os.path.join(td, "bad")
        os.makedirs(bad)
        open(os.path.join(bad, "run_state.json"), "w").write("{not json")
        r = read_run(bad)
        ok(r["error"] and "unreadable" in r["error"] and r["stepper"] is None, "RC1e unreadable run_state.json is an "
           "error the page shows")
        # RC2 blockers and gates, fail closed
        cards = read_run(rd)["cards"]
        kinds = [c["kind"] for c in cards]
        b1 = [c for c in cards if c["kind"] == "blocker" and c["blocker_id"] == "T-001"]
        ok(b1 and b1[0]["severity"] == "HIGH" and b1[0]["target_stage"] == "ANALYZE" and b1[0]["resolved"] is False
           and b1[0]["anchor"] == {"kind": "blocker", "id": "T-001", "file": "critique_domain.yaml"},
           "RC2a a blocker card carries id, severity, target stage, resolved and its anchor")
        g = [c for c in cards if c["kind"] == "gate"]
        ok(g and g[0]["action"] == "AWAIT_OWNER" and g[0]["rule"] == "3m" and g[0]["unresolved_high"],
           "RC2b the gate card carries action, rule and unresolved HIGHs")
        open(os.path.join(rd2, "critique_redteam.yaml"), "w").write("blockers: [unclosed")
        open(os.path.join(rd2, "gate_r1.json"), "w").write("{}")
        errs = [c for c in critique_cards(rd2) + gate_cards(rd2) if c["kind"] == "error"]
        ok(len(errs) == 2 and {e["anchor"]["file"] for e in errs} == {"critique_redteam.yaml", "gate_r1.json"},
           "RC2c an unreadable critique file and a gate record with no action are error cards (fail closed)")
        # RC3 figures and compute
        f = [c for c in cards if c["kind"] == "figure"]
        ok(f and f[0]["body"] == "plan option counts" and f[0]["producer"] == "toolkit/analysis/figures.py"
           and f[0]["at"] == "2026-01-01T00:05:30+00:00", "RC3a a figure card has its caption and provenance")
        cs = compute_status(rd)
        c0 = cs["calls"][0] if cs["calls"] else {}
        ok(cs["used_core_h"] == 2.0 and c0.get("done") == 25 and c0.get("n") == 100
           and 250 <= (c0.get("eta_s") or 0) <= 350,
           f"RC3b ledger and progress give core-hours, done/n and an ETA from the observed rate ({cs})")
        ok("decision_test" in kinds and "decision_needed" in kinds and "batch" in kinds and "stage" in kinds
           and "fix_loop" in kinds, "RC3c the stream holds stage, fix-loop, batch, decision-test and decision cards")
        ok([c.get("at") or "" for c in cards] == sorted(c.get("at") or "" for c in cards), "RC3d the stream is in "
           "time order")
        # RC4 feedback and export
        try:
            append_feedback(rd, {"kind": "opinion", "layer": "engine", "text": "x"})
            ok(False, "RC4a an unknown kind is refused")
        except ValueError:
            ok(True, "RC4a an unknown kind is refused")
        try:
            append_feedback(rd, {"kind": "guidance", "layer": "elsewhere", "text": "x"})
            ok(False, "RC4b an unknown layer is refused")
        except ValueError:
            ok(True, "RC4b an unknown layer is refused")
        f1 = append_feedback(rd, {"kind": "correction", "layer": "engine", "text": "the waning prior is too long",
                                  "anchor": {"kind": "blocker", "id": "T-001", "file": "critique_domain.yaml"},
                                  "proposed_change": "shorten it", "verdict": "agree"})
        append_feedback(rd2, {"kind": "guidance", "layer": "writer", "text": "say the rule in words"})
        rows, _ = read_jsonl(os.path.join(rd, FEEDBACK))
        ok(rows and set(rows[0]) >= {"id", "ts", "anchor", "kind", "layer", "text", "proposed_change"}
           and rows[0]["anchor"]["id"] == "T-001", "RC4c a feedback line has ts, anchor, kind, layer, text and change")
        p = export_digest([rd, rd2], os.path.join(td, "digest"))
        txt = open(p).read()
        ok("## engine (1)" in txt and "## writer (1)" in txt and f1["id"] in txt and "shorten it" in txt
           and txt.index("## engine") < txt.index("## writer"), "RC4d the export groups feedback by layer across runs")
        # RC5 scope answers through scripts/scope_signoff.py, read back by the harness's reader
        import scope_signoff as SS
        for kw, why in ((dict(answers={"decision_test:DT1": "accept"}, note="n", confirm=False), "not confirmed"),
                        (dict(answers={"decision_test:DT1": "accept"}, note=" ", confirm=True), "note"),
                        (dict(answers={}, note="n", confirm=True), "missing"),
                        (dict(answers={"decision_test:DT1": "maybe"}, note="n", confirm=True), "missing")):
            r = answer_scope(rd, **kw)
            ok(not r["ok"] and why in r.get("refused", ""), f"RC5a scope answer refused: {why}")
        r = answer_scope(rd, {"decision_test:DT1": "accept"}, "n", True, mirror=True)
        ok(not r["ok"] and "--cloud-config" in r["refused"], "RC5b a mirrored run is not answered locally")
        seen = []
        r = answer_scope(rd, {"decision_test:DT1": "decline"}, "remote note", True, mirror=True, cloud_config="c.py",
                         runner=lambda cmd, **k: seen.append((cmd, open(cmd[4]).read()))
                         or NS(returncode=0, stdout="ok", stderr=""))
        ok(r["ok"] and seen and seen[0][0][1:4] == ["cloud/run_cluster.py", "c.py", "sign"]
           and "decision_test:DT1: decline" in seen[0][1] and seen[0][0][-1] == "remote note",
           "RC5c a mirrored run is answered through run_cluster.py <config> sign <answers.yaml> <note>")
        r = answer_scope(rd, {"decision_test:DT1": "accept"}, "the plan stands", True)
        ans, bad = SS.load_answers(rd)
        a = ans.get("fp0000000000aaaa") or {}
        ok(r["ok"] and not bad and a.get("decision") == "accept" and a.get("by") == "owner"
           and a.get("note") == "the plan stands",
           f"RC5d the answer round-trips through scope_signoff.load_answers ({r})")
        ok(bool((json.load(open(os.path.join(rd, SS.SIGNOFF))) or {}).get("answers")),
           "RC5e h_OWNER_DECISION's check (scope_signoff.json has answers) now holds")
        ok(inbox(rd)["scope"]["items"][0]["answer"] == "accept", "RC5f the inbox shows the recorded answer")
        # RC6 spec sign-off
        ok(not sign_spec(rd, "n", False)["ok"], "RC6a spec sign-off refused without confirmation")
        ok("note" in sign_spec(rd, "", True).get("refused", ""), "RC6b spec sign-off refused without a note")
        r = sign_spec(rd, "n", True)
        ok(not r["ok"] and "not awaiting" in r["refused"], f"RC6c no spec: refused ({r.get('refused')})")
        r = sign_spec(rd, "n", True, status_fn=lambda d: "signed")
        ok(not r["ok"] and "signed" in r["refused"], "RC6d an already signed spec is refused")
        seen = []
        r = sign_spec(rd, "looks right", True, status_fn=lambda d: "awaiting",
                      runner=lambda cmd, **k: seen.append((cmd, k.get("env")))
                      or NS(returncode=0, stdout="{}", stderr=""))
        ok(r["ok"] and seen[0][0][1:] == ["agents/stage_machine.py", "sign", rd, "--by", "owner", "--note",
                                          "looks right"]
           and "MA_SIGNOFF_MODE" not in seen[0][1], "RC6e a confirmed sign runs stage_machine.py sign --by owner")
        # RC7 chat context and cost cap (mocked client)
        ctx = chat_context(rd)
        ok("[[critique_domain.yaml#T-001]]" in ctx["run"] and "T-002" not in ctx["run"] and "averts 120" in ctx["run"]
           and "AWAIT_OWNER" in ctx["run"] and "[[models/decision_tests/DT1.yaml#DT1]]" in ctx["run"],
           "RC7a context: state, results, latest gate, unresolved blockers only, decision tests with citations")
        old = BUDGET["results"]
        BUDGET["results"] = 20
        ctx2 = chat_context(rd)
        BUDGET["results"] = old
        ok("results.md" in ctx2["truncated"] and "[truncated: results.md, first 20 of" in ctx2["run"],
           "RC7b a cut part is stated in the context and listed")
        calls, logs = [], []

        class Stream:
            def __init__(self, kw):
                calls.append(kw)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get_final_message(self):
                return NS(content=[NS(type="thinking", thinking=""), NS(type="text", text="It averts 120 events "
                          "[[results.md#Recommendation]]. The budget is 5.")], stop_reason="end_turn", model=MODEL,
                          usage=NS(input_tokens=100, output_tokens=200, cache_creation_input_tokens=1000,
                                   cache_read_input_tokens=0))

        client = NS(beta=NS(messages=NS(stream=lambda **kw: Stream(kw))))
        os.environ["ANTHROPIC_API_KEY"], prev = "sk-test-SECRETKEY", os.environ.get("ANTHROPIC_API_KEY")
        try:
            cs = ChatSession(0.5, lambda: client, log=logs.append)
            r = cs.send(rd, "What does the plan avert?", {"kind": "figure", "id": "figures/plan.png",
                                                         "file": "figures/plan.png"}, stage="ANALYZE")
            kw = calls[0] if calls else {}
            ok(r["ok"] and kw.get("model") == "claude-opus-5-5" and kw.get("thinking") == {"type": "adaptive"}
               and kw["output_config"]["effort"] == EFFORT and kw["fallbacks"] == "default"
               and all(b.get("cache_control") for b in kw["system"]) and kw["system"][0]["text"] == SYSTEM
               and "Anchored to figure figures/plan.png" in kw["messages"][-1]["content"],
               "RC7c the call: Opus 5.5, adaptive thinking, explicit effort, fallback, both system blocks cached, "
               "anchor")
            ok(abs(r["reply"]["usd"] - (100 * 4 + 200 * 20 + 1000 * 5) / 1e6) < 1e-9 and cs.spent == r["reply"]["usd"]
               and r["reply"]["uncited"] == ["The budget is 5."], "RC7d cost from usage; an uncited number is marked")
            rows, _ = read_jsonl(os.path.join(rd, CHAT))
            ok([x["role"] for x in rows] == ["user", "assistant"], "RC7e the turn is persisted in owner/chat.jsonl")
            cs.send(rd, "And the interval?")
            ok(len(calls) == 2 and [m["role"] for m in calls[1]["messages"]] == ["user", "assistant", "user"],
               "RC7f the next turn carries the persisted history")
            cs.spent = 0.45
            r = cs.send(rd, "One more?")
            ok(not r["ok"] and "chat cap" in r["error"] and len(calls) == 2, "RC7g a call past the session cap is "
               "refused before it is made")
            cs.spent = 0.0
            r = cs.send(rd, "the waning prior is too long", kind="guidance", layer="engine",
                        anchor={"kind": "blocker", "id": "T-001", "file": "critique_domain.yaml"})
            ok(r["ok"] and r["feedback"]["layer"] == "engine" and "recorded as feedback" in calls[-1]["messages"][-1][
                "content"], "RC7h steering is recorded as feedback first and Claude is asked to acknowledge it")
            r = cs.send(rd, "guidance with no layer", kind="guidance")
            ok(not r["ok"] and "layer" in r["error"], "RC7i steering needs a layer")
            ok(not any("SECRETKEY" in m for m in logs) and "SECRETKEY" not in open(os.path.join(rd, CHAT)).read(),
               "RC7j the key is in no log line and no persisted row")
            del os.environ["ANTHROPIC_API_KEY"]
            r = ChatSession(1.0).send(rd, "hello")
            ok(not r["ok"] and "ANTHROPIC_API_KEY is not set" in r["error"], "RC7k no key: a clear message")
        finally:
            if prev is None:
                os.environ.pop("ANTHROPIC_API_KEY", None)
            else:
                os.environ["ANTHROPIC_API_KEY"] = prev
        # RC8 who may write owner/
        from agents import v2_sdk_tasks as T
        ok(all(T._allowed("owner/feedback.jsonl", rd, owns) and T._allowed("owner/chat.jsonl", rd, owns)
               for owns in T.OWNS.values()), "RC8a no v2 task may write owner/ (file tools only, OWNS allowlist)")
        before = _tree(rd)
        append_feedback(rd, {"kind": "approval", "layer": "process", "text": "fine"})
        cs = ChatSession(1.0, lambda: client, log=lambda *a: None)
        cs.send(rd, "q")
        drafts = os.path.join(td, "runs", "_drafts")
        build_on(rd, drafts)
        after = _tree(rd)
        changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
        ok(changed == [CHAT, FEEDBACK], f"RC8b the console's own writes stay under owner/ ({changed})")
        # RC9 discovery and build on
        runs = discover([os.path.join(td, "runs")])
        names = {r["name"]: r for r in runs}
        ok(set(names) == {"run1", "run2", "run3"} and names["run1"]["headline"].startswith("The plan averts 120")
           and names["run1"]["group"] == "proj" and names["run1"]["engine"] == "toy.engine:Toy",
           f"RC9a runs are found under the roots with question, headline, group and engine ({sorted(names)})")
        os.makedirs(os.path.join(rd, "data", "derive"))
        open(os.path.join(rd, "data", "derive", "units.py"), "w").write("print('toy')\n")
        open(os.path.join(rd, "decision_spec.yaml"), "w").write("toy: spec\n")
        os.makedirs(os.path.join(rd, "models", "batch"))
        open(os.path.join(rd, "models", "f.py"), "w").write("def f(x):\n    return x\n")
        import compute as C
        o2 = os.path.join(rd, "models", "batch", "old.jsonl")
        open(o2, "w").write("")
        json.dump({"fn": "models/f.py::f", "code_fp": "0" * 16, "n": 3}, open(o2 + ".summary.json", "w"))
        o = os.path.join(rd, "models", "batch", "rows.jsonl")   # another call's results file is part of a
        open(o, "w").write("")                                   # fingerprint: written before it is taken
        fp = C.code_fingerprint(rd, "models/f.py::f", exclude=(o, o + ".tmp", o + ".summary.json",
                                                               o + ".summary.json.tmp", o + C.PARTIAL_EXT,
                                                               o + C.JOB_EXT, o + ".log"))
        json.dump({"fn": "models/f.py::f", "code_fp": fp, "n": 3}, open(o + ".summary.json", "w"))
        b = build_on(rd, drafts)
        reuse = {x["out"]: x["reused"] for x in b["batches"]}
        ok(os.path.exists(os.path.join(b["draft_dir"], "decision_spec.yaml"))
           and os.path.exists(os.path.join(b["draft_dir"], "data", "derive", "units.py"))
           and os.path.exists(os.path.join(b["draft_dir"], "build_on.json"))
           and b["commands"]["continue_this_run"].endswith(f"--resume {rd}"),
           "RC9b the draft holds the spec, the derivation scripts, its manifest and the commands")
        ok(reuse == {"models/batch/rows.jsonl": True, "models/batch/old.jsonl": False},
           f"RC9c batch reuse is compute.code_fingerprint's verdict ({reuse})")
        ok(not any(r["name"].startswith("2") for r in discover([os.path.join(td, "runs")])),
           "RC9d drafts under _drafts are not listed as runs")
        # RC10 the server: loopback only, token on POST, endpoints
        try:
            make_server(Console([td]), "0.0.0.0", 0)
            ok(False, "RC10a a non-loopback bind is refused")
        except ValueError:
            ok(True, "RC10a a non-loopback bind is refused")
        con = Console([os.path.join(td, "runs")], client_factory=lambda: client, log=lambda *a: None)
        srv = make_server(con, "127.0.0.1", 0)
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        try:
            page = urllib.request.urlopen(base + "/").read().decode()
            ok(con.token in page and "__CONSOLE_TOKEN__" not in page, "RC10b the page is served with its token")
            lst = json.load(urllib.request.urlopen(base + "/api/runs"))
            run = json.load(urllib.request.urlopen(base + "/api/run?run=" + urllib.parse.quote(rd)))
            ok(len(lst["runs"]) == 3 and run["stepper"]["round"] == 2 and run["cards"],
               "RC10c /api/runs and /api/run answer on a toy run")
            fig = urllib.request.urlopen(base + "/api/file?run=" + urllib.parse.quote(rd) + "&path=figures/plan.png")
            ok(fig.headers["Content-Type"] == "image/png", "RC10d /api/file serves a figure")
            for bad_path in ("../../etc/passwd", "run_state.json/../../x"):
                try:
                    urllib.request.urlopen(base + "/api/file?run=" + urllib.parse.quote(rd) + "&path=" + bad_path)
                    ok(False, f"RC10e a path outside the run dir is refused ({bad_path})")
                except urllib.error.HTTPError as e:
                    ok(e.code == 400, f"RC10e a path outside the run dir is refused ({bad_path})")
            req = urllib.request.Request(base + "/api/feedback", data=json.dumps(
                {"run": rd, "kind": "question", "layer": "process", "text": "x"}).encode(), method="POST")
            try:
                urllib.request.urlopen(req)
                ok(False, "RC10f a POST without the console token is refused")
            except urllib.error.HTTPError as e:
                ok(e.code == 403, "RC10f a POST without the console token is refused")
            req.add_header("X-Console-Token", con.token)
            ok(json.load(urllib.request.urlopen(req))["kind"] == "question", "RC10g a POST with the token is accepted")
            cloud = json.load(urllib.request.urlopen(base + "/api/cloud?live=1"))
            ok(cloud["available"] is False and "not available" in cloud["why"], "RC10h no cloud config: not available")
        finally:
            srv.shutdown()
            srv.server_close()
        # RC11 the cloud panel: read-only commands, at most once a minute, only while live
        seen, t = [], [1000.0]
        cp = CloudPanel("cfg.py", runner=lambda cmd, **k: seen.append(cmd[2:]) or NS(
            returncode=0, stdout="cluster ma-x: running; keeper: running; overflow: stopped\n", stderr=""),
            clock=lambda: t[0], coiled_ok=True)
        s0 = cp.snapshot(False)
        s1 = cp.snapshot(True)
        t[0] += 30
        cp.snapshot(True)
        t[0] += 31
        cp.snapshot(True)
        ok(not seen[:0] and s0["snapshot"] is None and s1["snapshot"]["clusters"]["run"]["state"] == "running"
           and [c[1] for c in seen] == ["status", "health", "report-cost"] * 2,
           f"RC11 cloud refresh: only when live, never within 60 s, status/health/report-cost only ({seen})")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-dir", action="append", default=[], help="a run directory to open (repeatable)")
    ap.add_argument("--roots", nargs="*", default=None, help="directories searched for runs (default: runs/)")
    ap.add_argument("--mirror", help="a local mirror of a remote run tree (read-only for the console)")
    ap.add_argument("--sync-cmd", help="a command that refreshes --mirror (run every --sync-s seconds)")
    ap.add_argument("--sync-s", type=float, default=120.0)
    ap.add_argument("--cloud-config", help="cloud/configs/<x>.py: the remote sign path and the compute panel")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--chat-cap-usd", type=float, default=2.0, help="chat spend cap for this session (default $2)")
    ap.add_argument("--core-hours-cap", type=float, default=None,
                    help="the run's MA_COMPUTE_MAX_CORE_HOURS, if not the default")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)
    if a.self_test:
        return _self_test()
    if a.sync_cmd and not a.mirror:
        ap.error("--sync-cmd needs --mirror")
    roots = a.roots if a.roots is not None else [os.path.join(REPO, "runs")]
    con = Console(roots, a.run_dir, a.mirror, a.sync_cmd, a.sync_s, a.cloud_config, a.chat_cap_usd, a.core_hours_cap)
    if a.sync_cmd:
        threading.Thread(target=con.sync_loop, daemon=True).start()
    srv = make_server(con, "127.0.0.1", a.port)
    print(f"Run console: http://127.0.0.1:{srv.server_address[1]}/  (chat cap ${a.chat_cap_usd:.2f}; "
          f"chat {'on' if os.environ.get('ANTHROPIC_API_KEY') else 'off: ANTHROPIC_API_KEY is not set'})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
